"""query_template placeholders for bearer_request / request modes."""

from automation_tool.coinmap import (
    _api_export_mode,
    _coinmap_interval_minutes,
    _coinmap_interval_to_resolution_num,
    _merge_api_query_params,
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
    }
    step = {"interval": "5m", "symbol": "X"}
    q = _merge_api_query_params(api_cd, step)
    assert int(q["from"]) < int(q["to"])


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

def test_api_export_mode_defaults_to_bearer_request() -> None:
    assert _api_export_mode({}) == "bearer_request"


def test_bearer_http_parallel_enabled_default_and_off() -> None:
    from automation_tool.coinmap import _bearer_http_parallel_enabled

    assert _bearer_http_parallel_enabled({}) is True
    assert _bearer_http_parallel_enabled({"bearer_http_parallel": False}) is False
    assert _bearer_http_parallel_enabled({"bearer_http_parallel": True}) is True
