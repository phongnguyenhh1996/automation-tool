from __future__ import annotations

from automation_tool.mt5_openai_parse import (
    extract_output_ngan_gon_block,
    is_last_price_hit_stop_loss,
    parse_journal_intraday_action,
    parse_journal_intraday_action_from_openai_text,
    parse_openai_output_md,
    parse_trade_line,
)


def test_parse_openai_output_json_intraday() -> None:
    text = r"""
{"intraday_hanh_dong": "VÀO LỆNH",
 "trade_line": "SELL LIMIT 3360.0 | SL 3363.5 | TP1 3354.5 | TP2 3350.5 | Lot 0.02"}
"""
    trade, err = parse_openai_output_md(text, default_symbol="XAUUSD")
    assert err is None
    assert trade is not None
    assert trade.side == "SELL"
    assert trade.price == 3360.0


def test_schema_e_vao_lenh_uses_fallback_trade_line() -> None:
    """Schema E: không có trade_line trong JSON — dùng baseline vùng."""
    text = '{"phan_tich_alert": "OK", "intraday_hanh_dong": "VÀO LỆNH"}'
    fb = "SELL LIMIT 3360.0 | SL 3363.5 | TP1 3354.5 | Lot 0.02"
    trade, err = parse_openai_output_md(
        text,
        default_symbol="XAUUSD",
        fallback_trade_line=fb,
    )
    assert err is None
    assert trade is not None
    assert trade.side == "SELL"
    assert trade.price == 3360.0


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
    assert trade.symbol == "XAUUSDm"
    assert trade.side == "SELL"
    assert trade.kind == "LIMIT"
    assert trade.price == 3360.0
    assert trade.sl == 3363.5
    assert trade.tp1 == 3354.5
    assert trade.tp2 == 3350.5
    assert trade.lot == 0.02


def test_markdown_xauusd_hint_becomes_xauusdm() -> None:
    """Hint 📊 XAUUSD trong markdown được chuẩn hóa thành XAUUSDm."""
    text = """
📊 XAUUSD – PHÂN TÍCH
[OUTPUT_NGAN_GON]
SELL LIMIT 1.0 | SL 2.0 | TP1 0.5 | Lot 0.01
Hành động: VÀO LỆNH
"""
    trade, err = parse_openai_output_md(text, default_symbol="XAUUSD")
    assert err is None
    assert trade is not None
    assert trade.symbol == "XAUUSDm"


def test_symbol_override_xauusd_normalized() -> None:
    text = """
[OUTPUT_NGAN_GON]
BUY LIMIT 100.0 | SL 99.0 | TP1 101.0 | Lot 0.01
Hành động: VÀO LỆNH
"""
    trade, err = parse_openai_output_md(
        text,
        default_symbol="XAUUSD",
        symbol_override="XAUUSD",
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
    assert trade.symbol == "XAUUSDm"
    assert trade.side == "SELL"


def test_journal_intraday_action_cho_loai_vao_lenh() -> None:
    block = extract_output_ngan_gon_block(
        "[OUTPUT_NGAN_GON]\nHành động: chờ\n"
    )
    assert block is not None
    assert parse_journal_intraday_action(block) == "chờ"

    block2 = extract_output_ngan_gon_block(
        "[OUTPUT_NGAN_GON]\nBias: x\nHành động: loại\n"
    )
    assert parse_journal_intraday_action(block2 or "") == "loại"

    block3 = extract_output_ngan_gon_block(
        "[OUTPUT_NGAN_GON]\n"
        "BUY LIMIT 1 | SL 2 | TP1 3 | Lot 0.01\n"
        "Hành động: VÀO LỆNH\n"
    )
    assert parse_journal_intraday_action(block3 or "") == "VÀO LỆNH"


def test_journal_intraday_action_from_json() -> None:
    raw = '{"intraday_hanh_dong": "chờ"}'
    assert parse_journal_intraday_action_from_openai_text(raw) == "chờ"
    raw2 = "[OUTPUT_NGAN_GON]\nHành động: loại\n"
    assert parse_journal_intraday_action_from_openai_text(raw2) == "loại"


def test_journal_intraday_last_action_wins() -> None:
    block = extract_output_ngan_gon_block(
        "[OUTPUT_NGAN_GON]\nHành động: chờ\n...\nHành động: loại\n"
    )
    assert parse_journal_intraday_action(block or "") == "loại"


def test_is_last_price_hit_stop_loss_buy_sell() -> None:
    buy = parse_trade_line("BUY LIMIT 100.0 | SL 99.0 | TP1 101.0 | Lot 0.01", "XAUUSD")
    assert buy is not None
    assert is_last_price_hit_stop_loss(98.9, buy)
    assert not is_last_price_hit_stop_loss(100.0, buy)

    sell = parse_trade_line("SELL LIMIT 1.0 | SL 2.0 | TP1 0.5 | Lot 0.01", "XAUUSD")
    assert sell is not None
    assert is_last_price_hit_stop_loss(2.01, sell)
    assert not is_last_price_hit_stop_loss(1.5, sell)
