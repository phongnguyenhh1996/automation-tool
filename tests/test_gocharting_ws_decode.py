from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from automation_tool.gocharting_ws_decode import (
    FOOTPRINT_EXPORT_FORMAT_COMBINED,
    FOOTPRINT_EXPORT_FORMAT_RAW,
    decode_ws_footprint_frame,
    decode_ws_frames_dir,
    decode_ws_frames_merged,
    decode_ws_ohlc_frame,
    drop_forming_footprint_candle,
    parse_proto_candle_datetime,
    parse_ws_binary_envelope,
    pick_best_footprint_document,
    proto_candle_time_key,
    trim_footprint_document,
)

_FIXTURE_DIR = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "network_sniff"
    / "ws_frames_20260625_113253"
)
_FOOTPRINT_FRAME = _FIXTURE_DIR / "0023_recv.bin"


@pytest.mark.skipif(not _FOOTPRINT_FRAME.is_file(), reason="sniff fixture missing")
def test_parse_ws_binary_envelope_footprint() -> None:
    raw = _FOOTPRINT_FRAME.read_bytes()
    parsed = parse_ws_binary_envelope(raw)
    assert parsed is not None
    type_str, payload = parsed
    assert type_str.startswith("FOOTPRINT/V2")
    assert len(payload) > 1000


@pytest.mark.skipif(not _FOOTPRINT_FRAME.is_file(), reason="sniff fixture missing")
def test_decode_ws_footprint_frame() -> None:
    raw = _FOOTPRINT_FRAME.read_bytes()
    doc = decode_ws_footprint_frame(raw)
    assert doc is not None
    assert doc["symbol"] == "COMEX:GC1!"
    assert doc["timeframe"] == "5m"
    assert doc["type"] == "Bid/Ask Footprint"
    assert len(doc["candles"]) >= 50
    first = doc["candles"][0]
    assert "time" in first
    assert "GMT+0700" in first["time"]
    assert isinstance(first["price_levels"], list)
    assert first["price_levels"][0]["bid"] >= 0
    assert first["price_levels"][0]["ask"] >= 0


@pytest.mark.skipif(not _FOOTPRINT_FRAME.is_file(), reason="sniff fixture missing")
def test_decode_ws_footprint_frame_raw() -> None:
    raw = _FOOTPRINT_FRAME.read_bytes()
    doc = decode_ws_footprint_frame(raw, export_format=FOOTPRINT_EXPORT_FORMAT_RAW)
    assert doc is not None
    assert doc["symbol"] == "COMEX:GC1!"
    assert doc["request"]["interval"] == "5m"
    assert doc["ws_type"].startswith("FOOTPRINT/V2")
    assert "fp_day" in doc
    assert doc["fp_day"]["price_precision"] >= 0
    assert len(doc["candles"]) >= 50
    first = doc["candles"][0]
    assert first["date"]
    assert first["time_gmt7"]
    assert "total_buy" in first["ending_summary"]
    assert "max_delta" in first["ending_summary"]
    assert first["footprint"]
    level0 = first["footprint"][0]
    assert "level" in level0
    assert "price" in level0
    assert "buy" in level0
    assert "sell" in level0


@pytest.mark.skipif(not _FIXTURE_DIR.is_dir(), reason="sniff fixture missing")
def test_decode_ws_frames_dir_raw() -> None:
    docs = decode_ws_frames_dir(_FIXTURE_DIR, export_format=FOOTPRINT_EXPORT_FORMAT_RAW)
    assert docs
    best = pick_best_footprint_document(docs)
    assert best is not None
    assert best["request"]["interval"] == "5m"
    assert len(best["candles"]) >= 50


@pytest.mark.skipif(not _FIXTURE_DIR.is_dir(), reason="sniff fixture missing")
def test_decode_ws_ohlc_frame() -> None:
    raw = (_FIXTURE_DIR / "0019_recv.bin").read_bytes()
    doc = decode_ws_ohlc_frame(raw)
    assert doc is not None
    assert doc["ws_type"].startswith("TS/V2")
    assert len(doc["candles"]) >= 50
    target = next(
        c
        for c in doc["candles"]
        if c.get("time_gmt7") == "Thu Jun 25 2026 05:00:00 GMT+0700"
    )
    assert target["ohlc"]["open"] == 4019.0
    assert target["ohlc"]["high"] == 4024.9
    assert target["ohlc"]["low"] == 4018.4
    assert target["ohlc"]["close"] == 4023.6


@pytest.mark.skipif(not _FIXTURE_DIR.is_dir(), reason="sniff fixture missing")
def test_merge_footprint_with_ohlc_fixture() -> None:
    merged = decode_ws_frames_merged(_FIXTURE_DIR, export_format=FOOTPRINT_EXPORT_FORMAT_COMBINED)
    assert merged is not None
    assert len(merged["candles"]) >= 50
    assert merged["ohlc_matched"] == len(merged["candles"])
    first = merged["candles"][0]
    assert first["ohlc"] is not None
    assert first["ohlc"]["open"] == 4019.0
    assert first["footprint"]
    assert "ending_summary" not in first
    assert "max" not in first
    assert "min" not in first
    assert "is_complete" not in merged


@pytest.mark.skipif(not _FIXTURE_DIR.is_dir(), reason="sniff fixture missing")
def test_decode_ws_frames_dir_picks_footprint() -> None:
    docs = decode_ws_frames_dir(_FIXTURE_DIR)
    assert docs
    best = pick_best_footprint_document(docs)
    assert best is not None
    assert best["timeframe"] == "5m"
    assert len(best["candles"]) >= 50


