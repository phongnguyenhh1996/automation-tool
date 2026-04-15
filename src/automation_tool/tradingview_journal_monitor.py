"""
TradingView: tab Nhật ký — khớp một trong ba giá → Coinmap XAUUSD M5 + OpenAI intraday.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Literal, Optional, Set

from playwright.sync_api import BrowserContext, Page, sync_playwright
from zoneinfo import ZoneInfo

from automation_tool.coinmap import (
    _maybe_tradingview_dark_mode,
    _maybe_tradingview_login,
    _tradingview_ensure_watchlist_open,
    capture_charts,
    load_coinmap_yaml,
)
from automation_tool.config import Settings, resolved_model_for_intraday_alert
from automation_tool.images import (
    DEFAULT_MAIN_CHART_SYMBOL,
    coinmap_xauusd_5m_json_path,
    get_active_main_symbol,
    read_main_chart_symbol,
)
from automation_tool.first_response_trade import apply_first_response_vao_lenh
from automation_tool.mt5_execute import execute_trade, format_mt5_execution_for_telegram
from automation_tool.openai_errors import re_raise_unless_openai
from automation_tool.openai_prompt_flow import (
    JOURNAL_INTRADAY_FIRST_USER_TEMPLATE,
    JOURNAL_INTRADAY_RETRY_USER_TEMPLATE,
    run_single_followup_responses,
)
from automation_tool.browser_client import try_attach_playwright_via_service
from automation_tool.playwright_browser import close_browser_and_context, launch_chrome_context
from automation_tool.mt5_openai_parse import (
    is_last_price_hit_stop_loss,
    normalize_broker_xau_symbol,
    parse_journal_intraday_action_from_openai_text,
    parse_openai_output_md,
    parse_trade_line,
)
from automation_tool.state_files import (
    LOAI,
    LastAlertState,
    VAO_LENH,
    VUNG_CHO,
    default_last_alert_prices_path,
    read_last_alert_state,
    read_last_response_id,
    update_single_plan_status,
    watchlist_journal_active_work,
    write_journal_monitor_first_run,
)
from automation_tool.telegram_bot import (
    send_mt5_execution_log_to_ngan_gon_chat,
    send_openai_output_to_telegram,
    send_phan_tich_alert_to_main_chat_if_any,
    send_user_friendly_notice,
)
from automation_tool.tradingview_alerts import (
    _open_alerts_list_panel,
    parse_tv_alert_price_from_description,
)
from automation_tool.tradingview_last_price import read_watchlist_last_price_wait_stable
from automation_tool.tp1_followup import maybe_post_entry_tp1_tick
from automation_tool.tradingview_touch_flow import TouchFlowParams, run_intraday_touch_flow

_EPS = 0.01

# Trong vòng trong (touch): chỉ ghi status loại sau khi model trả "loại" đủ nhiều lần liên tiếp
# (mỗi lần chờ wait_minutes rồi chụp M5 + hỏi lại), tránh loại ngay từ lần đầu.
JOURNAL_LOAI_CONFIRM_ROUNDS = 4


def _journal_log(timezone_name: str, msg: str) -> None:
    """Log có timestamp (stderr + logger ``automation_tool.journal`` → Telegram nếu cấu hình)."""
    try:
        z = ZoneInfo(timezone_name)
        ts = datetime.now(z).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full = f"[{ts}] tv-journal | {msg}"
    logging.getLogger("automation_tool.journal").info(full)


def _truncate(s: str, max_len: int = 140) -> str:
    s = s.replace("\n", " ").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


JournalRunOutcome = Literal[
    "matched_and_entered",
    "matched_rejected",
    "cutoff_time",
    "all_plans_resolved",
]

InnerLoopOutcome = Literal["entered", "rejected", "cutoff", "superseded"]


@dataclass(frozen=True)
class InnerLoopResult:
    """Kết quả vòng trong: nếu ``superseded``, dùng ``supersede_touch`` để mở vòng trong mới."""

    outcome: InnerLoopOutcome
    supersede_touch: Optional[tuple[float, str, str]] = None  # (price, journal_line, label)


@dataclass(frozen=True)
class JournalMonitorParams:
    coinmap_tv_yaml: Path
    capture_coinmap_yaml: Path
    charts_dir: Path
    storage_state_path: Optional[Path]
    target_prices: tuple[float, float, float]
    headless: bool
    no_save_storage: bool
    poll_seconds: float
    wait_minutes: int
    until_hour: int
    timezone_name: str
    no_telegram: bool
    last_alert_path: Optional[Path] = None
    mt5_execute: bool = True
    mt5_symbol: Optional[str] = None
    mt5_dry_run: bool = False
    openai_model: Optional[str] = None
    openai_model_cli: Optional[str] = None
    # Đặt khi chạy monitor: mốc dừng động (trước 13:00 → 13:00 cùng ngày; từ 13:00 → 02:00 sáng hôm sau).
    session_cutoff_end: Optional[datetime] = None


def _journal_panel_css(tv: dict[str, Any]) -> str:
    s = (tv.get("journal_log_panel_selector") or "").strip()
    return s or "#id_alert-widget-tabs-slots_tabpanel_log"


def _journal_tab_selector(tv: dict[str, Any]) -> str:
    s = (tv.get("journal_log_tab_selector") or "").strip()
    return s or 'button#log[role="tab"]'


def _journal_desc_selector(tv: dict[str, Any]) -> str:
    # Log tab uses alert-log-item + message line (not alert-item-description, which is the alerts *list* tab).
    return (tv.get("journal_item_description_selector") or "").strip() or (
        'div[data-name="alert-log-item"] > div:first-child, '
        'div[data-name="alert-item-description"]'
    )


def open_journal_tab(page: Page, tv: dict[str, Any]) -> None:
    tab = page.locator(_journal_tab_selector(tv)).first
    tab.wait_for(state="visible", timeout=20_000)
    tab.click(timeout=15_000)
    page.wait_for_timeout(int(tv.get("journal_after_tab_ms", 400)))
    panel = page.locator(_journal_panel_css(tv)).first
    panel.wait_for(state="visible", timeout=20_000)


def list_journal_rows(page: Page, tv: dict[str, Any]) -> list[tuple[Optional[float], str]]:
    """``(parsed_price_or_none, raw_line_text)`` for each journal row."""
    panel_css = _journal_panel_css(tv)
    page.locator(panel_css).first.wait_for(state="visible", timeout=20_000)
    desc_sel = _journal_desc_selector(tv)
    descs = page.locator(f"{panel_css} {desc_sel}")
    n = descs.count()
    out: list[tuple[Optional[float], str]] = []
    for i in range(n):
        t = descs.nth(i).inner_text(timeout=10_000)
        p = parse_tv_alert_price_from_description(t)
        out.append((p, t.strip()))
    return out


def _price_matches_any(p: float, targets: tuple[float, float, float]) -> bool:
    for t in targets:
        if abs(p - t) <= _EPS:
            return True
    return False


def _next_02am_after(fr: datetime, z: ZoneInfo) -> datetime:
    """Mốc 02:00 sáng đầu tiên sau ``fr`` (cùng timezone)."""
    fr = fr.astimezone(z)
    d = fr.date()
    today_2am = datetime.combine(d, dt_time(2, 0), tzinfo=z)
    if fr < today_2am:
        return today_2am
    next_d = d + timedelta(days=1)
    return datetime.combine(next_d, dt_time(2, 0), tzinfo=z)


def compute_journal_session_cutoff(first_run: datetime, timezone_name: str) -> datetime:
    """
    - ``first_run`` **trước 13:00** địa phương → dừng lúc **13:00** cùng ngày.
    - **Từ 13:00** trở đi (gồm sau 13:20) → chạy tới **02:00 sáng** (ngày kế tiếp nếu cần).
    """
    z = ZoneInfo(timezone_name)
    fr = first_run.astimezone(z) if first_run.tzinfo else first_run.replace(tzinfo=z)
    noon = dt_time(13, 0)
    if fr.time() < noon:
        return fr.replace(hour=13, minute=0, second=0, microsecond=0)
    return _next_02am_after(fr, z)


def _before_cutoff(
    timezone_name: str,
    until_hour: int,
    session_cutoff_end: Optional[datetime] = None,
) -> bool:
    z = ZoneInfo(timezone_name)
    now = datetime.now(z)
    if session_cutoff_end is not None:
        co = session_cutoff_end
        if co.tzinfo is None:
            co = co.replace(tzinfo=z)
        else:
            co = co.astimezone(z)
        return now < co
    cutoff = now.replace(hour=until_hour, minute=0, second=0, microsecond=0)
    return now < cutoff


def _reload_page_and_list_journal_rows(
    page: Page,
    tv: dict[str, Any],
    settle_ms: int,
) -> list[tuple[Optional[float], str]]:
    """Reload chart → panel cảnh báo → tab Nhật ký → danh sách dòng (dùng chung vòng ngoài và poll trong lúc chờ)."""
    page.reload(wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(settle_ms)
    _open_alerts_list_panel(page, tv)
    open_journal_tab(page, tv)
    return list_journal_rows(page, tv)


def _sleep_wait_minutes_respecting_cutoff_with_journal_poll(
    wait_minutes: int,
    timezone_name: str,
    until_hour: int,
    session_cutoff_end: Optional[datetime],
    *,
    page: Page,
    tv: dict[str, Any],
    settle_ms: int,
    poll_seconds: float,
    last_alert_path: Path,
    processed: Set[str],
    touched_label: str,
    touched_price: float,
) -> tuple[Literal["ok", "cutoff", "rejected"], Optional[tuple[float, str, str]]]:
    """
    Chờ tối đa ``wait_minutes`` nhưng chia nhỏ: mỗi ``poll_seconds`` (tối đa 30s) reload Nhật ký
    và kiểm tra chạm giá. Nếu có dòng khớp plan **khác** (label hoặc giá khác plan đang xử lý):
    ghi ``loại`` cho plan cũ, thêm dòng mới vào ``processed``, trả về ``(True, (giá, dòng, label))``.
    Nếu hết giờ trong lúc chờ: ``(False, None)``. Chờ đủ phút không đổi giá: ``(True, None)``.
    """
    end = time.time() + wait_minutes * 60
    try:
        z = ZoneInfo(timezone_name)
        wake = datetime.fromtimestamp(end, tz=z).strftime("%H:%M %Z")
        wake_hint = f" (dự kiến khoảng {wake})"
    except Exception:
        wake_hint = ""
    _journal_log(
        timezone_name,
        f"Bắt đầu chờ {wait_minutes} phút (vẫn poll Nhật ký mỗi ~{poll_seconds:.0f}s) "
        f"trước lần chụp Coinmap M5 + OpenAI tiếp theo{wake_hint}.",
    )
    last_progress_log = 0.0
    poll_iv = min(30.0, max(1.0, float(poll_seconds)))
    sym = (tv.get("watchlist_symbol_short") or "").strip().upper()
    if not sym or sym == DEFAULT_MAIN_CHART_SYMBOL:
        sym = get_active_main_symbol().strip().upper()
    sym_parse = normalize_broker_xau_symbol(sym)

    while time.time() < end:
        if not _before_cutoff(timezone_name, until_hour, session_cutoff_end):
            _journal_log(
                timezone_name,
                "Hết khung giờ (session_cutoff hoặc --until-hour) trong lúc chờ — dừng.",
            )
            return ("cutoff", None)

        now = time.time()
        remain = end - now
        if now - last_progress_log >= 120.0:
            _journal_log(
                timezone_name,
                f"… vẫn chờ: còn khoảng {remain / 60.0:.1f} phút (poll Nhật ký đang bật)",
            )
            last_progress_log = now

        st = read_last_alert_state(last_alert_path)
        if st is None:
            raise SystemExit(f"Missing last alert state at {last_alert_path}")

        tl_sl = (st.trade_line_by_label.get(touched_label) or "").strip()
        if tl_sl:
            pt_sl = parse_trade_line(tl_sl, sym_parse)
            if pt_sl is not None:
                p_chk = read_watchlist_last_price_wait_stable(
                    page, tv, symbol=sym, timeout_ms=3000, poll_ms=250
                )
                if p_chk is not None and is_last_price_hit_stop_loss(p_chk, pt_sl, eps=_EPS):
                    _journal_log(
                        timezone_name,
                        f"Last={p_chk} chạm SL trong lúc chờ — loại {touched_label} và kết thúc vòng trong.",
                    )
                    update_single_plan_status(touched_label, LOAI, path=last_alert_path)
                    return ("rejected", None)

        waiting = _waiting_label_prices(st)
        if waiting:
            _journal_log(timezone_name, "Poll Nhật ký (trong lúc chờ) — reload + đọc dòng…")
            try:
                rows = _reload_page_and_list_journal_rows(page, tv, settle_ms)
            except Exception as e:
                _journal_log(
                    timezone_name,
                    f"Poll Nhật ký lỗi (bỏ qua lần này, thử lại sau): {e!s}",
                )
            else:
                m = _pick_matching_waiting_row(rows, waiting, processed)
                if m is not None:
                    new_price, new_line, new_label = m
                    same_touch = new_label == touched_label and abs(new_price - touched_price) <= _EPS
                    if same_touch:
                        key = new_line.strip()
                        if key and key not in processed:
                            processed.add(key)
                            _journal_log(
                                timezone_name,
                                f"Poll: thêm dòng trùng plan {touched_label}@{touched_price} vào dedupe — tiếp tục chờ.",
                            )
                    else:
                        _journal_log(
                            timezone_name,
                            f"Poll: chạm giá/plan khác — mới={new_label}@{new_price}, "
                            f"đang xử lý={touched_label}@{touched_price}. Ghi loại plan cũ, chuyển sang giá mới.",
                        )
                        update_single_plan_status(touched_label, LOAI, path=last_alert_path)
                        processed.add(new_line.strip())
                        _journal_log(timezone_name, f"Đã ghi status loại cho plan {touched_label}.")
                        return ("ok", (new_price, new_line, new_label))

        remain = end - time.time()
        if remain <= 0:
            break
        chunk = min(poll_iv, remain, 30.0)
        time.sleep(chunk)

        if not _before_cutoff(timezone_name, until_hour, session_cutoff_end):
            _journal_log(
                timezone_name,
                "Hết khung giờ trong lúc chờ — dừng.",
            )
            return ("cutoff", None)

    ok = _before_cutoff(timezone_name, until_hour, session_cutoff_end)
    if ok:
        _journal_log(
            timezone_name,
            f"Đã chờ xong {wait_minutes} phút (không có chạm plan khác) — chụp Coinmap + gửi OpenAI lại.",
        )
    return ("ok" if ok else "cutoff", None)


def _waiting_label_prices(state: LastAlertState) -> list[tuple[str, float]]:
    """Plans still in ``vung_cho`` as (label, price)."""
    out: list[tuple[str, float]] = []
    for i, lab in enumerate(state.labels):
        if state.status_by_label.get(lab, VUNG_CHO) == VUNG_CHO:
            out.append((lab, state.prices[i]))
    return out


def _pick_matching_waiting_row(
    rows: list[tuple[Optional[float], str]],
    waiting: list[tuple[str, float]],
    processed: Set[str],
) -> Optional[tuple[float, str, str]]:
    """Return ``(parsed_price, raw_line, label)`` for first row matching a waiting plan price."""
    for price, raw in rows:
        key = raw.strip()
        if not key or key in processed:
            continue
        if price is None:
            continue
        for lab, tp in waiting:
            if abs(price - tp) <= _EPS:
                return (price, raw, lab)
    return None


def _pick_matching_row(
    rows: list[tuple[Optional[float], str]],
    targets: tuple[float, float, float],
    processed: Set[str],
) -> Optional[tuple[float, str]]:
    for price, raw in rows:
        key = raw.strip()
        if not key or key in processed:
            continue
        if price is None:
            continue
        if _price_matches_any(price, targets):
            return (price, raw)
    return None


def _find_new_matching_row(
    page: Page,
    tv: dict[str, Any],
    targets: tuple[float, float, float],
    processed: Set[str],
) -> Optional[tuple[float, str]]:
    return _pick_matching_row(list_journal_rows(page, tv), targets, processed)


def _run_intraday_touch_loop(
    *,
    settings: Settings,
    params: JournalMonitorParams,
    touched_price: float,
    touched_label: str,
    journal_line: str,
    initial_response_id: str,
    browser_context: BrowserContext,
    last_alert_path: Path,
    page: Page,
    tv: dict[str, Any],
    settle_ms: int,
    processed: Set[str],
) -> InnerLoopResult:
    """``chờ`` → capture lại + hỏi lại; lặp tới loại / VÀO LỆNH (có trade_line) / hết giờ / superseded."""
    tz = params.timezone_name
    prev_id = read_last_response_id() or initial_response_id
    first = True
    inner_i = 0
    loai_streak = 0

    _journal_log(
        tz,
        f"=== Vòng trong (touch) — label={touched_label} | giá chạm={touched_price} | "
        f"dòng Nhật ký: {_truncate(journal_line, 200)!s}",
    )
    if not params.no_telegram:
        send_user_friendly_notice(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_python_bot_chat_id,
            title="Nhật ký TradingView: có dòng khớp giá cần xử lý.",
            body=f"Vùng: {touched_label}. Đang chạy phân tích lại.",
        )

    while _before_cutoff(
        params.timezone_name,
        params.until_hour,
        params.session_cutoff_end,
    ):
        inner_i += 1
        st = read_last_alert_state(last_alert_path)
        if st is None:
            raise SystemExit(f"Missing last alert state at {last_alert_path}")
        p1, p2, p3 = st.prices

        if not first:
            sym_j = read_main_chart_symbol(params.charts_dir)
            sym_parse = normalize_broker_xau_symbol((params.mt5_symbol or "").strip() or sym_j)
            wait_ms = min(15_000, max(3_000, int(float(params.poll_seconds) * 1000)))
            p_chk = read_watchlist_last_price_wait_stable(
                page, tv, symbol=sym_j, timeout_ms=wait_ms, poll_ms=250
            )
            if p_chk is not None:
                tl_sl = (st.trade_line_by_label.get(touched_label) or "").strip()
                if tl_sl:
                    pt_sl = parse_trade_line(tl_sl, sym_parse)
                    if pt_sl is not None and is_last_price_hit_stop_loss(
                        p_chk, pt_sl, eps=_EPS
                    ):
                        _journal_log(
                            tz,
                            f"Last={p_chk} chạm SL trước Coinmap (lặp) — loại {touched_label}.",
                        )
                        update_single_plan_status(touched_label, LOAI, path=last_alert_path)
                        return InnerLoopResult("rejected")

        _journal_log(
            tz,
            f"Vòng trong #{inner_i}: chụp Coinmap (yaml={params.capture_coinmap_yaml.name}, charts_dir={params.charts_dir})",
        )
        paths = capture_charts(
            coinmap_yaml=params.capture_coinmap_yaml,
            charts_dir=params.charts_dir,
            storage_state_path=params.storage_state_path,
            email=settings.coinmap_email,
            password=settings.coinmap_password,
            tradingview_password=settings.tradingview_password,
            save_storage_state=not params.no_save_storage,
            headless=params.headless,
            reuse_browser_context=browser_context,
            main_chart_symbol=read_main_chart_symbol(params.charts_dir),
        )
        _journal_log(tz, f"Coinmap capture xong: {len(paths)} file(s).")
        if paths:
            for j, pth in enumerate(paths[:12]):
                _journal_log(tz, f"  [{j}] {pth}")
            if len(paths) > 12:
                _journal_log(tz, f"  … và {len(paths) - 12} file khác.")
        json_path = coinmap_xauusd_5m_json_path(params.charts_dir)
        if json_path is None or not json_path.is_file():
            raise SystemExit(
                f"No main-pair 5m Coinmap JSON under {params.charts_dir} "
                f"(expected stamp coinmap_{read_main_chart_symbol(params.charts_dir)}_5m). "
                "Check coinmap_update.yaml capture_plan and api_data_export."
            )
        _journal_log(tz, f"JSON M5 {read_main_chart_symbol(params.charts_dir)}: {json_path}")

        if first:
            user_msg = JOURNAL_INTRADAY_FIRST_USER_TEMPLATE.format(
                touched_price=touched_price,
                p1=p1,
                p2=p2,
                p3=p3,
                journal_line=journal_line,
            )
        else:
            user_msg = JOURNAL_INTRADAY_RETRY_USER_TEMPLATE.format(
                wait_minutes=params.wait_minutes,
                touched_price=touched_price,
                journal_line=journal_line,
            )

        _journal_log(
            tz,
            f"Gửi OpenAI follow-up (lần {'đầu' if first else 'lặp'}), previous_response_id={_truncate(prev_id, 36)}…",
        )
        _journal_log(tz, f"User message ~{len(user_msg)} ký tự (kèm JSON Coinmap trong request).")

        try:
            out_text, new_id = run_single_followup_responses(
                api_key=settings.openai_api_key,
                prompt_id=settings.openai_prompt_id,
                prompt_version=settings.openai_prompt_version,
                user_text=user_msg,
                coinmap_json_paths=[json_path],
                previous_response_id=prev_id,
                vector_store_ids=settings.openai_vector_store_ids,
                store=settings.openai_responses_store,
                include=settings.openai_responses_include,
                model=resolved_model_for_intraday_alert(settings, params.openai_model_cli),
            )
        except Exception as e:
            re_raise_unless_openai(e)

        _journal_log(tz, f"OpenAI response_id: {new_id}")
        _journal_log(tz, f"--- Toàn bộ output OpenAI ({len(out_text)} ký tự) ---")
        print(out_text, flush=True)
        _journal_log(tz, "--- Hết output OpenAI ---")
        prev_id = new_id

        send_phan_tich_alert_to_main_chat_if_any(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            raw_openai_text=out_text,
            default_parse_mode=settings.telegram_parse_mode,
            no_telegram=params.no_telegram,
        )

        hop_done = apply_first_response_vao_lenh(
            out_text,
            last_alert_path=last_alert_path,
            mt5_execute=params.mt5_execute,
            mt5_dry_run=params.mt5_dry_run,
            mt5_symbol=params.mt5_symbol,
            telegram_bot_token=settings.telegram_bot_token,
            telegram_chat_id=settings.telegram_chat_id,
            telegram_log_chat_id=settings.telegram_log_chat_id,
            telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
            telegram_output_ngan_gon_chat_id=settings.telegram_output_ngan_gon_chat_id,
            telegram_source_label="tv-journal-monitor (Nhật ký)",
            auto_mt5_zone_label=touched_label,
        )
        if hop_done:
            _journal_log(
                tz,
                "JSON prices: plan_chinh/plan_phu hop_luu>75, scalp hop_luu>60 + trade_line tại vùng chạm — đã ghi vao_lenh / MT5 (nếu bật). Kết thúc vòng trong.",
            )
            if not params.no_telegram:
                _journal_log(
                    tz,
                    "Gửi Telegram — chat chính + OUTPUT_NGAN_GON nếu cấu hình.",
                )
                send_openai_output_to_telegram(
                    bot_token=settings.telegram_bot_token,
                    chat_id=settings.telegram_chat_id,
                    raw=out_text,
                    default_parse_mode=settings.telegram_parse_mode,
                    summary_chat_id=settings.telegram_output_ngan_gon_chat_id,
                )
            else:
                _journal_log(tz, "Bỏ qua Telegram (--no-telegram).")
            return InnerLoopResult("entered")

        act = parse_journal_intraday_action_from_openai_text(out_text)
        _journal_log(tz, f"Parse intraday (JSON hoặc [OUTPUT_NGAN_GON]): Hành động = {act!r}")
        if act == "VÀO LỆNH":
            loai_streak = 0
            parsed, tl_err = parse_openai_output_md(out_text, symbol_override=params.mt5_symbol)
            if tl_err or parsed is None:
                _journal_log(
                    tz,
                    f"VÀO LỆNH nhưng trade_line không hợp lệ: {tl_err} — coi như chờ, chờ {params.wait_minutes} phút.",
                )
                ok_sleep, sup = _sleep_wait_minutes_respecting_cutoff_with_journal_poll(
                    params.wait_minutes,
                    params.timezone_name,
                    params.until_hour,
                    params.session_cutoff_end,
                    page=page,
                    tv=tv,
                    settle_ms=settle_ms,
                    poll_seconds=params.poll_seconds,
                    last_alert_path=last_alert_path,
                    processed=processed,
                    touched_label=touched_label,
                    touched_price=touched_price,
                )
                if ok_sleep != "ok":
                    if ok_sleep == "rejected":
                        _journal_log(tz, "Chạm SL trong lúc chờ — kết thúc vòng trong (loại).")
                        return InnerLoopResult("rejected")
                    _journal_log(tz, "Hết giờ trong lúc chờ (trade_line) — cutoff.")
                    return InnerLoopResult("cutoff")
                if sup is not None:
                    return InnerLoopResult("superseded", sup)
                first = False
                continue
            update_single_plan_status(
                touched_label,
                VAO_LENH,
                path=last_alert_path,
                entry_manual=False,
            )
            if params.mt5_execute:
                _journal_log(
                    tz,
                    f"MT5 ({'dry-run' if params.mt5_dry_run else 'live'}): gửi lệnh…",
                )
                ex = execute_trade(
                    parsed,
                    dry_run=params.mt5_dry_run,
                    symbol_override=params.mt5_symbol,
                )
                _journal_log(tz, ex.message)
                if not params.no_telegram:
                    send_mt5_execution_log_to_ngan_gon_chat(
                        bot_token=settings.telegram_bot_token,
                        telegram_chat_id=settings.telegram_chat_id,
                        source="tv-journal-monitor",
                        text=format_mt5_execution_for_telegram(ex),
                        zone_label=touched_label,
                    )
            if not params.no_telegram:
                _journal_log(tz, "Gửi Telegram (VÀO LỆNH) — chat chính + OUTPUT_NGAN_GON nếu cấu hình.")
                send_openai_output_to_telegram(
                    bot_token=settings.telegram_bot_token,
                    chat_id=settings.telegram_chat_id,
                    raw=out_text,
                    default_parse_mode=settings.telegram_parse_mode,
                    summary_chat_id=settings.telegram_output_ngan_gon_chat_id,
                )
            else:
                _journal_log(tz, "Bỏ qua Telegram (--no-telegram).")
            _journal_log(tz, "Kết thúc vòng trong: VÀO LỆNH (đã ghi status + optional MT5).")
            return InnerLoopResult("entered")
        if act == "loại":
            loai_streak += 1
            if loai_streak < JOURNAL_LOAI_CONFIRM_ROUNDS:
                _journal_log(
                    tz,
                    f"Hành động: loại (lần {loai_streak}/{JOURNAL_LOAI_CONFIRM_ROUNDS}) — "
                    "chưa ghi status; tiếp tục chờ như «chờ».",
                )
                ok_sleep, sup = _sleep_wait_minutes_respecting_cutoff_with_journal_poll(
                    params.wait_minutes,
                    params.timezone_name,
                    params.until_hour,
                    params.session_cutoff_end,
                    page=page,
                    tv=tv,
                    settle_ms=settle_ms,
                    poll_seconds=params.poll_seconds,
                    last_alert_path=last_alert_path,
                    processed=processed,
                    touched_label=touched_label,
                    touched_price=touched_price,
                )
                if ok_sleep != "ok":
                    if ok_sleep == "rejected":
                        _journal_log(tz, "Chạm SL trong lúc chờ — kết thúc vòng trong (loại).")
                        return InnerLoopResult("rejected")
                    _journal_log(tz, "Hết giờ trong lúc chờ (xác nhận loại) — cutoff.")
                    return InnerLoopResult("cutoff")
                if sup is not None:
                    return InnerLoopResult("superseded", sup)
                first = False
                continue
            update_single_plan_status(touched_label, LOAI, path=last_alert_path)
            _journal_log(
                tz,
                f"Kết thúc vòng trong: loại (đã xác nhận {JOURNAL_LOAI_CONFIRM_ROUNDS} lần liên tiếp) — đã ghi status.",
            )
            return InnerLoopResult("rejected")
        if act == "chờ":
            loai_streak = 0
            _journal_log(tz, f"Hành động: chờ — sẽ nghỉ {params.wait_minutes} phút rồi chụp M5 + hỏi lại.")
            ok_sleep, sup = _sleep_wait_minutes_respecting_cutoff_with_journal_poll(
                params.wait_minutes,
                params.timezone_name,
                params.until_hour,
                params.session_cutoff_end,
                page=page,
                tv=tv,
                settle_ms=settle_ms,
                poll_seconds=params.poll_seconds,
                last_alert_path=last_alert_path,
                processed=processed,
                touched_label=touched_label,
                touched_price=touched_price,
            )
            if ok_sleep != "ok":
                if ok_sleep == "rejected":
                    _journal_log(tz, "Chạm SL trong lúc chờ — kết thúc vòng trong (loại).")
                    return InnerLoopResult("rejected")
                _journal_log(tz, "Hết giờ trong lúc chờ (chờ) — cutoff.")
                return InnerLoopResult("cutoff")
            if sup is not None:
                return InnerLoopResult("superseded", sup)
            first = False
            continue

        loai_streak = 0
        _journal_log(
            tz,
            "Cảnh báo: không parse được Hành động (chờ / loại / VÀO LỆNH) trong [OUTPUT_NGAN_GON] — coi như chờ.",
        )
        ok_sleep, sup = _sleep_wait_minutes_respecting_cutoff_with_journal_poll(
            params.wait_minutes,
            params.timezone_name,
            params.until_hour,
            params.session_cutoff_end,
            page=page,
            tv=tv,
            settle_ms=settle_ms,
            poll_seconds=params.poll_seconds,
            last_alert_path=last_alert_path,
            processed=processed,
            touched_label=touched_label,
            touched_price=touched_price,
        )
        if ok_sleep != "ok":
            if ok_sleep == "rejected":
                _journal_log(tz, "Chạm SL trong lúc chờ — kết thúc vòng trong (loại).")
                return InnerLoopResult("rejected")
            _journal_log(tz, "Hết giờ sau chờ mặc định — cutoff.")
            return InnerLoopResult("cutoff")
        if sup is not None:
            return InnerLoopResult("superseded", sup)
        first = False

    _journal_log(tz, "Vòng trong: hết phiên (session_cutoff hoặc --until-hour) — cutoff.")
    return InnerLoopResult("cutoff")


def run_tv_journal_monitor(
    *,
    settings: Settings,
    params: JournalMonitorParams,
    initial_response_id: str,
) -> JournalRunOutcome:
    """
    Mở TradingView → vòng ngoài: mỗi ``poll_seconds`` reload trang, mở tab Nhật ký, parse giá;
    lặp tới khi khớp hoặc hết giờ.
    """
    cfg = load_coinmap_yaml(params.coinmap_tv_yaml)
    tv = cfg.get("tradingview_capture") or {}
    if not isinstance(tv, dict) or not tv.get("chart_url"):
        raise SystemExit("tradingview_capture.chart_url missing in coinmap yaml.")

    settle_ms = int(cfg.get("settle_ms", 2000))
    vw = int(cfg.get("viewport_width", 1920))
    vh = int(cfg.get("viewport_height", 1080))
    tz = params.timezone_name

    lap = params.last_alert_path or default_last_alert_prices_path()
    st0 = read_last_alert_state(lap)
    if st0 is None:
        raise SystemExit(f"Cannot read last alert state from {lap} (need prices + labels).")
    p1, p2, p3 = st0.prices
    zinfo = ZoneInfo(tz)
    first_run = datetime.now(zinfo)
    session_cutoff_end = compute_journal_session_cutoff(first_run, tz)
    fr_path = write_journal_monitor_first_run(
        started_at=first_run,
        session_cutoff_end=session_cutoff_end,
        timezone_name=tz,
        last_alert_path=lap,
    )
    params = replace(params, session_cutoff_end=session_cutoff_end)

    _journal_log(
        tz,
        f"Bắt đầu monitor | chart={tv.get('chart_url')!s} | headless={params.headless} | "
        f"viewport={vw}x{vh} | settle_ms={settle_ms} | last_alert={lap}",
    )
    _journal_log(
        tz,
        f"first_run={first_run.strftime('%Y-%m-%d %H:%M:%S')} | "
        f"session_cutoff_end={session_cutoff_end.strftime('%Y-%m-%d %H:%M:%S')} | "
        f"đã ghi {fr_path.name}",
    )
    _journal_log(
        tz,
        f"3 giá + status: {p1} | {p2} | {p3} | {st0.status_by_label} (epsilon={_EPS}) | "
        f"poll sau mỗi chu kỳ={params.poll_seconds}s | "
        f"chờ OpenAI={params.wait_minutes}m | "
        f"mốc dừng phiên: trước 13:00→13:00 cùng ngày; từ 13:00→02:00 sáng (timezone {tz})",
    )

    processed: Set[str] = set()
    outer_cycle = 0

    with sync_playwright() as p:
        attached = try_attach_playwright_via_service(p)
        if attached is not None:
            browser, context = attached
            use_browser_service = True
        else:
            browser, context = launch_chrome_context(
                p,
                headless=params.headless,
                storage_state_path=params.storage_state_path,
                viewport_width=vw,
                viewport_height=vh,
            )
            use_browser_service = False
        page = context.new_page()
        try:
            url = str(tv.get("chart_url"))
            _journal_log(tz, f"Mở trang TradingView (goto)…")
            page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            _journal_log(tz, "Đăng nhập TradingView (nếu bật trong yaml)…")
            _maybe_tradingview_login(
                page,
                tv,
                settings.coinmap_email,
                settings.tradingview_password,
            )
            page.wait_for_timeout(int(tv.get("initial_settle_ms", 3000)))
            _maybe_tradingview_dark_mode(page, tv)
            intervals_id = (tv.get("intervals_toolbar_id") or "header-toolbar-intervals").strip().lstrip(
                "#"
            )
            page.locator(f"#{intervals_id}").first.wait_for(state="visible", timeout=90_000)
            _journal_log(tz, "Khung thời gian chart sẵn sàng — bắt đầu vòng ngoài (reload → Nhật ký → parse).")

            sym = (tv.get("watchlist_symbol_short") or "").strip().upper()
            if not sym or sym == DEFAULT_MAIN_CHART_SYMBOL:
                sym = get_active_main_symbol().strip().upper()
            current_response_id = initial_response_id

            def _tp1_tick_from_watchlist_last() -> None:
                nonlocal current_response_id
                _tradingview_ensure_watchlist_open(page, tv)
                wms = min(15_000, max(3_000, int(float(params.poll_seconds) * 1000)))
                p_last = read_watchlist_last_price_wait_stable(
                    page, tv, symbol=sym, timeout_ms=wms, poll_ms=250
                )
                if p_last is None:
                    _journal_log(tz, "Giá Last watchlist chưa ổn định sau chờ — bỏ qua tick TP1.")
                    return
                try:
                    rid = maybe_post_entry_tp1_tick(
                        settings=settings,
                        params=params,
                        last_alert_path=lap,
                        page=page,
                        tv=tv,
                        symbol=sym,
                        settle_ms=settle_ms,
                        p_last=p_last,
                        browser_context=context,
                        initial_response_id=current_response_id,
                        tick_source="journal",
                    )
                except Exception as e:
                    _journal_log(tz, f"post-entry TP1 tick lỗi (bỏ qua): {e!s}")
                else:
                    if rid:
                        current_response_id = rid

            while _before_cutoff(
                params.timezone_name,
                params.until_hour,
                params.session_cutoff_end,
            ):
                outer_cycle += 1
                now_local = datetime.now(ZoneInfo(tz)).strftime("%H:%M:%S")
                _journal_log(
                    tz,
                    f"--- Vòng ngoài #{outer_cycle} (giờ địa phương ~{now_local}) ---",
                )
                st = read_last_alert_state(lap)
                if st is None:
                    raise SystemExit(f"Lost last alert state at {lap}")
                if not watchlist_journal_active_work(st):
                    _journal_log(
                        tz,
                        "Không còn vùng chờ và không còn theo dõi TP1 sau vào lệnh — dừng monitor.",
                    )
                    return "all_plans_resolved"
                waiting = _waiting_label_prices(st)
                if not waiting:
                    _journal_log(
                        tz,
                        "Không còn plan vung_cho — chỉ theo dõi Last watchlist (±5 / TP1) nếu có.",
                    )
                    _tp1_tick_from_watchlist_last()
                    time.sleep(max(1.0, params.poll_seconds))
                    continue
                _journal_log(
                    tz,
                    f"Plan còn chờ ({VUNG_CHO}): "
                    + ", ".join(f"{lab}={px}" for lab, px in waiting),
                )

                _journal_log(tz, "Reload trang TradingView…")
                _journal_log(tz, f"Chờ settle {settle_ms}ms sau reload…")
                rows = _reload_page_and_list_journal_rows(page, tv, settle_ms)
                _journal_log(tz, f"Đọc Nhật ký: {len(rows)} dòng (selector mô tả trong yaml).")
                for idx, (pr, raw) in enumerate(rows):
                    skip = ""
                    rk = raw.strip()
                    if rk in processed:
                        skip = " [đã xử lý, bỏ qua]"
                    elif pr is None:
                        skip = " [không parse được giá]"
                    _journal_log(
                        tz,
                        f"  [{idx}] parse_giá={pr!s}{skip} | {_truncate(raw, 160)!r}",
                    )
                n_proc = len(processed)
                if n_proc:
                    _journal_log(tz, f"Số dòng Nhật ký đã xử lý (dedupe): {n_proc}")
                m = _pick_matching_waiting_row(rows, waiting, processed)
                if m is not None:
                    touched, line, tlab = m
                    _journal_log(
                        tz,
                        f"KHỚP giá {touched} (plan {tlab}) — bắt đầu vòng trong (Coinmap + OpenAI).",
                    )
                    processed.add(line.strip())
                    rid = read_last_response_id() or current_response_id

                    tfp = TouchFlowParams(
                        capture_coinmap_yaml=params.capture_coinmap_yaml,
                        charts_dir=params.charts_dir,
                        storage_state_path=params.storage_state_path,
                        headless=params.headless,
                        no_save_storage=params.no_save_storage,
                        wait_minutes=params.wait_minutes,
                        until_hour=params.until_hour,
                        timezone_name=params.timezone_name,
                        no_telegram=params.no_telegram,
                        mt5_execute=params.mt5_execute,
                        mt5_symbol=params.mt5_symbol,
                        mt5_dry_run=params.mt5_dry_run,
                        session_cutoff_end=params.session_cutoff_end,
                        openai_model=params.openai_model,
                        openai_model_cli=params.openai_model_cli,
                    )

                    def _poll_sup(touched_label: str, touched_price: float):
                        # Reload journal and pick a new waiting-row match (different plan) to supersede.
                        try:
                            stx = read_last_alert_state(lap)
                            if stx is None:
                                return None
                            waiting_x = _waiting_label_prices(stx)
                            if not waiting_x:
                                return None
                            rows_x = _reload_page_and_list_journal_rows(page, tv, settle_ms)
                            m2 = _pick_matching_waiting_row(rows_x, waiting_x, processed)
                            if m2 is None:
                                return None
                            np, nline, nlab = m2
                            same = nlab == touched_label and abs(np - touched_price) <= _EPS
                            if same:
                                return None
                            processed.add(nline.strip())
                            return (np, nline, nlab)
                        except Exception:
                            return None

                    inner_outcome, inner_rid = run_intraday_touch_flow(
                        settings=settings,
                        params=tfp,
                        touched_price=touched,
                        touched_label=tlab,
                        touch_line=line,
                        initial_response_id=rid,
                        browser_context=context,
                        last_alert_path=lap,
                        page=page,
                        tv=tv,
                        settle_ms=settle_ms,
                        poll_seconds=params.poll_seconds,
                        poll_supersede_touch=_poll_sup,
                    )
                    current_response_id = inner_rid

                    st2 = read_last_alert_state(lap)
                    if st2 is not None and not watchlist_journal_active_work(st2):
                        _journal_log(tz, "Kết quả: all_plans_resolved (đã xử lý đủ 3 plan).")
                        return "all_plans_resolved"
                    if inner_outcome == "cutoff":
                        _journal_log(tz, "Kết quả cuối: cutoff_time (hết giờ trong vòng trong).")
                        return "cutoff_time"
                    _journal_log(
                        tz,
                        f"Vòng trong kết thúc ({inner_outcome!s}) — tiếp tục vòng ngoài nếu còn plan chờ.",
                    )
                    continue

                _journal_log(
                    tz,
                    f"Chưa có dòng mới khớp plan đang chờ — tick TP1 (Last) rồi nghỉ {params.poll_seconds}s.",
                )
                _tp1_tick_from_watchlist_last()
                time.sleep(max(1.0, params.poll_seconds))

            if params.session_cutoff_end is not None:
                ce_s = params.session_cutoff_end.astimezone(ZoneInfo(tz)).strftime(
                    "%Y-%m-%d %H:%M"
                )
                _journal_log(
                    tz,
                    f"Đã qua mốc dừng phiên ({ce_s} {tz}) — dừng vòng ngoài.",
                )
            else:
                _journal_log(
                    tz,
                    f"Đã qua giờ kết thúc ({params.until_hour}:00 {tz}) — dừng vòng ngoài.",
                )
            return "cutoff_time"
        finally:
            _journal_log(tz, "Đóng trình duyệt (Playwright).")
            if use_browser_service:
                try:
                    page.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass
            else:
                close_browser_and_context(browser, context)
