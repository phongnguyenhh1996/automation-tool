"""Live MT5 XAU price for scalp SL/TP — không dùng giá gold futures từ Telegram."""

from __future__ import annotations

from typing import Any, Optional

from automation_tool.mt5_accounts import reference_price_for_lot
from automation_tool.mt5_candles import resolve_mt5_broker_symbol
from automation_tool.mt5_execute import _ensure_symbol
from automation_tool.mt5_openai_parse import ParsedTrade

from exec_line import DEFAULT_SL_POINTS, DEFAULT_TP_POINTS, scalp_market_sl_tp

XAU_LOGIC = "XAUUSD"
XAU_BROKER_CANDIDATES = ("XAUUSD", "XAUUSDm", "XAUUSDc")


def xau_symbol_candidates(account_symbol_map: Optional[dict[str, str]] = None) -> list[str]:
    out: list[str] = []
    mapped = resolve_mt5_broker_symbol(XAU_LOGIC, account_symbol_map=account_symbol_map)
    for cand in (mapped, *XAU_BROKER_CANDIDATES):
        name = (cand or "").strip()
        if name and name not in out:
            out.append(name)
    return out


def resolve_xau_symbol_on_mt5(
    mt5: Any,
    account_symbol_map: Optional[dict[str, str]] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Thử XAUUSD / XAUUSDm / XAUUSDc (+ symbol_map) trên broker."""
    last_err: Optional[str] = None
    for cand in xau_symbol_candidates(account_symbol_map):
        sym, err = _ensure_symbol(mt5, cand)
        if sym:
            return sym, None
        last_err = err
    return None, last_err or "Không tìm thấy XAUUSD/XAUUSDm/XAUUSDc trên broker"


def fetch_market_entry_price(mt5: Any, resolved_symbol: str, side: str) -> tuple[float, Optional[str]]:
    side_lit: str = "BUY" if side == "BUY" else "SELL"
    stub = ParsedTrade(
        symbol=resolved_symbol,
        side=side_lit,  # type: ignore[arg-type]
        kind="MARKET",
        price=None,
        sl=0.0,
        tp1=0.0,
        tp2=None,
        lot=0.01,
        raw_line="",
    )
    return reference_price_for_lot(mt5, resolved_symbol, stub)


def build_scalp_trade_live(
    parsed: dict[str, Any],
    *,
    lot: float,
    mt5: Any,
    account_symbol_map: Optional[dict[str, str]] = None,
    sl_points: float = DEFAULT_SL_POINTS,
    tp_points: float = DEFAULT_TP_POINTS,
) -> tuple[ParsedTrade, float]:
    """
    SL/TP ±N giá từ bid/ask MT5 hiện tại (BUY=ask, SELL=bid).
    Trả về ``(trade, live_entry)``.
    """
    side = str(parsed.get("side") or "")
    sym, err = resolve_xau_symbol_on_mt5(mt5, account_symbol_map)
    if not sym:
        raise RuntimeError(err or "Không resolve được symbol XAU trên MT5")

    entry, price_err = fetch_market_entry_price(mt5, sym, side)
    if entry <= 0:
        raise RuntimeError(price_err or f"symbol_info_tick({sym!r}) không có giá")

    sl, tp1 = scalp_market_sl_tp(entry, side, sl_points=sl_points, tp_points=tp_points)
    side_lit: str = "BUY" if side == "BUY" else "SELL"
    trade = ParsedTrade(
        symbol=sym,
        side=side_lit,  # type: ignore[arg-type]
        kind="MARKET",
        price=None,
        sl=sl,
        tp1=tp1,
        tp2=None,
        lot=float(lot),
        raw_line=str(parsed.get("raw_line") or ""),
    )
    return trade, entry
