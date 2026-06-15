from unittest.mock import MagicMock

from automation_tool.mt5_accounts import MT5AccountEntry, LotRuleFromTrade
from automation_tool.tv_watchlist_daemon import (
    WatchlistDaemonParams,
    _auto_entry_job,
    _filter_entry_accounts_for_zone,
    _mark_zone_loai_no_entry_accounts,
    _resolve_zone_entry_accounts,
)
from automation_tool.zones_state import Zone, ZonesState, read_zones_state_from_shard, write_zones_state_to_shard


def test_all_2_zone_entry_allows_all_accounts_in_subset(tmp_path) -> None:
    accounts = [
        MT5AccountEntry(
            id="primary",
            terminal_path="/tmp/mt5-a.exe",
            login=1,
            password="p",
            server="srv",
            primary=True,
            lot=LotRuleFromTrade(),
        ),
        MT5AccountEntry(
            id="secondary",
            terminal_path="/tmp/mt5-b.exe",
            login=2,
            password="p",
            server="srv",
            primary=False,
            lot=LotRuleFromTrade(),
        ),
    ]
    zone = Zone(
        id="plan_chinh__sang-2",
        label="plan_chinh",
        vung_cho="100-101",
        side="BUY",
        source="all-2",
        session_slot="sang",
    )
    params = WatchlistDaemonParams(
        coinmap_tv_yaml=tmp_path / "coinmap.yaml",
        capture_coinmap_yaml=tmp_path / "capture.yaml",
        charts_dir=tmp_path / "charts",
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        shard_path=tmp_path / "vung_plan_chinh_sang-2.json",
    )

    allowed, slot, blocked = _filter_entry_accounts_for_zone(accounts, zone, params)

    assert slot == "sang"
    assert {a.id for a in allowed} == {"primary", "secondary"}
    assert blocked == []


def test_all_2_entry_still_respects_entry_slots(tmp_path) -> None:
    accounts = [
        MT5AccountEntry(
            id="primary",
            terminal_path="/tmp/mt5-a.exe",
            login=1,
            password="p",
            server="srv",
            primary=True,
            lot=LotRuleFromTrade(),
            entry_slots=("chieu",),
        ),
        MT5AccountEntry(
            id="secondary",
            terminal_path="/tmp/mt5-b.exe",
            login=2,
            password="p",
            server="srv",
            primary=False,
            lot=LotRuleFromTrade(),
            entry_slots=("chieu",),
        ),
    ]
    zone = Zone(
        id="plan_chinh__sang-2",
        label="plan_chinh",
        vung_cho="100-101",
        side="BUY",
        source="all-2",
        session_slot="sang",
    )
    params = WatchlistDaemonParams(
        coinmap_tv_yaml=tmp_path / "coinmap.yaml",
        capture_coinmap_yaml=tmp_path / "capture.yaml",
        charts_dir=tmp_path / "charts",
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        shard_path=tmp_path / "vung_plan_chinh_sang-2.json",
    )

    allowed, slot, blocked = _filter_entry_accounts_for_zone(accounts, zone, params)

    assert slot == "sang"
    assert allowed == []
    assert blocked == ["primary", "secondary"]


def test_only_plan_chinh_blocks_non_chinh_zone(tmp_path) -> None:
    accounts = [
        MT5AccountEntry(
            id="chinh_only",
            terminal_path="/tmp/mt5-a.exe",
            login=1,
            password="p",
            server="srv",
            primary=True,
            lot=LotRuleFromTrade(),
            only_plan_chinh=True,
        ),
        MT5AccountEntry(
            id="all_plans",
            terminal_path="/tmp/mt5-b.exe",
            login=2,
            password="p",
            server="srv",
            primary=False,
            lot=LotRuleFromTrade(),
        ),
    ]
    zone = Zone(
        id="plan_phu__sang",
        label="plan_phu",
        vung_cho="100-101",
        side="BUY",
        source="all",
        session_slot="sang",
    )
    params = WatchlistDaemonParams(
        coinmap_tv_yaml=tmp_path / "coinmap.yaml",
        capture_coinmap_yaml=tmp_path / "capture.yaml",
        charts_dir=tmp_path / "charts",
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        shard_path=tmp_path / "vung_plan_phu_sang.json",
    )

    allowed, slot, blocked = _filter_entry_accounts_for_zone(accounts, zone, params)

    assert slot == "sang"
    assert {a.id for a in allowed} == {"all_plans"}
    assert blocked == ["chinh_only"]


def test_only_plan_chinh_allows_chinh_shard_by_zone_id(tmp_path) -> None:
    accounts = [
        MT5AccountEntry(
            id="chinh_only",
            terminal_path="/tmp/mt5-a.exe",
            login=1,
            password="p",
            server="srv",
            primary=True,
            lot=LotRuleFromTrade(),
            only_plan_chinh=True,
        ),
    ]
    zone = Zone(
        id="plan_chinh__toi-2",
        label="plan_chinh",
        vung_cho="100-101",
        side="BUY",
        source="all-2",
        session_slot="toi",
    )
    params = WatchlistDaemonParams(
        coinmap_tv_yaml=tmp_path / "coinmap.yaml",
        capture_coinmap_yaml=tmp_path / "capture.yaml",
        charts_dir=tmp_path / "charts",
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        shard_path=tmp_path / "vung_plan_chinh_toi-2.json",
    )

    allowed, slot, blocked = _filter_entry_accounts_for_zone(accounts, zone, params)

    assert slot == "toi"
    assert [a.id for a in allowed] == ["chinh_only"]
    assert blocked == []


