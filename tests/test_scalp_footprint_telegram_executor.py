"""Tests for scalp telegram executor token routing."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "scalp_footprint"))

from telegram_executor import (  # noqa: E402
    DEFAULT_SCALP_TELEGRAM_CHAT_ID,
    _listen_bot_token,
    _send_bot_token,
)


def test_listen_token_falls_back_to_send_token(monkeypatch) -> None:
    monkeypatch.delenv("SCALP_EXEC_LISTEN_BOT_TOKEN", raising=False)
    settings = MagicMock(telegram_bot_token="poster-token")
    assert _send_bot_token(settings) == "poster-token"
    assert _listen_bot_token(settings) == "poster-token"


def test_listen_token_uses_dedicated_env(monkeypatch) -> None:
    monkeypatch.setenv("SCALP_EXEC_LISTEN_BOT_TOKEN", "listener-token")
    settings = MagicMock(telegram_bot_token="poster-token")
    assert _listen_bot_token(settings) == "listener-token"
    assert _send_bot_token(settings) == "poster-token"


def test_scalp_chat_id_is_fixed() -> None:
    assert DEFAULT_SCALP_TELEGRAM_CHAT_ID == "-1004297700919"
