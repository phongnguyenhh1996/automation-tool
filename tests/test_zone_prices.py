"""Tests for zone price parsing and comparison."""

from automation_tool.tradingview_alerts import (
    format_price_for_tradingview_input,
    parse_tv_alert_price_from_description,
)
from automation_tool.zone_prices import (
    parse_three_zone_prices,
    prices_equal_triple,
)


def test_parse_three_sections_buy_sell() -> None:
    text = """
📍 PLAN CHÍNH VÙNG CHỜ
BUY
4698.0 – 4693.0

📍 PLAN PHỤ VÙNG CHỜ
SELL
2700.5 – 2695.0

⚡️SCALP VÙNG
BUY
100.0 – 90.0
"""
    zt, err = parse_three_zone_prices(text)
    assert err is None
    assert zt is not None
    assert zt[0] == 4698.0
    assert zt[1] == 2695.0
    assert zt[2] == 100.0


def test_prices_equal_triple() -> None:
    assert prices_equal_triple((1.0, 2.0, 3.0), (1.0, 2.0, 3.0))
    assert prices_equal_triple((1.001, 2.0, 3.0), (1.0, 2.0, 3.0), eps=0.05)
    assert not prices_equal_triple((1.0, 2.0, 3.0), (1.1, 2.0, 3.0))


def test_format_tv_input() -> None:
    assert format_price_for_tradingview_input(4708.0) == "4,708.000"


def test_parse_tv_description() -> None:
    assert parse_tv_alert_price_from_description("XAUUSD Giao cắt 4,708.000") == 4708.0
