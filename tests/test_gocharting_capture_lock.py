from __future__ import annotations

import os

import pytest

from automation_tool import gocharting_capture_lock as lock


@pytest.fixture
def lock_path(tmp_path, monkeypatch):
    path = tmp_path / "gocharting_intraday_capture.lock"
    monkeypatch.setattr(lock, "gocharting_capture_lock_path", lambda: path)
    return path


def test_acquire_release_roundtrip(lock_path) -> None:
    assert not lock.is_gocharting_capture_in_progress()
    with lock.gocharting_capture_lock():
        assert lock.is_gocharting_capture_in_progress()
        assert lock_path.is_file()
    assert not lock.is_gocharting_capture_in_progress()
    assert not lock_path.is_file()


def test_stale_lock_removed_when_pid_dead(lock_path) -> None:
    lock_path.write_text("999999999\n", encoding="utf-8")
    lock.release_stale_gocharting_capture_lock()
    assert not lock_path.is_file()
    assert not lock.is_gocharting_capture_in_progress()


def test_wait_until_idle_returns_immediately_when_free(monkeypatch) -> None:
    calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        calls.append(seconds)

    monkeypatch.setattr(lock.time, "sleep", fake_sleep)
    lock.wait_until_gocharting_capture_idle(sleep_s=60)
    assert calls == []


def test_wait_until_idle_waits_while_busy(lock_path, monkeypatch) -> None:
    lock_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    sleeps: list[float] = []
    checks = {"n": 0}

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        checks["n"] += 1
        if checks["n"] == 1:
            lock_path.unlink()

    monkeypatch.setattr(lock.time, "sleep", fake_sleep)
    lock.wait_until_gocharting_capture_idle(sleep_s=60)
    assert sleeps == [60.0]


def test_capture_lock_waits_then_acquires(lock_path, monkeypatch) -> None:
    sleeps: list[float] = []
    attempts = {"n": 0}
    real_acquire = lock.acquire_gocharting_capture_lock

    def flaky_acquire() -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("busy")
        real_acquire()

    monkeypatch.setattr(lock, "acquire_gocharting_capture_lock", flaky_acquire)
    monkeypatch.setattr(lock.time, "sleep", lambda s: sleeps.append(s))

    with lock.gocharting_capture_lock(sleep_s=5):
        assert lock.is_gocharting_capture_in_progress()
    assert not lock.is_gocharting_capture_in_progress()
    assert attempts["n"] == 2
    assert sleeps == [5.0]
