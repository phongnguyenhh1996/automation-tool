"""Tests for SCALP_EXEC line format/parse."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "scalp_footprint"))

from exec_line import (  # noqa: E402
    extract_exec_lines,
    format_exec_line,
    parse_exec_line,
    exec_to_parsed_trade,
    scalp_market_sl_tp,
)


def _sample_signal() -> dict:
    return {
        "pattern_id": "absorption_trap_long",
        "side": "BUY",
        "direction": "long",
        "entry_type": "limit",
        "entry_price": 4152.47,
        "stop_loss": 4146.1,
        "take_profit": [4156.47, 4160.47],
        "timeframe": "5m",
        "time_gmt7": "Mon Jul 6 2026 20:55:00 GMT+0700",
        "bar_index": 42,
    }


def test_scalp_market_sl_tp_long() -> None:
    sl, tp = scalp_market_sl_tp(3350.0, "BUY")
    assert sl == 3346.0
    assert tp == 3354.0


def test_scalp_market_sl_tp_short() -> None:
    sl, tp = scalp_market_sl_tp(3350.0, "SELL")
    assert sl == 3354.0
    assert tp == 3346.0


def test_format_and_parse_roundtrip() -> None:
    sig = _sample_signal()
    line = format_exec_line(sig, symbol="XAUUSD")
    assert line.startswith("SCALP_EXEC|")
    assert "|MARKET|" in line
    assert "|0|0|" in line
    parsed = parse_exec_line(line)
    assert parsed is not None
    assert parsed["pattern_id"] == "absorption_trap_long"
    assert parsed["side"] == "BUY"
    assert parsed["entry_price"] == 4152.47
    assert parsed["stop_loss"] == 0.0
    assert parsed["take_profit"] == [0.0]
    assert parsed["timeframe"] == "5m"
    assert parsed["bar_index"] == 42


def test_extract_exec_lines_from_alert_body() -> None:
    sig = _sample_signal()
    body = f"Human alert\n{format_exec_line(sig)}\nMore text"
    lines = extract_exec_lines(body)
    assert len(lines) == 1
    assert parse_exec_line(lines[0]) is not None


def test_exec_to_parsed_trade_from_live_entry() -> None:
    sig = _sample_signal()
    line = format_exec_line(sig)
    parsed = parse_exec_line(line)
    assert parsed is not None
    trade = exec_to_parsed_trade(parsed, lot=0.01, entry_price=3350.0)
    assert trade.side == "BUY"
    assert trade.kind == "MARKET"
    assert trade.price is None
    assert trade.sl == 3346.0
    assert trade.tp1 == 3354.0
    assert trade.tp2 is None
    assert trade.lot == 0.01


def test_parse_invalid_line() -> None:
    assert parse_exec_line("not exec") is None
    assert parse_exec_line("SCALP_EXEC|too|few|fields") is None
