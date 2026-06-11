"""Tests for accounts.json loader (no MetaTrader5 runtime)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from automation_tool.mt5_accounts import (
    LotRuleFixed,
    LotRuleFromTrade,
    LotRuleMaxLossUsd,
    LotRuleMaxNotionalUsd,
    MT5AccountEntry,
    SOURCE_ALL_2,
    SOURCE_UPDATE_SCALP,
    apply_account_short_tp,
    account_tp_r_multiplier,
    account_row_in_all2_subset,
    account_row_in_scalp_subset,
    compute_lot_override,
    filter_mt5_accounts_for_entry_slot,
    filter_mt5_accounts_for_zone_entry,
    filter_mt5_accounts_for_zone_label,
    is_plan_chinh_family,
    is_plan_phu_family,
    is_scalp_family,
    load_mt5_accounts_from_path,
    load_mt5_accounts_for_zone_entry,
    resolve_account_entry_tp_price,
    resolve_trade_filter_key,
    trade_with_update_scalp_entry_lot_default,
    exclude_all2_dedicated_accounts,
    sync_accounts_all2_json,
    sync_accounts_scalp_json,
)
from automation_tool.mt5_execute import resolve_mt5_trade_symbol
from automation_tool.mt5_openai_parse import ParsedTrade


def _write_accounts(path: Path, data: list | dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def test_omitted_lot_uses_from_trade() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "a",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                },
                {
                    "id": "b",
                    "terminal_path": "C:/MT5/B/metatrader64.exe",
                    "login": 2,
                    "password": "y",
                    "server": "S",
                    "primary": False,
                    "lot": {"mode": "from_trade"},
                },
            ],
        )
        accs = load_mt5_accounts_from_path(p)
        assert isinstance(accs[0].lot, LotRuleFromTrade)
        assert isinstance(accs[1].lot, LotRuleFromTrade)


def test_null_lot_uses_from_trade() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "a",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "lot": None,
                },
            ],
        )
        accs = load_mt5_accounts_from_path(p)
        assert isinstance(accs[0].lot, LotRuleFromTrade)


def test_load_valid_two_accounts_one_primary() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "a",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "lot": {"mode": "fixed", "volume": 0.02},
                },
                {
                    "id": "b",
                    "terminal_path": "C:/MT5/B/metatrader64.exe",
                    "login": 2,
                    "password": "y",
                    "server": "S",
                    "primary": False,
                    "lot": {"mode": "max_notional_usd", "max_usd": 50},
                },
            ],
        )
        accs = load_mt5_accounts_from_path(p)
        assert len(accs) == 2
        assert accs[0].id == "a"
        assert isinstance(accs[0].lot, LotRuleFixed)
        assert accs[0].lot.volume == 0.02
        assert isinstance(accs[1].lot, LotRuleMaxNotionalUsd)
        assert accs[1].lot.max_usd == 50.0


def test_load_fixed_lot_accepts_volume_by_zone_label() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "a",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "lot": {
                        "mode": "fixed",
                        "volume": {
                            "plan_chinh": 0.02,
                            "plan_phu": 0.01,
                            "default": 0.01,
                        },
                    },
                },
            ],
        )
        accs = load_mt5_accounts_from_path(p)
        assert isinstance(accs[0].lot, LotRuleFixed)
        assert accs[0].lot.volume == {
            "plan_chinh": 0.02,
            "plan_phu": 0.01,
            "default": 0.01,
        }


def test_load_max_loss_usd_rule() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "a",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "lot": {"mode": "max_loss_usd", "max_usd": 100},
                },
            ],
        )
        accs = load_mt5_accounts_from_path(p)
        assert isinstance(accs[0].lot, LotRuleMaxLossUsd)
        assert accs[0].lot.max_usd == 100.0


def test_load_max_loss_usd_rule_accepts_budget_by_zone_label() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "a",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "lot": {
                        "mode": "max_loss_usd",
                        "max_usd": {
                            "plan_chinh": 100,
                            "plan_phu": 50,
                            "default": 20,
                        },
                    },
                },
            ],
        )
        accs = load_mt5_accounts_from_path(p)
        assert isinstance(accs[0].lot, LotRuleMaxLossUsd)
        assert accs[0].lot.max_usd == {
            "plan_chinh": 100.0,
            "plan_phu": 50.0,
            "default": 20.0,
        }


def test_compute_lot_override_max_loss_usd_uses_zone_label_budget() -> None:
    class _FakeMt5:
        ORDER_TYPE_BUY = 0

        @staticmethod
        def symbol_info(_symbol):
            class _Info:
                volume_step = 0.01
                volume_min = 0.01
                volume_max = 100.0

            return _Info()

        @staticmethod
        def order_calc_profit(_order_type, _symbol, _volume, _entry, _sl):
            return -200.0

    trade = ParsedTrade(
        symbol="XAUUSD",
        side="BUY",
        kind="LIMIT",
        price=2600.0,
        sl=2590.0,
        tp1=2610.0,
        tp2=2620.0,
        lot=0.02,
        raw_line="BUY LIMIT 2600.0 | SL 2590.0 | TP1 2610.0 | TP2 2620.0 | Lot 0.02",
    )

    vol_plan_chinh, _ = compute_lot_override(
        trade,
        LotRuleMaxLossUsd(
            max_usd={"plan_chinh": 100.0, "plan_phu": 50.0, "default": 20.0}
        ),
        mt5=_FakeMt5(),
        resolved_symbol="XAUUSD",
        dry_run=False,
        zone_label="plan_chinh",
    )
    vol_default, _ = compute_lot_override(
        trade,
        LotRuleMaxLossUsd(
            max_usd={"plan_chinh": 100.0, "plan_phu": 50.0, "default": 20.0}
        ),
        mt5=_FakeMt5(),
        resolved_symbol="XAUUSD",
        dry_run=False,
        zone_label="scalp",
    )

    assert vol_plan_chinh == 0.5
    assert vol_default == 0.1


def test_rejects_zero_primary() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "a",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": False,
                    "lot": {"mode": "fixed", "volume": 0.01},
                },
            ],
        )
        with pytest.raises(ValueError, match="primary"):
            load_mt5_accounts_from_path(p)


def test_rejects_duplicate_ids() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "a",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "lot": {"mode": "fixed", "volume": 0.01},
                },
                {
                    "id": "a",
                    "terminal_path": "C:/MT5/B/metatrader64.exe",
                    "login": 2,
                    "password": "y",
                    "server": "S",
                    "primary": False,
                    "lot": {"mode": "fixed", "volume": 0.01},
                },
            ],
        )
        with pytest.raises(ValueError, match="khác nhau"):
            load_mt5_accounts_from_path(p)


def test_load_symbol_map_optional() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "micro",
                    "terminal_path": "C:/MT5/micro/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "lot": {"mode": "fixed", "volume": 0.01},
                    "symbol_map": {"XAUUSD": "XAUUSDm", "EURUSD": "EURUSD"},
                },
                {
                    "id": "std",
                    "terminal_path": "C:/MT5/std/metatrader64.exe",
                    "login": 2,
                    "password": "y",
                    "server": "S",
                    "primary": False,
                    "lot": {"mode": "fixed", "volume": 0.01},
                    "symbol_map": {"XAUUSD": "XAUUSD"},
                },
            ],
        )
        accs = load_mt5_accounts_from_path(p)
        assert accs[0].symbol_map["XAUUSD"] == "XAUUSDm"
        assert accs[1].symbol_map["XAUUSD"] == "XAUUSD"


def test_load_account_tp_r_by_plan() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "a",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "tp": {
                        "plan_chinh": 0,
                        "plan_phu": 1.1,
                        "scalp": 1.1,
                    },
                },
            ],
        )
        accs = load_mt5_accounts_from_path(p)
        assert accs[0].tp_r == {
            "plan_chinh": 0.0,
            "plan_phu": 1.1,
            "scalp": 1.1,
        }
        assert account_tp_r_multiplier(accs[0].tp_r, "plan_phu") == 1.1
        assert account_tp_r_multiplier(accs[0].tp_r, "plan_chinh") == 0.0
        assert account_tp_r_multiplier(accs[0].tp_r, "scalp_2__chieu") == 1.1


def test_resolve_account_entry_tp_price_zero_uses_trade_tp1() -> None:
    from automation_tool.mt5_accounts import MT5AccountEntry

    trade = ParsedTrade(
        symbol="XAUUSD",
        side="BUY",
        kind="LIMIT",
        price=4709.0,
        sl=4699.0,
        tp1=4720.0,
        tp2=4730.0,
        lot=0.02,
        raw_line="",
    )
    entry = MT5AccountEntry(
        id="x",
        terminal_path="C:/MT5/A/metatrader64.exe",
        login=1,
        password="p",
        server="S",
        primary=True,
        lot=LotRuleFromTrade(),
        tp_r={"plan_chinh": 0, "plan_phu": 1.1},
    )
    assert resolve_account_entry_tp_price(trade, entry, "plan_chinh") == 4720.0
    assert resolve_account_entry_tp_price(trade, entry, "plan_phu") == pytest.approx(4720.0)
    assert resolve_account_entry_tp_price(trade, entry, None) is None


def test_load_entry_take_profit_per_account_defaults_to_tp2() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "tp1_runner",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "entry_take_profit": "tp1",
                },
                {
                    "id": "tp2_runner",
                    "terminal_path": "C:/MT5/B/metatrader64.exe",
                    "login": 2,
                    "password": "y",
                    "server": "S",
                    "primary": False,
                },
            ],
        )
        accs = load_mt5_accounts_from_path(p)
        assert accs[0].entry_take_profit == "tp1"
        assert accs[1].entry_take_profit == "tp2"


def test_load_entry_slots_per_account_defaults_to_all_slots() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "day_runner",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "entry_slots": ["sang", "chieu"],
                },
                {
                    "id": "all_day",
                    "terminal_path": "C:/MT5/B/metatrader64.exe",
                    "login": 2,
                    "password": "y",
                    "server": "S",
                    "primary": False,
                },
            ],
        )

        accs = load_mt5_accounts_from_path(p)

        assert accs[0].entry_slots == ("sang", "chieu")
        assert accs[1].entry_slots is None
        assert [a.id for a in filter_mt5_accounts_for_entry_slot(accs, "toi")] == ["all_day"]
        assert [a.id for a in filter_mt5_accounts_for_entry_slot(accs, "sang")] == [
            "day_runner",
            "all_day",
        ]


def test_load_only_plan_chinh_flag() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "chinh_only",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "only_plan_chinh": True,
                },
                {
                    "id": "all_plans",
                    "terminal_path": "C:/MT5/B/metatrader64.exe",
                    "login": 2,
                    "password": "y",
                    "server": "S",
                    "primary": False,
                },
            ],
        )
        accs = load_mt5_accounts_from_path(p)
        assert accs[0].only_plan_chinh is True
        assert accs[1].only_plan_chinh is False


def test_is_plan_chinh_family_includes_suffixed_zone_ids() -> None:
    assert is_plan_chinh_family("plan_chinh")
    assert is_plan_chinh_family("plan_chinh__sang")
    assert is_plan_chinh_family("plan_chinh__toi-2")
    assert is_plan_chinh_family(None, "plan_chinh__chieu")
    assert not is_plan_chinh_family("plan_phu")
    assert not is_plan_chinh_family("plan_phu__sang")
    assert not is_plan_chinh_family("scalp", "scalp_1")


def test_filter_only_plan_chinh_skips_other_labels() -> None:
    chinh_only = MT5AccountEntry(
        id="chinh_only",
        terminal_path="/tmp/mt5-a.exe",
        login=1,
        password="p",
        server="srv",
        primary=True,
        lot=LotRuleFromTrade(),
        only_plan_chinh=True,
    )
    all_plans = MT5AccountEntry(
        id="all_plans",
        terminal_path="/tmp/mt5-b.exe",
        login=2,
        password="p",
        server="srv",
        primary=False,
        lot=LotRuleFromTrade(),
    )
    accounts = [chinh_only, all_plans]

    assert [a.id for a in filter_mt5_accounts_for_zone_label(accounts, "plan_chinh")] == [
        "chinh_only",
        "all_plans",
    ]
    assert [a.id for a in filter_mt5_accounts_for_zone_label(accounts, "plan_chinh__sang-2")] == [
        "chinh_only",
        "all_plans",
    ]
    assert [a.id for a in filter_mt5_accounts_for_zone_label(accounts, "plan_phu")] == ["all_plans"]
    assert [a.id for a in filter_mt5_accounts_for_zone_label(accounts, "scalp_1")] == ["all_plans"]

    assert [a.id for a in filter_mt5_accounts_for_zone_entry(accounts, "sang", "plan_phu")] == [
        "all_plans"
    ]
    assert [a.id for a in filter_mt5_accounts_for_zone_entry(
        accounts, "sang", "plan_chinh", zone_id="plan_chinh__sang-2"
    )] == ["chinh_only", "all_plans"]
    assert [a.id for a in filter_mt5_accounts_for_zone_entry(
        accounts, "toi", "plan_phu", zone_id="plan_phu__toi-2"
    )] == ["all_plans"]


def test_rejects_missing_terminal_path() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "a",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "lot": {"mode": "fixed", "volume": 0.01},
                },
            ],
        )
        with pytest.raises(ValueError, match=r"terminal_path"):
            load_mt5_accounts_from_path(p)


def test_rejects_empty_terminal_path() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "a",
                    "terminal_path": "   ",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "lot": {"mode": "fixed", "volume": 0.01},
                },
            ],
        )
        with pytest.raises(ValueError, match=r"terminal_path"):
            load_mt5_accounts_from_path(p)


def test_rejects_non_string_terminal_path() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "a",
                    "terminal_path": 123,
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "lot": {"mode": "fixed", "volume": 0.01},
                },
            ],
        )
        with pytest.raises(ValueError, match=r"terminal_path"):
            load_mt5_accounts_from_path(p)


def test_sync_accounts_scalp_json_writes_subset_and_strips_flag() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "full_only",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "lot": {"mode": "fixed", "volume": 0.01},
                    "update-scalp": False,
                },
                {
                    "id": "scalp_acc",
                    "terminal_path": "C:/MT5/B/metatrader64.exe",
                    "login": 2,
                    "password": "y",
                    "server": "S",
                    "primary": False,
                    "lot": {"mode": "fixed", "volume": 0.02},
                    "update-scalp": True,
                },
            ],
        )
        out = sync_accounts_scalp_json(p)
        assert out is not None
        dest = Path(td) / "accounts-scalp.json"
        assert out == dest.resolve()
        rows = json.loads(dest.read_text(encoding="utf-8"))
        assert len(rows) == 1
        assert rows[0]["id"] == "scalp_acc"
        assert rows[0]["primary"] is True
        assert "update-scalp" not in rows[0]
        accs = load_mt5_accounts_from_path(dest)
        assert len(accs) == 1
        assert accs[0].id == "scalp_acc"
        assert accs[0].primary is True


def test_sync_accounts_scalp_json_keeps_sole_primary_in_subset() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "a",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "update-scalp": True,
                },
                {
                    "id": "b",
                    "terminal_path": "C:/MT5/B/metatrader64.exe",
                    "login": 2,
                    "password": "y",
                    "server": "S",
                    "primary": False,
                    "update-scalp": True,
                },
            ],
        )
        out = sync_accounts_scalp_json(p)
        assert out is not None
        dest = Path(td) / "accounts-scalp.json"
        rows = json.loads(dest.read_text(encoding="utf-8"))
        assert len(rows) == 2
        prim = [r for r in rows if r.get("primary") is True]
        assert len(prim) == 1
        assert prim[0]["id"] == "a"
        load_mt5_accounts_from_path(dest)


def test_sync_accounts_scalp_json_truthy_string_not_included() -> None:
    """Chỉ JSON true được coi là bật; chuỗi \"true\" bị bỏ qua."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "a",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "update-scalp": "true",
                },
            ],
        )
        assert sync_accounts_scalp_json(p) is None


