"""query_template placeholders for bearer_request / request modes."""

from datetime import datetime, timezone

from automation_tool.coinmap import (
    _api_export_mode,
    _coinmap_endpoint_key_from_response_url,
    _coinmap_interval_minutes,
    _coinmap_interval_to_resolution_num,
    _merge_api_query_params,
    _merge_api_query_params_for_endpoint,
    _vn_session_anchor_utc_ms,
)


def test_interval_to_resolution_5m_15m_1h() -> None:
    assert _coinmap_interval_to_resolution_num("5m") == "5"
    assert _coinmap_interval_to_resolution_num("15m") == "15"
    assert _coinmap_interval_minutes("1h") == 60
    assert _coinmap_interval_to_resolution_num("1h") == "60"


def test_merge_placeholders_resolution_and_countback() -> None:
    api_cd = {
        "query_template": {
            "symbol": "{main_symbol}",
            "resolution": "{resolution}",
            "countback": "{countback}",
        },
        "auto_countback": 500,
    }
    step = {"symbol": "XAUUSD", "interval": "15m"}
    q = _merge_api_query_params(api_cd, step)
    assert q["resolution"] == "15"
    assert q["countback"] == "500"


def test_merge_auto_from_to_ordering() -> None:
    api_cd = {
        "query_template": {"from": "{from_ms}", "to": "{to_ms}"},
        "auto_countback": 5,
        "auto_from_to_mode": "countback",
    }
    step = {"interval": "5m", "symbol": "X"}
    q = _merge_api_query_params(api_cd, step)
    assert int(q["from"]) < int(q["to"])


def test_vn_session_anchor_before_5am_rolls_to_previous_day() -> None:
    """04:59 VN Jun 1 → anchor May 31 05:00 VN (UTC May 30 22:00)."""
    t = datetime(2024, 5, 31, 21, 59, 0, tzinfo=timezone.utc)
    ms = _vn_session_anchor_utc_ms(t, tz_name="Asia/Ho_Chi_Minh", start_hour=5)
    expected = datetime(2024, 5, 30, 22, 0, 0, tzinfo=timezone.utc)
    assert ms == int(expected.timestamp() * 1000)


def test_vn_session_anchor_after_5am_same_calendar_day() -> None:
    """09:00 VN Jun 1 → anchor Jun 1 05:00 VN (UTC May 31 22:00)."""
    t = datetime(2024, 6, 1, 2, 0, 0, tzinfo=timezone.utc)
    ms = _vn_session_anchor_utc_ms(t, tz_name="Asia/Ho_Chi_Minh", start_hour=5)
    expected = datetime(2024, 5, 31, 22, 0, 0, tzinfo=timezone.utc)
    assert ms == int(expected.timestamp() * 1000)


def test_auto_from_to_default_uses_vn_session_window() -> None:
    """Default ``auto_from_to_mode`` is vn_session: from anchor to now (from <= to)."""
    api_cd = {"query_template": {"from": "{from_ms}", "to": "{to_ms}"}, "auto_countback": 5}
    step = {"interval": "5m", "symbol": "X"}
    q = _merge_api_query_params(api_cd, step)
    assert int(q["from"]) <= int(q["to"])


def test_merge_symbol_is_per_step_not_main() -> None:
    """query_template.symbol: {symbol} → capture_plan watchlist code (e.g. USDINDEX)."""
    api_cd = {
        "query_template": {
            "symbol": "{symbol}",
            "resolution": "{resolution}",
        },
        "auto_countback": 100,
    }
    step = {
        "symbol": "USDINDEX",
        "interval": "15m",
        "export_symbol": "DXY",
        "watchlist_category": "forex 1",
    }
    q = _merge_api_query_params(api_cd, step)
    assert q["symbol"] == "USDINDEX"
    assert q["resolution"] == "15"


def test_merge_export_symbol_placeholder() -> None:
    api_cd = {"query_template": {"label": "{export_symbol}"}}
    step = {"symbol": "USDINDEX", "export_symbol": "DXY"}
    q = _merge_api_query_params(api_cd, step)
    assert q["label"] == "DXY"

    step2 = {"symbol": "XAUUSD"}
    q2 = _merge_api_query_params(api_cd, step2)
    assert q2["label"] == "XAUUSD"

def test_merge_cvd_params_maps_period_to_cvd_and_drops_unused_keys() -> None:
    api_cd = {
        "query_template": {
            "symbol": "XAUUSD",
            "period": "day",
            "source": "hlc3",
            "bandsmultiplier": "1,2,3",
            "typedata": "dly",
            "resolution": "{resolution}",
            "countback": "{countback}",
        },
        "auto_countback": 1000,
    }
    step = {"symbol": "XAUUSD", "interval": "15m"}
    q_base = _merge_api_query_params(api_cd, step)
    assert "period" in q_base
    qcvd = _merge_api_query_params_for_endpoint(api_cd, step, "getcandlehistorycvd")
    assert qcvd["cvd"] == "day"
    assert "period" not in qcvd
    assert "source" not in qcvd
    assert "bandsmultiplier" not in qcvd
    assert qcvd["resolution"] == "15"
    assert qcvd["countback"] == "1000"


def test_endpoint_key_prefers_cvd_path_suffix() -> None:
    assert (
        _coinmap_endpoint_key_from_response_url(
            "https://gw.coinmap.tech/cm-api/api/v1/getcandlehistorycvd?cvd=day"
        )
        == "getcandlehistorycvd"
    )
    assert (
        _coinmap_endpoint_key_from_response_url(
            "https://gw.coinmap.tech/cm-api/api/v1/getcandlehistory?x=1"
        )
        == "getcandlehistory"
    )


def test_api_export_mode_defaults_to_bearer_request() -> None:
    assert _api_export_mode({}) == "bearer_request"


def test_bearer_http_parallel_enabled_default_and_off() -> None:
    from automation_tool.coinmap import _bearer_http_parallel_enabled

    assert _bearer_http_parallel_enabled({}) is True
    assert _bearer_http_parallel_enabled({"bearer_http_parallel": False}) is False
    assert _bearer_http_parallel_enabled({"bearer_http_parallel": True}) is True
