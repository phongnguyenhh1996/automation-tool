from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from automation_tool.gocharting_footprint_ocr import (
    FOOTPRINT_CHART_TYPE,
    FootprintOcrSkipped,
    append_candle_to_footprint_document,
    batch_ocr_footprint_clip_images,
    closed_candle_time_hhmm,
    compute_footprint_level_prices,
    csv_time_to_hhmm,
    enrich_footprint_bid_ask_document,
    extract_time_hhmm_from_ocr_text,
    footprint_interval_json_path,
    list_footprint_clip_pngs,
    merge_footprint_candles_by_time,
    new_footprint_document,
    parse_footprint_clip_png_name,
    parse_footprint_candle_from_ocr,
    parse_gocharting_csv_ohlc_by_hhmm,
    parse_price_levels_from_overlay,
    parse_price_levels_from_parsed_text,
    process_footprint_clip_image,
)


def test_footprint_interval_json_path() -> None:
    p = footprint_interval_json_path(Path("/tmp/out"), "5m")
    assert p.name == "footprint_bid_ask_5m.json"


def test_parse_footprint_clip_png_name() -> None:
    parsed = parse_footprint_clip_png_name("20250624_9h55m_5m.png")
    assert parsed is not None
    closed_open, interval = parsed
    assert closed_open == datetime(2025, 6, 24, 9, 55)
    assert interval == "5m"
    assert parse_footprint_clip_png_name("not_a_footprint.png") is None


def test_list_footprint_clip_pngs_sorts_by_time(tmp_path: Path) -> None:
    out = tmp_path / "footprint_images"
    out.mkdir()
    for name in (
        "20250624_10h0m_5m.png",
        "20250624_9h55m_5m.png",
        "20250624_10h0m_15m.png",
    ):
        (out / name).write_bytes(b"png")
    items = list_footprint_clip_pngs(out, intervals={"5m"})
    assert [p.name for p, _, iv in items] == [
        "20250624_9h55m_5m.png",
        "20250624_10h0m_5m.png",
    ]
    assert all(iv == "5m" for _, _, iv in items)


def test_merge_footprint_candles_by_time_keeps_more_levels() -> None:
    merged = merge_footprint_candles_by_time(
        [
            {"time": "08:25", "price_levels": [{"bid": 1, "ask": 2}]},
            {"time": "08:20", "price_levels": [{"bid": 0, "ask": 1}, {"bid": 2, "ask": 3}]},
            {"time": "08:25", "price_levels": [{"bid": 1, "ask": 2}, {"bid": 3, "ask": 4}]},
        ]
    )
    assert [c["time"] for c in merged] == ["08:20", "08:25"]
    assert len(merged[1]["price_levels"]) == 2


def test_batch_ocr_footprint_clip_images_writes_sorted_deduped_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    out = tmp_path / "footprint_images"
    out.mkdir()
    for name in (
        "20250624_9h55m_5m.png",
        "20250623_10h0m_5m.png",
        "20250624_10h0m_5m.png",
    ):
        (out / name).write_bytes(b"png")

    def _fake_parse(path: Path, **_kwargs):
        if path.name == "20250624_9h55m_5m.png":
            return {"time": "09:55", "price_levels": [{"bid": 1, "ask": 2}]}
        if path.name == "20250623_10h0m_5m.png":
            return {"time": "10:00", "price_levels": [{"bid": 1, "ask": 2}]}
        if path.name == "20250624_10h0m_5m.png":
            return {
                "time": "10:00",
                "price_levels": [
                    {"bid": 1, "ask": 2},
                    {"bid": 3, "ask": 4},
                    {"bid": 5, "ask": 6},
                ],
            }
        raise AssertionError(path.name)

    monkeypatch.setattr(
        "automation_tool.gocharting_footprint_ocr.parse_footprint_candle_from_clip_image",
        _fake_parse,
    )

    docs = batch_ocr_footprint_clip_images(
        out,
        ocr_api_key="test-key",
        symbol="COMEX:GC1!",
        image_width=230,
        intervals=("5m",),
        ocr_delay_s=0,
    )
    assert len(docs["5m"]["candles"]) == 2
    assert [c["time"] for c in docs["5m"]["candles"]] == ["09:55", "10:00"]
    assert len(docs["5m"]["candles"][1]["price_levels"]) == 3

    json_path = out / "footprint_bid_ask_5m.json"
    assert json_path.is_file()
    on_disk = json.loads(json_path.read_text(encoding="utf-8"))
    assert on_disk["type"] == FOOTPRINT_CHART_TYPE
    assert [c["time"] for c in on_disk["candles"]] == ["09:55", "10:00"]


