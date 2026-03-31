from __future__ import annotations

from automation_tool.mt5_openai_parse import (
    parse_openai_output_md,
    parse_trade_line,
)


def test_parse_sample_output_md() -> None:
    text = """
[OUTPUT_NGAN_GON]

Bias chính: SELL
Lệnh đủ điều kiện vào ngay:
SELL LIMIT 3360.0 | SL 3363.5 | TP1 3354.5 | TP2 3350.5 | Lot 0.02

Hành động: VÀO LỆNH
"""
    trade, err = parse_openai_output_md(text, default_symbol="XAUUSD")
    assert err is None
    assert trade is not None
    assert trade.symbol == "XAUUSD"
    assert trade.side == "SELL"
    assert trade.kind == "LIMIT"
    assert trade.price == 3360.0
    assert trade.sl == 3363.5
    assert trade.tp1 == 3354.5
    assert trade.tp2 == 3350.5
    assert trade.lot == 0.02


def test_symbol_override() -> None:
    text = """
[OUTPUT_NGAN_GON]
BUY LIMIT 100.0 | SL 99.0 | TP1 101.0 | Lot 0.01
Hành động: VÀO LỆNH
"""
    trade, err = parse_openai_output_md(
        text,
        default_symbol="XAUUSD",
        symbol_override="XAUUSDm",
    )
    assert err is None
    assert trade is not None
    assert trade.symbol == "XAUUSDm"


def test_dung_ngoai_skips_trade() -> None:
    text = """
[OUTPUT_NGAN_GON]
SELL LIMIT 3360.0 | SL 3363.5 | TP1 3354.5 | Lot 0.02
Hành động: ĐỨNG NGOÀI
"""
    trade, err = parse_openai_output_md(text)
    assert trade is None
    assert err is not None


def test_parse_trade_line_market_optional() -> None:
    line = "BUY MARKET | SL 99.0 | TP1 101.0 | Lot 0.01"
    p = parse_trade_line(line, "XAUUSD")
    assert p is not None
    assert p.kind == "MARKET"
    assert p.price is None
    assert p.sl == 99.0


def test_missing_output_ngan_gon() -> None:
    trade, err = parse_openai_output_md("no markers here")
    assert trade is None
    assert err is not None


def test_uses_last_output_ngan_gon_marker() -> None:
    """Prompt template có [OUTPUT_NGAN_GON] giả; phần sau cùng là kết quả thật."""
    text = """
[OUTPUT_NGAN_GON]
Hành động: ĐỨNG NGOÀI
[OUTPUT_CHI_TIET]
x
[OUTPUT_NGAN_GON]
SELL LIMIT 1.0 | SL 2.0 | TP1 0.5 | Lot 0.01
Hành động: VÀO LỆNH
"""
    trade, err = parse_openai_output_md(text, default_symbol="XAUUSD")
    assert err is None
    assert trade is not None
    assert trade.side == "SELL"
