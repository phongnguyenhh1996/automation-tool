from __future__ import annotations

from automation_tool.gocharting_footprint_idb import (
    merge_footprint_documents,
    parse_idb_footprint_key,
)


def test_parse_idb_footprint_key() -> None:
    key = "COMEX:FUTURE:GC1!:15m:2026-07-01:ETH"
    meta = parse_idb_footprint_key(key)
    assert meta["exchange"] == "COMEX"
    assert meta["segment"] == "FUTURE"
    assert meta["symbol"] == "GC1!"
    assert meta["interval"] == "15m"
    assert meta["date"] == "2026-07-01"
    assert meta["session"] == "ETH"


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
