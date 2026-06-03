from __future__ import annotations

from automation_tool.coinmap import (
    CoinmapNetworkCapture,
    _coinmap_should_pan_chart,
    _coinmap_should_reload_after_interval,
    _network_capture_use_first_response_only,
)


def test_should_reload_after_interval_for_network_capture_by_default() -> None:
    cd: dict = {}
    api_cd = {"mode": "network_capture"}
    assert _coinmap_should_reload_after_interval(cd, api_cd) is True


def test_should_not_reload_when_disabled_or_not_network_capture() -> None:
    assert _coinmap_should_reload_after_interval(
        {"api_network_capture_reload_after_interval": False},
        {"mode": "network_capture"},
    ) is False
    assert _coinmap_should_reload_after_interval({}, {"mode": "bearer_request"}) is False


def test_should_not_pan_for_network_capture_json_only() -> None:
    cd = {"chart_view_adjustments_enabled": True, "coinmap_screenshot_enabled": False}
    api_cd = {"mode": "network_capture"}
    assert _coinmap_should_pan_chart(cd, api_cd) is False


def test_should_pan_when_network_capture_screenshot_enabled() -> None:
    cd = {"chart_view_adjustments_enabled": True, "coinmap_screenshot_enabled": True}
    api_cd = {"mode": "network_capture"}
    assert _coinmap_should_pan_chart(cd, api_cd) is True


def test_network_capture_keeps_first_response_per_endpoint() -> None:
    cap = CoinmapNetworkCapture(page=None, api_cd={"mode": "network_capture"})  # type: ignore[arg-type]
    cap._records = [
        {
            "key": "getcandlehistory",
            "ok": True,
            "status": 200,
            "body": [{"t": 1, "i": "5m", "s": "XAUUSD"}],
        },
        {
            "key": "getcandlehistory",
            "ok": True,
            "status": 200,
            "body": [{"t": 2, "i": "5m", "s": "XAUUSD"}],
        },
        {
            "key": "getorderflowhistory",
            "ok": True,
            "status": 200,
            "body": [{"t": 1, "i": "5m", "s": "XAUUSD"}],
        },
        {
            "key": "getindicatorsvwap",
            "ok": True,
            "status": 200,
            "body": [{"t": 1, "i": "5m", "s": "XAUUSD"}],
        },
    ]
    out = cap._last_body_per_key(cap._records, {"symbol": "XAUUSD", "interval": "5m"})
    assert out["getcandlehistory"]["body"] == [{"t": 1, "i": "5m", "s": "XAUUSD"}]
    assert _network_capture_use_first_response_only({"mode": "network_capture"}) is True
