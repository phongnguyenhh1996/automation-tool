"""zones_state Zone model and legacy JSON migration."""

from __future__ import annotations

import json
from pathlib import Path

from automation_tool.zones_state import (
    Zone,
    ZonesState,
    _parse_zone,
    can_apply_old_price_loai,
    read_zones_state,
    remove_zones_state_file,
    zones_from_analysis_payload,
    zones_from_analysis_payload_merged,
)
from automation_tool.openai_analysis_json import AnalysisPayload, PriceZoneEntry


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
