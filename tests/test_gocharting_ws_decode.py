from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from automation_tool.gocharting_ws_decode import (
    FOOTPRINT_EXPORT_FORMAT_COMBINED,
    FOOTPRINT_EXPORT_FORMAT_RAW,
    bar_flow_from_ws_candle,
    decode_ws_footprint_frame,
    decode_ws_frames_dir,
    decode_ws_frames_merged,
    decode_ws_ohlc_frame,
    drop_forming_footprint_candle,
    enrich_footprint_document_with_ws_bar_flow,
    footprint_ws_extra_session_days,
    merge_footprint_ws_documents,
    parse_proto_candle_datetime,
    parse_ws_binary_envelope,
    pick_best_footprint_document,
    prior_session_dates_to_request,
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


@pytest.mark.skipif(not _FOOTPRINT_FRAME.is_file(), reason="sniff fixture missing")
def test_bar_flow_from_ws_candle_matches_ending_summary() -> None:
    raw = _FOOTPRINT_FRAME.read_bytes()
    doc = decode_ws_footprint_frame(raw, export_format=FOOTPRINT_EXPORT_FORMAT_RAW)
    assert doc is not None
    pp = int(doc["fp_day"]["price_precision"])
    first = doc["candles"][0]
    es = first["ending_summary"]
    bar = bar_flow_from_ws_candle(first, price_precision=pp)
    assert bar["delta"] == int(es["close_delta"])
    assert bar["max_delta"] == int(es["max_delta"])
    assert bar["buy_volume"] == int(es["total_buy"])
    assert bar["sell_volume"] == int(es["total_sell"])
    enriched = enrich_footprint_document_with_ws_bar_flow(doc)
    assert enriched["bar_flow_source"] == "ws"
    assert enriched["candles"][0]["bar_flow"]["cum_delta"] == bar["delta"]


@pytest.mark.skipif(not _FOOTPRINT_FRAME.is_file(), reason="sniff fixture missing")
def test_bar_flow_cum_delta_matches_csv_overlap() -> None:
    from automation_tool.gocharting_gc_spot_convert import _parse_gc_csv_bar_flow_rows

    raw = _FOOTPRINT_FRAME.read_bytes()
    doc = decode_ws_footprint_frame(raw, export_format=FOOTPRINT_EXPORT_FORMAT_RAW)
    assert doc is not None
    ws_path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "network_sniff"
        / "footprint_ws_5m_2026-07-01.json"
    )
    csv_path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "XAUUSD"
        / "charts"
        / "20260702_132817_gocharting_GC_5m.csv"
    )
    if not ws_path.is_file() or not csv_path.is_file():
        pytest.skip("overlap fixture missing")
    full_doc = json.loads(ws_path.read_text())
    enriched = enrich_footprint_document_with_ws_bar_flow(full_doc)
    csv_rows = _parse_gc_csv_bar_flow_rows(csv_path.read_text())
    ok = 0
    for candle in enriched["candles"]:
        tk = candle.get("time_gmt7")
        csv = csv_rows.get(tk)
        if not csv or csv.get("cum_delta") is None:
            continue
        assert candle["bar_flow"]["cum_delta"] == csv["cum_delta"]
        ok += 1
    assert ok >= 40


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
    assert isinstance(first.get("bar_flow"), dict)
    assert first["bar_flow"].get("delta") is not None
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


def test_slim_footprint_combined_for_openai() -> None:
    from automation_tool.gocharting_ws_decode import slim_footprint_combined_for_openai

    doc = {
        "ws_type": "FOOTPRINT/V2",
        "version": 1,
        "fp_day": {"tick_size": 1},
        "symbol": "COMEX:GC1!",
        "candles": [
            {
                "time_gmt7": "Thu Jun 25 2026 05:00:00 GMT+0700",
                "totals": {"buy": {"volume": "10"}},
                "footprint": [
                    {
                        "level": 40414,
                        "price": 4041.4,
                        "buy": {"trades": 5, "volume": 6},
                        "sell": {"trades": 6, "volume": 8},
                        "rl": 1.33,
                        "imbalance": "ASK",
                    }
                ],
            }
        ],
    }
    slim = slim_footprint_combined_for_openai(doc)
    assert "ws_type" not in slim
    assert "version" not in slim
    assert "fp_day" not in slim
    c0 = slim["candles"][0]
    assert "totals" not in c0
    lvl = c0["footprint"][0]
    assert lvl == {"price": 4041.4, "buy": 6, "sell": 8, "rl": 1.33, "imbalance": "ASK"}
    assert "level" not in lvl


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


