from __future__ import annotations

import json
import logging
import math
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import sync_playwright

from automation_tool.browser_client import BrowserClient, is_service_responding, try_attach_playwright_via_service
from automation_tool.browser_protocol import (
    METHOD_CLOSE_TAB,
    METHOD_EVAL,
    METHOD_TV_WATCHLIST_INIT,
    METHOD_TV_WATCHLIST_POLL,
)
from automation_tool.coinmap import (
    _maybe_tradingview_dark_mode,
    _maybe_tradingview_login,
    load_coinmap_yaml,
)
from automation_tool.coinmap import _tradingview_ensure_watchlist_open  # reuse internal helper
from automation_tool.config import Settings, resolved_model_for_intraday_alert
from automation_tool.images import DEFAULT_MAIN_CHART_SYMBOL, get_active_main_symbol
from automation_tool.mt5_execute import execute_trade, format_mt5_execution_for_telegram
from automation_tool.mt5_openai_parse import (
    is_last_price_hit_stop_loss,
    parse_journal_intraday_action_from_openai_text,
    parse_openai_output_md,
)
from automation_tool.mt5_manage import mt5_cancel_pending_or_close_position, mt5_ticket_still_open
from automation_tool.openai_errors import re_raise_unless_openai
from automation_tool.openai_prompt_flow import TP1_POST_TOUCH_USER_TEMPLATE, run_single_followup_responses
from automation_tool.playwright_browser import close_browser_and_context, launch_chrome_context
from automation_tool.state_files import read_last_response_id
from automation_tool.telegram_bot import (
    send_message,
    send_mt5_execution_log_to_ngan_gon_chat,
    send_openai_output_to_telegram,
    send_phan_tich_alert_to_main_chat_if_any,
    send_user_friendly_notice,
)
from automation_tool.openai_analysis_json import (
    ARM_THRESHOLD_TP1_DEFAULT,
    arm_threshold_tp1_for_label,
    auto_mt5_hop_luu_threshold_for_label,
    parse_vung_cho_bounds,
)
from automation_tool.daemon_launcher import (
    reconcile_daemon_plans_at_boot,
    register_daemon_plan_pidfile_for_current_process,
    register_stop_daemon_plans_on_exit,
)
from automation_tool.last_price_ipc import (
    open_writer_shared_memory,
    read_last_price_for_daemon_plan,
    write_last_price_shared,
)
from automation_tool.zones_paths import default_last_price_path, default_zones_dir, read_last_price_file, write_last_price_file
from automation_tool.zones_state import (
    Zone,
    ZonesState,
    read_zones_state,
    read_zones_state_from_shard,
    write_zones_state,
    write_zones_state_to_shard,
)

_log = logging.getLogger("automation_tool.tv_watchlist_daemon")


def _poll_terminal_only_logger() -> logging.Logger:
    """
    Chỉ stderr — không propagate lên ``automation_tool`` → không qua TelegramLogHandler.
    Dùng cho tick mỗi vòng poll; heartbeat Telegram vẫn dùng ``_log.info`` (mỗi ~5 phút).
    """
    name = "automation_tool.tv_watchlist_daemon.poll_tick"
    lg = logging.getLogger(name)
    if lg.handlers:
        return lg
    lg.setLevel(logging.INFO)
    lg.propagate = False
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(message)s"))
    lg.addHandler(h)
    return lg


_poll_terminal = _poll_terminal_only_logger()

# After integer rounding of Last vs zone touch ref (from vung_cho + side): touch if abs(diff) <= this.
_EPS_DEFAULT = 1.0
_TP1_EPS = 0.01
# Re-export default cho test (plan_chinh / plan_phu).
_ARM_THRESHOLD = ARM_THRESHOLD_TP1_DEFAULT
_RETRY_WAIT_MINUTES = 15


_TV_TITLE_PRICE_RE = re.compile(r"^\s*(?P<sym>[A-Z0-9:_-]+)\s+(?P<price>\d[\d,]*(?:\.\d+)?)\b")


