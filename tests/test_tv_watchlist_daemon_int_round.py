"""Zone touch uses integer-rounded prices before ``eps`` comparison."""

from __future__ import annotations

from automation_tool.openai_analysis_json import ARM_THRESHOLD_TP1_SCALP
from automation_tool.tv_watchlist_daemon import (
    _ARM_THRESHOLD,
    _EPS_DEFAULT,
    _arm_threshold_met_for_zone,
    _price_round_nearest_int,
    _zone_side_ref_from_vung_cho,
)
from automation_tool.zones_state import Zone


def test_price_round_nearest_int_half_up() -> None:
    assert _price_round_nearest_int(4755.4) == 4755.0
    assert _price_round_nearest_int(4755.5) == 4756.0
    assert _price_round_nearest_int(4755.49) == 4755.0


def test_touch_match_same_integer_after_round() -> None:
    p_last = 2950.35
    alert = 2949.72
    p_n = _price_round_nearest_int(p_last)
    a_n = _price_round_nearest_int(alert)
    assert abs(p_n - a_n) <= _EPS_DEFAULT


def test_touch_adjacent_integers_no_match_when_default_eps_zero() -> None:
    """4755 vs 4756 after round → |Δ|=1 > default eps (0): không chạm."""
    p_last = 4755.2
    alert = 4756.4
    p_n = _price_round_nearest_int(p_last)
    a_n = _price_round_nearest_int(alert)
    assert p_n == 4755.0 and a_n == 4756.0
    assert abs(p_n - a_n) > _EPS_DEFAULT


def test_touch_no_match_when_gap_exceeds_eps() -> None:
    p_last = 2950.4
    alert = 2952.6
    p_n = _price_round_nearest_int(p_last)
    a_n = _price_round_nearest_int(alert)
    assert abs(p_n - a_n) > _EPS_DEFAULT


def test_zone_side_ref_buy_max_sell_min() -> None:
    z = Zone(
        id="plan_chinh",
        label="plan_chinh",
        vung_cho="4738.0–4742.0",
        side="BUY",
    )
    assert _zone_side_ref_from_vung_cho(z) == 4742.0
    z2 = Zone(
        id="plan_phu",
        label="plan_phu",
        vung_cho="4738.0–4742.0",
        side="SELL",
    )
    assert _zone_side_ref_from_vung_cho(z2) == 4738.0


def test_arm_uses_trade_line_ref() -> None:
    """Arm khi last−ref (ref từ parse trade_line) trong [0, 3] (BUY) hoặc [-3, 0] (SELL) cho plan_chinh/plan_phu."""
    tl_buy = "BUY LIMIT 4742.0 | SL 4735.0 | TP1 4750.0 | Lot 0.01"
    z_buy = Zone(
        id="a",
        label="plan_chinh",
        vung_cho="4738.0–4742.0",
        side="BUY",
        trade_line=tl_buy,
    )
    ref = 4742.0
    assert _arm_threshold_met_for_zone(z_buy, ref) is True  # diff 0
    assert _arm_threshold_met_for_zone(z_buy, ref + 2.5) is True
    assert _arm_threshold_met_for_zone(z_buy, ref + _ARM_THRESHOLD) is True
    assert _arm_threshold_met_for_zone(z_buy, ref + _ARM_THRESHOLD + 0.5) is False
    assert _arm_threshold_met_for_zone(z_buy, ref - 0.5) is False
    tl_sell = "SELL LIMIT 4738.0 | SL 4745.0 | TP1 4730.0 | Lot 0.01"
    z_sell = Zone(
        id="b",
        label="plan_phu",
        vung_cho="4738.0–4742.0",
        side="SELL",
        trade_line=tl_sell,
    )
    ref_s = 4738.0
    assert _arm_threshold_met_for_zone(z_sell, ref_s) is True  # diff 0
    assert _arm_threshold_met_for_zone(z_sell, ref_s - 2.5) is True
    assert _arm_threshold_met_for_zone(z_sell, ref_s - _ARM_THRESHOLD) is True
    assert _arm_threshold_met_for_zone(z_sell, ref_s - _ARM_THRESHOLD - 0.5) is False
    assert _arm_threshold_met_for_zone(z_sell, ref_s + 0.5) is False


def test_arm_scalp_narrower_than_default() -> None:
    """Scalp: dải ±1 thay vì ±3 (ref từ trade_line)."""
    z = Zone(
        id="s",
        label="scalp",
        vung_cho="4738.0–4742.0",
        side="BUY",
        trade_line="BUY LIMIT 4742.0 | SL 4735.0 | TP1 4750.0 | Lot 0.01",
    )
    ref = 4742.0
    assert _arm_threshold_met_for_zone(z, ref + ARM_THRESHOLD_TP1_SCALP) is True
    assert _arm_threshold_met_for_zone(z, ref + ARM_THRESHOLD_TP1_SCALP + 0.25) is False
    z2 = Zone(
        id="t",
        label="scalp",
        vung_cho="4738.0–4742.0",
        side="SELL",
        trade_line="SELL LIMIT 4738.0 | SL 4745.0 | TP1 4730.0 | Lot 0.01",
    )
    ref_s = 4738.0
    assert _arm_threshold_met_for_zone(z2, ref_s - ARM_THRESHOLD_TP1_SCALP) is True
    assert _arm_threshold_met_for_zone(z2, ref_s - ARM_THRESHOLD_TP1_SCALP - 0.25) is False
