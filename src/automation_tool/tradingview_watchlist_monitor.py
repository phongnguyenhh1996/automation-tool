"""
TradingView: Watchlist price monitor (no alerts).

Open a TradingView chart, ensure Watchlist is open, read realtime "Last" price from the
Watchlist row, and trigger the shared touch flow when price touches one of the waiting
zone levels in last_alert_prices.json.

Key requirement: only read/parse price when the price span does NOT have a CSS class
starting with highlightUp- or highlightDown- (unstable highlight state).

Optional: set ``AUTOMATION_USE_BROWSER_SERVICE=1`` and run ``coinmap-automation browser up``
first so this monitor attaches via CDP (``connect_over_cdp``) to the long-lived service
instead of launching a new browser (requires ``PLAYWRIGHT_CHROME_USER_DATA_DIR`` / same profile).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import BrowserContext, Page, sync_playwright
from zoneinfo import ZoneInfo

from automation_tool.coinmap import (
    _maybe_tradingview_dark_mode,
    _maybe_tradingview_login,
    load_coinmap_yaml,
)
from automation_tool.config import Settings
from automation_tool.images import DEFAULT_MAIN_CHART_SYMBOL, get_active_main_symbol
from automation_tool.browser_client import try_attach_playwright_via_service
from automation_tool.playwright_browser import close_browser_and_context, launch_chrome_context
from automation_tool.state_files import (
    VUNG_CHO,
    default_last_alert_prices_path,
    read_last_alert_state,
    read_last_response_id,
    watchlist_journal_active_work,
    write_journal_monitor_first_run,
)
from automation_tool.tradingview_last_price import read_watchlist_last_price_wait_stable
from automation_tool.tradingview_touch_flow import (
    TouchFlowParams,
    before_cutoff,
    compute_session_cutoff,
    run_intraday_touch_flow,
)

from automation_tool.coinmap import _tradingview_ensure_watchlist_open  # reuse internal helper
from automation_tool.tp1_followup import maybe_post_entry_tp1_tick

_log = logging.getLogger("automation_tool.tv_watchlist")

_EPS = 0.01


def _tvw_log(timezone_name: str, msg: str) -> None:
    try:
        z = ZoneInfo(timezone_name)
        ts = datetime.now(z).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.getLogger("automation_tool.journal").info(f"[{ts}] tv-watchlist | {msg}")


@dataclass(frozen=True)
class WatchlistMonitorParams:
    coinmap_tv_yaml: Path
    capture_coinmap_yaml: Path
    charts_dir: Path
    storage_state_path: Optional[Path]
    headless: bool
    no_save_storage: bool
    watchlist_poll_seconds: float
    wait_minutes: int
    until_hour: int
    timezone_name: str
    no_telegram: bool
    last_alert_path: Optional[Path] = None
    mt5_execute: bool = True
    mt5_symbol: Optional[str] = None
    mt5_dry_run: bool = False
    session_cutoff_end: Optional[datetime] = None


def _waiting_label_prices(state) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for i, lab in enumerate(state.labels):
        if state.status_by_label.get(lab, VUNG_CHO) == VUNG_CHO:
            out.append((lab, state.prices[i]))
    return out


def _poll_supersede_from_watchlist(
    *,
    page: Page,
    tv: dict[str, Any],
    symbol: str,
    last_alert_path: Path,
    touched_label: str,
    touched_price: float,
) -> Optional[tuple[float, str, str]]:
    """
    During inner-loop sleeps, check if another waiting plan has been touched.
    If yes, return (price, touch_line, label).
    """
    st = read_last_alert_state(last_alert_path)
    if st is None:
        return None
    waiting = _waiting_label_prices(st)
    if not waiting:
        return None

    wms = min(15_000, max(2_000, int(float(tv.get("watchlist_poll_seconds", 10)) * 1000)))
    p = read_watchlist_last_price_wait_stable(
        page, tv, symbol=symbol, timeout_ms=wms, poll_ms=250
    )
    # In touch-flow sleeps, this function may be called frequently (e.g. every 10s).
    # To avoid Telegram/log spam, emit a lightweight heartbeat at most once per minute.
    if p is None:
        # Optional heartbeat when price cannot be read (highlight up/down).
        now_s = datetime.now().timestamp()
        last_log = float(getattr(_poll_supersede_from_watchlist, "_last_heartbeat_s", 0.0))
        if now_s - last_log >= 60.0:
            setattr(_poll_supersede_from_watchlist, "_last_heartbeat_s", now_s)
            # Log via the shared "journal" logger so it follows existing Telegram piping.
            try:
                z = ZoneInfo(tv.get("timezone") or tv.get("timezone_name") or "UTC")
                ts = datetime.now(z).strftime("%Y-%m-%d %H:%M:%S %Z")
            except Exception:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logging.getLogger("automation_tool.journal").info(
                f"[{ts}] tv-touch | Poll (chờ): watchlist last=unavailable (highlight) | waiting_plans={len(waiting)}"
            )
        return None

    now_s = datetime.now().timestamp()
    last_log = float(getattr(_poll_supersede_from_watchlist, "_last_heartbeat_s", 0.0))
    if now_s - last_log >= 60.0:
        setattr(_poll_supersede_from_watchlist, "_last_heartbeat_s", now_s)
        try:
            z = ZoneInfo(tv.get("timezone") or tv.get("timezone_name") or "UTC")
            ts = datetime.now(z).strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logging.getLogger("automation_tool.journal").info(
            f"[{ts}] tv-touch | Poll (chờ): watchlist last={p} | waiting_plans={len(waiting)}"
        )

    for lab, tp in waiting:
        if abs(p - tp) <= _EPS:
            same = lab == touched_label and abs(p - touched_price) <= _EPS
            if same:
                return None
            line = f"watchlist:{symbol} last={p}"
            return (p, line, lab)
    return None


def run_tv_watchlist_monitor(
    *,
    settings: Settings,
    params: WatchlistMonitorParams,
    initial_response_id: str,
) -> str:
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

    poll_s = float(tv.get("watchlist_poll_seconds", params.watchlist_poll_seconds) or params.watchlist_poll_seconds)
    if poll_s <= 0:
        poll_s = 10.0

    # Which symbol to read from watchlist?
    sym = (tv.get("watchlist_symbol_short") or "").strip().upper()
    if not sym or sym == DEFAULT_MAIN_CHART_SYMBOL:
        # Default to the project's active main symbol (data/.main_chart_symbol or env).
        sym = get_active_main_symbol().strip().upper()

    zinfo = ZoneInfo(tz)
    first_run = datetime.now(zinfo)
    session_cutoff_end = compute_session_cutoff(first_run, tz)
    write_journal_monitor_first_run(
        started_at=first_run,
        session_cutoff_end=session_cutoff_end,
        timezone_name=tz,
        last_alert_path=lap,
    )
    params = replace(params, session_cutoff_end=session_cutoff_end)

    _tvw_log(
        tz,
        f"Bắt đầu watchlist-monitor | symbol={sym} | poll={poll_s}s | "
        f"headless={params.headless} | viewport={vw}x{vh} | settle_ms={settle_ms} | last_alert={lap}",
    )

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
            _tvw_log(tz, "Mở TradingView (goto)…")
            page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            _tvw_log(tz, "Đăng nhập TradingView (nếu bật trong yaml)…")
            _maybe_tradingview_login(page, tv, settings.coinmap_email, settings.tradingview_password)
            page.wait_for_timeout(int(tv.get("initial_settle_ms", 3000)))
            _maybe_tradingview_dark_mode(page, tv)

            _tvw_log(tz, "Mở Watchlist panel (nếu chưa mở)…")
            _tradingview_ensure_watchlist_open(page, tv)

            while before_cutoff(
                params.timezone_name,
                params.until_hour,
                session_cutoff_end=params.session_cutoff_end,
            ):
                st = read_last_alert_state(lap)
                if st is None:
                    raise SystemExit(f"Lost last alert state at {lap}")
                if not watchlist_journal_active_work(st):
                    _tvw_log(tz, "Không còn vùng chờ và không còn theo dõi TP1 sau vào lệnh — dừng.")
                    return "all_plans_resolved"

                wms_outer = min(15_000, max(2_000, int(poll_s * 1000)))
                p_last = read_watchlist_last_price_wait_stable(
                    page, tv, symbol=sym, timeout_ms=wms_outer, poll_ms=250
                )
                if p_last is None:
                    _tvw_log(tz, "Giá Last chưa ổn định sau chờ — skip nhịp.")
                    page.wait_for_timeout(int(poll_s * 1000))
                    continue

                waiting = _waiting_label_prices(st)
                match = None
                if waiting:
                    for lab, tp in waiting:
                        if abs(p_last - tp) <= _EPS:
                            match = (lab, tp)
                            break

                if match is None:
                    rid = initial_response_id
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
                            initial_response_id=initial_response_id,
                            tick_source="watchlist",
                        )
                    except Exception as e:
                        _tvw_log(tz, f"post-entry TP1 tick lỗi (bỏ qua): {e!s}")
                    else:
                        if rid:
                            initial_response_id = rid
                    _tvw_log(tz, f"Không chạm vùng chờ — last={p_last}. Chờ {poll_s}s…")
                    page.wait_for_timeout(int(poll_s * 1000))
                    continue

                touched_label, touched_target = match[0], match[1]
                touch_line = f"watchlist:{sym} last={p_last} target={touched_target}"
                _tvw_log(tz, f"CHẠM: {touched_label}@{touched_target} (last={p_last}) — vào vòng trong.")

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
                )

                def _poll_sup(tlab: str, tprice: float):
                    return _poll_supersede_from_watchlist(
                        page=page,
                        tv=tv,
                        symbol=sym,
                        last_alert_path=lap,
                        touched_label=tlab,
                        touched_price=tprice,
                    )

                outcome = run_intraday_touch_flow(
                    settings=settings,
                    params=tfp,
                    touched_price=p_last,
                    touched_label=touched_label,
                    touch_line=touch_line,
                    initial_response_id=initial_response_id,
                    browser_context=context,
                    last_alert_path=lap,
                    page=page,
                    tv=tv,
                    settle_ms=settle_ms,
                    poll_seconds=poll_s,
                    poll_supersede_touch=_poll_sup,
                )
                _tvw_log(tz, f"Vòng trong kết thúc: {outcome}")
                initial_response_id = read_last_response_id() or initial_response_id

                # If superseded happened, the touch flow already marked LOAI for previous label.
                # Continue outer loop.
                page.wait_for_timeout(int(poll_s * 1000))

            _tvw_log(tz, "Hết khung giờ — dừng monitor.")
            return "cutoff_time"
        finally:
            _tvw_log(tz, "Đóng trình duyệt (Playwright).")
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

