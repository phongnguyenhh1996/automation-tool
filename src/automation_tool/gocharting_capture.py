from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml
from playwright.sync_api import BrowserContext, Page, sync_playwright

from automation_tool.browser_client import browser_service_state_path, try_attach_playwright_via_service
from automation_tool.chart_payload_validate import (
    prepare_gocharting_csv_file,
    validate_gocharting_csv_file,
)
from automation_tool.config import default_storage_state_path
from automation_tool.gocharting_capture_lock import gocharting_capture_lock
from automation_tool.playwright_browser import close_browser_and_context, launch_chrome_context

_log = logging.getLogger(__name__)

_INTERVAL_SLUG_RE = re.compile(r"[^\w]+")
_DEFAULT_DOWNLOAD_BUTTON = (
    'button:has(span div:text-is("Download")), '
    'button:has(span div:text-is("Tải xuống"))'
)
_DEFAULT_DETAIL_HISTORY_STEPS = 3
# ``update-scalp --gocharting``: detail zoom only (no pan-back history PNGs).
GOCHARTING_UPDATE_SCALP_DETAIL_HISTORY_STEPS = 0
_DEFAULT_CHART_LOAD_MS = 2000
_DEFAULT_EMPTY_DOWNLOAD_MAX_RETRIES = 2
_DEFAULT_EMPTY_DOWNLOAD_RETRY_DELAY_MS = 500


def _chart_load_ms(cfg: dict[str, Any], *, section: Optional[str] = None) -> int:
    if section:
        block = cfg.get(section) or {}
        if isinstance(block, dict) and block.get("chart_load_ms") is not None:
            try:
                return max(0, int(block["chart_load_ms"]))
            except (TypeError, ValueError):
                pass
    try:
        return max(0, int(cfg.get("chart_load_ms", _DEFAULT_CHART_LOAD_MS)))
    except (TypeError, ValueError):
        return _DEFAULT_CHART_LOAD_MS


def _wait_for_chart_before_export(
    page: Page,
    cfg: dict[str, Any],
    *,
    section: Optional[str] = None,
) -> None:
    ms = _chart_load_ms(cfg, section=section)
    if ms > 0:
        page.wait_for_timeout(ms)


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
    """``{stem}_detail_{suffix}.png`` e.g. ``detail_zoom``, ``detail_back_1``, ``detail_back_2``."""
    stem = gocharting_export_stem(stamp, export_label, interval)
    return charts_dir / f"{stem}_detail_{suffix}.png"


def gocharting_detail_back_suffix(step_index: int) -> str:
    """History pan step N → ``back_N`` (e.g. step 2 → ``back_2``)."""
    return f"back_{max(1, int(step_index))}"


def gocharting_detail_back_suffixes(*, steps: int) -> list[str]:
    return [gocharting_detail_back_suffix(i) for i in range(1, max(1, steps) + 1)]


def _session_viewport(cfg: dict[str, Any]) -> tuple[int, int]:
    vw = int(cfg.get("viewport_width", 1920))
    vh = int(cfg.get("viewport_height", 1080))
    return vw, vh


def _normalize_interval_key(interval: str) -> str:
    return (interval or "").strip().lower()


def gocharting_detail_crop_width_thirds(cfg: dict[str, Any]) -> bool:
    """When True (default), split detail PNGs into 3 horizontal panels for OpenAI."""
    detail = cfg.get("detail_chart") or {}
    if not isinstance(detail, dict):
        return True
    return detail.get("crop_width_thirds", True) is not False


def _detail_chart_cfg_for_interval(cfg: dict[str, Any], interval: str) -> dict[str, Any]:
    """Merge ``detail_chart`` with optional ``detail_chart.by_interval.{interval}`` overrides."""
    detail = cfg.get("detail_chart") or {}
    if not isinstance(detail, dict):
        return {}

    base = {k: v for k, v in detail.items() if k != "by_interval"}
    by_iv = detail.get("by_interval")
    if not isinstance(by_iv, dict):
        return base

    iv_key = _normalize_interval_key(interval)
    overrides: dict[str, Any] | None = None
    for key, value in by_iv.items():
        if _normalize_interval_key(str(key)) == iv_key:
            overrides = value if isinstance(value, dict) else None
            break

    if not overrides:
        return base

    merged = dict(base)
    merged.update(overrides)
    return merged


