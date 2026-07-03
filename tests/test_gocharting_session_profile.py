from __future__ import annotations

import json
from pathlib import Path

import pytest

from automation_tool.gocharting_session_profile import (
    VALUE_AREA_EXPANSION_GOCHARTING_ASYMMETRIC,
    VALUE_AREA_EXPANSION_SYMMETRIC,
    aggregate_session_footprint_rows,
    build_session_profile,
    compute_session_vwap,
    compute_value_area_profile,
    enrich_footprint_document_with_session_profiles,
    split_candles_into_eth_sessions,
)


def _level(price: float, buy: int, sell: int) -> dict:
    return {"price": price, "buy": buy, "sell": sell}


def test_compute_value_area_profile_gocharing_expansion() -> None:
    # High → low: POC at 99 (50 vol); target 70% of 100 = 70
    rows = [(100.0, 10), (99.0, 50), (98.0, 20), (97.0, 15), (96.0, 5)]
    profile = compute_value_area_profile(
        rows,
        value_area_pct=0.7,
        expansion_mode=VALUE_AREA_EXPANSION_GOCHARTING_ASYMMETRIC,
    )
    assert profile["poc"] == 99.0
    assert profile["vah"] == 99.0
    assert profile["val"] == 97.0
    assert profile["value_area_volume"] >= 70
    assert profile["total_volume"] == 100


def test_compute_value_area_profile_symmetric_matches_chart() -> None:
    rows = [(100.0, 10), (99.0, 50), (98.0, 20), (97.0, 15), (96.0, 5)]
    profile = compute_value_area_profile(
        rows,
        value_area_pct=0.7,
        expansion_mode=VALUE_AREA_EXPANSION_SYMMETRIC,
    )
    assert profile["poc"] == 99.0
    assert profile["vah"] == 99.0
    assert profile["val"] == 98.0


def test_compute_session_vwap_typical_price_weighted() -> None:
    candles = [
        {
            "ohlc": {"high": 103.0, "low": 100.0, "close": 101.0},
            "bar_flow": {"volume": 300},
        },
        {
            "ohlc": {"high": 102.0, "low": 99.0, "close": 100.0},
            "bar_flow": {"volume": 200},
        },
    ]
    out = compute_session_vwap(candles, price_precision=1)
    ap1 = (103.0 + 100.0 + 101.0) / 3.0
    ap2 = (102.0 + 99.0 + 100.0) / 3.0
    expected = round((ap1 * 300 + ap2 * 200) / 500, 1)
    assert out["vwap"] == expected
    assert out["session_volume"] == 500
    assert out["vwap_candles"] == 2


def test_build_session_profile_vwap_only_when_value_area_disabled() -> None:
    candles = [
        {
            "time_gmt7": "Wed Jul 1 2026 05:00:00 GMT+0700",
            "date": "2026-06-30T18:00:00-04:00",
            "ohlc": {"high": 100.0, "low": 98.0, "close": 99.0},
            "bar_flow": {"volume": 100, "high": 100.0, "low": 98.0, "close": 99.0},
            "footprint": [_level(100.0, 5, 5), _level(99.0, 25, 25)],
        },
        {
            "time_gmt7": "Wed Jul 1 2026 05:05:00 GMT+0700",
            "date": "2026-06-30T18:05:00-04:00",
            "ohlc": {"high": 99.0, "low": 97.0, "close": 98.0},
            "bar_flow": {"volume": 100, "high": 99.0, "low": 97.0, "close": 98.0},
            "footprint": [_level(99.0, 10, 10), _level(98.0, 10, 10)],
        },
    ]
    profile = build_session_profile(candles, value_area_enabled=False)
    assert "poc" not in profile
    assert "vah" not in profile
    assert "val" not in profile
    assert profile["vwap"] == compute_session_vwap(candles, price_precision=1)["vwap"]

    cfg = {"footprint_ws": {"session_profile": {"enabled": True, "value_area_enabled": False}}}
    doc = {"candles": candles}
    out = enrich_footprint_document_with_session_profiles(doc, cfg=cfg)
    sp = out["candles"][0]["session_profile"]
    assert "poc" not in sp
    assert sp["vwap"] == profile["vwap"]


