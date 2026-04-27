"""Orchestrator tests (mocked execute_trade, no MetaTrader5)."""

from __future__ import annotations

import json
import threading
import time

import pytest

from automation_tool.mt5_accounts import MT5AccountEntry, LotRuleFixed, LotRuleFromTrade
from automation_tool.mt5_execute import MT5ExecutionResult
from automation_tool.mt5_multi import execute_trade_all_accounts
from automation_tool.mt5_openai_parse import parse_openai_output_md


@pytest.fixture
def sample_trade():
    minimal = json.dumps(
        {
            "intraday_hanh_dong": "VÀO LỆNH",
            "trade_line": (
                "BUY LIMIT 2600.0 | SL 2590.0 | TP1 2610.0 | TP2 2620.0 | Lot 0.02"
            ),
        },
        ensure_ascii=False,
    )
    parsed, err = parse_openai_output_md(minimal, default_symbol="XAUUSD")
    assert err is None and parsed is not None
    return parsed


def test_execute_trade_all_accounts_one_call_per_account(
    sample_trade, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts = [
        MT5AccountEntry(
            id="acc_a",
            terminal_path="/tmp/mt5-acc-a/terminal64.exe",
            login=1,
            password="p",
            server="srv",
            primary=True,
            lot=LotRuleFixed(volume=0.03),
        ),
        MT5AccountEntry(
            id="acc_b",
            terminal_path="/tmp/mt5-acc-b/terminal64.exe",
            login=2,
            password="p",
            server="srv",
            primary=False,
            lot=LotRuleFixed(volume=0.04),
        ),
    ]
    seen: list[tuple[str | None, float | None]] = []

    def fake_execute_trade(trade, **kwargs):
        aid = kwargs.get("account_id")
        lot_ov = kwargs.get("lot_override")
        seen.append((aid, lot_ov))
        oid = 1000 + len(seen)
        return MT5ExecutionResult(
            ok=True,
            message=f"mock {aid}",
            order=oid,
            account_id=aid,
        )

    monkeypatch.setattr("automation_tool.mt5_multi.execute_trade", fake_execute_trade)

    summ = execute_trade_all_accounts(sample_trade, accounts, dry_run=True)
    assert summ.ok_all
    assert len(summ.results) == 2
    assert seen == [("acc_a", 0.03), ("acc_b", 0.04)]
    assert summ.tickets_by_account_id["acc_a"] == 1001
    assert summ.tickets_by_account_id["acc_b"] == 1002
    assert summ.primary_ticket(accounts) == 1001


def test_execute_trade_all_accounts_runs_accounts_sequentially(
    sample_trade, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts = [
        MT5AccountEntry(
            id="acc_a",
            terminal_path="/tmp/mt5-acc-a/terminal64.exe",
            login=1,
            password="p",
            server="srv",
            primary=True,
            lot=LotRuleFixed(volume=0.03),
        ),
        MT5AccountEntry(
            id="acc_b",
            terminal_path="/tmp/mt5-acc-b/terminal64.exe",
            login=2,
            password="p",
            server="srv",
            primary=False,
            lot=LotRuleFixed(volume=0.04),
        ),
    ]
    lock = threading.Lock()
    active_calls = 0
    overlap_seen = False
    seen: list[str | None] = []

    def fake_execute_trade(trade, **kwargs):
        nonlocal active_calls, overlap_seen
        aid = kwargs.get("account_id")
        with lock:
            active_calls += 1
            overlap_seen = overlap_seen or active_calls > 1
            seen.append(aid)
        time.sleep(0.05)
        with lock:
            active_calls -= 1
        return MT5ExecutionResult(
            ok=True,
            message=f"mock {aid}",
            order=3000 + len(seen),
            account_id=aid,
        )

    monkeypatch.setattr("automation_tool.mt5_multi.execute_trade", fake_execute_trade)

    summ = execute_trade_all_accounts(sample_trade, accounts, dry_run=True)
    assert summ.ok_all
    assert seen == ["acc_a", "acc_b"]
    assert not overlap_seen


def test_execute_trade_all_accounts_from_trade_uses_trade_lot(
    sample_trade, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sample_trade.lot == 0.02
    accounts = [
        MT5AccountEntry(
            id="acc_a",
            terminal_path="/tmp/mt5-acc-a/terminal64.exe",
            login=1,
            password="p",
            server="srv",
            primary=True,
            lot=LotRuleFromTrade(),
        ),
        MT5AccountEntry(
            id="acc_b",
            terminal_path="/tmp/mt5-acc-b/terminal64.exe",
            login=2,
            password="p",
            server="srv",
            primary=False,
            lot=LotRuleFixed(volume=0.05),
        ),
    ]
    seen: list[tuple[str | None, float | None]] = []

    def fake_execute_trade(trade, **kwargs):
        aid = kwargs.get("account_id")
        lot_ov = kwargs.get("lot_override")
        seen.append((aid, lot_ov))
        assert trade.lot == 0.02
        oid = 2000 + len(seen)
        return MT5ExecutionResult(
            ok=True,
            message=f"mock {aid}",
            order=oid,
            account_id=aid,
        )

    monkeypatch.setattr("automation_tool.mt5_multi.execute_trade", fake_execute_trade)

    summ = execute_trade_all_accounts(sample_trade, accounts, dry_run=True)
    assert summ.ok_all
    assert seen == [("acc_a", None), ("acc_b", 0.05)]