def test_load_mt5_accounts_for_zone_entry_non_scalp_uses_cli(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "a",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "lot": {"mode": "fixed", "volume": 0.01},
                },
            ],
        )
        monkeypatch.delenv("MT5_ACCOUNTS_JSON", raising=False)
        accs = load_mt5_accounts_for_zone_entry(zone_source="all", cli_path=p)
        assert accs is not None and len(accs) == 1 and accs[0].id == "a"


def test_load_mt5_accounts_for_zone_entry_scalp_prefers_sibling_file_and_forces_001(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as td:
        full_p = Path(td) / "accounts.json"
        _write_accounts(
            full_p,
            [
                {
                    "id": "full_only",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "lot": {"mode": "fixed", "volume": 0.01},
                },
                {
                    "id": "scalp_acc",
                    "terminal_path": "C:/MT5/B/metatrader64.exe",
                    "login": 2,
                    "password": "y",
                    "server": "S",
                    "primary": False,
                    "lot": {"mode": "fixed", "volume": 0.02},
                    "update-scalp": True,
                },
            ],
        )
        sync_accounts_scalp_json(full_p)
        monkeypatch.delenv("MT5_ACCOUNTS_JSON", raising=False)
        accs = load_mt5_accounts_for_zone_entry(zone_source=SOURCE_UPDATE_SCALP, cli_path=full_p)
        assert accs is not None and len(accs) == 1 and accs[0].id == "scalp_acc"
        assert isinstance(accs[0].lot, LotRuleFixed)
        assert accs[0].lot.volume == 0.01


def test_load_mt5_accounts_for_zone_entry_scalp_forces_manual_sibling_lot_to_001(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as td:
        full_p = Path(td) / "accounts.json"
        scalp_p = Path(td) / "accounts-scalp.json"
        _write_accounts(
            full_p,
            [
                {
                    "id": "full_only",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                },
            ],
        )
        _write_accounts(
            scalp_p,
            [
                {
                    "id": "scalp_acc",
                    "terminal_path": "C:/MT5/B/metatrader64.exe",
                    "login": 2,
                    "password": "y",
                    "server": "S",
                    "primary": True,
                    "lot": {"mode": "max_loss_usd", "max_usd": 100},
                },
            ],
        )
        monkeypatch.delenv("MT5_ACCOUNTS_JSON", raising=False)

        accs = load_mt5_accounts_for_zone_entry(zone_source=SOURCE_UPDATE_SCALP, cli_path=full_p)

        assert accs is not None and len(accs) == 1
        assert isinstance(accs[0].lot, LotRuleFixed)
        assert accs[0].lot.volume == 0.01


def test_load_mt5_accounts_for_zone_entry_scalp_defaults_missing_lot_to_001(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        full_p = Path(td) / "accounts.json"
        _write_accounts(
            full_p,
            [
                {
                    "id": "scalp_acc",
                    "terminal_path": "C:/MT5/B/metatrader64.exe",
                    "login": 2,
                    "password": "y",
                    "server": "S",
                    "primary": True,
                    "update-scalp": True,
                },
            ],
        )
        sync_accounts_scalp_json(full_p)
        monkeypatch.delenv("MT5_ACCOUNTS_JSON", raising=False)

        accs = load_mt5_accounts_for_zone_entry(zone_source=SOURCE_UPDATE_SCALP, cli_path=full_p)

        assert accs is not None and len(accs) == 1
        assert isinstance(accs[0].lot, LotRuleFixed)
        assert accs[0].lot.volume == 0.01


def test_update_scalp_entry_trade_defaults_lot_to_001() -> None:
    trade = ParsedTrade(
        symbol="XAUUSD",
        side="BUY",
        kind="LIMIT",
        price=2600.0,
        sl=2590.0,
        tp1=2610.0,
        tp2=None,
        lot=0.04,
        raw_line="BUY LIMIT 2600.0 | SL 2590.0 | TP1 2610.0 | Lot 0.04",
    )

    out = trade_with_update_scalp_entry_lot_default(trade, zone_source=SOURCE_UPDATE_SCALP)

    assert out.lot == 0.01
    assert out.raw_line.endswith("Lot 0.01")


def test_load_mt5_accounts_for_zone_entry_scalp_missing_sibling_empty(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        full_p = Path(td) / "accounts.json"
        _write_accounts(
            full_p,
            [
                {
                    "id": "a",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "lot": {"mode": "fixed", "volume": 0.01},
                },
            ],
        )
        monkeypatch.delenv("MT5_ACCOUNTS_JSON", raising=False)
        accs = load_mt5_accounts_for_zone_entry(zone_source=SOURCE_UPDATE_SCALP, cli_path=full_p)
        assert accs == []


def test_sync_accounts_scalp_json_empty_removes_dest() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "a",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                },
            ],
        )
        dest = Path(td) / "accounts-scalp.json"
        dest.write_text("[]\n", encoding="utf-8")
        assert sync_accounts_scalp_json(p) is None
        assert not dest.is_file()


def test_sync_accounts_all2_json_writes_subset_and_strips_flag() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "full_only",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "lot": {"mode": "fixed", "volume": 0.01},
                    "all-2": False,
                },
                {
                    "id": "all2_acc",
                    "terminal_path": "C:/MT5/B/metatrader64.exe",
                    "login": 2,
                    "password": "y",
                    "server": "S",
                    "primary": False,
                    "lot": {"mode": "fixed", "volume": 0.02},
                    "all-2": True,
                },
            ],
        )
        out = sync_accounts_all2_json(p)
        assert out is not None
        dest = Path(td) / "accounts-all2.json"
        rows = json.loads(dest.read_text(encoding="utf-8"))
        assert len(rows) == 1
        assert rows[0]["id"] == "all2_acc"
        assert "all-2" not in rows[0]
        accs = load_mt5_accounts_for_zone_entry(zone_source=SOURCE_ALL_2, cli_path=p)
        assert accs is not None
        assert len(accs) == 1
        assert accs[0].id == "all2_acc"


