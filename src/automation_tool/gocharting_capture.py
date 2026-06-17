from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import yaml
from playwright.sync_api import BrowserContext, Page, sync_playwright

from automation_tool.browser_client import browser_service_state_path, try_attach_playwright_via_service
from automation_tool.chart_payload_validate import prepare_gocharting_csv_file
from automation_tool.config import default_storage_state_path
from automation_tool.playwright_browser import close_browser_and_context, launch_chrome_context

_log = logging.getLogger(__name__)

_INTERVAL_SLUG_RE = re.compile(r"[^\w]+")
_DEFAULT_DOWNLOAD_BUTTON = (
    'button:has(span div:text-is("Download")), '
    'button:has(span div:text-is("Tải xuống"))'
)
_DEFAULT_DETAIL_HISTORY_STEPS = 3


def load_gocharting_yaml(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    return yaml.safe_load(raw) or {}


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _id_locator(page: Page, element_id: str):
    return page.locator(f'[id="{element_id}"]')


def _interval_slug(interval: str) -> str:
    iv = (interval or "").strip()
    return _INTERVAL_SLUG_RE.sub("_", iv).strip("_")[:20] or "iv"


def gocharting_export_stem(stamp: str, export_label: str, interval: str) -> str:
    sym_slug = re.sub(r"[^\w.-]+", "_", export_label.strip()).strip("_")[:40] or "sym"
    return f"{stamp}_gocharting_{sym_slug}_{_interval_slug(interval)}"


def gocharting_detail_png_path(
    charts_dir: Path,
    stamp: str,
    export_label: str,
    interval: str,
    suffix: str,
) -> Path:
    """``{stem}_detail_{suffix}.png`` e.g. ``detail_zoom``, ``detail_back_3h``."""
    stem = gocharting_export_stem(stamp, export_label, interval)
    return charts_dir / f"{stem}_detail_{suffix}.png"


def _hours_back_for_interval(cfg: dict[str, Any], interval: str) -> int:
    detail = cfg.get("detail_chart") or {}
    hours_map = detail.get("hours_back") if isinstance(detail, dict) else {}
    if not isinstance(hours_map, dict):
        return 3
    iv = (interval or "").strip().lower()
    raw = hours_map.get(iv) or hours_map.get(interval)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 3


def gocharting_detail_back_suffix(interval: str, step_index: int, *, hours_back: int) -> str:
    """``back_3h`` for M5 step 1; cumulative ``step_index * hours_back``."""
    total = max(1, int(step_index)) * max(1, int(hours_back))
    return f"back_{total}h"


def gocharting_detail_back_suffixes(interval: str, *, hours_back: int, steps: int) -> list[str]:
    return [
        gocharting_detail_back_suffix(interval, i, hours_back=hours_back)
        for i in range(1, max(1, steps) + 1)
    ]


def _to_24h(hour12: int, am_pm: str) -> int:
    ap = (am_pm or "").strip().lower()
    h = int(hour12)
    if h == 12:
        return 0 if ap == "am" else 12
    return h if ap == "am" else h + 12


def _from_24h(h24: int, minute: int) -> tuple[int, int, str]:
    h24 = int(h24) % 24
    m = int(minute)
    if h24 == 0:
        return 12, m, "am"
    if h24 < 12:
        return h24, m, "am"
    if h24 == 12:
        return 12, m, "pm"
    return h24 - 12, m, "pm"


def subtract_hours_12h(hour12: int, minute: int, am_pm: str, hours: int) -> tuple[int, int, str]:
    """12-hour clock + AM/PM → subtract hours (wraps at midnight)."""
    h24 = _to_24h(hour12, am_pm)
    base = datetime(2000, 1, 1, h24, int(minute))
    result = base - timedelta(hours=int(hours))
    return _from_24h(result.hour, result.minute)


def _detail_chart_enabled(cfg: dict[str, Any]) -> bool:
    detail = cfg.get("detail_chart") or {}
    if not isinstance(detail, dict):
        return False
    return bool(str(detail.get("page_url") or "").strip())


def _resolve_symbol_entry(
    cfg: dict[str, Any],
    plan_symbol: str,
    main_chart_symbol: Optional[str],
) -> dict[str, Any]:
    symbols = cfg.get("symbols")
    if not isinstance(symbols, dict):
        wl = cfg.get("watchlist") or {}
        symbols = wl.get("symbols") if isinstance(wl, dict) else {}
    if not isinstance(symbols, dict):
        raise ValueError("gocharting.yaml symbols must be a mapping")

    key = (plan_symbol or "").strip().upper()
    if key in symbols and isinstance(symbols[key], dict):
        entry = dict(symbols[key])
    elif main_chart_symbol and key == "XAUUSD":
        main = main_chart_symbol.strip().upper()
        if main in symbols and isinstance(symbols[main], dict):
            entry = dict(symbols[main])
        else:
            entry = dict(symbols.get("XAUUSD") or {})
            if entry:
                entry["export_label"] = main
    else:
        raise ValueError(f"Unknown symbol {plan_symbol!r} in gocharting capture_plan")

    if not str(entry.get("export_label") or "").strip():
        raise ValueError(f"gocharting symbol {key!r} missing export_label")
    return entry


def _maybe_login_gocharting(page: Page, cfg: dict[str, Any], email: str, password: str) -> None:
    login = cfg.get("login") or {}
    if not isinstance(login, dict):
        return
    avatar_sel = str(login.get("avatar_button") or "#login-avatar")
    email_sel = str(login.get("email") or "#email_field")
    password_sel = str(login.get("password") or "#password_field")
    submit_sel = str(login.get("submit") or 'button[type="submit"]')

    avatar = page.locator(avatar_sel).first
    if not avatar.is_visible(timeout=3000):
        _log.debug("gocharting: login avatar not visible — assume session active")
        return

    avatar.click()
    page.wait_for_timeout(3000)

    email_loc = page.locator(email_sel).first
    password_loc = page.locator(password_sel).first
    email_visible = email_loc.is_visible(timeout=500)
    password_visible = password_loc.is_visible(timeout=500)

    if not email_visible and not password_visible:
        _log.info("gocharting: no login form after 3s — session already active")
        return

    if email_visible:
        email_loc.fill(email)
    if password_visible:
        password_loc.fill(password)
    page.locator(submit_sel).first.click()
    page.wait_for_timeout(1500)
    _log.info("gocharting: submitted login form")


def _select_chart_symbol(page: Page, cfg: dict[str, Any], entry: dict[str, Any]) -> None:
    search = cfg.get("symbol_search") or {}
    input_id = str(search.get("input_id") or "input-search-ticks-input")
    results_id = str(search.get("results_id") or "search-results")
    tmpl = str(search.get("query_template") or "EXNESS:{symbol}")
    settle_ms = int(search.get("settle_ms", 700))
    type_delay_ms = int(search.get("type_delay_ms", 400))

    export_label = str(entry["export_label"]).strip().upper()
    query = str(entry.get("search_query") or "").strip() or tmpl.format(symbol=export_label)
    openai_label = str(entry.get("openai_label") or "").strip()

    search_input = _id_locator(page, input_id).first
    search_input.click(timeout=15_000)
    search_input.fill(query)
    page.wait_for_timeout(type_delay_ms)

    _id_locator(page, results_id).locator("div").first.click(timeout=15_000)
    page.wait_for_timeout(settle_ms)

    confirm_sel = str(entry.get("result_confirm_selector") or "").strip()
    if confirm_sel:
        page.locator(confirm_sel).first.click(timeout=15_000)
        page.wait_for_timeout(settle_ms)
        _log.debug("gocharting: confirmed symbol via %s", confirm_sel)

    _log.info(
        "gocharting: selected symbol export_label=%s search_query=%r%s",
        export_label,
        query,
        f" ({openai_label})" if openai_label else "",
    )


def _select_interval(page: Page, cfg: dict[str, Any], interval: str) -> None:
    panel = cfg.get("interval_panel") or {}
    settle_ms = int(panel.get("settle_ms", 3000))
    tmpl = str(panel.get("button_selector") or 'button:has(div:text-is("{interval}"))')
    sel = tmpl.format(interval=interval)
    page.locator(sel).first.click(timeout=15_000)
    page.wait_for_timeout(settle_ms)


def _force_click_id(page: Page, element_id: str, *, delay_ms: int = 0) -> None:
    _id_locator(page, element_id).first.click(force=True, timeout=15_000)
    if delay_ms > 0:
        page.wait_for_timeout(delay_ms)


def _prepare_overview_chart(page: Page, cfg: dict[str, Any]) -> None:
    overview = cfg.get("overview") or {}
    if not isinstance(overview, dict):
        return
    refresh_id = str(overview.get("refresh_button_id") or "refresh-button")
    zoom_out_id = str(overview.get("zoom_out_button_id") or "zoomOut-button")
    zoom_clicks = int(overview.get("zoom_out_clicks", 2))
    delay_ms = int(overview.get("zoom_click_delay_ms", 500))
    settle_ms = int(overview.get("settle_ms", 2000))

    _force_click_id(page, refresh_id)
    page.wait_for_timeout(settle_ms)
    for _ in range(max(0, zoom_clicks)):
        _force_click_id(page, zoom_out_id, delay_ms=delay_ms)
    _log.debug("gocharting: overview prepared (refresh + zoomOut x%s)", zoom_clicks)


def _save_download(page: Page, click_fn, dest: Path, timeout_ms: int) -> None:
    _ensure_dir(dest.parent)
    with page.expect_download(timeout=timeout_ms) as dl_info:
        click_fn()
    download = dl_info.value
    download.save_as(dest)


def _capture_png(page: Page, cfg: dict[str, Any], dest: Path) -> None:
    shot = cfg.get("screenshot") or {}
    open_btn = str(shot.get("open_button") or "#user-screenshot-btn")
    dl_btn = str(shot.get("download_button") or _DEFAULT_DOWNLOAD_BUTTON)
    timeout_ms = int(shot.get("download_timeout_ms", 30_000))
    escapes = int(shot.get("popup_escape_presses", 1))

    page.locator(open_btn).first.click(timeout=15_000)
    page.wait_for_timeout(400)

    def _click_download() -> None:
        page.locator(dl_btn).first.click(timeout=15_000)

    _save_download(page, _click_download, dest, timeout_ms)
    for _ in range(max(0, escapes)):
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)


