"""Tests for daemon-plan MT5 execution price (ask/bid) helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from automation_tool.mt5_execute import execution_price_from_tick
from automation_tool.tv_watchlist_daemon import (
    compute_daemon_plan_stop_deadline_local,
    daemon_plan_should_exit_if_mt5_tickets_closed,
)
from automation_tool.zones_state import Zone


@dataclass
class _FakeTick:
    bid: float | None
    ask: float | None


def test_execution_price_from_tick_buy_uses_ask() -> None:
    t = _FakeTick(bid=2650.1, ask=2650.5)
    assert execution_price_from_tick(t, "BUY") == 2650.5


def test_execution_price_from_tick_buy_falls_back_to_bid_if_ask_missing() -> None:
    t = _FakeTick(bid=2650.1, ask=None)
    assert execution_price_from_tick(t, "BUY") == 2650.1


def test_execution_price_from_tick_buy_falls_back_to_bid_if_ask_zero() -> None:
    t = _FakeTick(bid=2650.1, ask=0.0)
    assert execution_price_from_tick(t, "BUY") == 2650.1


def test_execution_price_from_tick_sell_uses_bid() -> None:
    t = _FakeTick(bid=2650.1, ask=2650.5)
    assert execution_price_from_tick(t, "SELL") == 2650.1


def test_execution_price_from_tick_sell_falls_back_to_ask_if_bid_missing() -> None:
    t = _FakeTick(bid=None, ask=2650.5)
    assert execution_price_from_tick(t, "SELL") == 2650.5


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


def test_daemon_plan_ticket_closed_exits_when_none_on_mt5(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon.mt5_ticket_status_for_cutoff",
        lambda ticket, **kwargs: ("none", "closed"),
    )
    monkeypatch.setattr("automation_tool.tv_watchlist_daemon._send_log", lambda *a, **k: None)
    z = Zone(
        id="z1",
        label="L1",
        vung_cho="2600-2700",
        side="BUY",
        mt5_ticket=123456,
    )
    settings = MagicMock()
    ok, msg = daemon_plan_should_exit_if_mt5_tickets_closed(
        [z],
        dry_run=False,
        accounts_json=None,
        settings=settings,
        shard_tag="/tmp/vung_x.json",
    )
    assert ok is True
    assert "123456" in msg


def test_daemon_plan_ticket_closed_keeps_running_if_position(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon.mt5_ticket_status_for_cutoff",
        lambda ticket, **kwargs: ("position", "open"),
    )
    z = Zone(
        id="z1",
        label="L1",
        vung_cho="2600-2700",
        side="BUY",
        mt5_ticket=123456,
    )
    settings = MagicMock()
    ok, _ = daemon_plan_should_exit_if_mt5_tickets_closed(
        [z],
        dry_run=False,
        accounts_json=None,
        settings=settings,
        shard_tag="shard",
    )
    assert ok is False


def test_daemon_plan_ticket_closed_no_ticket_in_state() -> None:
    z = Zone(
        id="z1",
        label="L1",
        vung_cho="2600-2700",
        side="BUY",
        mt5_ticket=None,
    )
    settings = MagicMock()
    ok, _ = daemon_plan_should_exit_if_mt5_tickets_closed(
        [z],
        dry_run=False,
        accounts_json=None,
        settings=settings,
        shard_tag="shard",
    )
    assert ok is False
