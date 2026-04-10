from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import sync_playwright

from automation_tool.browser_client import BrowserClient, is_service_responding, try_attach_playwright_via_service
from automation_tool.browser_protocol import METHOD_CLOSE_TAB, METHOD_TV_WATCHLIST_INIT, METHOD_TV_WATCHLIST_POLL
from automation_tool.coinmap import (
    _maybe_tradingview_dark_mode,
    _maybe_tradingview_login,
    load_coinmap_yaml,
)
from automation_tool.coinmap import _tradingview_ensure_watchlist_open  # reuse internal helper
from automation_tool.config import Settings
from automation_tool.images import DEFAULT_MAIN_CHART_SYMBOL, get_active_main_symbol
from automation_tool.mt5_execute import execute_trade, format_mt5_execution_for_telegram
from automation_tool.mt5_openai_parse import (
    parse_journal_intraday_action_from_openai_text,
    parse_openai_output_md,
)
from automation_tool.mt5_manage import mt5_cancel_pending_or_close_position
from automation_tool.openai_errors import re_raise_unless_openai
from automation_tool.openai_prompt_flow import TP1_POST_TOUCH_USER_TEMPLATE, run_single_followup_responses
from automation_tool.playwright_browser import close_browser_and_context, launch_chrome_context
from automation_tool.state_files import read_last_response_id, write_last_response_id
from automation_tool.telegram_bot import (
    send_message,
    send_mt5_execution_log_to_ngan_gon_chat,
    send_openai_output_to_telegram,
)
from automation_tool.tradingview_last_price import read_watchlist_last_price_wait_stable
from automation_tool.openai_analysis_json import auto_mt5_hop_luu_threshold_for_label, parse_analysis_from_openai_text
from automation_tool.zones_state import Zone, ZonesState, read_zones_state, upsert_last_observed, write_zones_state

_log = logging.getLogger("automation_tool.tv_watchlist_daemon")

_EPS_DEFAULT = 0.01
_TP1_EPS = 0.01
_ARM_THRESHOLD = 5.0
_RETRY_WAIT_MINUTES = 15


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _retry_at_iso(minutes: int = _RETRY_WAIT_MINUTES) -> str:
    return (_now_utc() + timedelta(minutes=int(minutes))).isoformat()


def _is_retry_due(retry_at: str) -> bool:
    s = (retry_at or "").strip()
    if not s:
        return False
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt <= _now_utc()
    except Exception:
        return False


@dataclass(frozen=True)
class WatchlistDaemonParams:
    coinmap_tv_yaml: Path
    capture_coinmap_yaml: Path
    charts_dir: Path
    storage_state_path: Optional[Path]
    headless: bool
    no_save_storage: bool
    poll_seconds: float = 10.0
    timezone_name: str = "Asia/Ho_Chi_Minh"
    no_telegram: bool = False
    mt5_execute: bool = True
    mt5_symbol: Optional[str] = None
    mt5_dry_run: bool = False
    zones_state_path: Optional[Path] = None
    eps: float = _EPS_DEFAULT


def _send_log(settings: Settings, text: str) -> None:
    """
    Best-effort: send plain text to TELEGRAM_LOG_CHAT_ID.
    """
    cid = (settings.telegram_log_chat_id or "").strip()
    if not cid:
        return
    body = (text or "").strip()
    if not body:
        return
    try:
        send_message(
            bot_token=settings.telegram_bot_token,
            chat_id=cid,
            text=body,
            parse_mode=None,
        )
    except Exception:
        # Never let logging break the daemon.
        return


def _touch_prompt(
    *,
    zone: Zone,
    last_price: float,
) -> str:
    """
    Ask OpenAI to output a single JSON object.
    We need:
    - intraday_hanh_dong: "VÀO LỆNH" | "chờ" | "loại"
    - trade_line: required when "VÀO LỆNH"
    - prices: include a matching entry for this zone label with `hop_luu` so daemon can
      decide whether to execute MT5 (plan >=80, scalp >=70).
    - optional out_chi_tiet/output_ngan_gon
    """
    return (
        "Bạn là trợ lý giao dịch intraday.\n"
        "Dưới đây là 1 vùng giá (zone) đã CHẠM theo TradingView Watchlist Last.\n\n"
        f"- label: {zone.label}\n"
        f"- side (gợi ý): {zone.side}\n"
        f"- alert_price: {zone.alert_price}\n"
        f"- range_low: {zone.range_low}\n"
        f"- range_high: {zone.range_high}\n"
        f"- last_price (watchlist): {last_price}\n\n"
        "Hãy xem Coinmap M5 JSON đính kèm và trả về 1 JSON object DUY NHẤT với keys:\n"
        '- "intraday_hanh_dong": "VÀO LỆNH" | "chờ" | "loại"\n'
        '- "trade_line": string (bắt buộc nếu intraday_hanh_dong là "VÀO LỆNH")\n'
        '- "prices": array gồm ít nhất 1 phần tử cho đúng label ở trên, ví dụ:\n'
        '  [{"label": "plan_chinh", "value": 0, "hop_luu": 85, "trade_line": "..."}]\n'
        "  (value có thể để 0; quan trọng là hop_luu + trade_line theo đúng label)\n"
        '- "output_ngan_gon": string (tùy chọn)\n'
        '- "out_chi_tiet": string (tùy chọn)\n'
    )


