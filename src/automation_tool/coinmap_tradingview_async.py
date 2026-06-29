"""
Async TradingView helpers for browser_service (Playwright async API).

Duplicated from sync helpers in coinmap.py / tradingview_last_price.py with await/async.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import Locator, Page

from automation_tool.coinmap import (
    _tradingview_download_empty_max_retries,
    _tradingview_download_empty_retry_delay_ms,
    _tradingview_download_pickup_timeout_ms,
    _tradingview_download_png_is_empty,
    _tradingview_download_scan_dirs,
    _tradingview_indicator_loading_markers,
    _tradingview_interval_slug,
    _tradingview_is_delete_indicator_label,
    _tradingview_legend_item_selector,
    _tradingview_legend_is_still_loading,
    _tradingview_pickup_newest_png,
    _tradingview_poll_legend_loading_state,
    _tradingview_symbol_locator,
    _tv_apply_indicator_profile,
    _tv_forbidden_indicator_groups,
    _tv_required_indicator_groups,
)

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
                try:
                    await method_loc.wait_for(state="visible", timeout=method_timeout)
                    await method_loc.click(timeout=15_000)
                    if after_method_ms > 0:
                        await page.wait_for_timeout(after_method_ms)
                except Exception:
                    pass
            elif login_method_text:
                method_loc = fl.get_by_text(login_method_text, exact=True).first
                try:
                    await method_loc.wait_for(state="visible", timeout=method_timeout)
                    await method_loc.click(timeout=15_000)
                    if after_method_ms > 0:
                        await page.wait_for_timeout(after_method_ms)
                except Exception:
                    pass
            email_loc = fl.locator(email_sel).first
            pass_loc = fl.locator(pass_sel).first
            sub_loc = fl.locator(submit_sel).first
        else:
            if login_method_sel:
                method_loc = page.locator(login_method_sel).first
                try:
                    await method_loc.wait_for(state="visible", timeout=method_timeout)
                    await method_loc.click(timeout=15_000)
                    if after_method_ms > 0:
                        await page.wait_for_timeout(after_method_ms)
                except Exception:
                    pass
            elif login_method_text:
                method_loc = page.get_by_text(login_method_text, exact=True).first
                try:
                    await method_loc.wait_for(state="visible", timeout=method_timeout)
                    await method_loc.click(timeout=15_000)
                    if after_method_ms > 0:
                        await page.wait_for_timeout(after_method_ms)
                except Exception:
                    pass
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


_log_tv = logging.getLogger("automation_tool.coinmap_tradingview_async")


async def _maybe_dismiss_tradingview_blocking_overlay_async(page: Page, tv: dict[str, Any]) -> None:
    if not tv.get("tradingview_blocking_overlay_dismiss_enabled", True):
        return
    if tv.get("tradingview_blocking_overlay_escape", True):
        try:
            await page.keyboard.press("Escape")
            ms = int(tv.get("after_tradingview_overlay_escape_ms", 250))
            if ms > 0:
                await page.wait_for_timeout(ms)
        except Exception:
            pass
    overlay_sel = (tv.get("tradingview_blocking_overlay_selector") or "").strip()
    if not overlay_sel:
        return
    hide_ms = int(tv.get("tradingview_blocking_overlay_hide_timeout_ms", 8_000))
    try:
        loc = page.locator(overlay_sel).first
        if await loc.is_visible(timeout=300):
            await loc.wait_for(state="hidden", timeout=hide_ms)
    except Exception:
        pass


async def tv_select_symbol_async(page: Page, tv: dict[str, Any], symbol: str) -> None:
    await _maybe_dismiss_tradingview_blocking_overlay_async(page, tv)
    loc = _tradingview_symbol_locator(page, tv, symbol)
    await loc.wait_for(state="visible", timeout=25_000)
    use_force = bool(tv.get("symbol_list_item_click_force", True))
    try:
        await loc.click(timeout=15_000, force=use_force)
    except Exception:
        await _maybe_dismiss_tradingview_blocking_overlay_async(page, tv)
        await loc.click(timeout=15_000, force=True)
    await page.wait_for_timeout(int(tv.get("after_symbol_select_ms", 1_500)))


async def tv_select_interval_async(
    page: Page,
    tv: dict[str, Any],
    interval_aria: str,
    settle_ms: int,
) -> None:
    intervals_id = (tv.get("intervals_toolbar_id") or "header-toolbar-intervals").strip()
    toolbar = page.locator(f"#{intervals_id}")
    interval_btn = toolbar.locator(f'button[aria-label="{interval_aria}"]').first
    await interval_btn.wait_for(state="attached", timeout=30_000)
    use_force = bool(tv.get("interval_button_click_force", True))
    await interval_btn.click(timeout=15_000, force=use_force)
    await page.wait_for_timeout(int(tv.get("after_interval_select_ms", settle_ms)))


async def tv_reset_chart_position_async(page: Page, tv: dict[str, Any]) -> None:
    shortcut = (tv.get("tradingview_reset_shortcut") or "Alt+R").strip()
    wait_ms = int(tv.get("after_tradingview_reset_ms", 400))
    if not shortcut:
        return
    try:
        await page.keyboard.press(shortcut)
        if wait_ms > 0:
            await page.wait_for_timeout(wait_ms)
    except Exception:
        pass


async def tv_list_legend_item_texts_async(page: Page, tv: dict[str, Any]) -> list[str]:
    sel = _tradingview_legend_item_selector(tv)
    loc = page.locator(sel)
    n = await loc.count()
    out: list[str] = []
    for i in range(int(n or 0)):
        try:
            t = (await loc.nth(i).inner_text(timeout=1500) or "").strip()
        except Exception:
            t = ""
        if t:
            out.append(t)
    return out


async def tv_list_legend_item_statuses_async(page: Page, tv: dict[str, Any]) -> list[str]:
    sel = _tradingview_legend_item_selector(tv)
    loc = page.locator(sel)
    n = await loc.count()
    out: list[str] = []
    for i in range(int(n or 0)):
        try:
            st = (await loc.nth(i).get_attribute("data-status") or "").strip()
        except Exception:
            st = ""
        out.append(st)
    return out


def tv_has_required_indicators_async_logic(page_texts: list[str], tv: dict[str, Any]) -> bool:
    groups = _tv_required_indicator_groups(tv)
    forbidden = _tv_forbidden_indicator_groups(tv)
    if not page_texts:
        return False
    hay = "\n".join(page_texts).lower()

    def _has_any(names: list[str]) -> bool:
        for nm in names:
            if nm and nm.lower() in hay:
                return True
        return False

    if not groups:
        return True
    for fg in forbidden:
        if _has_any(list(fg or [])):
            return False
    for g in groups:
        if not _has_any(list(g or [])):
            return False
    return True


async def tv_wait_for_legend_empty_after_clear_async(page: Page, tv: dict[str, Any]) -> list[str]:
    after_ms = int(tv.get("after_indicator_clear_ms", 450) or 450)
    if after_ms > 0:
        await page.wait_for_timeout(after_ms)

    last_legend = await tv_list_legend_item_texts_async(page, tv)
    if not last_legend:
        return []

    timeout_ms = int(tv.get("indicator_clear_verify_timeout_ms", 2500) or 0)
    poll_ms = int(tv.get("indicator_clear_verify_poll_ms", 150) or 150)
    poll_ms = max(50, poll_ms)
    deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
    while time.monotonic() < deadline:
        await page.wait_for_timeout(poll_ms)
        last_legend = await tv_list_legend_item_texts_async(page, tv)
        if not last_legend:
            return []
    return last_legend


async def tv_chart_center_xy_async(page: Page, tv: dict[str, Any]) -> tuple[float, float]:
    y_ratio = float(tv.get("chart_context_click_y_ratio", 0.10) or 0.10)
    y_ratio = min(1.0, max(0.0, y_ratio))
    raw = tv.get("chart_center_click_selector")
    sels: list[str] = []
    if isinstance(raw, str) and raw.strip():
        sels.append(raw.strip())
    elif isinstance(raw, list):
        sels.extend([str(x).strip() for x in raw if str(x).strip()])
    sels.extend(
        [
            'div[data-name="pane"]',
            '[data-qa-id="chart-container"]',
            "div.chart-container",
            "div.tv-chart",
        ]
    )
    for sel in sels:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=1500)
            bb = await loc.bounding_box()
            if bb and bb.get("width", 0) and bb.get("height", 0):
                x = float(bb["x"]) + float(bb["width"]) / 2.0
                y = float(bb["y"]) + float(bb["height"]) * y_ratio
                return x, y
        except Exception:
            continue
    vp = page.viewport_size or {"width": 1600, "height": 900}
    return float(vp["width"]) / 2.0, float(vp["height"]) * y_ratio


def tv_shifted_context_click_xy_async(
    page_vp: dict[str, float],
    tv: dict[str, Any],
    base_x: float,
    base_y: float,
    attempt_index: int,
) -> tuple[float, float]:
    w = float(page_vp.get("width") or 1600)
    h = float(page_vp.get("height") or 900)
    step = float(tv.get("indicator_clear_retry_offset_ratio", 0.10) or 0.10)
    step = min(0.5, max(0.0, step))
    margin = 8.0
    x = base_x - (w * step * attempt_index)
    y = base_y + (h * step * attempt_index)
    return min(max(x, margin), w - margin), min(max(y, margin), h - margin)


async def tv_open_context_menu_and_clear_indicators_async(page: Page, tv: dict[str, Any]) -> None:
    _log_tv.info("tv: clear indicators | open context menu")
    texts = tv.get("context_menu_delete_indicators_texts")
    if isinstance(texts, list) and texts:
        candidates = [str(x).strip() for x in texts if str(x).strip()]
    else:
        candidates = ["Xóa 1 chỉ báo", "Xóa 2 chỉ báo"]

    attempts = int(tv.get("indicator_clear_retry_attempts", 8) or 8)
    click_timeout_ms = int(tv.get("indicator_clear_click_timeout_ms", 3000) or 3000)
    visible_timeout_ms = int(tv.get("indicator_clear_visible_timeout_ms", 1500) or 1500)
    menu_settle_ms = int(tv.get("indicator_clear_menu_settle_ms", 150) or 150)

    last_err: Optional[BaseException] = None
    last_legend: list[str] = []
    base_x, base_y = await tv_chart_center_xy_async(page, tv)
    vp = page.viewport_size or {"width": 1600.0, "height": 900.0}
    page_vp = {"width": float(vp.get("width") or 1600), "height": float(vp.get("height") or 900)}

    for i in range(max(1, attempts)):
        x, y = tv_shifted_context_click_xy_async(page_vp, tv, base_x, base_y, i)
        _log_tv.info(
            "tv: clear indicators | context click attempt %s/%s at x=%.1f y=%.1f",
            i + 1,
            attempts,
            x,
            y,
        )
        await page.mouse.click(x, y, button="right")
        if menu_settle_ms > 0:
            await page.wait_for_timeout(menu_settle_ms)
        clicked = False

        labels = page.locator('[data-role="menuitem"] [data-label="true"]')
        try:
            n = int(await labels.count() or 0)
        except Exception:
            n = 0
        for j in range(n):
            try:
                item = labels.nth(j)
                label = (await item.inner_text(timeout=500) or "").strip()
                if not _tradingview_is_delete_indicator_label(label):
                    continue
                await item.wait_for(state="visible", timeout=visible_timeout_ms)
                row = item.locator('xpath=ancestor::*[@data-role="menuitem"][1]')
                await row.click(timeout=click_timeout_ms, force=True)
                _log_tv.info(
                    "tv: clear indicators | clicked %r (attempt %s/%s)",
                    label,
                    i + 1,
                    attempts,
                )
                clicked = True
                break
            except Exception as e:
                last_err = e
                continue

        for t in candidates:
            if clicked:
                break
            try:
                item = page.locator(
                    '[data-role="menuitem"] [data-label="true"]',
                    has_text=t,
                ).first
                await item.wait_for(state="visible", timeout=visible_timeout_ms)
                row = item.locator('xpath=ancestor::*[@data-role="menuitem"][1]')
                await row.click(timeout=click_timeout_ms, force=True)
                _log_tv.info(
                    "tv: clear indicators | clicked %r (attempt %s/%s)",
                    t,
                    i + 1,
                    attempts,
                )
                clicked = True
                break
            except Exception as e:
                last_err = e
                continue

        if clicked:
            last_legend = await tv_wait_for_legend_empty_after_clear_async(page, tv)
            if not last_legend:
                _log_tv.info("tv: clear indicators | no legend indicators remain")
                return
            _log_tv.info(
                "tv: clear indicators | indicators remain after delete: %r",
                last_legend,
            )
            continue

        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await page.wait_for_timeout(300)
        try:
            last_legend = await tv_list_legend_item_texts_async(page, tv)
            if not last_legend:
                _log_tv.info("tv: clear indicators | no legend indicators remain")
                return
        except Exception as e:
            last_err = e

    try:
        last_legend = await tv_list_legend_item_texts_async(page, tv)
        if not last_legend:
            _log_tv.info("tv: clear indicators | no legend indicators remain")
            return
    except Exception as e:
        last_err = e
    msg = f"tv: clear indicators failed after {attempts} attempt(s)"
    if last_legend:
        msg += f"; indicators still present: {last_legend!r}"
    if last_err is not None:
        msg += f" ({last_err})"
    raise RuntimeError(msg)


async def tv_add_required_indicators_from_favorites_async(page: Page, tv: dict[str, Any]) -> None:
    btn_sel = (
        tv.get("favorite_indicators_button_selector") or 'button[data-name="show-favorite-indicators"]'
    ).strip()
    tpl = (
        tv.get("favorite_indicator_item_selector_template")
        or 'div[data-role="menuitem"][aria-label="{name}"]'
    ).strip()
    parent_sel = (tv.get("favorite_indicators_parent_selector") or "").strip()
    names = tv.get("favorite_indicator_names")
    if isinstance(names, list) and names:
        favs = [str(x).strip() for x in names if str(x).strip()]
    else:
        inferred: list[str] = []
        raw_req = tv.get("required_indicators")
        if isinstance(raw_req, list):
            for row in raw_req:
                if not isinstance(row, dict):
                    continue
                fn = str(row.get("favorite_name") or "").strip()
                if fn:
                    inferred.append(fn)
        favs = inferred or ["Smart Money Concepts (SMC) [LuxAlgo]", "VSA Volume"]

    after_add = int(tv.get("after_indicator_add_ms", 500))

    root: Locator | Page = page.locator(parent_sel).first if parent_sel else page
    if parent_sel:
        try:
            await root.wait_for(state="visible", timeout=20_000)
        except Exception:
            root = page

    btn = root.locator(btn_sel).first

    async def _open_favorites_menu() -> None:
        _log_tv.info("tv: favorites | open menu")
        try:
            await btn.wait_for(state="visible", timeout=4000)
            await btn.click(timeout=10_000)
            return
        except Exception:
            pass
        try:
            await btn.click(timeout=10_000, force=True)
            return
        except Exception:
            pass
        child = btn.locator(":scope div").first
        await child.wait_for(state="attached", timeout=10_000)
        await child.click(timeout=10_000, force=True)

    for nm in favs:
        await _open_favorites_menu()
        _log_tv.info("tv: favorites | add indicator=%r", nm)
        sel = tpl.format(name=nm)
        it = page.locator(sel).first
        await it.wait_for(state="visible", timeout=10_000)
        await it.click(timeout=10_000)
        if after_add > 0:
            await page.wait_for_timeout(after_add)


async def tv_ensure_required_indicators_async(page: Page, tv: dict[str, Any]) -> None:
    if not bool(tv.get("required_indicators_enabled", False)):
        return

    verify_timeout_ms = int(tv.get("indicator_verify_timeout_ms", 1000))
    groups = _tv_required_indicator_groups(tv)
    forbidden = _tv_forbidden_indicator_groups(tv)
    _log_tv.info(
        "tv: ensure indicators | required=%s",
        [[x for x in g if x] for g in (groups or [])],
    )
    if forbidden:
        _log_tv.info(
            "tv: ensure indicators | forbidden=%s",
            [[x for x in g if x] for g in (forbidden or [])],
        )
    deadline = time.monotonic() + max(0, verify_timeout_ms) / 1000.0
    while time.monotonic() < deadline:
        texts = await tv_list_legend_item_texts_async(page, tv)
        if tv_has_required_indicators_async_logic(texts, tv):
            _log_tv.info("tv: ensure indicators | ok")
            return
        await page.wait_for_timeout(200)

    legend_now = await tv_list_legend_item_texts_async(page, tv)
    if not legend_now:
        _log_tv.info("tv: ensure indicators | legend empty -> add-only recover")
        await tv_add_required_indicators_from_favorites_async(page, tv)
    else:
        _log_tv.info("tv: ensure indicators | missing -> recover")
        await tv_open_context_menu_and_clear_indicators_async(page, tv)
        await tv_add_required_indicators_from_favorites_async(page, tv)

    deadline2 = time.monotonic() + max(0, verify_timeout_ms) / 1000.0
    while time.monotonic() < deadline2:
        texts = await tv_list_legend_item_texts_async(page, tv)
        if tv_has_required_indicators_async_logic(texts, tv):
            _log_tv.info("tv: ensure indicators | ok after recover")
            return
        await page.wait_for_timeout(200)

    got = await tv_list_legend_item_texts_async(page, tv)
    if not got:
        _log_tv.info("tv: ensure indicators | legend still empty after recover; skip hard-fail")
        return
    _log_tv.info(
        "tv: ensure indicators | failed after recover | legend_items=%r",
        got,
    )
    expected = [[x for x in g if x] for g in (groups or [])]
    raise RuntimeError(
        "TradingView required indicators missing after recovery. "
        f"Expected groups={expected!r}, got legend items: {got!r}"
    )


async def tv_recover_stuck_indicators_async(page: Page, tv: dict[str, Any]) -> None:
    _log_tv.info("tv: recover stuck indicators | reset, clear, re-add from favorites")
    await tv_reset_chart_position_async(page, tv)
    await tv_open_context_menu_and_clear_indicators_async(page, tv)
    await tv_add_required_indicators_from_favorites_async(page, tv)
    extra_ms = int(tv.get("indicator_loading_recovery_after_add_ms", 2000))
    if extra_ms > 0:
        await page.wait_for_timeout(extra_ms)


async def tv_wait_indicators_loaded_once_async(
    page: Page,
    tv: dict[str, Any],
    *,
    attempt: int,
    max_attempts: int,
) -> bool:
    timeout_ms = int(tv.get("indicator_loading_timeout_ms", 45_000))
    poll_ms = int(tv.get("indicator_loading_poll_ms", 500))
    settle_ms = int(tv.get("indicator_loading_settle_ms", 300))
    deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
    cycle_start = time.monotonic()
    saw_loading = False
    logged_wait = False
    while time.monotonic() < deadline:
        texts = await tv_list_legend_item_texts_async(page, tv)
        statuses = await tv_list_legend_item_statuses_async(page, tv)
        elapsed_ms = (time.monotonic() - cycle_start) * 1000.0
        ready, saw_loading = _tradingview_poll_legend_loading_state(
            texts,
            statuses,
            tv,
            saw_loading=saw_loading,
            elapsed_ms=elapsed_ms,
        )
        if ready:
            if saw_loading:
                _log_tv.info("tv: indicators loaded | legend data-status ready for screenshot")
            if settle_ms > 0:
                await page.wait_for_timeout(settle_ms)
            return True
        if not logged_wait and (
            _tradingview_legend_is_still_loading(texts, statuses, tv)
            or bool(tv.get("indicator_loading_require_loading_cycle", True))
        ):
            _log_tv.info(
                "tv: indicators loading | attempt %s/%s | waiting up to %sms "
                "(data-status=%s, text_markers=%s)",
                attempt,
                max_attempts,
                timeout_ms,
                tv.get("indicator_loading_status_value", "loading"),
                _tradingview_indicator_loading_markers(tv),
            )
            logged_wait = True
        await page.wait_for_timeout(poll_ms)
    return False


async def tv_wait_for_indicators_loaded_async(page: Page, tv: dict[str, Any]) -> None:
    if tv.get("indicator_loading_wait_disabled", False):
        return
    max_attempts = max(1, int(tv.get("indicator_loading_retry_attempts", 2)))
    fail_on_timeout = bool(tv.get("indicator_loading_fail_on_timeout", True))
    timeout_ms = int(tv.get("indicator_loading_timeout_ms", 45_000))

    for attempt in range(1, max_attempts + 1):
        if await tv_wait_indicators_loaded_once_async(
            page, tv, attempt=attempt, max_attempts=max_attempts
        ):
            return
        if attempt < max_attempts:
            _log_tv.warning(
                "tv: indicators still loading after %sms; recovery %s/%s",
                timeout_ms,
                attempt,
                max_attempts - 1,
            )
            await tv_recover_stuck_indicators_async(page, tv)
            continue

    got = await tv_list_legend_item_texts_async(page, tv)
    if fail_on_timeout:
        raise RuntimeError(
            "TradingView indicators still loading after "
            f"{max_attempts} wait attempt(s) ({timeout_ms}ms each). "
            f"Legend: {got!r}"
        )
    _log_tv.warning(
        "tv: indicators still loading after %s attempt(s); proceeding with screenshot anyway",
        max_attempts,
    )


async def _tradingview_cdp_set_download_path_async(
    page: Page,
    download_dir: Path,
    tv: dict[str, Any],
) -> None:
    """Async Playwright: CDP must be awaited or Chrome keeps saving outside ``download_dir``."""
    if not bool(tv.get("tradingview_snapshot_download_cdp_dir_enabled", True)):
        return
    try:
        session = await page.context.new_cdp_session(page)
        await session.send(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allowAndName",
                "downloadPath": str(download_dir.resolve()),
                "eventsEnabled": True,
            },
        )
    except Exception:
        pass


async def _tradingview_materialize_browser_download_async(
    page: Page,
    download,
    dest: Path,
    tv: dict[str, Any],
    *,
    download_dir: Path,
    since_epoch: float,
    wait_ms: int,
) -> bool:
    """Async Playwright download → ``dest`` (see coinmap._tradingview_materialize_browser_download)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        await download.save_as(dest)
    except Exception:
        pass
    if not _tradingview_download_png_is_empty(dest)[0]:
        return True

    suggested = (download.suggested_filename or "").strip()
    scan_dirs = _tradingview_download_scan_dirs(download_dir)
    deadline = time.monotonic() + max(0, wait_ms) / 1000.0
    poll_ms = 200
    while time.monotonic() < deadline:
        try:
            tmp = await download.path()
            if tmp:
                src = Path(tmp)
                if src.is_file() and src.stat().st_size > 0:
                    shutil.copy2(src, dest)
                    if not _tradingview_download_png_is_empty(dest)[0]:
                        return True
        except Exception:
            pass

        for directory in scan_dirs:
            if suggested:
                cand = directory / suggested
                if cand.is_file() and cand.stat().st_size > 0:
                    shutil.copy2(cand, dest)
                    return True
            newest = _tradingview_pickup_newest_png(
                directory,
                since_epoch=since_epoch,
                name_hint=suggested,
            )
            if newest is not None:
                shutil.copy2(newest, dest)
                return True
        await page.wait_for_timeout(poll_ms)
    return not _tradingview_download_png_is_empty(dest)[0]


