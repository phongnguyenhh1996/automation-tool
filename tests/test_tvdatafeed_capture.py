"""Unit tests for tvdatafeed_capture helpers (no network)."""

from __future__ import annotations

import pytest

from automation_tool.tvdatafeed_capture import (
    _merge_interval_map,
    _n_bars_for_interval_label,
    _parse_capture_plan_rows,
    effective_tvdatafeed_plan,
    parse_tradingview_chart_url,
    resolve_tvdatafeed_credentials,
    tradingview_signin_diagnose,
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


def test_resolve_tvdatafeed_credentials_yaml_then_args(monkeypatch) -> None:
    monkeypatch.delenv("TRADINGVIEW_USERNAME", raising=False)
    monkeypatch.delenv("COINMAP_EMAIL", raising=False)
    monkeypatch.delenv("TRADINGVIEW_PASSWORD", raising=False)
    tv = {
        "tvdatafeed": {"username": "u1", "password": "p1"},
    }
    u, p = resolve_tvdatafeed_credentials(tv, tradingview_username="u2", tradingview_password="p2")
    assert u == "u1"
    assert p == "p1"


def test_tradingview_signin_diagnose_success(monkeypatch) -> None:
    class FakeResp:
        status_code = 200
        text = "{}"

        def json(self) -> dict:
            return {"user": {"auth_token": "toktoktok"}}

    class FakeClient:
        def __init__(self, *a: object, **k: object) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *a: object) -> None:
            pass

        def get(self, *a: object, **k: object) -> FakeResp:
            return FakeResp()

        def post(self, *a: object, **k: object) -> FakeResp:
            return FakeResp()

    monkeypatch.setattr(
        "automation_tool.tvdatafeed_capture.httpx.Client", lambda *a, **k: FakeClient()
    )
    ok, msg = tradingview_signin_diagnose("u", "p")
    assert ok is True
    assert "auth_token received" in msg
    assert "length=" in msg
    assert "toktok" not in msg


def test_tradingview_signin_diagnose_json_error(monkeypatch) -> None:
    class FakeResp:
        status_code = 403
        text = "{}"

        def json(self) -> dict:
            return {"error": "Invalid credentials"}

    class FakeClient:
        def __init__(self, *a: object, **k: object) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *a: object) -> None:
            pass

        def get(self, *a: object, **k: object) -> FakeResp:
            return FakeResp()

        def post(self, *a: object, **k: object) -> FakeResp:
            return FakeResp()

    monkeypatch.setattr(
        "automation_tool.tvdatafeed_capture.httpx.Client", lambda *a, **k: FakeClient()
    )
    ok, msg = tradingview_signin_diagnose("u", "p")
    assert ok is False
    assert "403" in msg
    assert "Invalid credentials" in msg


def test_resolve_tvdatafeed_credentials_env_fallback(monkeypatch) -> None:
    monkeypatch.setenv("TRADINGVIEW_USERNAME", "tvu")
    monkeypatch.setenv("TRADINGVIEW_PASSWORD", "tvp")
    u, p = resolve_tvdatafeed_credentials({}, tradingview_username=None, tradingview_password=None)
    assert u == "tvu"
    assert p == "tvp"


def test_n_bars_for_interval_label_map_and_fallback() -> None:
    tvd = {
        "n_bars_by_interval": {
            "1 giờ": 50,
            "15 phút": 70,
            "5 phút": 100,
        }
    }
    assert _n_bars_for_interval_label("1 giờ", tvd, 1000) == 50
    assert _n_bars_for_interval_label("15 phút", tvd, 1000) == 70
    assert _n_bars_for_interval_label("5 phút", tvd, 1000) == 100
    assert _n_bars_for_interval_label("30 phút", tvd, 999) == 999
    assert _n_bars_for_interval_label("30 phút", {}, 888) == 888


def test_n_bars_for_interval_label_clamp_and_bad_value() -> None:
    tvd = {"n_bars_by_interval": {"1 giờ": 6000, "x": "notint"}}
    assert _n_bars_for_interval_label("1 giờ", tvd, 100) == 5000
    assert _n_bars_for_interval_label("x", tvd, 200) == 200


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