def test_batch_ocr_skips_times_already_in_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    out = tmp_path / "footprint_images"
    out.mkdir()
    json_path = out / "footprint_bid_ask_5m.json"
    json_path.write_text(
        json.dumps(
            {
                "symbol": "COMEX:GC1!",
                "timeframe": "5m",
                "type": FOOTPRINT_CHART_TYPE,
                "candles": [{"time": "09:55", "price_levels": [{"bid": 9, "ask": 9}]}],
            }
        ),
        encoding="utf-8",
    )
    for name in ("20250624_9h55m_5m.png", "20250624_10h0m_5m.png"):
        (out / name).write_bytes(b"png")

    calls: list[str] = []

    def _fake_parse(path: Path, **_kwargs):
        calls.append(path.name)
        if path.name == "20250624_10h0m_5m.png":
            return {"time": "10:00", "price_levels": [{"bid": 1, "ask": 2}]}
        raise AssertionError(path.name)

    monkeypatch.setattr(
        "automation_tool.gocharting_footprint_ocr.parse_footprint_candle_from_clip_image",
        _fake_parse,
    )

    docs = batch_ocr_footprint_clip_images(
        out,
        ocr_api_key="test-key",
        symbol="COMEX:GC1!",
        image_width=230,
        intervals=("5m",),
        ocr_delay_s=0,
    )
    assert calls == ["20250624_10h0m_5m.png"]
    assert [c["time"] for c in docs["5m"]["candles"]] == ["09:55", "10:00"]
    assert docs["5m"]["candles"][0]["price_levels"] == [{"bid": 9, "ask": 9}]


def test_batch_ocr_retries_times_with_empty_price_levels_in_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    out = tmp_path / "footprint_images"
    out.mkdir()
    json_path = out / "footprint_bid_ask_5m.json"
    json_path.write_text(
        json.dumps(
            {
                "symbol": "COMEX:GC1!",
                "timeframe": "5m",
                "type": FOOTPRINT_CHART_TYPE,
                "candles": [{"time": "09:55", "price_levels": []}],
            }
        ),
        encoding="utf-8",
    )
    (out / "20250624_9h55m_5m.png").write_bytes(b"png")

    monkeypatch.setattr(
        "automation_tool.gocharting_footprint_ocr.parse_footprint_candle_from_clip_image",
        lambda *_a, **_k: {
            "time": "09:55",
            "price_levels": [{"bid": 1, "ask": 2}, {"bid": 3, "ask": 4}],
        },
    )

    docs = batch_ocr_footprint_clip_images(
        out,
        ocr_api_key="test-key",
        symbol="COMEX:GC1!",
        image_width=230,
        intervals=("5m",),
        ocr_delay_s=0,
    )
    assert len(docs["5m"]["candles"]) == 1
    assert docs["5m"]["candles"][0]["price_levels"] == [
        {"bid": 1, "ask": 2},
        {"bid": 3, "ask": 4},
    ]


