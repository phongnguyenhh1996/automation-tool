from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from automation_tool.mt5_manage import mt5_chinh_trade_line_inplace
from automation_tool.mt5_openai_parse import ParsedTrade


@dataclass
class _FakeTerminalInfo:
    connected: bool = True


class _FakeMT5:
    TRADE_ACTION_SLTP = 1
    TRADE_ACTION_MODIFY = 2
    ORDER_TYPE_BUY_LIMIT = 10
    ORDER_TYPE_SELL_LIMIT = 11
    ORDER_TYPE_BUY_STOP = 12
    ORDER_TYPE_SELL_STOP = 13
    TRADE_RETCODE_DONE = 10009

    def __init__(self, *, orders=None, positions=None):
        self._orders = orders or []
        self._positions = positions or []
        self.requests: list[dict] = []

    def terminal_info(self):
        return _FakeTerminalInfo()

    def symbol_info(self, sym):
        return SimpleNamespace(name=sym)

    def symbol_select(self, _sym, _enable):
        return True

    def orders_get(self):
        return self._orders

    def positions_get(self):
        return self._positions

    def order_send(self, req):
        self.requests.append(req)
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE)


def _trade_with_tp2(kind: str = "LIMIT") -> ParsedTrade:
    return ParsedTrade(
        symbol="XAUUSDm",
        side="BUY",
        kind=kind,  # type: ignore[arg-type]
        price=2600.0 if kind != "MARKET" else None,
        sl=2590.0,
        tp1=2610.0,
        tp2=2620.0,
        lot=0.02,
        raw_line="BUY LIMIT 2600.0 | SL 2590.0 | TP1 2610.0 | TP2 2620.0 | Lot 0.02",
    )


def test_chinh_trade_line_uses_tp2_when_modifying_position(monkeypatch) -> None:
    mt5 = _FakeMT5(positions=[SimpleNamespace(ticket=123, symbol="XAUUSDm")])
    monkeypatch.setattr("automation_tool.mt5_manage._mt5_init", lambda *a, **k: mt5)

    result = mt5_chinh_trade_line_inplace(
        123,
        _trade_with_tp2(kind="MARKET"),
        terminal_path="/tmp/metatrader64.exe",
    )

    assert result.ok is True
    assert mt5.requests[0]["action"] == mt5.TRADE_ACTION_SLTP
    assert mt5.requests[0]["tp"] == 2620.0


def test_chinh_trade_line_uses_tp2_when_modifying_pending(monkeypatch) -> None:
    mt5 = _FakeMT5(
        orders=[
            SimpleNamespace(
                ticket=123,
                symbol="XAUUSDm",
                type=_FakeMT5.ORDER_TYPE_BUY_LIMIT,
                price_open=2600.0,
                volume_initial=0.02,
            )
        ]
    )
    monkeypatch.setattr("automation_tool.mt5_manage._mt5_init", lambda *a, **k: mt5)

    result = mt5_chinh_trade_line_inplace(
        123,
        _trade_with_tp2(kind="LIMIT"),
        terminal_path="/tmp/metatrader64.exe",
    )

    assert result.ok is True
    assert mt5.requests[0]["action"] == mt5.TRADE_ACTION_MODIFY
    assert mt5.requests[0]["tp"] == 2620.0
