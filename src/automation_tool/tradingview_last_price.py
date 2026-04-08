"""Đọc giá Last realtime từ Watchlist TradingView (dùng chung watchlist + journal + TP1)."""

from __future__ import annotations

import re
import time
from typing import Any, Optional

from playwright.sync_api import Page

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
    """Giống giá trên UI watchlist: làm tròn về số nguyên (4656.355 → 4656.0)."""
    v = _parse_first_float(text)
    if v is None:
        return None
    try:
        return float(int(v))
    except Exception:
        return None


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


def read_watchlist_last_price_wait_stable(
    page: Page,
    tv: dict[str, Any],
    *,
    symbol: str,
    timeout_ms: int = 10_000,
    poll_ms: int = 250,
) -> Optional[float]:
    """
    Poll cho đến khi ô Last không còn highlight (ổn định) hoặc hết ``timeout_ms``.
    """
    deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
    poll = max(50, int(poll_ms))
    while time.monotonic() < deadline:
        p = read_watchlist_last_price_stable(page, tv, symbol=symbol)
        if p is not None:
            return p
        page.wait_for_timeout(poll)
    return None