def _detail_chart_viewport(cfg: dict[str, Any], interval: Optional[str] = None) -> tuple[int, int]:
    """Detail tab viewport; width defaults to 2× session width unless overridden."""
    session_w, session_h = _session_viewport(cfg)
    if interval:
        detail = _detail_chart_cfg_for_interval(cfg, interval)
    else:
        raw = cfg.get("detail_chart") or {}
        detail = raw if isinstance(raw, dict) else {}
        detail = {k: v for k, v in detail.items() if k != "by_interval"}

    raw_w = detail.get("viewport_width")
    if raw_w is not None:
        width = int(raw_w)
    else:
        width = session_w * 2

    raw_h = detail.get("viewport_height")
    if raw_h is not None:
        height = int(raw_h)
    else:
        height = session_h

    return max(1, width), max(1, height)


def _detail_chart_browser_zoom_percent(
    cfg: dict[str, Any],
    interval: Optional[str] = None,
) -> int:
    """Browser page zoom for the detail tab (default 125%)."""
    if interval:
        detail = _detail_chart_cfg_for_interval(cfg, interval)
    else:
        raw = cfg.get("detail_chart") or {}
        detail = raw if isinstance(raw, dict) else {}
        detail = {k: v for k, v in detail.items() if k != "by_interval"}

    try:
        return max(1, int(detail.get("browser_zoom_percent", 125)))
    except (TypeError, ValueError):
        return 125


def _apply_detail_chart_viewport(
    page: Page,
    cfg: dict[str, Any],
    *,
    interval: Optional[str] = None,
) -> None:
    w, h = _detail_chart_viewport(cfg, interval)
    page.set_viewport_size({"width": w, "height": h})
    _log.debug("gocharting: detail tab viewport %sx%s", w, h)


def _apply_detail_chart_browser_zoom(
    page: Page,
    cfg: dict[str, Any],
    *,
    interval: Optional[str] = None,
) -> None:
    percent = _detail_chart_browser_zoom_percent(cfg, interval)
    if percent == 100:
        return

    page.evaluate(
        """(pct) => {
            const v = pct + '%';
            document.documentElement.style.zoom = v;
            if (document.body) document.body.style.zoom = v;
        }""",
        percent,
    )
    _log.info("gocharting: detail tab CSS zoom %s%%", percent)


def _prepare_detail_chart(page: Page, cfg: dict[str, Any]) -> None:
    """Refresh, chart zoom-in, then CSS page zoom."""
    detail = cfg.get("detail_chart") or {}
    if not isinstance(detail, dict):
        return

    refresh_id = str(detail.get("refresh_button_id") or "refresh-button")
    zoom_in_id = str(detail.get("zoom_in_button_id") or "zoomIn-button")
    zoom_clicks = int(detail.get("zoom_clicks", 2))
    delay_ms = int(detail.get("zoom_click_delay_ms", 500))

    _force_click_id(page, refresh_id)
    for _ in range(max(0, zoom_clicks)):
        _force_click_id(page, zoom_in_id, delay_ms=delay_ms)
    _apply_detail_chart_browser_zoom(page, cfg)

    percent = _detail_chart_browser_zoom_percent(cfg)
    _log.info(
        "gocharting: detail prepared (refresh + zoomIn x%s + CSS zoom %s%%)",
        zoom_clicks,
        percent,
    )


def _detail_chart_enabled(cfg: dict[str, Any]) -> bool:
    detail = cfg.get("detail_chart") or {}
    if not isinstance(detail, dict):
        return False
    return bool(str(detail.get("page_url") or "").strip())


def _symbol_detail_chart_enabled(cfg: dict[str, Any], entry: dict[str, Any]) -> bool:
    from automation_tool.gocharting_ws_decode import footprint_ws_enabled

    if footprint_ws_enabled(cfg):
        return False
    if not _detail_chart_enabled(cfg):
        return False
    if entry.get("detail_chart") is False:
        return False
    return True


def _symbol_chart_page_url(cfg: dict[str, Any], entry: dict[str, Any]) -> str:
    """Per-symbol saved chart URL, else global ``chart_page_url``."""
    url = str(entry.get("chart_page_url") or "").strip()
    if url:
        return url
    return str(cfg.get("chart_page_url") or "").strip()


def _symbol_uses_dedicated_chart(entry: dict[str, Any]) -> bool:
    return bool(str(entry.get("chart_page_url") or "").strip())


