from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from automation_tool import mt5_manage


def test_mt5_api_helpers_pass_credentials_by_keyword(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

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
        assert terminal_path is None
        assert login == 123456
        assert password == "secret"
        assert server == "broker"
        return FakeMT5()

    monkeypatch.setattr(mt5_manage, "_mt5_init", fake_init)
    monkeypatch.setattr(mt5_manage, "_mt5_init_current_terminal", lambda: SimpleNamespace())

    assert mt5_manage.mt5_latest_position_ticket("XAUUSD", login=123456, password="secret", server="broker") is None
    assert mt5_manage.mt5_ticket_still_open(42, login=123456, password="secret", server="broker")[0] is False
    assert mt5_manage.mt5_ticket_is_open_position(42, login=123456, password="secret", server="broker")[0] is False
    assert (
        mt5_manage.mt5_ticket_status_for_cutoff(42, login=123456, password="secret", server="broker")[0]
        == "none"
    )

    assert len(calls) == 4
