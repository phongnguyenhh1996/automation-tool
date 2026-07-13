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


# Extra slack after bar close + buffer before SCALP_EXEC is considered stale.
EXEC_GRACE_AFTER_CLOSE_SEC = 30


def interval_minutes(tf: str) -> int:
    t = str(tf or "").strip().lower()
    if t in ("5m", "5min"):
        return 5
    if t in ("15m", "15min"):
        return 15
    raise ValueError(f"unknown scalp interval: {tf!r}")


def signal_bar_close_naive(sig: dict) -> datetime | None:
    """When the signal bar closes (entry becomes actionable)."""
    from automation_tool.gocharting_footprint_ocr import parse_footprint_candle_datetime

    bar_open = parse_footprint_candle_datetime(str(sig.get("time_gmt7") or ""))
    if bar_open is None:
        return None
    return bar_open + timedelta(minutes=interval_minutes(str(sig.get("timeframe") or "5m")))


def signal_exec_deadline(sig: dict, *, buffer_sec: int) -> datetime | None:
    """Latest wall-clock time to emit SCALP_EXEC for this signal."""
    bar_close = signal_bar_close_naive(sig)
    if bar_close is None:
        return None
    return bar_close + timedelta(seconds=int(buffer_sec) + EXEC_GRACE_AFTER_CLOSE_SEC)


def is_signal_exec_fresh(sig: dict, now: datetime, *, buffer_sec: int) -> bool:
    deadline = signal_exec_deadline(sig, buffer_sec=buffer_sec)
    return deadline is not None and now <= deadline


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