def _ensure_chart_page(
    page: Page,
    cfg: dict[str, Any],
    entry: dict[str, Any],
    email: str,
    password: str,
    *,
    current_url: Optional[str],
) -> str:
    chart_url = _symbol_chart_page_url(cfg, entry)
    if not chart_url:
        raise ValueError("gocharting.yaml chart_page_url is required")
    if current_url == chart_url:
        return chart_url
    page.goto(chart_url, wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(1200)
    if current_url is None:
        _maybe_login_gocharting(page, cfg, email, password)
    export_label = str(entry.get("export_label") or "").strip().upper()
    _log.info(
        "gocharting: opened chart page for %s → %s",
        export_label or "?",
        chart_url,
    )
    return chart_url


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


def _gocharting_tick_search_already_set(
    current: str,
    *,
    query: str,
    export_label: str,
) -> bool:
    """True when tick search input already shows the symbol we would search for."""
    cur = (current or "").strip()
    if not cur:
        return False
    cur_u = cur.upper()
    q = (query or "").strip()
    label = (export_label or "").strip().upper()
    if cur_u == q.upper():
        return True
    if label and cur_u == label:
        return True
    if ":" in q:
        suffix = q.rsplit(":", 1)[-1].strip().upper()
        if suffix and cur_u == suffix:
            return True
    return False


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
    current = str(search_input.input_value(timeout=5_000)).strip()
    if _gocharting_tick_search_already_set(current, query=query, export_label=export_label):
        _log.info(
            "gocharting: symbol search skipped — input already %r (export_label=%s, query=%r)",
            current,
            export_label,
            query,
        )
        return

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

    _force_click_id(page, refresh_id)
    for _ in range(max(0, zoom_clicks)):
        _force_click_id(page, zoom_out_id, delay_ms=delay_ms)
    _log.debug("gocharting: overview prepared (refresh + zoomOut x%s)", zoom_clicks)


def _empty_download_max_retries(cfg: dict[str, Any]) -> int:
    try:
        return max(0, int(cfg.get("empty_download_max_retries", _DEFAULT_EMPTY_DOWNLOAD_MAX_RETRIES)))
    except (TypeError, ValueError):
        return _DEFAULT_EMPTY_DOWNLOAD_MAX_RETRIES


def _empty_download_retry_delay_ms(cfg: dict[str, Any]) -> int:
    try:
        return max(0, int(cfg.get("empty_download_retry_delay_ms", _DEFAULT_EMPTY_DOWNLOAD_RETRY_DELAY_MS)))
    except (TypeError, ValueError):
        return _DEFAULT_EMPTY_DOWNLOAD_RETRY_DELAY_MS


def _gocharting_png_is_empty(path: Path) -> tuple[bool, str]:
    try:
        if path.stat().st_size == 0:
            return True, "empty PNG"
    except OSError as e:
        return True, f"read error: {e}"
    return False, ""


def _save_download(page: Page, click_fn, dest: Path, timeout_ms: int) -> None:
    _ensure_dir(dest.parent)
    with page.expect_download(timeout=timeout_ms) as dl_info:
        click_fn()
    download = dl_info.value
    download.save_as(dest)


def _capture_png(
    page: Page,
    cfg: dict[str, Any],
    dest: Path,
    *,
    chart_section: Optional[str] = None,
) -> None:
    if chart_section == "detail_chart":
        _apply_detail_chart_browser_zoom(page, cfg)
    _wait_for_chart_before_export(page, cfg, section=chart_section)
    shot = cfg.get("screenshot") or {}
    open_btn = str(shot.get("open_button") or "#user-screenshot-btn")
    dl_btn = str(shot.get("download_button") or _DEFAULT_DOWNLOAD_BUTTON)
    timeout_ms = int(shot.get("download_timeout_ms", 30_000))
    escapes = int(shot.get("popup_escape_presses", 1))
    max_retries = _empty_download_max_retries(cfg)
    retry_delay_ms = _empty_download_retry_delay_ms(cfg)

    for attempt in range(max_retries + 1):
        page.locator(open_btn).first.click(timeout=15_000)
        page.wait_for_timeout(400)

        def _click_download() -> None:
            page.locator(dl_btn).first.click(timeout=15_000)

        _save_download(page, _click_download, dest, timeout_ms)
        for _ in range(max(0, escapes)):
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)

        empty, reason = _gocharting_png_is_empty(dest)
        if not empty:
            return
        if attempt < max_retries:
            _log.warning(
                "gocharting: PNG empty (%s) — retry download %d/%d (%s)",
                reason,
                attempt + 1,
                max_retries,
                dest.name,
            )
            if retry_delay_ms > 0:
                page.wait_for_timeout(retry_delay_ms)
        else:
            _log.warning(
                "gocharting: PNG still empty after %d retries (%s)",
                max_retries,
                dest.name,
            )