def test_load_mt5_accounts_for_zone_entry_all_excludes_all2_flagged(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        full_p = Path(td) / "accounts.json"
        _write_accounts(
            full_p,
            [
                {
                    "id": "main",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "lot": {"mode": "fixed", "volume": 0.01},
                },
                {
                    "id": "flow2",
                    "terminal_path": "C:/MT5/B/metatrader64.exe",
                    "login": 2,
                    "password": "y",
                    "server": "S",
                    "primary": False,
                    "all-2": True,
                    "lot": {"mode": "fixed", "volume": 0.02},
                },
            ],
        )
        monkeypatch.delenv("MT5_ACCOUNTS_JSON", raising=False)
        accs_all = load_mt5_accounts_for_zone_entry(zone_source="all", cli_path=full_p)
        assert accs_all is not None
        assert [a.id for a in accs_all] == ["main"]
        accs_all2 = load_mt5_accounts_for_zone_entry(zone_source=SOURCE_ALL_2, cli_path=full_p)
        sync_accounts_all2_json(full_p)
        accs_all2 = load_mt5_accounts_for_zone_entry(zone_source=SOURCE_ALL_2, cli_path=full_p)
        assert accs_all2 is not None
        assert [a.id for a in accs_all2] == ["flow2"]


def test_exclude_all2_dedicated_accounts_rehomes_primary() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "only_all2",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "all-2": True,
                },
                {
                    "id": "main",
                    "terminal_path": "C:/MT5/B/metatrader64.exe",
                    "login": 2,
                    "password": "y",
                    "server": "S",
                    "primary": False,
                },
            ],
        )
        full = load_mt5_accounts_from_path(p)
        out = exclude_all2_dedicated_accounts(full, p)
        assert len(out) == 1
        assert out[0].id == "main"
        assert out[0].primary is True


