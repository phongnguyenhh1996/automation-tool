"""zones_state Zone model and legacy JSON migration."""

from __future__ import annotations

import json
from pathlib import Path

from automation_tool.zones_state import (
    Zone,
    ZonesState,
    _parse_zone,
    can_apply_old_price_loai,
    read_zones_state_from_shard,
    read_zones_state,
    remap_scalp_zones_avoiding_shard_collision,
    remove_zones_state_file,
    write_zones_state_to_shard,
    write_zones_for_slot,
    zones_from_analysis_payload,
    zones_from_analysis_payload_merged,
    zone_from_price_entry,
)
from automation_tool.openai_analysis_json import AnalysisPayload, PriceZoneEntry
from automation_tool.zones_paths import shard_path


def test_parse_zone_legacy_range_migrates_to_vung_cho() -> None:
    z = _parse_zone(
        {
            "id": "plan_chinh",
            "label": "plan_chinh",
            "range_low": 4738.0,
            "range_high": 4742.0,
            "alert_price": 4740.0,
            "side": "BUY",
        }
    )
    assert z is not None
    assert z.vung_cho == "4738.0–4742.0"


def test_parse_zone_vung_cho_required_parseable() -> None:
    assert _parse_zone({"id": "a", "label": "x", "vung_cho": "bad", "side": "BUY"}) is None


