"""Absorption trap reversal — shared detection for patterns, watch, and backtest."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from footprint_metrics import (
    bar_range,
    candle_delta,
    candle_poc,
    candle_volume,
    has_absorption_at,
    is_bear,
    is_bull,
    range_mid,
    session_volume_median,
)
from patterns import Direction


@dataclass(frozen=True)
class AbsorptionTrapSetup:
    direction: Direction
    trap_index: int
    confirm_index: int
    entry_price: float
    stop_loss: float
    take_profit: float
    entry_hint: str
    metrics: dict[str, Any]


def _reject_mid_range_no_poc_shift(
    candles: list[dict[str, Any]],
    trap_index: int,
    *,
    lookback: int = 8,
) -> bool:
    start = max(0, trap_index - lookback)
    window = candles[start : trap_index + 1]
    if len(window) < 3:
        return False
    highs = [c["ohlc"]["high"] for c in window]
    lows = [c["ohlc"]["low"] for c in window]
    rng_high, rng_low = max(highs), min(lows)
    if rng_high <= rng_low:
        return False
    close = candles[trap_index]["ohlc"]["close"]
    in_mid = rng_low + (rng_high - rng_low) * 0.35 <= close <= rng_low + (rng_high - rng_low) * 0.65
    pocs = [candle_poc(c) for c in window[-3:]]
    poc_flat = (
        len([p for p in pocs if p is not None]) >= 2
        and pocs[-1] is not None
        and pocs[-2] is not None
        and abs(pocs[-1] - pocs[-2]) < 0.4  # type: ignore[operator]
    )
    return in_mid and poc_flat


def try_absorption_trap_at_confirm(
    candles: list[dict[str, Any]],
    confirm_index: int,
    *,
    vol_median: float,
) -> Optional[AbsorptionTrapSetup]:
    """Trap bar is ``confirm_index - 1``; entry at confirm bar open."""
    if confirm_index < 2 or confirm_index >= len(candles):
        return None

    trap = candles[confirm_index - 1]
    prev = candles[confirm_index - 2]
    confirm = candles[confirm_index]
    delta = candle_delta(trap)
    rng = bar_range(trap)
    vol = candle_volume(trap)

    if vol < vol_median * 0.8:
        return None

    swept_high = trap["ohlc"]["high"] > prev["ohlc"]["high"]
    if swept_high and abs(delta) >= 30 and rng <= 6.0 and has_absorption_at(trap, side="ASK"):
        if not is_bear(confirm):
            return None
        if _reject_mid_range_no_poc_shift(candles, confirm_index - 1):
            return None
        entry = confirm["ohlc"]["open"]
        sl = trap["ohlc"]["high"] + 2.0
        poc = candle_poc(trap) or range_mid(trap)
        tp = entry - min(max(abs(entry - poc), 5.0), 8.0)
        return AbsorptionTrapSetup(
            direction=Direction.SHORT,
            trap_index=confirm_index - 1,
            confirm_index=confirm_index,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            entry_hint="SELL sau upthrust bị absorb + bar confirm đóng bear",
            metrics={"delta": delta, "range": rng, "volume": vol, "trap": "upthrust", "tier": "A+"},
        )

    swept_low = trap["ohlc"]["low"] < prev["ohlc"]["low"]
    if swept_low and abs(delta) >= 30 and rng <= 6.0 and has_absorption_at(trap, side="BID"):
        if not is_bull(confirm):
            return None
        if _reject_mid_range_no_poc_shift(candles, confirm_index - 1):
            return None
        entry = confirm["ohlc"]["open"]
        sl = trap["ohlc"]["low"] - 2.0
        poc = candle_poc(trap) or range_mid(trap)
        tp = entry + min(max(abs(poc - entry), 5.0), 8.0)
        return AbsorptionTrapSetup(
            direction=Direction.LONG,
            trap_index=confirm_index - 1,
            confirm_index=confirm_index,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            entry_hint="BUY sau flush bị absorb + bar confirm đóng bull",
            metrics={"delta": delta, "range": rng, "volume": vol, "trap": "flush", "tier": "A+"},
        )

    return None


def scan_absorption_traps(
    candles: list[dict[str, Any]],
    *,
    confirm_indices: list[int] | None = None,
) -> list[AbsorptionTrapSetup]:
    vol_median = session_volume_median(candles)
    indices = confirm_indices if confirm_indices is not None else list(range(2, len(candles)))
    out: list[AbsorptionTrapSetup] = []
    seen: set[tuple[int, str]] = set()
    for i in indices:
        setup = try_absorption_trap_at_confirm(candles, i, vol_median=vol_median)
        if setup is None:
            continue
        key = (setup.confirm_index, setup.direction.value)
        if key in seen:
            continue
        seen.add(key)
        out.append(setup)
    return out
