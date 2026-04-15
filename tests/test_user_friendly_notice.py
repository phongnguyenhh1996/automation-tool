"""Tests for TELEGRAM_PYTHON_BOT_CHAT_ID helper (non-tech notices)."""

from __future__ import annotations

from unittest.mock import patch

from automation_tool.telegram_bot import send_user_friendly_notice


def test_send_user_friendly_notice_skips_empty_chat_id() -> None:
    with patch("automation_tool.telegram_bot.send_message") as sm:
        send_user_friendly_notice(bot_token="tok", chat_id="", title="Hello")
        sm.assert_not_called()


def test_send_user_friendly_notice_skips_whitespace_chat_id() -> None:
    with patch("automation_tool.telegram_bot.send_message") as sm:
        send_user_friendly_notice(bot_token="tok", chat_id="   ", title="Hello")
        sm.assert_not_called()


def test_send_user_friendly_notice_skips_empty_title_and_body() -> None:
    with patch("automation_tool.telegram_bot.send_message") as sm:
        send_user_friendly_notice(bot_token="tok", chat_id="-1001", title="", body="")
        sm.assert_not_called()


def test_send_user_friendly_notice_calls_send_message_with_plain_text() -> None:
    with patch("automation_tool.telegram_bot.send_message") as sm:
        send_user_friendly_notice(
            bot_token="tok",
            chat_id="-1003925067947",
            title="Tiêu đề",
            body="Dòng một\nDòng hai",
        )
        sm.assert_called_once()
        kw = sm.call_args.kwargs
        assert kw["bot_token"] == "tok"
        assert kw["chat_id"] == "-1003925067947"
        assert kw["parse_mode"] is None
        text = kw["text"]
        assert "🔔 Bước quan trọng" in text
        assert "Tiêu đề" in text
        assert "Dòng một" in text
        assert "Dòng hai" in text
