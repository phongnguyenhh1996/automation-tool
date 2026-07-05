"""Candle-close scheduling helpers (GMT+7, aligned with GoCharting footprint bars)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

_TZ_GMT7 = timezone(timedelta(hours=7))


def now_gmt7_naive() -> datetime:
    return datetime.now(_TZ_GMT7).replace(tzinfo=None)


def forming_candle_open(now: datetime, interval_min: int) -> datetime:
    floored_minute = (now.minute // interval_min) * interval_min
    return now.replace(minute=floored_minute, second=0, microsecond=0)


def latest_closed_candle_open(now: datetime, interval_min: int) -> datetime:
    return forming_candle_open(now, interval_min) - timedelta(minutes=interval_min)


def next_close_trigger(
    now: datetime,
    interval_min: int,
    *,
    buffer_sec: int,
) -> datetime:
    """Earliest future time to run after a candle close (+ buffer)."""
    open_ = forming_candle_open(now, interval_min)
    trigger = open_ + timedelta(seconds=buffer_sec)
    if now >= trigger:
        trigger = open_ + timedelta(minutes=interval_min, seconds=buffer_sec)
    return trigger


def seconds_until(dt: datetime, now: datetime) -> float:
    return max(0.0, (dt - now).total_seconds())


def intervals_due(
    now: datetime,
    *,
    buffer_sec: int,
    last_processed: dict[str, datetime],
) -> list[str]:
    """
    Return timeframe keys (``5m``, ``15m``) whose latest closed bar has not been processed
    and ``now`` is past close + buffer.
    """
    due: list[str] = []
    for iv, minutes in (("5m", 5), ("15m", 15)):
        forming = forming_candle_open(now, minutes)
        if now < forming + timedelta(seconds=buffer_sec):
            continue
        closed_open = latest_closed_candle_open(now, minutes)
        prev = last_processed.get(iv)
        if prev is None or closed_open > prev:
            due.append(iv)
    return due