def _capture_csv(page: Page, cfg: dict[str, Any], dest: Path) -> None:
    csv_cfg = cfg.get("csv_export") or {}
    btn = str(
        csv_cfg.get("button_selector")
        or 'button:has(svg path[d*="M439.658,91.21"])'
    )
    timeout_ms = int(csv_cfg.get("download_timeout_ms", 30_000))

    def _click_csv() -> None:
        page.locator(btn).first.click(timeout=15_000)

    _save_download(page, _click_csv, dest, timeout_ms)
    if prepare_gocharting_csv_file(dest):
        _log.debug("gocharting: normalized CSV on disk (%s)", dest.name)


def _read_go_to_date_fields(page: Page) -> tuple[int, int, str]:
    hour_loc = page.locator('input[name="hour12"]').first
    minute_loc = page.locator('input[name="minute"]').first
    am_pm_loc = page.locator('select[name="amPm"]').first
    hour12 = int(str(hour_loc.input_value(timeout=5_000)).strip() or "12")
    minute = int(str(minute_loc.input_value(timeout=5_000)).strip() or "0")
    am_pm = str(am_pm_loc.input_value(timeout=5_000)).strip().lower() or "am"
    return hour12, minute, am_pm


def _fill_go_to_date_and_apply(
    page: Page,
    cfg: dict[str, Any],
    *,
    hour12: int,
    minute: int,
    am_pm: str,
) -> None:
    detail = cfg.get("detail_chart") or {}
    if not isinstance(detail, dict):
        detail = {}
    apply_sel = str(
        detail.get("apply_button_selector")
        or 'button:has-text("Áp dụng"), button:has-text("Apply")'
    )
    settle_ms = int(detail.get("settle_ms", 2000))

    page.locator('input[name="hour12"]').first.fill(str(int(hour12)))
    page.locator('input[name="minute"]').first.fill(str(int(minute)))
    page.locator('select[name="amPm"]').first.select_option(am_pm.lower())
    page.locator(apply_sel).first.click(timeout=15_000)
    page.wait_for_timeout(settle_ms)


