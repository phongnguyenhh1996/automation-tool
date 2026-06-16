from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml
from playwright.sync_api import BrowserContext, Page, sync_playwright

from automation_tool.browser_client import browser_service_state_path, try_attach_playwright_via_service
from automation_tool.config import default_storage_state_path
from automation_tool.playwright_browser import close_browser_and_context, launch_chrome_context

_log = logging.getLogger(__name__)

_INTERVAL_SLUG_RE = re.compile(r"[^\w]+")


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


def _resolve_symbol_entry(
    cfg: dict[str, Any],
    plan_symbol: str,
    main_chart_symbol: Optional[str],
) -> dict[str, Any]:
    wl = cfg.get("watchlist") or {}
    symbols = wl.get("symbols") if isinstance(wl, dict) else {}
    if not isinstance(symbols, dict):
        raise ValueError("gocharting.yaml watchlist.symbols must be a mapping")

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

    for req in ("watchlist_id", "chart_id", "export_label"):
        if not str(entry.get(req) or "").strip():
            raise ValueError(f"gocharting symbol {key!r} missing {req}")
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


def _symbol_visible(page: Page, entry: dict[str, Any]) -> bool:
    watchlist_id = str(entry["watchlist_id"])
    chart_id = str(entry["chart_id"])
    wl = _id_locator(page, watchlist_id).first
    if wl.is_visible(timeout=800):
        return True
    chart = _id_locator(page, chart_id).first
    return chart.is_visible(timeout=800)


def _select_watchlist_symbol(page: Page, cfg: dict[str, Any], entry: dict[str, Any]) -> None:
    if _symbol_visible(page, entry):
        chart_id = str(entry["chart_id"])
        _id_locator(page, chart_id).first.click(timeout=15_000)
        page.wait_for_timeout(500)
        return

    wl_cfg = cfg.get("watchlist") or {}
    toggle = str(wl_cfg.get("toggle_button") or "#watchlist-icontab")
    page.locator(toggle).first.click(timeout=15_000)
    page.wait_for_timeout(600)

    chart_id = str(entry["chart_id"])
    _id_locator(page, chart_id).first.click(timeout=20_000)
    page.wait_for_timeout(700)


def _select_interval(page: Page, cfg: dict[str, Any], interval: str) -> None:
    panel = cfg.get("interval_panel") or {}
    settle_ms = int(panel.get("settle_ms", 2000))
    tmpl = str(panel.get("button_selector") or 'button:has(div:text-is("{interval}"))')
    sel = tmpl.format(interval=interval)
    page.locator(sel).first.click(timeout=15_000)
    page.wait_for_timeout(settle_ms)


def _save_download(page: Page, click_fn, dest: Path, timeout_ms: int) -> None:
    _ensure_dir(dest.parent)
    with page.expect_download(timeout=timeout_ms) as dl_info:
        click_fn()
    download = dl_info.value
    download.save_as(dest)


def _capture_png(page: Page, cfg: dict[str, Any], dest: Path) -> None:
    shot = cfg.get("screenshot") or {}
    open_btn = str(shot.get("open_button") or "#user-screenshot-btn")
    dl_btn = str(shot.get("download_button") or 'button:has(span div:text-is("Download"))')
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
    page: Page,
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

    page.goto(chart_url, wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(1200)
    _maybe_login_gocharting(page, cfg, email, password)

    slot_filter: set[tuple[str, str]] | None = None
    if only_slots:
        slot_filter = {(lbl.upper(), iv.lower()) for lbl, iv in only_slots}

    paths: list[Path] = []
    plan = _filter_capture_plan(
        cfg,
        capture_symbols=capture_symbols,
        capture_intervals=capture_intervals,
        main_chart_symbol=main_chart_symbol,
    )
    for entry, _plan_sym, intervals in plan:
        export_label = str(entry["export_label"]).strip().upper()
        _select_watchlist_symbol(page, cfg, entry)
        for interval in intervals:
            if slot_filter is not None:
                if (export_label, interval.lower()) not in slot_filter:
                    continue
            _select_interval(page, cfg, interval)
            stem = gocharting_export_stem(stamp, export_label, interval)
            png_path = charts_dir / f"{stem}.png"
            csv_path = charts_dir / f"{stem}.csv"
            _capture_png(page, cfg, png_path)
            _capture_csv(page, cfg, csv_path)
            paths.extend([png_path, csv_path])
            _log.info(
                "gocharting: captured %s %s → %s + %s",
                export_label,
                interval,
                png_path.name,
                csv_path.name,
            )
    return paths


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
    page = context.new_page()
    try:
        paths = _capture_gocharting_in_context(
            page,
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
    finally:
        try:
            page.close()
        except Exception:
            pass


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