async def tv_snapshot_download_capture_async(
    page: Page,
    tv: dict[str, Any],
    charts_dir: Path,
    stamp: str,
    symbol_key: str,
    interval_slug: str,
    *,
    dest_path: Optional[Path] = None,
) -> Path:
    shortcut = (tv.get("tradingview_snapshot_download_shortcut") or "Control+Alt+S").strip()
    timeout_ms = int(tv.get("tradingview_snapshot_download_timeout_ms", 15_000))
    after_ms = int(tv.get("after_tradingview_snapshot_download_ms", 300))
    dest = dest_path or (charts_dir / f"{stamp}_tradingview_{symbol_key}_{interval_slug}.png")
    dest.parent.mkdir(parents=True, exist_ok=True)
    download_dir = dest.parent / ".tv_downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    max_retries = _tradingview_download_empty_max_retries(tv)
    retry_delay_ms = _tradingview_download_empty_retry_delay_ms(tv)
    pickup_ms = _tradingview_download_pickup_timeout_ms(tv)
    try:
        await page.bring_to_front()
    except Exception:
        pass
    try:
        cx, cy = await tv_chart_center_xy_async(page, tv)
        await page.mouse.click(cx, cy)
        await page.wait_for_timeout(80)
    except Exception:
        pass

    for attempt in range(max_retries + 1):
        since = time.time()
        await _tradingview_cdp_set_download_path_async(page, download_dir, tv)
        async with page.expect_download(timeout=timeout_ms) as dl_info:
            await page.keyboard.press(shortcut)
        download = await dl_info.value
        ok = await _tradingview_materialize_browser_download_async(
            page,
            download,
            dest,
            tv,
            download_dir=download_dir,
            since_epoch=since,
            wait_ms=pickup_ms,
        )
        empty, reason = _tradingview_download_png_is_empty(dest)
        if ok and not empty:
            if after_ms > 0:
                await page.wait_for_timeout(after_ms)
            return dest
        if attempt < max_retries:
            _log_tv.warning(
                "tv: PNG empty after download (%s) — retry %d/%d (%s)",
                reason,
                attempt + 1,
                max_retries,
                dest.name,
            )
            if retry_delay_ms > 0:
                await page.wait_for_timeout(retry_delay_ms)
        else:
            _log_tv.warning(
                "tv: PNG still empty after %d retries (%s); Chrome may have saved to Downloads only",
                max_retries,
                dest.name,
            )

    if after_ms > 0:
        await page.wait_for_timeout(after_ms)
    return dest


