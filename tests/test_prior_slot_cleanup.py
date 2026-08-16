"""Prior session slot cleanup when running ``all``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from automation_tool.zones_paths import prior_session_slots
from automation_tool.zones_state import (
    Zone,
    cancel_all_zone_pending_before_clear,
    invalidate_prior_session_slot_zones,
    read_zone_shard_file,
    write_zones_for_slot,
)


def test_prior_session_slots() -> None:
    assert prior_session_slots("sang") == ()
    assert prior_session_slots("chieu") == ("sang",)
    assert prior_session_slots("toi") == ("sang", "chieu")


def _write_shard(
    zones_dir: Path,
    *,
    slot: str,
    suffix: str = "",
    status: str = "vung_cho",
    mt5_ticket: int | None = 1001,
    has_position: bool = False,
) -> Path:
    zone_id = f"plan_chinh__{slot}{suffix}"
    z = Zone(
        id=zone_id,
        label="plan_chinh",
        vung_cho="2600–2610",
        side="BUY",
        status=status,  # type: ignore[arg-type]
        source="all-2" if suffix == "-2" else "all",
        session_slot=slot,  # type: ignore[arg-type]
        mt5_ticket=mt5_ticket,
        has_position=has_position,
    )
    write_zones_for_slot(
        symbol="XTEST",
        zones=[z],
        slot=slot,  # type: ignore[arg-type]
        zones_dir=zones_dir,
        shard_suffix=suffix,
        update_manifest_slot=False,
    )
    return zones_dir / f"vung_plan_chinh_{slot}{suffix}.json"


def test_invalidate_prior_slot_marks_loai_and_cancels_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    zones_dir = tmp_path / "zones"
    zones_dir.mkdir()
    sang_shard = _write_shard(zones_dir, slot="sang", mt5_ticket=2001)
    chieu_shard = _write_shard(zones_dir, slot="chieu", mt5_ticket=3001)

    monkeypatch.setattr(
        "automation_tool.daemon_launcher.stop_daemon_plan_for_shard",
        lambda _sp: True,
    )
    monkeypatch.setattr(
        "automation_tool.mt5_manage.zone_has_open_position_on_mt5",
        lambda **_kw: False,
    )
    cancel_calls: list[object] = []

    def fake_cancel(zone, **kwargs):
        cancel_calls.append(zone.id)
        return True, "ok", 1

    monkeypatch.setattr(
        "automation_tool.mt5_manage.cancel_zone_pending_tickets",
        fake_cancel,
    )

    result = invalidate_prior_session_slot_zones(
        zones_dir, current_slot="chieu", mt5_accounts_json=None
    )

    assert result.marked_loai == 1
    assert result.cancelled_pending == 1
    assert read_zone_shard_file(sang_shard).status == "loai"
    assert read_zone_shard_file(sang_shard).mt5_ticket is None
    assert read_zone_shard_file(chieu_shard).status == "vung_cho"
    assert cancel_calls == ["plan_chinh__sang"]


def test_invalidate_prior_slot_skips_position_zone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    zones_dir = tmp_path / "zones"
    zones_dir.mkdir()
    sang_shard = _write_shard(
        zones_dir, slot="sang", mt5_ticket=2001, has_position=True
    )

    monkeypatch.setattr(
        "automation_tool.mt5_manage.zone_has_open_position_on_mt5",
        lambda **_kw: True,
    )
    cancel_called = False

    def fake_cancel(*_a, **_kw):
        nonlocal cancel_called
        cancel_called = True
        return True, "ok", 0

    monkeypatch.setattr(
        "automation_tool.mt5_manage.cancel_zone_pending_tickets",
        fake_cancel,
    )

    result = invalidate_prior_session_slot_zones(
        zones_dir, current_slot="chieu", mt5_accounts_json=None
    )

    assert result.skipped_position == 1
    assert result.marked_loai == 0
    assert not cancel_called
    assert read_zone_shard_file(sang_shard).status == "vung_cho"


def test_invalidate_prior_slot_includes_second_flow_shard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    zones_dir = tmp_path / "zones"
    zones_dir.mkdir()
    sang2 = _write_shard(zones_dir, slot="sang", suffix="-2", mt5_ticket=2101)

    monkeypatch.setattr(
        "automation_tool.daemon_launcher.stop_daemon_plan_for_shard",
        lambda _sp: True,
    )
    monkeypatch.setattr(
        "automation_tool.mt5_manage.zone_has_open_position_on_mt5",
        lambda **_kw: False,
    )
    seen_ids: list[str] = []

    def fake_cancel(zone, **kwargs):
        seen_ids.append(zone.id)
        return True, "ok", 1

    monkeypatch.setattr(
        "automation_tool.mt5_manage.cancel_zone_pending_tickets",
        fake_cancel,
    )

    result = invalidate_prior_session_slot_zones(
        zones_dir, current_slot="chieu", mt5_accounts_json=None
    )

    assert result.marked_loai == 1
    assert seen_ids == ["plan_chinh__sang-2"]
    assert read_zone_shard_file(sang2).status == "loai"
    assert read_zone_shard_file(sang2).source == "all-2"


def test_cancel_all_zone_pending_before_clear(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    zones_dir = tmp_path / "zones"
    zones_dir.mkdir()
    _write_shard(zones_dir, slot="sang", mt5_ticket=1001)
    _write_shard(zones_dir, slot="chieu", mt5_ticket=1002)

    monkeypatch.setattr(
        "automation_tool.daemon_launcher.stop_daemon_plan_for_shard",
        lambda _sp: True,
    )
    monkeypatch.setattr(
        "automation_tool.mt5_manage.zone_has_open_position_on_mt5",
        lambda **_kw: False,
    )
    monkeypatch.setattr(
        "automation_tool.mt5_manage.cancel_zone_pending_tickets",
        lambda zone, **kwargs: (True, "ok", 1),
    )

    result = cancel_all_zone_pending_before_clear(zones_dir, mt5_accounts_json=None)
    assert result.shards_processed == 2
    assert result.cancelled_pending == 2


def test_cmd_all_chieu_calls_invalidate_prior_slots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from automation_tool import cli

    invalidate_called: list[str] = []

    def fake_invalidate(zones_dir, *, current_slot, mt5_accounts_json=None):
        invalidate_called.append(current_slot)
        from automation_tool.zones_state import PriorSlotCleanupResult

        return PriorSlotCleanupResult()

    monkeypatch.setattr(cli, "session_slot_now_hcm", lambda *a, **k: "chieu")
    monkeypatch.setattr(cli, "zones_dir_from_cli_path", lambda _p: tmp_path / "zones")
    monkeypatch.setattr(cli, "load_settings", lambda: MagicMock())
    monkeypatch.setattr(cli, "invalidate_prior_session_slot_zones", fake_invalidate)
    monkeypatch.setattr(cli, "cancel_all_zone_pending_before_clear", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_send_python_bot_job_started", lambda *a, **k: None)
    monkeypatch.setattr(cli, "capture_charts", lambda **k: (_ for _ in ()).throw(SystemExit(0)))

    args = MagicMock()
    args.zones_json = None
    args.no_clear_zones_state = False
    args.main_symbol = None
    args.mt5_accounts_json = None
    args.config = None
    args.storage_state = None
    args.gocharting = False
    args.gc_only = False
    args.no_tradingview = True

    with pytest.raises(SystemExit):
        cli.cmd_all(args)

    assert invalidate_called == ["chieu"]