def _mark_zone_status(
    *,
    st: ZonesState,
    zone_id: str,
    new_status: str,
) -> ZonesState:
    for z in st.zones:
        if z.id == zone_id:
            z.status = new_status  # type: ignore[assignment]
            break
    return st


def _parse_trade_from_zone_trade_line(trade_line: str, *, symbol_override: Optional[str]) -> tuple[object, Optional[str]]:
    """
    Reuse existing `parse_openai_output_md` by wrapping trade_line into minimal JSON.
    Returns (parsed, err). Parsed is the ParsedTrade-like object used by execute_trade.
    """
    tl = (trade_line or "").strip()
    if not tl:
        return None, "trade_line is empty"
    minimal = json.dumps({"intraday_hanh_dong": "VÀO LỆNH", "trade_line": tl}, ensure_ascii=False)
    return parse_openai_output_md(minimal, symbol_override=symbol_override)


def _entry_reference_price(parsed) -> float:
    if getattr(parsed, "kind", "") == "MARKET" or getattr(parsed, "price", None) is None:
        return (float(parsed.sl) + float(parsed.tp1)) / 2.0
    return float(parsed.price)


def _arm_threshold_met(parsed, p_last: float) -> bool:
    """
    Arm rule (per requirement):
    - BUY: (Last - range_high) >= +5
    - SELL: (Last - range_low)  <= -5
    """
    # parsed is derived from trade_line; range info lives on the zone, not parsed.
    # This function is kept for backward compatibility but is no longer used for arming.
    ref = _entry_reference_price(parsed)
    if getattr(parsed, "side", "") == "BUY":
        return p_last > ref + _ARM_THRESHOLD
    return p_last < ref - _ARM_THRESHOLD


def _arm_threshold_met_for_zone(zone: Zone, p_last: float) -> bool:
    """
    Arm condition based on zone range edges + side:
    - SELL: Last - range_low <= -5  (Last <= range_low - 5)
    - BUY:  Last - range_high >= +5 (Last >= range_high + 5)
    """
    side = (zone.side or "").strip().upper()
    if side == "SELL":
        return (float(p_last) - float(zone.range_low)) <= (-_ARM_THRESHOLD)
    # default BUY
    return (float(p_last) - float(zone.range_high)) >= _ARM_THRESHOLD


def _tp1_touched(parsed, p_last: float) -> bool:
    tp = float(parsed.tp1)
    if getattr(parsed, "side", "") == "BUY":
        return p_last >= tp - _TP1_EPS
    return p_last <= tp + _TP1_EPS


