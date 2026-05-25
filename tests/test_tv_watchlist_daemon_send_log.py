from __future__ import annotations

from unittest.mock import MagicMock

import automation_tool.tv_watchlist_daemon as daemon


def test_send_log_reports_local_warning_when_telegram_send_fails(monkeypatch) -> None:
    reported: list[tuple[str, str, BaseException]] = []

    def fake_send_message(**_kwargs):
        raise RuntimeError("telegram down")

    monkeypatch.setattr("automation_tool.tv_watchlist_daemon.send_message", fake_send_message)
    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon._report_telegram_send_failure",
        lambda context, text, exc: reported.append((context, text, exc)),
    )

    settings = MagicMock(telegram_log_chat_id="-100123", telegram_bot_token="bot-token")

    daemon._send_log(settings, "[daemon-plan] watch | hello")

    assert len(reported) == 1
    context, text, exc = reported[0]
    assert context == "tv_watchlist_daemon._send_log"
    assert text == "[daemon-plan] watch | hello"
    assert str(exc) == "telegram down"