def test_load_mt5_accounts_for_zone_entry_all2_missing_sibling_empty(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        full_p = Path(td) / "accounts.json"
        _write_accounts(
            full_p,
            [
                {
                    "id": "a",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "lot": {"mode": "fixed", "volume": 0.01},
                },
            ],
        )
        monkeypatch.delenv("MT5_ACCOUNTS_JSON", raising=False)
        accs = load_mt5_accounts_for_zone_entry(zone_source=SOURCE_ALL_2, cli_path=full_p)
        assert accs == []


def test_resolve_mt5_trade_symbol_uses_per_account_map() -> None:
    t = ParsedTrade(
        symbol="XAUUSD",
        side="BUY",
        kind="LIMIT",
        price=2600.0,
        sl=2590.0,
        tp1=2610.0,
        tp2=None,
        lot=0.01,
        raw_line="",
    )
    m_micro = {"XAUUSD": "XAUUSDm"}
    r1 = resolve_mt5_trade_symbol(t, None, account_symbol_map=m_micro)
    assert r1.symbol == "XAUUSDm"
    m_std = {"XAUUSD": "XAUUSD"}
    r2 = resolve_mt5_trade_symbol(t, None, account_symbol_map=m_std)
    assert r2.symbol == "XAUUSD"
    r3 = resolve_mt5_trade_symbol(t, None, account_symbol_map=None)
    assert r3.symbol == "XAUUSDm"


def test_resolve_trade_filter_key_mapping() -> None:
    assert resolve_trade_filter_key(zone_label="plan_chinh", zone_source="all") == "chinh"
    assert (
        resolve_trade_filter_key(
            zone_label="plan_chinh",
            zone_id="plan_chinh__sang-2",
            zone_source="all-2",
        )
        == "chinh-2"
    )
    assert resolve_trade_filter_key(zone_label="plan_phu", zone_id="plan_phu__toi") == "phu"
    assert (
        resolve_trade_filter_key(
            zone_label="plan_phu",
            zone_id="plan_phu__toi-2",
            zone_source="all-2",
        )
        == "phu-2"
    )
    assert resolve_trade_filter_key(zone_label="scalp_1", zone_source="all") == "scalp"
    assert (
        resolve_trade_filter_key(
            zone_label="scalp",
            zone_id="scalp__toi-2",
            zone_source="all-2",
        )
        == "scalp-2"
    )
    assert (
        resolve_trade_filter_key(zone_label="scalp_1", zone_source=SOURCE_UPDATE_SCALP)
        == "update-scalp"
    )


def test_is_plan_phu_and_scalp_family() -> None:
    assert is_plan_phu_family("plan_phu__sang")
    assert not is_plan_phu_family("plan_chinh")
    assert is_scalp_family("scalp_1")
    assert not is_scalp_family("plan_phu")


def test_load_trade_filter_from_accounts_json() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "mixed",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "trade": {
                        "chinh-2": True,
                        "phu-2": False,
                        "chinh": True,
                        "phu": True,
                        "update-scalp": True,
                        "scalp": False,
                        "scalp-2": True,
                    },
                },
            ],
        )
        accs = load_mt5_accounts_from_path(p)
        assert accs[0].trade == {
            "chinh-2": True,
            "phu-2": False,
            "chinh": True,
            "phu": True,
            "update-scalp": True,
            "scalp": False,
            "scalp-2": True,
        }


