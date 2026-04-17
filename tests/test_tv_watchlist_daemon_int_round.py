"""Zone touch uses integer-rounded prices before ``eps`` comparison."""

from __future__ import annotations

from pathlib import Path

from automation_tool.openai_analysis_json import ARM_THRESHOLD_TP1_SCALP
from automation_tool.state_files import read_last_response_id, write_last_response_id
from automation_tool.tv_watchlist_daemon import (
    _ARM_THRESHOLD,
    _EPS_DEFAULT,
    _arm_threshold_met_for_zone,
    WatchlistDaemonParams,
    _daemon_plan_response_id_path,
    _openai_followup_persist_new_id,
    _openai_followup_prev_response_id,
    _price_round_nearest_int,
    _zone_side_ref_from_vung_cho,
)
from automation_tool.zones_state import Zone


def test_daemon_plan_sidecar_filename_matches_json_stem() -> None:
    """``vung_plan_chinh_sang.json`` → ``vung_plan_chinh_sang.last_response_id.txt`` (cùng thư mục)."""
    shard = Path("/tmp/zones/vung_plan_chinh_sang.json")
    assert _daemon_plan_response_id_path(shard) == Path("/tmp/zones/vung_plan_chinh_sang.last_response_id.txt")


def test_daemon_plan_openai_sidecar_next_to_shard(tmp_path) -> None:
    """daemon-plan ghi chain id vào sidecar; không ghi last_response_id.txt chính."""
    shard = tmp_path / "vung_sang.json"
    sidecar = _daemon_plan_response_id_path(shard)
    assert sidecar == tmp_path / "vung_sang.last_response_id.txt"
    write_last_response_id("thread-a", path=sidecar)
    params = WatchlistDaemonParams(
        coinmap_tv_yaml=tmp_path / "coinmap_tv.yaml",
        capture_coinmap_yaml=tmp_path / "cap.yaml",
        charts_dir=tmp_path / "charts",
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        shard_path=shard,
    )
    assert _openai_followup_prev_response_id(params) == "thread-a"
    _openai_followup_persist_new_id(params, "thread-b")
    assert read_last_response_id(sidecar) == "thread-b"


def test_daemon_plan_prev_seeds_from_main_when_sidecar_empty(monkeypatch, tmp_path) -> None:
    calls: list[object] = []

    def fake_read(path=None):
        calls.append(path)
        if path is not None:
            return None
        return "seed-from-main"

    monkeypatch.setattr("automation_tool.tv_watchlist_daemon.read_last_response_id", fake_read)
    shard = tmp_path / "vung_sang.json"
    params = WatchlistDaemonParams(
        coinmap_tv_yaml=tmp_path / "coinmap_tv.yaml",
        capture_coinmap_yaml=tmp_path / "cap.yaml",
        charts_dir=tmp_path / "charts",
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        shard_path=shard,
    )
    assert _openai_followup_prev_response_id(params) == "seed-from-main"
    assert calls[0] == _daemon_plan_response_id_path(shard)
    assert calls[1] is None


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
