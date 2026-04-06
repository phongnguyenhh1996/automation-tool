"""Tests for zone prices JSON helpers."""

from __future__ import annotations

from automation_tool.openai_analysis_json import (
    AUTO_MT5_HOP_LUU_THRESHOLD,
    AUTO_MT5_HOP_LUU_THRESHOLD_SCALP,
    PriceZoneEntry,
    select_zone_for_auto_mt5,
    select_zone_for_auto_mt5_for_label,
)


def test_select_zone_highest_hop_luu() -> None:
    prices = [
        PriceZoneEntry("plan_chinh", 100.0, hop_luu=70, trade_line=""),
        PriceZoneEntry("plan_phu", 200.0, hop_luu=85, trade_line="BUY LIMIT 200 | SL 199 | TP1 205 | Lot 0.01"),
        PriceZoneEntry("scalp", 300.0, hop_luu=90, trade_line="SELL LIMIT 300 | SL 301 | TP1 298 | Lot 0.01"),
    ]
    z = select_zone_for_auto_mt5(prices)
    assert z is not None
    lab, hop, tl = z
    assert hop == 90
    assert lab == "scalp"
    assert "SELL LIMIT" in tl


def test_select_zone_tiebreak_order() -> None:
    line = "BUY LIMIT 1 | SL 0 | TP1 2 | Lot 0.01"
    prices = [
        PriceZoneEntry("plan_chinh", 1.0, hop_luu=85, trade_line=line),
        PriceZoneEntry("plan_phu", 2.0, hop_luu=85, trade_line=line),
    ]
    z = select_zone_for_auto_mt5(prices)
    assert z is not None
    assert z[0] == "plan_chinh"


def test_select_zone_requires_above_threshold() -> None:
    tl = "BUY LIMIT 1 | SL 0 | TP1 2 | Lot 0.01"
    assert AUTO_MT5_HOP_LUU_THRESHOLD == 80
    assert AUTO_MT5_HOP_LUU_THRESHOLD_SCALP == 70
    assert select_zone_for_auto_mt5([PriceZoneEntry("plan_chinh", 1.0, hop_luu=80, trade_line=tl)]) is None
    assert select_zone_for_auto_mt5([PriceZoneEntry("plan_chinh", 1.0, hop_luu=81, trade_line=tl)]) is not None


def test_select_zone_scalp_uses_lower_threshold() -> None:
    tl = "BUY LIMIT 1 | SL 0 | TP1 2 | Lot 0.01"
    assert select_zone_for_auto_mt5([PriceZoneEntry("scalp", 1.0, hop_luu=70, trade_line=tl)]) is None
    assert select_zone_for_auto_mt5([PriceZoneEntry("scalp", 1.0, hop_luu=71, trade_line=tl)]) is not None


def test_select_zone_for_label_scalp_threshold() -> None:
    tl = "BUY LIMIT 1 | SL 0 | TP1 2 | Lot 0.01"
    assert select_zone_for_auto_mt5_for_label(
        [PriceZoneEntry("scalp", 1.0, hop_luu=71, trade_line=tl)], "scalp"
    ) is not None
    assert select_zone_for_auto_mt5_for_label(
        [PriceZoneEntry("scalp", 1.0, hop_luu=70, trade_line=tl)], "scalp"
    ) is None


def test_select_zone_for_label_ignores_other_plans() -> None:
    tl = "BUY LIMIT 1 | SL 0 | TP1 2 | Lot 0.01"
    prices = [
        PriceZoneEntry("plan_chinh", 1.0, hop_luu=90, trade_line=tl),
        PriceZoneEntry("plan_phu", 2.0, hop_luu=50, trade_line=""),
        PriceZoneEntry("scalp", 3.0, hop_luu=99, trade_line=tl),
    ]
    z = select_zone_for_auto_mt5_for_label(prices, "plan_chinh")
    assert z is not None
    assert z[0] == "plan_chinh"
    assert select_zone_for_auto_mt5_for_label(prices, "plan_phu") is None