async def tv_snapshot_url_capture_async(
    page: Page,
    tv: dict[str, Any],
    charts_dir: Path,
    stamp: str,
    symbol_key: str,
    interval_slug: str,
    *,
    dest_url_path: Optional[Path] = None,
) -> Path:
    shot_sel = (tv.get("screenshot_button_selector") or "#header-toolbar-screenshot").strip()
    open_sel = (tv.get("snapshot_open_in_new_tab_selector") or '[data-qa-id="open-image-in-new-tab"]').strip()
    img_sel = (tv.get("snapshot_image_selector") or "img.tv-snapshot-image").strip()
    after_shot_ms = int(tv.get("after_screenshot_button_ms", 600))
    tab_timeout = int(tv.get("snapshot_new_tab_timeout_ms", 5_000))
    tab_settle_ms = int(tv.get("snapshot_tab_settle_ms", 1000))
    after_esc_ms = int(tv.get("after_snapshot_escape_ms", 500))

    await page.locator(shot_sel).first.wait_for(state="visible", timeout=5_000)
    await page.locator(shot_sel).first.click(timeout=5_000)
    await page.wait_for_timeout(after_shot_ms)

    open_btn = page.locator(open_sel).first
    await open_btn.wait_for(state="visible", timeout=5_000)

    context = page.context
    async with context.expect_page(timeout=tab_timeout) as new_page_info:
        await open_btn.click(timeout=15_000)
    snap_page = await new_page_info.value
    dest_base = charts_dir / f"{stamp}_tradingview_{symbol_key}_{interval_slug}"
    out_url_path = dest_url_path or dest_base.with_suffix(".url")
    out_png_path = dest_base.with_suffix(".png")
    try:
        await snap_page.wait_for_load_state("domcontentloaded", timeout=5_000)
        await snap_page.wait_for_timeout(tab_settle_ms)
        loc = snap_page.locator(img_sel).first
        await loc.wait_for(state="visible", timeout=5_000)
        src = (await loc.get_attribute("src") or "").strip()
        if src.startswith("https://") or src.startswith("http://"):
            out_url_path.parent.mkdir(parents=True, exist_ok=True)
            out_url_path.write_text(src + "\n", encoding="utf-8")
            return out_url_path
        await loc.screenshot(path=str(out_png_path), timeout=5_000)
        return out_png_path
    finally:
        try:
            await snap_page.close()
        except Exception:
            pass
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(after_esc_ms)


