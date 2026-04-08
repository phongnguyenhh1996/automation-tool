"""Đọc giá Last realtime từ Watchlist TradingView (dùng chung watchlist + journal + TP1)."""

from __future__ import annotations

from typing import Any, Optional

from playwright.sync_api import Page

from automation_tool.tradingview_touch_flow import parse_first_float_trunc0


def read_watchlist_last_price_stable(
    page: Page,
    tv: dict[str, Any],
    *,
    symbol: str,
) -> Optional[float]:
    """
    Trả về giá Last (float) khi ô giá **ổn định** (không class highlightUp/Down), ngược lại None.
    """
    row_tpl = (tv.get("watchlist_row_selector") or "").strip()
    if not row_tpl:
        row_tpl = '[data-symbol-short="{symbol}"]'
    row_sel = row_tpl.format(symbol=symbol)
    row = page.locator(row_sel).first
    row.wait_for(state="visible", timeout=30_000)

    cell_sel = (tv.get("watchlist_last_cell_selector") or "").strip()
    if not cell_sel:
        cell_sel = 'span[class*="cell"][class*="last"] span[class*="inner"]'
    price_span = row.locator(cell_sel).first
    price_span.wait_for(state="visible", timeout=15_000)

    deny = tv.get("watchlist_price_stable_class_prefix_denylist")
    prefixes = ["highlightUp-", "highlightDown-"]
    if isinstance(deny, list) and deny:
        prefixes = [str(x) for x in deny if str(x)]

    cls = (price_span.get_attribute("class") or "").strip()
    for pref in prefixes:
        if pref and pref in cls:
            return None

    txt = price_span.inner_text(timeout=5_000)
    return parse_first_float_trunc0(txt)
