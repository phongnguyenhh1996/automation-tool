"""
Một tiến trình ``all`` hoặc ``update`` tại một thời điểm: lệnh mới dừng lệnh cũ (cùng nhóm).
Ghi PID vào ``data/coinmap_all_update.pid``; xóa khi thoát bình thường hoặc SIGINT/SIGTERM.
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from automation_tool.config import default_data_dir

_log = logging.getLogger("automation_tool.exclusive_all_update")

_PID_FILENAME = "coinmap_all_update.pid"
_registered = False
_our_pid: int | None = None


def _pid_path() -> Path:
    return default_data_dir() / _PID_FILENAME


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _terminate_process_tree(pid: int) -> None:
    """Gửi tín hiệu dừng tới PID và (trên Windows) toàn bộ cây tiến trình con."""
    if pid == os.getpid() or not _pid_alive(pid):
        return
    _log.info("Dừng tiến trình all/update đang chạy (PID %s)…", pid)
    try:
        if sys.platform == "win32":
            r = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T"],
                capture_output=True,
                text=True,
                timeout=45,
            )
            if r.returncode != 0 and r.stderr:
                _log.debug("taskkill (nhẹ): %s", r.stderr.strip()[:500])
        else:
            os.kill(pid, signal.SIGTERM)
    except Exception as e:
        _log.warning("Không gửi được tín hiệu dừng tới PID %s: %s", pid, e)

    deadline = time.time() + 15.0
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.25)

    if not _pid_alive(pid):
        _log.info("Tiến trình cũ (PID %s) đã dừng.", pid)
        return

    _log.warning("PID %s vẫn còn — buộc dừng.", pid)
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=45,
            )
        else:
            os.kill(pid, signal.SIGKILL)
    except Exception as e:
        _log.warning("Buộc dừng PID %s: %s", pid, e)


def _release_if_holder() -> None:
    global _our_pid
    if _our_pid is None:
        return
    path = _pid_path()
    try:
        if path.is_file():
            raw = path.read_text(encoding="utf-8").strip().split()
            if raw and int(raw[0]) == os.getpid():
                path.unlink()
    except OSError:
        pass
    except ValueError:
        pass
    _our_pid = None


def _handle_signal(signum: int, frame: object) -> None:
    _release_if_holder()
    if signum == signal.SIGINT:
        sys.exit(130)
    # SIGTERM, etc.
    sys.exit(128 + signum if 0 < signum < 128 else 1)


def acquire_exclusive_all_update_run() -> None:
    """
    Nếu đã có file PID và tiến trình còn sống (khác PID hiện tại), dừng tiến trình đó.
    Ghi PID hiện tại và đăng ký giải phóng khi thoát.
    """
    global _registered, _our_pid
    default_data_dir().mkdir(parents=True, exist_ok=True)
    path = _pid_path()
    if path.is_file():
        try:
            old = int(path.read_text(encoding="utf-8").strip().split()[0])
        except (ValueError, OSError, IndexError):
            old = -1
        if old > 0 and old != os.getpid():
            _terminate_process_tree(old)
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass

    try:
        path.write_text(str(os.getpid()) + "\n", encoding="utf-8")
    except OSError as e:
        _log.warning("Không ghi được %s: %s", path, e)

    _our_pid = os.getpid()
    if not _registered:
        atexit.register(_release_if_holder)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handle_signal)
            except (OSError, ValueError):
                pass
        _registered = True
