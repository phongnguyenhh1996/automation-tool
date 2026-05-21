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
    SOURCE_UPDATE_SCALP,
    account_tp_r_multiplier,
    filter_mt5_accounts_for_entry_slot,
    load_mt5_accounts_from_path,
    load_mt5_accounts_for_zone_entry,
    resolve_account_entry_tp_price,
    trade_with_update_scalp_entry_lot_default,
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
