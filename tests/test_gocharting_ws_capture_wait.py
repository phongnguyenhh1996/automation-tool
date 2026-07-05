"""Tests for event-driven footprint WS wait helpers."""

from datetime import datetime

from automation_tool.gocharting_ws_capture import (
    footprint_ws_data_ready,
    _ws_ready_stats,
)
from automation_tool.gocharting_ws_decode import latest_closed_candle_open_for_interval


def test_ws_ready_stats_empty() -> None:
    count, complete = _ws_ready_stats([])
    assert count == 0
    assert complete is False


def test_ws_ready_stats_complete() -> None:
    docs = [{"candles": [{"time_gmt7": f"t{i}"} for i in range(12)], "is_complete": True}]
    count, complete = _ws_ready_stats(docs)
    assert count == 12
    assert complete is True


def test_footprint_ws_data_ready_idb() -> None:
    assert footprint_ws_data_ready(footprint_docs=[], idb_candle_count=10, min_candles=10)


def test_footprint_ws_data_ready_idb_fresh() -> None:
    now = datetime(2026, 7, 3, 10, 6, 30)
    expected = latest_closed_candle_open_for_interval(now, "5m")
    assert footprint_ws_data_ready(
        footprint_docs=[],
        idb_candle_count=10,
        min_candles=10,
        idb_last_open=expected,
        expected_closed_open=expected,
    )


def test_footprint_ws_data_ready_idb_stale() -> None:
    now = datetime(2026, 7, 3, 10, 6, 30)
    expected = latest_closed_candle_open_for_interval(now, "5m")
    stale = datetime(2026, 7, 3, 9, 55, 0)
    assert not footprint_ws_data_ready(
        footprint_docs=[],
        idb_candle_count=10,
        min_candles=10,
        idb_last_open=stale,
        expected_closed_open=expected,
    )


def test_latest_closed_candle_open_for_interval_5m() -> None:
    now = datetime(2026, 7, 3, 10, 6, 30)
    assert latest_closed_candle_open_for_interval(now, "5m") == datetime(2026, 7, 3, 10, 0, 0)


def test_latest_closed_candle_open_for_interval_15m() -> None:
    now = datetime(2026, 7, 3, 10, 16, 0)
    assert latest_closed_candle_open_for_interval(now, "15m") == datetime(2026, 7, 3, 10, 0, 0)


def test_footprint_ws_data_ready_ws_complete() -> None:
    docs = [{"candles": [{}] * 15, "is_complete": True}]
    assert footprint_ws_data_ready(footprint_docs=docs, idb_candle_count=0, min_candles=10)


def test_footprint_ws_data_ready_ws_incomplete() -> None:
    docs = [{"candles": [{}] * 15, "is_complete": False}]
    assert not footprint_ws_data_ready(footprint_docs=docs, idb_candle_count=0, min_candles=10)


def test_footprint_ws_data_ready_ws_too_few() -> None:
    docs = [{"candles": [{}] * 5, "is_complete": True}]
    assert not footprint_ws_data_ready(footprint_docs=docs, idb_candle_count=0, min_candles=10)
