from __future__ import annotations

from types import SimpleNamespace

import pytest

from automation_tool.telegram_listen import (
    TelegramListenParams,
    _ASK_HIGH_FOLLOWUP_MODEL,
    _explain_followup_model,
    run_telegram_listener,
)


def test_explain_followup_model_uses_mini_even_when_override_is_set() -> None:
    assert _explain_followup_model("gpt-5.4") == "gpt-5.4-mini"


def test_ask_high_model_is_full_gpt_54() -> None:
    assert _ASK_HIGH_FOLLOWUP_MODEL == "gpt-5.4"


def test_tim_scalp_command_starts_update_scalp_runner(monkeypatch) -> None:
    threads: list[dict] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "ok": True,
                "result": [
                    {
                        "update_id": 123,
                        "message": {
                            "message_id": 456,
                            "chat": {"id": 789},
                            "text": "/tim-scalp",
                        },
                    }
                ],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            self.calls = 0

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args) -> None:
            return None

        def get(self, *args, **kwargs) -> FakeResponse:
            self.calls += 1
            if self.calls > 1:
                raise KeyboardInterrupt
            return FakeResponse()

    class FakeThread:
        def __init__(self, *, target, kwargs, daemon, name) -> None:
            threads.append(
                {
                    "target": target,
                    "kwargs": kwargs,
                    "daemon": daemon,
                    "name": name,
                }
            )

        def start(self) -> None:
            return None

    monkeypatch.setattr("automation_tool.telegram_listen.httpx.Client", FakeClient)
    monkeypatch.setattr("automation_tool.telegram_listen.threading.Thread", FakeThread)

    settings = SimpleNamespace(
        telegram_bot_token="token",
        telegram_listen_chat_id="789",
        telegram_chat_id="789",
        telegram_parse_mode=None,
    )

    with pytest.raises(KeyboardInterrupt):
        run_telegram_listener(
            settings=settings,
            params=TelegramListenParams(update_main_symbol="xauusd"),
        )

    assert len(threads) == 1
    assert threads[0]["name"] == "telegram-update-scalp-runner"
    assert threads[0]["kwargs"]["update_main_symbol"] == "XAUUSD"
    assert threads[0]["kwargs"]["trigger_message_id"] == 456
