"""Zone touch uses integer-rounded prices before ``eps`` comparison."""

from __future__ import annotations

from automation_tool.tv_watchlist_daemon import _EPS_DEFAULT, _price_round_nearest_int


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