def _price_round_nearest_int(v: object) -> float:
    """
    Normalize price by rounding to the nearest whole number (integer), returned as float.
    Used for zone touch: compare Last vs side ref (BUY=max, SELL=min from ``vung_cho``) after this
    rounding; touch if ``abs(last_int - ref_int) <= eps`` (default eps=1 allows adjacent integers).
    """
    d = Decimal(str(v)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(d)


def _zone_side_ref_from_vung_cho(zone: Zone) -> Optional[float]:
    """
    Parse ``zone.vung_cho`` into (lo, hi); BUY uses max(hi), SELL uses min(lo) — same ref for touch and arm.
    """
    lo, hi = parse_vung_cho_bounds(zone.vung_cho)
    if lo is None or hi is None:
        return None
    side = (zone.side or "").strip().upper()
    if side == "SELL":
        return float(lo)
    return float(hi)


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
    poll_seconds: float = 1.0
    timezone_name: str = "Asia/Ho_Chi_Minh"
    no_telegram: bool = False
    mt5_execute: bool = True
    mt5_symbol: Optional[str] = None
    mt5_dry_run: bool = False
    zones_state_path: Optional[Path] = None
    """One shard JSON (``daemon-plan``); when set, read/write only this file."""
    shard_path: Optional[Path] = None
    """Optional ``last.txt`` when :attr:`mirror_last_price_file` is True; else IPC only."""
    last_price_path: Optional[Path] = None
    mirror_last_price_file: bool = False
    """Also write atomic ``last.txt`` (legacy/debug); primary Last is ``multiprocessing.shared_memory``."""
    stop_daemon_plans_on_exit: bool = False
    """On process exit (Ctrl+C, atexit, Windows console close): SIGTERM tracked ``daemon-plan`` PIDs."""
    eps: float = _EPS_DEFAULT  # max |Δ| between integer-rounded Last and touch ref (default 1.0)
    openai_model: Optional[str] = None
    openai_model_cli: Optional[str] = None


def _state_read(params: WatchlistDaemonParams) -> Optional[ZonesState]:
    if params.shard_path is not None:
        return read_zones_state_from_shard(params.shard_path)
    return read_zones_state(params.zones_state_path)


def _state_write(params: WatchlistDaemonParams, st: ZonesState) -> None:
    if params.shard_path is not None:
        write_zones_state_to_shard(params.shard_path, st)
    else:
        write_zones_state(st, path=params.zones_state_path)


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


def _send_user_notice(settings: Settings, title: str, body: str = "") -> None:
    """Tin ngắn tới TELEGRAM_PYTHON_BOT_CHAT_ID (non-tech)."""
    send_user_friendly_notice(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_python_bot_chat_id,
        title=title,
        body=body,
    )


def _touch_prompt(
    *,
    zone: Zone,
    last_price: float,
) -> str:
    """
    User turn for zone-touch OpenAI follow-up: ``[INTRADAY_ALERT]`` / **Schema E** (system prompt).

    Model chỉ cần ``phan_tich_alert`` + ``intraday_hanh_dong``. Khi ``VÀO LỆNH``.
    """
    ref = _zone_side_ref_from_vung_cho(zone)
    ref_bit = f" (mức so Last: {ref})" if ref is not None else ""
    return (
        "[INTRADAY_ALERT]\n"
        f"Cảnh báo chạm vùng chờ {zone.vung_cho}{ref_bit} (plan: {zone.label}).\n"
        "Footprint Coinmap M5 JSON đính kèm.\n"
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


def _maybe_loai_zone_if_last_hit_sl(
    zone: Zone,
    p_last: float,
    *,
    settings: Settings,
    params: WatchlistDaemonParams,
) -> bool:
    """
    ``vung_cho`` / ``cham``: nếu Last đã chạm/vượt mức SL trên ``trade_line``, loại vùng ngay.
    """
    tl = (zone.trade_line or "").strip()
    if not tl:
        return False
    parsed, err = _parse_trade_from_zone_trade_line(tl, symbol_override=params.mt5_symbol)
    if err or parsed is None:
        return False
    if not is_last_price_hit_stop_loss(float(p_last), parsed, eps=_TP1_EPS):
        return False
    zone.status = "loai"
    zone.loai_streak = 0
    _send_log(
        settings,
        f"[sl-hit] last={p_last} touched SL -> loai | zone_id={zone.id} label={zone.label} "
        f"sl={parsed.sl} side={parsed.side}",
    )
    _send_user_notice(
        settings,
        f"Loại plan {zone.label}: giá chạm SL ({parsed.sl}).",
        f"Last {p_last} — vùng chờ không còn hiệu lực.",
    )
    return True


def _daemon_plan_watch_telegram_text(
    z: Zone,
    *,
    sym: str,
    shard_tag: str,
    p_last: Optional[float],
) -> str:
    """Một dòng cho kênh log kỹ thuật: plan/shard đang theo dõi."""
    shard_name = Path(shard_tag).name
    last_s = f"{p_last}" if p_last is not None else "(none)"
    extra: list[str] = []
    if z.mt5_ticket is not None and int(z.mt5_ticket) > 0:
        extra.append(f"mt5={z.mt5_ticket}")
    if z.hop_luu is not None:
        extra.append(f"hop_luu={z.hop_luu}")
    vc = (z.vung_cho or "").replace("\n", " ").strip()
    if len(vc) > 100:
        vc = vc[:97] + "..."
    ref = _zone_side_ref_from_vung_cho(z)
    ref_s = f"{ref}" if ref is not None else "—"
    tail = " | ".join(extra) if extra else ""
    base = (
        f"[daemon-plan] watch | zone_id={z.id} | status={z.status} | last={last_s}"
    )
    return f"{base} | {tail}" if tail else base


def _entry_reference_price(parsed) -> float:
    if getattr(parsed, "kind", "") == "MARKET" or getattr(parsed, "price", None) is None:
        return (float(parsed.sl) + float(parsed.tp1)) / 2.0
    return float(parsed.price)


def _arm_threshold_met_for_zone(zone: Zone, p_last: float) -> bool:
    """
    Arm after entry: same side ref as touch (BUY=max, SELL=min from ``vung_cho``).
    Dải ±thr theo ``zone.label`` (scalp hẹp hơn plan_chinh / plan_phu).
    """
    thr = arm_threshold_tp1_for_label(zone.label)
    ref = _zone_side_ref_from_vung_cho(zone)
    if ref is None:
        return False
    diff = float(p_last) - ref
    side = (zone.side or "").strip().upper()
    if side == "SELL":
        return -thr <= diff <= 0.0
    return 0.0 <= diff <= thr


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
    try:
        st0 = _state_read(params)
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
            _state_write(params, st0)
            return

        parsed, err = _parse_trade_from_zone_trade_line(z0.trade_line, symbol_override=params.mt5_symbol)
        if err or parsed is None:
            z0.tp1_followup_done = False
            _state_write(params, st0)
            return

        tk_check = int(z0.mt5_ticket or 0)
        dry = bool(params.mt5_dry_run)
        exe = bool(params.mt5_execute)
        if exe and tk_check > 0:
            still_open, ticket_msg = mt5_ticket_still_open(tk_check, dry_run=dry)
            _send_log(settings, f"[tp1] kiểm tra ticket | {ticket_msg}")
            if not still_open:
                st_done = _state_read(params)
                if st_done is not None:
                    z_done = next((z for z in st_done.zones if z.id == zone_id), None)
                    if z_done is not None:
                        z_done.status = "done"
                        z_done.mt5_ticket = None
                        z_done.tp1_followup_done = True
                        _state_write(params, st_done)
                _send_log(
                    settings,
                    f"[tp1] bỏ qua follow-up TP1 (ticket đã đóng trên MT5) | zone_id={zone_id} | {ticket_msg}",
                )
                return

        # Scalp: chạm TP1 → huỷ ticket ngay, không gọi OpenAI / Coinmap.
        if (z0.label or "").strip().lower() == "scalp":
            tk = tk_check
            if exe and tk > 0:
                r = mt5_cancel_pending_or_close_position(tk, dry_run=dry)
                _send_log(settings, f"[tp1] scalp chạm TP1 — mt5_cancel_close: {r.message}".strip())
                if not params.no_telegram and settings.telegram_bot_token and settings.telegram_chat_id:
                    send_mt5_execution_log_to_ngan_gon_chat(
                        bot_token=settings.telegram_bot_token,
                        telegram_chat_id=settings.telegram_chat_id,
                        source="tp1-scalp-tp1",
                        text=f"scalp: chạm TP1 — huỷ ticket\n{r.message}",
                        zone_label="scalp",
                        trade_line=z0.trade_line,
                        execution_ok=r.ok,
                    )
            z0.status = "loai"
            z0.mt5_ticket = None
            z0.tp1_followup_done = True
            _state_write(params, st0)
            _send_user_notice(
                settings,
                "Scalp chạm TP1 — đã huỷ lệnh (không gọi AI).",
                "Vùng scalp chuyển trạng thái loại.",
            )
            return

        from automation_tool.coinmap import capture_charts
        from automation_tool.images import coinmap_xauusd_5m_json_path, read_main_chart_symbol
        from automation_tool.tp1_followup import parse_tp1_followup_decision

        _send_user_notice(
            settings,
            "Giá đã tới vùng theo dõi sau TP1.",
            "Đang lấy biểu đồ M5 và hỏi AI bước tiếp theo.",
        )

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
            coinmap_json_paths=[json_path],
            previous_response_id=prev or "",
            vector_store_ids=settings.openai_vector_store_ids,
            store=settings.openai_responses_store,
            include=settings.openai_responses_include,
            model=resolved_model_for_intraday_alert(settings, params.openai_model_cli),
        )
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
        st1 = _state_read(params)
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
            _state_write(params, st1)
            _send_user_notice(
                settings,
                "Sau TP1: không đọc được quyết định từ AI.",
                "Hệ thống sẽ thử lại — xem log kỹ thuật nếu cần chi tiết.",
            )
            return

        tk = int(z1.mt5_ticket or 0)
        dry = bool(params.mt5_dry_run)
        exe = bool(params.mt5_execute)

        if dec.sau_tp1 == "loại":
            if exe and tk > 0:
                r = mt5_cancel_pending_or_close_position(tk, dry_run=dry)
                _send_log(settings, f"[tp1] mt5_cancel_close: {r.message}".strip())
            z1.status = "loai"
            _state_write(params, st1)
            _send_user_notice(
                settings,
                "Sau TP1: AI chọn «loại» — đóng / bỏ theo dõi vùng.",
                "Đã gửi lệnh đóng trên MT5 nếu bật thực thi.",
            )
            return

        # chinh_trade_line
        if not dec.trade_line_moi.strip():
            z1.tp1_followup_done = False
            z1.status = "cho_tp1"
            _state_write(params, st1)
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
            _state_write(params, st1)
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
                    zone_label=z1.label,
                    trade_line=dec.trade_line_moi.strip(),
                    execution_ok=ex.ok,
                )
            _send_log(settings, f"[tp1] mt5_execute_trade: {ex.message}".strip())
            tid = int(ex.order) if ex.order else 0
            if ex.ok and tid > 0:
                z1.mt5_ticket = tid
            _tp1_lines = [ex.message]
            if ex.order:
                _tp1_lines.append(f"Mã lệnh: {ex.order}")
            if dry:
                _tp1_lines.append("(Chế độ thử.)")
            _send_user_notice(
                settings,
                "Sau TP1: đã đặt lệnh mới theo trade line cập nhật.",
                "\n".join(_tp1_lines),
            )
        z1.trade_line = dec.trade_line_moi.strip()
        z1.status = "vao_lenh"
        z1.tp1_followup_done = False
        _state_write(params, st1)
        return
    except Exception as e:
        _send_log(settings, f"[tp1] ERROR | zone_id={zone_id} | {e!s}")
        _send_user_notice(settings, "Lỗi khi xử lý bước sau TP1.", str(e))
        re_raise_unless_openai(e)