def _go_to_date_and_capture_back(
    page: Page,
    cfg: dict[str, Any],
    *,
    interval: str,
    dest: Path,
    hours_back: int,
) -> None:
    detail = cfg.get("detail_chart") or {}
    if not isinstance(detail, dict):
        detail = {}
    go_btn_id = str(detail.get("go_to_date_button_id") or "go-to-date-btn")

    _id_locator(page, go_btn_id).first.click(timeout=15_000)
    page.wait_for_timeout(400)
    h, m, ap = _read_go_to_date_fields(page)
    new_h, new_m, new_ap = subtract_hours_12h(h, m, ap, hours_back)
    _fill_go_to_date_and_apply(page, cfg, hour12=new_h, minute=new_m, am_pm=new_ap)
    _capture_png(page, cfg, dest)


def _capture_detail_footprint(
    context: BrowserContext,
    cfg: dict[str, Any],
    *,
    charts_dir: Path,
    email: str,
    password: str,
    stamp: str,
    entry: dict[str, Any],
    interval: str,
) -> list[Path]:
    detail = cfg.get("detail_chart") or {}
    if not isinstance(detail, dict):
        return []
    detail_url = str(detail.get("page_url") or "").strip()
    if not detail_url:
        return []

    export_label = str(entry["export_label"]).strip().upper()
    refresh_id = str(detail.get("refresh_button_id") or "refresh-button")
    zoom_in_id = str(detail.get("zoom_in_button_id") or "zoomIn-button")
    zoom_clicks = int(detail.get("zoom_clicks", 4))
    delay_ms = int(detail.get("zoom_click_delay_ms", 500))
    settle_ms = int(detail.get("settle_ms", 2000))
    history_steps = int(detail.get("history_steps", _DEFAULT_DETAIL_HISTORY_STEPS))
    per_step_hours = _hours_back_for_interval(cfg, interval)

    detail_page = context.new_page()
    paths: list[Path] = []
    try:
        detail_page.goto(detail_url, wait_until="domcontentloaded", timeout=90_000)
        detail_page.wait_for_timeout(1200)
        _maybe_login_gocharting(detail_page, cfg, email, password)
        # Detail chart URL is symbol-specific; only switch interval.
        _select_interval(detail_page, cfg, interval)

        _force_click_id(detail_page, refresh_id)
        detail_page.wait_for_timeout(settle_ms)
        for _ in range(max(0, zoom_clicks)):
            _force_click_id(detail_page, zoom_in_id, delay_ms=delay_ms)

        zoom_path = gocharting_detail_png_path(charts_dir, stamp, export_label, interval, "zoom")
        _capture_png(detail_page, cfg, zoom_path)
        paths.append(zoom_path)

        for step in range(1, max(1, history_steps) + 1):
            suffix = gocharting_detail_back_suffix(interval, step, hours_back=per_step_hours)
            back_path = gocharting_detail_png_path(charts_dir, stamp, export_label, interval, suffix)
            _go_to_date_and_capture_back(
                detail_page,
                cfg,
                interval=interval,
                dest=back_path,
                hours_back=per_step_hours,
            )
            paths.append(back_path)

        _log.info(
            "gocharting: detail %s %s → %s",
            export_label,
            interval,
            ", ".join(p.name for p in paths),
        )
    finally:
        try:
            detail_page.close()
        except Exception:
            pass
    return paths


