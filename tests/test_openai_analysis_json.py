"""Tests for zone prices JSON helpers."""

from __future__ import annotations

from automation_tool.openai_analysis_json import (
    AUTO_MT5_HOP_LUU_THRESHOLD,
    AUTO_MT5_HOP_LUU_THRESHOLD_SCALP,
    AnalysisPayload,
    PriceZoneEntry,
    format_plan_lines_for_telegram,
    parse_analysis_from_openai_text,
    select_zone_for_auto_mt5,
    select_zone_for_auto_mt5_for_label,
    try_parse_analysis_payload,
)


def test_format_plan_lines_for_telegram_order_and_hop_luu() -> None:
    p = AnalysisPayload(
        prices=[
            PriceZoneEntry("scalp", 3.0, hop_luu=62, trade_line="S"),
            PriceZoneEntry("plan_chinh", 1.0, hop_luu=85, trade_line="A"),
            PriceZoneEntry("plan_phu", 2.0, hop_luu=None, trade_line="B"),
        ]
    )
    s = format_plan_lines_for_telegram(p)
    assert s.splitlines() == [
        "plan_chinh (hợp lưu: 85) : A",
        "plan_phu (hợp lưu: —) : B",
        "scalp (hợp lưu: 62) : S",
    ]
    assert format_plan_lines_for_telegram(None) == ""


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
    assert AUTO_MT5_HOP_LUU_THRESHOLD == 85
    assert AUTO_MT5_HOP_LUU_THRESHOLD_SCALP == 60
    assert select_zone_for_auto_mt5([PriceZoneEntry("plan_chinh", 1.0, hop_luu=84, trade_line=tl)]) is None
    assert select_zone_for_auto_mt5([PriceZoneEntry("plan_chinh", 1.0, hop_luu=85, trade_line=tl)]) is not None


def test_select_zone_scalp_uses_lower_threshold() -> None:
    tl = "BUY LIMIT 1 | SL 0 | TP1 2 | Lot 0.01"
    assert select_zone_for_auto_mt5([PriceZoneEntry("scalp", 1.0, hop_luu=59, trade_line=tl)]) is None
    assert select_zone_for_auto_mt5([PriceZoneEntry("scalp", 1.0, hop_luu=60, trade_line=tl)]) is not None


def test_select_zone_for_label_scalp_threshold() -> None:
    tl = "BUY LIMIT 1 | SL 0 | TP1 2 | Lot 0.01"
    assert select_zone_for_auto_mt5_for_label(
        [PriceZoneEntry("scalp", 1.0, hop_luu=60, trade_line=tl)], "scalp"
    ) is not None
    assert select_zone_for_auto_mt5_for_label(
        [PriceZoneEntry("scalp", 1.0, hop_luu=59, trade_line=tl)], "scalp"
    ) is None


def test_vung_cho_string_parses_to_range_low_high() -> None:
    raw = """
    {
      "prices": [
        {"label": "plan_chinh", "value": 2650.0, "vung_cho": "4762.0–4766.0", "hop_luu": 85, "trade_line": "x"}
      ]
    }
    """
    p = parse_analysis_from_openai_text(raw)
    assert p is not None and len(p.prices) == 1
    z = p.prices[0]
    assert z.range_low == 4762.0
    assert z.range_high == 4766.0
    assert z.vung_cho == "4762.0–4766.0"


def test_vung_cho_reversed_order_uses_min_max() -> None:
    data = {
        "prices": [
            {
                "label": "plan_chinh",
                "value": 1.0,
                "vung_cho": "4709.0–4705.0",
                "hop_luu": 85,
                "trade_line": "t",
            }
        ]
    }
    p = try_parse_analysis_payload(data)
    assert p is not None
    z = p.prices[0]
    assert z.range_low == 4705.0
    assert z.range_high == 4709.0
    assert z.vung_cho == "4709.0–4705.0"


def test_legacy_range_low_high_without_vung_cho() -> None:
    data = {
        "prices": [
            {
                "label": "plan_chinh",
                "value": 1.0,
                "range_low": 10.0,
                "range_high": 20.0,
                "hop_luu": 85,
                "trade_line": "t",
            }
        ]
    }
    p = try_parse_analysis_payload(data)
    assert p is not None
    assert p.prices[0].range_low == 10.0
    assert p.prices[0].range_high == 20.0


def test_invalid_vung_cho_keeps_range_low_high() -> None:
    data = {
        "prices": [
            {
                "label": "plan_chinh",
                "value": 1.0,
                "vung_cho": "not-a-range",
                "range_low": 3.0,
                "range_high": 4.0,
                "hop_luu": 85,
                "trade_line": "t",
            }
        ]
    }
    p = try_parse_analysis_payload(data)
    assert p is not None
    assert p.prices[0].range_low == 3.0
    assert p.prices[0].range_high == 4.0
    assert p.prices[0].vung_cho == "not-a-range"


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
