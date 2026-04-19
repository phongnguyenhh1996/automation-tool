"""Pure helpers: giá đạt mức lợi 1R (khoảng Entry–SL) theo hướng có lợi — dùng daemon zones."""

from __future__ import annotations

from automation_tool.mt5_openai_parse import ParsedTrade


def entry_reference_price(parsed: ParsedTrade) -> float:
    """Đồng bộ với ``tv_watchlist_daemon._entry_reference_price`` / ``tp1_followup._entry_reference_price``."""
    if parsed.kind == "MARKET" or parsed.price is None:
        return (float(parsed.sl) + float(parsed.tp1)) / 2.0
    return float(parsed.price)


def risk_price_distance(parsed: ParsedTrade) -> float:
    """R = |entry_ref − SL| (đơn vị giá)."""
    ref = entry_reference_price(parsed)
    return abs(ref - float(parsed.sl))


def one_r_favorable_price(parsed: ParsedTrade) -> float:
    """Mức giá tương đương +1R theo hướng TP (BUY: lên; SELL: xuống)."""
    ref = entry_reference_price(parsed)
    r = risk_price_distance(parsed)
    if parsed.side == "BUY":
        return ref + r
    return ref - r


def one_r_reached(parsed: ParsedTrade, p_last: float, *, eps: float = 0.01) -> bool:
    """
    True khi giá đã chạm/vượt mức 1R có lợi (khoảng từ entry_ref tới giá hiện tại
    theo hướng lợi nhuận bằng khoảng Entry–SL).
    """
    ref = entry_reference_price(parsed)
    r = risk_price_distance(parsed)
    if r <= 0:
        return False
    target = one_r_favorable_price(parsed)
    if parsed.side == "BUY":
        return p_last >= target - eps
    return p_last <= target + eps
