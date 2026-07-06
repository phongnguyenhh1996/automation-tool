"""Scalp footprint patterns — detection and entry hints from backtested rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class EntryType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"

    @property
    def side(self) -> str:
        return "BUY" if self == Direction.LONG else "SELL"


@dataclass(frozen=True)
class PatternSpec:
    id: str
    name: str
    direction: Direction
    entry_type: EntryType
    timeframes: frozenset[str]
    min_volume: float
    description: str


@dataclass
class ScalpSignal:
    pattern_id: str
    pattern_name: str
    direction: Direction
    entry_type: EntryType
    timeframe: str
    bar_index: int
    time_gmt7: str
    ohlc: dict[str, float]
    bar_flow: dict[str, float]
    entry_price: float
    entry_hint: str
    stop_loss: float
    take_profit_low: float
    take_profit_high: float
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        side = self.direction.side
        order = f"{side} {self.entry_type.value.upper()} @ {self.entry_price:.2f}"
        entry_bar_level = describe_entry_bar_level(self.ohlc, self.entry_price)
        return {
            "pattern_id": self.pattern_id,
            "pattern_name": self.pattern_name,
            "side": side,
            "direction": self.direction.value,
            "entry_type": self.entry_type.value,
            "order": order,
            "timeframe": self.timeframe,
            "bar_index": self.bar_index,
            "time_gmt7": self.time_gmt7,
            "ohlc": self.ohlc,
            "bar_flow": {k: round(v, 2) for k, v in self.bar_flow.items()},
            "entry_price": round(self.entry_price, 2),
            "entry_bar_level": entry_bar_level,
            "entry_hint": self.entry_hint,
            "stop_loss": round(self.stop_loss, 2),
            "take_profit": [round(self.take_profit_low, 2), round(self.take_profit_high, 2)],
            "metrics": self.metrics,
        }


PATTERNS: tuple[PatternSpec, ...] = (
    PatternSpec(
        id="sell_stack_short",
        name="Sell Stack Climax Short",
        direction=Direction.SHORT,
        entry_type=EntryType.LIMIT,
        timeframes=frozenset({"15m"}),
        min_volume=300,
        description="delta <= -30 and 3+ consecutive sell imbalance levels",
    ),
    PatternSpec(
        id="sell_climax_short",
        name="Sell Climax Short",
        direction=Direction.SHORT,
        entry_type=EntryType.LIMIT,
        timeframes=frozenset({"15m"}),
        min_volume=300,
        description="delta <= -40 and cot_high <= -40 — aggressive selling at top",
    ),
    PatternSpec(
        id="exhaustion_long",
        name="Exhaustion Absorption Long",
        direction=Direction.LONG,
        entry_type=EntryType.MARKET,
        timeframes=frozenset({"5m"}),
        min_volume=100,
        description="delta <= -50 and cot_low >= +20 — sell climax with absorption at low",
    ),
    PatternSpec(
        id="v_reversal_long",
        name="V-Reversal Long",
        direction=Direction.LONG,
        entry_type=EntryType.MARKET,
        timeframes=frozenset({"5m"}),
        min_volume=100,
        description="prev delta <= -150, current delta >= +50, cot_low >= +80",
    ),
    PatternSpec(
        id="sweep_absorb_long",
        name="Sweep + Absorb Long",
        direction=Direction.LONG,
        entry_type=EntryType.MARKET,
        timeframes=frozenset({"5m"}),
        min_volume=100,
        description="low < prev low, cot_low >= +30, delta >= +20",
    ),
    PatternSpec(
        id="absorption_trap_long",
        name="Absorption Trap Long",
        direction=Direction.LONG,
        entry_type=EntryType.MARKET,
        timeframes=frozenset({"5m"}),
        min_volume=0,
        description="flush + BID absorption, confirm bull bar — entry @ confirm open (A+)",
    ),
    PatternSpec(
        id="absorption_trap_short",
        name="Absorption Trap Short",
        direction=Direction.SHORT,
        entry_type=EntryType.MARKET,
        timeframes=frozenset({"5m"}),
        min_volume=0,
        description="upthrust + ASK absorption, confirm bear bar — entry @ confirm open (A+)",
    ),
)

STACK_RATIO = 3.0
STACK_MIN_VOL = 5


def max_sell_stack(candle: dict[str, Any]) -> int:
    return _max_stacked(candle, side="sell")


def max_buy_stack(candle: dict[str, Any]) -> int:
    return _max_stacked(candle, side="buy")


def _max_stacked(candle: dict[str, Any], *, side: str) -> int:
    levels = sorted(candle.get("footprint") or [], key=lambda x: x["price"])
    stack = 0
    best = 0
    for lvl in levels:
        buy, sell = lvl["buy"], lvl["sell"]
        if side == "sell":
            ok = (buy == 0 and sell >= STACK_MIN_VOL) or (
                buy > 0 and sell / buy >= STACK_RATIO and sell >= STACK_MIN_VOL
            )
        else:
            ok = (sell == 0 and buy >= STACK_MIN_VOL) or (
                sell > 0 and buy / sell >= STACK_RATIO and buy >= STACK_MIN_VOL
            )
        stack = stack + 1 if ok else 0
        best = max(best, stack)
    return best


def describe_entry_bar_level(ohlc: dict[str, float], entry: float, *, tol: float = 0.02) -> str:
    """Where entry sits on the signal bar vs open/high/low/close."""
    o, h, l, c = ohlc["open"], ohlc["high"], ohlc["low"], ohlc["close"]
    matches = [
        name
        for name, px in (("open", o), ("high", h), ("low", l), ("close", c))
        if abs(px - entry) <= tol
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return " & ".join(matches)
    retrace_50 = c + (h - c) * 0.5
    if abs(entry - retrace_50) <= tol:
        return "50% retrace (close→high)"
    if min(o, c) <= entry <= max(o, c):
        return "giữa open–close"
    if min(c, h) <= entry <= max(c, h):
        return "giữa close–high"
    if min(l, c) <= entry <= max(l, c):
        return "giữa low–close"
    return f"{entry:.2f} (không trùng OHLC)"


def limit_retrace_price(candle: dict[str, Any], *, fraction: float, direction: Direction) -> float:
    o, h, l, c = (
        candle["ohlc"]["open"],
        candle["ohlc"]["high"],
        candle["ohlc"]["low"],
        candle["ohlc"]["close"],
    )
    rng = h - l
    if direction == Direction.SHORT:
        return c + rng * fraction
    return c - rng * fraction


def _sl_tp_short(candle: dict[str, Any], entry: float) -> tuple[float, float, float]:
    high = candle["ohlc"]["high"]
    sl = high + 1.0
    tp_low = entry - 5.0
    tp_high = entry - 10.0
    return sl, tp_low, tp_high


def _sl_tp_long(candle: dict[str, Any], entry: float, prev_low: float | None = None) -> tuple[float, float, float]:
    low = prev_low if prev_low is not None else candle["ohlc"]["low"]
    sl = low - 1.0
    tp_low = entry + 4.0
    tp_high = entry + 8.0
    return sl, tp_low, tp_high


def _bf(candle: dict[str, Any]) -> dict[str, float]:
    return candle.get("bar_flow") or {}


def _vol(candle: dict[str, Any]) -> float:
    return candle["ohlc"].get("volume") or _bf(candle).get("volume", 0)


def _detect_sell_stack_short(c: dict, p: dict | None, i: int, *, tf: str, spec: PatternSpec) -> ScalpSignal | None:
    bf = _bf(c)
    stack = max_sell_stack(c)
    if bf.get("delta", 0) > -30 or stack < 3:
        return None
    entry = c["ohlc"]["high"]
    sl, tp_lo, tp_hi = _sl_tp_short(c, entry)
    return ScalpSignal(
        pattern_id=spec.id,
        pattern_name=spec.name,
        direction=spec.direction,
        entry_type=spec.entry_type,
        timeframe=tf,
        bar_index=i,
        time_gmt7=c.get("time_gmt7", ""),
        ohlc=dict(c["ohlc"]),
        bar_flow=dict(bf),
        entry_price=entry,
        entry_hint=f"LIMIT SELL @ bar high {entry:.2f} (expire 3 bars)",
        stop_loss=sl,
        take_profit_low=tp_lo,
        take_profit_high=tp_hi,
        metrics={"delta": bf.get("delta"), "sell_stack": stack},
    )


def _detect_sell_climax_short(c: dict, p: dict | None, i: int, *, tf: str, spec: PatternSpec) -> ScalpSignal | None:
    bf = _bf(c)
    if bf.get("delta", 0) > -40 or bf.get("cot_high", 0) > -40:
        return None
    entry = limit_retrace_price(c, fraction=0.5, direction=Direction.SHORT)
    sl, tp_lo, tp_hi = _sl_tp_short(c, entry)
    return ScalpSignal(
        pattern_id=spec.id,
        pattern_name=spec.name,
        direction=spec.direction,
        entry_type=spec.entry_type,
        timeframe=tf,
        bar_index=i,
        time_gmt7=c.get("time_gmt7", ""),
        ohlc=dict(c["ohlc"]),
        bar_flow=dict(bf),
        entry_price=entry,
        entry_hint=f"LIMIT SELL @ 50% retrace {entry:.2f} (close + half range to high)",
        stop_loss=sl,
        take_profit_low=tp_lo,
        take_profit_high=tp_hi,
        metrics={"delta": bf.get("delta"), "cot_high": bf.get("cot_high")},
    )


def _detect_exhaustion_long(c: dict, p: dict | None, i: int, *, tf: str, spec: PatternSpec) -> ScalpSignal | None:
    bf = _bf(c)
    if bf.get("delta", 0) > -50 or bf.get("cot_low", 0) < 20:
        return None
    entry = c["ohlc"]["close"]
    sl, tp_lo, tp_hi = _sl_tp_long(c, entry)
    return ScalpSignal(
        pattern_id=spec.id,
        pattern_name=spec.name,
        direction=spec.direction,
        entry_type=spec.entry_type,
        timeframe=tf,
        bar_index=i,
        time_gmt7=c.get("time_gmt7", ""),
        ohlc=dict(c["ohlc"]),
        bar_flow=dict(bf),
        entry_price=entry,
        entry_hint=f"MARKET BUY @ close {entry:.2f} (next bar open)",
        stop_loss=sl,
        take_profit_low=tp_lo,
        take_profit_high=tp_hi,
        metrics={"delta": bf.get("delta"), "cot_low": bf.get("cot_low")},
    )


def _detect_v_reversal_long(c: dict, p: dict | None, i: int, *, tf: str, spec: PatternSpec) -> ScalpSignal | None:
    if p is None:
        return None
    bf, pbf = _bf(c), _bf(p)
    if pbf.get("delta", 0) > -150 or bf.get("delta", 0) < 50 or bf.get("cot_low", 0) < 80:
        return None
    entry = c["ohlc"]["close"]
    sl, tp_lo, tp_hi = _sl_tp_long(c, entry, prev_low=p["ohlc"]["low"])
    return ScalpSignal(
        pattern_id=spec.id,
        pattern_name=spec.name,
        direction=spec.direction,
        entry_type=spec.entry_type,
        timeframe=tf,
        bar_index=i,
        time_gmt7=c.get("time_gmt7", ""),
        ohlc=dict(c["ohlc"]),
        bar_flow=dict(bf),
        entry_price=entry,
        entry_hint=f"MARKET BUY @ close {entry:.2f} after dump bar (prev delta {pbf.get('delta'):+.0f})",
        stop_loss=sl,
        take_profit_low=tp_lo,
        take_profit_high=tp_hi,
        metrics={
            "delta": bf.get("delta"),
            "prev_delta": pbf.get("delta"),
            "cot_low": bf.get("cot_low"),
        },
    )


def _detect_sweep_absorb_long(c: dict, p: dict | None, i: int, *, tf: str, spec: PatternSpec) -> ScalpSignal | None:
    if p is None:
        return None
    bf = _bf(c)
    if (
        c["ohlc"]["low"] >= p["ohlc"]["low"]
        or bf.get("cot_low", 0) < 30
        or bf.get("delta", 0) < 20
    ):
        return None
    entry = c["ohlc"]["close"]
    sl, tp_lo, tp_hi = _sl_tp_long(c, entry)
    return ScalpSignal(
        pattern_id=spec.id,
        pattern_name=spec.name,
        direction=spec.direction,
        entry_type=spec.entry_type,
        timeframe=tf,
        bar_index=i,
        time_gmt7=c.get("time_gmt7", ""),
        ohlc=dict(c["ohlc"]),
        bar_flow=dict(bf),
        entry_price=entry,
        entry_hint=f"MARKET BUY @ close {entry:.2f} (swept low {p['ohlc']['low']:.2f})",
        stop_loss=sl,
        take_profit_low=tp_lo,
        take_profit_high=tp_hi,
        metrics={
            "delta": bf.get("delta"),
            "cot_low": bf.get("cot_low"),
            "prev_low": p["ohlc"]["low"],
        },
    )


def _absorption_trap_pattern_ids() -> frozenset[str]:
    return frozenset({"absorption_trap_long", "absorption_trap_short"})


def _detect_absorption_trap_signals(
    candles: list[dict[str, Any]],
    *,
    tf: str,
    pattern_ids: set[str] | None,
    indices: list[int],
) -> list[ScalpSignal]:
    from absorption_trap import scan_absorption_traps
    from footprint_metrics import enrich_document

    want = _absorption_trap_pattern_ids()
    if pattern_ids is not None and not want.intersection(pattern_ids):
        return []

    enriched = enrich_document({"candles": candles}, for_backtest=True)
    setups = scan_absorption_traps(enriched, confirm_indices=indices)
    signals: list[ScalpSignal] = []
    id_by_direction = {
        Direction.LONG: "absorption_trap_long",
        Direction.SHORT: "absorption_trap_short",
    }
    spec_by_id = {p.id: p for p in PATTERNS if p.id in want}

    for setup in setups:
        pid = id_by_direction[setup.direction]
        if pattern_ids is not None and pid not in pattern_ids:
            continue
        spec = spec_by_id[pid]
        trap_c = enriched[setup.trap_index]
        confirm_c = enriched[setup.confirm_index]
        sig = ScalpSignal(
            pattern_id=spec.id,
            pattern_name=spec.name,
            direction=spec.direction,
            entry_type=spec.entry_type,
            timeframe=tf,
            bar_index=setup.confirm_index,
            time_gmt7=confirm_c.get("time_gmt7", ""),
            ohlc=dict(trap_c["ohlc"]),
            bar_flow=dict(trap_c.get("bar_flow") or {}),
            entry_price=setup.entry_price,
            entry_hint=setup.entry_hint,
            stop_loss=setup.stop_loss,
            take_profit_low=setup.take_profit,
            take_profit_high=setup.take_profit,
            metrics=dict(setup.metrics),
        )
        signals.append(sig)
    return signals


def _build_detectors() -> list[tuple[PatternSpec, Callable[..., ScalpSignal | None]]]:
    fn_by_id: dict[str, Callable[..., ScalpSignal | None]] = {
        "sell_stack_short": _detect_sell_stack_short,
        "sell_climax_short": _detect_sell_climax_short,
        "exhaustion_long": _detect_exhaustion_long,
        "v_reversal_long": _detect_v_reversal_long,
        "sweep_absorb_long": _detect_sweep_absorb_long,
    }
    return [(spec, fn_by_id[spec.id]) for spec in PATTERNS if spec.id in fn_by_id]


def detect_patterns(
    candles: list[dict[str, Any]],
    *,
    interval: str,
    pattern_ids: set[str] | None = None,
    latest_only: bool = False,
    min_volume_override: dict[str, float] | None = None,
) -> list[ScalpSignal]:
    """Scan candles and return all matching scalp signals."""
    tf = interval.lower().replace("m", "m")
    if tf.endswith("min"):
        tf = tf.replace("min", "m")
    signals: list[ScalpSignal] = []
    if len(candles) < 2:
        return signals

    indices = [len(candles) - 1] if latest_only else list(range(1, len(candles)))
    vol_override = min_volume_override or {}

    for spec, detector in _build_detectors():
        if pattern_ids is not None and spec.id not in pattern_ids:
            continue
        if tf and tf not in spec.timeframes and spec.timeframes:
            continue
        min_vol = vol_override.get(spec.id, spec.min_volume)

        for i in indices:
            c = candles[i]
            if min_vol > 0 and _vol(c) < min_vol:
                continue
            prev = candles[i - 1]
            sig = detector(c, prev, i, tf=tf or interval, spec=spec)
            if sig is not None:
                signals.append(sig)

    absorption_indices = [i for i in indices if i >= 2]
    signals.extend(
        _detect_absorption_trap_signals(
            candles,
            tf=tf or interval,
            pattern_ids=pattern_ids,
            indices=absorption_indices,
        )
    )

    signals.sort(key=lambda s: (s.bar_index, s.pattern_id))
    return signals
