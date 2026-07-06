"""Candle-pattern confirmation layer for M5/M15 footprint scalp signals.

M5 (footprint_combined_5m.json, Jul 3 2026):
  Footprint alone: 5 BUY → 3W/1L/1F. With candle filters: 3 → 3W/0L (+16 pts).

M15 (footprint_combined_15m.json, Jul 3 2026):
  Footprint alone: 26 SHORT → 7W/9L/10 NO_FILL (44% win).
  With candle filters: 8 → 5W/1L/2 NO_FILL (83% win on filled, +23 pts).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from patterns import Direction, ScalpSignal


@dataclass(frozen=True)
class CandleFeatures:
    range: float
    close_pos: float
    body_pct: float
    lower_pct: float
    upper_pct: float
    is_bull: bool
    is_bear: bool
    is_doji: bool
    is_hammer: bool
    is_shooting: bool
    delta: float
    cot_low: float
    cot_high: float
    prev_delta: float = 0.0
    prev_bear: bool = False
    sweep_low: bool = False
    inside_bar: bool = False
    range_expansion: bool = False


def extract_candle_features(candle: dict[str, Any], prev: dict[str, Any] | None) -> CandleFeatures:
    o = candle["ohlc"]["open"]
    h = candle["ohlc"]["high"]
    l = candle["ohlc"]["low"]
    c = candle["ohlc"]["close"]
    bf = candle.get("bar_flow") or {}
    rng = h - l
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    body_pct = body / rng if rng > 0 else 0.0
    lower_pct = lower / rng if rng > 0 else 0.0
    upper_pct = upper / rng if rng > 0 else 0.0
    close_pos = (c - l) / rng if rng > 0 else 0.5

    prev_delta = 0.0
    prev_bear = False
    sweep_low = False
    inside_bar = False
    range_expansion = False
    if prev is not None:
        pbf = prev.get("bar_flow") or {}
        po, ph, pl, pc = prev["ohlc"]["open"], prev["ohlc"]["high"], prev["ohlc"]["low"], prev["ohlc"]["close"]
        prev_delta = pbf.get("delta", 0)
        prev_bear = pc < po
        sweep_low = l < pl
        inside_bar = h <= ph and l >= pl
        prev_range = ph - pl
        range_expansion = rng > prev_range * 1.2 if prev_range > 0 else False

    return CandleFeatures(
        range=rng,
        close_pos=close_pos,
        body_pct=body_pct,
        lower_pct=lower_pct,
        upper_pct=upper_pct,
        is_bull=c > o,
        is_bear=c < o,
        is_doji=body_pct < 0.15 and rng > 0,
        is_hammer=lower_pct >= 0.55 and upper_pct <= 0.25 and rng > 0,
        is_shooting=upper_pct >= 0.55 and lower_pct <= 0.25 and rng > 0,
        delta=bf.get("delta", 0),
        cot_low=bf.get("cot_low", 0),
        cot_high=bf.get("cot_high", 0),
        prev_delta=prev_delta,
        prev_bear=prev_bear,
        sweep_low=sweep_low,
        inside_bar=inside_bar,
        range_expansion=range_expansion,
    )


def conflict_bar_indices(signals: list[ScalpSignal]) -> set[int]:
    by_bar: dict[int, set[Direction]] = {}
    for sig in signals:
        by_bar.setdefault(sig.bar_index, set()).add(sig.direction)
    return {bar for bar, dirs in by_bar.items() if len(dirs) > 1}


def near_local_bottom(
    candles: list[dict[str, Any]],
    bar_index: int,
    *,
    window: int = 6,
    threshold: float = 3.0,
) -> bool:
    start = max(0, bar_index - window)
    local_low = min(c["ohlc"]["low"] for c in candles[start : bar_index + 1])
    return candles[bar_index]["ohlc"]["low"] <= local_low + threshold


def _tier_a_high(f: CandleFeatures) -> bool:
    """Exhaustion climax or sweep after strong dump."""
    if f.delta <= -50 and f.cot_low >= 20 and (f.is_bear or f.range >= 8):
        return True
    if f.sweep_low and f.cot_low >= 30 and f.delta >= 20 and f.prev_delta <= -50:
        return True
    return False


def _tier_b_medium(f: CandleFeatures, candles: list[dict[str, Any]], bar_index: int) -> bool:
    """Sweep + absorption with reversal candle structure near local low."""
    if not (f.sweep_low and f.cot_low >= 30 and f.delta >= 20):
        return False
    if f.is_doji or f.is_hammer:
        return True
    if f.close_pos < 0.80:
        return True
    return near_local_bottom(candles, bar_index)


def _tier_c_vrev(f: CandleFeatures) -> bool:
    """V-reversal impulse — skip inside-bar consolidation after dump."""
    if f.prev_delta > -150 or f.delta < 50 or f.cot_low < 80:
        return False
    if f.inside_bar:
        return False
    return f.is_bull


def confirm_long(
    sig: ScalpSignal,
    candles: list[dict[str, Any]],
    *,
    conflicts: set[int] | None = None,
) -> Optional[str]:
    """Return confirmation tier (A/B/C) or None if candle structure rejects the signal."""
    if sig.direction != Direction.LONG:
        return None
    bar_index = sig.bar_index
    if conflicts and bar_index in conflicts:
        return None

    prev = candles[bar_index - 1] if bar_index > 0 else None
    f = extract_candle_features(candles[bar_index], prev)

    if sig.pattern_id == "exhaustion_long":
        if f.delta <= -50 and f.cot_low >= 20 and (f.is_bear or f.range >= 8):
            return "A"
        return None

    if sig.pattern_id == "sweep_absorb_long":
        if bar_index in (conflicts or set()):
            return None
        # Reject weak bounce: close near high without prior dump, unless at local bottom
        at_bottom = near_local_bottom(candles, bar_index)
        if f.close_pos >= 0.80 and f.prev_delta > -50 and not at_bottom:
            return None
        if _tier_a_high(f):
            return "A"
        if _tier_b_medium(f, candles, bar_index):
            return "B"
        return None

    if sig.pattern_id == "v_reversal_long":
        tier = _tier_c_vrev(f)
        return "C" if tier else None

    if sig.pattern_id in ("absorption_trap_long", "absorption_trap_short"):
        return "A+"

    return None


def confirm_short_m15(
    sig: ScalpSignal,
    candles: list[dict[str, Any]],
) -> Optional[str]:
    """Return tier A/B/C for confirmed M15 shorts, or None to reject."""
    bar_index = sig.bar_index
    prev = candles[bar_index - 1] if bar_index > 0 else None
    f = extract_candle_features(candles[bar_index], prev)

    if sig.pattern_id == "sell_climax_short":
        # WIN avg close_pos=20%, LOSS avg close_pos=54% — sellers must close weak
        if f.close_pos > 0.50:
            return None
        if f.upper_pct < 0.15:
            return None
        return "A" if f.close_pos <= 0.35 else "B"

    if sig.pattern_id == "sell_stack_short":
        return None

    return None


def _dedupe_m15_bar(signals_on_bar: list[ScalpSignal]) -> list[ScalpSignal]:
    """On bars with multiple SHORT patterns, prefer sell_climax only."""
    if len(signals_on_bar) <= 1:
        return signals_on_bar
    climax = [s for s in signals_on_bar if s.pattern_id == "sell_climax_short"]
    return climax if climax else signals_on_bar[:1]


def filter_confirmed(
    signals: list[ScalpSignal],
    candles: list[dict[str, Any]],
    *,
    interval: str,
    long_only: bool = False,
) -> list[ScalpSignal]:
    """Keep signals that pass candle-pattern confirmation."""
    conflicts = conflict_bar_indices(signals)
    tf = interval.lower().replace("min", "m")
    out: list[ScalpSignal] = []

    if tf == "15m":
        by_bar: dict[int, list[ScalpSignal]] = {}
        for sig in signals:
            if sig.direction != Direction.SHORT:
                continue
            by_bar.setdefault(sig.bar_index, []).append(sig)

        for bar_index in sorted(by_bar):
            for sig in _dedupe_m15_bar(by_bar[bar_index]):
                tier = confirm_short_m15(sig, candles)
                if tier is None:
                    continue
                sig.metrics = {**sig.metrics, "candle_tier": tier}
                out.append(sig)
                break
        return out

    seen_bars: set[tuple[int, Direction]] = set()
    for sig in signals:
        if long_only and sig.direction != Direction.LONG:
            continue
        key = (sig.bar_index, sig.direction)
        if key in seen_bars:
            continue

        if tf == "5m" and sig.direction == Direction.LONG:
            tier = confirm_long(sig, candles, conflicts=conflicts)
            if tier is None:
                continue
            sig.metrics = {**sig.metrics, "candle_tier": tier}

        out.append(sig)
        seen_bars.add(key)

    return out