def test_batch_ocr_waits_between_api_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    out = tmp_path / "footprint_images"
    out.mkdir()
    for name in ("20250624_9h55m_5m.png", "20250624_10h0m_5m.png"):
        (out / name).write_bytes(b"png")

    sleeps: list[float] = []

    def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("automation_tool.gocharting_footprint_ocr.time.sleep", _fake_sleep)
    monkeypatch.setattr(
        "automation_tool.gocharting_footprint_ocr.parse_footprint_candle_from_clip_image",
        lambda path, **_kwargs: {
            "time": "09:55" if path.name.endswith("9h55m_5m.png") else "10:00",
            "price_levels": [{"bid": 1, "ask": 2}],
        },
    )

    batch_ocr_footprint_clip_images(
        out,
        ocr_api_key="test-key",
        symbol="COMEX:GC1!",
        image_width=230,
        intervals=("5m",),
        ocr_delay_s=5.0,
    )
    assert sleeps == [5.0]


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


def test_csv_time_to_hhmm() -> None:
    assert csv_time_to_hhmm("2026-06-16 10:05:00") == "10:05"
    assert csv_time_to_hhmm("10:05:00") == "10:05"
    assert (
        csv_time_to_hhmm("Tue Jun 23 2026 10:00:00 GMT+0700 (Indochina Time)")
        == "10:00"
    )


def test_parse_gocharting_csv_gmt_date_format() -> None:
    csv_text = (
        "Time,Open,High,Low,Close\n"
        "Tue Jun 23 2026 10:00:00 GMT+0700 (Indochina Time),4164.9,4167.6,4156.3,4160.7\n"
    )
    out = parse_gocharting_csv_ohlc_by_hhmm(csv_text)
    assert out["10:00"] == {"high": 4167.6, "low": 4156.3}


def test_enrich_footprint_with_gocharting_gmt_csv_time(tmp_path: Path) -> None:
    csv_path = tmp_path / "gc.csv"
    csv_path.write_text(
        "Time,Open,High,Low,Close\n"
        "Tue Jun 23 2026 10:00:00 GMT+0700 (Indochina Time),4164.9,4167.6,4156.3,4160.7\n",
        encoding="utf-8",
    )
    doc = {
        "candles": [
            {
                "time": "10:00",
                "price_levels": [{"bid": 0, "ask": 4}, {"bid": 2, "ask": 0}],
            }
        ],
    }
    out = enrich_footprint_bid_ask_document(doc, csv_path, block_size=0.4)
    levels = out["candles"][0]["price_levels"]
    assert levels[0]["price"] == 4167.6
    assert levels[1]["price"] == 4167.2


def test_compute_footprint_level_prices() -> None:
    assert compute_footprint_level_prices(4091.6, 5, block_size=0.4) == [
        4091.6,
        4091.2,
        4090.8,
        4090.4,
        4090.0,
    ]
    assert compute_footprint_level_prices(4220, 10, block_size=0.4) == [
        4220.0,
        4219.6,
        4219.2,
        4218.8,
        4218.4,
        4218.0,
        4217.6,
        4217.2,
        4216.8,
        4216.4,
    ]
    assert compute_footprint_level_prices(4091.25, 3, block_size=0.4) == [4091.2, 4090.8, 4090.4]


def test_footprint_block_size_from_yaml() -> None:
    from automation_tool.gocharting_footprint_ocr import footprint_block_size

    assert footprint_block_size() == 0.4


def test_parse_gocharting_csv_ohlc_by_hhmm() -> None:
    fixture = (Path(__file__).resolve().parent / "fixtures" / "gocharting_sample.csv").read_text(
        encoding="utf-8"
    )
    out = parse_gocharting_csv_ohlc_by_hhmm(fixture)
    assert out["10:00"] == {"high": 2651.2, "low": 2649.5}
    assert out["10:05"] == {"high": 2652.0, "low": 2650.2}


def test_enrich_footprint_bid_ask_document(tmp_path: Path) -> None:
    csv_path = tmp_path / "gc_5m.csv"
    csv_path.write_text(
        "Time,Open,High,Low,Close\n"
        "2026-06-16 10:05:00,4200,4220,4200,4210\n",
        encoding="utf-8",
    )
    doc = {
        "symbol": "COMEX:GC1!",
        "timeframe": "5m",
        "candles": [
            {
                "time": "10:05",
                "price_levels": [
                    {"bid": 1, "ask": 2},
                    {"bid": 3, "ask": 4},
                ],
            }
        ],
    }
    out = enrich_footprint_bid_ask_document(doc, csv_path)
    levels = out["candles"][0]["price_levels"]
    assert levels[0]["price"] == 4220
    assert levels[1]["price"] == 4219.6
    assert doc["candles"][0]["price_levels"][0] == {"bid": 1, "ask": 2}


