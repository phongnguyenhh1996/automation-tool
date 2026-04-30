"""Zone touch compares prices directly (no rounding)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from automation_tool.openai_analysis_json import ARM_THRESHOLD_TP1_SCALP
from automation_tool.state_files import read_last_response_id, write_last_response_id
from automation_tool.tradingview_touch_flow import LOAI_CONFIRM_ROUNDS
from automation_tool.tv_watchlist_daemon import (
    _ARM_THRESHOLD,
    _DAEMON_PLAN_SL_LOAI_STATUSES,
    _EPS_DEFAULT,
    _ZONE_TOUCH_INITIAL_DELAY_MINUTES,
    _ZONE_TOUCH_LOAI_CONFIRM_ROUNDS,
    _apply_zone_touch_loai_decision,
    _arm_threshold_met_for_zone,
    _daemon_plan_main_loop,
    _invalidate_same_side_zones_after_touch,
    _mark_initial_zone_touch_wait,
    _maybe_loai_zone_if_last_hit_sl,
    _skip_scalp_r1_followup_if_needed,
    WatchlistDaemonParams,
    _daemon_plan_response_id_path,
    _openai_followup_persist_new_id,
    _openai_followup_prev_response_id,
    _entry_touched_for_position_check,
    _should_check_managed_tp_done,
    _r1_followup_job,
    _should_write_intraday_alert_anchor,
    _tp1_followup_job,
)
from automation_tool.zones_state import Zone, ZonesState, read_zones_state_from_shard, write_zones_state_to_shard


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


def test_daemon_plan_cutoff_marks_zone_loai_before_exit(monkeypatch, tmp_path) -> None:
    shard = tmp_path / "vung_plan_chinh_sang.json"
    write_zones_state_to_shard(
        shard,
        ZonesState(
            symbol="XAUUSD",
            zones=[
                Zone(
                    id="plan_chinh_sang",
                    label="plan_chinh",
                    vung_cho="100–101",
                    side="BUY",
                    status="vung_cho",
                    retry_at="2026-01-01T00:00:00+00:00",
                    loai_streak=2,
                )
            ],
        ),
    )
    params = WatchlistDaemonParams(
        coinmap_tv_yaml=tmp_path / "coinmap_tv.yaml",
        capture_coinmap_yaml=tmp_path / "cap.yaml",
        charts_dir=tmp_path / "charts",
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        shard_path=shard,
    )
    past_deadline = datetime.now(timezone.utc) - timedelta(minutes=1)

    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon.compute_daemon_plan_effective_stop_deadline_local",
        lambda *a, **k: past_deadline,
    )
    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon.daemon_plan_resolve_cutoff_mt5",
        lambda *a, **k: (False, "cutoff: không còn pending/position theo ticket trong state"),
    )
    monkeypatch.setattr("automation_tool.tv_watchlist_daemon._send_log", lambda *a, **k: None)
    monkeypatch.setattr("automation_tool.tv_watchlist_daemon._send_user_notice", lambda *a, **k: None)

    _daemon_plan_main_loop(settings=MagicMock(), params=params, sym="XAUUSD", poll_s=0.01)

    st = read_zones_state_from_shard(shard)
    assert st is not None
    assert st.zones[0].status == "loai"
    assert st.zones[0].retry_at == ""
    assert st.zones[0].loai_streak == 0


def test_touch_exact_match_when_default_eps_zero() -> None:
    p_last = 2950.35
    alert = 2950.35
    assert abs(float(p_last) - float(alert)) <= _EPS_DEFAULT


def test_touch_no_match_when_gap_exceeds_eps() -> None:
    p_last = 2950.4
    alert = 2952.6
    assert abs(float(p_last) - float(alert)) > _EPS_DEFAULT


def test_initial_zone_touch_waits_10_minutes_and_notifies(monkeypatch, tmp_path) -> None:
    notices: list[tuple[str, str, str]] = []
    logs: list[str] = []

    def fake_notice(settings, title, body="", *, zone=None, params=None, zone_label=None):
        notices.append((title, body, zone.id if zone is not None else ""))

    monkeypatch.setattr("automation_tool.tv_watchlist_daemon._send_user_notice", fake_notice)
    monkeypatch.setattr("automation_tool.tv_watchlist_daemon._send_log", lambda _settings, text: logs.append(text))

    settings = MagicMock()
    params = WatchlistDaemonParams(
        coinmap_tv_yaml=tmp_path / "coinmap_tv.yaml",
        capture_coinmap_yaml=tmp_path / "cap.yaml",
        charts_dir=tmp_path / "charts",
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        shard_path=tmp_path / "vung_plan_chinh_sang.json",
    )
    zone = Zone(
        id="plan_chinh_sang",
        label="plan_chinh",
        vung_cho="100–101",
        side="BUY",
        status="vung_cho",
    )
    st = ZonesState(symbol="XAUUSD", zones=[zone])

    before = datetime.now(timezone.utc) + timedelta(minutes=_ZONE_TOUCH_INITIAL_DELAY_MINUTES)
    invalidated = _mark_initial_zone_touch_wait(
        st,
        touched_zone=zone,
        last_price=100.5,
        settings=settings,
        params=params,
    )
    after = datetime.now(timezone.utc) + timedelta(minutes=_ZONE_TOUCH_INITIAL_DELAY_MINUTES)

    retry_at = datetime.fromisoformat(zone.retry_at)
    assert invalidated == []
    assert zone.status == "cham"
    assert before <= retry_at <= after
    assert notices == [
        (
            "Giá đã chạm vùng chờ.",
            "Hệ thống sẽ đợi 10 phút rồi kiểm tra lại với AI.",
            "plan_chinh_sang",
        )
    ]
    assert any("initial_touch_wait" in line for line in logs)


def test_zone_touch_loai_decision_requires_three_confirmations() -> None:
    assert _ZONE_TOUCH_LOAI_CONFIRM_ROUNDS == 3
    assert LOAI_CONFIRM_ROUNDS == 3

    zone = Zone(
        id="plan_chinh_sang",
        label="plan_chinh",
        vung_cho="100–101",
        side="BUY",
        status="cham",
    )

    assert _apply_zone_touch_loai_decision(zone) is False
    assert zone.status == "cham"
    assert zone.loai_streak == 1
    assert zone.retry_at

    assert _apply_zone_touch_loai_decision(zone) is False
    assert zone.status == "cham"
    assert zone.loai_streak == 2
    assert zone.retry_at

    assert _apply_zone_touch_loai_decision(zone) is True
    assert zone.status == "loai"
    assert zone.loai_streak == 3
    assert zone.retry_at == ""


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


def test_skip_scalp_r1_followup_no_longer_skips() -> None:
    z = Zone(
        id="s1",
        label="scalp",
        vung_cho="100–101",
        side="BUY",
        status="cho_tp1",
        trade_line="BUY LIMIT 100 | SL 99 | TP1 101 | Lot 0.01",
        mt5_ticket=123,
    )

    assert _skip_scalp_r1_followup_if_needed(z, settings=MagicMock(), params=MagicMock()) is False
    assert z.r1_followup_done is False


def test_tp1_followup_scalp_calls_openai_instead_of_auto_cancel(monkeypatch, tmp_path) -> None:
    notices: list[tuple[str, str, str]] = []
    logs: list[str] = []
    openai_calls: list[dict] = []

    def fake_notice(settings, title, body="", *, zone=None, params=None, zone_label=None):
        notices.append((title, body, zone.label if zone is not None else ""))

    monkeypatch.setattr("automation_tool.tv_watchlist_daemon._send_user_notice", fake_notice)
    monkeypatch.setattr("automation_tool.tv_watchlist_daemon._send_log", lambda _settings, text: logs.append(text))
    chart_json = tmp_path / "charts" / "coinmap.json"
    chart_json.parent.mkdir()
    chart_json.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("automation_tool.coinmap.capture_charts", lambda **_kwargs: [chart_json])
    monkeypatch.setattr("automation_tool.images.read_main_chart_symbol", lambda _charts_dir: "XAUUSD")
    monkeypatch.setattr("automation_tool.images.coinmap_xauusd_5m_json_path", lambda _charts_dir: chart_json)
    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon.write_openai_coinmap_merged_from_raw_export",
        lambda path: path,
    )

    def fake_run_single_followup_responses(**kwargs):
        openai_calls.append(kwargs)
        return ('{"hanh_dong_quan_ly_lenh":"giu_nguyen","reason":"Scalp vẫn hợp lệ."}', "resp-scalp-tp1")

    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon.run_single_followup_responses",
        fake_run_single_followup_responses,
    )
    acc = MagicMock(terminal_path="/tmp/mt5-primary/terminal64.exe", login=111001, password="p", server="srv")
    monkeypatch.setattr("automation_tool.tv_watchlist_daemon.load_mt5_accounts_for_cli", lambda *_a, **_k: [acc])
    monkeypatch.setattr("automation_tool.tv_watchlist_daemon.primary_account", lambda accs: accs[0])
    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon.mt5_ticket_still_open",
        lambda *_a, **_k: (True, "ticket=123 open"),
    )
    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon.mt5_ticket_is_open_position",
        lambda *_a, **_k: (True, "ticket=123 is position"),
    )
    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon.mt5_ticket_current_sltp",
        lambda *_a, **_k: (99.0, 101.0, "ok"),
    )

    settings = MagicMock()
    params = WatchlistDaemonParams(
        coinmap_tv_yaml=tmp_path / "coinmap_tv.yaml",
        capture_coinmap_yaml=tmp_path / "cap.yaml",
        charts_dir=tmp_path / "charts",
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        shard_path=tmp_path / "vung_scalp_sang.json",
        no_telegram=True,
    )
    write_zones_state_to_shard(
        params.shard_path,
        ZonesState(
            symbol="XAUUSD",
            zones=[
                Zone(
                    id="s1",
                    label="scalp",
                    vung_cho="100–101",
                    side="BUY",
                    status="dang_thuc_thi",
                    trade_line="BUY LIMIT 100 | SL 99 | TP1 101 | Lot 0.01",
                    mt5_ticket=123,
                    tp1_followup_done=True,
                )
            ],
        ),
    )

    _tp1_followup_job(settings=settings, params=params, zone_id="s1", p_last=101.0)

    st = read_zones_state_from_shard(params.shard_path)
    assert st is not None
    assert st.zones[0].status == "vao_lenh"
    assert st.zones[0].tp1_followup_done is True
    assert openai_calls
    assert notices and notices[0][2] == "scalp"
    assert any("OpenAI TRADE_MANAGEMENT" in line for line in logs)


def test_r1_followup_skips_pending_ticket_before_capture(monkeypatch, tmp_path) -> None:
    shard = tmp_path / "vung_plan_chinh_sang.json"
    write_zones_state_to_shard(
        shard,
        ZonesState(
            symbol="XAUUSD",
            zones=[
                Zone(
                    id="z1",
                    label="plan_chinh",
                    vung_cho="100–101",
                    side="BUY",
                    status="dang_thuc_thi",
                    trade_line="BUY LIMIT 100 | SL 99 | TP1 103 | Lot 0.01",
                    mt5_ticket=123,
                    r1_followup_done=True,
                )
            ],
        ),
    )
    params = WatchlistDaemonParams(
        coinmap_tv_yaml=tmp_path / "coinmap_tv.yaml",
        capture_coinmap_yaml=tmp_path / "cap.yaml",
        charts_dir=tmp_path / "charts",
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        shard_path=shard,
        mt5_execute=True,
    )

    monkeypatch.setattr("automation_tool.tv_watchlist_daemon.load_mt5_accounts_for_cli", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon.mt5_ticket_is_open_position",
        lambda *_a, **_k: (False, "ticket=123 vẫn pending (chưa position)"),
    )
    monkeypatch.setattr("automation_tool.tv_watchlist_daemon._send_log", lambda *a, **k: None)
    monkeypatch.setattr("automation_tool.tv_watchlist_daemon._send_user_notice", lambda *a, **k: None)

    def fail_capture(*_args, **_kwargs):
        raise AssertionError("pending ticket must not capture Coinmap for R1")

    monkeypatch.setattr("automation_tool.coinmap.capture_charts", fail_capture)

    _r1_followup_job(
        settings=MagicMock(),
        params=params,
        zone_id="z1",
        prev_status="cho_tp1",
        reached_r_level=1,
    )

    st = read_zones_state_from_shard(shard)
    assert st is not None
    assert st.zones[0].status == "cho_tp1"
    assert st.zones[0].r1_followup_done is False


def test_r1_followup_restores_cho_tp1_after_successful_inplace_change(monkeypatch, tmp_path) -> None:
    shard = tmp_path / "vung_plan_chinh_sang.json"
    write_zones_state_to_shard(
        shard,
        ZonesState(
            symbol="XAUUSD",
            zones=[
                Zone(
                    id="z1",
                    label="plan_chinh",
                    vung_cho="100–101",
                    side="BUY",
                    status="dang_thuc_thi",
                    trade_line="BUY LIMIT 100 | SL 99 | TP1 103 | Lot 0.01",
                    mt5_ticket=123,
                    r1_followup_done=True,
                )
            ],
        ),
    )
    params = WatchlistDaemonParams(
        coinmap_tv_yaml=tmp_path / "coinmap_tv.yaml",
        capture_coinmap_yaml=tmp_path / "cap.yaml",
        charts_dir=tmp_path / "charts",
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        no_telegram=True,
        shard_path=shard,
        mt5_execute=True,
        mt5_symbol="XAUUSD",
    )
    charts_file = tmp_path / "x.json"
    charts_file.write_text("{}", encoding="utf-8")

    acc = MagicMock(terminal_path="/tmp/mt5-primary/terminal64.exe", login=111001, password="p", server="srv")
    monkeypatch.setattr("automation_tool.tv_watchlist_daemon.load_mt5_accounts_for_cli", lambda *_a, **_k: [acc])
    monkeypatch.setattr("automation_tool.tv_watchlist_daemon.primary_account", lambda accs: accs[0])
    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon.mt5_ticket_is_open_position",
        lambda *_a, **_k: (True, "ticket=123 is position"),
    )
    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon.mt5_ticket_current_sltp",
        lambda *_a, **_k: (99.0, 103.0, "ok"),
    )
    monkeypatch.setattr("automation_tool.coinmap.capture_charts", lambda **_k: [charts_file])
    monkeypatch.setattr("automation_tool.images.coinmap_xauusd_5m_json_path", lambda _charts_dir: charts_file)
    monkeypatch.setattr("automation_tool.images.read_main_chart_symbol", lambda _charts_dir: "XAUUSD")
    monkeypatch.setattr("automation_tool.tv_watchlist_daemon.write_openai_coinmap_merged_from_raw_export", lambda p: p)
    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon.run_single_followup_responses",
        lambda **_k: (
            '{"hanh_dong_quan_ly_lenh":"chinh_trade_line","new_SL":100,"new_TP":103,"reason":"Dời SL."}',
            "resp-r1",
        ),
    )
    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon.mt5_chinh_trade_line_inplace",
        lambda *_a, **_k: MagicMock(ok=True, outcome="modified_sltp", message="modified"),
    )
    monkeypatch.setattr("automation_tool.tv_watchlist_daemon._send_log", lambda *a, **k: None)
    monkeypatch.setattr("automation_tool.tv_watchlist_daemon._send_user_notice", lambda *a, **k: None)

    _r1_followup_job(
        settings=MagicMock(),
        params=params,
        zone_id="z1",
        prev_status="cho_tp1",
        reached_r_level=2,
    )

    st = read_zones_state_from_shard(shard)
    assert st is not None
    assert st.zones[0].status == "cho_tp1"
    assert st.zones[0].trade_line == "BUY LIMIT 100 | SL 99 | TP1 103 | Lot 0.01"
    assert st.zones[0].r1_followup_done is False
    assert st.zones[0].tp1_followup_done is False
    assert st.zones[0].managed_sl == 100.0
    assert st.zones[0].managed_tp == 103.0
    assert st.zones[0].last_r_followup_level == 2


def test_entry_touch_check_uses_trade_entry_price() -> None:
    parsed_buy = MagicMock(side="BUY", price=100.0)
    parsed_sell = MagicMock(side="SELL", price=100.0)
    assert _entry_touched_for_position_check(parsed_buy, 99.99) is True
    assert _entry_touched_for_position_check(parsed_buy, 100.5) is False
    assert _entry_touched_for_position_check(parsed_sell, 100.01) is True
    assert _entry_touched_for_position_check(parsed_sell, 99.5) is False


def test_should_check_managed_tp_done_requires_has_position() -> None:
    parsed_buy = MagicMock(side="BUY")
    z = Zone(
        id="z-managed",
        label="plan_chinh",
        vung_cho="100–101",
        side="BUY",
        status="cho_tp1",
        managed_tp=101.0,
        has_position=False,
    )
    assert _should_check_managed_tp_done(z, parsed_buy, 101.0) is False
    z.has_position = True
    assert _should_check_managed_tp_done(z, parsed_buy, 101.0) is True


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


def test_maybe_loai_zone_if_sl_hit_prefers_managed_sl(monkeypatch, tmp_path) -> None:
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
    z = Zone(
        id="z-managed",
        label="plan_chinh",
        vung_cho="98–100",
        side="BUY",
        status="cho_tp1",
        trade_line="BUY LIMIT 100 | SL 99 | TP1 101 | Lot 0.01",
        managed_sl=100.5,
    )
    assert _maybe_loai_zone_if_last_hit_sl(z, 100.4, settings=settings, params=params) is True
    assert z.status == "loai"


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
