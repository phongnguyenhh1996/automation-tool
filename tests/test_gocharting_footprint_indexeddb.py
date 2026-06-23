from __future__ import annotations

import base64
from pathlib import Path

import pytest

from automation_tool.gocharting_footprint_extract import validate_footprint_extract_json
from automation_tool.gocharting_footprint_indexeddb import (
    GoChartingIndexedDBFootprintError,
    footprint_key_matches,
    footprint_response_to_extract_payload,
    merge_footprint_payloads,
    parse_footprint_indexeddb_key,
    records_to_footprint_payload,
    require_footprint_json_path,
    resolve_footprint_symbol,
)
from automation_tool.proto import footprint_pb2


def _sample_response(*, interval: str = "15m", date: str = "2026-06-23") -> footprint_pb2.FootPrintForDateResponse:
    msg = footprint_pb2.FootPrintForDateResponse()
    msg.request.exchange = "COMEX"
    msg.request.segment = "FUTURE"
    msg.request.symbol = "GC1!"
    msg.request.interval = interval
    msg.request.session = "ETH"
    msg.request.date = date
    msg.fp_day.price_precision = 1
    msg.fp_day.size_precision = 0
    msg.is_complete = True
    msg.version = 1

    candle = msg.candles.add()
    candle.date = "2026-06-23T10:15:00"
    fp_high = candle.footprint.add()
    fp_high.level = 26505
    fp_high.buy.volume = 2
    fp_high.sell.volume = 4
    fp_low = candle.footprint.add()
    fp_low.level = 26500
    fp_low.buy.volume = 4
    fp_low.sell.volume = 16
    return msg


def test_parse_footprint_indexeddb_key() -> None:
    key = "COMEX:FUTURE:GC1!:15m:2026-06-23:ETH"
    parsed = parse_footprint_indexeddb_key(key)
    assert parsed == {
        "exchange": "COMEX",
        "segment": "FUTURE",
        "symbol": "GC1!",
        "interval": "15m",
        "date": "2026-06-23",
        "session": "ETH",
    }


def test_footprint_key_matches() -> None:
    key = "COMEX:FUTURE:GC1!:5m:2026-06-23:ETH"
    assert footprint_key_matches(key, symbol="GC1!", interval="5m")
    assert not footprint_key_matches(key, symbol="GC1!", interval="15m")


def test_resolve_footprint_symbol_prefers_search_query() -> None:
    entry = {"export_label": "GC", "search_query": "GC1!"}
    assert resolve_footprint_symbol(entry) == "GC1!"


def test_footprint_response_to_extract_payload() -> None:
    response = _sample_response()
    chart_info = {
        "symbol": "COMEX:GC1!",
        "timeframe": "15m",
        "type": "Bid/Ask Footprint",
    }
    payload = footprint_response_to_extract_payload(response, chart_info=chart_info)
    validated = validate_footprint_extract_json(payload)
    assert validated["candles"][0]["time"] == "10:15"
    levels = validated["candles"][0]["price_levels"]
    assert levels[0] == {"bid": 2, "ask": 4, "attributes": []}
    assert levels[1] == {"bid": 4, "ask": 16, "attributes": ["imbalance"]}


def test_merge_footprint_payloads_dedupes_by_time() -> None:
    chart_info = {
        "symbol": "COMEX:GC1!",
        "timeframe": "15m",
        "type": "Bid/Ask Footprint",
    }
    p1 = footprint_response_to_extract_payload(_sample_response(), chart_info=chart_info)
    p2 = footprint_response_to_extract_payload(_sample_response(date="2026-06-22"), chart_info=chart_info)
    merged = merge_footprint_payloads([p1, p2])
    assert len(merged["candles"]) == 1


def test_records_to_footprint_payload_from_b64() -> None:
    response = _sample_response(interval="5m")
    raw = response.SerializeToString()
    records = [{"key": "COMEX:FUTURE:GC1!:5m:2026-06-23:ETH", "data_b64": base64.b64encode(raw).decode()}]
    chart_info = {
        "symbol": "COMEX:GC1!",
        "timeframe": "5m",
        "type": "Bid/Ask Footprint",
    }
    payload = records_to_footprint_payload(records, chart_info=chart_info)
    assert payload is not None
    assert payload["chart_info"]["timeframe"] == "5m"
    assert payload["candles"][0]["price_levels"][0]["bid"] == 2


def test_require_footprint_json_path_rejects_empty_candles(tmp_path: Path) -> None:
    path = tmp_path / "m5_GC1!_footprint.json"
    path.write_text(
        '{"chart_info":{"symbol":"COMEX:GC1!","timeframe":"5m","type":"Bid/Ask Footprint"},"candles":[]}\n',
        encoding="utf-8",
    )
    with pytest.raises(GoChartingIndexedDBFootprintError, match="no candles"):
        require_footprint_json_path(path, interval="5m")


def test_require_footprint_json_path_accepts_valid(tmp_path: Path) -> None:
    path = tmp_path / "m15_GC1!_footprint.json"
    path.write_text(
        '{"chart_info":{"symbol":"COMEX:GC1!","timeframe":"15m","type":"Bid/Ask Footprint"},'
        '"candles":[{"time":"10:15","price_levels":[{"bid":1,"ask":2,"attributes":[]}]}]}\n',
        encoding="utf-8",
    )
    data = require_footprint_json_path(path, interval="15m")
    assert len(data["candles"]) == 1
