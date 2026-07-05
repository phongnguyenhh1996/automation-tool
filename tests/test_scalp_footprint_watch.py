"""Tests for scalp footprint watch scheduling."""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "scalp_footprint"))

from scheduling import (  # noqa: E402
    forming_candle_open,
    intervals_due,
    latest_closed_candle_open,
    next_close_trigger,
)


def test_forming_candle_open_5m() -> None:
    now = datetime(2026, 7, 3, 10, 7, 30)
    assert forming_candle_open(now, 5) == datetime(2026, 7, 3, 10, 5, 0)


def test_latest_closed_candle_open_5m() -> None:
    now = datetime(2026, 7, 3, 10, 7, 30)
    assert latest_closed_candle_open(now, 5) == datetime(2026, 7, 3, 10, 0, 0)


def test_intervals_due_after_buffer() -> None:
    # 10:06:30 — 90s after 10:05 close; 10:00-10:05 bar should be due for 5m
    now = datetime(2026, 7, 3, 10, 6, 30)
    due = intervals_due(now, buffer_sec=90, last_processed={})
    assert "5m" in due


def test_intervals_not_due_before_buffer() -> None:
    now = datetime(2026, 7, 3, 10, 1, 0)
    due = intervals_due(now, buffer_sec=90, last_processed={})
    assert due == []


def test_intervals_skip_already_processed() -> None:
    now = datetime(2026, 7, 3, 10, 6, 30)
    last = {"5m": latest_closed_candle_open(now, 5)}
    due = intervals_due(now, buffer_sec=90, last_processed=last)
    assert "5m" not in due


def test_next_close_trigger_future() -> None:
    now = datetime(2026, 7, 3, 10, 6, 0)
    nxt = next_close_trigger(now, 5, buffer_sec=90)
    assert nxt == datetime(2026, 7, 3, 10, 6, 30)


def test_15m_due_on_quarter_hour() -> None:
    now = datetime(2026, 7, 3, 10, 16, 30)
    due = intervals_due(now, buffer_sec=90, last_processed={})
    assert "15m" in due
    assert "5m" in due
