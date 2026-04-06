"""Mốc dừng phiên tv-journal-monitor theo first_run (trước 13:00 vs từ 13:00)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from automation_tool.tradingview_journal_monitor import compute_journal_session_cutoff


def test_session_cutoff_morning_stops_at_1300_same_day() -> None:
    z = ZoneInfo("Asia/Ho_Chi_Minh")
    fr = datetime(2026, 4, 6, 10, 0, 0, tzinfo=z)
    co = compute_journal_session_cutoff(fr, "Asia/Ho_Chi_Minh")
    assert co == datetime(2026, 4, 6, 13, 0, 0, tzinfo=z)


def test_session_cutoff_early_morning_stops_at_1300_same_day() -> None:
    z = ZoneInfo("Asia/Ho_Chi_Minh")
    fr = datetime(2026, 4, 6, 1, 30, 0, tzinfo=z)
    co = compute_journal_session_cutoff(fr, "Asia/Ho_Chi_Minh")
    assert co == datetime(2026, 4, 6, 13, 0, 0, tzinfo=z)


def test_session_cutoff_afternoon_runs_until_next_0200() -> None:
    z = ZoneInfo("Asia/Ho_Chi_Minh")
    fr = datetime(2026, 4, 6, 14, 0, 0, tzinfo=z)
    co = compute_journal_session_cutoff(fr, "Asia/Ho_Chi_Minh")
    assert co == datetime(2026, 4, 7, 2, 0, 0, tzinfo=z)


def test_session_cutoff_after_1320_runs_until_next_0200() -> None:
    z = ZoneInfo("Asia/Ho_Chi_Minh")
    fr = datetime(2026, 4, 6, 13, 25, 0, tzinfo=z)
    co = compute_journal_session_cutoff(fr, "Asia/Ho_Chi_Minh")
    assert co == datetime(2026, 4, 7, 2, 0, 0, tzinfo=z)


def test_session_cutoff_exactly_1300_uses_next_0200() -> None:
    """Từ 13:00 trở đi: mốc 02:00 sáng kế tiếp (không còn «trước 13:00»)."""
    z = ZoneInfo("Asia/Ho_Chi_Minh")
    fr = datetime(2026, 4, 6, 13, 0, 0, tzinfo=z)
    co = compute_journal_session_cutoff(fr, "Asia/Ho_Chi_Minh")
    assert co == datetime(2026, 4, 7, 2, 0, 0, tzinfo=z)
