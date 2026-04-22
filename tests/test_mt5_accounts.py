"""Tests for accounts.json loader (no MetaTrader5 runtime)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from automation_tool.mt5_accounts import (
    LotRuleFixed,
    LotRuleFromTrade,
    LotRuleMaxNotionalUsd,
    load_mt5_accounts_from_path,
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
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                },
                {
                    "id": "b",
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
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "lot": {"mode": "fixed", "volume": 0.02},
                },
                {
                    "id": "b",
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


def test_rejects_zero_primary() -> None:
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
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "lot": {"mode": "fixed", "volume": 0.01},
                },
                {
                    "id": "a",
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
                    "login": 1,
                    "password": "x",
                    "server": "S",
                    "primary": True,
                    "lot": {"mode": "fixed", "volume": 0.01},
                    "symbol_map": {"XAUUSD": "XAUUSDm", "EURUSD": "EURUSD"},
                },
                {
                    "id": "std",
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
