"""
Async Coinmap chart helpers for browser_service (Playwright async API).

Warm tab prewarm + network_capture multi-shot on a long-lived chart page.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from playwright.async_api import Page

from automation_tool.coinmap import (
    _COINMAP_NETWORK_CAPTURE_WAIT_KEYS,
    _api_export_mode,
    _coinmap_endpoint_key_from_response_url,
    _coinmap_filter_capture_plan_by_intervals,
    _coinmap_maybe_fail_api_shot,
    _coinmap_resolve_api_bump_interval,
    _coinmap_should_pan_chart,
    _network_capture_require_nonempty,
    _write_coinmap_api_shot_json,
    coinmap_network_last_body_per_key,
)


class CoinmapNetworkCaptureAsync:
    """Records JSON bodies from chart-originated gateway responses."""

    def __init__(self, page: Page, api_cd: dict[str, Any]) -> None:
        self.page = page
        self.api_cd = api_cd
        self._records: list[dict[str, Any]] = []
        self._handler: Optional[Callable[..., None]] = None

    def install(self) -> None:
        def handler(response) -> None:
            asyncio.create_task(self._on_response_async(response))

        self._handler = handler
        self.page.on("response", self._handler)

    async def uninstall(self) -> None:
        if self._handler is not None:
            try:
                self.page.remove_listener("response", self._handler)
            except Exception:
                pass
            self._handler = None

    async def _on_response_async(self, response) -> None:
        url = response.url
        if "gw.coinmap.tech" not in url and not self.api_cd.get("capture_any_host", False):
            return
        key = _coinmap_endpoint_key_from_response_url(url)
        if not key:
            return
        max_ch = max(256, int(self.api_cd.get("max_nonjson_body_chars") or 8000))
        try:
            status = response.status
            ok = 200 <= status < 300
            try:
                body: Any = await response.json()
            except Exception:
                text = await response.text()
                body = text if len(text) <= max_ch else text[:max_ch] + "...(truncated)"
            self._records.append(
                {"key": key, "url": url, "status": status, "ok": ok, "body": body}
            )
        except Exception as e:
            self._records.append(
                {"key": key, "url": url, "status": 0, "ok": False, "body": str(e)}
            )

    async def consume_shot(
        self, start_index: int, step_ctx: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        wait_ms = max(0, int(self.api_cd.get("network_capture_wait_ms") or 12_000))
        poll_ms = max(50, int(self.api_cd.get("network_capture_poll_ms") or 300))
        deadline = time.monotonic() + wait_ms / 1000.0
        while time.monotonic() < deadline:
            if self._shot_has_all_keys(start_index, step_ctx):
                break
            await self.page.wait_for_timeout(poll_ms)
        slice_ = self._records[start_index:]
        return self._last_body_per_key(slice_, step_ctx=step_ctx)

    def _shot_has_all_keys(
        self, start_index: int, step_ctx: Optional[dict[str, Any]] = None
    ) -> bool:
        slice_ = self._records[start_index:]
        seen = {r["key"] for r in slice_}
        if not all(k in seen for k in _COINMAP_NETWORK_CAPTURE_WAIT_KEYS):
            return False
        if step_ctx is None or not _network_capture_require_nonempty(self.api_cd):
            return True
        grouped = coinmap_network_last_body_per_key(
            slice_, step_ctx=step_ctx, api_cd=self.api_cd
        )
        for key in _COINMAP_NETWORK_CAPTURE_WAIT_KEYS:
            block = grouped.get(key)
            if not isinstance(block, dict) or not block.get("ok"):
                return False
            body = block.get("body")
            if not isinstance(body, list) or len(body) == 0:
                return False
        return True

    def _last_body_per_key(
        self,
        slice_: list[dict[str, Any]],
        step_ctx: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return coinmap_network_last_body_per_key(
            slice_, step_ctx=step_ctx, api_cd=self.api_cd
        )


async def _coinmap_shot_from_network_async(
    net_capture: CoinmapNetworkCaptureAsync, start_idx: int, step_ctx: dict[str, Any]
) -> dict[str, Any]:
    grouped = await net_capture.consume_shot(start_idx, step_ctx)
    out: dict[str, Any] = {
        "symbol": step_ctx.get("symbol"),
        "interval": step_ctx.get("interval"),
        "watchlist_category": step_ctx.get("watchlist_category"),
    }
    ex = step_ctx.get("export_symbol")
    if isinstance(ex, str) and ex.strip():
        out["export_symbol"] = ex.strip()
    out.update(grouped)
    return out


async def _login_form_is_visible_async(
    page: Page, email_sel: str, password_sel: str, timeout_ms: int = 8000
) -> bool:
    try:
        await page.locator(email_sel).first.wait_for(state="visible", timeout=timeout_ms)
        await page.locator(password_sel).first.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


async def _coinmap_page_on_login_url(page: Page, login_cfg: dict[str, Any]) -> bool:
    login_url = str(login_cfg.get("login_url") or "https://coinmap.tech/login").strip().lower()
    u = (page.url or "").strip().lower()
    if not u:
        return False
    if "login" in u:
        return True
    if login_url and u.rstrip("/") == login_url.rstrip("/"):
        return True
    return False


async def _coinmap_needs_login_async(
    page: Page,
    login_cfg: dict[str, Any],
    *,
    email_sel: str,
    password_sel: str,
) -> bool:
    if await _coinmap_page_on_login_url(page, login_cfg):
        return True
    return await _login_form_is_visible_async(page, email_sel, password_sel, timeout_ms=3000)


async def _coinmap_maybe_login_if_needed_async(
    page: Page,
    cd: dict[str, Any],
    login_cfg: dict[str, Any],
    *,
    email: Optional[str],
    password: Optional[str],
    settle_ms: int,
    chart_url: str,
) -> None:
    """Submit login only when chart navigation landed on /login or the login form is visible."""
    if not email or not password:
        return
    email_sel = login_cfg.get("email_selector") or 'input[type="email"]'
    password_sel = login_cfg.get("password_selector") or 'input[type="password"]'
    submit_sel = login_cfg.get("submit_selector") or 'button[type="submit"]'
    post_wait = (login_cfg.get("post_login_wait_selector") or "").strip()

    if not await _coinmap_needs_login_async(
        page, login_cfg, email_sel=email_sel, password_sel=password_sel
    ):
        return

    await _coinmap_fill_and_submit_login_async(
        page,
        email=email,
        password=password,
        email_sel=email_sel,
        password_sel=password_sel,
        submit_sel=submit_sel,
        settle_ms=settle_ms,
    )
    if post_wait:
        try:
            await page.locator(post_wait).first.wait_for(state="visible", timeout=30_000)
        except Exception:
            pass
    if await _coinmap_page_on_login_url(page, login_cfg) or await _login_form_is_visible_async(
        page, email_sel, password_sel, timeout_ms=2_000
    ):
        await page.goto(chart_url, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(settle_ms)
        await _maybe_dismiss_coinmap_symbol_search_modal_async(page, cd)
        await _maybe_switch_to_dark_mode_async(page, cd)
        await _maybe_dismiss_light_theme_modal_async(page, cd)
        await _maybe_dismiss_coinmap_symbol_search_modal_async(page, cd)


async def _coinmap_fill_and_submit_login_async(
    page: Page,
    *,
    email: str,
    password: str,
    email_sel: str,
    password_sel: str,
    submit_sel: str,
    settle_ms: int,
) -> None:
    email_loc = page.locator(email_sel).first
    password_loc = page.locator(password_sel).first
    await email_loc.wait_for(state="visible", timeout=15_000)
    await email_loc.fill(email, timeout=15_000)
    await password_loc.wait_for(state="visible", timeout=15_000)
    await password_loc.fill(password, timeout=15_000)
    await page.wait_for_timeout(300)

    submit_ok = False
    try:
        await page.locator(submit_sel).first.click(timeout=8_000)
        submit_ok = True
    except Exception:
        submit_ok = False

    if not submit_ok:
        try:
            await page.locator(password_sel).first.press("Enter", timeout=5_000)
        except Exception:
            pass

    try:
        await page.wait_for_load_state("networkidle", timeout=60_000)
    except Exception:
        pass
    await page.wait_for_timeout(settle_ms)

    if await _login_form_is_visible_async(page, email_sel, password_sel, timeout_ms=2_000):
        try:
            await page.locator(submit_sel).first.click(timeout=8_000, force=True)
        except Exception:
            try:
                await page.locator(password_sel).first.press("Enter", timeout=5_000)
            except Exception:
                pass
        try:
            await page.wait_for_load_state("networkidle", timeout=60_000)
        except Exception:
            pass
        await page.wait_for_timeout(settle_ms)


async def _maybe_dismiss_coinmap_symbol_search_modal_async(page: Page, cd: dict[str, Any]) -> None:
    if not cd.get("dismiss_symbol_search_modal", True):
        return
    close_sel = (cd.get("symbol_search_modal_close_selector") or "").strip()
    after = int(cd.get("after_symbol_modal_dismiss_ms", 350))
    gap = int(cd.get("symbol_modal_escape_gap_ms", 180))
    if close_sel:
        try:
            await page.locator(close_sel).first.click(timeout=5_000)
            await page.wait_for_timeout(after)
        except Exception:
            pass
    presses = max(0, int(cd.get("symbol_search_modal_escape_presses", 2)))
    for _ in range(presses):
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(gap)
    await page.wait_for_timeout(after)


async def _maybe_switch_to_dark_mode_async(page: Page, cd: dict[str, Any]) -> None:
    if not cd.get("dark_mode_enabled", True):
        return
    btn_sel = (cd.get("dark_mode_theme_button_selector") or "").strip()
    if not btn_sel:
        btn_sel = '[class*="Header_menuIconTheme"]'
    sun_sel = (cd.get("dark_mode_sun_icon_selector") or "span.anticon-sun").strip()
    try:
        btn = page.locator(btn_sel).first
        await btn.wait_for(state="visible", timeout=15_000)
        if await btn.locator(sun_sel).count() == 0:
            await btn.click(timeout=10_000)
            await page.wait_for_timeout(int(cd.get("dark_mode_after_click_ms", 400)))
    except Exception:
        pass


async def _maybe_dismiss_light_theme_modal_async(page: Page, cd: dict[str, Any]) -> None:
    if not cd.get("light_theme_confirm_enabled", True):
        return
    sel = (cd.get("light_theme_confirm_selector") or "").strip()
    if not sel:
        sel = 'button:has-text("Confirm"), button:has-text("OK"), button:has-text("Continue")'
    wait_ms = int(cd.get("light_theme_modal_wait_ms", 2500))
    try:
        loc = page.locator(sel).first
        await loc.wait_for(state="visible", timeout=wait_ms)
        await loc.click(timeout=10_000)
        await page.wait_for_timeout(400)
    except Exception:
        pass


async def _coinmap_press_escape_n_async(page: Page, cd: dict[str, Any], *, presses: int) -> None:
    n = max(0, int(presses))
    if n <= 0:
        return
    gap = max(0, int(cd.get("coinmap_fullscreen_exit_escape_gap_ms", 200)))
    for _ in range(n):
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(gap)


async def _coinmap_unstick_fullscreen_loop_start_async(page: Page, cd: dict[str, Any]) -> None:
    presses = max(0, int(cd.get("coinmap_fullscreen_loop_start_escape_presses", 1)))
    await _coinmap_press_escape_n_async(page, cd, presses=presses)


async def _coinmap_maybe_relogin_if_login_form_visible_async(
    page: Page,
    cd: dict[str, Any],
    *,
    email: Optional[str],
    password: Optional[str],
    login_cfg: Optional[dict[str, Any]],
    settle_ms: int,
) -> None:
    if not email or not password:
        return
    base = login_cfg if isinstance(login_cfg, dict) else {}
    email_sel = base.get("email_selector") or 'input[type="email"]'
    password_sel = base.get("password_selector") or 'input[type="password"]'
    submit_sel = base.get("submit_selector") or 'button[type="submit"]'
    form_timeout = int(base.get("mid_flow_login_form_detect_timeout_ms", 4_000))
    if not await _login_form_is_visible_async(
        page, email_sel, password_sel, timeout_ms=form_timeout
    ):
        return
    await _coinmap_fill_and_submit_login_async(
        page,
        email=email,
        password=password,
        email_sel=email_sel,
        password_sel=password_sel,
        submit_sel=submit_sel,
        settle_ms=settle_ms,
    )
    post_wait = (base.get("post_login_wait_selector") or "").strip()
    if post_wait:
        try:
            await page.locator(post_wait).first.wait_for(state="visible", timeout=30_000)
        except Exception:
            pass
    chart_url = cd.get("chart_page_url") or "https://coinmap.tech/chart"
    u = (page.url or "").lower()
    if "login" in u:
        await page.goto(chart_url, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(settle_ms)
        await _maybe_dismiss_coinmap_symbol_search_modal_async(page, cd)
        await _maybe_switch_to_dark_mode_async(page, cd)
        await _maybe_dismiss_light_theme_modal_async(page, cd)
        await _maybe_dismiss_coinmap_symbol_search_modal_async(page, cd)


async def _coinmap_toggle_right_sidebar_async(
    page: Page,
    cd: dict[str, Any],
    *,
    login_email: Optional[str] = None,
    login_password: Optional[str] = None,
    login_cfg: Optional[dict[str, Any]] = None,
    settle_ms: int = 2000,
) -> None:
    await _coinmap_maybe_relogin_if_login_form_visible_async(
        page,
        cd,
        email=login_email,
        password=login_password,
        login_cfg=login_cfg,
        settle_ms=settle_ms,
    )
    prefix = (cd.get("right_sidebar_container_class_prefix") or "ChartDesktopPage_rightSidebar__").strip()
    ms = int(cd.get("sidebar_toggle_after_click_ms", 450))
    force = bool(cd.get("right_sidebar_toggle_click_force", True))
    custom = (cd.get("right_sidebar_toggle_button_selector") or "").strip()
    if custom:
        btn = page.locator(custom).first
    else:
        action_pf = (cd.get("right_sidebar_action_button_class_prefix") or "ChartDesktopPage_actionButton").strip()
        btn = page.locator(f'[class*="{prefix}"] [class*="{action_pf}"]').first
    await btn.wait_for(state="visible", timeout=30_000)
    await btn.click(timeout=15_000, force=force)
    await page.wait_for_timeout(ms)


async def _coinmap_ensure_right_sidebar_open_async(
    page: Page,
    cd: dict[str, Any],
    *,
    login_email: Optional[str] = None,
    login_password: Optional[str] = None,
    login_cfg: Optional[dict[str, Any]] = None,
    settle_ms: int = 2000,
) -> None:
    wt = (cd.get("watchlist_title_class_prefix") or "ChartWatchList_title__").strip()
    title = page.locator(f'[class*="{wt}"]').first
    quick_ms = int(cd.get("right_sidebar_already_open_check_ms", 2_000))
    max_toggles = max(1, int(cd.get("right_sidebar_open_max_toggles", 4)))
    final_timeout = int(cd.get("watchlist_title_visible_timeout_ms", 20_000))

    for _ in range(max_toggles):
        await _coinmap_maybe_relogin_if_login_form_visible_async(
            page,
            cd,
            email=login_email,
            password=login_password,
            login_cfg=login_cfg,
            settle_ms=settle_ms,
        )
        try:
            await title.wait_for(state="visible", timeout=quick_ms)
            return
        except Exception:
            pass
        await _coinmap_toggle_right_sidebar_async(
            page,
            cd,
            login_email=login_email,
            login_password=login_password,
            login_cfg=login_cfg,
            settle_ms=settle_ms,
        )

    await title.wait_for(state="visible", timeout=final_timeout)


async def _coinmap_click_ant_select_item_option_async(
    page: Page, label: str, *, visible_timeout_ms: int = 15_000
) -> None:
    by_title = page.locator(f'.ant-select-item-option[title="{label}"]').first
    try:
        await by_title.wait_for(state="visible", timeout=visible_timeout_ms)
        await by_title.click(timeout=15_000)
        return
    except Exception:
        pass
    opt = page.locator(".ant-select-item-option").filter(
        has=page.locator(".ant-select-item-option-content").get_by_text(label, exact=True)
    ).first
    await opt.wait_for(state="visible", timeout=visible_timeout_ms)
    await opt.click(timeout=15_000)


async def _coinmap_select_watchlist_category_async(
    page: Page, cd: dict[str, Any], category_text: str
) -> None:
    title_prefix = (cd.get("watchlist_title_class_prefix") or "ChartWatchList_title__").strip()
    after_open = int(cd.get("watchlist_dropdown_open_ms", 350))
    title = page.locator(f'[class*="{title_prefix}"]').first
    await title.wait_for(state="visible", timeout=int(cd.get("watchlist_title_visible_timeout_ms", 20_000)))
    sel = title.locator(".ant-select").first
    await sel.wait_for(state="visible", timeout=int(cd.get("watchlist_ant_select_visible_timeout_ms", 15_000)))
    await sel.click(timeout=15_000)
    await page.wait_for_timeout(after_open)
    opt_sel = (cd.get("watchlist_category_option_selector") or "").strip()
    if opt_sel:
        await page.locator(opt_sel.format(text=category_text)).first.click(timeout=15_000)
    else:
        await _coinmap_click_ant_select_item_option_async(
            page, category_text, visible_timeout_ms=int(cd.get("watchlist_option_visible_timeout_ms", 15_000))
        )
    await page.wait_for_timeout(int(cd.get("after_watchlist_category_ms", 400)))


async def _coinmap_select_watchlist_symbol_async(page: Page, cd: dict[str, Any], symbol: str) -> None:
    name_prefix = (cd.get("watchlist_symbol_name_class_prefix") or "TableData_symbolNameContent__").strip()
    custom = (cd.get("watchlist_symbol_row_selector") or "").strip()
    if custom:
        await page.locator(custom.format(symbol=symbol)).first.click(timeout=15_000)
    else:
        await page.locator(f'[class*="{name_prefix}"]').get_by_text(symbol, exact=True).first.click(
            timeout=15_000
        )
    await page.wait_for_timeout(int(cd.get("after_watchlist_symbol_click_ms", 700)))


async def _coinmap_select_interval_async(page: Page, cd: dict[str, Any], interval_text: str) -> None:
    iv_prefix = (cd.get("interval_select_class_prefix") or "IntervalSelect_intervalSelect__").strip()
    after_open = int(cd.get("interval_dropdown_open_ms", 350))
    root = page.locator(f'[class*="{iv_prefix}"]').first
    await root.click(timeout=15_000)
    await page.wait_for_timeout(after_open)
    opt_sel = (cd.get("interval_option_selector") or "").strip()
    if opt_sel:
        await page.locator(opt_sel.format(text=interval_text)).first.click(timeout=15_000)
    else:
        await _coinmap_click_ant_select_item_option_async(
            page, interval_text, visible_timeout_ms=int(cd.get("interval_option_visible_timeout_ms", 15_000))
        )
    await page.wait_for_timeout(int(cd.get("after_interval_select_ms", 800)))


async def _coinmap_maybe_bump_interval_before_target_async(
    page: Page,
    cd: dict[str, Any],
    *,
    target_interval: str,
    settle_ms: int,
    for_api_capture: bool,
) -> None:
    if not for_api_capture:
        return
    raw = (cd.get("api_network_capture_bump_interval") or "").strip()
    bump = _coinmap_resolve_api_bump_interval(raw, target_interval)
    if not bump:
        return
    await _coinmap_select_interval_async(page, cd, bump)
    await page.wait_for_timeout(int(cd.get("after_interval_change_settle_ms", settle_ms)))


async def _chart_drag_in_box_async(
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
    await page.mouse.move(x0, y0)
    await page.mouse.down()
    await page.mouse.move(x1, y1, steps=max(1, drag_steps))
    await page.mouse.up()


async def _apply_coinmap_chart_view_adjustments_async(
    page: Page, cd: dict[str, Any], api_cd: Optional[dict[str, Any]] = None
) -> None:
    if not _coinmap_should_pan_chart(cd, api_cd):
        return
    sel = (cd.get("chart_interaction_selector") or "").strip()
    if not sel:
        sel = (
            "svg.react-stockchart-canvas, svg[class*='react-stockchart-canvas'], "
            "svg[class*='react-stockchart']"
        )
    try:
        chart = page.locator(sel).first
        await chart.wait_for(state="visible", timeout=25_000)
    except Exception:
        return

    box = await chart.bounding_box()
    if not box:
        await page.wait_for_timeout(400)
        box = await chart.bounding_box()
    if not box:
        return

    between_ms = int(cd.get("chart_between_pan_ms", 200))
    pan_y_default = float(cd.get("chart_pan_y_ratio", 0.52))
    edge_drag_steps = int(cd.get("chart_edge_pan_drag_steps", 12))

    if cd.get("chart_time_edge_pan_enabled", True):
        ty = float(cd.get("chart_time_edge_pan_y_ratio", 0.96))
        tsx = float(cd.get("chart_time_edge_pan_start_x_ratio", 0.38))
        tex = float(cd.get("chart_time_edge_pan_end_x_ratio", 0.58))
        time_repeats = max(1, int(cd.get("chart_time_edge_pan_repeats", 2)))
        for i in range(time_repeats):
            await _chart_drag_in_box_async(
                page,
                box,
                x0r=tsx,
                y0r=ty,
                x1r=tex,
                y1r=ty,
                drag_steps=edge_drag_steps,
            )
            if between_ms and i < time_repeats - 1:
                await page.wait_for_timeout(between_ms)
        if between_ms:
            await page.wait_for_timeout(between_ms)

    if cd.get("chart_price_edge_pan_enabled", True):
        px = float(cd.get("chart_price_edge_pan_x_ratio", 0.93))
        psy = float(cd.get("chart_price_edge_pan_start_y_ratio", 0.22))
        pey = float(cd.get("chart_price_edge_pan_end_y_ratio", 0.32))
        await _chart_drag_in_box_async(
            page,
            box,
            x0r=px,
            y0r=psy,
            x1r=px,
            y1r=pey,
            drag_steps=edge_drag_steps,
        )
        if between_ms:
            await page.wait_for_timeout(between_ms)

    sx = float(cd.get("chart_pan_start_x_ratio", 0.28))
    ex = float(cd.get("chart_pan_end_x_ratio", 0.62))
    y_ratio = float(cd.get("chart_pan_y_ratio", pan_y_default))
    main_steps = int(cd.get("chart_pan_drag_steps", 14))
    await _chart_drag_in_box_async(
        page,
        box,
        x0r=sx,
        y0r=y_ratio,
        x1r=ex,
        y1r=y_ratio,
        drag_steps=main_steps,
    )


async def coinmap_warmup_tab_async(
    page: Page,
    cd: dict[str, Any],
    login_cfg: dict[str, Any],
    *,
    email: Optional[str],
    password: Optional[str],
    settle_ms: int,
) -> None:
    """Open chart directly; login only when redirected to /login or the login form appears."""
    chart_url = cd.get("chart_page_url") or "https://coinmap.tech/chart"

    await page.goto(chart_url, wait_until="domcontentloaded", timeout=90_000)
    await page.wait_for_timeout(settle_ms)

    await _coinmap_maybe_login_if_needed_async(
        page,
        cd,
        login_cfg,
        email=email,
        password=password,
        settle_ms=settle_ms,
        chart_url=str(chart_url),
    )

    await _maybe_dismiss_coinmap_symbol_search_modal_async(page, cd)
    await _maybe_switch_to_dark_mode_async(page, cd)
    await _maybe_dismiss_light_theme_modal_async(page, cd)
    await _maybe_dismiss_coinmap_symbol_search_modal_async(page, cd)


async def coinmap_capture_plan_async(
    page: Page,
    *,
    cd: dict[str, Any],
    api_cd: dict[str, Any],
    plan: list[dict[str, Any]],
    charts_dir: Path,
    stamp: str,
    settle_ms: int,
    login_cfg: Optional[dict[str, Any]] = None,
    coinmap_email: Optional[str] = None,
    coinmap_password: Optional[str] = None,
) -> list[Path]:
    """Multi-shot capture with network_capture on a warm chart tab."""
    if _api_export_mode(api_cd) != "network_capture":
        raise ValueError("coinmap_capture_plan_async requires api_data_export.mode network_capture")

    net_capture = CoinmapNetworkCaptureAsync(page, api_cd)
    net_capture.install()
    written: list[Path] = []
    prev_symbol: Optional[str] = None
    try:
        for step in plan:
            await _coinmap_unstick_fullscreen_loop_start_async(page, cd)
            sym = step["symbol"]
            interval = step["interval"]
            cat = step.get("watchlist_category")
            need_pick = cat is not None or prev_symbol != sym
            if need_pick:
                await _coinmap_ensure_right_sidebar_open_async(
                    page,
                    cd,
                    login_email=coinmap_email,
                    login_password=coinmap_password,
                    login_cfg=login_cfg,
                    settle_ms=settle_ms,
                )
                if cat:
                    await _coinmap_select_watchlist_category_async(page, cd, cat)
                await _coinmap_select_watchlist_symbol_async(page, cd, sym)
                await _coinmap_toggle_right_sidebar_async(
                    page,
                    cd,
                    login_email=coinmap_email,
                    login_password=coinmap_password,
                    login_cfg=login_cfg,
                    settle_ms=settle_ms,
                )

            await _coinmap_maybe_bump_interval_before_target_async(
                page,
                cd,
                target_interval=interval,
                settle_ms=settle_ms,
                for_api_capture=True,
            )
            net_start = len(net_capture._records)
            await _coinmap_select_interval_async(page, cd, interval)
            await page.wait_for_timeout(int(cd.get("after_interval_change_settle_ms", settle_ms)))

            step_ctx: dict[str, Any] = {
                "symbol": sym,
                "interval": interval,
                "watchlist_category": cat,
                "api_query": step.get("api_query"),
            }
            ex = step.get("export_symbol")
            if isinstance(ex, str) and ex.strip():
                step_ctx["export_symbol"] = ex.strip()
            label = (ex.strip() if isinstance(ex, str) and ex.strip() else sym)
            sym_slug = re.sub(r"[^\w.-]+", "_", label).strip("_")[:40] or "sym"
            iv_slug = re.sub(r"[^\w]+", "_", interval).strip("_")[:20] or "iv"

            shot = await _coinmap_shot_from_network_async(net_capture, net_start, step_ctx)
            _coinmap_maybe_fail_api_shot(api_cd, shot)
            stem = f"{stamp}_coinmap_{sym_slug}_{iv_slug}"
            json_path = _write_coinmap_api_shot_json(
                charts_dir, file_stem=stem, stamp=stamp, shot=shot, api_cd=api_cd
            )

            shot_enabled = bool(cd.get("coinmap_screenshot_enabled", True))
            if shot_enabled:
                raise RuntimeError(
                    "coinmap_screenshot_enabled is not supported on warm-tab RPC capture; set false in YAML"
                )
            if _coinmap_should_pan_chart(cd, api_cd):
                await _apply_coinmap_chart_view_adjustments_async(page, cd, api_cd)
                await page.wait_for_timeout(int(cd.get("chart_after_adjust_ms", 800)))
            written.append(json_path)
            prev_symbol = sym
    finally:
        await net_capture.uninstall()
    return written