def _capture_csv(
    page: Page,
    cfg: dict[str, Any],
    dest: Path,
    *,
    chart_section: Optional[str] = None,
) -> None:
    _wait_for_chart_before_export(page, cfg, section=chart_section)
    csv_cfg = cfg.get("csv_export") or {}
    btn = str(
        csv_cfg.get("button_selector")
        or 'button:has(svg path[d*="M439.658,91.21"])'
    )
    timeout_ms = int(csv_cfg.get("download_timeout_ms", 30_000))
    max_retries = _empty_download_max_retries(cfg)
    retry_delay_ms = _empty_download_retry_delay_ms(cfg)

    for attempt in range(max_retries + 1):

        def _click_csv() -> None:
            page.locator(btn).first.click(timeout=15_000)

        _save_download(page, _click_csv, dest, timeout_ms)
        if prepare_gocharting_csv_file(dest):
            _log.debug("gocharting: normalized CSV on disk (%s)", dest.name)

        ok, reason = validate_gocharting_csv_file(dest)
        if ok:
            return
        if attempt < max_retries:
            _log.warning(
                "gocharting: CSV empty (%s) — retry download %d/%d (%s)",
                reason,
                attempt + 1,
                max_retries,
                dest.name,
            )
            if retry_delay_ms > 0:
                page.wait_for_timeout(retry_delay_ms)
        else:
            _log.warning(
                "gocharting: CSV still empty after %d retries (%s)",
                max_retries,
                dest.name,
            )


def _drag_in_box(
    page: Page,
    box: dict[str, float],
    *,
    x0r: float,
    y0r: float,
    x1r: float,
    y1r: float,
    drag_steps: int,
) -> None:
    x0 = box["x"] + box["width"] * x0r
    y0 = box["y"] + box["height"] * y0r
    x1 = box["x"] + box["width"] * x1r
    y1 = box["y"] + box["height"] * y1r
    page.mouse.move(x0, y0)
    page.mouse.down()
    page.mouse.move(x1, y1, steps=max(1, drag_steps))
    page.mouse.up()


def _pan_detail_chart(page: Page, cfg: dict[str, Any]) -> None:
    detail = cfg.get("detail_chart") or {}
    if not isinstance(detail, dict):
        detail = {}
    chart_id = str(detail.get("chart_root_id") or "chart-root-0")
    start_x = float(detail.get("pan_start_x_ratio", 0.1))
    end_x = float(detail.get("pan_end_x_ratio", 0.9))
    y_ratio = float(detail.get("pan_y_ratio", 0.5))
    drag_steps = int(detail.get("pan_drag_steps", 12))

    chart = _id_locator(page, chart_id).first
    box = chart.bounding_box()
    if not box:
        raise RuntimeError(f"gocharting: chart #{chart_id} has no bounding box")
    _drag_in_box(
        page,
        box,
        x0r=start_x,
        y0r=y_ratio,
        x1r=end_x,
        y1r=y_ratio,
        drag_steps=drag_steps,
    )


