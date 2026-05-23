from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import automation_tool.tv_watchlist_daemon as daemon
from automation_tool.tv_watchlist_daemon import (
    WatchlistDaemonParams,
    _daemon_gia_tick_manifest_updated_at_reconcile,
    _tv_watchlist_price_only_loop,
)
from automation_tool.zones_state import read_manifest_updated_at


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
    monkeypatch.setattr(daemon, "read_manifest_updated_at", lambda *_args, **_kwargs: None)
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


def test_daemon_gia_manifest_reconcile_debounce_waits_then_spawns(
    monkeypatch, tmp_path
) -> None:
    zones_dir = tmp_path / "zones"
    zones_dir.mkdir()
    manifest = zones_dir / "zones_manifest.json"
    manifest.write_text(
        '{"symbol":"XAUUSD","updated_at":"2026-01-01T00:00:00Z"}',
        encoding="utf-8",
    )
    state: dict = {}
    assert _daemon_gia_tick_manifest_updated_at_reconcile(zones_dir, state=state) is None
    assert state["last_reconciled_updated_at"] == "2026-01-01T00:00:00Z"

    manifest.write_text(
        '{"symbol":"XAUUSD","updated_at":"2026-01-01T00:00:10Z"}',
        encoding="utf-8",
    )
    mono = {"t": 100.0}
    monkeypatch.setattr(daemon.time, "monotonic", lambda: mono["t"])

    assert _daemon_gia_tick_manifest_updated_at_reconcile(zones_dir, state=state) is None
    assert state["pending_updated_at"] == "2026-01-01T00:00:10Z"

    mono["t"] = 109.0
    assert _daemon_gia_tick_manifest_updated_at_reconcile(zones_dir, state=state) is None

    calls: list[int] = []

    def fake_reconcile(_zd):
        calls.append(1)
        return 2

    monkeypatch.setattr(daemon, "reconcile_daemon_plans_at_boot", fake_reconcile)
    mono["t"] = 110.0
    assert _daemon_gia_tick_manifest_updated_at_reconcile(zones_dir, state=state) == 2
    assert calls == [1]
    assert state["last_reconciled_updated_at"] == "2026-01-01T00:00:10Z"
    assert "pending_updated_at" not in state


def test_daemon_gia_manifest_reconcile_debounce_resets_on_rapid_writes(
    monkeypatch, tmp_path
) -> None:
    zones_dir = tmp_path / "zones"
    zones_dir.mkdir()
    manifest = zones_dir / "zones_manifest.json"
    manifest.write_text(
        '{"symbol":"XAUUSD","updated_at":"2026-01-01T00:00:00Z"}',
        encoding="utf-8",
    )
    state = {"last_reconciled_updated_at": "2026-01-01T00:00:00Z"}
    mono = {"t": 0.0}
    monkeypatch.setattr(daemon.time, "monotonic", lambda: mono["t"])
    monkeypatch.setattr(
        daemon, "reconcile_daemon_plans_at_boot", lambda _zd: (_ for _ in ()).throw(AssertionError)
    )

    manifest.write_text(
        '{"symbol":"XAUUSD","updated_at":"2026-01-01T00:00:01Z"}',
        encoding="utf-8",
    )
    _daemon_gia_tick_manifest_updated_at_reconcile(zones_dir, state=state)
    mono["t"] = 9.0
    manifest.write_text(
        '{"symbol":"XAUUSD","updated_at":"2026-01-01T00:00:02Z"}',
        encoding="utf-8",
    )
    _daemon_gia_tick_manifest_updated_at_reconcile(zones_dir, state=state)
    mono["t"] = 18.0
    assert _daemon_gia_tick_manifest_updated_at_reconcile(zones_dir, state=state) is None


def test_read_manifest_updated_at(tmp_path) -> None:
    zones_dir = tmp_path / "zones"
    zones_dir.mkdir()
    assert read_manifest_updated_at(zones_dir) is None
    (zones_dir / "zones_manifest.json").write_text(
        '{"updated_at":"2099-06-01T12:00:00+07:00"}',
        encoding="utf-8",
    )
    assert read_manifest_updated_at(zones_dir) == "2099-06-01T12:00:00+07:00"
