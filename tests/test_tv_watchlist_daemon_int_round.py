"""Zone touch compares prices directly (no rounding)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from automation_tool.openai_analysis_json import ARM_THRESHOLD_TP1_SCALP
from automation_tool.state_files import read_last_response_id, write_last_response_id
from automation_tool.tv_watchlist_daemon import (
    _ARM_THRESHOLD,
    _DAEMON_PLAN_SL_LOAI_STATUSES,
    _EPS_DEFAULT,
    _arm_threshold_met_for_zone,
    _invalidate_same_side_zones_after_touch,
    _maybe_loai_zone_if_last_hit_sl,
    WatchlistDaemonParams,
    _daemon_plan_response_id_path,
    _openai_followup_persist_new_id,
    _openai_followup_prev_response_id,
    _should_write_intraday_alert_anchor,
)
from automation_tool.zones_state import Zone, ZonesState


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


def test_intraday_alert_anchor_only_writes_when_sidecar_empty(tmp_path) -> None:
    """[INTRADAY_ALERT] lần đầu ghi anchor; sidecar đã có id thì không ghi đè (retry tái dùng id)."""
    shard = tmp_path / "vung_sang.json"
    sidecar = _daemon_plan_response_id_path(shard)
    base = dict(
        coinmap_tv_yaml=tmp_path / "coinmap_tv.yaml",
        capture_coinmap_yaml=tmp_path / "cap.yaml",
        charts_dir=tmp_path / "charts",
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        shard_path=shard,
    )
    assert _should_write_intraday_alert_anchor(WatchlistDaemonParams(**base)) is True
    write_last_response_id("first-alert-id", path=sidecar)
    assert _should_write_intraday_alert_anchor(WatchlistDaemonParams(**base)) is False
    no_shard = WatchlistDaemonParams(
        coinmap_tv_yaml=tmp_path / "coinmap_tv.yaml",
        capture_coinmap_yaml=tmp_path / "cap.yaml",
        charts_dir=tmp_path / "charts",
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
    )
    assert _should_write_intraday_alert_anchor(no_shard) is False


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


def test_touch_exact_match_when_default_eps_zero() -> None:
    p_last = 2950.35
    alert = 2950.35
    assert abs(float(p_last) - float(alert)) <= _EPS_DEFAULT


def test_touch_no_match_when_gap_exceeds_eps() -> None:
    p_last = 2950.4
    alert = 2952.6
    assert abs(float(p_last) - float(alert)) > _EPS_DEFAULT


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


def test_daemon_plan_sl_loai_includes_post_entry_statuses() -> None:
    assert _DAEMON_PLAN_SL_LOAI_STATUSES == frozenset(
        {"vung_cho", "cham", "vao_lenh", "cho_tp1"}
    )


def test_maybe_loai_zone_if_sl_hit_applies_to_vao_lenh_cho_tp1(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("automation_tool.tv_watchlist_daemon._send_log", lambda *a, **k: None)
    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon._send_user_notice", lambda *a, **k: None
    )
    settings = MagicMock()
    params = WatchlistDaemonParams(
        coinmap_tv_yaml=tmp_path / "coinmap_tv.yaml",
        capture_coinmap_yaml=tmp_path / "cap.yaml",
        charts_dir=tmp_path / "charts",
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        mt5_symbol="XAUUSD",
    )
    tl = "BUY LIMIT 100 | SL 99 | TP1 101 | Lot 0.01"
    z_in = Zone(
        id="z1",
        label="plan_chinh",
        vung_cho="98–100",
        side="BUY",
        status="vao_lenh",
        trade_line=tl,
    )
    assert _maybe_loai_zone_if_last_hit_sl(z_in, 98.9, settings=settings, params=params) is True
    assert z_in.status == "loai"
    z_tp = Zone(
        id="z2",
        label="plan_chinh",
        vung_cho="98–100",
        side="BUY",
        status="cho_tp1",
        trade_line=tl,
    )
    assert _maybe_loai_zone_if_last_hit_sl(z_tp, 98.9, settings=settings, params=params) is True
    assert z_tp.status == "loai"


def test_invalidate_same_side_sell_uses_hi_excludes_scalp_and_non_waiting_statuses() -> None:
    st = ZonesState(
        symbol="XAUUSD",
        zones=[
            Zone(id="touched", label="plan_chinh", vung_cho="10–12", side="SELL", status="cham"),
            Zone(id="loai1", label="plan_phu", vung_cho="8–9", side="SELL", status="vung_cho"),
            Zone(id="keep1", label="plan_phu", vung_cho="12–13", side="SELL", status="vung_cho"),
            Zone(id="scalp_low", label="scalp", vung_cho="1–2", side="SELL", status="vung_cho"),
            Zone(id="other_side", label="plan_phu", vung_cho="100–101", side="BUY", status="vung_cho"),
            Zone(id="post_entry", label="plan_phu", vung_cho="1–3", side="SELL", status="vao_lenh"),
        ],
    )
    touched = st.zones[0]
    invalidated = _invalidate_same_side_zones_after_touch(st, touched_zone=touched)
    assert {z.id for z, _prev, _ref in invalidated} == {"loai1"}
    by_id = {z.id: z for z in st.zones}
    assert by_id["loai1"].status == "loai"
    assert by_id["keep1"].status == "vung_cho"
    assert by_id["scalp_low"].status == "vung_cho"
    assert by_id["other_side"].status == "vung_cho"
    assert by_id["post_entry"].status == "vao_lenh"


def test_invalidate_same_side_buy_uses_lo_only_waiting_and_touched() -> None:
    st = ZonesState(
        symbol="XAUUSD",
        zones=[
            Zone(id="touched", label="plan_chinh", vung_cho="10–12", side="BUY", status="cham"),
            Zone(id="loai1", label="plan_phu", vung_cho="13–14", side="BUY", status="vung_cho"),
            Zone(id="keep1", label="plan_phu", vung_cho="9–9.5", side="BUY", status="vung_cho"),
            Zone(id="keep2", label="plan_phu", vung_cho="20–21", side="BUY", status="cho_tp1"),
        ],
    )
    touched = st.zones[0]
    invalidated = _invalidate_same_side_zones_after_touch(st, touched_zone=touched)
    assert {z.id for z, _prev, _ref in invalidated} == {"loai1"}
    by_id = {z.id: z for z in st.zones}
    assert by_id["loai1"].status == "loai"
    assert by_id["keep1"].status == "vung_cho"
    assert by_id["keep2"].status == "cho_tp1"
