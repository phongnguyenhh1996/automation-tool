from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import automation_tool.telegram_bot as tg


def test_send_capture_screenshots_to_log_chat_skips_when_unconfigured() -> None:
    assert (
        tg.send_capture_screenshots_to_log_chat(
            bot_token=None,
            telegram_log_chat_id="-100123",
            png_paths=[],
            header="hdr",
        )
        == 0
    )


def test_send_capture_screenshots_to_log_chat_sends_header_and_photos(
    tmp_path: Path, monkeypatch
) -> None:
    p1 = tmp_path / "20260101_120000_coinmap_XAUUSD_15m.png"
    p2 = tmp_path / "20260101_120000_tradingview_XAUUSD_5m.png"
    p1.write_bytes(b"\x89PNG\r\n\x1a\n")
    p2.write_bytes(b"\x89PNG\r\n\x1a\n")

    calls: list[str] = []

    def fake_send_message(**kwargs):
        calls.append(f"msg:{kwargs['text']}")

    def fake_send_photo(**kwargs):
        calls.append(f"photo:{kwargs['photo_path'].name}")

    monkeypatch.setattr(tg, "send_message", fake_send_message)
    monkeypatch.setattr(tg, "send_photo", fake_send_photo)

    n = tg.send_capture_screenshots_to_log_chat(
        bot_token="bot-token",
        telegram_log_chat_id="-100123",
        png_paths=[p1, p2],
        header="all: capture screenshots | stamp=20260101_120000 | 2 PNG",
    )
    assert n == 2
    assert calls == [
        "msg:all: capture screenshots | stamp=20260101_120000 | 2 PNG",
        "photo:20260101_120000_coinmap_XAUUSD_15m.png",
        "photo:20260101_120000_tradingview_XAUUSD_5m.png",
    ]


def test_send_capture_screenshots_continues_after_photo_error(
    tmp_path: Path, monkeypatch
) -> None:
    p1 = tmp_path / "a.png"
    p2 = tmp_path / "b.png"
    p1.write_bytes(b"\x89PNG\r\n\x1a\n")
    p2.write_bytes(b"\x89PNG\r\n\x1a\n")

    monkeypatch.setattr(tg, "send_message", MagicMock())
    sent: list[str] = []

    def fake_send_photo(**kwargs):
        name = kwargs["photo_path"].name
        if name == "a.png":
            raise RuntimeError("fail a")
        sent.append(name)

    monkeypatch.setattr(tg, "send_photo", fake_send_photo)

    n = tg.send_capture_screenshots_to_log_chat(
        bot_token="bot-token",
        telegram_log_chat_id="-100123",
        png_paths=[p1, p2],
        header="hdr",
    )
    assert n == 1
    assert sent == ["b.png"]
