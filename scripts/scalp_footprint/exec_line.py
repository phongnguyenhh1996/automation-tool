"""Machine-readable scalp EXEC lines for Telegram → VPS MT5 executor."""

from __future__ import annotations

from typing import Any, Literal, Optional

from signal_tracker import trade_id_from_signal

EXEC_PREFIX = "SCALP_EXEC"
# SCALP_EXEC|pattern|side|kind|entry|sl|tp1|tp2|tf|time_gmt7|bar_index|symbol

DEFAULT_SL_POINTS = 4.0
DEFAULT_TP_POINTS = 4.0


def scalp_market_sl_tp(
    entry: float,
    side: str,
    *,
    sl_points: float = DEFAULT_SL_POINTS,
    tp_points: float = DEFAULT_TP_POINTS,
) -> tuple[float, float]:
    """Fixed scalp risk: MARKET entry ref, SL/TP ±N giá from entry."""
    if side == "BUY":
        return round(entry - sl_points, 2), round(entry + tp_points, 2)
    return round(entry + sl_points, 2), round(entry - tp_points, 2)


def format_exec_line(
    sig: dict[str, Any],
    *,
    symbol: str = "XAUUSD",
) -> str:
    """EXEC line: entry = tham chiếu signal (futures); SL/TP = 0 (VPS tính từ giá MT5 live)."""
    side = sig.get("side") or ("BUY" if sig.get("direction") == "long" else "SELL")
    entry = float(sig.get("entry_price", 0))
    return "|".join(
        [
            EXEC_PREFIX,
            str(sig.get("pattern_id") or ""),
            side,
            "MARKET",
            f"{entry:.2f}",
            "0",
            "0",
            "",
            str(sig.get("timeframe") or ""),
            str(sig.get("time_gmt7") or ""),
            str(sig.get("bar_index") or ""),
            symbol,
        ]
    )


def parse_exec_line(line: str) -> Optional[dict[str, Any]]:
    """Parse a SCALP_EXEC line. Returns None if not a valid exec line."""
    raw = (line or "").strip()
    if not raw.startswith(EXEC_PREFIX + "|"):
        return None
    parts = raw.split("|")
    if len(parts) != 12:
        return None
    if parts[0] != EXEC_PREFIX:
        return None
    try:
        entry = float(parts[4])
        sl = float(parts[5]) if parts[5].strip() else 0.0
        tp1 = float(parts[6]) if parts[6].strip() else 0.0
        tp2 = float(parts[7]) if parts[7].strip() else None
        bar_index = int(parts[10])
    except (TypeError, ValueError):
        return None
    return {
        "pattern_id": parts[1],
        "side": parts[2],
        "kind": parts[3],
        "entry_price": entry,
        "stop_loss": sl,
        "take_profit": [tp1] + ([tp2] if tp2 is not None else []),
        "timeframe": parts[8],
        "time_gmt7": parts[9],
        "bar_index": bar_index,
        "symbol": parts[11],
        "trade_id": trade_id_from_signal(
            {
                "timeframe": parts[8],
                "time_gmt7": parts[9],
                "pattern_id": parts[1],
                "bar_index": bar_index,
            }
        ),
        "raw_line": raw,
    }


def exec_to_parsed_trade(
    parsed: dict[str, Any],
    *,
    lot: float,
    entry_price: float,
    symbol_override: Optional[str] = None,
    sl_points: float = DEFAULT_SL_POINTS,
    tp_points: float = DEFAULT_TP_POINTS,
) -> "ParsedTrade":
    """Build ParsedTrade from explicit entry (typically MT5 live bid/ask)."""
    from automation_tool.mt5_openai_parse import ParsedTrade

    side: Literal["BUY", "SELL"] = "BUY" if parsed["side"] == "BUY" else "SELL"
    sl, tp1 = scalp_market_sl_tp(entry_price, side, sl_points=sl_points, tp_points=tp_points)
    symbol = symbol_override or str(parsed.get("symbol") or "XAUUSD")
    return ParsedTrade(
        symbol=symbol,
        side=side,
        kind="MARKET",
        price=None,
        sl=sl,
        tp1=tp1,
        tp2=None,
        lot=float(lot),
        raw_line=str(parsed.get("raw_line") or ""),
    )


def extract_exec_lines(text: str) -> list[str]:
    """Return all SCALP_EXEC lines found in a Telegram message body."""
    out: list[str] = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if s.startswith(EXEC_PREFIX + "|"):
            out.append(s)
    return out