def _tp1_followup_job(
    *,
    settings: Settings,
    params: WatchlistDaemonParams,
    zone_id: str,
    p_last: float,
) -> None:
    """
    Follow-up after TP1 touch:
    - capture Coinmap M5
    - call OpenAI TP1 template
    - parse decision: loai | chinh_trade_line
    - act on MT5 and update zones_state
    """
    zs_path = params.zones_state_path
    try:
        st0 = read_zones_state(zs_path)
        if st0 is None:
            return
        z0 = next((z for z in st0.zones if z.id == zone_id), None)
        if z0 is None:
            return
        if z0.status in ("done", "loai"):
            return
        if not z0.trade_line or not z0.mt5_ticket:
            z0.status = "cho_tp1"
            z0.tp1_followup_done = False
            write_zones_state(st0, path=zs_path)
            return

        parsed, err = _parse_trade_from_zone_trade_line(z0.trade_line, symbol_override=params.mt5_symbol)
        if err or parsed is None:
            z0.tp1_followup_done = False
            write_zones_state(st0, path=zs_path)
            return

        from automation_tool.coinmap import capture_charts
        from automation_tool.images import coinmap_xauusd_5m_json_path, read_main_chart_symbol
        from automation_tool.tp1_followup import parse_tp1_followup_decision

        capture_charts(
            coinmap_yaml=params.capture_coinmap_yaml,
            charts_dir=params.charts_dir,
            storage_state_path=params.storage_state_path,
            email=settings.coinmap_email,
            password=settings.coinmap_password,
            tradingview_password=settings.tradingview_password,
            save_storage_state=not params.no_save_storage,
            headless=params.headless,
            reuse_browser_context=None,
            main_chart_symbol=read_main_chart_symbol(params.charts_dir),
        )
        json_path = coinmap_xauusd_5m_json_path(params.charts_dir)
        if json_path is None or not json_path.is_file():
            raise SystemExit(f"tp1-followup: no main 5m Coinmap JSON under {params.charts_dir}")

        prev = read_last_response_id() or ""
        user_text = TP1_POST_TOUCH_USER_TEMPLATE.format(
            plan_label=z0.label,
            trade_line=z0.trade_line,
            last_price=p_last,
            tp1_price=getattr(parsed, "tp1", ""),
        )
        out_text, new_id = run_single_followup_responses(
            api_key=settings.openai_api_key,
            prompt_id=settings.openai_prompt_id,
            prompt_version=settings.openai_prompt_version,
            user_text=user_text,
            coinmap_json_path=json_path,
            previous_response_id=prev or None,
            vector_store_ids=settings.openai_vector_store_ids,
            store=settings.openai_responses_store,
            include=settings.openai_responses_include,
        )
        if new_id:
            write_last_response_id(new_id)

        if not params.no_telegram:
            send_openai_output_to_telegram(
                bot_token=settings.telegram_bot_token,
                chat_id=settings.telegram_chat_id,
                raw=out_text,
                default_parse_mode=settings.telegram_parse_mode,
                summary_chat_id=settings.telegram_output_ngan_gon_chat_id,
            )
        _send_log(settings, f"[tp1] openai_output_raw:\n{out_text}".strip())

        dec = parse_tp1_followup_decision(out_text)
        st1 = read_zones_state(zs_path)
        if st1 is None:
            return
        z1 = next((z for z in st1.zones if z.id == zone_id), None)
        if z1 is None:
            return

        # Mark handled so we don't spam.
        z1.tp1_followup_done = True

        if dec is None:
            # cannot parse -> allow retry later
            z1.tp1_followup_done = False
            z1.status = "cho_tp1"
            write_zones_state(st1, path=zs_path)
            return

        tk = int(z1.mt5_ticket or 0)
        dry = bool(params.mt5_dry_run)
        exe = bool(params.mt5_execute)

        if dec.sau_tp1 == "loại":
            if exe and tk > 0:
                r = mt5_cancel_pending_or_close_position(tk, dry_run=dry)
                _send_log(settings, f"[tp1] mt5_cancel_close: {r.message}".strip())
            z1.status = "loai"
            write_zones_state(st1, path=zs_path)
            return

        # chinh_trade_line
        if not dec.trade_line_moi.strip():
            z1.tp1_followup_done = False
            z1.status = "cho_tp1"
            write_zones_state(st1, path=zs_path)
            return

        # close old ticket then execute new trade line
        if exe and tk > 0:
            r0 = mt5_cancel_pending_or_close_position(tk, dry_run=dry)
            _send_log(settings, f"[tp1] mt5_close_old: {r0.message}".strip())

        minimal = json.dumps(
            {"intraday_hanh_dong": "VÀO LỆNH", "trade_line": dec.trade_line_moi.strip()},
            ensure_ascii=False,
        )
        new_parsed, err2 = parse_openai_output_md(minimal, symbol_override=params.mt5_symbol)
        if err2 or new_parsed is None:
            z1.tp1_followup_done = False
            z1.status = "cho_tp1"
            write_zones_state(st1, path=zs_path)
            return

        if exe:
            ex = execute_trade(
                new_parsed,
                dry_run=dry,
                symbol_override=params.mt5_symbol,
            )
            if not params.no_telegram:
                send_mt5_execution_log_to_ngan_gon_chat(
                    bot_token=settings.telegram_bot_token,
                    telegram_chat_id=settings.telegram_chat_id,
                    source="tp1-followup",
                    text=format_mt5_execution_for_telegram(ex),
                )
            _send_log(settings, f"[tp1] mt5_execute_trade: {ex.message}".strip())
            tid = int(ex.order) if ex.order else 0
            if ex.ok and tid > 0:
                z1.mt5_ticket = tid
        z1.trade_line = dec.trade_line_moi.strip()
        z1.status = "vao_lenh"
        z1.tp1_followup_done = False
        write_zones_state(st1, path=zs_path)
        return
    except Exception as e:
        _send_log(settings, f"[tp1] ERROR | zone_id={zone_id} | {e!s}")
        re_raise_unless_openai(e)


