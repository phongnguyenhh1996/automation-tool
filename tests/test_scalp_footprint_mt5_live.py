"""Tests for live MT5 XAU price used in scalp executor."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "scalp_footprint"))

from scalp_mt5_live import (  # noqa: E402
    attach_scalp_sltp_to_position,
    build_scalp_market_entry_only,
    build_scalp_trade_live,
    resolve_xau_symbol_on_mt5,
    xau_symbol_candidates,
)
from automation_tool.mt5_execute import MT5ExecutionResult  # noqa: E402


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


def test_build_scalp_market_entry_only_skips_tick(monkeypatch) -> None:
    def fail_tick(*_a, **_k):
        raise AssertionError("symbol_info_tick should not be called")

    class FakeMt5:
        symbol_info_tick = fail_tick

    monkeypatch.setattr(
        "scalp_mt5_live.resolve_xau_symbol_on_mt5",
        lambda *_a, **_k: ("XAUUSDm", None),
    )
    parsed = {"side": "BUY", "raw_line": "SCALP_EXEC|..."}
    trade = build_scalp_market_entry_only(parsed, lot=0.01, mt5=FakeMt5())
    assert trade.symbol == "XAUUSDm"
    assert trade.sl == 0.0
    assert trade.tp1 == 0.0
    assert trade.kind == "MARKET"


def test_attach_scalp_sltp_from_price_open(monkeypatch) -> None:
    pos = SimpleNamespace(ticket=99, symbol="XAUUSDm", price_open=3350.0)
    sent: list[dict] = []

    class FakeMt5:
        TRADE_ACTION_SLTP = 6

        def positions_get(self, symbol=None):
            return [pos]

        def order_send(self, req):
            sent.append(req)
            return SimpleNamespace(retcode=10009)

    monkeypatch.setattr(
        "scalp_mt5_live._ensure_symbol",
        lambda _mt5, sym: (sym, None),
    )
    monkeypatch.setattr(
        "scalp_mt5_live._is_mt5_trade_success_retcode",
        lambda _mt5, rc: rc == 10009,
    )

    entry, sl, tp = attach_scalp_sltp_to_position(
        FakeMt5(),
        position_ticket=99,
        side="BUY",
        sl_points=4,
        tp_points=4,
    )
    assert entry == 3350.0
    assert sl == 3346.0
    assert tp == 3354.0
    assert sent[0]["position"] == 99
    assert sent[0]["sl"] == 3346.0
    assert sent[0]["tp"] == 3354.0


def test_execute_scalp_market_fast_two_phase(monkeypatch) -> None:
    fake_mt5 = object()
    entry_trade_holder: list = []

    def fake_build_entry(parsed, *, lot, mt5, account_symbol_map=None):
        from automation_tool.mt5_openai_parse import ParsedTrade

        t = ParsedTrade(
            symbol="XAUUSDm",
            side="BUY",
            kind="MARKET",
            price=None,
            sl=0.0,
            tp1=0.0,
            tp2=None,
            lot=lot,
            raw_line="",
        )
        entry_trade_holder.append(t)
        return t

    def fake_execute(trade, **kwargs):
        assert trade.sl == 0.0
        assert kwargs.get("take_profit_override") == 0.0
        return MT5ExecutionResult(ok=True, message="MARKET OK", order=12345)

    def fake_find(mt5, *, symbol, magic=None, preferred_ticket=None):
        assert preferred_ticket == 12345
        return 777

    def fake_attach(mt5, *, position_ticket, side, sl_points, tp_points):
        assert position_ticket == 777
        return 3350.5, 3346.5, 3354.5

    monkeypatch.setattr(
        "automation_tool.mt5_execute.ensure_mt5_session",
        lambda **kw: SimpleNamespace(ok=True, mt5=fake_mt5, message=""),
    )
    monkeypatch.setattr("scalp_mt5_live.build_scalp_market_entry_only", fake_build_entry)
    monkeypatch.setattr("scalp_mt5_live.execute_trade", fake_execute)
    monkeypatch.setattr("scalp_mt5_live.find_scalp_position_ticket", fake_find)
    monkeypatch.setattr("scalp_mt5_live.attach_scalp_sltp_to_position", fake_attach)

    from scalp_mt5_live import execute_scalp_market_fast

    ex, entry, sl, tp = execute_scalp_market_fast(
        {"side": "BUY", "pattern_id": "p1"},
        lot=0.01,
        dry_run=False,
        sl_points=4,
        tp_points=4,
    )
    assert ex.ok
    assert entry == 3350.5
    assert sl == 3346.5
    assert tp == 3354.5
    assert "SLTP" in ex.message


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