def _auto_entry_job(
    *,
    settings: Settings,
    params: WatchlistDaemonParams,
    zone_id: str,
) -> None:
    """
    Fire-and-forget worker:
    - Zone must already be ``dang_vao_lenh`` (main loop sets this before spawn; next poll skips duplicate dispatch).
    - Re-check hop_luu / trade_line / ticket; execute MT5; persist ``vao_lenh`` or revert to ``cham`` on failure.

    Does not use ``dang_thuc_thi``; that status remains for zone-touch / TP1 / other flows.
    """
    try:
        st0 = _state_read(params)
        if st0 is None:
            return
        z0 = next((z for z in st0.zones if z.id == zone_id), None)
        if z0 is None:
            return
        if z0.status != "dang_vao_lenh":
            return
        if z0.mt5_ticket is not None and int(z0.mt5_ticket or 0) > 0:
            return
        if not z0.trade_line:
            z0.status = "cham"
            z0.auto_entry_retry_after = ""
            _state_write(params, st0)
            return
        if z0.hop_luu is None:
            z0.status = "cham"
            z0.auto_entry_retry_after = ""
            _state_write(params, st0)
            return
        thr = int(auto_mt5_hop_luu_threshold_for_label(z0.label))
        if int(z0.hop_luu) <= thr:
            z0.status = "cham"
            z0.auto_entry_retry_after = ""
            _state_write(params, st0)
            return
        if not params.mt5_execute:
            _send_log(settings, f"[auto-entry] mt5_execute=off | zone_id={zone_id} skip")
            _send_user_notice(
                settings,
                "Tự động vào lệnh đang tắt.",
                "Vùng được giữ ở trạng thái chờ — bật thực thi MT5 nếu cần.",
            )
            z0.status = "cham"
            z0.auto_entry_retry_after = ""
            _state_write(params, st0)
            return

        parsed, err = _parse_trade_from_zone_trade_line(z0.trade_line, symbol_override=params.mt5_symbol)
        if err or parsed is None:
            st1 = _state_read(params)
            if st1 is not None:
                for z in st1.zones:
                    if z.id == zone_id:
                        z.status = "cham"
                        z.auto_entry_retry_after = ""
                        break
                _state_write(params, st1)
            _send_log(settings, f"[auto-entry] parse_trade_line_failed | zone_id={zone_id} err={err}")
            _send_user_notice(
                settings,
                "Tự động vào lệnh: không hiểu được dòng lệnh.",
                "Kiểm tra trade_line trong trạng thái vùng.",
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
                source="auto-entry",
                text=format_mt5_execution_for_telegram(ex),
                zone_label=z0.label,
                trade_line=z0.trade_line,
                execution_ok=ex.ok,
            )
        _send_log(settings, f"[auto-entry] mt5_execute_trade: {ex.message}".strip())
        _lines = [ex.message]
        if ex.order:
            _lines.append(f"Mã lệnh: {ex.order}")
        if params.mt5_dry_run:
            _lines.append("(Chế độ thử.)")
        _send_user_notice(
            settings,
            "Tự động vào lệnh — kết quả MT5",
            "\n".join(_lines),
        )

        tid = int(ex.order) if ex.order else 0
        st2 = _state_read(params)
        if st2 is None:
            return
        for z in st2.zones:
            if z.id != zone_id:
                continue
            if ex.ok and tid > 0:
                z.mt5_ticket = tid
                z.status = "vao_lenh"
                z.auto_entry_retry_after = ""
            else:
                # allow retry sau cooldown; tránh auto-entry lặp mỗi tick khi MT5 lỗi (vd. INVALID_PRICE)
                z.status = "cham"
                z.auto_entry_retry_after = _retry_at_iso()
                _send_log(
                    settings,
                    f"[auto-entry] mt5_failed -> cham cooldown_until={z.auto_entry_retry_after} | zone_id={zone_id}",
                )
            break
        _state_write(params, st2)
        return
    except Exception as e:
        _send_log(settings, f"[auto-entry] ERROR | zone_id={zone_id} | {e!s}")
        _send_user_notice(settings, "Lỗi khi tự động vào lệnh.", str(e))
        re_raise_unless_openai(e)


