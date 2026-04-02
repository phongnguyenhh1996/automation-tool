"""
TradingView: sync exactly three price alerts with target levels (Vietnamese UI).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import Page, sync_playwright

from automation_tool.coinmap import (
    _maybe_tradingview_dark_mode,
    _maybe_tradingview_login,
    load_coinmap_yaml,
)
from automation_tool.playwright_browser import close_browser_and_context, launch_chrome_context

_EPS = 0.01
_MAX_DELETE_ROUNDS = 40
_MAX_CREATE_ROUNDS = 10


def format_price_for_tradingview_input(x: float) -> str:
    """Format like ``4,708.000`` (comma thousands, dot decimals)."""
    neg = x < 0
    ax = abs(x)
    s = f"{ax:.3f}"
    whole, frac = s.split(".")
    rev = whole[::-1]
    segs: list[str] = []
    for i in range(0, len(rev), 3):
        segs.append(rev[i : i + 3][::-1])
    left = ",".join(reversed(segs))
    return ("-" if neg else "") + left + "." + frac


def parse_tv_alert_price_from_description(text: str) -> Optional[float]:
    """Parse price from e.g. ``XAUUSD Giao cắt 4,708.000`` → 4708.0."""
    text = text.strip()
    matches = list(
        re.finditer(r"\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+\.\d+", text)
    )
    if not matches:
        return None
    raw = matches[-1].group(0)
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _price_in_targets(p: float, targets: set[float]) -> bool:
    for t in targets:
        if abs(p - t) <= _EPS:
            return True
    return False


def _alerts_panel_list_locator(page: Page, tv: dict[str, Any]):
    sel = (tv.get("alerts_panel_list_selector") or "").strip()
    return page.locator(sel or "#id_alert-widget-tabs-slots_tabpanel_list")


def _alerts_list_tab_locator(page: Page, tv: dict[str, Any]):
    custom = (tv.get("alerts_list_tab_selector") or "").strip()
    if custom:
        return page.locator(custom)
    # Locale-agnostic: tab that controls the list panel (works when button#list is absent e.g. EN UI)
    return page.locator(
        '[role="tab"][aria-controls="id_alert-widget-tabs-slots_tabpanel_list"]'
    )


def _open_alerts_list_panel(page: Page, tv: dict[str, Any]) -> None:
    """
    Open the Alerts sidebar and switch to the List tab so the list panel is visible.
    Do not return early when another tab (e.g. Log) is selected — list panel stays hidden.
    """
    panel = _alerts_panel_list_locator(page, tv)
    list_tab = _alerts_list_tab_locator(page, tv)
    list_timeout = int(tv.get("alerts_list_panel_timeout_ms", 60_000))
    after_open = int(tv.get("alerts_after_open_ms", 400))

    alerts_btn = page.locator('button[data-name="alerts"]').first
    alerts_btn.wait_for(state="visible", timeout=30_000)

    def _list_tab_selected() -> bool:
        if not list_tab.count():
            return False
        try:
            return list_tab.first.get_attribute("aria-selected") == "true"
        except Exception:
            return False

    # Sidebar open + list tab active + list panel visible → done
    try:
        if panel.first.is_visible(timeout=2_000) and _list_tab_selected():
            return
    except Exception:
        pass

    # Toolbar button is a toggle: if the alerts sidebar is already open (aria-pressed="true"),
    # clicking again closes it — then the list panel never becomes visible.
    try:
        pressed = alerts_btn.get_attribute("aria-pressed")
    except Exception:
        pressed = None
    if pressed != "true":
        alerts_btn.click(timeout=15_000)
    page.wait_for_timeout(after_open)

    if list_tab.count():
        if not _list_tab_selected():
            list_tab.first.click(timeout=15_000)
            page.wait_for_timeout(300)
    else:
        # Legacy fallback (older Vietnamese DOM)
        legacy = page.locator(
            'button#list[role="tab"][aria-controls="id_alert-widget-tabs-slots_tabpanel_list"]'
        )
        if legacy.count():
            legacy.first.click(timeout=15_000)
            page.wait_for_timeout(300)

    panel.first.wait_for(state="visible", timeout=list_timeout)


def _list_alert_prices(page: Page, tv: dict[str, Any]) -> list[float]:
    panel = _alerts_panel_list_locator(page, tv)
    list_timeout = int(tv.get("alerts_list_panel_timeout_ms", 60_000))
    panel.wait_for(state="visible", timeout=list_timeout)
    panel_id = (
        (tv.get("alerts_panel_list_selector") or "").strip()
        or "#id_alert-widget-tabs-slots_tabpanel_list"
    )
    descs = page.locator(f'{panel_id} div[data-name="alert-item-description"]')
    n = descs.count()
    out: list[float] = []
    for i in range(n):
        t = descs.nth(i).inner_text(timeout=10_000)
        p = parse_tv_alert_price_from_description(t)
        if p is not None:
            out.append(p)
    return out


def _delete_alert_at_index(page: Page, tv: dict[str, Any], index: int) -> None:
    panel_id = (
        (tv.get("alerts_panel_list_selector") or "").strip()
        or "#id_alert-widget-tabs-slots_tabpanel_list"
    )
    desc = page.locator(
        f'{panel_id} div[data-name="alert-item-description"]'
    ).nth(index)
    # Description text can sit under a sibling layer; TV's row shell (itemBody) receives the hit.
    row = desc.locator("xpath=ancestor::div[contains(@class,'itemBody')][1]")
    try:
        row.click(button="right", timeout=15_000)
    except Exception:
        desc.click(button="right", timeout=15_000, force=True)
    page.wait_for_timeout(250)
    page.locator('tr[data-role="menuitem"]').filter(has_text="Xóa").first.click(
        timeout=10_000
    )
    page.wait_for_timeout(200)
    page.locator('button[name="yes"][data-qa-id="yes-btn"]').first.click(timeout=10_000)
    page.wait_for_timeout(500)


def _delete_stray_alerts(
    page: Page,
    tv: dict[str, Any],
    targets: tuple[float, float, float],
    settle_ms: int,
) -> None:
    """Remove alerts whose price is not in ``targets``. After each delete, reload the chart and
    reopen the alerts list — TradingView does not always refresh the list in place."""
    tset = set(targets)
    for _ in range(_MAX_DELETE_ROUNDS):
        prices = _list_alert_prices(page, tv)
        if not prices:
            break
        stray_idx: Optional[int] = None
        for i, p in enumerate(prices):
            if not _price_in_targets(p, tset):
                stray_idx = i
                break
        if stray_idx is None:
            break
        _delete_alert_at_index(page, tv, stray_idx)
        _reload_and_reopen(page, tv, settle_ms)


def _reload_and_reopen(page: Page, tv: dict[str, Any], settle_ms: int) -> None:
    page.reload(wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(settle_ms)
    _open_alerts_list_panel(page, tv)


def _ensure_three_alerts(
    page: Page, tv: dict[str, Any], targets: tuple[float, float, float]
) -> None:
    for _ in range(_MAX_CREATE_ROUNDS):
        on_screen = _list_alert_prices(page, tv)
        missing: list[float] = []
        for t in targets:
            if not any(abs(t - p) <= _EPS for p in on_screen):
                missing.append(t)
        if not missing:
            return
        price = missing[0]
        page.locator("#header-toolbar-alerts").first.click(timeout=15_000)
        page.wait_for_timeout(400)
        inp = page.locator(
            'input[data-qa-id="ui-lib-Input-input end-band-range-input"]'
        ).first
        inp.wait_for(state="visible", timeout=15_000)
        inp.fill("")
        inp.fill(format_price_for_tradingview_input(price))
        page.wait_for_timeout(200)
        page.locator('button[data-qa-id="submit"]').first.click(timeout=15_000)
        page.wait_for_timeout(int(tv.get("after_alert_create_ms", 1200)))
    raise RuntimeError("Could not create three alerts (timeout).")


def sync_alerts_on_page(page: Page, tv: dict[str, Any], settle_ms: int, targets: tuple[float, float, float]) -> None:
    """Assume chart URL already loaded and logged in. Mutates alerts to match ``targets``."""
    page.wait_for_timeout(int(tv.get("initial_settle_ms", 3000)))
    _maybe_tradingview_dark_mode(page, tv)
    intervals_id = (tv.get("intervals_toolbar_id") or "header-toolbar-intervals").strip().lstrip("#")
    page.locator(f"#{intervals_id}").first.wait_for(state="visible", timeout=90_000)

    _open_alerts_list_panel(page, tv)
    _delete_stray_alerts(page, tv, targets, settle_ms)
    _reload_and_reopen(page, tv, settle_ms)
    _ensure_three_alerts(page, tv, targets)


def sync_tradingview_alerts(
    *,
    coinmap_yaml: Path,
    storage_state_path: Optional[Path],
    email: Optional[str],
    tradingview_password: Optional[str],
    target_prices: tuple[float, float, float],
    headless: bool = True,
) -> None:
    """
    Launch browser, open TradingView chart from ``config/coinmap.yaml`` ``tradingview_capture``,
    sync alerts to exactly three price levels.
    """
    cfg = load_coinmap_yaml(coinmap_yaml)
    tv = cfg.get("tradingview_capture") or {}
    if not isinstance(tv, dict) or not tv.get("chart_url"):
        raise SystemExit("tradingview_capture.chart_url missing in coinmap yaml.")

    settle_ms = int(cfg.get("settle_ms", 2000))
    vw = int(cfg.get("viewport_width", 1920))
    vh = int(cfg.get("viewport_height", 1080))

    with sync_playwright() as p:
        browser, context = launch_chrome_context(
            p,
            headless=headless,
            storage_state_path=storage_state_path,
            viewport_width=vw,
            viewport_height=vh,
        )
        page = context.new_page()
        try:
            url = str(tv.get("chart_url"))
            page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            _maybe_tradingview_login(page, tv, email, tradingview_password)
            sync_alerts_on_page(page, tv, settle_ms, target_prices)
        finally:
            close_browser_and_context(browser, context)
