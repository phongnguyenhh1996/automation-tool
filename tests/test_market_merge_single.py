"""Tests for Coinmap multi-timeframe merge (``market_merge_single``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from automation_tool.coinmap_merged import (
    validate_coinmap_merged_payload,
    write_coinmap_merged_json,
    write_openai_coinmap_merged_from_raw_export,
)
from datetime import datetime
from zoneinfo import ZoneInfo

from automation_tool.market_merge_single import (
    AnalysisPayloadOptions,
    build_analysis_payload,
    build_merged_analysis_from_files,
    build_raw_bundle,
    build_session_master,
    compute_footprint_summary,
    _session_profile_for_tf,
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


def test_validate_merged_5m_only_single_raw(tmp_path: Path) -> None:
    """INTRADAY-style: one M5 export → merged payload with only ``5m`` frame."""
    t0 = 1_770_000_000_000
    raw = tmp_path / "touch_coinmap_XAUUSD_5m.json"
    raw.write_text(
        json.dumps(
            {
                "symbol": "XAUUSD",
                "interval": "5m",
                "getcandlehistory": [_minimal_bar(t0)],
                "getorderflowhistory": [_of_bar(t0)],
            }
        ),
        encoding="utf-8",
    )
    out = write_openai_coinmap_merged_from_raw_export(raw)
    assert out.name == "touch_coinmap_XAUUSD_5m_openai_coinmap_merged.json"
    data = json.loads(out.read_text(encoding="utf-8"))
    ok, rea = validate_coinmap_merged_payload(data)
    assert ok, rea
    assert list((data.get("frames") or {}).keys()) == ["5m"]


def test_master_session_profile_histogram_volume_sum_matches_total(tmp_path: Path) -> None:
    t0 = 1_770_000_000_000
    f = tmp_path / "hist_sum.json"
    f.write_text(
        json.dumps(
            {
                "generated_at": "2026-01-01T00:00:00+00:00",
                "symbol": "Z",
                "interval": "5m",
                "getcandlehistory": [_minimal_bar(t0)],
                "getorderflowhistory": [_of_bar(t0)],
            }
        ),
        encoding="utf-8",
    )
    raw = build_raw_bundle([f])
    m = build_session_master(raw, filter_session_profile_by_session_start=False)
    sp = m["session_profile"]
    hist = sp["histogram"]
    summed = sum(int(x["volume"]) for x in hist)
    assert sp["total_volume"] == summed
    assert sp["histogram_volume_sum"] == summed
    assert sp["histogram_truncated"] is False


def test_analysis_payload_truncated_histogram_meta() -> None:
    candles = [
        {
            "t": 1,
            "footprint": [
                {"price": float(i), "volume": 10, "buy_volume": 5, "sell_volume": 5}
                for i in range(130)
            ],
        }
    ]
    sp = _session_profile_for_tf("5m", candles)
    assert sp["histogram_truncated"] is False
    assert sp["histogram_volume_sum"] == sp["total_volume"] == 1300
    master = {"session_profile": sp, "frames": {}, "symbol": "T"}
    out = build_analysis_payload(
        master, options=AnalysisPayloadOptions(histogram_max=50)
    )
    sp2 = out["session_profile"]
    assert sp2["histogram_truncated"] is True
    assert sp2["poc"] == sp["poc"]
    assert sp2["total_volume"] == 1300
    assert float(sp2["histogram_volume_sum"]) < 1300
    assert len(sp2["histogram"]) < 130


def test_session_start_filter_changes_session_profile(tmp_path: Path) -> None:
    anchor = datetime(2026, 6, 15, 5, 0, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    ms = int(anchor.timestamp() * 1000)
    t_early = ms - 600_000
    t_late = ms + 600_000
    f = tmp_path / "sess_filter.json"
    f.write_text(
        json.dumps(
            {
                "generated_at": "2026-06-15T08:00:00+00:00",
                "symbol": "Z2",
                "interval": "5m",
                "getcandlehistory": [
                    {
                        **_minimal_bar(t_early),
                        "o": 1.0,
                        "h": 1.0,
                        "l": 1.0,
                        "c": 1.0,
                        "v": 999,
                    },
                    {
                        **_minimal_bar(t_late),
                        "o": 2.0,
                        "h": 2.0,
                        "l": 2.0,
                        "c": 2.0,
                        "v": 50,
                    },
                ],
                "getorderflowhistory": [
                    {"t": t_early, "aggs": [{"tp": 100.0, "v": 999, "bv": 500, "sv": 499}]},
                    {"t": t_late, "aggs": [{"tp": 200.0, "v": 50, "bv": 25, "sv": 25}]},
                ],
            }
        ),
        encoding="utf-8",
    )
    raw = build_raw_bundle(
        [f], session_start="2026-06-15T05:00:00+07:00", session_timezone="Asia/Ho_Chi_Minh"
    )
    m_filtered = build_session_master(raw, filter_session_profile_by_session_start=True)
    m_all = build_session_master(raw, filter_session_profile_by_session_start=False)
    assert m_filtered["session_profile"]["poc"] == 200.0
    assert m_all["session_profile"]["poc"] == 100.0


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