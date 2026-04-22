"""Tests for Coinmap multi-timeframe merge (``market_merge_single``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from automation_tool.coinmap_merged import (
    validate_coinmap_merged_payload,
    write_coinmap_merged_json,
)
from automation_tool.market_merge_single import (
    build_merged_analysis_from_files,
    build_raw_bundle,
    build_session_master,
    compute_footprint_summary,
    _smallest_timeframe,
)


def _minimal_bar(t: int) -> dict:
    return {
        "s": "X",
        "i": "5m",
        "t": t,
        "ct": t + 60_000,
        "o": 1.0,
        "h": 2.0,
        "l": 0.5,
        "c": 1.5,
        "v": 100,
        "bv": 40,
        "sv": 60,
        "d": -20,
        "dMax": 10,
        "dMin": -30,
        "n": 5,
    }


def _of_bar(t: int) -> dict:
    return {
        "t": t,
        "aggs": [
            {"tp": 1.0, "v": 10, "bv": 3, "sv": 7},
            {"tp": 1.5, "v": 20, "bv": 8, "sv": 12},
        ],
    }


def test_build_raw_and_master_two_intervals(tmp_path: Path) -> None:
    t0 = 1_770_000_000_000
    p15 = tmp_path / "a_coinmap_X_15m.json"
    p5 = tmp_path / "a_coinmap_X_5m.json"
    base = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "stamp": "a",
        "symbol": "XAUUSD",
        "watchlist_category": "f",
    }
    p15.write_text(
        json.dumps(
            {
                **base,
                "interval": "15m",
                "getcandlehistory": [_minimal_bar(t0), _minimal_bar(t0 - 900_000)],
                "getorderflowhistory": [_of_bar(t0), _of_bar(t0 - 900_000)],
                "getindicatorsvwap": [
                    {
                        "t": t0,
                        "data": {
                            "vwap": 1.2,
                            "sd": 0.1,
                            "topBand1": 2.0,
                            "botBand1": 0.0,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    t1 = 1_770_000_000_000
    p5.write_text(
        json.dumps(
            {
                **base,
                "interval": "5m",
                "getcandlehistory": [_minimal_bar(t1)],
                "getorderflowhistory": [_of_bar(t1)],
            }
        ),
        encoding="utf-8",
    )
    raw = build_raw_bundle([p15, p5])
    assert "15m" in raw["timeframes"] and "5m" in raw["timeframes"]
    assert _smallest_timeframe(raw["timeframes"]) == "5m"
    m = build_session_master(raw)
    assert m["source"] == "coinmap_merged"
    assert "15m" in m["frames"] and "5m" in m["frames"]
    assert "session_profile" in m
    assert m["session_profile"].get("interval") == "5m"
    assert m.get("session_start") is not None
    assert m.get("session_timezone") == "Asia/Ho_Chi_Minh"
    for iv in ("15m", "5m"):
        assert "session_profile" not in m["frames"][iv]


def test_session_start_explicit_override(tmp_path: Path) -> None:
    f = tmp_path / "one.json"
    f.write_text(
        json.dumps(
            {
                "generated_at": "2026-06-01T10:00:00+00:00",
                "symbol": "X",
                "interval": "5m",
                "getcandlehistory": [],
                "getorderflowhistory": [],
            }
        ),
        encoding="utf-8",
    )
    raw = build_raw_bundle([f], session_start="2020-01-01T05:00:00+07:00")
    assert raw["session_start"] == "2020-01-01T05:00:00+07:00"


def test_footprint_summary_aggregates() -> None:
    s = compute_footprint_summary(
        [{"tp": 100.0, "v": 50, "bv": 30, "sv": 20}, {"tp": 101, "v": 10, "bv": 2, "sv": 8}]
    )
    assert s["price_levels"] == 2
    assert s["poc_candle"] == 100.0
    assert s["delta_from_footprint"] == 4


def test_validate_merged_dxy_and_main(tmp_path: Path) -> None:
    t0 = 1_770_000_000_000
    dxy = tmp_path / "s_coinmap_DXY_15m.json"
    dxy.write_text(
        json.dumps(
            {
                "symbol": "DXY",
                "interval": "15m",
                "getcandlehistory": [_minimal_bar(t0)],
                "getorderflowhistory": [_of_bar(t0)],
            }
        ),
        encoding="utf-8",
    )
    p_out = write_coinmap_merged_json(
        tmp_path, "s", raw_paths=[dxy], out_path=tmp_path / "s_merged_dxy.json"
    )
    data = json.loads(p_out.read_text(encoding="utf-8"))
    ok, rea = validate_coinmap_merged_payload(data)
    assert ok, rea
    assert data["source"] == "coinmap_merged"
    assert list((data.get("frames") or {}).keys()) == ["15m"]


def test_merged_from_files_end_to_end(tmp_path: Path) -> None:
    t0 = 1_770_000_000_000
    for iv in ("15m", "5m"):
        f = tmp_path / f"batch_coinmap_T_{iv}.json"
        f.write_text(
            json.dumps(
                {
                    "symbol": "T",
                    "interval": iv,
                    "getcandlehistory": [_minimal_bar(t0)],
                    "getorderflowhistory": [_of_bar(t0)],
                }
            ),
            encoding="utf-8",
        )
    out = build_merged_analysis_from_files(
        [tmp_path / "batch_coinmap_T_15m.json", tmp_path / "batch_coinmap_T_5m.json"]
    )
    assert out["source"] == "coinmap_merged"
    assert out.get("session_start")
    assert "session_profile" in out and isinstance(out["session_profile"], dict)
    fr = out["frames"]
    assert "15m" in fr and "5m" in fr
    for _, block in fr.items():
        assert "recent_candles" in block
        assert "candles" not in block  # analysis payload is compact
        assert "session_profile" not in block