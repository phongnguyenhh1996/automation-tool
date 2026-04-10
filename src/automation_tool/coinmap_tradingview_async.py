"""
Async TradingView helpers for browser_service (Playwright async API).

Duplicated from sync helpers in coinmap.py / tradingview_last_price.py with await/async.
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

from playwright.async_api import Page

_FLOAT_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _parse_first_float(text: str) -> Optional[float]:
    raw = (text or "").strip().replace(",", "")
    m = _FLOAT_RE.search(raw)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def parse_first_float_trunc0(text: str) -> Optional[float]:
    v = _parse_first_float(text)
    if v is None:
        return None
    try:
        return float(int(v))
    except Exception:
        return None


async def read_watchlist_last_price_stable_async(
    page: Page,
    tv: dict[str, Any],
    *,
    symbol: str,
) -> Optional[float]:
    row_tpl = (tv.get("watchlist_row_selector") or "").strip()
    if not row_tpl:
        row_tpl = '[data-symbol-short="{symbol}"]'
    row_sel = row_tpl.format(symbol=symbol)
    row = page.locator(row_sel).first
    await row.wait_for(state="visible", timeout=30_000)

    cell_sel = (tv.get("watchlist_last_cell_selector") or "").strip()
    if not cell_sel:
        cell_sel = 'span[class*="cell"][class*="last"] span[class*="inner"]'
    price_span = row.locator(cell_sel).first
    await price_span.wait_for(state="visible", timeout=15_000)

    deny = tv.get("watchlist_price_stable_class_prefix_denylist")
    prefixes = ["highlightUp-", "highlightDown-"]
    if isinstance(deny, list) and deny:
        prefixes = [str(x) for x in deny if str(x)]

    cls = (await price_span.get_attribute("class") or "").strip()
    for pref in prefixes:
        if pref and pref in cls:
            return None

    txt = await price_span.inner_text(timeout=5_000)
    return parse_first_float_trunc0(txt)


async def read_watchlist_last_price_wait_stable_async(
    page: Page,
    tv: dict[str, Any],
    *,
    symbol: str,
    timeout_ms: int = 10_000,
    poll_ms: int = 250,
) -> Optional[float]:
    deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
    poll = max(50, int(poll_ms))
    while time.monotonic() < deadline:
        p = await read_watchlist_last_price_stable_async(page, tv, symbol=symbol)
        if p is not None:
            return p
        await page.wait_for_timeout(poll)
    return None


async def maybe_tradingview_dark_mode_async(page: Page, tv: dict[str, Any]) -> None:
    if not tv.get("dark_mode_enabled", True):
        return
    prefix = (tv.get("dark_mode_menu_button_class_prefix") or "topLeftButton-").strip()
    label_for = (tv.get("theme_switcher_label_for") or "theme-switcher").strip()
    switch_sel = (tv.get("theme_switch_input_selector") or "input#theme-switcher").strip()
    open_ms = int(tv.get("dark_mode_menu_open_ms", 500))
    after_ms = int(tv.get("dark_mode_after_theme_click_ms", 600))
    menu_sel = f'[class*="{prefix}"]'
    menu_opened = False
    try:
        menu_btn = page.locator(menu_sel).first
        await menu_btn.wait_for(state="visible", timeout=20_000)
        await menu_btn.click(timeout=15_000)
        menu_opened = True
        await page.wait_for_timeout(open_ms)
        label = page.locator(f'label[for="{label_for}"]').first
        await label.wait_for(state="visible", timeout=10_000)
        switch_input = label.locator(switch_sel).first
        await switch_input.wait_for(state="attached", timeout=10_000)
        aria_checked = (await switch_input.get_attribute("aria-checked") or "").lower()
        if aria_checked != "true":
            await label.click(timeout=10_000, force=True)
            await page.wait_for_timeout(after_ms)
    except Exception:
        pass
    finally:
        if menu_opened:
            try:
                await page.locator(menu_sel).first.click(timeout=15_000)
                await page.wait_for_timeout(after_ms)
            except Exception:
                pass


async def maybe_tradingview_login_async(
    page: Page,
    tv: dict[str, Any],
    email: Optional[str],
    password: Optional[str],
) -> None:
    if not tv.get("login_enabled", True):
        return
    if not email or not password:
        return

    intervals_id = (tv.get("intervals_toolbar_id") or "header-toolbar-intervals").strip().lstrip("#")
    chart_ready_sel = (tv.get("login_chart_ready_selector") or "").strip() or f"#{intervals_id}"
    chart_ready_timeout_ms = int(tv.get("login_chart_ready_timeout_ms", 90_000))

    prefix = (tv.get("dark_mode_menu_button_class_prefix") or "topLeftButton-").strip()
    menu_sel = f'[class*="{prefix}"]'
    open_ms = int(tv.get("login_menu_open_ms", 500))
    sign_timeout = int(tv.get("login_sign_in_visible_timeout_ms", 5_000))
    after_sign_ms = int(tv.get("login_after_sign_in_click_ms", 1_500))
    method_timeout = int(tv.get("login_method_visible_timeout_ms",  8_000))
    after_method_ms = int(tv.get("login_after_method_click_ms", 1_000))
    post_submit_ms = int(tv.get("login_post_submit_settle_ms", 800))

    email_sel = (tv.get("login_email_selector") or "").strip() or (
        'input[type="email"], input#id_username, input[name="username"], '
        'input[name="email"], input[autocomplete="username"]'
    )
    pass_sel = (tv.get("login_password_selector") or 'input[type="password"]').strip()
    submit_sel = (tv.get("login_submit_selector") or "").strip() or (
        'button[type="submit"], button:has-text("Đăng nhập"), button:has-text("Sign in")'
    )

    sign_in_custom = (tv.get("login_sign_in_selector") or "").strip()
    sign_in_text = (tv.get("login_sign_in_text") or "Đăng nhập").strip()
    login_method_sel = (tv.get("login_email_method_selector") or "").strip()
    login_method_text = (tv.get("login_email_method_text") or "").strip()
    iframe_sel = (tv.get("login_iframe_selector") or "").strip()

    menu_opened = False
    try:
        menu_btn = page.locator(menu_sel).first
        await menu_btn.wait_for(state="visible", timeout=45_000)
        await menu_btn.click(timeout=15_000)
        menu_opened = True
        await page.wait_for_timeout(open_ms)

        if sign_in_custom:
            sign_loc = page.locator(sign_in_custom).first
        else:
            sign_loc = page.get_by_text(sign_in_text, exact=True).first

        try:
            await sign_loc.wait_for(state="visible", timeout=sign_timeout)
        except Exception:
            return

        await sign_loc.click(timeout=15_000)
        menu_opened = False
        await page.wait_for_timeout(after_sign_ms)

        if iframe_sel:
            fl = page.frame_locator(iframe_sel)
            if login_method_sel:
                method_loc = fl.locator(login_method_sel).first
                await method_loc.wait_for(state="visible", timeout=method_timeout)
                await method_loc.click(timeout=15_000)
                if after_method_ms > 0:
                    await page.wait_for_timeout(after_method_ms)
            elif login_method_text:
                method_loc = fl.get_by_text(login_method_text, exact=True).first
                await method_loc.wait_for(state="visible", timeout=method_timeout)
                await method_loc.click(timeout=15_000)
                if after_method_ms > 0:
                    await page.wait_for_timeout(after_method_ms)
            email_loc = fl.locator(email_sel).first
            pass_loc = fl.locator(pass_sel).first
            sub_loc = fl.locator(submit_sel).first
        else:
            if login_method_sel:
                method_loc = page.locator(login_method_sel).first
                await method_loc.wait_for(state="visible", timeout=method_timeout)
                await method_loc.click(timeout=15_000)
                if after_method_ms > 0:
                    await page.wait_for_timeout(after_method_ms)
            elif login_method_text:
                method_loc = page.get_by_text(login_method_text, exact=True).first
                await method_loc.wait_for(state="visible", timeout=method_timeout)
                await method_loc.click(timeout=15_000)
                if after_method_ms > 0:
                    await page.wait_for_timeout(after_method_ms)
            email_loc = page.locator(email_sel).first
            pass_loc = page.locator(pass_sel).first
            sub_loc = page.locator(submit_sel).first

        await email_loc.wait_for(state="visible", timeout=45_000)
        await email_loc.fill(email, timeout=15_000)
        await pass_loc.fill(password, timeout=15_000)
        await sub_loc.click(timeout=15_000)

        await page.locator(chart_ready_sel).first.wait_for(
            state="visible",
            timeout=chart_ready_timeout_ms,
        )
        if post_submit_ms > 0:
            await page.wait_for_timeout(post_submit_ms)

    finally:
        if menu_opened:
            try:
                await page.locator(menu_sel).first.click(timeout=10_000)
                await page.wait_for_timeout(open_ms)
            except Exception:
                pass


async def tradingview_ensure_watchlist_open_async(page: Page, tv: dict[str, Any]) -> None:
    primary = (tv.get("watchlist_button_aria_label") or "").strip()
    if not primary:
        primary = "Danh sách theo dõi, thông tin chi tiết và tin tức"
    fallback = (tv.get("watchlist_button_aria_label_fallback") or "").strip()
    if not fallback:
        fallback = "Watchlist, details, and news"
    ms = int(tv.get("watchlist_open_ms", 500))
    if primary == fallback:
        btn = page.locator(f'button[aria-label="{primary}"]').first
    else:
        btn = page.locator(
            f'button[aria-label="{primary}"], button[aria-label="{fallback}"]'
        ).first
    await btn.wait_for(state="visible", timeout=30_000)
    pressed = (await btn.get_attribute("aria-pressed") or "").lower()
    if pressed != "true":
        await btn.click(timeout=15_000)
        await page.wait_for_timeout(ms)
