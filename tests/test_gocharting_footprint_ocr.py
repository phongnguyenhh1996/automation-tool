from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from automation_tool.gocharting_footprint_ocr import (
    FOOTPRINT_CHART_TYPE,
    FootprintOcrSkipped,
    append_candle_to_footprint_document,
    closed_candle_time_hhmm,
    extract_time_hhmm_from_ocr_text,
    footprint_interval_json_path,
    new_footprint_document,
    parse_footprint_candle_from_ocr,
    parse_price_levels_from_overlay,
    parse_price_levels_from_parsed_text,
    process_footprint_clip_image,
)


def test_footprint_interval_json_path() -> None:
    p = footprint_interval_json_path(Path("/tmp/out"), "5m")
    assert p.name == "footprint_bid_ask_5m.json"


def test_existing_footprint_bid_ask_json_paths(tmp_path: Path) -> None:
    from automation_tool.gocharting_footprint_ocr import (
        existing_footprint_bid_ask_json_paths,
        footprint_images_dir,
    )

    out = footprint_images_dir(tmp_path / "charts")
    out.mkdir(parents=True)
    (out / "footprint_bid_ask_5m.json").write_text("{}", encoding="utf-8")
    paths = existing_footprint_bid_ask_json_paths(tmp_path / "charts")
    assert [p.name for p in paths] == ["footprint_bid_ask_5m.json"]


def test_extend_openai_payloads_with_footprint_bid_ask(tmp_path: Path) -> None:
    from automation_tool.images import extend_openai_payloads_with_footprint_bid_ask

    charts = tmp_path / "charts"
    out = charts / "footprint_images"
    out.mkdir(parents=True)
    json_path = out / "footprint_bid_ask_15m.json"
    json_path.write_text("{}", encoding="utf-8")
    base = [("json", charts / "other.json")]
    extended = extend_openai_payloads_with_footprint_bid_ask(base, charts)
    assert extended[0] == base[0]
    assert extended[1] == ("json", json_path)


def test_extract_time_hhmm_from_ocr_text() -> None:
    assert extract_time_hhmm_from_ocr_text("Wed 24 Jun 26 08:25") == "08:25"
    assert extract_time_hhmm_from_ocr_text("no time") is None


def test_closed_candle_time_hhmm() -> None:
    assert closed_candle_time_hhmm(datetime(2025, 6, 24, 8, 25)) == "08:25"


def test_parse_price_levels_from_parsed_text() -> None:
    text = "2 5\n0 4\n1 9\n@ @ @ @\n0\n8\n"
    assert parse_price_levels_from_parsed_text(text) == [
        {"bid": 2, "ask": 5},
        {"bid": 0, "ask": 4},
        {"bid": 1, "ask": 9},
    ]


def test_parse_price_levels_from_overlay_rejects_single_column() -> None:
    assert parse_price_levels_from_overlay(parsed_text="0\n8\n0\n6\n") == []


def _footprint_line(bid: int, ask: int, top: int) -> dict:
    return {
        "LineText": f"{bid} {ask}",
        "MinTop": top,
        "Words": [
            {"WordText": f"{bid} {ask}", "Left": 50, "Top": top, "Width": 40, "Height": 12},
        ],
    }


def test_parse_price_levels_ignores_footer_date_line() -> None:
    rows = [(0, 1, 100), (0, 7, 118), (3, 0, 136)]
    lines = [_footprint_line(bid, ask, top) for bid, ask, top in rows]
    lines.append(
        {
            "LineText": "Wed 24 Jun 26 09:20",
            "MinTop": 900,
            "Words": [],
        }
    )
    levels = parse_price_levels_from_overlay(lines=lines)
    assert levels == [
        {"bid": 0, "ask": 1},
        {"bid": 0, "ask": 7},
        {"bid": 3, "ask": 0},
    ]


def test_parse_footprint_candle_from_ocr() -> None:
    payload = {
        "ParsedResults": [
            {
                "ParsedText": "0 2\n",
                "TextOverlay": {
                    "Lines": [
                        {
                            "LineText": "0 2",
                            "MinTop": 100,
                            "Words": [
                                {"WordText": "0 2", "Left": 30, "Top": 100},
                            ],
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


def test_parse_footprint_candle_from_ocr_skips_without_pairs() -> None:
    payload = {
        "ParsedResults": [
            {
                "ParsedText": "0\n2\n",
                "TextOverlay": {"Lines": []},
            }
        ]
    }
    with pytest.raises(FootprintOcrSkipped):
        parse_footprint_candle_from_ocr(
            payload,
            image_width=230,
            closed_candle_open=datetime(2025, 6, 24, 8, 25),
        )


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


def test_process_footprint_clip_image_skips_without_pairs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image = tmp_path / "clip.png"
    image.write_bytes(b"png")
    json_path = tmp_path / "footprint_bid_ask_5m.json"

    def _raise_skip(*_a, **_k):
        raise FootprintOcrSkipped("no pairs")

    monkeypatch.setattr(
        "automation_tool.gocharting_footprint_ocr.parse_footprint_candle_from_clip_image",
        _raise_skip,
    )

    assert (
        process_footprint_clip_image(
            image,
            ocr_api_key="test-key",
            closed_candle_open=datetime(2025, 6, 24, 8, 25),
            image_width=230,
            out_json_path=json_path,
            symbol="COMEX:GC1!",
            timeframe="5m",
        )
        is None
    )
    assert image.is_file()
    assert not json_path.is_file()


def test_process_footprint_clip_image_deletes_png(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image = tmp_path / "clip.png"
    image.write_bytes(b"png")
    json_path = tmp_path / "footprint_bid_ask_5m.json"

    monkeypatch.setattr(
        "automation_tool.gocharting_footprint_ocr.parse_footprint_candle_from_clip_image",
        lambda *_a, **_k: {
            "time": "08:25",
            "price_levels": [{"bid": 1, "ask": 2}],
        },
    )

    result = process_footprint_clip_image(
        image,
        ocr_api_key="test-key",
        closed_candle_open=datetime(2025, 6, 24, 8, 25),
        image_width=230,
        out_json_path=json_path,
        symbol="COMEX:GC1!",
        timeframe="5m",
        delete_image_after=True,
    )
    assert result is not None
    candle, doc = result
    assert candle["time"] == "08:25"
    assert not image.is_file()
    assert json_path.is_file()
    assert len(doc["candles"]) == 1
