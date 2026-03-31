"""Coinmap JSON slimming for OpenAI embedding."""

from pathlib import Path

from automation_tool.coinmap_openai_slim import (
    slim_coinmap_export_for_openai,
    slim_limits_for_interval,
)


def test_limits_15m_5m():
    assert slim_limits_for_interval("15m") == (30, 11)
    assert slim_limits_for_interval("5m") == (35, 16)
    assert slim_limits_for_interval("1h") is None


def test_slim_trims_candles_and_orderflow():
    data = {
        "symbol": "X",
        "interval": "15m",
        "getcandlehistory": [{"i": "15m", "t": i} for i in range(100, 0, -1)],
        "getorderflowhistory": [{"i": "15m", "t": i} for i in range(50, 0, -1)],
        "getindicatorsvwap": [{"i": "15m", "t": 100, "data": {}}],
    }
    out = slim_coinmap_export_for_openai(data)
    assert len(out["getcandlehistory"]) == 30
    assert len(out["getorderflowhistory"]) == 11
    assert out["getindicatorsvwap"][0]["t"] == 100


def test_interval_from_filename():
    p = Path("20260330_101242_coinmap_XAUUSD_15m.json")
    data = {"symbol": "XAUUSD", "getcandlehistory": [{"t": 1}]}
    out = slim_coinmap_export_for_openai(data, path=p)
    assert "interval" not in out or out.get("getcandlehistory") == [{"t": 1}]
    data2 = {"symbol": "XAUUSD", "interval": "15m", "getcandlehistory": [{"i": "15m", "t": i} for i in range(50)]}
    out2 = slim_coinmap_export_for_openai(data2, path=p)
    assert len(out2["getcandlehistory"]) == 30