def test_build_session_profile_includes_session_vwap() -> None:
    candles = [
        {
            "time_gmt7": "Wed Jul 1 2026 05:00:00 GMT+0700",
            "date": "2026-06-30T18:00:00-04:00",
            "ohlc": {"high": 100.0, "low": 98.0, "close": 99.0},
            "bar_flow": {"volume": 100, "high": 100.0, "low": 98.0, "close": 99.0},
            "footprint": [_level(100.0, 5, 5), _level(99.0, 25, 25)],
        },
        {
            "time_gmt7": "Wed Jul 1 2026 05:05:00 GMT+0700",
            "date": "2026-06-30T18:05:00-04:00",
            "ohlc": {"high": 99.0, "low": 97.0, "close": 98.0},
            "bar_flow": {"volume": 100, "high": 99.0, "low": 97.0, "close": 98.0},
            "footprint": [_level(99.0, 10, 10), _level(98.0, 10, 10)],
        },
    ]
    profile = build_session_profile(candles)
    rows = aggregate_session_footprint_rows(candles)
    assert sum(v for _, v in rows) == 100
    assert profile["poc"] == 99.0
    assert profile["candles"] == 2
    assert profile["vwap"] == compute_session_vwap(candles, price_precision=1)["vwap"]


def test_apply_running_session_vwap_to_candles() -> None:
    from automation_tool.gocharting_session_profile import apply_running_session_vwap_to_candles

    candles = [
        {
            "time_gmt7": "Wed Jul 1 2026 05:00:00 GMT+0700",
            "ohlc": {"high": 100.0, "low": 98.0, "close": 99.0},
            "bar_flow": {"volume": 100, "delta": 0},
        },
        {
            "time_gmt7": "Wed Jul 1 2026 05:05:00 GMT+0700",
            "ohlc": {"high": 99.0, "low": 97.0, "close": 98.0},
            "bar_flow": {"volume": 100, "delta": 0},
        },
    ]
    apply_running_session_vwap_to_candles(candles, price_precision=1)
    assert candles[0]["bar_flow"]["vwap"] == pytest.approx(99.0)
    assert candles[1]["bar_flow"]["vwap"] == pytest.approx(98.5)
    assert candles[1]["bar_flow"]["vwap"] == compute_session_vwap(candles, price_precision=1)["vwap"]


def test_split_eth_sessions_at_0500() -> None:
    candles = [
        {"time_gmt7": "Wed Jul 1 2026 05:00:00 GMT+0700", "date": "2026-06-30T18:00:00-04:00"},
        {"time_gmt7": "Thu Jul 2 2026 03:55:00 GMT+0700", "date": "2026-07-01T16:55:00-04:00"},
        {"time_gmt7": "Thu Jul 2 2026 05:00:00 GMT+0700", "date": "2026-07-01T18:00:00-04:00"},
    ]
    sessions = split_candles_into_eth_sessions(candles)
    assert len(sessions) == 2
    assert len(sessions[0]) == 2
    assert len(sessions[1]) == 1


@pytest.mark.skipif(
    not (
        Path(__file__).resolve().parents[1]
        / "data"
        / "network_sniff"
        / "footprint_ws_5m_2026-07-01.json"
    ).is_file(),
    reason="WS fixture missing",
)
def test_session_profile_on_real_ws_fixture() -> None:
    ws_path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "network_sniff"
        / "footprint_ws_5m_2026-07-01.json"
    )
    from automation_tool.gocharting_ws_decode import enrich_footprint_document_with_ws_bar_flow

    doc = json.loads(ws_path.read_text(encoding="utf-8"))
    cfg = {"footprint_ws": {"session_profile": {"enabled": True, "value_area_pct": 0.7}}}
    out = enrich_footprint_document_with_ws_bar_flow(doc, cfg=cfg)
    profiles = out.get("session_profiles") or []
    assert profiles
    first = profiles[0]
    assert first["poc"] > 0
    assert first["vah"] >= first["poc"] >= first["val"]
    if first.get("vwap") is not None:
        assert first["vwap"] > 0
        assert out["candles"][0].get("session_profile", {}).get("vwap") == first["vwap"]
    assert out["candles"][0].get("session_profile", {}).get("poc") == first["poc"]
