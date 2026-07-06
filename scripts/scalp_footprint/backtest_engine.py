"""Simulate SL/TP outcomes for footprint scalp signals."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from patterns import Direction, ScalpSignal


class TradeOutcome(str, Enum):
    WIN = "WIN"
    LOSS = "LOSS"
    TIMEOUT = "TIMEOUT"
    NO_FILL = "NO_FILL"


@dataclass
class TradeResult:
    signal: ScalpSignal
    outcome: TradeOutcome
    exit_bar: int
    exit_price: float
    pnl: float
    bars_held: int
    detail: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.signal.to_dict(),
            "outcome": self.outcome.value,
            "exit_bar": self.exit_bar,
            "exit_price": round(self.exit_price, 2),
            "pnl": round(self.pnl, 2),
            "bars_held": self.bars_held,
            "detail": self.detail,
        }


def simulate_trade(
    signal: ScalpSignal,
    candles: list[dict[str, Any]],
    *,
    max_bars: int = 12,
) -> TradeResult:
    entry_bar = signal.bar_index
    if entry_bar >= len(candles):
        return TradeResult(
            signal=signal,
            outcome=TradeOutcome.NO_FILL,
            exit_bar=entry_bar,
            exit_price=signal.entry_price,
            pnl=0.0,
            bars_held=0,
            detail="entry bar out of range",
        )

    entry = signal.entry_price
    sl = signal.stop_loss
    tp = signal.take_profit_low
    is_long = signal.direction == Direction.LONG

    end = min(len(candles) - 1, entry_bar + max_bars)
    for j in range(entry_bar, end + 1):
        h, l = candles[j]["ohlc"]["high"], candles[j]["ohlc"]["low"]
        if is_long:
            sl_hit = l <= sl
            tp_hit = h >= tp
        else:
            sl_hit = h >= sl
            tp_hit = l <= tp

        if sl_hit and tp_hit:
            # Same bar: assume worst case (SL first) for conservative backtest
            pnl = tp - entry if is_long else entry - tp
            # If open gaps through SL, use SL
            o = candles[j]["ohlc"]["open"]
            if (is_long and o <= sl) or (not is_long and o >= sl):
                pnl = sl - entry if is_long else entry - sl
                return TradeResult(
                    signal=signal,
                    outcome=TradeOutcome.LOSS,
                    exit_bar=j,
                    exit_price=sl,
                    pnl=pnl,
                    bars_held=j - entry_bar,
                    detail="SL hit (same bar as TP)",
                )
            return TradeResult(
                signal=signal,
                outcome=TradeOutcome.WIN,
                exit_bar=j,
                exit_price=tp,
                pnl=pnl,
                bars_held=j - entry_bar,
                detail="TP hit (same bar as SL)",
            )

        if sl_hit:
            pnl = sl - entry if is_long else entry - sl
            return TradeResult(
                signal=signal,
                outcome=TradeOutcome.LOSS,
                exit_bar=j,
                exit_price=sl,
                pnl=pnl,
                bars_held=j - entry_bar,
                detail="stop loss",
            )

        if tp_hit:
            pnl = tp - entry if is_long else entry - tp
            return TradeResult(
                signal=signal,
                outcome=TradeOutcome.WIN,
                exit_bar=j,
                exit_price=tp,
                pnl=pnl,
                bars_held=j - entry_bar,
                detail="take profit",
            )

    last = candles[end]
    exit_px = last["ohlc"]["close"]
    pnl = exit_px - entry if is_long else entry - exit_px
    return TradeResult(
        signal=signal,
        outcome=TradeOutcome.TIMEOUT,
        exit_bar=end,
        exit_price=exit_px,
        pnl=pnl,
        bars_held=end - entry_bar,
        detail=f"timeout after {max_bars} bars",
    )


def run_backtest(
    candles: list[dict[str, Any]],
    signals: list[ScalpSignal],
    *,
    max_bars: int = 12,
) -> list[TradeResult]:
    return [simulate_trade(sig, candles, max_bars=max_bars) for sig in signals]


def summarize_results(results: list[TradeResult]) -> dict[str, Any]:
    by_strategy: dict[str, list[TradeResult]] = {}
    for r in results:
        sid = r.signal.pattern_id
        if sid.startswith("absorption_trap"):
            sid = "absorption_trap"
        by_strategy.setdefault(sid, []).append(r)

    overall = _stats_block(results)
    per_strategy = {sid: _stats_block(rows) for sid, rows in sorted(by_strategy.items())}
    return {"overall": overall, "by_strategy": per_strategy, "trade_count": len(results)}


def _stats_block(results: list[TradeResult]) -> dict[str, Any]:
    if not results:
        return {
            "trades": 0,
            "filled": 0,
            "no_fill": 0,
            "wins": 0,
            "losses": 0,
            "timeouts": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "avg_bars_held": 0.0,
        }

    no_fill = sum(1 for r in results if r.outcome == TradeOutcome.NO_FILL)
    wins = sum(1 for r in results if r.outcome == TradeOutcome.WIN)
    losses = sum(1 for r in results if r.outcome == TradeOutcome.LOSS)
    timeouts = sum(1 for r in results if r.outcome == TradeOutcome.TIMEOUT)
    filled = [r for r in results if r.outcome != TradeOutcome.NO_FILL]
    win_rate = wins / len(filled) if filled else 0.0
    total_pnl = sum(r.pnl for r in filled)

    return {
        "trades": len(results),
        "filled": len(filled),
        "no_fill": no_fill,
        "wins": wins,
        "losses": losses,
        "timeouts": timeouts,
        "win_rate": round(win_rate * 100, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / len(filled), 2) if filled else 0.0,
        "avg_bars_held": round(sum(r.bars_held for r in filled) / len(filled), 1) if filled else 0.0,
    }