def _filter_capture_plan(
    cfg: dict[str, Any],
    *,
    capture_symbols: Optional[tuple[str, ...]],
    capture_intervals: Optional[tuple[str, ...]],
    main_chart_symbol: Optional[str],
) -> list[tuple[dict[str, Any], str, list[str]]]:
    raw_plan = cfg.get("capture_plan") or []
    if not isinstance(raw_plan, list):
        return []
    sym_filter = {s.strip().upper() for s in capture_symbols} if capture_symbols else None
    iv_filter = {i.strip().lower() for i in capture_intervals} if capture_intervals else None
    main = (main_chart_symbol or "").strip().upper()

    out: list[tuple[dict[str, Any], str, list[str]]] = []
    for step in raw_plan:
        if not isinstance(step, dict):
            continue
        sym = str(step.get("symbol") or "").strip().upper()
        if not sym:
            continue
        if sym_filter is not None:
            if sym not in sym_filter and not (sym == "XAUUSD" and main in sym_filter):
                continue
        intervals = step.get("intervals") or []
        if not isinstance(intervals, list):
            continue
        ivs: list[str] = []
        for iv in intervals:
            iv_s = str(iv).strip()
            if not iv_s:
                continue
            if iv_filter is not None and iv_s.lower() not in iv_filter:
                continue
            ivs.append(iv_s)
        if not ivs:
            continue
        entry = _resolve_symbol_entry(cfg, sym, main_chart_symbol)
        out.append((entry, sym, ivs))
    return out