def _zone_touch_job(
    *,
    settings: Settings,
    params: WatchlistDaemonParams,
    zone_id: str,
    last_price: float,
    after_retry_wait: bool = False,
) -> None:
    """
    Fire-and-forget worker:
    - capture Coinmap M5
    - call OpenAI follow-up
    - update zone status + trade_line + mt5 ticket (optional)

    ``after_retry_wait``: True khi dispatch từ vòng ``cham`` sau khi hết ``retry_at`` (~15 phút),
    khác với lần chạm đầu từ ``vung_cho``.
    """
    st0 = _state_read(params)
    if st0 is None:
        return
    zone = next((z for z in st0.zones if z.id == zone_id), None)
    if zone is None:
        return

    try:
        ref = _zone_side_ref_from_vung_cho(zone)
        _send_log(
            settings,
            f"[zone-touch] start | zone_id={zone_id} label={zone.label} "
            f"vung_cho={zone.vung_cho} ref={ref} last={last_price}",
        )
        side_vn = "mua" if (zone.side or "").strip().upper() == "BUY" else "bán"
        _touch_title = (
            f"Sau {_RETRY_WAIT_MINUTES}p giá chạm vùng chờ ({zone.label})."
            if after_retry_wait
            else f"Giá đã chạm vùng chờ ({zone.label})."
        )
        _send_user_notice(
            settings,
            _touch_title,
            "Đang lấy dữ liệu biểu đồ M5 và phân tích lại với AI.",
        )

        loai_confirm_rounds = 6

        st_check = _state_read(params)
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
        _state_write(params, st_check)

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
            coinmap_json_paths=[json_path],
            previous_response_id=prev or "",
            vector_store_ids=settings.openai_vector_store_ids,
            store=settings.openai_responses_store,
            include=settings.openai_responses_include,
            model=resolved_model_for_intraday_alert(settings, params.openai_model_cli),
        )
        if new_id:
            _send_log(settings, f"[zone-touch] openai_response_id={new_id}")

        send_phan_tich_alert_to_main_chat_if_any(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            raw_openai_text=out_text,
            default_parse_mode=settings.telegram_parse_mode,
            no_telegram=params.no_telegram,
            alert_label=zone.label,
            alert_vung_cho=(zone.vung_cho or "").strip(),
        )

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

        st1 = _state_read(params)
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
                _state_write(params, st1)
                _send_log(
                    settings,
                    f"[zone-touch] act=loai confirm {z1.loai_streak}/{loai_confirm_rounds} "
                    f"| zone_id={zone_id} -> status=loai",
                )
                _send_user_notice(
                    settings,
                    "Vùng được đánh dấu «loại» sau nhiều lần xác nhận.",
                    "Hệ thống không còn theo dõi vùng này theo kịch bản chạm giá.",
                )
                return
            # keep touched state; daemon will re-dispatch after retry_at
            z1.status = "cham"
            z1.retry_at = _retry_at_iso()
            _state_write(params, st1)
            _send_log(
                settings,
                f"[zone-touch] act=loai confirm {z1.loai_streak}/{loai_confirm_rounds} "
                f"| zone_id={zone_id} -> status=cham retry_at={z1.retry_at}",
            )
            _send_user_notice(
                settings,
                "AI gợi ý «loại» — chưa đủ lần xác nhận.",
                "Vùng vẫn được theo dõi; sẽ thử lại sau.",
            )
            return

        # Any non-loai action resets loai_streak.
        z1.loai_streak = 0
        z1.tp1_followup_done = False

        if act != "VÀO LỆNH":
            # keep touched state (no revert to vung_cho); daemon can retry later
            z1.status = "cham"
            z1.retry_at = _retry_at_iso()
            _state_write(params, st1)
            _send_log(
                settings,
                f"[zone-touch] act={act} | zone_id={zone_id} -> status=cham retry_at={z1.retry_at}",
            )
            _send_user_notice(
                settings,
                "Sau khi chạm vùng: chưa vào lệnh lần này.",
                f"AI trả về hành động «{act}». Hệ thống sẽ thử lại sau.",
            )
            return

        # Schema E: ``VÀO LỆNH`` → vào lệnh ngay (không gate hop_luu); trade_line từ baseline vùng.
        zone_tl = (z1.trade_line or "").strip()
        parsed, err = parse_openai_output_md(
            out_text,
            symbol_override=params.mt5_symbol,
            fallback_trade_line=zone_tl or None,
        )
        if err or parsed is None:
            z1.status = "cham"
            z1.retry_at = _retry_at_iso()
            _state_write(params, st1)
            _send_log(
                settings,
                f"[zone-touch] parse_trade_line_failed | err={err} | zone_id={zone_id} -> status=cham",
            )
            return

        z1.trade_line = (parsed.raw_line or "").strip()
        z1.status = "vao_lenh"
        _state_write(params, st1)
        _send_log(
            settings,
            f"[zone-touch] act=VAO_LENH | zone_id={zone_id} -> status=vao_lenh | trade_line={z1.trade_line!r}",
        )
        _send_user_notice(
            settings,
            f"Sau khi chạm vùng: AI xác nhận «VÀO LỆNH» ({z1.label}).",
        )

        if not params.mt5_execute:
            _send_log(settings, f"[zone-touch] mt5_execute=off | done | zone_id={zone_id}")
            return

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
                zone_label=z1.label,
                trade_line=z1.trade_line,
                execution_ok=ex.ok,
            )
        _send_log(settings, f"[zone-touch] mt5_execute_trade: {ex.message}".strip())
        _lines = [ex.message]
        if ex.order:
            _lines.append(f"Mã lệnh: {ex.order}")
        if params.mt5_dry_run:
            _lines.append("(Chế độ thử.)")
        _send_user_notice(
            settings,
            "Tự động vào lệnh — kết quả MT5",
            "\n".join(_lines),
        )

        tid = int(ex.order) if ex.order else 0
        if ex.ok and tid > 0:
            st2 = _state_read(params)
            if st2 is None:
                return
            for z in st2.zones:
                if z.id == zone_id:
                    z.mt5_ticket = tid
                    break
            _state_write(params, st2)
            _send_log(settings, f"[zone-touch] mt5_ticket_saved | zone_id={zone_id} ticket={tid}")
        return
    except Exception as e:
        # On any error: keep touched state (no revert to vung_cho); daemon will retry using retry_at
        try:
            stx = _state_read(params)
            if stx is not None:
                for z in stx.zones:
                    if z.id == zone_id:
                        z.status = "cham"
                        z.retry_at = _retry_at_iso()
                        break
                _state_write(params, stx)
        except Exception:
            pass
        _send_log(settings, f"[zone-touch] ERROR | zone_id={zone_id} | {e!s}")
        _send_user_notice(
            settings,
            "Lỗi khi xử lý chạm vùng chờ.",
            "Xem kênh log kỹ thuật để biết chi tiết.",
        )
        re_raise_unless_openai(e)