def test_filter_trade_map_per_zone_type() -> None:
    acc = MT5AccountEntry(
        id="mixed",
        terminal_path="/tmp/mt5.exe",
        login=1,
        password="p",
        server="srv",
        primary=True,
        lot=LotRuleFromTrade(),
        trade={
            "chinh-2": True,
            "phu-2": False,
            "chinh": True,
            "phu": True,
            "update-scalp": True,
            "scalp": False,
            "scalp-2": True,
        },
    )
    accounts = [acc]

    assert [a.id for a in filter_mt5_accounts_for_zone_entry(
        accounts, "sang", "plan_chinh", zone_source="all"
    )] == ["mixed"]
    assert [a.id for a in filter_mt5_accounts_for_zone_entry(
        accounts, "sang", "plan_chinh", zone_id="plan_chinh__sang-2", zone_source="all-2"
    )] == ["mixed"]
    assert [a.id for a in filter_mt5_accounts_for_zone_entry(
        accounts, "sang", "plan_phu", zone_source="all"
    )] == ["mixed"]
    assert [a.id for a in filter_mt5_accounts_for_zone_entry(
        accounts, "sang", "plan_phu", zone_id="plan_phu__sang-2", zone_source="all-2"
    )] == []
    assert [a.id for a in filter_mt5_accounts_for_zone_entry(
        accounts, "sang", "scalp_1", zone_source="all"
    )] == []
    assert [a.id for a in filter_mt5_accounts_for_zone_entry(
        accounts, "toi", "scalp", zone_id="scalp__toi-2", zone_source="all-2"
    )] == ["mixed"]
    assert [a.id for a in filter_mt5_accounts_for_zone_entry(
        accounts, "sang", "scalp_1", zone_source=SOURCE_UPDATE_SCALP
    )] == ["mixed"]