def _capture_gocharting_in_context(
    context: BrowserContext,
    cfg: dict[str, Any],
    *,
    charts_dir: Path,
    email: str,
    password: str,
    stamp: str,
    main_chart_symbol: Optional[str],
    capture_symbols: Optional[tuple[str, ...]],
    capture_intervals: Optional[tuple[str, ...]],
    only_slots: Optional[list[tuple[str, str]]] = None,
) -> list[Path]:
    chart_url = str(cfg.get("chart_page_url") or "").strip()
    if not chart_url:
        raise ValueError("gocharting.yaml chart_page_url is required")

    main_page = context.new_page()
    try:
        main_page.goto(chart_url, wait_until="domcontentloaded", timeout=90_000)
        main_page.wait_for_timeout(1200)
        _maybe_login_gocharting(main_page, cfg, email, password)

        slot_filter: set[tuple[str, str]] | None = None
        if only_slots:
            slot_filter = {(lbl.upper(), iv.lower()) for lbl, iv in only_slots}

        paths: list[Path] = []
        detail_enabled = _detail_chart_enabled(cfg)
        plan = _filter_capture_plan(
            cfg,
            capture_symbols=capture_symbols,
            capture_intervals=capture_intervals,
            main_chart_symbol=main_chart_symbol,
        )
        for entry, _plan_sym, intervals in plan:
            export_label = str(entry["export_label"]).strip().upper()
            _select_chart_symbol(main_page, cfg, entry)
            for interval in intervals:
                if slot_filter is not None:
                    if (export_label, interval.lower()) not in slot_filter:
                        continue
                _select_interval(main_page, cfg, interval)
                _prepare_overview_chart(main_page, cfg)
                stem = gocharting_export_stem(stamp, export_label, interval)
                png_path = charts_dir / f"{stem}.png"
                csv_path = charts_dir / f"{stem}.csv"
                _capture_png(main_page, cfg, png_path)
                _capture_csv(main_page, cfg, csv_path)
                paths.extend([png_path, csv_path])
                _log.info(
                    "gocharting: captured %s %s → %s + %s",
                    export_label,
                    interval,
                    png_path.name,
                    csv_path.name,
                )
                if detail_enabled:
                    detail_paths = _capture_detail_footprint(
                        context,
                        cfg,
                        charts_dir=charts_dir,
                        email=email,
                        password=password,
                        stamp=stamp,
                        entry=entry,
                        interval=interval,
                    )
                    paths.extend(detail_paths)
        return paths
    finally:
        try:
            main_page.close()
        except Exception:
            pass