def _auto_entry_job(
    *,
    settings: Settings,
    params: WatchlistDaemonParams,
    zone_id: str,
) -> None:
    """
    Fire-and-forget worker:
    - re-check zone has hop_luu above threshold + has trade_line + no mt5_ticket
    - execute MT5 and persist mt5_ticket + status=vao_lenh
    """
    zs_path = params.zones_state_path
    try:
        st0 = read_zones_state(zs_path)
        if st0 is None:
            return
        z0 = next((z for z in st0.zones if z.id == zone_id), None)
        if z0 is None:
            return
        if z0.status in ("done", "loai"):
            return
        # Only auto-entry from vung_cho or cham.
        if z0.status not in ("vung_cho", "cham"):
            return
        if z0.mt5_ticket is not None and int(z0.mt5_ticket or 0) > 0:
            return
        if not z0.trade_line:
            return
        if z0.hop_luu is None:
            return
        thr = int(auto_mt5_hop_luu_threshold_for_label(z0.label))
        if int(z0.hop_luu) <= thr:
            return
        if not params.mt5_execute:
            _send_log(settings, f"[auto-entry] mt5_execute=off | zone_id={zone_id} skip")
            return

        # Mark in-progress to prevent duplicate dispatch across ticks.
        z0.status = "dang_thuc_thi"
        write_zones_state(st0, path=zs_path)

        parsed, err = _parse_trade_from_zone_trade_line(z0.trade_line, symbol_override=params.mt5_symbol)
        if err or parsed is None:
            st1 = read_zones_state(zs_path)
            if st1 is not None:
                for z in st1.zones:
                    if z.id == zone_id:
                        z.status = "cham"
                        break
                write_zones_state(st1, path=zs_path)
            _send_log(settings, f"[auto-entry] parse_trade_line_failed | zone_id={zone_id} err={err}")
            return

        ex = execute_trade(
            parsed,
            dry_run=params.mt5_dry_run,
            symbol_override=params.mt5_symbol,
        )
        if not params.no_telegram:
            send_mt5_execution_log_to_ngan_gon_chat(
                bot_token=settings.telegram_bot_token,
                telegram_chat_id=settings.telegram_chat_id,
                source="auto-entry",
                text=format_mt5_execution_for_telegram(ex),
            )
        _send_log(settings, f"[auto-entry] mt5_execute_trade: {ex.message}".strip())

        tid = int(ex.order) if ex.order else 0
        st2 = read_zones_state(zs_path)
        if st2 is None:
            return
        for z in st2.zones:
            if z.id != zone_id:
                continue
            if ex.ok and tid > 0:
                z.mt5_ticket = tid
                z.status = "vao_lenh"
            else:
                # allow retry via touch flow or manual; keep cham for visibility
                z.status = "cham"
            break
        write_zones_state(st2, path=zs_path)
        return
    except Exception as e:
        _send_log(settings, f"[auto-entry] ERROR | zone_id={zone_id} | {e!s}")
        re_raise_unless_openai(e)