def test_mark_zone_loai_no_entry_accounts_sets_terminal_status(tmp_path) -> None:
    zone = Zone(
        id="plan_chinh__sang",
        label="plan_chinh",
        vung_cho="100-101",
        side="BUY",
        status="dang_vao_lenh",
        retry_at="2026-01-01T00:00:00+00:00",
        auto_entry_retry_after="2026-01-01T00:00:00+00:00",
        auto_entry_mt5_failed=True,
        mt5_ticket=99,
    )
    params = WatchlistDaemonParams(
        coinmap_tv_yaml=tmp_path / "coinmap.yaml",
        capture_coinmap_yaml=tmp_path / "capture.yaml",
        charts_dir=tmp_path / "charts",
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        shard_path=tmp_path / "vung_plan_chinh_sang.json",
    )

    _mark_zone_loai_no_entry_accounts(
        zone,
        zone_id=zone.id,
        settings=MagicMock(),
        params=params,
        slot="sang",
        log_prefix="auto-entry",
    )

    assert zone.status == "loai"
    assert zone.loai_streak == 0
    assert zone.retry_at == ""
    assert zone.auto_entry_retry_after == ""
    assert zone.auto_entry_mt5_failed is False
    assert zone.mt5_ticket is None
    assert zone.mt5_tickets_by_account is None


def test_mark_zone_loai_no_entry_accounts_is_idempotent(monkeypatch, tmp_path) -> None:
    zone = Zone(
        id="plan_chinh__sang",
        label="plan_chinh",
        vung_cho="100-101",
        side="BUY",
        status="loai",
    )
    params = WatchlistDaemonParams(
        coinmap_tv_yaml=tmp_path / "coinmap.yaml",
        capture_coinmap_yaml=tmp_path / "capture.yaml",
        charts_dir=tmp_path / "charts",
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        shard_path=tmp_path / "vung_plan_chinh_sang.json",
    )
    notices: list = []
    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon._send_user_notice",
        lambda *a, **k: notices.append((a, k)),
    )
    monkeypatch.setattr("automation_tool.tv_watchlist_daemon._send_log", lambda *a, **k: None)

    assert _mark_zone_loai_no_entry_accounts(
        zone,
        zone_id=zone.id,
        settings=MagicMock(),
        params=params,
        slot="sang",
        log_prefix="auto-entry",
    )
    assert notices == []


def test_resolve_zone_entry_accounts_empty_when_all_filtered(monkeypatch, tmp_path) -> None:
    chieu_only = MT5AccountEntry(
        id="chieu_only",
        terminal_path="/tmp/mt5.exe",
        login=1,
        password="p",
        server="srv",
        primary=True,
        lot=LotRuleFromTrade(),
        entry_slots=("chieu",),
    )
    zone = Zone(
        id="plan_chinh__sang",
        label="plan_chinh",
        vung_cho="100-101",
        side="BUY",
        source="all",
        session_slot="sang",
    )
    params = WatchlistDaemonParams(
        coinmap_tv_yaml=tmp_path / "coinmap.yaml",
        capture_coinmap_yaml=tmp_path / "capture.yaml",
        charts_dir=tmp_path / "charts",
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        shard_path=tmp_path / "vung_plan_chinh_sang.json",
    )
    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon.load_mt5_accounts_for_zone_entry",
        lambda **_kwargs: [chieu_only],
    )

    exec_accs, slot, blocked, missing = _resolve_zone_entry_accounts(zone, params)

    assert exec_accs == []
    assert slot == "sang"
    assert blocked == ["chieu_only"]
    assert missing is None


def test_auto_entry_job_marks_loai_when_no_entry_accounts(monkeypatch, tmp_path) -> None:
    shard = tmp_path / "vung_plan_chinh_sang.json"
    write_zones_state_to_shard(
        shard,
        ZonesState(
            symbol="XAUUSD",
            zones=[
                Zone(
                    id="plan_chinh__sang",
                    label="plan_chinh",
                    vung_cho="100-101",
                    side="BUY",
                    source="all",
                    session_slot="sang",
                    status="dang_vao_lenh",
                    hop_luu=71,
                    trade_line="BUY LIMIT 100 | SL 99 | TP1 103 | Lot 0.01",
                )
            ],
        ),
    )
    params = WatchlistDaemonParams(
        coinmap_tv_yaml=tmp_path / "coinmap.yaml",
        capture_coinmap_yaml=tmp_path / "capture.yaml",
        charts_dir=tmp_path / "charts",
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        shard_path=shard,
        mt5_execute=True,
    )
    chieu_only = MT5AccountEntry(
        id="chieu_only",
        terminal_path="/tmp/mt5.exe",
        login=1,
        password="p",
        server="srv",
        primary=True,
        lot=LotRuleFromTrade(),
        entry_slots=("chieu",),
    )

    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon.load_mt5_accounts_for_zone_entry",
        lambda **_kwargs: [chieu_only],
    )
    execute_calls: list = []
    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon.execute_trade_all_accounts",
        lambda *args, **kwargs: execute_calls.append((args, kwargs)) or MagicMock(),
    )
    monkeypatch.setattr("automation_tool.tv_watchlist_daemon._send_log", lambda *a, **k: None)
    monkeypatch.setattr("automation_tool.tv_watchlist_daemon._send_user_notice", lambda *a, **k: None)

    _auto_entry_job(settings=MagicMock(), params=params, zone_id="plan_chinh__sang")

    st = read_zones_state_from_shard(shard)
    assert st is not None
    assert st.zones[0].status == "loai"
    assert execute_calls == []
