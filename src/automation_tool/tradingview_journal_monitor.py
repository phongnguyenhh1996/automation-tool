"""
TradingView: tab Nhật ký — khớp một trong ba giá → Coinmap XAUUSD M5 + OpenAI intraday.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional, Set

from playwright.sync_api import BrowserContext, Page, sync_playwright
from zoneinfo import ZoneInfo

from automation_tool.coinmap import (
    _maybe_tradingview_dark_mode,
    _maybe_tradingview_login,
    capture_charts,
    load_coinmap_yaml,
)
from automation_tool.config import Settings
from automation_tool.images import coinmap_xauusd_5m_json_path
from automation_tool.mt5_execute import execute_trade
from automation_tool.mt5_openai_parse import (
    parse_journal_intraday_action_from_openai_text,
    parse_openai_output_md,
)
from automation_tool.openai_errors import re_raise_unless_openai
from automation_tool.openai_prompt_flow import (
    JOURNAL_INTRADAY_FIRST_USER_TEMPLATE,
    JOURNAL_INTRADAY_RETRY_USER_TEMPLATE,
    run_single_followup_responses,
)
from automation_tool.playwright_browser import close_browser_and_context, launch_chrome_context
from automation_tool.state_files import (
    LOAI,
    LastAlertState,
    VAO_LENH,
    VUNG_CHO,
    all_plans_terminal,
    default_last_alert_prices_path,
    read_last_alert_state,
    read_last_response_id,
    update_single_plan_status,
    write_last_response_id,
)
from automation_tool.telegram_bot import send_openai_output_to_telegram
from automation_tool.tradingview_alerts import (
    _open_alerts_list_panel,
    parse_tv_alert_price_from_description,
)

_EPS = 0.01


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

InnerLoopOutcome = Literal["entered", "rejected", "cutoff"]


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
    mt5_execute: bool = False
    mt5_symbol: Optional[str] = None
    mt5_dry_run: bool = True


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


def _before_cutoff(timezone_name: str, until_hour: int) -> bool:
    z = ZoneInfo(timezone_name)
    now = datetime.now(z)
    cutoff = now.replace(hour=until_hour, minute=0, second=0, microsecond=0)
    return now < cutoff


def _sleep_wait_minutes_respecting_cutoff(
    wait_minutes: int,
    timezone_name: str,
    until_hour: int,
) -> bool:
    """
    Sleep up to ``wait_minutes`` in small steps. Returns False if cutoff passed during sleep.
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
        f"Bắt đầu chờ {wait_minutes} phút trước lần chụp Coinmap M5 + OpenAI tiếp theo{wake_hint}.",
    )
    last_progress_log = 0.0
    while time.time() < end:
        if not _before_cutoff(timezone_name, until_hour):
            _journal_log(timezone_name, "Hết khung giờ (--until-hour) trong lúc chờ — dừng.")
            return False
        now = time.time()
        remain = end - now
        if now - last_progress_log >= 120.0:
            _journal_log(
                timezone_name,
                f"… vẫn chờ: còn khoảng {remain / 60.0:.1f} phút",
            )
            last_progress_log = now
        time.sleep(min(30.0, remain))
    ok = _before_cutoff(timezone_name, until_hour)
    if ok:
        _journal_log(timezone_name, f"Đã chờ xong {wait_minutes} phút — chụp Coinmap + gửi OpenAI lại.")
    return ok


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
) -> InnerLoopOutcome:
    """``chờ`` → capture lại + hỏi lại; lặp tới loại / VÀO LỆNH (có trade_line) / hết giờ."""
    tz = params.timezone_name
    prev_id = read_last_response_id() or initial_response_id
    first = True
    inner_i = 0

    _journal_log(
        tz,
        f"=== Vòng trong (touch) — label={touched_label} | giá chạm={touched_price} | "
        f"dòng Nhật ký: {_truncate(journal_line, 200)!s}",
    )

    while _before_cutoff(params.timezone_name, params.until_hour):
        inner_i += 1
        st = read_last_alert_state(last_alert_path)
        if st is None:
            raise SystemExit(f"Missing last alert state at {last_alert_path}")
        p1, p2, p3 = st.prices

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
                f"No XAUUSD 5m Coinmap JSON under {params.charts_dir}. "
                "Check coinmap_update.yaml capture_plan and api_data_export."
            )
        _journal_log(tz, f"JSON M5 XAUUSD: {json_path}")

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
                coinmap_json_path=json_path,
                previous_response_id=prev_id,
                vector_store_ids=settings.openai_vector_store_ids,
                store=settings.openai_responses_store,
                include=settings.openai_responses_include,
            )
        except Exception as e:
            re_raise_unless_openai(e)

        _journal_log(tz, f"OpenAI response_id: {new_id}")
        _journal_log(tz, f"--- Toàn bộ output OpenAI ({len(out_text)} ký tự) ---")
        print(out_text, flush=True)
        _journal_log(tz, "--- Hết output OpenAI ---")
        write_last_response_id(new_id)
        prev_id = new_id

        act = parse_journal_intraday_action_from_openai_text(out_text)
        _journal_log(tz, f"Parse intraday (JSON hoặc [OUTPUT_NGAN_GON]): Hành động = {act!r}")
        if act == "VÀO LỆNH":
            parsed, tl_err = parse_openai_output_md(out_text, symbol_override=params.mt5_symbol)
            if tl_err or parsed is None:
                _journal_log(
                    tz,
                    f"VÀO LỆNH nhưng trade_line không hợp lệ: {tl_err} — coi như chờ, chờ {params.wait_minutes} phút.",
                )
                if not _sleep_wait_minutes_respecting_cutoff(
                    params.wait_minutes,
                    params.timezone_name,
                    params.until_hour,
                ):
                    _journal_log(tz, "Hết giờ trong lúc chờ (trade_line) — cutoff.")
                    return "cutoff"
                first = False
                continue
            update_single_plan_status(touched_label, VAO_LENH, path=last_alert_path)
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
            return "entered"
        if act == "loại":
            update_single_plan_status(touched_label, LOAI, path=last_alert_path)
            _journal_log(tz, "Kết thúc vòng trong: loại (vùng không còn cơ hội) — đã ghi status.")
            return "rejected"
        if act == "chờ":
            _journal_log(tz, f"Hành động: chờ — sẽ nghỉ {params.wait_minutes} phút rồi chụp M5 + hỏi lại.")
            if not _sleep_wait_minutes_respecting_cutoff(
                params.wait_minutes,
                params.timezone_name,
                params.until_hour,
            ):
                _journal_log(tz, "Hết giờ trong lúc chờ (chờ) — cutoff.")
                return "cutoff"
            first = False
            continue

        _journal_log(
            tz,
            "Cảnh báo: không parse được Hành động (chờ / loại / VÀO LỆNH) trong [OUTPUT_NGAN_GON] — coi như chờ.",
        )
        if not _sleep_wait_minutes_respecting_cutoff(
            params.wait_minutes,
            params.timezone_name,
            params.until_hour,
        ):
            _journal_log(tz, "Hết giờ sau chờ mặc định — cutoff.")
            return "cutoff"
        first = False

    _journal_log(tz, "Vòng trong: hết giờ --until-hour — cutoff.")
    return "cutoff"


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
    _journal_log(
        tz,
        f"Bắt đầu monitor | chart={tv.get('chart_url')!s} | headless={params.headless} | "
        f"viewport={vw}x{vh} | settle_ms={settle_ms} | last_alert={lap}",
    )
    _journal_log(
        tz,
        f"3 giá + status: {p1} | {p2} | {p3} | {st0.status_by_label} (epsilon={_EPS}) | "
        f"poll sau mỗi chu kỳ={params.poll_seconds}s | "
        f"chờ OpenAI={params.wait_minutes}m | tới {params.until_hour}:00 ({tz})",
    )

    processed: Set[str] = set()
    outer_cycle = 0

    with sync_playwright() as p:
        browser, context = launch_chrome_context(
            p,
            headless=params.headless,
            storage_state_path=params.storage_state_path,
            viewport_width=vw,
            viewport_height=vh,
        )
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

            while _before_cutoff(params.timezone_name, params.until_hour):
                outer_cycle += 1
                now_local = datetime.now(ZoneInfo(tz)).strftime("%H:%M:%S")
                _journal_log(
                    tz,
                    f"--- Vòng ngoài #{outer_cycle} (giờ địa phương ~{now_local}) ---",
                )
                st = read_last_alert_state(lap)
                if st is None:
                    raise SystemExit(f"Lost last alert state at {lap}")
                if all_plans_terminal(st):
                    _journal_log(
                        tz,
                        "Cả 3 plan đã có trạng thái vao_lenh hoặc loai — dừng monitor.",
                    )
                    return "all_plans_resolved"
                waiting = _waiting_label_prices(st)
                if not waiting:
                    _journal_log(tz, "Không còn plan vung_cho — dừng.")
                    return "all_plans_resolved"
                _journal_log(
                    tz,
                    f"Plan còn chờ ({VUNG_CHO}): "
                    + ", ".join(f"{lab}={px}" for lab, px in waiting),
                )

                _journal_log(tz, "Reload trang TradingView…")
                page.reload(wait_until="domcontentloaded", timeout=120_000)
                _journal_log(tz, f"Chờ settle {settle_ms}ms sau reload…")
                page.wait_for_timeout(settle_ms)
                _journal_log(tz, "Mở panel Cảnh báo (danh sách)…")
                _open_alerts_list_panel(page, tv)
                _journal_log(tz, "Chuyển tab Nhật ký (#log)…")
                open_journal_tab(page, tv)
                rows = list_journal_rows(page, tv)
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
                    rid = read_last_response_id() or initial_response_id
                    inner = _run_intraday_touch_loop(
                        settings=settings,
                        params=params,
                        touched_price=touched,
                        touched_label=tlab,
                        journal_line=line,
                        initial_response_id=rid,
                        browser_context=context,
                        last_alert_path=lap,
                    )
                    st2 = read_last_alert_state(lap)
                    if st2 is not None and all_plans_terminal(st2):
                        _journal_log(tz, "Kết quả: all_plans_resolved (đã xử lý đủ 3 plan).")
                        return "all_plans_resolved"
                    if inner == "cutoff":
                        _journal_log(tz, "Kết quả cuối: cutoff_time (hết giờ trong vòng trong).")
                        return "cutoff_time"
                    _journal_log(
                        tz,
                        f"Vòng trong kết thúc ({inner!s}) — tiếp tục vòng ngoài nếu còn plan chờ.",
                    )
                    continue

                _journal_log(
                    tz,
                    f"Chưa có dòng mới khớp plan đang chờ — nghỉ {params.poll_seconds}s trước chu kỳ reload tiếp theo.",
                )
                time.sleep(max(1.0, params.poll_seconds))

            _journal_log(
                tz,
                f"Đã qua giờ kết thúc ({params.until_hour}:00 {tz}) — dừng vòng ngoài.",
            )
            return "cutoff_time"
        finally:
            _journal_log(tz, "Đóng trình duyệt (Playwright).")
            close_browser_and_context(browser, context)
