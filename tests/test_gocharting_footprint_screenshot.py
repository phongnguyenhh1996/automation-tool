from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from automation_tool.gocharting_footprint_screenshot import (
    _FootprintCaptureDue,
    _candle_open_local,
    _clip_box_from_config,
    _closed_candle_open,
    _due_footprint_captures,
    _footprint_image_path,
    _footprint_screenshot_cfg,
    _format_candle_time_label,
    _interval_minutes,
    _is_first_minute_of_candle,
    _run_footprint_ocr_for_captures,
    _wait_seconds_until_next_minute,
)


def test_interval_minutes() -> None:
    assert _interval_minutes("5m") == 5
    assert _interval_minutes("15m") == 15


@pytest.mark.parametrize(
    ("now", "interval_min", "expected"),
    [
        (datetime(2025, 6, 24, 10, 1), 5, True),
        (datetime(2025, 6, 24, 10, 6), 5, True),
        (datetime(2025, 6, 24, 10, 5), 5, False),
        (datetime(2025, 6, 24, 10, 16), 15, True),
        (datetime(2025, 6, 24, 10, 31), 15, True),
        (datetime(2025, 6, 24, 10, 15), 15, False),
    ],
)
def test_is_first_minute_of_candle(now: datetime, interval_min: int, expected: bool) -> None:
    assert _is_first_minute_of_candle(now, interval_min) is expected


def test_candle_open_local_m5() -> None:
    now = datetime(2025, 6, 24, 10, 1)
    assert _candle_open_local(now, 5) == datetime(2025, 6, 24, 10, 0)


def test_closed_candle_open_m5_at_10_01() -> None:
    now = datetime(2025, 6, 24, 10, 1)
    closed = _closed_candle_open(now, 5)
    assert closed == datetime(2025, 6, 24, 9, 55)
    assert _format_candle_time_label(closed) == "9h55m"


def test_closed_candle_open_m5_at_10_06() -> None:
    now = datetime(2025, 6, 24, 10, 6)
    closed = _closed_candle_open(now, 5)
    assert closed == datetime(2025, 6, 24, 10, 0)
    assert _format_candle_time_label(closed) == "10h0m"


def test_closed_candle_open_m15_at_10_16() -> None:
    now = datetime(2025, 6, 24, 10, 16)
    closed = _closed_candle_open(now, 15)
    assert closed == datetime(2025, 6, 24, 10, 0)
    assert _format_candle_time_label(closed) == "10h0m"


def test_footprint_image_path() -> None:
    closed = datetime(2025, 6, 24, 9, 55)
    path = _footprint_image_path(Path("/tmp/out"), closed, "5m")
    assert path.name == "20250624_9h55m_5m.png"


def test_clip_box_from_config() -> None:
    clip = _clip_box_from_config({"x1": 50, "y1": 50, "x2": 300, "y2": 1100})
    assert clip == {"x": 50, "y": 50, "width": 250, "height": 1050}


def test_wait_seconds_until_next_minute() -> None:
    now = datetime(2025, 6, 24, 10, 1, 20)
    assert _wait_seconds_until_next_minute(now) == 40.0


def test_footprint_screenshot_cfg_merges_defaults() -> None:
    cfg = _footprint_screenshot_cfg({})
    assert cfg["output_subdir"] == "footprint_images"
    assert cfg["intervals"]["5m"]["zoom_clicks"] == 10
    assert cfg["clip"]["x2"] == 300


def test_footprint_screenshot_cfg_yaml_override() -> None:
    cfg = _footprint_screenshot_cfg(
        {
            "footprint_screenshot": {
                "intervals": {
                    "5m": {"zoom_clicks": 7},
                }
            }
        }
    )
    assert cfg["intervals"]["5m"]["zoom_clicks"] == 7
    assert cfg["intervals"]["15m"]["page_url"].endswith("S0kcqfQKt")


def test_due_footprint_captures_at_m5_and_m15_trigger_minute(tmp_path: Path) -> None:
    now = datetime(2025, 6, 24, 10, 1)
    captured: set[tuple[str, datetime]] = set()
    due = _due_footprint_captures(
        now=now,
        intervals=("5m", "15m"),
        captured=captured,
        out_dir=tmp_path,
    )
    assert len(due) == 2
    assert {d.interval for d in due} == {"5m", "15m"}


def test_due_footprint_captures_skips_deduped(tmp_path: Path) -> None:
    now = datetime(2025, 6, 24, 10, 6)
    closed = _closed_candle_open(now, 5)
    captured = {("5m", closed)}
    due = _due_footprint_captures(
        now=now,
        intervals=("5m",),
        captured=captured,
        out_dir=tmp_path,
    )
    assert due == []


def test_run_footprint_ocr_for_captures_continues_after_http_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    closed = datetime(2025, 6, 24, 9, 55)
    items = [
        _FootprintCaptureDue(
            interval="5m",
            closed_open=closed,
            dest=tmp_path / "a.png",
            dedupe_key=("5m", closed),
        ),
        _FootprintCaptureDue(
            interval="15m",
            closed_open=closed,
            dest=tmp_path / "b.png",
            dedupe_key=("15m", closed),
        ),
    ]
    calls: list[Path] = []

    def fake_process(image_path: Path, **kwargs: object) -> object:
        calls.append(image_path)
        if image_path.name == "a.png":
            raise httpx.HTTPStatusError(
                "504 Gateway Timeout",
                request=httpx.Request("POST", "https://api.ocr.space/parse/image"),
                response=httpx.Response(504),
            )
        return (
            {"time": "09:55", "price_levels": [{"price": 1.0, "bid": 1, "ask": 2}]},
            {"candles": []},
        )

    monkeypatch.setattr(
        "automation_tool.gocharting_footprint_ocr.process_footprint_clip_image",
        fake_process,
    )
    captured: set[tuple[str, datetime]] = set()
    _run_footprint_ocr_for_captures(
        ok=items,
        out_dir=tmp_path,
        ocr_key="test-key",
        clip_width=250,
        symbol="COMEX:GC1!",
        ocr_split_ratio=0.5,
        delete_after_ocr=False,
        captured=captured,
        now=datetime(2025, 6, 24, 10, 1),
    )
    assert calls == [tmp_path / "a.png", tmp_path / "b.png"]
    assert captured == {("15m", closed)}


def test_run_footprint_ocr_for_captures_marks_skip_as_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed = datetime(2025, 6, 24, 9, 55)
    item = _FootprintCaptureDue(
        interval="5m",
        closed_open=closed,
        dest=tmp_path / "a.png",
        dedupe_key=("5m", closed),
    )

    monkeypatch.setattr(
        "automation_tool.gocharting_footprint_ocr.process_footprint_clip_image",
        lambda *args, **kwargs: None,
    )
    captured: set[tuple[str, datetime]] = set()
    _run_footprint_ocr_for_captures(
        ok=[item],
        out_dir=tmp_path,
        ocr_key="test-key",
        clip_width=250,
        symbol="COMEX:GC1!",
        ocr_split_ratio=0.5,
        delete_after_ocr=False,
        captured=captured,
        now=datetime(2025, 6, 24, 10, 1),
    )
    assert captured == {("5m", closed)}
