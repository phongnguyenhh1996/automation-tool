"""Tests for accounts.json loader (no MetaTrader5 runtime)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from automation_tool.mt5_accounts import (
    LotRuleFixed,
    LotRuleMaxNotionalUsd,
    load_mt5_accounts_from_path,
)


def _write_accounts(path: Path, data: list | dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


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