async def tv_capture_one_chart_frame_async(
    page: Page,
    tv: dict[str, Any],
    charts_dir: Path,
    stamp: str,
    symbol_key: str,
    interval_slug: str,
    *,
    dest_url_path: Optional[Path] = None,
) -> Path:
    await tv_wait_for_indicators_loaded_async(page, tv)
    if bool(tv.get("tradingview_snapshot_url_flow", True)):
        return await tv_snapshot_url_capture_async(
            page,
            tv,
            charts_dir,
            stamp,
            symbol_key,
            interval_slug,
            dest_url_path=dest_url_path,
        )
    if bool(tv.get("tradingview_snapshot_download_flow", False)):
        return await tv_snapshot_download_capture_async(
            page,
            tv,
            charts_dir,
            stamp,
            symbol_key,
            interval_slug,
        )
    fs_sel = (tv.get("fullscreen_button_selector") or "#header-toolbar-fullscreen").strip()
    await page.locator(fs_sel).first.wait_for(state="visible", timeout=45_000)
    await page.locator(fs_sel).first.click(timeout=15_000)
    fs_wait = int(tv.get("fullscreen_settle_ms", 2000))
    await page.wait_for_timeout(fs_wait)
    full_page = bool(tv.get("fullscreen_screenshot_full_page", True))
    dest = charts_dir / f"{stamp}_tradingview_{symbol_key}_{interval_slug}.png"
    await page.screenshot(path=str(dest), full_page=full_page)
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(int(tv.get("after_fullscreen_escape_ms", 800)))
    return dest