def test_aggregate_footprint_levels_block_multiplier_3() -> None:
    from automation_tool.gocharting_ws_decode import aggregate_footprint_levels

    levels = [
        {"price": 4046.7, "buy": {"trades": 1, "volume": 1}, "sell": {"trades": 0, "volume": 0}},
        {"price": 4046.6, "buy": {"trades": 3, "volume": 3}, "sell": {"trades": 0, "volume": 0}},
        {"price": 4046.5, "buy": {"trades": 1, "volume": 3}, "sell": {"trades": 0, "volume": 0}},
        {"price": 4046.3, "buy": {"trades": 1, "volume": 1}, "sell": {"trades": 0, "volume": 0}},
    ]
    out = aggregate_footprint_levels(
        levels, block_size=0.3, price_precision=1, tick_size=0.1, block_multiplier=3
    )
    assert [row["price"] for row in out] == [4046.6, 4046.3]
    assert out[0]["buy"]["volume"] == 7
    assert out[0]["buy"]["trades"] == 5
    assert out[1]["buy"]["volume"] == 1


def test_aggregate_footprint_levels_multiplier_1_unchanged() -> None:
    from automation_tool.gocharting_ws_decode import aggregate_footprint_combined_document

    doc = {
        "fp_day": {"tick_size": 1, "display_tick_size": 1, "price_precision": 1},
        "candles": [
            {
                "footprint": [
                    {"price": 4046.7, "buy": {"volume": 1}, "sell": {"volume": 0}},
                    {"price": 4046.6, "buy": {"volume": 3}, "sell": {"volume": 0}},
                ]
            }
        ],
    }
    out = aggregate_footprint_combined_document(
        doc, cfg={"footprint_ws": {"block_multiplier": 1}}
    )
    assert out["candles"][0]["footprint"] == doc["candles"][0]["footprint"]


def test_aggregate_footprint_combined_document() -> None:
    from automation_tool.gocharting_ws_decode import aggregate_footprint_combined_document

    doc = {
        "fp_day": {"tick_size": 1, "display_tick_size": 1, "price_precision": 1},
        "candles": [
            {
                "time_gmt7": "Thu Jun 25 2026 10:05:00 GMT+0700",
                "footprint": [
                    {"price": 4024.8, "buy": {"trades": 1, "volume": 14}, "sell": {"volume": 0}},
                    {"price": 4024.7, "buy": {"trades": 2, "volume": 12}, "sell": {"volume": 1}},
                    {"price": 4024.6, "buy": {"trades": 3, "volume": 20}, "sell": {"volume": 4}},
                ],
            }
        ],
    }
    out = aggregate_footprint_combined_document(
        doc, cfg={"footprint_ws": {"tick_size": 0.1, "block_multiplier": 3}}
    )
    fp = out["candles"][0]["footprint"]
    assert [row["price"] for row in fp] == [4024.7]
    assert fp[0]["buy"]["volume"] == 46
    assert fp[0]["sell"]["volume"] == 5


def test_aggregate_footprint_levels_block_multiplier_2() -> None:
    from automation_tool.gocharting_ws_decode import aggregate_footprint_levels

    levels = [
        {"price": 4061.0, "buy": {"volume": 2}, "sell": {"volume": 0}},
        {"price": 4060.9, "buy": {"volume": 0}, "sell": {"volume": 0}},
        {"price": 4060.8, "buy": {"volume": 5}, "sell": {"volume": 0}},
        {"price": 4060.7, "buy": {"volume": 3}, "sell": {"volume": 1}},
    ]
    out = aggregate_footprint_levels(
        levels,
        block_size=0.2,
        price_precision=1,
        tick_size=0.1,
        block_multiplier=2,
    )
    by_price = {row["price"]: row for row in out}
    assert by_price[4060.9]["buy"]["volume"] == 2
    assert by_price[4060.9]["sell"]["volume"] == 0
    assert by_price[4060.7]["buy"]["volume"] == 8
    assert by_price[4060.7]["sell"]["volume"] == 1


def test_aggregate_footprint_combined_document_block_multiplier_4() -> None:
    from automation_tool.gocharting_ws_decode import aggregate_footprint_combined_document

    doc = {
        "fp_day": {"tick_size": 1, "display_tick_size": 1, "price_precision": 1},
        "candles": [
            {
                "time_gmt7": "Thu Jun 25 2026 10:05:00 GMT+0700",
                "footprint": [
                    {"price": 4060.8, "buy": {"volume": 1}, "sell": {"volume": 0}},
                    {"price": 4060.7, "buy": {"volume": 2}, "sell": {"volume": 0}},
                    {"price": 4060.6, "buy": {"volume": 3}, "sell": {"volume": 0}},
                    {"price": 4060.5, "buy": {"volume": 4}, "sell": {"volume": 0}},
                    {"price": 4060.4, "buy": {"volume": 10}, "sell": {"volume": 0}},
                ],
            }
        ],
    }
    out = aggregate_footprint_combined_document(
        doc, cfg={"footprint_ws": {"tick_size": 0.1, "block_multiplier": 4}}
    )
    fp = out["candles"][0]["footprint"]
    assert [row["price"] for row in fp] == [4060.6, 4060.2]
    assert fp[0]["buy"]["volume"] == 10
    assert fp[1]["buy"]["volume"] == 10


