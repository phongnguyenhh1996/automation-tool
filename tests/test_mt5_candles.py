from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from automation_tool.images import ordered_chart_openai_payloads
from automation_tool.mt5_candles import (
    MT5_CANDLES_TIMEZONE,
    export_mt5_spot_candles_json,
    fetch_mt5_spot_candles_payload,
    footprint_candle_time_bounds,
    mt5_footprint_range_utc_bounds,
    mt5_spot_candles_json_stem,
    resolve_mt5_broker_symbol,
)


def test_mt5_spot_candles_json_stem() -> None:
    assert mt5_spot_candles_json_stem("20260618_120000", "XAUUSD", "5m") == (
        "20260618_120000_mt5_XAUUSD_5m"
    )


def test_resolve_mt5_broker_symbol() -> None:
    assert resolve_mt5_broker_symbol("XAUUSD") == "XAUUSDm"
    assert resolve_mt5_broker_symbol("XAUUSD", account_symbol_map={"XAUUSD": "XAUUSD"}) == "XAUUSD"


def test_footprint_candle_time_bounds() -> None:
    candles = [
        {"time_gmt7": "Tue Jun 30 2026 22:00:00 GMT+0700"},
        {"date": "2026-07-01T05:00:00+07:00"},
    ]
    bounds = footprint_candle_time_bounds(candles)
    assert bounds is not None
    lo, hi = bounds
    assert lo == datetime(2026, 6, 30, 22, 0, 0)
    assert hi == datetime(2026, 7, 1, 5, 0, 0)


def test_mt5_footprint_range_utc_bounds_padding_10_bars() -> None:
    lo = datetime(2026, 7, 1, 22, 0, 0)
    hi = datetime(2026, 7, 2, 5, 0, 0)
    from_utc, to_utc, range_from, range_to = mt5_footprint_range_utc_bounds(
        lo,
        hi,
        interval="5m",
        padding_bars=10,
    )
    assert range_from == "2026-07-01T21:10:00+07:00"
    assert range_to == "2026-07-02T05:55:00+07:00"
    assert from_utc.isoformat().startswith("2026-07-01T14:10:00")
    assert to_utc.isoformat().startswith("2026-07-01T22:55:00")


def test_fetch_mt5_spot_candles_payload_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    rates = np.array(
        [
            (1_700_000_000, 2650.0, 2651.0, 2649.0, 2650.5, 100, 0, 0),
            (1_700_000_300, 2650.5, 2652.0, 2650.0, 2651.0, 120, 0, 0),
        ],
        dtype=[
            ("time", "i8"),
            ("open", "f8"),
            ("high", "f8"),
            ("low", "f8"),
            ("close", "f8"),
            ("tick_volume", "i8"),
            ("spread", "i4"),
            ("real_volume", "i8"),
        ],
    )
    mt5 = MagicMock()
    mt5.TIMEFRAME_M5 = 5
    mt5.symbol_select.return_value = True
    mt5.copy_rates_from_pos.return_value = rates

    monkeypatch.setattr("automation_tool.mt5_candles._load_mt5", lambda: mt5)
    monkeypatch.setattr(
        "automation_tool.mt5_candles._connect_mt5_for_candles",
        lambda _account: (mt5, False),
    )
    monkeypatch.setattr("automation_tool.mt5_candles.load_mt5_accounts_for_cli", lambda _p: None)

    payload = fetch_mt5_spot_candles_payload(logic_symbol="XAUUSD", interval="5m", count=2)
    assert payload is not None
    assert payload["source"] == "mt5"
    assert payload["symbol"] == "XAUUSD"
    assert payload["broker_symbol"] == "XAUUSDm"
    assert payload["interval"] == "5m"
    assert payload["timezone"] == MT5_CANDLES_TIMEZONE
    assert payload["n_bars"] == 2
    assert payload["bars"][0]["close"] == 2650.5
    assert payload["bars"][0]["t"] == "2023-11-15T05:13:20+07:00"
    assert payload["bars"][1]["t"] == "2023-11-15T05:18:20+07:00"
    assert payload["generated_at"].endswith("+07:00")


def test_fetch_mt5_spot_candles_range_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    rates = np.array(
        [
            (1_718_000_000, 2650.0, 2651.0, 2649.0, 2650.5, 100, 0, 0),
            (1_718_000_300, 2650.5, 2652.0, 2650.0, 2651.0, 120, 0, 0),
        ],
        dtype=[
            ("time", "i8"),
            ("open", "f8"),
            ("high", "f8"),
            ("low", "f8"),
            ("close", "f8"),
            ("tick_volume", "i8"),
            ("spread", "i4"),
            ("real_volume", "i8"),
        ],
    )
    mt5 = MagicMock()
    mt5.TIMEFRAME_M5 = 5
    mt5.symbol_select.return_value = True
    mt5.copy_rates_range.return_value = rates
    mt5.copy_rates_from_pos.return_value = None

    monkeypatch.setattr("automation_tool.mt5_candles._load_mt5", lambda: mt5)
    monkeypatch.setattr(
        "automation_tool.mt5_candles._connect_mt5_for_candles",
        lambda _account: (mt5, False),
    )
    monkeypatch.setattr("automation_tool.mt5_candles.load_mt5_accounts_for_cli", lambda _p: None)

    footprint_candles = [
        {"time_gmt7": "Wed Jul 1 2026 22:00:00 GMT+0700"},
        {"time_gmt7": "Wed Jul 1 2026 22:05:00 GMT+0700"},
    ]
    payload = fetch_mt5_spot_candles_payload(
        logic_symbol="XAUUSD",
        interval="5m",
        count=50,
        footprint_candles=footprint_candles,
    )
    assert payload is not None
    assert payload["fetch_mode"] == "range"
    assert payload["n_bars"] == 2
    assert mt5.copy_rates_range.called
    assert not mt5.copy_rates_from_pos.called


def test_export_mt5_spot_candles_json_writes_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "automation_tool.mt5_candles.fetch_mt5_spot_candles_payload",
        lambda **kwargs: {
            "source": "mt5",
            "symbol": "XAUUSD",
            "broker_symbol": "XAUUSDm",
            "interval": "5m",
            "n_bars": 1,
            "n_bars_requested": 50,
            "bars": [{"t": 1, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "tick_volume": 10}],
        },
    )
    out = export_mt5_spot_candles_json(
        charts_dir=tmp_path,
        stamp="20260618_120000",
        logic_symbol="XAUUSD",
    )
    assert out is not None
    assert out.name == "20260618_120000_mt5_XAUUSD_5m.json"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["n_bars"] == 1


def test_ordered_payloads_append_mt5_after_gocharting(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    stamp = "20260618_120000"
    (charts / f"{stamp}_gocharting_GC_5m.csv").write_text("h\n1", encoding="utf-8")
    (charts / f"{stamp}_mt5_XAUUSD_5m.json").write_text("{}", encoding="utf-8")
    (charts / ".main_chart_symbol").write_text("XAUUSD\n", encoding="utf-8")

    payloads = ordered_chart_openai_payloads(charts, stamp=stamp)
    kinds = [k for k, _ in payloads]
    assert ("json", charts / f"{stamp}_mt5_XAUUSD_5m.json") in payloads
    assert kinds[-1] == "json"
    assert payloads[-1][1].name.endswith("_mt5_XAUUSD_5m.json")