async def tv_warmup_tab_async(
    page: Page,
    tv: dict[str, Any],
    *,
    symbol: str,
    interval_label: str,
    settle_ms: int,
    login_email: Optional[str],
    login_password: Optional[str],
    skip_login: bool = False,
    skip_dark_mode: bool = False,
) -> None:
    tw = int(tv.get("viewport_width", 0) or 0)
    th = int(tv.get("viewport_height", 0) or 0)
    if tw > 0 and th > 0 and page.viewport_size is not None:
        await page.set_viewport_size({"width": tw, "height": th})

    url = tv.get("chart_url") or "https://vn.tradingview.com/chart/?symbol=OANDA%3AXAUUSD"
    await page.goto(str(url), wait_until="domcontentloaded", timeout=120_000)
    init_wait = int(tv.get("initial_settle_ms", settle_ms))
    await page.wait_for_timeout(init_wait)

    if not skip_login:
        await maybe_tradingview_login_async(page, tv, login_email, login_password)

    intervals_id = (tv.get("intervals_toolbar_id") or "header-toolbar-intervals").strip()
    toolbar = page.locator(f"#{intervals_id}")
    await toolbar.wait_for(state="visible", timeout=90_000)

    if not skip_dark_mode:
        await maybe_tradingview_dark_mode_async(page, tv)

    await tradingview_ensure_watchlist_open_async(page, tv)
    await tv_select_symbol_async(page, tv, symbol)
    await tv_select_interval_async(page, tv, interval_label, settle_ms)
    await tv_reset_chart_position_async(page, tv)
    await tv_ensure_required_indicators_async(page, tv)


def tv_apply_profile(tv: dict[str, Any], profile: str) -> dict[str, Any]:
    """Shallow merge indicator profile (same as coinmap._tv_apply_indicator_profile)."""
    return _tv_apply_indicator_profile(tv, profile)


def tv_interval_slug_from_label(label: str, tv: dict[str, Any]) -> str:
    return _tradingview_interval_slug(label, tv)