def test_snap_to_gocharting_chart_block_multiplier_4() -> None:
    from automation_tool.gocharting_footprint_ocr import snap_to_gocharting_chart_block

    snap = lambda p: snap_to_gocharting_chart_block(p, 0.4, tick_size=0.1, block_multiplier=4)
    assert snap(0.1) == 0.2
    assert snap(0.4) == 0.2
    assert snap(0.5) == 0.6
    assert snap(0.8) == 0.6
    assert snap(0.9) == 1.0
    assert snap(1.2) == 1.0
    assert snap(4060.8) == 4060.6
    assert snap(4060.9) == 4061.0


def test_aggregate_footprint_levels_block_multiplier_4() -> None:
    from automation_tool.gocharting_ws_decode import aggregate_footprint_levels

    levels = [
        {"price": 4060.8, "buy": {"volume": 1}, "sell": {"volume": 0}},
        {"price": 4060.7, "buy": {"volume": 2}, "sell": {"volume": 0}},
        {"price": 4060.6, "buy": {"volume": 3}, "sell": {"volume": 0}},
        {"price": 4060.5, "buy": {"volume": 4}, "sell": {"volume": 0}},
        {"price": 4060.4, "buy": {"volume": 10}, "sell": {"volume": 0}},
    ]
    out = aggregate_footprint_levels(
        levels,
        block_size=0.4,
        price_precision=1,
        tick_size=0.1,
        block_multiplier=4,
    )
    assert [row["price"] for row in out] == [4060.6, 4060.2]
    assert out[0]["buy"]["volume"] == 10
    assert out[1]["buy"]["volume"] == 10


def test_footprint_raw_document_to_bid_ask_block_multiplier(tmp_path: Path) -> None:
    from automation_tool.gocharting_ws_decode import footprint_raw_document_to_bid_ask

    raw = {
        "symbol": "COMEX:GC1!",
        "request": {"interval": "5m"},
        "fp_day": {"price_precision": 1},
        "candles": [
            {
                "time_gmt7": "Thu Jun 25 2026 05:00:00 GMT+0700",
                "footprint": [
                    {"price": 4060.8, "buy": {"volume": 1}, "sell": {"volume": 0}},
                    {"price": 4060.7, "buy": {"volume": 2}, "sell": {"volume": 0}},
                    {"price": 4060.6, "buy": {"volume": 3}, "sell": {"volume": 0}},
                    {"price": 4060.5, "buy": {"volume": 4}, "sell": {"volume": 0}},
                ],
            }
        ],
    }
    out = footprint_raw_document_to_bid_ask(
        raw, block_multiplier=4, tick_size=0.1, include_price=True
    )
    assert out["block_multiplier"] == 4
    levels = out["candles"][0]["price_levels"]
    assert len(levels) == 1
    assert levels[0] == {"bid": 10, "ask": 0, "price": 4060.6}


def test_footprint_ws_extra_session_days_default() -> None:
    assert footprint_ws_extra_session_days({}) == 1
    assert footprint_ws_extra_session_days({"footprint_ws": {"extra_session_days": 0}}) == 0


def test_prior_session_dates_to_request() -> None:
    docs = [{"request": {"date": "2026-07-02"}, "candles": [{"time_gmt7": "t1"}]}]
    assert prior_session_dates_to_request(docs, extra_days=1) == ["2026-07-01"]
    assert prior_session_dates_to_request(docs, extra_days=2) == ["2026-06-30", "2026-07-01"]

    both = [
        {"request": {"date": "2026-07-02"}, "candles": []},
        {"request": {"date": "2026-07-01"}, "candles": []},
    ]
    assert prior_session_dates_to_request(both, extra_days=1) == []


def test_merge_footprint_ws_documents_multi_session() -> None:
    docs = [
        {
            "request": {"date": "2026-07-01"},
            "candles": [{"time_gmt7": "Wed Jul 1 2026 05:00:00 GMT+0700"}],
        },
        {
            "request": {"date": "2026-07-02"},
            "candles": [{"time_gmt7": "Thu Jul 2 2026 05:00:00 GMT+0700"}],
        },
    ]
    merged = merge_footprint_ws_documents(docs)
    assert merged is not None
    assert merged["ws_session_dates"] == ["2026-07-01", "2026-07-02"]
    assert len(merged["candles"]) == 2


def test_trim_after_multi_session_merge_keeps_newest() -> None:
    docs = [
        {
            "request": {"date": "2026-07-01"},
            "candles": [{"time_gmt7": f"t{i:03d}"} for i in range(40)],
        },
        {
            "request": {"date": "2026-07-02"},
            "candles": [{"time_gmt7": f"t{i:03d}"} for i in range(40, 60)],
        },
    ]
    merged = merge_footprint_ws_documents(docs)
    assert merged is not None
    trimmed = trim_footprint_document(merged, max_candles=50)
    assert len(trimmed["candles"]) == 50
    assert trimmed["candles"][0]["time_gmt7"] == "t010"
    assert trimmed["candles"][-1]["time_gmt7"] == "t059"
