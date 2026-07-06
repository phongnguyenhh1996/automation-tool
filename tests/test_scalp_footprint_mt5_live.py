"""Tests for live MT5 XAU price used in scalp executor."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "scalp_footprint"))

from scalp_mt5_live import (  # noqa: E402
    build_scalp_trade_live,
    resolve_xau_symbol_on_mt5,
    xau_symbol_candidates,
)


def test_xau_symbol_candidates_includes_map_and_suffixes() -> None:
    cands = xau_symbol_candidates({"XAUUSD": "XAUUSDc"})
    assert cands[0] == "XAUUSDc"
    assert "XAUUSD" in cands
    assert "XAUUSDm" in cands
    assert "XAUUSDc" in cands


def test_resolve_xau_symbol_on_mt5_tries_candidates(monkeypatch) -> None:
    tried: list[str] = []

    def fake_ensure(_mt5, symbol):
        tried.append(symbol)
        if symbol == "XAUUSDm":
            return symbol, None
        return None, "missing"

    monkeypatch.setattr("scalp_mt5_live._ensure_symbol", fake_ensure)
    sym, err = resolve_xau_symbol_on_mt5(object(), None)
    assert sym == "XAUUSDm"
    assert err is None
    assert tried[0] == "XAUUSDm"


def test_build_scalp_trade_live_uses_ask_for_buy(monkeypatch) -> None:
    tick = SimpleNamespace(ask=3350.25, bid=3349.75)

    class FakeMt5:
        def symbol_info_tick(self, _sym):
            return tick

    monkeypatch.setattr(
        "scalp_mt5_live.resolve_xau_symbol_on_mt5",
        lambda *_a, **_k: ("XAUUSDm", None),
    )

    parsed = {
        "side": "BUY",
        "raw_line": "SCALP_EXEC|p|BUY|MARKET|0|0|0||5m|t|1|XAUUSD",
    }
    trade, entry = build_scalp_trade_live(
        parsed,
        lot=0.01,
        mt5=FakeMt5(),
        sl_points=4,
        tp_points=4,
    )
    assert entry == 3350.25
    assert trade.symbol == "XAUUSDm"
    assert trade.sl == 3346.25
    assert trade.tp1 == 3354.25


def test_build_scalp_trade_live_uses_bid_for_sell(monkeypatch) -> None:
    tick = SimpleNamespace(ask=3350.25, bid=3349.75)

    class FakeMt5:
        def symbol_info_tick(self, _sym):
            return tick

    monkeypatch.setattr(
        "scalp_mt5_live.resolve_xau_symbol_on_mt5",
        lambda *_a, **_k: ("XAUUSD", None),
    )

    parsed = {"side": "SELL", "raw_line": ""}
    trade, entry = build_scalp_trade_live(parsed, lot=0.01, mt5=FakeMt5())
    assert entry == 3349.75
    assert trade.sl == 3353.75
    assert trade.tp1 == 3345.75
