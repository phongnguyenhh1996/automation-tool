"""Coinmap JSON slimming for OpenAI embedding."""

from pathlib import Path

import pytest

from automation_tool.coinmap_openai_slim import (
    slim_coinmap_export_for_openai,
    slim_limits_for_interval,
)


def _clear_slim_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaults in coinmap_openai_slim use _env_int; clear overrides for stable tests."""
    for name in (
        "COINMAP_OPENAI_BARS_15M",
        "COINMAP_OPENAI_FP_15M",
        "COINMAP_OPENAI_BARS_5M",
        "COINMAP_OPENAI_FP_5M",
    ):
        monkeypatch.delenv(name, raising=False)


def test_limits_15m_5m(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_slim_env(monkeypatch)
    assert slim_limits_for_interval("15m") == (60, 11)
    assert slim_limits_for_interval("5m") == (60, 16)
    assert slim_limits_for_interval("1h") is None


def test_slim_trims_candles_and_orderflow(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_slim_env(monkeypatch)
    data = {
        "symbol": "X",
        "interval": "15m",
        "getcandlehistory": [{"i": "15m", "t": i} for i in range(100, 0, -1)],
        "getcandlehistorycvd": [{"i": "15m", "t": i} for i in range(100, 0, -1)],
        "getorderflowhistory": [{"i": "15m", "t": i} for i in range(50, 0, -1)],
        "getindicatorsvwap": [{"i": "15m", "t": 100, "data": {}}],
    }
    out = slim_coinmap_export_for_openai(data)
    assert len(out["getcandlehistory"]) == 60
    assert len(out["getcandlehistorycvd"]) == 60
    assert len(out["getorderflowhistory"]) == 11
    assert out["getindicatorsvwap"][0]["t"] == 100


def test_interval_from_filename(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_slim_env(monkeypatch)
    p = Path("20260330_101242_coinmap_XAUUSD_15m.json")
    data = {"symbol": "XAUUSD", "getcandlehistory": [{"t": 1}]}
    out = slim_coinmap_export_for_openai(data, path=p)
    assert "interval" not in out or out.get("getcandlehistory") == [{"t": 1}]
    data2 = {"symbol": "XAUUSD", "interval": "15m", "getcandlehistory": [{"i": "15m", "t": i} for i in range(50)]}
    out2 = slim_coinmap_export_for_openai(data2, path=p)
    # Default cap is 60 bars; only 50 supplied → all kept
    assert len(out2["getcandlehistory"]) == 50
