"""Spawn and reconcile ``daemon-plan`` processes (one PID file per shard)."""

from __future__ import annotations

import atexit
import hashlib
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from automation_tool.zones_paths import default_zones_dir, resolve_zones_directory
from automation_tool.zones_state import read_zone_shard_file

_log = logging.getLogger("automation_tool.daemon_launcher")

# --- stop on process exit (tv-watchlist-daemon + Windows console close) ---
_cleanup_zones_dir_exit: Optional[Path] = None
_cleanup_exit_done = False
_win_console_ctrl_handler = None  # keep ctypes callback alive


def _cleanup_daemon_plans_on_exit_once() -> None:
    global _cleanup_exit_done
    if _cleanup_exit_done:
        return
    zd = _cleanup_zones_dir_exit
    if zd is None:
        return
    _cleanup_exit_done = True
    try:
        n = stop_daemon_plans_in_zones(zd, log_chat=None)
        _log.info("stop-daemon-plans on exit | dir=%s signalled=%s", zd, n)
    except Exception as e:
        _log.warning("stop-daemon-plans on exit failed: %s", e)


def register_stop_daemon_plans_on_exit(zones_dir: Path) -> None:
    """
    Register cleanup: :func:`stop_daemon_plans_in_zones` on normal exit, signals, and (Windows)
    console close / logoff / shutdown control events.
    """
    global _cleanup_zones_dir_exit, _win_console_ctrl_handler
    _cleanup_zones_dir_exit = zones_dir.resolve()
    atexit.register(_cleanup_daemon_plans_on_exit_once)

    def _on_signal(_signum: int, _frame: object) -> None:
        _cleanup_daemon_plans_on_exit_once()
        raise SystemExit(0)

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _on_signal)
        except (OSError, ValueError):
            pass

    if sys.platform == "win32":
        try:
            import ctypes

            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint32)
            def _win_ctrl_handler(_ctrl_type: int) -> bool:
                _cleanup_daemon_plans_on_exit_once()
                return False

            _win_console_ctrl_handler = _win_ctrl_handler
            if not ctypes.windll.kernel32.SetConsoleCtrlHandler(_win_console_ctrl_handler, True):
                _log.warning("SetConsoleCtrlHandler(stop-daemon-plans) failed")
        except Exception as e:
            _log.warning("register_stop_daemon_plans_on_exit: %s", e)


def _pid_path(zones_dir: Path, shard_path: Path) -> Path:
    key = str(shard_path.resolve())
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
    return zones_dir / f".daemon-plan-{h}.pid"


def _read_pid(path: Path) -> Optional[int]:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw)
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Windows: e.g. WinError 87 (ERROR_INVALID_PARAMETER) for stale/garbage PIDs — not alive.
        return False


def stop_daemon_plans_in_zones(zones_dir: Path, *, log_chat: Optional[Callable[[str], None]] = None) -> int:
    """
    Stop every ``daemon-plan`` tracked under ``zones_dir`` (``.daemon-plan-*.pid``).
    Returns number of processes signalled.
    """
    n = 0
    if not zones_dir.is_dir():
        return 0
    for child in zones_dir.iterdir():
        if not child.is_file() or not child.name.startswith(".daemon-plan-") or not child.name.endswith(
            ".pid"
        ):
            continue
        pid = _read_pid(child)
        if pid is None:
            try:
                child.unlink()
            except OSError:
                pass
            continue
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                n += 1
                msg = f"[launcher] stop daemon-plan pid={pid} file={child.name}"
                _log.info(msg)
                if log_chat:
                    log_chat(msg)
            except ProcessLookupError:
                pass
            except OSError as e:
                _log.warning("stop pid %s: %s", pid, e)
        try:
            child.unlink()
        except OSError:
            pass
    return n


def spawn_daemon_plan_if_needed(
    *,
    shard_path: Path,
    zones_dir: Path,
    log_chat: Optional[Callable[[str], None]] = None,
) -> str:
    """
    If no live PID for this shard, spawn ``coinmap-automation daemon-plan --shard ...``.
    Returns ``spawned`` | ``skipped``.
    """
    shard_path = shard_path.resolve()
    zones_dir = zones_dir.resolve()
    pid_file = _pid_path(zones_dir, shard_path)
    if pid_file.is_file():
        old = _read_pid(pid_file)
        if old is not None and _pid_alive(old):
            msg = f"[launcher] skip daemon-plan (already running) pid={old} shard={shard_path}"
            _log.info(msg)
            if log_chat:
                log_chat(msg)
            return "skipped"
        try:
            pid_file.unlink()
        except OSError:
            pass

    cmd = [
        sys.executable,
        "-m",
        "automation_tool.cli",
        "daemon-plan",
        "--shard",
        str(shard_path),
    ]
    # Windows: redirect stdio so CreateProcess does not inherit invalid console handles (Task Scheduler).
    # Keep creationflags=0 — CREATE_NO_WINDOW / CREATE_NEW_PROCESS_GROUP have caused WinError 87 on some hosts.
    try:
        cwd = str(Path.cwd().resolve())
    except OSError:
        cwd = None

    # Hide console without creationflags: STARTUPINFO (works when CREATE_NO_WINDOW does not).
    startupinfo: Optional[subprocess.STARTUPINFO] = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0,
            startupinfo=startupinfo,
        )
    except OSError as e:
        _log.warning(
            "spawn daemon-plan Popen failed | cmd=%s cwd=%s | %s",
            cmd,
            cwd,
            e,
        )
        raise
    try:
        pid_file.write_text(str(proc.pid), encoding="utf-8")
    except OSError as e:
        _log.warning("could not write pid file %s: %s", pid_file, e)
    msg = f"[launcher] spawn daemon-plan pid={proc.pid} shard={shard_path}"
    _log.info(msg)
    if log_chat:
        log_chat(msg)
    return "spawned"


def launch_daemon_plans_for_written_shards(
    *,
    zones_dir: Path,
    shard_paths: list[Path],
    log_chat: Optional[Callable[[str], None]] = None,
) -> None:
    for sp in shard_paths:
        spawn_daemon_plan_if_needed(shard_path=sp, zones_dir=zones_dir, log_chat=log_chat)


def reconcile_daemon_plans_at_boot(
    zones_dir: Optional[Path] = None,
    *,
    log_chat: Optional[Callable[[str], None]] = None,
) -> int:
    """
    For each ``vung_*.json`` with a non-terminal zone and no live PID, spawn ``daemon-plan``.
    Returns number of spawns.
    """
    root = zones_dir or default_zones_dir()
    if not root.is_dir():
        return 0
    n = 0
    for child in sorted(root.iterdir()):
        if not child.is_file() or not child.name.startswith("vung_") or not child.name.endswith(".json"):
            continue
        z = read_zone_shard_file(child)
        if z is None:
            continue
        if z.status in ("done", "loai"):
            continue
        if spawn_daemon_plan_if_needed(shard_path=child, zones_dir=root, log_chat=log_chat) == "spawned":
            n += 1
    return n


def zones_dir_from_cli_path(zones_json: Optional[Path]) -> Path:
    return resolve_zones_directory(zones_json)
