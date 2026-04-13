"""Zone touch uses integer-rounded prices before ``eps`` comparison."""

from __future__ import annotations

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


def test_touch_adjacent_integers_still_match() -> None:
    """4755 vs 4756 after round → |Δ|=1 ≤ default eps."""
    p_last = 4755.2
    alert = 4756.4
    p_n = _price_round_nearest_int(p_last)
    a_n = _price_round_nearest_int(alert)
    assert p_n == 4755.0 and a_n == 4756.0
    assert abs(p_n - a_n) <= _EPS_DEFAULT


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


def test_arm_uses_same_ref_as_touch() -> None:
    z_buy = Zone(id="a", label="plan_chinh", vung_cho="4738.0–4742.0", side="BUY")
    ref = 4742.0
    assert _arm_threshold_met_for_zone(z_buy, ref + _ARM_THRESHOLD) is True
    assert _arm_threshold_met_for_zone(z_buy, ref + _ARM_THRESHOLD - 0.5) is False
    z_sell = Zone(id="b", label="plan_phu", vung_cho="4738.0–4742.0", side="SELL")
    ref_s = 4738.0
    assert _arm_threshold_met_for_zone(z_sell, ref_s - _ARM_THRESHOLD) is True
    assert _arm_threshold_met_for_zone(z_sell, ref_s - _ARM_THRESHOLD + 0.5) is False
