from __future__ import annotations

from dataclasses import dataclass

from automation_tool.mt5_execute import build_request, symbol_uses_market_execution
from automation_tool.mt5_openai_parse import ParsedTrade


@dataclass
class _FakeSymbolInfo:
    name: str = "XAUUSDm"
    filling_mode: int = 2  # IOC
    # 0=request, 1=instant, 2=market (MQL5); market → không đặt price cho DEAL
    trade_exemode: int = 1


@dataclass
class _FakeTick:
    bid: float
    ask: float


class _FakeMT5:
    # minimal constants used by build_request
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 2
    ORDER_TIME_GTC = 10
    ORDER_TYPE_BUY = 20
    ORDER_TYPE_SELL = 21
    ORDER_TYPE_BUY_LIMIT = 30
    ORDER_TYPE_SELL_LIMIT = 31
    ORDER_TYPE_BUY_STOP = 32
    ORDER_TYPE_SELL_STOP = 33
    ORDER_FILLING_FOK = 40
    ORDER_FILLING_IOC = 41
    ORDER_FILLING_RETURN = 42
    SYMBOL_TRADE_EXECUTION_MARKET = 2

    @dataclass
    class _TerminalInfo:
        connected: bool = True

    def terminal_info(self):
        return self._TerminalInfo(connected=True)

    def symbol_info(self, sym: str):
        return _FakeSymbolInfo(name=sym)

    def symbol_select(self, sym: str, _enable: bool):
        return True

    def symbol_info_tick(self, sym: str):
        return _FakeTick(bid=2649.9, ask=2650.1)


def test_build_request_uses_tp2_when_present_for_pending() -> None:
    mt5 = _FakeMT5()
    t = ParsedTrade(
        symbol="XAUUSDm",
        side="BUY",
        kind="LIMIT",
        price=2600.0,
        sl=2590.0,
        tp1=2610.0,
        tp2=2620.0,
        lot=0.02,
        raw_line="BUY LIMIT 2600.0 | SL 2590.0 | TP1 2610.0 | TP2 2620.0 | Lot 0.02",
    )
    req = build_request(mt5, t)
    assert req["tp"] == 2620.0


def test_build_request_falls_back_to_tp1_when_tp2_missing_for_pending() -> None:
    mt5 = _FakeMT5()
    t = ParsedTrade(
        symbol="XAUUSDm",
        side="BUY",
        kind="LIMIT",
        price=2600.0,
        sl=2590.0,
        tp1=2610.0,
        tp2=None,
        lot=0.02,
        raw_line="BUY LIMIT 2600.0 | SL 2590.0 | TP1 2610.0 | Lot 0.02",
    )
    req = build_request(mt5, t)
    assert req["tp"] == 2610.0


def test_build_request_uses_tp2_when_present_for_market() -> None:
    mt5 = _FakeMT5()
    t = ParsedTrade(
        symbol="XAUUSDm",
        side="SELL",
        kind="MARKET",
        price=None,
        sl=2660.0,
        tp1=2640.0,
        tp2=2630.0,
        lot=0.02,
        raw_line="SELL MARKET | SL 2660.0 | TP1 2640.0 | TP2 2630.0 | Lot 0.02",
    )
    req = build_request(mt5, t)
    assert req["tp"] == 2630.0


def test_symbol_uses_market_execution_detects_trade_exemode() -> None:
    mt5 = _FakeMT5()
    mt5._sym_info = _FakeSymbolInfo(trade_exemode=2)  # type: ignore[attr-defined]

    def _si(sym: str):  # noqa: ARG001
        return mt5._sym_info

    mt5.symbol_info = _si  # type: ignore[method-assign]
    assert symbol_uses_market_execution(mt5, "XAUUSDm") is True


def test_build_request_market_omits_price_when_market_execution() -> None:
    mt5 = _FakeMT5()
    mt5._sym_info = _FakeSymbolInfo(trade_exemode=2)  # type: ignore[attr-defined]

    def _si(sym: str):  # noqa: ARG001
        return mt5._sym_info

    mt5.symbol_info = _si  # type: ignore[method-assign]
    t = ParsedTrade(
        symbol="XAUUSDm",
        side="BUY",
        kind="MARKET",
        price=None,
        sl=2590.0,
        tp1=2610.0,
        tp2=None,
        lot=0.01,
        raw_line="BUY MARKET | SL 2590.0 | TP1 2610.0 | Lot 0.01",
    )
    req = build_request(mt5, t)
    assert "price" not in req
    assert req["action"] == mt5.TRADE_ACTION_DEAL


def test_build_request_market_includes_price_when_not_market_execution() -> None:
    mt5 = _FakeMT5()
    t = ParsedTrade(
        symbol="XAUUSDm",
        side="BUY",
        kind="MARKET",
        price=None,
        sl=2590.0,
        tp1=2610.0,
        tp2=None,
        lot=0.01,
        raw_line="BUY MARKET | SL 2590.0 | TP1 2610.0 | Lot 0.01",
    )
    req = build_request(mt5, t)
    assert req["price"] == 2650.1  # ask from _FakeTick

