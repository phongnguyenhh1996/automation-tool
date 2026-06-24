from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from automation_tool.gocharting_footprint_ocr import (
    FOOTPRINT_CHART_TYPE,
    append_candle_to_footprint_document,
    closed_candle_time_hhmm,
    extract_time_hhmm_from_ocr_text,
    footprint_interval_json_path,
    new_footprint_document,
    parse_footprint_candle_from_ocr,
    parse_price_levels_from_overlay,
    process_footprint_clip_image,
)


def test_footprint_interval_json_path() -> None:
    p = footprint_interval_json_path(Path("/tmp/out"), "5m")
    assert p.name == "footprint_bid_ask_5m.json"


def test_extract_time_hhmm_from_ocr_text() -> None:
    assert extract_time_hhmm_from_ocr_text("Wed 24 Jun 26 08:25") == "08:25"
    assert extract_time_hhmm_from_ocr_text("no time") is None


def test_closed_candle_time_hhmm() -> None:
    assert closed_candle_time_hhmm(datetime(2025, 6, 24, 8, 25)) == "08:25"


def test_parse_price_levels_from_overlay() -> None:
    words = [
        {"WordText": "0", "Left": 30, "Top": 100, "Height": 12},
        {"WordText": "2", "Left": 180, "Top": 100, "Height": 12},
        {"WordText": "4", "Left": 25, "Top": 120, "Height": 12},
        {"WordText": "0", "Left": 185, "Top": 120, "Height": 12},
    ]
    levels = parse_price_levels_from_overlay(words, image_width=230, split_ratio=0.5)
    assert levels == [{"bid": 0, "ask": 2}, {"bid": 4, "ask": 0}]


def _footprint_line(bid: int, ask: int, top: int, *, split_x: int = 120) -> dict:
    bid_x = max(10, split_x - 70)
    ask_x = split_x + 60
    return {
        "LineText": f"{bid} {ask}",
        "MinTop": top,
        "Words": [
            {"WordText": str(bid), "Left": bid_x, "Top": top, "Width": 10, "Height": 12},
            {"WordText": str(ask), "Left": ask_x, "Top": top, "Width": 10, "Height": 12},
        ],
    }


def test_parse_price_levels_ignores_footer_date_line() -> None:
    rows = [
        (0, 1, 100),
        (0, 7, 118),
        (3, 0, 136),
    ]
    lines = [_footprint_line(bid, ask, top) for bid, ask, top in rows]
    lines.append(
        {
            "LineText": "Wed 24 Jun 26 09:20",
            "MinTop": 900,
            "Words": [
                {"WordText": "24", "Left": 80, "Top": 900, "Width": 12, "Height": 12},
                {"WordText": "26", "Left": 140, "Top": 900, "Width": 12, "Height": 12},
                {"WordText": "09", "Left": 170, "Top": 900, "Width": 12, "Height": 12},
                {"WordText": "20", "Left": 200, "Top": 900, "Width": 12, "Height": 12},
            ],
        }
    )
    levels = parse_price_levels_from_overlay(
        [],
        image_width=240,
        split_ratio=0.5,
        lines=lines,
        image_height=950,
    )
    assert levels == [
        {"bid": 0, "ask": 1},
        {"bid": 0, "ask": 7},
        {"bid": 3, "ask": 0},
    ]


def test_parse_price_levels_full_footprint_candle_shape() -> None:
    expected_rows = [
        (0, 1),
        (0, 7),
        (1, 10),
        (7, 27),
        (14, 31),
        (63, 28),
        (16, 20),
        (7, 17),
        (12, 10),
        (5, 6),
        (2, 6),
        (2, 4),
        (6, 8),
        (8, 6),
        (14, 6),
        (12, 10),
        (8, 1),
        (2, 4),
        (7, 3),
        (2, 0),
        (3, 0),
    ]
    lines = [_footprint_line(bid, ask, 80 + idx * 16) for idx, (bid, ask) in enumerate(expected_rows)]
    levels = parse_price_levels_from_overlay(
        [],
        image_width=240,
        split_ratio=0.5,
        lines=lines,
        image_height=420,
    )
    assert levels == [{"bid": bid, "ask": ask} for bid, ask in expected_rows]


def test_parse_footprint_candle_from_ocr() -> None:
    payload = {
        "ParsedResults": [
            {
                "ParsedText": "Wed 24 Jun 26 08:25",
                "TextOverlay": {
                    "Lines": [
                        {
                            "Words": [
                                {"WordText": "0", "Left": 30, "Top": 100},
                                {"WordText": "2", "Left": 180, "Top": 100},
                            ]
                        }
                    ]
                },
            }
        ]
    }
    candle = parse_footprint_candle_from_ocr(
        payload,
        image_width=230,
        closed_candle_open=datetime(2025, 6, 24, 8, 25),
    )
    assert candle["time"] == "08:25"
    assert candle["price_levels"] == [{"bid": 0, "ask": 2}]


def test_append_candle_to_footprint_document(tmp_path: Path) -> None:
    path = tmp_path / "footprint_bid_ask_5m.json"
    doc = append_candle_to_footprint_document(
        path,
        {"time": "08:25", "price_levels": [{"bid": 1, "ask": 2}]},
        symbol="COMEX:GC1!",
        timeframe="5m",
    )
    assert doc["symbol"] == "COMEX:GC1!"
    assert doc["timeframe"] == "5m"
    assert doc["type"] == FOOTPRINT_CHART_TYPE
    assert len(doc["candles"]) == 1

    doc2 = append_candle_to_footprint_document(
        path,
        {"time": "08:25", "price_levels": [{"bid": 3, "ask": 4}]},
        symbol="COMEX:GC1!",
        timeframe="5m",
    )
    assert len(doc2["candles"]) == 1
    assert doc2["candles"][0]["price_levels"][0]["bid"] == 3
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["candles"][0]["time"] == "08:25"


def test_process_footprint_clip_image_deletes_png(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image = tmp_path / "clip.png"
    image.write_bytes(b"png")
    json_path = tmp_path / "footprint_bid_ask_5m.json"

    monkeypatch.setattr(
        "automation_tool.gocharting_footprint_ocr.ocr_space_parse_image",
        lambda *_a, **_k: {
            "ParsedResults": [
                {
                    "ParsedText": "08:25",
                    "TextOverlay": {
                        "Lines": [
                            {
                                "Words": [
                                    {"WordText": "1", "Left": 20, "Top": 50},
                                    {"WordText": "2", "Left": 170, "Top": 50},
                                ]
                            }
                        ]
                    },
                }
            ]
        },
    )

    candle, doc = process_footprint_clip_image(
        image,
        ocr_api_key="test-key",
        closed_candle_open=datetime(2025, 6, 24, 8, 25),
        image_width=230,
        out_json_path=json_path,
        symbol="COMEX:GC1!",
        timeframe="5m",
        delete_image_after=True,
    )
    assert candle["time"] == "08:25"
    assert not image.is_file()
    assert json_path.is_file()
    assert len(doc["candles"]) == 1
