from __future__ import annotations

import json
from pathlib import Path

import pytest

from automation_tool.zones_state import (
    Zone,
    ZonesState,
    mark_zone_status_loai_by_id,
    read_zone_shard_file,
    read_zones_state,
    write_zones_state,
)


def _shard_payload(*, sym: str, slot: str, zone: dict) -> str:
    return json.dumps(
        {
            "symbol": sym,
            "slot": slot,
            "updated_at": "2099-01-01T00:00:00Z",
            "zone": zone,
        },
        ensure_ascii=False,
    )


def test_mark_zone_loai_shard_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    zones_dir = tmp_path / "zones"
    zones_dir.mkdir(parents=True)
    zid = "plan_chinh__sang"
    shard = zones_dir / "vung_plan_chinh_sang.json"
    shard.write_text(
        _shard_payload(
            sym="XTEST",
            slot="sang",
            zone={
                "id": zid,
                "label": "plan_chinh",
                "vung_cho": "2600–2610",
                "side": "BUY",
                "status": "vung_cho",
                "trade_line": "",
            },
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "automation_tool.zones_state.default_zones_dir",
        lambda symbol=None: zones_dir,
    )

    ok, msg = mark_zone_status_loai_by_id(zid, symbol="XTEST")
    assert ok is True
    assert "loai" in msg.lower() or "loại" in msg
    z2 = read_zone_shard_file(shard)
    assert z2 is not None
    assert z2.status == "loai"
    assert z2.loai_streak == 0


def test_mark_zone_loai_legacy_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sym = "XLEG"
    root = tmp_path / sym
    legacy = root / "zones_state.json"
    root.mkdir(parents=True)
    zid = "legacy_zone_1"
    st = ZonesState(
        symbol=sym,
        zones=[
            Zone(
                id=zid,
                label="plan_chinh",
                vung_cho="1–2",
                side="BUY",
                status="cham",
            ),
        ],
    )
    write_zones_state(st, path=legacy)

    monkeypatch.setattr(
        "automation_tool.zones_state.default_zones_dir",
        lambda symbol=None: tmp_path / "empty_zones",
    )
    monkeypatch.setattr(
        "automation_tool.zones_state.symbol_data_dir",
        lambda s=None: root,
    )

    ok, msg = mark_zone_status_loai_by_id(zid, symbol=sym)
    assert ok is True
    st2 = read_zones_state(legacy)
    assert st2 is not None
    assert st2.zones[0].status == "loai"


def test_mark_zone_loai_not_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    zones_dir = tmp_path / "zones"
    zones_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "automation_tool.zones_state.default_zones_dir",
        lambda symbol=None: zones_dir,
    )
    ok, msg = mark_zone_status_loai_by_id("no_such__id", symbol="XAUUSD")
    assert ok is False
    assert "Không tìm thấy" in msg


def test_mark_zone_loai_empty_id() -> None:
    ok, msg = mark_zone_status_loai_by_id("   ", symbol="XAUUSD")
    assert ok is False
    assert "Thiếu" in msg