def test_proto_candle_time_key_converts_to_gmt7() -> None:
    dt = parse_proto_candle_datetime("2026-06-24T18:00:00-04:00")
    assert dt.hour == 18
    key = proto_candle_time_key("2026-06-24T18:00:00-04:00")
    assert "Jun 25 2026 05:00:00 GMT+0700" in key


def test_parse_ws_binary_envelope_rejects_json() -> None:
    assert parse_ws_binary_envelope(b'{"command":"PING"}') is None


def test_trim_footprint_document_keeps_newest_candles() -> None:
    doc = {
        "symbol": "COMEX:GC1!",
        "candles": [{"time_gmt7": f"t{i}"} for i in range(10)],
    }
    trimmed = trim_footprint_document(doc, max_candles=3)
    assert len(trimmed["candles"]) == 3
    assert trimmed["candles"][0]["time_gmt7"] == "t7"
    assert trimmed["candles"][-1]["time_gmt7"] == "t9"


def test_drop_forming_footprint_candle_removes_current_bar() -> None:
    doc = {
        "is_complete": False,
        "candles": [
            {"time_gmt7": "Thu Jun 25 2026 11:25:00 GMT+0700"},
            {"time_gmt7": "Thu Jun 25 2026 11:30:00 GMT+0700"},
        ],
    }
    now = datetime(2026, 6, 25, 11, 32)
    out = drop_forming_footprint_candle(doc, interval="5m", now=now)
    assert len(out["candles"]) == 1
    assert out["candles"][-1]["time_gmt7"] == "Thu Jun 25 2026 11:25:00 GMT+0700"


def test_drop_forming_footprint_candle_keeps_when_closed() -> None:
    doc = {
        "is_complete": False,
        "candles": [
            {"time_gmt7": "Thu Jun 25 2026 11:25:00 GMT+0700"},
            {"time_gmt7": "Thu Jun 25 2026 11:30:00 GMT+0700"},
        ],
    }
    now = datetime(2026, 6, 25, 11, 36)
    out = drop_forming_footprint_candle(doc, interval="5m", now=now)
    assert len(out["candles"]) == 2
    assert out["candles"][-1]["time_gmt7"] == "Thu Jun 25 2026 11:30:00 GMT+0700"


def test_drop_forming_footprint_candle_fallback_is_complete() -> None:
    doc = {
        "is_complete": False,
        "candles": [
            {"time_gmt7": "closed"},
            {"time_gmt7": "unparsable"},
        ],
    }
    out = drop_forming_footprint_candle(doc, interval="5m", now=datetime(2026, 6, 25, 11, 32))
    assert len(out["candles"]) == 1
    assert out["candles"][-1]["time_gmt7"] == "closed"


def test_drop_forming_footprint_candle_noop_single_candle() -> None:
    doc = {
        "is_complete": False,
        "candles": [{"time_gmt7": "Thu Jun 25 2026 11:30:00 GMT+0700"}],
    }
    out = drop_forming_footprint_candle(doc, interval="5m", now=datetime(2026, 6, 25, 11, 32))
    assert out is doc


def test_slim_footprint_combined_document() -> None:
    from automation_tool.gocharting_ws_decode import slim_footprint_combined_document

    doc = {
        "is_complete": True,
        "candles": [
            {
                "time_gmt7": "t1",
                "ending_summary": {"high": "1"},
                "max": {"buy": {}},
                "min": {"sell": {}},
                "totals": {"buy": {}},
                "footprint": [],
            }
        ],
    }
    slim = slim_footprint_combined_document(doc)
    assert "is_complete" not in slim
    c0 = slim["candles"][0]
    assert "ending_summary" not in c0
    assert "max" not in c0
    assert "min" not in c0
    assert "totals" in c0


def test_mt5_bar_time_to_footprint_key() -> None:
    from automation_tool.gocharting_ws_decode import mt5_bar_time_to_footprint_key

    key = mt5_bar_time_to_footprint_key("2026-06-25T05:00:00+07:00")
    assert key == "Thu Jun 25 2026 05:00:00 GMT+0700"


def test_merge_footprint_with_mt5_spot() -> None:
    from automation_tool.gocharting_ws_decode import merge_footprint_with_mt5_spot

    time_key = "Thu Jun 25 2026 05:00:00 GMT+0700"
    doc = {
        "candles": [
            {"time_gmt7": time_key, "ohlc": {"open": 4019.0}},
            {"time_gmt7": "Thu Jun 25 2026 05:05:00 GMT+0700"},
        ]
    }
    mt5_payload = {
        "symbol": "XAUUSD",
        "broker_symbol": "XAUUSDm",
        "interval": "5m",
        "bars": [
            {
                "t": "2026-06-25T05:00:00+07:00",
                "open": 4020.1,
                "high": 4025.0,
                "low": 4018.0,
                "close": 4023.5,
                "tick_volume": 100,
            }
        ],
    }
    merged = merge_footprint_with_mt5_spot(doc, mt5_payload)
    assert merged["candles"][0]["mt5_spot_ohlc"]["open"] == 4020.1
    assert merged["candles"][1]["mt5_spot_ohlc"] is None
    assert merged["mt5_spot"]["matched"] == 1