def test_sync_subset_from_trade_map() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "main",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "trade": {"chinh": True, "phu": True, "scalp": True},
                },
                {
                    "id": "flow2",
                    "terminal_path": "C:/MT5/B/metatrader64.exe",
                    "login": 2,
                    "password": "y",
                    "server": "S",
                    "primary": False,
                    "trade": {"chinh-2": True, "update-scalp": True},
                },
                {
                    "id": "only_l2",
                    "terminal_path": "C:/MT5/C/metatrader64.exe",
                    "login": 3,
                    "password": "z",
                    "server": "S",
                    "primary": False,
                    "trade": {"chinh-2": True, "phu-2": False},
                },
            ],
        )
        assert account_row_in_scalp_subset({"trade": {"update-scalp": True}})
        assert account_row_in_all2_subset({"trade": {"phu-2": True}})
        scalp_out = sync_accounts_scalp_json(p)
        assert scalp_out is not None
        scalp_rows = json.loads((Path(td) / "accounts-scalp.json").read_text(encoding="utf-8"))
        assert [r["id"] for r in scalp_rows] == ["flow2"]
        all2_out = sync_accounts_all2_json(p)
        assert all2_out is not None
        all2_rows = json.loads((Path(td) / "accounts-all2.json").read_text(encoding="utf-8"))
        assert {r["id"] for r in all2_rows} == {"flow2", "only_l2"}
        all_accs = exclude_all2_dedicated_accounts(load_mt5_accounts_from_path(p), p)
        assert {a.id for a in all_accs} == {"main", "flow2"}