def test_read_zones_state_legacy_file(tmp_path: Path) -> None:
    p = tmp_path / "zones_state.json"
    p.write_text(
        json.dumps(
            {
                "symbol": "X",
                "zones": [
                    {
                        "id": "plan_chinh",
                        "label": "plan_chinh",
                        "range_low": 10.0,
                        "range_high": 20.0,
                        "alert_price": 15.0,
                        "side": "SELL",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    st = read_zones_state(p)
    assert st is not None
    assert st.zones[0].vung_cho == "10.0–20.0"


def test_zones_from_analysis_prefers_pe_vung_cho() -> None:
    payload = AnalysisPayload(
        prices=[
            PriceZoneEntry(
                label="plan_chinh",
                value=4709.0,
                range_low=4707.0,
                range_high=4709.0,
                vung_cho="4707.0–4709.0",
                hop_luu=78,
                trade_line="BUY LIMIT 4709.0 | SL 4699.0",
            ),
            PriceZoneEntry("plan_phu", 2600.0, hop_luu=50, trade_line=""),
            PriceZoneEntry("scalp", 2601.0, hop_luu=50, trade_line=""),
        ]
    )
    zones = zones_from_analysis_payload(symbol="XAUUSD", payload=payload, source="t")
    by_id = {z.id: z for z in zones}
    assert by_id["plan_chinh"].vung_cho == "4707.0–4709.0"


def test_zones_from_analysis_skips_scalp_for_all_and_update_sources() -> None:
    payload = AnalysisPayload(
        prices=[
            PriceZoneEntry("plan_chinh", 100.0, hop_luu=70, trade_line="BUY LIMIT 100"),
            PriceZoneEntry("plan_phu", 101.0, hop_luu=65, trade_line="BUY LIMIT 101"),
            PriceZoneEntry("scalp", 102.0, hop_luu=60, trade_line="BUY LIMIT 102"),
        ]
    )

    for source in ("all", "update"):
        zones = zones_from_analysis_payload(symbol="XAUUSD", payload=payload, source=source)
        assert [z.label for z in zones] == ["plan_chinh", "plan_phu"]


def test_zones_from_analysis_merged_keeps_zone_when_no_change_true() -> None:
    existing = ZonesState(
        symbol="XAUUSD",
        zones=[
            Zone(
                id="plan_chinh",
                label="plan_chinh",
                vung_cho="1.0–2.0",
                side="BUY",
                hop_luu=85,
                trade_line="old",
                status="vung_cho",
            ),
        ],
    )
    payload = AnalysisPayload(
        prices=[
            PriceZoneEntry(
                label="plan_chinh",
                value=99.0,
                vung_cho="98.0–100.0",
                hop_luu=50,
                trade_line="new",
                no_change=True,
            ),
            PriceZoneEntry("plan_phu", 2.0, hop_luu=50, trade_line="", no_change=False),
            PriceZoneEntry("scalp", 3.0, hop_luu=50, trade_line="", no_change=False),
        ]
    )
    zones = zones_from_analysis_payload_merged(existing=existing, payload=payload, source="u")
    by_label = {z.label: z for z in zones}
    assert by_label["plan_chinh"].trade_line == "old"
    assert by_label["plan_phu"].vung_cho == "2.0–2.0"
    assert by_label["scalp"].vung_cho == "3.0–3.0"


def test_old_prices_loai_only_applies_to_waiting_or_touched_zones() -> None:
    assert can_apply_old_price_loai("vung_cho")
    assert can_apply_old_price_loai("cham")
    assert not can_apply_old_price_loai("vao_lenh")
    assert not can_apply_old_price_loai("cho_tp1")
    assert not can_apply_old_price_loai("done")


def test_remove_zones_state_file(tmp_path: Path) -> None:
    p = tmp_path / "zones_state.json"
    assert remove_zones_state_file(p) is False
    p.write_text("{}", encoding="utf-8")
    assert remove_zones_state_file(p) is True
    assert remove_zones_state_file(p) is False


def test_write_zones_for_slot_offsets_trade_line_tp1_before_persisting(tmp_path: Path) -> None:
    write_zones_for_slot(
        symbol="XAUUSD",
        slot="sang",
        zones=[
            Zone(
                id="buy",
                label="plan_chinh",
                vung_cho="4708–4710",
                side="BUY",
                trade_line="BUY LIMIT 4709.0 | SL 4699.0 | TP1 4720.0 | Lot 0.02",
            ),
            Zone(
                id="sell",
                label="plan_phu",
                vung_cho="4728–4730",
                side="SELL",
                trade_line="SELL LIMIT 4729.0 | SL 4739.0 | TP1 4718.0 | Lot 0.02",
            ),
        ],
        zones_dir=tmp_path,
    )

    buy_data = json.loads(shard_path(tmp_path, "plan_chinh", "sang").read_text())
    sell_data = json.loads(shard_path(tmp_path, "plan_phu", "sang").read_text())

    assert buy_data["zone"]["trade_line"] == (
        "BUY LIMIT 4709.0 | SL 4699.0 | TP1 4718.5 | Lot 0.02"
    )
    assert buy_data["zone"]["tp1_write_adjusted"] is True
    assert sell_data["zone"]["trade_line"] == (
        "SELL LIMIT 4729.0 | SL 4739.0 | TP1 4719.5 | Lot 0.02"
    )

    buy_shard = shard_path(tmp_path, "plan_chinh", "sang")
    written_state = read_zones_state_from_shard(buy_shard)
    assert written_state is not None
    write_zones_state_to_shard(buy_shard, written_state)
    rewritten_data = json.loads(buy_shard.read_text())
    assert rewritten_data["zone"]["trade_line"] == buy_data["zone"]["trade_line"]


def test_parse_zone_reads_has_position_flag() -> None:
    z = _parse_zone(
        {
            "id": "plan_chinh_sang",
            "label": "plan_chinh",
            "vung_cho": "100–101",
            "side": "BUY",
            "status": "cho_tp1",
            "has_position": True,
            "openai_manage_retry_at": "2026-05-01T03:15:00+00:00",
            "managed_sl": 99.5,
            "managed_tp": 103.0,
            "last_r_followup_level": 2,
        }
    )
    assert z is not None
    assert z.has_position is True
    assert z.openai_manage_retry_at == "2026-05-01T03:15:00+00:00"
    assert z.managed_sl == 99.5
    assert z.managed_tp == 103.0
    assert z.last_r_followup_level == 2


def test_remap_scalp_keeps_label_when_shard_missing(tmp_path: Path) -> None:
    z = zone_from_price_entry(
        lab="scalp_1",
        pe=PriceZoneEntry(label="scalp_1", value=100.0, hop_luu=60, trade_line="BUY LIMIT 100"),
        source="update-scalp",
        session_slot="sang",
    )
    out = remap_scalp_zones_avoiding_shard_collision([z], zones_dir=tmp_path, slot="sang")
    assert len(out) == 1
    assert out[0].label == "scalp_1"
    assert out[0].id == "scalp_1__sang"


def test_remap_scalp_bumps_when_shard_file_exists(tmp_path: Path) -> None:
    zones_dir = tmp_path
    shard_path(zones_dir, "scalp_1", "sang").write_text(
        '{"symbol":"X","slot":"sang","zone":{"id":"x","label":"scalp_1","vung_cho":"1–2","side":"BUY"}}',
        encoding="utf-8",
    )
    z = zone_from_price_entry(
        lab="scalp_1",
        pe=PriceZoneEntry(label="scalp_1", value=200.0, hop_luu=60, trade_line="SELL LIMIT 200"),
        source="update-scalp",
        session_slot="sang",
    )
    out = remap_scalp_zones_avoiding_shard_collision([z], zones_dir=zones_dir, slot="sang")
    assert out[0].label == "scalp_2"
    assert out[0].id == "scalp_2__sang"


def test_remap_scalp_second_zone_avoids_double_booking_same_batch(tmp_path: Path) -> None:
    zones_dir = tmp_path
    shard_path(zones_dir, "scalp_1", "sang").write_text(
        '{"symbol":"X","slot":"sang","zone":{"id":"x","label":"scalp_1","vung_cho":"1–2","side":"BUY"}}',
        encoding="utf-8",
    )
    z1 = zone_from_price_entry(
        lab="scalp_1",
        pe=PriceZoneEntry(label="scalp_1", value=1.0, hop_luu=60, trade_line="BUY"),
        source="update-scalp",
        session_slot="sang",
    )
    z2 = zone_from_price_entry(
        lab="scalp_2",
        pe=PriceZoneEntry(label="scalp_2", value=2.0, hop_luu=60, trade_line="BUY"),
        source="update-scalp",
        session_slot="sang",
    )
    out = remap_scalp_zones_avoiding_shard_collision([z1, z2], zones_dir=zones_dir, slot="sang")
    assert [z.label for z in out] == ["scalp_2", "scalp_3"]
