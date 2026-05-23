from automation_tool.mt5_accounts import MT5AccountEntry, LotRuleFromTrade
from automation_tool.tv_watchlist_daemon import (
    WatchlistDaemonParams,
    _filter_entry_accounts_for_zone,
)
from automation_tool.zones_state import Zone


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