def test_load_short_flags_defaults_false() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "a",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                },
            ],
        )
        acc = load_mt5_accounts_from_path(p)[0]
        assert acc.short_scalp is False
        assert acc.short_chinh is False
        assert acc.short_phu is False


def test_load_short_flags_true() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts.json"
        _write_accounts(
            p,
            [
                {
                    "id": "a",
                    "terminal_path": "C:/MT5/A/metatrader64.exe",
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "short-scalp": True,
                    "short-chinh": True,
                    "short-phu": True,
                },
            ],
        )
        acc = load_mt5_accounts_from_path(p)[0]
        assert acc.short_scalp is True
        assert acc.short_chinh is True
        assert acc.short_phu is True


def test_apply_account_short_tp_scalp_buy() -> None:
    trade = ParsedTrade(
        symbol="XAUUSD",
        side="BUY",
        kind="LIMIT",
        price=4742.0,
        sl=4735.0,
        tp1=4750.0,
        tp2=4760.0,
        lot=0.01,
        raw_line="BUY LIMIT 4742.0 | SL 4735.0 | TP1 4750.0 | TP2 4760.0 | Lot 0.01",
    )
    acc = MT5AccountEntry(
        id="a",
        terminal_path="/tmp/mt5.exe",
        login=1,
        password="p",
        server="srv",
        primary=True,
        lot=LotRuleFromTrade(),
        short_scalp=True,
    )
    out = apply_account_short_tp(trade, acc, "scalp_1")
    assert out.sl == pytest.approx(4739.0)
    assert out.tp1 == pytest.approx(4750.0)
    assert out.tp2 is None
    assert "SL 4739" in out.raw_line
    assert "TP1 4750" in out.raw_line
    assert "TP2" not in out.raw_line


