"""Tests for daemon-plan MT5 execution price (ask/bid) helpers."""

from __future__ import annotations

from dataclasses import dataclass

from datetime import datetime

from zoneinfo import ZoneInfo

from automation_tool.mt5_execute import execution_price_from_tick
from automation_tool.tv_watchlist_daemon import compute_daemon_plan_stop_deadline_local


@dataclass
class _FakeTick:
    bid: float
    ask: float


def test_execution_price_from_tick_buy_uses_ask() -> None:
    t = _FakeTick(bid=2650.1, ask=2650.5)
    assert execution_price_from_tick(t, "BUY") == 2650.5


def test_execution_price_from_tick_sell_uses_bid() -> None:
    t = _FakeTick(bid=2650.1, ask=2650.5)
    assert execution_price_from_tick(t, "SELL") == 2650.1


def test_daemon_plan_stop_deadline_same_calendar_day() -> None:
    z = ZoneInfo("Asia/Ho_Chi_Minh")
    started = datetime(2026, 4, 18, 8, 30, tzinfo=z)
    d = compute_daemon_plan_stop_deadline_local(started, "Asia/Ho_Chi_Minh", 12, 0)
    assert d == datetime(2026, 4, 18, 12, 0, tzinfo=z)


def test_daemon_plan_stop_deadline_after_noon_is_today_noon() -> None:
    z = ZoneInfo("Asia/Ho_Chi_Minh")
    started = datetime(2026, 4, 18, 14, 0, tzinfo=z)
    d = compute_daemon_plan_stop_deadline_local(started, "Asia/Ho_Chi_Minh", 12, 0)
    assert d == datetime(2026, 4, 18, 12, 0, tzinfo=z)
    assert started >= d


def test_daemon_plan_stop_deadline_midnight_is_next_day() -> None:
    """0:0 = 12h đêm → 00:00 ngày kế (local)."""
    z = ZoneInfo("Asia/Ho_Chi_Minh")
    started = datetime(2026, 4, 18, 8, 30, tzinfo=z)
    d = compute_daemon_plan_stop_deadline_local(started, "Asia/Ho_Chi_Minh", 0, 0)
    assert d == datetime(2026, 4, 19, 0, 0, tzinfo=z)


def test_daemon_plan_stop_deadline_midnight_late_same_evening() -> None:
    z = ZoneInfo("Asia/Ho_Chi_Minh")
    started = datetime(2026, 4, 18, 23, 45, tzinfo=z)
    d = compute_daemon_plan_stop_deadline_local(started, "Asia/Ho_Chi_Minh", 0, 0)
    assert d == datetime(2026, 4, 19, 0, 0, tzinfo=z)
