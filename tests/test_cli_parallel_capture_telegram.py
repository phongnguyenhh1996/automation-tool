from __future__ import annotations

import threading
import time
from pathlib import Path

from automation_tool.cli import _run_capture_telegram_log_parallel_with


def test_run_capture_telegram_log_parallel_with_runs_both(monkeypatch, tmp_path: Path) -> None:
    p = tmp_path / "a.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")

    tg_started = threading.Event()
    work_started = threading.Event()
    overlap = threading.Event()

    def fake_tg(**kwargs):
        tg_started.set()
        work_started.wait(timeout=2.0)
        overlap.set()
        return 1

    def work():
        work_started.set()
        tg_started.wait(timeout=2.0)
        overlap.set()
        return "openai-done"

    monkeypatch.setattr(
        "automation_tool.cli.send_capture_screenshots_to_log_chat",
        fake_tg,
    )

    n_sent, result = _run_capture_telegram_log_parallel_with(
        bot_token="tok",
        telegram_log_chat_id="-100",
        png_paths=[p],
        header="hdr",
        work_fn=work,
    )

    assert n_sent == 1
    assert result == "openai-done"
    assert overlap.is_set()


def test_run_capture_telegram_log_parallel_with_faster_than_sequential(
    monkeypatch, tmp_path: Path
) -> None:
    p = tmp_path / "a.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    delay = 0.15

    def slow_tg(**kwargs):
        time.sleep(delay)
        return 1

    def slow_work():
        time.sleep(delay)
        return 42

    monkeypatch.setattr(
        "automation_tool.cli.send_capture_screenshots_to_log_chat",
        slow_tg,
    )

    t0 = time.monotonic()
    n_sent, result = _run_capture_telegram_log_parallel_with(
        bot_token="tok",
        telegram_log_chat_id="-100",
        png_paths=[p],
        header="hdr",
        work_fn=slow_work,
    )
    elapsed = time.monotonic() - t0

    assert n_sent == 1
    assert result == 42
    # Song song: ~delay; tuần tự sẽ ~2*delay
    assert elapsed < delay * 1.75