def _zone_touch_job(
    *,
    settings: Settings,
    params: WatchlistDaemonParams,
    zone_id: str,
    last_price: float,
) -> None:
    """
    Fire-and-forget worker:
    - capture Coinmap M5
    - call OpenAI follow-up
    - update zone status + trade_line + mt5 ticket (optional)
    """
    zs_path = params.zones_state_path
    st0 = read_zones_state(zs_path)
    if st0 is None:
        return
    zone = next((z for z in st0.zones if z.id == zone_id), None)
    if zone is None:
        return

    try:
        _send_log(
            settings,
            f"[zone-touch] start | zone_id={zone_id} label={zone.label} "
            f"alert={zone.alert_price} range=[{zone.range_low},{zone.range_high}] last={last_price}",
        )

        loai_confirm_rounds = 4

        st_check = read_zones_state(zs_path)
        if st_check is None:
            return
        zc = next((z for z in st_check.zones if z.id == zone_id), None)
        if zc is None:
            return
        # If user manually marked terminal states while job is running, stop.
        if zc.status in ("done", "loai"):
            _send_log(settings, f"[zone-touch] stop: zone already terminal ({zc.status}) | zone_id={zone_id}")
            return
        zone = zc

        # Mark running (anti-spam + visibility). Daemon will handle retries using retry_at.
        zone.status = "dang_thuc_thi"
        zone.retry_at = ""
        write_zones_state(st_check, path=zs_path)

        # Capture Coinmap (reuse capture pipeline)
        from automation_tool.coinmap import capture_charts
        from automation_tool.images import coinmap_xauusd_5m_json_path, read_main_chart_symbol

        capture_charts(
            coinmap_yaml=params.capture_coinmap_yaml,
            charts_dir=params.charts_dir,
            storage_state_path=params.storage_state_path,
            email=settings.coinmap_email,
            password=settings.coinmap_password,
            tradingview_password=settings.tradingview_password,
            save_storage_state=not params.no_save_storage,
            headless=params.headless,
            reuse_browser_context=None,
            main_chart_symbol=read_main_chart_symbol(params.charts_dir),
        )
        json_path = coinmap_xauusd_5m_json_path(params.charts_dir)
        if json_path is None or not json_path.is_file():
            raise SystemExit(f"zone-touch: no main 5m Coinmap JSON under {params.charts_dir}")

        _send_log(settings, f"[zone-touch] coinmap_m5_json={json_path}")

        prev = read_last_response_id() or ""
        user_text = _touch_prompt(zone=zone, last_price=last_price)
        out_text, new_id = run_single_followup_responses(
            api_key=settings.openai_api_key,
            prompt_id=settings.openai_prompt_id,
            prompt_version=settings.openai_prompt_version,
            user_text=user_text,
            coinmap_json_path=json_path,
            previous_response_id=prev or None,
            vector_store_ids=settings.openai_vector_store_ids,
            store=settings.openai_responses_store,
            include=settings.openai_responses_include,
        )
        if new_id:
            write_last_response_id(new_id)
            _send_log(settings, f"[zone-touch] openai_response_id={new_id}")

        act = parse_journal_intraday_action_from_openai_text(out_text)
        # Default to "chờ" on parse failure
        if act is None:
            act = "chờ"

            # Always forward OpenAI output to Telegram if enabled:
            # - short/main channels as configured
            # - full raw log to TELEGRAM_LOG_CHAT_ID
            if not params.no_telegram:
                send_openai_output_to_telegram(
                    bot_token=settings.telegram_bot_token,
                    chat_id=settings.telegram_chat_id,
                    raw=out_text,
                    default_parse_mode=settings.telegram_parse_mode,
                    summary_chat_id=settings.telegram_output_ngan_gon_chat_id,
                )
            _send_log(settings, f"[zone-touch] openai_output_raw:\n{out_text}".strip())

        st1 = read_zones_state(zs_path)
        if st1 is None:
            return
        z1 = next((z for z in st1.zones if z.id == zone_id), None)
        if z1 is None:
            return

        if act == "loại":
            z1.loai_streak = int(getattr(z1, "loai_streak", 0) or 0) + 1
            if z1.loai_streak >= loai_confirm_rounds:
                z1.status = "loai"
                z1.retry_at = ""
                write_zones_state(st1, path=zs_path)
                _send_log(
                    settings,
                    f"[zone-touch] act=loai confirm {z1.loai_streak}/{loai_confirm_rounds} "
                    f"| zone_id={zone_id} -> status=loai",
                )
                return
            # keep touched state; daemon will re-dispatch after retry_at
            z1.status = "cham"
            z1.retry_at = _retry_at_iso()
            write_zones_state(st1, path=zs_path)
            _send_log(
                settings,
                f"[zone-touch] act=loai confirm {z1.loai_streak}/{loai_confirm_rounds} "
                f"| zone_id={zone_id} -> status=cham retry_at={z1.retry_at}",
            )
            return

        # Any non-loai action resets loai_streak.
        z1.loai_streak = 0
        z1.tp1_followup_done = False

        if act != "VÀO LỆNH":
            # keep touched state (no revert to vung_cho); daemon can retry later
            z1.status = "cham"
            z1.retry_at = _retry_at_iso()
            write_zones_state(st1, path=zs_path)
            _send_log(
                settings,
                f"[zone-touch] act={act} | zone_id={zone_id} -> status=cham retry_at={z1.retry_at}",
            )
            return

            # Parse hop_luu from the OpenAI JSON so daemon can gate MT5 entry.
            hop_luu: Optional[int] = None
            try:
                payload = parse_analysis_from_openai_text(out_text)
                if payload is not None and payload.prices:
                    for pe in payload.prices:
                        if pe.label.strip().lower() == z1.label.strip().lower():
                            hop_luu = pe.hop_luu
                            break
            except Exception:
                hop_luu = None

            thr = int(auto_mt5_hop_luu_threshold_for_label(z1.label))
            hop_ok = hop_luu is not None and int(hop_luu) > thr
            if not hop_ok:
                z1.status = "cham"
                z1.retry_at = _retry_at_iso()
                write_zones_state(st1, path=zs_path)
                _send_log(
                    settings,
                    f"[zone-touch] hop_luu_gate_failed | zone_id={zone_id} label={z1.label} "
                    f"hop_luu={hop_luu} thr(>)={thr} -> status=cham retry_at={z1.retry_at} (skip MT5)",
                )
                return

            parsed, err = parse_openai_output_md(out_text, symbol_override=params.mt5_symbol)
            if err or parsed is None:
                # couldn't parse trade_line -> keep touched state
                z1.status = "cham"
                z1.retry_at = _retry_at_iso()
                write_zones_state(st1, path=zs_path)
                _send_log(
                    settings,
                    f"[zone-touch] parse_trade_line_failed | err={err} | zone_id={zone_id} -> status=cham",
                )
                return

            z1.trade_line = (parsed.raw_line or "").strip()
            z1.status = "vao_lenh"
            write_zones_state(st1, path=zs_path)
            _send_log(
                settings,
                f"[zone-touch] act=VAO_LENH | zone_id={zone_id} -> status=vao_lenh | trade_line={z1.trade_line!r}",
            )

            if not params.mt5_execute:
                _send_log(settings, f"[zone-touch] mt5_execute=off | done | zone_id={zone_id}")
                return

            # Policy: only execute MT5 if we don't have a ticket yet.
            if z1.mt5_ticket is not None and int(z1.mt5_ticket or 0) > 0:
                _send_log(
                    settings,
                    f"[zone-touch] skip_mt5_execute | already_has_ticket | zone_id={zone_id} ticket={z1.mt5_ticket}",
                )
                return

            ex = execute_trade(
                parsed,
                dry_run=params.mt5_dry_run,
                symbol_override=params.mt5_symbol,
            )
            if not params.no_telegram:
                send_mt5_execution_log_to_ngan_gon_chat(
                    bot_token=settings.telegram_bot_token,
                    telegram_chat_id=settings.telegram_chat_id,
                    source="zone-touch",
                    text=format_mt5_execution_for_telegram(ex),
                )
            _send_log(settings, f"[zone-touch] mt5_execute_trade: {ex.message}".strip())
            tid = int(ex.order) if ex.order else 0
            if ex.ok and tid > 0:
                st2 = read_zones_state(zs_path)
                if st2 is None:
                    return
                for z in st2.zones:
                    if z.id == zone_id:
                        z.mt5_ticket = tid
                        break
                write_zones_state(st2, path=zs_path)
                _send_log(settings, f"[zone-touch] mt5_ticket_saved | zone_id={zone_id} ticket={tid}")
            return
    except Exception as e:
        # On any error: keep touched state (no revert to vung_cho); daemon will retry using retry_at
        try:
            stx = read_zones_state(zs_path)
            if stx is not None:
                for z in stx.zones:
                    if z.id == zone_id:
                        z.status = "cham"
                        z.retry_at = _retry_at_iso()
                        break
                write_zones_state(stx, path=zs_path)
        except Exception:
            pass
        _send_log(settings, f"[zone-touch] ERROR | zone_id={zone_id} | {e!s}")
        re_raise_unless_openai(e)


