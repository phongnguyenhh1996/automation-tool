"""Exclusive lock for GoCharting browser capture (``all``, ``update-scalp``, footprint daemon)."""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from automation_tool.config import default_data_dir

_log = logging.getLogger(__name__)

_LOCK_FILENAME = "gocharting_intraday_capture.lock"
_DEFAULT_BUSY_WAIT_S = 60.0


def gocharting_capture_lock_path() -> Path:
    return default_data_dir() / _LOCK_FILENAME


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lock_owner_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip().split()
        if not raw:
            return None
        return int(raw[0])
    except (OSError, ValueError, IndexError):
        return None


def release_stale_gocharting_capture_lock() -> None:
    """Remove lock file if corrupt or owning PID is not alive."""
    path = gocharting_capture_lock_path()
    if not path.is_file():
        return
    pid = _lock_owner_pid(path)
    if pid is None or not _pid_alive(pid):
        try:
            path.unlink()
        except OSError:
            pass


def is_gocharting_capture_in_progress() -> bool:
    """True when any GoCharting capture flow holds the lock."""
    release_stale_gocharting_capture_lock()
    path = gocharting_capture_lock_path()
    if not path.is_file():
        return False
    pid = _lock_owner_pid(path)
    if pid is None:
        return False
    if pid == os.getpid():
        return True
    return _pid_alive(pid)


def acquire_gocharting_capture_lock() -> None:
    default_data_dir().mkdir(parents=True, exist_ok=True)
    release_stale_gocharting_capture_lock()
    path = gocharting_capture_lock_path()
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        owner = _lock_owner_pid(path)
        if owner == os.getpid():
            return
        if owner is not None and _pid_alive(owner):
            raise RuntimeError(
                f"GoCharting intraday capture already in progress (lock: {path}, pid={owner})"
            )
        release_stale_gocharting_capture_lock()
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
    finally:
        os.close(fd)


def release_gocharting_capture_lock() -> None:
    path = gocharting_capture_lock_path()
    try:
        if path.is_file():
            owner = _lock_owner_pid(path)
            if owner == os.getpid():
                path.unlink()
    except OSError:
        pass


@contextmanager
def gocharting_capture_lock(*, sleep_s: float = _DEFAULT_BUSY_WAIT_S) -> Iterator[None]:
    """Acquire exclusive GoCharting capture lock, waiting if another flow holds it."""
    while True:
        try:
            acquire_gocharting_capture_lock()
            break
        except RuntimeError:
            _log.info(
                "gocharting capture: another flow holds the lock — waiting %.0fs",
                sleep_s,
            )
            print(
                f"gocharting capture: luồng GoCharting khác đang chạy — chờ {int(sleep_s)}s...",
                flush=True,
            )
            time.sleep(max(1.0, float(sleep_s)))
    try:
        yield
    finally:
        release_gocharting_capture_lock()


def wait_until_gocharting_capture_idle(*, sleep_s: float = _DEFAULT_BUSY_WAIT_S) -> None:
    """Block until no GoCharting capture holds the lock; retry every ``sleep_s`` seconds."""
    while is_gocharting_capture_in_progress():
        _log.info(
            "gocharting capture: waiting %.0fs for lock to clear",
            sleep_s,
        )
        print(
            f"gocharting capture: luồng GoCharting khác đang chạy — chờ {int(sleep_s)}s...",
            flush=True,
        )
        time.sleep(max(1.0, float(sleep_s)))
