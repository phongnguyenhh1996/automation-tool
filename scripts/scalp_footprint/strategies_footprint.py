"""Footprint backtest wrapper — absorption trap (delegates to absorption_trap + patterns)."""

from __future__ import annotations

from typing import Any

from absorption_trap import scan_absorption_traps
from footprint_metrics import enrich_document
from patterns import Direction, EntryType, ScalpSignal

DEFAULT_STRATEGY_ID = "absorption_trap"


class StrategyTier:
    A_PLUS = "A+"


def detect_all_footprint_strategies(
    candles: list[dict[str, Any]],
    *,
    interval: str,
    strategy_ids: set[str] | None = None,
) -> list[ScalpSignal]:
    """Backtest scan — returns ScalpSignal list (same shape as detect_patterns)."""
    if strategy_ids is not None and DEFAULT_STRATEGY_ID not in strategy_ids:
        return []

    tf = interval.lower().replace("min", "m")
    enriched = enrich_document({"candles": candles}, for_backtest=True)
    setups = scan_absorption_traps(enriched)
    signals: list[ScalpSignal] = []

    for setup in setups:
        trap_c = enriched[setup.trap_index]
        confirm_c = enriched[setup.confirm_index]
        pid = (
            "absorption_trap_long"
            if setup.direction == Direction.LONG
            else "absorption_trap_short"
        )
        signals.append(
            ScalpSignal(
                pattern_id=pid,
                pattern_name=(
                    "Absorption Trap Long"
                    if setup.direction == Direction.LONG
                    else "Absorption Trap Short"
                ),
                direction=setup.direction,
                entry_type=EntryType.MARKET,
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
        )

    signals.sort(key=lambda s: s.bar_index)
    return _dedupe_signals(signals)


# Backtest engine expects FootprintSignal-like objects with .signal, .to_dict etc.
# Re-export ScalpSignal as FootprintSignal alias for backtest_engine compatibility.
FootprintSignal = ScalpSignal


def _dedupe_signals(signals: list[ScalpSignal]) -> list[ScalpSignal]:
    by_key: dict[tuple[int, str], ScalpSignal] = {}
    for sig in signals:
        key = (sig.bar_index, sig.direction.value)
        by_key[key] = sig
    return sorted(by_key.values(), key=lambda s: s.bar_index)
