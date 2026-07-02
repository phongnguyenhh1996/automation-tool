from __future__ import annotations

from automation_tool.gocharting_footprint_idb import (
    decode_idb_footprint_bytes,
    merge_footprint_documents,
    parse_idb_footprint_key,
)
from automation_tool.gocharting_ws_decode import (
    FOOTPRINT_EXPORT_FORMAT_RAW,
    WS_BINARY_MARKER,
    footprint_protobuf_payload_from_bytes,
)
from automation_tool.proto import footprint_pb2 as pb


def _wrap_footprint_ws_envelope(proto: bytes, type_str: str = "FOOTPRINT/V2~~2~b") -> bytes:
    type_b = type_str.encode("ascii")
    return bytes([WS_BINARY_MARKER, 0, 0, 0, len(type_b)]) + type_b + proto


def _sample_footprint_response_bytes() -> bytes:
    msg = pb.FootPrintForDateResponse()
    msg.request.exchange = "COMEX"
    msg.request.segment = "FUTURE"
    msg.request.symbol = "GC1!"
    msg.request.interval = "5m"
    msg.request.session = "ETH"
    msg.request.date = "2026-07-01"
    candle = msg.candles.add()
    candle.date = "2026-07-01T05:00:00+07:00"
    return msg.SerializeToString()


def test_parse_idb_footprint_key() -> None:
    key = "COMEX:FUTURE:GC1!:15m:2026-07-01:ETH"
    meta = parse_idb_footprint_key(key)
    assert meta["exchange"] == "COMEX"
    assert meta["segment"] == "FUTURE"
    assert meta["symbol"] == "GC1!"
    assert meta["interval"] == "15m"
    assert meta["date"] == "2026-07-01"
    assert meta["session"] == "ETH"


def test_decode_idb_footprint_bytes_raw_protobuf() -> None:
    raw = _sample_footprint_response_bytes()
    doc = decode_idb_footprint_bytes(
        raw,
        export_format=FOOTPRINT_EXPORT_FORMAT_RAW,
        idb_key="COMEX:FUTURE:GC1!:5m:2026-07-01:ETH",
    )
    assert doc["symbol"] == "COMEX:GC1!"
    assert doc["request"]["date"] == "2026-07-01"
    assert doc["ws_type"] == "INDEXEDDB/COMEX:FUTURE:GC1!:5m:2026-07-01:ETH"
    assert len(doc["candles"]) == 1


def test_decode_idb_footprint_bytes_ws_envelope() -> None:
    raw = _wrap_footprint_ws_envelope(_sample_footprint_response_bytes())
    payload, envelope_type = footprint_protobuf_payload_from_bytes(raw)
    assert envelope_type is not None
    assert envelope_type.startswith("FOOTPRINT/V2")
    doc = decode_idb_footprint_bytes(
        raw,
        export_format=FOOTPRINT_EXPORT_FORMAT_RAW,
        idb_key="COMEX:FUTURE:GC1!:5m:2026-07-01:ETH",
    )
    assert doc["symbol"] == "COMEX:GC1!"
    assert doc["request"]["date"] == "2026-07-01"
    assert doc["ws_type"] == "INDEXEDDB/FOOTPRINT/V2~~2~b"
    assert len(doc["candles"]) == 1
    assert payload != raw


def test_merge_footprint_documents_by_time() -> None:
    d1 = {
        "symbol": "COMEX:GC1!",
        "candles": [
            {"time_gmt7": "01/07 08:00 GMT+0700", "footprint": []},
            {"time_gmt7": "01/07 08:15 GMT+0700", "footprint": []},
        ],
    }
    d2 = {
        "symbol": "COMEX:GC1!",
        "candles": [
            {"time_gmt7": "02/07 08:00 GMT+0700", "footprint": []},
        ],
    }
    merged = merge_footprint_documents([d1, d2])
    assert merged is not None
    assert len(merged["candles"]) == 3
    assert merged["candles"][0]["time_gmt7"].startswith("01/07")
    assert merged["candles"][-1]["time_gmt7"].startswith("02/07")


def test_merge_footprint_documents_chronological_not_string_sort() -> None:
    """Wed Jul 1 23:55 must sort after Thu Jul 2 05:00 (string sort breaks trim)."""
    from automation_tool.gocharting_ws_decode import trim_footprint_document

    jul1_evening = [
        {"time_gmt7": f"Wed Jul 1 2026 {h:02d}:{m:02d}:00 GMT+0700", "footprint": []}
        for h, m in ((19, 50), (23, 55))
    ]
    jul2_morning = [
        {"time_gmt7": f"Thu Jul 2 2026 {h:02d}:{m:02d}:00 GMT+0700", "footprint": []}
        for h, m in ((5, 0), (13, 45), (17, 50))
    ]
    d1 = {"symbol": "COMEX:GC1!", "candles": jul1_evening}
    d2 = {"symbol": "COMEX:GC1!", "candles": jul2_morning}
    merged = merge_footprint_documents([d1, d2])
    assert merged is not None
    keys = [c["time_gmt7"] for c in merged["candles"]]
    assert keys[0].startswith("Wed Jul 1")
    assert keys[-1].startswith("Thu Jul 2")
    trimmed = trim_footprint_document(merged, max_candles=3)
    last_three = [c["time_gmt7"] for c in trimmed["candles"]]
    assert all(k.startswith("Thu Jul 2") for k in last_three)