def _tv_watchlist_price_only_loop(
    *,
    settings: Settings,
    params: WatchlistDaemonParams,
    sym: str,
    poll_s: float,
    get_price: Callable[[int], Optional[float]],
) -> None:
    """Daemon giá: poll TradingView title price → shared memory (optional mirror ``last.txt``)."""
    last_path = params.last_price_path or default_last_price_path()
    shm = open_writer_shared_memory(sym)
    reconciled_boot = False
    heartbeat_s = 300.0
    last_heartbeat_at = 0.0
    telegram_log_interval_s = 60.0
    last_telegram_log_at = 0.0
    try:
        while True:
            wms = min(15_000, max(2_000, int(poll_s * 1000)))
            p_last = get_price(wms)
            if p_last is not None:
                write_last_price_shared(shm, float(p_last))
                if params.mirror_last_price_file:
                    write_last_price_file(float(p_last), last_path)
                if not reconciled_boot:
                    reconciled_boot = True
                    try:
                        n = reconcile_daemon_plans_at_boot(None)
                        _log.info(
                            "tv-watchlist-daemon (gia) | reconcile-daemon-plans spawned %s process(es)",
                            n,
                        )
                    except Exception as e:
                        _log.warning("tv-watchlist-daemon | reconcile-daemon-plans at boot failed: %s", e)
                now_mono_tg = time.monotonic()
                if (now_mono_tg - last_telegram_log_at) >= telegram_log_interval_s:
                    last_telegram_log_at = now_mono_tg
                    mirror = f" mirror={last_path}" if params.mirror_last_price_file else ""
                    _send_log(
                        settings,
                        f"[daemon-gia] last (shared memory) <- {p_last} | symbol={sym}{mirror}",
                    )
                _poll_terminal.info("tv-watchlist-daemon (gia) | symbol=%s | last=%s", sym, p_last)
            else:
                _poll_terminal.info("tv-watchlist-daemon (gia) | symbol=%s | last=(none)", sym)
            try:
                now_mono = time.monotonic()
                if p_last is not None and (now_mono - last_heartbeat_at) >= heartbeat_s:
                    last_heartbeat_at = now_mono
                    _log.info(
                        "tv-watchlist-daemon (gia) alive | symbol=%s last=%s",
                        sym,
                        p_last,
                    )
            except Exception:
                pass
            time.sleep(poll_s)
    finally:
        try:
            shm.close()
        except Exception:
            pass


