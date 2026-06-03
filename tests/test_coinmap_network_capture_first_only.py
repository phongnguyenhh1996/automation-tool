from __future__ import annotations

from automation_tool.coinmap import (
    CoinmapNetworkCapture,
    _coinmap_should_pan_chart,
    _network_capture_use_first_response_only,
    coinmap_network_last_body_per_key,
)


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


def test_network_capture_skips_empty_first_response_when_prefer_nonempty() -> None:
    api_cd = {"mode": "network_capture"}
    records = [
        {
            "key": "getcandlehistory",
            "ok": True,
            "status": 200,
            "body": [{"t": 99, "i": "15m", "s": "XAUUSD"}],
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
            "body": [{"t": 2, "i": "5m", "s": "XAUUSD"}],
        },
        {
            "key": "getindicatorsvwap",
            "ok": True,
            "status": 200,
            "body": [{"t": 2, "i": "5m", "s": "XAUUSD"}],
        },
    ]
    out = coinmap_network_last_body_per_key(
        records, step_ctx={"symbol": "XAUUSD", "interval": "5m"}, api_cd=api_cd
    )
    assert out["getcandlehistory"]["body"] == [{"t": 2, "i": "5m", "s": "XAUUSD"}]


def test_network_capture_slice_includes_responses_during_settle_window() -> None:
    """Responses that arrive during interval select + settle must fall after net_start."""
    api_cd = {"mode": "network_capture"}
    # net_start=0 simulates marker placed before select_interval; records are target-TF responses.
    records = [
        {
            "key": "getcandlehistory",
            "ok": True,
            "status": 200,
            "body": [{"t": 1, "i": "5m", "s": "XAUUSD"}],
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
            "body": [{"t": 1, "i": "5m", "s": "XAUUSD", "data": {}}],
        },
    ]
    out = coinmap_network_last_body_per_key(
        records, step_ctx={"symbol": "XAUUSD", "interval": "5m"}, api_cd=api_cd
    )
    assert out["getcandlehistory"]["ok"] is True
    assert len(out["getcandlehistory"]["body"]) == 1
