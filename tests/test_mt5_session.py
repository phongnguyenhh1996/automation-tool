from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from automation_tool.mt5_execute import ensure_mt5_session


def test_ensure_mt5_session_reuses_matching_terminal_and_account() -> None:
    class FakeMT5:
        def terminal_info(self) -> Any:
            return SimpleNamespace(path="/tmp/mt5-primary")

        def account_info(self) -> Any:
            return SimpleNamespace(login=123456, server="broker")

        def shutdown(self) -> None:
            raise AssertionError("shutdown should not be called for matching session")

        def initialize(self, *args: Any, **kwargs: Any) -> bool:
            raise AssertionError("initialize should not be called for matching session")

    result = ensure_mt5_session(
        terminal_path="/tmp/mt5-primary/terminal64.exe",
        login=123456,
        password="secret",
        server="broker",
        mt5=FakeMT5(),
    )

    assert result.ok
    assert result.reused
    assert not result.initialized


def test_ensure_mt5_session_waits_then_switches_when_account_mismatches() -> None:
    sleeps: list[int] = []
    events: list[str] = []

    class FakeMT5:
        def __init__(self) -> None:
            self._login = 999999

        def terminal_info(self) -> Any:
            return SimpleNamespace(path="/tmp/mt5-primary")

        def account_info(self) -> Any:
            return SimpleNamespace(login=self._login, server="broker")

        def shutdown(self) -> None:
            events.append("shutdown")

        def initialize(self, path: str, **kwargs: Any) -> bool:
            events.append(f"initialize:{path}:{kwargs['login']}:{kwargs['password']}:{kwargs['server']}")
            self._login = int(kwargs["login"])
            return True

    result = ensure_mt5_session(
        terminal_path="/tmp/mt5-primary/terminal64.exe",
        login=123456,
        password="secret",
        server="broker",
        mt5=FakeMT5(),
        sleep_fn=sleeps.append,
        delay_choice_fn=lambda values: 4,
    )

    assert result.ok
    assert not result.reused
    assert result.initialized
    assert sleeps == [4]
    assert events == [
        "shutdown",
        "initialize:/tmp/mt5-primary/terminal64.exe:123456:secret:broker",
    ]


def test_ensure_mt5_session_retries_when_verify_after_initialize_is_wrong() -> None:
    sleeps: list[int] = []

    class FakeMT5:
        def __init__(self) -> None:
            self._login = 999999
            self.initialize_calls = 0
            self.shutdown_calls = 0

        def terminal_info(self) -> Any:
            return SimpleNamespace(path="/tmp/mt5-primary")

        def account_info(self) -> Any:
            return SimpleNamespace(login=self._login, server="broker")

        def shutdown(self) -> None:
            self.shutdown_calls += 1

        def initialize(self, path: str, **kwargs: Any) -> bool:
            self.initialize_calls += 1
            if self.initialize_calls >= 2:
                self._login = int(kwargs["login"])
            return True

    mt5 = FakeMT5()
    result = ensure_mt5_session(
        terminal_path="/tmp/mt5-primary/terminal64.exe",
        login=123456,
        password="secret",
        server="broker",
        mt5=mt5,
        sleep_fn=sleeps.append,
        delay_choice_fn=lambda values: 2,
        max_attempts=2,
    )

    assert result.ok
    assert mt5.initialize_calls == 2
    assert mt5.shutdown_calls == 2
    assert sleeps == [2, 2]