def _daemon_plan_main_loop(
    *,
    settings: Settings,
    params: WatchlistDaemonParams,
    sym: str,
    poll_s: float,
) -> None:
    """
    One shard, one thread: read Last from shared memory (fallback ``last.txt``), run zone pipeline **sequentially**.
    Exit when the zone reaches ``done`` or ``loai``.
    """
    if params.shard_path is None:
        raise ValueError("daemon-plan requires params.shard_path")
    last_path = params.last_price_path or default_last_price_path()
    heartbeat_s = 300.0
    last_heartbeat_at = 0.0
    shard_tag = str(params.shard_path)
    telegram_plan_interval_s = 60.0
    last_plan_tg_at = 0.0
    _send_log(
        settings,
        f"[daemon-plan] start | shard={shard_tag} last_ipc+file={last_path} symbol={sym}",
    )
    while True:
        st = _state_read(params)
        if st is None or not st.zones:
            _poll_terminal.info(
                "daemon-plan | shard=%s | tick | zones=0 (no state)",
                shard_tag,
            )
            time.sleep(poll_s)
            continue

        z0 = st.zones[0]
        if z0.status in ("done", "loai"):
            _send_log(
                settings,
                f"[daemon-plan] exit | status={z0.status} shard={shard_tag} zone_id={z0.id}",
            )
            return

        p_last = read_last_price_for_daemon_plan(sym, last_path)
        now_plan_tg = time.monotonic()
        if (now_plan_tg - last_plan_tg_at) >= telegram_plan_interval_s:
            last_plan_tg_at = now_plan_tg
            _send_log(
                settings,
                _daemon_plan_watch_telegram_text(
                    z0, sym=sym, shard_tag=shard_tag, p_last=p_last
                ),
            )
        if p_last is None:
            _poll_terminal.info(
                "daemon-plan | shard=%s | tick | last=(none) ipc|file=%s",
                shard_tag,
                last_path,
            )
            time.sleep(poll_s)
            continue

        _poll_terminal.info(
            "daemon-plan | shard=%s | tick | last=%s",
            shard_tag,
            p_last,
        )

        try:
            now_mono = time.monotonic()
            if (now_mono - last_heartbeat_at) >= heartbeat_s:
                last_heartbeat_at = now_mono
        except Exception:
            pass

        # vung_cho / cham: SL hit → loại
        sl_invalidated = False
        for z in st.zones:
            if z.status not in ("vung_cho", "cham"):
                continue
            if _maybe_loai_zone_if_last_hit_sl(z, float(p_last), settings=settings, params=params):
                sl_invalidated = True
        if sl_invalidated:
            _state_write(params, st)

        st_auto = _state_read(params)
        if st_auto is not None:
            for z in st_auto.zones:
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
                aer = (getattr(z, "auto_entry_retry_after", "") or "").strip()
                if aer and not _is_retry_due(aer):
                    continue
                z.status = "dang_vao_lenh"
                z.auto_entry_retry_after = ""
                _state_write(params, st_auto)
                _send_log(
                    settings,
                    f"[auto-entry] dispatch | zone_id={z.id} label={z.label} hop_luu={z.hop_luu} thr(>)={thr}",
                )
                _auto_entry_job(settings=settings, params=params, zone_id=z.id)

        st_retry = _state_read(params)
        if st_retry is not None:
            for z in st_retry.zones:
                if z.status != "cham":
                    continue
                if not _is_retry_due(getattr(z, "retry_at", "")):
                    continue
                z.status = "dang_thuc_thi"
                z.retry_at = ""
                _state_write(params, st_retry)
                _send_log(settings, f"[zone-touch] retry_dispatch | zone_id={z.id} last={p_last}")
                _zone_touch_job(
                    settings=settings,
                    params=params,
                    zone_id=z.id,
                    last_price=float(p_last),
                    after_retry_wait=True,
                )

        st = _state_read(params)
        if st is None or not st.zones:
            time.sleep(poll_s)
            continue

        matched: list[Zone] = []
        for z in st.zones:
            if z.status != "vung_cho":
                continue
            ref = _zone_side_ref_from_vung_cho(z)
            if ref is None:
                continue
            try:
                p_last_n = _price_round_nearest_int(p_last)
                ref_n = _price_round_nearest_int(ref)
            except Exception:
                p_last_n = float(p_last)
                ref_n = float(ref)
            if abs(p_last_n - ref_n) <= float(params.eps):
                matched.append(z)

        for z in matched:
            z.status = "cham"
            _state_write(params, st)
            _zone_touch_job(
                settings=settings,
                params=params,
                zone_id=z.id,
                last_price=float(p_last),
            )

        st_tp1 = _state_read(params)
        if st_tp1 is not None:
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
                _state_write(params, st_tp1)

            st_tp1b = _state_read(params)
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
                    z.status = "dang_thuc_thi"
                    z.tp1_followup_done = True
                    _state_write(params, st_tp1b)
                    _send_log(settings, f"[tp1] touched | zone_id={z.id} -> followup last={p_last}")
                    _tp1_followup_job(
                        settings=settings,
                        params=params,
                        zone_id=z.id,
                        p_last=float(p_last),
                    )

        time.sleep(poll_s)