def _pan_detail_and_capture_back(
    page: Page,
    cfg: dict[str, Any],
    *,
    dest: Path,
) -> None:
    """Pan detail chart horizontally, then export PNG."""
    _pan_detail_chart(page, cfg)
    _capture_png(page, cfg, dest, chart_section="detail_chart")


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
    detail_history_steps: Optional[int] = None,
) -> list[Path]:
    detail = _detail_chart_cfg_for_interval(cfg, interval)
    detail_url = str(detail.get("page_url") or "").strip()
    if not detail_url:
        return []

    export_label = str(entry["export_label"]).strip().upper()
    if detail_history_steps is not None:
        history_steps = max(0, int(detail_history_steps))
    else:
        history_steps = int(detail.get("history_steps", _DEFAULT_DETAIL_HISTORY_STEPS))

    detail_cfg = {**cfg, "detail_chart": detail}
    detail_page = context.new_page()
    paths: list[Path] = []
    try:
        _apply_detail_chart_viewport(detail_page, detail_cfg)
        detail_page.goto(detail_url, wait_until="domcontentloaded", timeout=90_000)
        detail_page.wait_for_timeout(1200)
        _maybe_login_gocharting(detail_page, detail_cfg, email, password)
        _select_chart_symbol(detail_page, detail_cfg, entry)
        _select_interval(detail_page, detail_cfg, interval)

        _prepare_detail_chart(detail_page, detail_cfg)

        zoom_path = gocharting_detail_png_path(charts_dir, stamp, export_label, interval, "zoom")
        _capture_png(detail_page, detail_cfg, zoom_path, chart_section="detail_chart")
        paths.append(zoom_path)

        # Each step: pan chart left (history) then capture PNG.
        for step in range(1, history_steps + 1):
            suffix = gocharting_detail_back_suffix(step)
            back_path = gocharting_detail_png_path(charts_dir, stamp, export_label, interval, suffix)
            _pan_detail_and_capture_back(detail_page, detail_cfg, dest=back_path)
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
    detail_history_steps: Optional[int] = None,
    overview_capture: bool = True,
    gocharting_yaml: Optional[Path] = None,
    mt5_accounts_json: Optional[Path] = None,
) -> list[Path]:
    slot_filter: set[tuple[str, str]] | None = None
    if only_slots:
        slot_filter = {(lbl.upper(), iv.lower()) for lbl, iv in only_slots}

    plan = _filter_capture_plan(
        cfg,
        capture_symbols=capture_symbols,
        capture_intervals=capture_intervals,
        main_chart_symbol=main_chart_symbol,
    )

    if not overview_capture:
        paths: list[Path] = []
        for entry, _plan_sym, intervals in plan:
            export_label = str(entry["export_label"]).strip().upper()
            for interval in intervals:
                if slot_filter is not None:
                    if (export_label, interval.lower()) not in slot_filter:
                        continue
                if _symbol_detail_chart_enabled(cfg, entry):
                    detail_paths = _capture_detail_footprint(
                        context,
                        cfg,
                        charts_dir=charts_dir,
                        email=email,
                        password=password,
                        stamp=stamp,
                        entry=entry,
                        interval=interval,
                        detail_history_steps=detail_history_steps,
                    )
                    paths.extend(detail_paths)
        return paths

    if not str(cfg.get("chart_page_url") or "").strip():
        raise ValueError("gocharting.yaml chart_page_url is required")

    main_page = context.new_page()
    try:
        paths = []
        current_chart_url: Optional[str] = None
        for entry, _plan_sym, intervals in plan:
            export_label = str(entry["export_label"]).strip().upper()
            current_chart_url = _ensure_chart_page(
                main_page,
                cfg,
                entry,
                email,
                password,
                current_url=current_chart_url,
            )
            if not _symbol_uses_dedicated_chart(entry):
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
                _capture_png(main_page, cfg, png_path, chart_section="overview")
                _capture_csv(main_page, cfg, csv_path, chart_section="overview")
                paths.extend([png_path, csv_path])
                _log.info(
                    "gocharting: captured %s %s → %s + %s",
                    export_label,
                    interval,
                    png_path.name,
                    csv_path.name,
                )
                if _symbol_detail_chart_enabled(cfg, entry):
                    detail_paths = _capture_detail_footprint(
                        context,
                        cfg,
                        charts_dir=charts_dir,
                        email=email,
                        password=password,
                        stamp=stamp,
                        entry=entry,
                        interval=interval,
                        detail_history_steps=detail_history_steps,
                    )
                    paths.extend(detail_paths)
        if overview_capture:
            from automation_tool.gocharting_ws_decode import footprint_ws_enabled

            if footprint_ws_enabled(cfg):
                from automation_tool.gocharting_ws_capture import capture_footprint_ws_plan

                ws_paths = capture_footprint_ws_plan(
                    context,
                    cfg,
                    charts_dir=charts_dir,
                    email=email,
                    password=password,
                    gocharting_yaml=gocharting_yaml,
                    main_symbol=main_chart_symbol,
                    mt5_accounts_json=mt5_accounts_json,
                )
                paths.extend(ws_paths)
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
    detail_history_steps: Optional[int] = None,
    overview_capture: bool = True,
    gocharting_yaml: Optional[Path] = None,
    mt5_accounts_json: Optional[Path] = None,
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
        detail_history_steps=detail_history_steps,
        overview_capture=overview_capture,
        gocharting_yaml=gocharting_yaml,
        mt5_accounts_json=mt5_accounts_json,
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
    detail_history_steps: Optional[int] = None,
    overview_capture: bool = True,
    mt5_accounts_json: Optional[Path] = None,
) -> list[Path]:
    """
    Capture GoCharting footprint charts: PNG screenshot + CSV export per (symbol, interval).

    When ``footprint_ws.enabled`` is true, captures combined footprint JSON via WebSocket
    (``footprint_screenshot.intervals.*.page_url``) instead of detail-chart PNGs.

    ``overview_capture=False`` — chỉ capture detail footprint (legacy; no-op when WS enabled).

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
        detail_history_steps=detail_history_steps,
        overview_capture=overview_capture,
        gocharting_yaml=gocharting_yaml,
        mt5_accounts_json=mt5_accounts_json,
    )

    with gocharting_capture_lock():
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