def test_apply_account_short_tp_scalp_sell() -> None:
    trade = ParsedTrade(
        symbol="XAUUSD",
        side="SELL",
        kind="LIMIT",
        price=4738.0,
        sl=4745.0,
        tp1=4730.0,
        tp2=None,
        lot=0.01,
        raw_line="SELL LIMIT 4738.0 | SL 4745.0 | TP1 4730.0 | Lot 0.01",
    )
    acc = MT5AccountEntry(
        id="a",
        terminal_path="/tmp/mt5.exe",
        login=1,
        password="p",
        server="srv",
        primary=True,
        lot=LotRuleFromTrade(),
        short_scalp=True,
    )
    out = apply_account_short_tp(trade, acc, "scalp")
    assert out.sl == pytest.approx(4741.0)
    assert out.tp1 == pytest.approx(4730.0)


def test_apply_account_short_tp_scalp_keeps_tp_when_min_distance_met() -> None:
    trade = ParsedTrade(
        symbol="XAUUSD",
        side="BUY",
        kind="LIMIT",
        price=4742.0,
        sl=4735.0,
        tp1=4755.0,
        tp2=4765.0,
        lot=0.01,
        raw_line="BUY LIMIT 4742.0 | SL 4735.0 | TP1 4755.0 | TP2 4765.0 | Lot 0.01",
    )
    acc = MT5AccountEntry(
        id="a",
        terminal_path="/tmp/mt5.exe",
        login=1,
        password="p",
        server="srv",
        primary=True,
        lot=LotRuleFromTrade(),
        short_scalp=True,
    )
    out = apply_account_short_tp(trade, acc, "scalp_1")
    assert out.tp1 == pytest.approx(4753.0)


def test_apply_account_short_tp_chinh_buy() -> None:
    trade = ParsedTrade(
        symbol="XAUUSD",
        side="BUY",
        kind="LIMIT",
        price=2600.0,
        sl=2590.0,
        tp1=2620.0,
        tp2=2640.0,
        lot=0.02,
        raw_line="BUY LIMIT 2600.0 | SL 2590.0 | TP1 2620.0 | TP2 2640.0 | Lot 0.02",
    )
    acc = MT5AccountEntry(
        id="a",
        terminal_path="/tmp/mt5.exe",
        login=1,
        password="p",
        server="srv",
        primary=True,
        lot=LotRuleFromTrade(),
        short_chinh=True,
    )
    out = apply_account_short_tp(trade, acc, "plan_chinh")
    assert out.sl == pytest.approx(2597.0)
    assert out.tp1 == pytest.approx(2618.0)
    assert out.tp2 is None


def test_apply_account_short_tp_phu_sell() -> None:
    trade = ParsedTrade(
        symbol="XAUUSD",
        side="SELL",
        kind="LIMIT",
        price=2650.0,
        sl=2660.0,
        tp1=2630.0,
        tp2=2610.0,
        lot=0.02,
        raw_line="SELL LIMIT 2650.0 | SL 2660.0 | TP1 2630.0 | TP2 2610.0 | Lot 0.02",
    )
    acc = MT5AccountEntry(
        id="a",
        terminal_path="/tmp/mt5.exe",
        login=1,
        password="p",
        server="srv",
        primary=True,
        lot=LotRuleFromTrade(),
        short_phu=True,
    )
    out = apply_account_short_tp(trade, acc, "plan_phu__sang")
    assert out.sl == pytest.approx(2653.0)
    assert out.tp1 == pytest.approx(2632.0)
    assert out.tp2 is None


def test_apply_account_short_tp_skips_non_matching_label() -> None:
    trade = ParsedTrade(
        symbol="XAUUSD",
        side="BUY",
        kind="LIMIT",
        price=2600.0,
        sl=2590.0,
        tp1=2610.0,
        tp2=2620.0,
        lot=0.02,
        raw_line="BUY LIMIT 2600.0 | SL 2590.0 | TP1 2610.0 | TP2 2620.0 | Lot 0.02",
    )
    acc = MT5AccountEntry(
        id="a",
        terminal_path="/tmp/mt5.exe",
        login=1,
        password="p",
        server="srv",
        primary=True,
        lot=LotRuleFromTrade(),
        short_scalp=True,
    )
    assert apply_account_short_tp(trade, acc, "plan_chinh") is trade