def _tv_watchlist_rpc_poll_price(
    client: BrowserClient,
    *,
    tab_id: str,
    tv: dict[str, Any],
    sym: str,
    wms: int,
) -> Optional[float]:
    # Backward-compat: old watchlist polling (kept for reference).
    # The daemon now uses chart tab `document.title` as the source of truth.
    return None


def _parse_price_from_tv_title(title: str, *, sym: str) -> Optional[float]:
    """
    Title example: "XAUUSD 4,755.145 ▼ −0.22% Vô danh"
    We parse the first number after the symbol.
    """
    t = (title or "").strip()
    if not t:
        return None
    m = _TV_TITLE_PRICE_RE.match(t)
    if not m:
        return None
    sym_in_title = str(m.group("sym") or "").strip().upper()
    if sym_in_title != str(sym or "").strip().upper():
        return None
    raw = str(m.group("price") or "").replace(",", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


# If ``document.title`` parses to the same price for this long, treat as stale and reload the chart tab.
# A pure "N polls in a row" rule is too aggressive: TV often updates the tab title slower than the
# poll interval, so identical parses for a few seconds are normal, not a broken feed.
_TITLE_PRICE_STALE_MIN_SECONDS = 30.0


def _title_price_should_reload_stale(
    st: dict[str, Any],
    p: Optional[float],
) -> tuple[bool, float]:
    """
    Track whether the parsed title price has been unchanged long enough to reload.

    ``st`` holds ``last_p`` and ``since`` (monotonic time when ``last_p`` was first seen).

    Returns:
        ``(should_reload, elapsed_seconds)`` — elapsed is meaningful only when ``should_reload``.
    """
    if p is None:
        st.clear()
        return False, 0.0
    now = time.monotonic()
    lp = st.get("last_p")
    if lp is None or p != lp:
        st["last_p"] = p
        st["since"] = now
        return False, 0.0
    since = float(st["since"])
    elapsed = now - since
    if elapsed >= _TITLE_PRICE_STALE_MIN_SECONDS:
        return True, elapsed
    return False, 0.0


def _tv_watchlist_init_request_params(tv: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Payload for ``METHOD_TV_WATCHLIST_INIT`` (browser service RPC)."""
    return {
        "chart_url": str(tv.get("chart_url")),
        "tv": tv,
        "email": settings.coinmap_email,
        "password": settings.tradingview_password,
        "initial_settle_ms": int(tv.get("initial_settle_ms", 3000)),
    }


def _prepare_tv_chart_page(page: Any, tv: dict[str, Any], settings: Settings) -> None:
    """Open chart URL, login, settle, dark mode, watchlist — shared by initial load and stale-title reload."""
    chart_url = str(tv.get("chart_url") or "").strip()
    if not chart_url:
        raise SystemExit("tradingview_capture.chart_url missing in coinmap yaml.")
    page.goto(chart_url, wait_until="domcontentloaded", timeout=120_000)
    _maybe_tradingview_login(page, tv, settings.coinmap_email, settings.tradingview_password)
    page.wait_for_timeout(int(tv.get("initial_settle_ms", 3000)))
    _maybe_tradingview_dark_mode(page, tv)
    _tradingview_ensure_watchlist_open(page, tv)


def _make_playwright_title_price_getter(
    page: Any,
    *,
    sym: str,
    tv: dict[str, Any],
    settings: Settings,
) -> Callable[[int], Optional[float]]:
    """
    Poll ``page.title()`` → parse price. If the same value persists for
    ``_TITLE_PRICE_STALE_MIN_SECONDS``, run ``_prepare_tv_chart_page`` and re-parse.
    """

    stale_st: dict[str, Any] = {}

    def get_price(_wms: int) -> Optional[float]:
        p = _parse_price_from_tv_title(page.title(), sym=sym)
        do_reload, elapsed = _title_price_should_reload_stale(stale_st, p)
        if not do_reload:
            return p
        _log.info(
            "tv-watchlist-daemon | title price unchanged %s for %.0fs — reloading chart",
            p,
            elapsed,
        )
        try:
            _prepare_tv_chart_page(page, tv, settings)
        except Exception as e:
            _log.warning("tv-watchlist-daemon | reload chart failed: %s", e)
        stale_st.clear()
        p = _parse_price_from_tv_title(page.title(), sym=sym)
        _title_price_should_reload_stale(stale_st, p)
        return p

    return get_price


def _tv_rpc_poll_title_price(
    client: BrowserClient,
    *,
    tab_id: str,
    sym: str,
    wms: int,
) -> Optional[float]:
    timeout_rpc = max(30.0, float(wms) / 1000.0 + 10.0)
    resp = client.request(
        METHOD_EVAL,
        {"tab_id": tab_id, "script": "() => document.title", "arg": None},
        timeout_s=timeout_rpc,
    )
    if not resp.get("ok"):
        _log.warning("eval(document.title) RPC failed: %s", resp.get("error"))
        return None
    title = (resp.get("result") or {}).get("value")
    if not isinstance(title, str):
        return None
    return _parse_price_from_tv_title(title, sym=sym)


def _rpc_replace_chart_tab(
    client: BrowserClient,
    *,
    old_tab_id: str,
    tv: dict[str, Any],
    settings: Settings,
) -> str:
    """
    Open a fresh chart via ``tv_watchlist_init``, then close ``old_tab_id``.
    Creates the new tab first so a failed init leaves the previous tab usable.
    """
    init = client.request(
        METHOD_TV_WATCHLIST_INIT,
        _tv_watchlist_init_request_params(tv, settings),
        timeout_s=600.0,
    )
    if not init.get("ok"):
        raise RuntimeError(str(init.get("error") or "tv_watchlist_init failed"))
    new_id = str((init.get("result") or {}).get("tab_id") or "").strip()
    if not new_id:
        raise RuntimeError("tv_watchlist_init: missing tab_id")
    try:
        client.request(METHOD_CLOSE_TAB, {"tab_id": old_tab_id}, timeout_s=60.0)
    except OSError:
        pass
    return new_id


def _make_rpc_title_price_getter(
    client: BrowserClient,
    tab_id_holder: list[str],
    *,
    sym: str,
    tv: dict[str, Any],
    settings: Settings,
) -> Callable[[int], Optional[float]]:
    """
    Same stale-title logic as ``_make_playwright_title_price_getter`` but reloads by RPC:
    new ``tv_watchlist_init`` tab, then close the old tab.
    ``tab_id_holder`` is a single-element list updated when the tab is replaced.
    """

    stale_st: dict[str, Any] = {}

    def get_price(wms: int) -> Optional[float]:
        tid = tab_id_holder[0]
        p = _tv_rpc_poll_title_price(client, tab_id=tid, sym=sym, wms=wms)
        do_reload, elapsed = _title_price_should_reload_stale(stale_st, p)
        if not do_reload:
            return p
        _log.info(
            "tv-watchlist-daemon | title price unchanged %s for %.0fs — RPC reload chart (new tab)",
            p,
            elapsed,
        )
        try:
            tab_id_holder[0] = _rpc_replace_chart_tab(
                client,
                old_tab_id=tid,
                tv=tv,
                settings=settings,
            )
        except Exception as e:
            _log.warning("tv-watchlist-daemon | RPC reload chart failed: %s", e)
        stale_st.clear()
        p = _tv_rpc_poll_title_price(
            client, tab_id=tab_id_holder[0], sym=sym, wms=wms
        )
        _title_price_should_reload_stale(stale_st, p)
        return p

    return get_price


def run_tv_watchlist_daemon(
    *,
    settings: Settings,
    params: WatchlistDaemonParams,
) -> str:
    cfg = load_coinmap_yaml(params.coinmap_tv_yaml)
    tv = cfg.get("tradingview_capture") or {}
    if not isinstance(tv, dict) or not tv.get("chart_url"):
        raise SystemExit("tradingview_capture.chart_url missing in coinmap yaml.")

    poll_s = float(params.poll_seconds or 1.0)
    if poll_s <= 0:
        poll_s = 1.0

    # Which symbol to read from watchlist?
    sym = (tv.get("watchlist_symbol_short") or "").strip().upper()
    if not sym or sym == DEFAULT_MAIN_CHART_SYMBOL:
        sym = get_active_main_symbol().strip().upper()

    last_p = params.last_price_path or default_last_price_path()
    _log.info(
        "tv-watchlist-daemon (gia) start | symbol=%s poll=%.1fs mirror_last_file=%s path=%s stop_plans_on_exit=%s",
        sym,
        poll_s,
        params.mirror_last_price_file,
        last_p,
        params.stop_daemon_plans_on_exit,
    )
    if params.stop_daemon_plans_on_exit:
        register_stop_daemon_plans_on_exit(default_zones_dir(sym))

    # Browser service up: drive TradingView entirely via RPC (no second CDP client in this process).
    if is_service_responding():
        c = BrowserClient.from_state_file()
        if not c:
            raise SystemExit("browser service state missing; run: coinmap-automation browser up")
        _log.info("tv-watchlist-daemon mode=rpc | no local Playwright CDP attach")
        init = c.request(
            METHOD_TV_WATCHLIST_INIT,
            _tv_watchlist_init_request_params(tv, settings),
            timeout_s=600.0,
        )
        if not init.get("ok"):
            raise SystemExit(f"tv_watchlist_init failed: {init.get('error')}")
        tab_id = str((init.get("result") or {}).get("tab_id") or "").strip()
        if not tab_id:
            raise SystemExit("tv_watchlist_init: missing tab_id")
        tab_holder = [tab_id]
        get_price = _make_rpc_title_price_getter(
            c, tab_holder, sym=sym, tv=tv, settings=settings
        )
        try:
            _tv_watchlist_price_only_loop(
                settings=settings,
                params=params,
                sym=sym,
                poll_s=poll_s,
                get_price=get_price,
            )
        finally:
            try:
                c.request(METHOD_CLOSE_TAB, {"tab_id": tab_holder[0]}, timeout_s=60.0)
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
            _prepare_tv_chart_page(page, tv, settings)
            get_price = _make_playwright_title_price_getter(page, sym=sym, tv=tv, settings=settings)

            _tv_watchlist_price_only_loop(
                settings=settings,
                params=params,
                sym=sym,
                poll_s=poll_s,
                get_price=get_price,
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


def run_daemon_plan(
    *,
    settings: Settings,
    params: WatchlistDaemonParams,
) -> str:
    """
    One process per shard JSON: reads Last from daemon giá (shared memory, fallback ``last.txt``).
    """
    if params.shard_path is None:
        raise SystemExit("daemon-plan requires --shard PATH (vung_*.json)")
    register_daemon_plan_pidfile_for_current_process(params.shard_path)
    poll_s = float(params.poll_seconds or 1.0)
    if poll_s <= 0:
        poll_s = 1.0
    sym = get_active_main_symbol().strip().upper()
    last_p = params.last_price_path or default_last_price_path()
    _log.info(
        "daemon-plan start | shard=%s poll=%.1fs symbol=%s last_price=%s",
        params.shard_path,
        poll_s,
        sym,
        last_p,
    )
    _daemon_plan_main_loop(settings=settings, params=params, sym=sym, poll_s=poll_s)
    return "stopped"