def _tv_watchlist_daemon_main_loop(
    *,
    settings: Settings,
    params: WatchlistDaemonParams,
    sym: str,
    poll_s: float,
    zs_path: Optional[Path],
    get_price: Callable[[int], Optional[float]],
) -> None:
    while True:
        st = read_zones_state(zs_path)
        if st is None or not st.zones:
            time.sleep(poll_s)
            continue

        wms = min(15_000, max(2_000, int(poll_s * 1000)))
        p_last = get_price(wms)
        if p_last is None:
            time.sleep(poll_s)
            continue

        # Best-effort: record last observed in zones_state.json
        try:
            upsert_last_observed(tv_watchlist_last=float(p_last), path=zs_path)
        except Exception:
            pass

        # Auto-entry: every tick, if zone has hop_luu above threshold and no mt5_ticket, enter immediately.
        st_auto = read_zones_state(zs_path)
        if st_auto is not None:
            for z in st_auto.zones:
                # Only auto-entry from vung_cho or cham.
                if z.status not in ("vung_cho", "cham"):
                    continue
                if z.mt5_ticket is not None and int(z.mt5_ticket or 0) > 0:
                    continue
                if not z.trade_line:
                    continue
                if z.hop_luu is None:
                    continue
                thr = int(auto_mt5_hop_luu_threshold_for_label(z.label))
                if int(z.hop_luu) <= thr:
                    continue
                # mark in-progress and persist before dispatch
                z.status = "dang_thuc_thi"
                write_zones_state(st_auto, path=zs_path)
                _send_log(
                    settings,
                    f"[auto-entry] dispatch | zone_id={z.id} label={z.label} hop_luu={z.hop_luu} thr(>)={thr}",
                )
                th0 = threading.Thread(
                    target=_auto_entry_job,
                    name=f"auto-entry-{z.id}-{uuid.uuid4().hex[:6]}",
                    daemon=True,
                    kwargs={"settings": settings, "params": params, "zone_id": z.id},
                )
                th0.start()

        # Retry touched zones: daemon re-dispatches zone-touch when retry_at is due.
        st_retry = read_zones_state(zs_path)
        if st_retry is not None:
            for z in st_retry.zones:
                if z.status != "cham":
                    continue
                if not _is_retry_due(getattr(z, "retry_at", "")):
                    continue
                z.status = "dang_thuc_thi"
                z.retry_at = ""
                write_zones_state(st_retry, path=zs_path)
                _send_log(settings, f"[zone-touch] retry_dispatch | zone_id={z.id} last={p_last}")
                th_retry = threading.Thread(
                    target=_zone_touch_job,
                    name=f"zone-touch-retry-{z.id}-{uuid.uuid4().hex[:6]}",
                    daemon=True,
                    kwargs={
                        "settings": settings,
                        "params": params,
                        "zone_id": z.id,
                        "last_price": float(p_last),
                    },
                )
                th_retry.start()

        # match zones by alert_price touch only
        matched: list[Zone] = []
        for z in st.zones:
            if z.status != "vung_cho":
                continue
            if abs(float(p_last) - float(z.alert_price)) <= float(params.eps):
                matched.append(z)

        for z in matched:
            # mark touched and persist before dispatch
            z.status = "cham"
            write_zones_state(st, path=zs_path)
            th = threading.Thread(
                target=_zone_touch_job,
                name=f"zone-touch-{z.id}-{uuid.uuid4().hex[:6]}",
                daemon=True,
                kwargs={
                    "settings": settings,
                    "params": params,
                    "zone_id": z.id,
                    "last_price": float(p_last),
                },
            )
            th.start()

        # TP1 tick/dispatch for post-entry zones
        st_tp1 = read_zones_state(zs_path)
        if st_tp1 is not None:
            # 1) arm: vao_lenh -> cho_tp1
            changed = False
            for z in st_tp1.zones:
                if z.status != "vao_lenh":
                    continue
                if not z.trade_line or not z.mt5_ticket or int(z.mt5_ticket) <= 0:
                    continue
                if _arm_threshold_met_for_zone(z, float(p_last)):
                    z.status = "cho_tp1"
                    z.tp1_followup_done = False
                    changed = True
                    _send_log(settings, f"[tp1] arm | zone_id={z.id} vao_lenh->cho_tp1 last={p_last}")
            if changed:
                write_zones_state(st_tp1, path=zs_path)

            # 2) follow-up when touched TP1
            st_tp1b = read_zones_state(zs_path)
            if st_tp1b is not None:
                for z in st_tp1b.zones:
                    if z.status != "cho_tp1":
                        continue
                    if z.tp1_followup_done:
                        continue
                    if not z.trade_line or not z.mt5_ticket or int(z.mt5_ticket) <= 0:
                        continue
                    parsed, err = _parse_trade_from_zone_trade_line(
                        z.trade_line, symbol_override=params.mt5_symbol
                    )
                    if err or parsed is None:
                        continue
                    if not _tp1_touched(parsed, float(p_last)):
                        continue
                    # mark in-progress to prevent duplicate dispatch
                    z.status = "dang_thuc_thi"
                    z.tp1_followup_done = True
                    write_zones_state(st_tp1b, path=zs_path)
                    _send_log(settings, f"[tp1] touched | zone_id={z.id} -> dispatch followup last={p_last}")
                    th2 = threading.Thread(
                        target=_tp1_followup_job,
                        name=f"tp1-{z.id}-{uuid.uuid4().hex[:6]}",
                        daemon=True,
                        kwargs={
                            "settings": settings,
                            "params": params,
                            "zone_id": z.id,
                            "p_last": float(p_last),
                        },
                    )
                    th2.start()

        time.sleep(poll_s)


