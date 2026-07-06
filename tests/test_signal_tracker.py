"""Tests for scalp signal TP/SL tracking."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "scalp_footprint"))

from signal_tracker import (  # noqa: E402
    evaluate_open_trades,
    open_trade_from_signal,
    register_open_trades,
)


def _sig(*, time: str, entry: float, sl: float, tp: float, direction: str = "long") -> dict:
    return {
        "timeframe": "5m",
        "time_gmt7": time,
        "pattern_id": "exhaustion_long",
        "bar_index": 0,
        "direction": direction,
        "side": "BUY" if direction == "long" else "SELL",
        "entry_price": entry,
        "stop_loss": sl,
        "take_profit": [tp, tp + 4],
        "entry_hint": "test",
    }


def _candle(time: str, o: float, h: float, l: float, c: float) -> dict:
    return {"time_gmt7": time, "ohlc": {"open": o, "high": h, "low": l, "close": c}}


def test_register_and_tp_hit(tmp_path: Path) -> None:
    trades_file = tmp_path / "trades.json"
    sig = _sig(time="t0", entry=100.0, sl=98.0, tp=104.0)
    register_open_trades(trades_file, [sig])

    candles = [
        _candle("t0", 100, 101, 99, 100),
        _candle("t1", 100, 105, 99, 104),
    ]
    closed = evaluate_open_trades(trades_file, candles, interval="5m", max_bars=12)
    assert len(closed) == 1
    assert closed[0]["status"] == "WIN"
    assert closed[0]["pnl"] == 4.0
    assert closed[0]["bars_held"] == 1


def test_no_tp_on_signal_bar_before_next_bar(tmp_path: Path) -> None:
    """High on signal bar must not count — entry is at close / next bar."""
    trades_file = tmp_path / "trades.json"
    sig = _sig(time="t0", entry=100.0, sl=98.0, tp=104.0)
    register_open_trades(trades_file, [sig])
    candles = [
        _candle("t0", 100, 105, 99, 100),
        _candle("t1", 100, 101, 99, 100),
    ]
    closed = evaluate_open_trades(trades_file, candles, interval="5m", max_bars=12)
    assert closed == []


def test_sl_hit(tmp_path: Path) -> None:
    trades_file = tmp_path / "trades.json"
    trade = open_trade_from_signal(_sig(time="t0", entry=100.0, sl=98.0, tp=104.0))
    trades_file.write_text(
        '{"trades": [' + __import__("json").dumps(trade) + "]}",
        encoding="utf-8",
    )
    candles = [
        _candle("t0", 100, 101, 99, 100),
        _candle("t1", 100, 101, 97, 97),
    ]
    closed = evaluate_open_trades(trades_file, candles, interval="5m", max_bars=12)
    assert closed[0]["status"] == "LOSS"
    assert closed[0]["pnl"] == -2.0


def test_short_tp_hit(tmp_path: Path) -> None:
    trades_file = tmp_path / "trades.json"
    sig = _sig(time="t0", entry=100.0, sl=101.0, tp=95.0, direction="short")
    register_open_trades(trades_file, [sig])
    candles = [
        _candle("t0", 100, 100.5, 99, 100),
        _candle("t1", 100, 100, 94, 95),
    ]
    closed = evaluate_open_trades(trades_file, candles, interval="5m", max_bars=12)
    assert closed[0]["status"] == "WIN"
    assert closed[0]["pnl"] == 5.0