def _capture_gocharting_with_context(
    context: BrowserContext,
    *,
    cfg: dict[str, Any],
    charts_dir: Path,
    storage_state_path: Optional[Path],
    email: str,
    password: str,
    save_storage_state: bool,
    stamp: str,
    main_chart_symbol: Optional[str],
    capture_symbols: Optional[tuple[str, ...]],
    capture_intervals: Optional[tuple[str, ...]],
    only_slots: Optional[list[tuple[str, str]]] = None,
) -> list[Path]:
    paths = _capture_gocharting_in_context(
        context,
        cfg,
        charts_dir=charts_dir,
        email=email,
        password=password,
        stamp=stamp,
        main_chart_symbol=main_chart_symbol,
        capture_symbols=capture_symbols,
        capture_intervals=capture_intervals,
        only_slots=only_slots,
    )
    if save_storage_state and storage_state_path:
        _ensure_dir(storage_state_path.parent)
        context.storage_state(path=str(storage_state_path))
    return paths


def capture_gocharting(
    *,
    gocharting_yaml: Path,
    charts_dir: Path,
    email: str,
    password: str,
    storage_state_path: Optional[Path] = None,
    save_storage_state: bool = True,
    headless: bool = True,
    main_chart_symbol: Optional[str] = None,
    stamp_override: Optional[str] = None,
    capture_symbols: Optional[tuple[str, ...]] = None,
    capture_intervals: Optional[tuple[str, ...]] = None,
    clear_charts_before_capture: Optional[bool] = None,
    only_slots: Optional[list[tuple[str, str]]] = None,
    reuse_browser_context: Optional[BrowserContext] = None,
    require_browser_service: bool = False,
) -> list[Path]:
    """
    Capture GoCharting footprint charts: PNG screenshot + CSV export per (symbol, interval).

    When ``detail_chart.page_url`` is set, also captures detail footprint PNGs on a separate tab
    (zoom + go-to-date history steps).

    Attaches to the long-lived browser service when ``data/browser_service_state.json`` is
    present (same as Coinmap/TV ``capture_charts``). Otherwise launches a standalone Chrome.

    ``only_slots`` — optional list of ``(export_label, interval)`` for partial recapture.
    """
    storage = storage_state_path or default_storage_state_path()
    if not email or not password:
        if not storage.is_file():
            raise SystemExit(
                "GOCHARTING_EMAIL and GOCHARTING_PASSWORD are required "
                f"(or existing storage state at {storage})."
            )

    cfg = load_gocharting_yaml(gocharting_yaml)
    _ensure_dir(charts_dir)

    if clear_charts_before_capture is None:
        clear = bool(cfg.get("clear_charts_before_capture", False))
    else:
        clear = clear_charts_before_capture
    if clear:
        for pat in ("*_gocharting_*.png", "*_gocharting_*.csv"):
            for p in charts_dir.glob(pat):
                try:
                    p.unlink()
                except OSError:
                    pass

    stamp = stamp_override or datetime.now().strftime("%Y%m%d_%H%M%S")
    vw = int(cfg.get("viewport_width", 1920))
    vh = int(cfg.get("viewport_height", 1080))

    common_kw = dict(
        cfg=cfg,
        charts_dir=charts_dir,
        storage_state_path=storage,
        email=email,
        password=password,
        save_storage_state=save_storage_state,
        stamp=stamp,
        main_chart_symbol=main_chart_symbol,
        capture_symbols=capture_symbols,
        capture_intervals=capture_intervals,
        only_slots=only_slots,
    )

    if reuse_browser_context is not None:
        return _capture_gocharting_with_context(reuse_browser_context, **common_kw)

    with sync_playwright() as p:
        attached = try_attach_playwright_via_service(p, force=require_browser_service)
        if attached is not None:
            browser, context = attached
            use_browser_service = True
            _log.info("gocharting: attached to browser service")
        elif require_browser_service:
            raise SystemExit(
                "GoCharting capture requires browser service but could not attach via CDP. "
                "Run: coinmap-automation browser up "
                f"(state file: {browser_service_state_path()})."
            )
        else:
            browser, context = launch_chrome_context(
                p,
                headless=headless,
                storage_state_path=storage if storage.is_file() else None,
                viewport_width=vw,
                viewport_height=vh,
            )
            use_browser_service = False
            _log.info("gocharting: launched standalone Chrome (no browser service)")
        try:
            return _capture_gocharting_with_context(context, **common_kw)
        finally:
            if use_browser_service:
                try:
                    browser.close()
                except Exception:
                    pass
            else:
                close_browser_and_context(browser, context)
