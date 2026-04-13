"""Tests for zone price parsing and comparison."""

from automation_tool.tradingview_alerts import (
    format_price_for_tradingview_input,
    parse_tv_alert_price_from_description,
)
from automation_tool.zone_prices import (
    parse_three_zone_prices,
    parse_update_zone_triple,
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
    zt, err, nc = parse_three_zone_prices(text)
    assert err is None
    assert nc is None
    assert zt is not None
    assert zt[0] == 4698.0
    assert zt[1] == 2695.0
    assert zt[2] == 100.0


def test_prices_equal_triple() -> None:
    assert prices_equal_triple((1.0, 2.0, 3.0), (1.0, 2.0, 3.0))
    assert prices_equal_triple((1.001, 2.0, 3.0), (1.0, 2.0, 3.0), eps=0.05)
    assert not prices_equal_triple((1.0, 2.0, 3.0), (1.1, 2.0, 3.0))


def test_parse_three_zone_prices_from_json() -> None:
    text = """```json
{
  "prices": [
    {"label": "plan_chinh", "value": 4698.0},
    {"label": "plan_phu", "value": 2695.0},
    {"label": "scalp", "value": 100.0}
  ]
}
```"""
    zt, err, nc = parse_three_zone_prices(text)
    assert err is None
    assert nc is None
    assert zt is not None
    assert zt == (4698.0, 2695.0, 100.0)


def test_parse_no_change_json() -> None:
    text = '{"no_change": true}'
    zt, err, nc = parse_three_zone_prices(text)
    assert zt is None
    assert err is None
    assert nc is True


def test_parse_update_zone_triple_merges_per_no_change() -> None:
    text = """{
      "intraday_hanh_dong": "chờ",
      "trade_line": "",
      "prices": [
        {"label": "plan_chinh", "value": 10.0, "vung_cho": "9.0–11.0", "hop_luu": 50, "trade_line": "", "no_change": false},
        {"label": "plan_phu", "value": 99.0, "vung_cho": "98.0–100.0", "hop_luu": 50, "trade_line": "", "no_change": true},
        {"label": "scalp", "value": 30.0, "vung_cho": "29.0–31.0", "hop_luu": 50, "trade_line": "", "no_change": false}
      ]
    }"""
    baseline = (1.0, 2.0, 3.0)
    zt, err, nc = parse_update_zone_triple(text, baseline)
    assert err is None
    assert nc is False
    assert zt == (10.0, 2.0, 30.0)


def test_parse_update_zone_triple_root_no_change() -> None:
    zt, err, nc = parse_update_zone_triple('{"no_change": true}', (1.0, 2.0, 3.0))
    assert zt is None and err is None and nc is True


def test_format_tv_input() -> None:
    assert format_price_for_tradingview_input(4708.0) == "4,708.000"


def test_parse_tv_description() -> None:
    assert parse_tv_alert_price_from_description("XAUUSD Giao cắt 4,708.000") == 4708.0