def _tv_watchlist_rpc_poll_price(
    client: BrowserClient,
    *,
    tab_id: str,
    tv: dict[str, Any],
    sym: str,
    wms: int,
) -> Optional[float]:
    timeout_rpc = max(180.0, float(wms) / 1000.0 + 90.0)
    resp = client.request(
        METHOD_TV_WATCHLIST_POLL,
        {
            "tab_id": tab_id,
            "tv": tv,
            "symbol": sym,
            "timeout_ms": wms,
            "poll_ms": 250,
        },
        timeout_s=timeout_rpc,
    )
    if not resp.get("ok"):
        _log.warning("tv_watchlist_poll RPC failed: %s", resp.get("error"))
        return None
    raw = (resp.get("result") or {}).get("price")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def run_tv_watchlist_daemon(
    *,
    settings: Settings,
    params: WatchlistDaemonParams,
) -> str:
    cfg = load_coinmap_yaml(params.coinmap_tv_yaml)
    tv = cfg.get("tradingview_capture") or {}
    if not isinstance(tv, dict) or not tv.get("chart_url"):
        raise SystemExit("tradingview_capture.chart_url missing in coinmap yaml.")

    poll_s = float(params.poll_seconds or 10.0)
    if poll_s <= 0:
        poll_s = 10.0

    # Which symbol to read from watchlist?
    sym = (tv.get("watchlist_symbol_short") or "").strip().upper()
    if not sym or sym == DEFAULT_MAIN_CHART_SYMBOL:
        sym = get_active_main_symbol().strip().upper()

    zs_path = params.zones_state_path

    _log.info(
        "tv-watchlist-daemon start | symbol=%s poll=%.1fs zones_state=%s",
        sym,
        poll_s,
        zs_path or "(default)",
    )

    # Browser service up: drive TradingView entirely via RPC (no second CDP client in this process).
    if is_service_responding():
        c = BrowserClient.from_state_file()
        if not c:
            raise SystemExit("browser service state missing; run: coinmap-automation browser up")
        _log.info("tv-watchlist-daemon mode=rpc | no local Playwright CDP attach")
        init = c.request(
            METHOD_TV_WATCHLIST_INIT,
            {
                "chart_url": str(tv.get("chart_url")),
                "tv": tv,
                "email": settings.coinmap_email,
                "password": settings.tradingview_password,
                "initial_settle_ms": int(tv.get("initial_settle_ms", 3000)),
            },
            timeout_s=600.0,
        )
        if not init.get("ok"):
            raise SystemExit(f"tv_watchlist_init failed: {init.get('error')}")
        tab_id = str((init.get("result") or {}).get("tab_id") or "").strip()
        if not tab_id:
            raise SystemExit("tv_watchlist_init: missing tab_id")
        try:
            _tv_watchlist_daemon_main_loop(
                settings=settings,
                params=params,
                sym=sym,
                poll_s=poll_s,
                zs_path=zs_path,
                get_price=lambda wms: _tv_watchlist_rpc_poll_price(
                    c, tab_id=tab_id, tv=tv, sym=sym, wms=wms
                ),
            )
        finally:
            try:
                c.request(METHOD_CLOSE_TAB, {"tab_id": tab_id}, timeout_s=60.0)
            except OSError:
                pass
        return "stopped"

    with sync_playwright() as p:
        _log.info("tv-watchlist-daemon playwright ready | attempting_attach_via_service=true")
        attached = try_attach_playwright_via_service(p)
        if attached is not None:
            browser, context = attached
            use_browser_service = True
            _log.info("tv-watchlist-daemon using_browser_service=true (local CDP; rare if service down)")
        else:
            _log.info("tv-watchlist-daemon using_browser_service=false | fallback=launch_chrome_context")
            browser, context = launch_chrome_context(
                p,
                headless=params.headless,
                storage_state_path=params.storage_state_path,
                viewport_width=int(cfg.get("viewport_width", 1920)),
                viewport_height=int(cfg.get("viewport_height", 1080)),
            )
            use_browser_service = False
        page = context.new_page()
        try:
            page.goto(str(tv.get("chart_url")), wait_until="domcontentloaded", timeout=120_000)
            _maybe_tradingview_login(page, tv, settings.coinmap_email, settings.tradingview_password)
            page.wait_for_timeout(int(tv.get("initial_settle_ms", 3000)))
            _maybe_tradingview_dark_mode(page, tv)
            _tradingview_ensure_watchlist_open(page, tv)

            _tv_watchlist_daemon_main_loop(
                settings=settings,
                params=params,
                sym=sym,
                poll_s=poll_s,
                zs_path=zs_path,
                get_price=lambda wms: read_watchlist_last_price_wait_stable(
                    page, tv, symbol=sym, timeout_ms=wms, poll_ms=250
                ),
            )
        finally:
            try:
                page.close()
            except Exception:
                pass
            if use_browser_service:
                try:
                    browser.close()
                except Exception:
                    pass
            else:
                close_browser_and_context(browser, context)
    return "stopped"

