from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import automation_tool.tv_watchlist_daemon as daemon
from automation_tool.tv_watchlist_daemon import WatchlistDaemonParams, _tv_watchlist_price_only_loop


def test_price_only_loop_does_not_launch_update_scalp_scheduler(monkeypatch, tmp_path) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 5, 5, 13, 0, tzinfo=tz)

    class DummySharedMemory:
        def close(self) -> None:
            pass

    launched_threads: list[object] = []

    class RecordingThread:
        def __init__(self, *args, **kwargs) -> None:
            launched_threads.append((args, kwargs))

        def is_alive(self) -> bool:
            return False

        def start(self) -> None:
            pass

    monkeypatch.setattr(daemon, "datetime", FrozenDatetime)
    monkeypatch.setattr(daemon, "threading", SimpleNamespace(Thread=RecordingThread), raising=False)
    monkeypatch.setattr(daemon, "open_writer_shared_memory_v2", lambda _sym: DummySharedMemory())
    monkeypatch.setattr(daemon, "write_last_prices_shared", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(daemon, "write_last_price_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(daemon, "read_manifest_last_write_slot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(daemon, "reconcile_daemon_plans_at_boot", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(daemon, "_send_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        daemon.time,
        "sleep",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stop")),
    )

    params = WatchlistDaemonParams(
        coinmap_tv_yaml=tmp_path / "coinmap_tv.yaml",
        capture_coinmap_yaml=tmp_path / "cap.yaml",
        charts_dir=tmp_path / "charts",
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        last_price_path=tmp_path / "last.txt",
    )

    with pytest.raises(RuntimeError, match="stop"):
        _tv_watchlist_price_only_loop(
            settings=MagicMock(),
            params=params,
            sym="XAUUSD",
            poll_s=0.01,
            get_price=lambda _wait_ms: 4700.0,
        )

    assert launched_threads == []
