from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from automation_tool import mt5_manage


def test_mt5_api_helpers_pass_credentials_by_keyword(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    terminal_path = "/tmp/mt5-primary/terminal64.exe"

    class FakeMT5:
        def orders_get(self) -> list[Any]:
            return []

        def positions_get(self, *args: Any, **kwargs: Any) -> list[Any]:
            return []

        def shutdown(self) -> None:
            pass

    def fake_init(
        terminal_path: str | None = None,
        login: int | None = None,
        password: str | None = None,
        server: str | None = None,
    ) -> FakeMT5:
        calls.append(
            {
                "terminal_path": terminal_path,
                "login": login,
                "password": password,
                "server": server,
            }
        )
        assert terminal_path == "/tmp/mt5-primary/terminal64.exe"
        assert login == 123456
        assert password == "secret"
        assert server == "broker"
        return FakeMT5()

    monkeypatch.setattr(mt5_manage, "_mt5_init", fake_init)
    monkeypatch.setattr(mt5_manage, "_mt5_init_current_terminal", lambda: SimpleNamespace())

    assert (
        mt5_manage.mt5_latest_position_ticket(
            "XAUUSD",
            terminal_path=terminal_path,
            login=123456,
            password="secret",
            server="broker",
        )
        is None
    )
    assert (
        mt5_manage.mt5_ticket_still_open(
            42,
            terminal_path=terminal_path,
            login=123456,
            password="secret",
            server="broker",
        )[0]
        is False
    )
    assert (
        mt5_manage.mt5_ticket_is_open_position(
            42,
            terminal_path=terminal_path,
            login=123456,
            password="secret",
            server="broker",
        )[0]
        is False
    )
    assert (
        mt5_manage.mt5_ticket_status_for_cutoff(
            42,
            terminal_path=terminal_path,
            login=123456,
            password="secret",
            server="broker",
        )[0]
        == "none"
    )

    assert len(calls) == 4


def test_mt5_close_position_partial_closes_half_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeMT5:
        TRADE_ACTION_DEAL = 1
        POSITION_TYPE_BUY = 0
        ORDER_TYPE_SELL = 1
        ORDER_TIME_GTC = 0
        TRADE_RETCODE_DONE = 10009

        def __init__(self) -> None:
            self.requests: list[dict[str, Any]] = []

        def positions_get(self) -> list[Any]:
            return [
                SimpleNamespace(
                    ticket=123,
                    symbol="XAUUSDm",
                    volume=0.02,
                    type=self.POSITION_TYPE_BUY,
                    magic=222,
                )
            ]

        def symbol_info(self, _symbol: str) -> Any:
            return SimpleNamespace(volume_min=0.01, volume_step=0.01)

        def symbol_info_tick(self, _symbol: str) -> Any:
            return SimpleNamespace(bid=2650.0, ask=2650.2)

        def order_send(self, request: dict[str, Any]) -> Any:
            self.requests.append(request)
            return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE)

    fake = FakeMT5()
    monkeypatch.setattr(mt5_manage, "_mt5_init", lambda *a, **k: fake)
    monkeypatch.setattr(mt5_manage, "_filling_for_symbol", lambda _mt5, _sym: 7)
    monkeypatch.setattr(mt5_manage, "symbol_uses_market_execution", lambda _mt5, _sym: False)

    result = mt5_manage.mt5_close_position_partial(
        123,
        fraction=0.5,
        expected_initial_volume=0.02,
        terminal_path="/tmp/metatrader64.exe",
    )

    assert result.ok is True
    assert result.kind == "position"
    assert fake.requests == [
        {
            "action": fake.TRADE_ACTION_DEAL,
            "symbol": "XAUUSDm",
            "volume": 0.01,
            "type": fake.ORDER_TYPE_SELL,
            "position": 123,
            "deviation": 20,
            "magic": 222,
            "comment": "tp1-partial-close",
            "type_time": fake.ORDER_TIME_GTC,
            "type_filling": 7,
            "price": 2650.0,
        }
    ]


def test_mt5_close_position_partial_skips_when_runner_already_remaining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMT5:
        TRADE_ACTION_DEAL = 1
        POSITION_TYPE_BUY = 0
        ORDER_TYPE_SELL = 1
        ORDER_TIME_GTC = 0
        TRADE_RETCODE_DONE = 10009

        def __init__(self) -> None:
            self.requests: list[dict[str, Any]] = []

        def positions_get(self) -> list[Any]:
            return [
                SimpleNamespace(
                    ticket=123,
                    symbol="XAUUSDm",
                    volume=0.01,
                    type=self.POSITION_TYPE_BUY,
                    magic=222,
                )
            ]

        def symbol_info(self, _symbol: str) -> Any:
            return SimpleNamespace(volume_min=0.01, volume_step=0.01)

    fake = FakeMT5()
    monkeypatch.setattr(mt5_manage, "_mt5_init", lambda *a, **k: fake)

    result = mt5_manage.mt5_close_position_partial(
        123,
        fraction=0.5,
        expected_initial_volume=0.02,
        terminal_path="/tmp/metatrader64.exe",
    )

    assert result.ok is True
    assert "chốt một phần" in result.message.lower()
    assert fake.requests == []
