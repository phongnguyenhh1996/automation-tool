"""Tests for footprint-only scalp backtest strategies."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "scalp_footprint"))

from backtest_engine import TradeOutcome, run_backtest, summarize_results  # noqa: E402
from footprint_metrics import (  # noqa: E402
    candle_poc,
    delta_weakening_same_sign,
    enrich_document,
)
from strategies_footprint import detect_all_footprint_strategies  # noqa: E402


def _level(price: float, buy: int, sell: int) -> dict:
    return {"price": price, "buy": buy, "sell": sell}


def _candle(
    *,
    o: float,
    h: float,
    l: float,
    c: float,
    delta: float,
    footprint: list[dict],
    time: str = "",
) -> dict:
    buy = sum(lvl["buy"] for lvl in footprint)
    sell = sum(lvl["sell"] for lvl in footprint)
    return {
        "time_gmt7": time,
        "ohlc": {"open": o, "high": h, "low": l, "close": c, "volume": buy + sell},
        "bar_flow": {"delta": delta, "volume": buy + sell, "buy_volume": buy, "sell_volume": sell},
        "footprint": footprint,
    }


def test_delta_weakening_positive() -> None:
    assert delta_weakening_same_sign([220, 110, 35], min_first=30.0)
    assert not delta_weakening_same_sign([35, 110, 220], min_first=30.0)


def test_candle_poc() -> None:
    c = _candle(o=1, h=2, l=0, c=1.5, delta=0, footprint=[_level(1.0, 5, 5), _level(1.1, 20, 3)])
    assert candle_poc(c) == 1.1


from patterns import detect_patterns  # noqa: E402


def test_detect_patterns_absorption_trap_short_synthetic() -> None:
    """detect_patterns wires absorption_trap via patterns module."""
    fp_trap = [
        _level(100.3, 50, 2),
        _level(100.2, 40, 3),
        _level(100.1, 10, 10),
        _level(100.0, 8, 8),
        _level(99.9, 12, 5),
    ]
    fp_prev = [_level(100.0, 10, 10), _level(99.9, 8, 8), _level(99.8, 6, 6)]
    fp_confirm = [_level(100.0, 5, 20), _level(99.9, 8, 15), _level(99.8, 10, 12), _level(99.7, 12, 10)]

    doc = {
        "fp_day": {"tick_size": 1, "price_precision": 1},
        "candles": [
            _candle(o=99.8, h=100.0, l=99.7, c=99.9, delta=-10, footprint=fp_prev, time="t0"),
            _candle(o=99.9, h=100.3, l=99.8, c=100.0, delta=55, footprint=fp_trap, time="t1"),
            _candle(o=100.0, h=100.0, l=99.5, c=99.6, delta=-20, footprint=fp_confirm, time="t2"),
        ],
    }
    candles = enrich_document(doc)
    signals = detect_patterns(
        candles,
        interval="5m",
        pattern_ids={"absorption_trap_short"},
        latest_only=True,
    )
    assert len(signals) == 1
    assert signals[0].pattern_id == "absorption_trap_short"


def test_absorption_trap_short_synthetic() -> None:
    """Upthrust with ASK absorption at high, bear confirm bar."""
    # High level: heavy bid (passive) absorbing aggressive sells at the high wick
    fp_trap = [
        _level(100.3, 50, 2),
        _level(100.2, 40, 3),
        _level(100.1, 10, 10),
        _level(100.0, 8, 8),
        _level(99.9, 12, 5),
    ]
    fp_prev = [_level(100.0, 10, 10), _level(99.9, 8, 8), _level(99.8, 6, 6)]
    fp_confirm = [_level(100.0, 5, 20), _level(99.9, 8, 15), _level(99.8, 10, 12), _level(99.7, 12, 10)]

    doc = {
        "fp_day": {"tick_size": 1, "price_precision": 1},
        "candles": [
            _candle(o=99.8, h=100.0, l=99.7, c=99.9, delta=-10, footprint=fp_prev, time="t0"),
            _candle(o=99.9, h=100.3, l=99.8, c=100.0, delta=55, footprint=fp_trap, time="t1"),
            _candle(o=100.0, h=100.0, l=99.5, c=99.6, delta=-20, footprint=fp_confirm, time="t2"),
            _candle(o=99.6, h=99.7, l=99.0, c=99.1, delta=-30, footprint=fp_confirm, time="t3"),
        ],
    }
    candles = enrich_document(doc)
    signals = detect_all_footprint_strategies(candles, interval="5m", strategy_ids={"absorption_trap"})
    assert len(signals) >= 1
    trap_signals = [s for s in signals if s.pattern_id == "absorption_trap_short"]
    assert trap_signals

    results = run_backtest(candles, trap_signals[:1], max_bars=3)
    assert results[0].outcome in (TradeOutcome.WIN, TradeOutcome.TIMEOUT, TradeOutcome.LOSS)


def test_backtest_on_live_footprint_if_present() -> None:
    path = Path("data/XAUUSD/charts/footprint_images/footprint_combined_5m.json")
    if not path.is_file():
        pytest.skip("no footprint data")
    doc = json.loads(path.read_text(encoding="utf-8"))
    candles = enrich_document(doc)
    signals = detect_all_footprint_strategies(candles, interval="5m")
    results = run_backtest(candles, signals, max_bars=12)
    summary = summarize_results(results)
    assert "overall" in summary
    assert summary["trade_count"] == len(signals)