def test_parse_price_levels_from_parsed_text() -> None:
    text = "2 5\n0 4\n1 9\n@ @ @ @\n0\n8\n"
    assert parse_price_levels_from_parsed_text(text) == [
        {"bid": 2, "ask": 5},
        {"bid": 0, "ask": 4},
        {"bid": 1, "ask": 9},
    ]


def test_sanitize_parsed_text_strips_no_text_detected() -> None:
    from automation_tool.gocharting_footprint_ocr import _sanitize_parsed_text

    assert _sanitize_parsed_text("*[No text detected]*") == ""
    assert _sanitize_parsed_text("0 1\n1 2\n") == "0 1\n1 2"


def test_parse_footprint_clip_image_retries_rgb_when_grayscale_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from automation_tool.gocharting_footprint_ocr import parse_footprint_candle_from_clip_image

    image = tmp_path / "20260624_13h35m_5m.png"
    image.write_bytes(
        (Path(__file__).resolve().parent / "fixtures" / "footprint_20260624_17h25m_5m.png").read_bytes()
    )
    calls: list[str] = []

    def _fake_ocr(image, **_kwargs):
        mode = "L" if image.mode == "L" else "RGB"
        calls.append(mode)
        if mode == "L":
            return {
                "ParsedResults": [
                    {
                        "ParsedText": "*[No text detected]*",
                        "FileParseExitCode": "1",
                        "TextOverlay": {"Lines": []},
                    }
                ]
            }
        return {
            "ParsedResults": [
                {
                    "ParsedText": "0 1\n",
                    "FileParseExitCode": "1",
                    "TextOverlay": {
                        "Lines": [
                            {
                                "LineText": "0 1",
                                "MinTop": 100,
                                "Words": [{"WordText": "0 1", "Left": 55, "Top": 100}],
                            }
                        ]
                    },
                }
            ]
        }

    monkeypatch.setattr(
        "automation_tool.gocharting_footprint_ocr.ocr_space_parse_pil_image",
        _fake_ocr,
    )

    candle = parse_footprint_candle_from_clip_image(
        image,
        api_key="test-key",
        closed_candle_open=datetime(2026, 6, 24, 13, 35),
        image_width=240,
    )
    assert calls == ["L", "RGB"]
    assert candle["time"] == "13:35"
    assert candle["price_levels"] == [{"bid": 0, "ask": 1}]


def test_parse_price_levels_from_overlay_rejects_single_column() -> None:
    assert parse_price_levels_from_overlay(parsed_text="0\n8\n0\n6\n") == []


def test_parse_price_levels_from_overlay_pairs_split_columns() -> None:
    """OCR sometimes emits bid/ask as separate words on left/right of center."""
    lines = [
        {
            "LineText": "0",
            "MinTop": 100,
            "Words": [{"WordText": "0", "Left": 55, "Top": 100}],
        },
        {
            "LineText": "1",
            "MinTop": 100,
            "Words": [{"WordText": "1", "Left": 83, "Top": 100}],
        },
        {
            "LineText": "26",
            "MinTop": 200,
            "Words": [{"WordText": "26", "Left": 52, "Top": 200}],
        },
        {
            "LineText": "4",
            "MinTop": 200,
            "Words": [{"WordText": "4", "Left": 83, "Top": 200}],
        },
    ]
    levels = parse_price_levels_from_overlay(
        lines=lines,
        parsed_text="",
        image_width=179,
        split_ratio=0.5,
    )
    assert levels == [{"bid": 0, "ask": 1}, {"bid": 26, "ask": 4}]


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
