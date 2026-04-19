"""PID liveness used by stop-daemon-plans / spawn_daemon_plan_if_needed."""

import os

from automation_tool.daemon_launcher import _pid_alive


def test_pid_alive_self() -> None:
    assert _pid_alive(os.getpid()) is True


def test_pid_alive_zero() -> None:
    assert _pid_alive(0) is False


def test_pid_alive_impossible_pid() -> None:
    assert _pid_alive(-1) is False
    # Very unlikely to exist on any host
    assert _pid_alive(2**30) is False
