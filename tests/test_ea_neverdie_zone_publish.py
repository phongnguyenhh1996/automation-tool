"""Tests for EA NeverDie zone JSON builder."""

from __future__ import annotations

from pathlib import Path

from automation_tool.ea_neverdie_zone_publish import build_neverdie_payload
from automation_tool.zones_state import Zone, ZonesState


def _zone(
    *,
    label: str,
    trade_line: str,
    slot: str = "sang",
    side: str = "BUY",
) -> Zone:
    return Zone(
        id=f"{label}__{slot}",
        label=label,
        vung_cho="2600|2610",
        side=side,  # type: ignore[arg-type]
        trade_line=trade_line,
        session_slot=slot,  # type: ignore[arg-type]
    )


def test_build_empty_state_off_off() -> None:
    payload = build_neverdie_payload(
        zones_dir=Path("/tmp"),
        symbol="XAUUSD",
        state=ZonesState(symbol="XAUUSD", zones=[]),
        manifest_slot="sang",
    )
    assert payload["buy"]["mode"] == "off"
    assert payload["sell"]["mode"] == "off"


def test_plan_chinh_overwrites_plan_phu_same_side_buy() -> None:
    """plan_phu first, plan_chinh second → BUY prices from plan_chinh."""
    phu = _zone(
        label="plan_phu",
        trade_line="BUY LIMIT 2650.0 | SL 2645.0 | TP1 2660.0 | TP2 2670.0 | Lot 0.01",
    )
    chinh = _zone(
        label="plan_chinh",
        trade_line="BUY LIMIT 2700.0 | SL 2690.0 | TP1 2720.0 | TP2 2730.0 | Lot 0.01",
    )
    st = ZonesState(symbol="XAUUSD", zones=[phu, chinh])
    payload = build_neverdie_payload(
        zones_dir=Path("/tmp"),
        symbol="XAUUSD",
        state=st,
        manifest_slot="sang",
    )
    assert payload["buy"]["mode"] == "trade"
    assert payload["buy"]["low"] == 2700.0
    assert payload["buy"]["high"] == 2720.0
    assert payload["buy"]["sl"] == 2690.0
    assert payload["sell"]["mode"] == "off"


def test_plan_phu_sell_plan_chinh_buy() -> None:
    phu = _zone(
        label="plan_phu",
        trade_line="SELL LIMIT 2680.0 | SL 2690.0 | TP1 2660.0 | Lot 0.01",
        side="SELL",
    )
    chinh = _zone(
        label="plan_chinh",
        trade_line="BUY LIMIT 2650.0 | SL 2645.0 | TP1 2660.0 | Lot 0.01",
    )
    st = ZonesState(symbol="XAUUSD", zones=[phu, chinh])
    payload = build_neverdie_payload(
        zones_dir=Path("/tmp"),
        symbol="XAUUSD",
        state=st,
        manifest_slot="sang",
    )
    assert payload["buy"]["mode"] == "trade"
    assert payload["sell"]["mode"] == "trade"
    assert payload["sell"]["low"] == 2680.0
    assert payload["sell"]["high"] == 2660.0


def test_slot_filter_ignores_other_session_shard() -> None:
    """Only zones matching manifest_slot are used."""
    phu_sang = _zone(
        label="plan_phu",
        trade_line="BUY LIMIT 2650.0 | SL 2645.0 | TP1 2660.0 | Lot 0.01",
        slot="sang",
    )
    chinh_toi = _zone(
        label="plan_chinh",
        trade_line="BUY LIMIT 2700.0 | SL 2690.0 | TP1 2720.0 | Lot 0.01",
        slot="toi",
    )
    st = ZonesState(symbol="XAUUSD", zones=[phu_sang, chinh_toi])
    payload = build_neverdie_payload(
        zones_dir=Path("/tmp"),
        symbol="XAUUSD",
        state=st,
        manifest_slot="sang",
    )
    assert payload["buy"]["low"] == 2650.0
    assert payload["buy"]["high"] == 2660.0


def test_market_entry_uses_midpoint_sl_tp1() -> None:
    z = _zone(
        label="plan_chinh",
        trade_line="BUY MARKET | SL 2640.0 | TP1 2660.0 | Lot 0.01",
    )
    st = ZonesState(symbol="XAUUSD", zones=[z])
    payload = build_neverdie_payload(
        zones_dir=Path("/tmp"),
        symbol="XAUUSD",
        state=st,
        manifest_slot="sang",
    )
    assert payload["buy"]["low"] == (2640.0 + 2660.0) / 2.0
    assert payload["buy"]["high"] == 2660.0
    assert payload["buy"]["sl"] == 2640.0
