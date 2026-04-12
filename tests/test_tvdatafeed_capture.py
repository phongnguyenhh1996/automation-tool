"""Unit tests for tvdatafeed_capture helpers (no network)."""

from __future__ import annotations

import pytest

from automation_tool.tvdatafeed_capture import (
    _merge_interval_map,
    _parse_capture_plan_rows,
    effective_tvdatafeed_plan,
    parse_tradingview_chart_url,
)


def test_parse_tradingview_chart_url_oanda_xauusd() -> None:
    url = "https://vn.tradingview.com/chart/?symbol=OANDA%3AXAUUSD"
    ex, sym = parse_tradingview_chart_url(url)
    assert ex == "OANDA"
    assert sym == "XAUUSD"


def test_parse_tradingview_chart_url_empty() -> None:
    ex, sym = parse_tradingview_chart_url("")
    assert ex == ""
    assert sym == "XAUUSD"


def test_merge_interval_map_overrides() -> None:
    tv = {}
    tvd = {"interval_map": {"9 phút": "in_5_minute"}}
    m = _merge_interval_map(tv, tvd)
    assert m["9 phút"] == "in_5_minute"
    assert m["5 phút"] == "in_5_minute"


def test_parse_capture_plan_rows_exchange() -> None:
    tv = {
        "capture_plan": [
            {"symbol": "DXY", "exchange": "TVC", "intervals": ["4 giờ"]},
        ]
    }
    rows = _parse_capture_plan_rows(tv)
    assert len(rows) == 1
    assert rows[0]["exchange"] == "TVC"
    assert rows[0]["symbol"] == "DXY"


def test_effective_plan_single_shot() -> None:
    tv = {
        "multi_shot_enabled": False,
        "chart_url": "https://x.com/chart/?symbol=FX_IDC%3AUSDINDEX",
        "interval_button_aria_label": "1 giờ",
    }
    rows = effective_tvdatafeed_plan(tv)
    assert len(rows) == 1
    assert rows[0]["intervals"] == ["1 giờ"]
    assert rows[0]["exchange"] == "FX_IDC"
    assert rows[0]["symbol"] == "USDINDEX"


def test_interval_enum_resolution() -> None:
    from tvDatafeed import Interval

    from automation_tool.tvdatafeed_capture import _interval_enum_from_label

    m = _merge_interval_map({}, {})
    iv = _interval_enum_from_label("15 phút", interval_map=m, row=None)
    assert iv == Interval.in_15_minute

    with pytest.raises(ValueError, match="Unknown interval"):
        _interval_enum_from_label("not_a_real_label_xyz", interval_map=m, row=None)
