"""Orchestrator tests (mocked execute_trade, no MetaTrader5)."""

from __future__ import annotations

import json
import threading
import time

import pytest

from automation_tool.mt5_accounts import MT5AccountEntry, LotRuleFixed, LotRuleFromTrade
from automation_tool.mt5_execute import MT5ExecutionResult
from automation_tool.mt5_manage import MT5ManageResult
from automation_tool.mt5_multi import (
    execute_trade_all_accounts,
    mt5_cancel_pending_or_close_all_accounts,
    mt5_partial_close_tp1_all_accounts,
)
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


def test_execute_trade_all_accounts_fixed_lot_uses_zone_label_volume(
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
            lot=LotRuleFixed(
                volume={
                    "plan_chinh": 0.02,
                    "plan_phu": 0.01,
                    "default": 0.01,
                }
            ),
        ),
    ]
    seen: list[float | None] = []

    def fake_execute_trade(trade, **kwargs):
        seen.append(kwargs.get("lot_override"))
        return MT5ExecutionResult(
            ok=True,
            message="mock acc_a",
            order=5001,
            account_id=kwargs.get("account_id"),
        )

    monkeypatch.setattr("automation_tool.mt5_multi.execute_trade", fake_execute_trade)

    summ = execute_trade_all_accounts(
        sample_trade,
        accounts,
        dry_run=True,
        zone_label="plan_chinh",
    )
    assert summ.ok_all
    assert seen == [0.02]


def test_execute_trade_all_accounts_uses_account_entry_take_profit(
    sample_trade, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts = [
        MT5AccountEntry(
            id="acc_tp1",
            terminal_path="/tmp/mt5-acc-a/terminal64.exe",
            login=1,
            password="p",
            server="srv",
            primary=True,
            lot=LotRuleFromTrade(),
            entry_take_profit="tp1",
        ),
        MT5AccountEntry(
            id="acc_tp2",
            terminal_path="/tmp/mt5-acc-b/terminal64.exe",
            login=2,
            password="p",
            server="srv",
            primary=False,
            lot=LotRuleFromTrade(),
            entry_take_profit="tp2",
        ),
    ]
    seen: list[tuple[str | None, str | None]] = []

    def fake_execute_trade(trade, **kwargs):
        aid = kwargs.get("account_id")
        seen.append((aid, kwargs.get("take_profit_target")))
        oid = 4000 + len(seen)
        return MT5ExecutionResult(
            ok=True,
            message=f"mock {aid}",
            order=oid,
            account_id=aid,
        )

    monkeypatch.setattr("automation_tool.mt5_multi.execute_trade", fake_execute_trade)

    summ = execute_trade_all_accounts(sample_trade, accounts, dry_run=True)
    assert summ.ok_all
    assert seen == [("acc_tp1", "tp1"), ("acc_tp2", "tp2")]


def test_cancel_close_all_accounts_retries_failed_position_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    ]
    calls: list[int] = []

    def fake_cancel_close(ticket, **kwargs):
        calls.append(int(ticket))
        if len(calls) < 3:
            return MT5ManageResult(
                ok=False,
                message=f"Đóng position thất bại lần {len(calls)}",
                kind="position",
            )
        return MT5ManageResult(ok=True, message="Đã đóng position", kind="position")

    monkeypatch.setattr(
        "automation_tool.mt5_multi.mt5_cancel_pending_or_close_position",
        fake_cancel_close,
    )

    summary = mt5_cancel_pending_or_close_all_accounts(
        {"acc_a": 101},
        accounts,
        dry_run=False,
    )

    assert summary.ok_all
    assert calls == [101, 101, 101]
    assert summary.results[0][1].message == "Đã đóng position"


def test_cancel_close_all_accounts_does_not_retry_pending_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    ]
    calls: list[int] = []

    def fake_cancel_close(ticket, **kwargs):
        calls.append(int(ticket))
        return MT5ManageResult(
            ok=False,
            message="Huỷ pending thất bại",
            kind="pending",
        )

    monkeypatch.setattr(
        "automation_tool.mt5_multi.mt5_cancel_pending_or_close_position",
        fake_cancel_close,
    )

    summary = mt5_cancel_pending_or_close_all_accounts(
        {"acc_a": 101},
        accounts,
        dry_run=False,
    )

    assert not summary.ok_all
    assert calls == [101]
    assert summary.results[0][1].message == "Huỷ pending thất bại"


def test_partial_close_tp1_all_accounts_uses_position_volume(
    sample_trade, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Partial close không truyền expected_initial_volume — luôn dùng volume thực từ position MT5."""
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
    seen: list[tuple[int, int, float | None]] = []

    def fake_partial(ticket, **kwargs):
        seen.append(
            (
                kwargs["login"],
                ticket,
                kwargs.get("expected_initial_volume"),
            )
        )
        return MT5ManageResult(ok=True, message=f"partial {ticket}", kind="position")

    monkeypatch.setattr("automation_tool.mt5_multi.mt5_close_position_partial", fake_partial)
    monkeypatch.setattr(
        "automation_tool.mt5_multi.mt5_ticket_is_open_position",
        lambda ticket, **kwargs: (True, f"ticket={ticket} position mở"),
    )

    summary = mt5_partial_close_tp1_all_accounts(
        {"acc_a": 101, "acc_b": 202},
        accounts,
        sample_trade,
        dry_run=False,
    )

    assert summary.ok_all
    # expected_initial_volume phải là None — để mt5_close_position_partial tự đọc volume từ MT5
    assert seen == [(1, 101, None), (2, 202, None)]


def test_partial_close_tp1_all_accounts_accepts_missing_tp1_entry_position(
    sample_trade, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts = [
        MT5AccountEntry(
            id="acc_tp2",
            terminal_path="/tmp/mt5-acc-a/terminal64.exe",
            login=1,
            password="p",
            server="srv",
            primary=True,
            lot=LotRuleFromTrade(),
            entry_take_profit="tp2",
        ),
        MT5AccountEntry(
            id="acc_tp1",
            terminal_path="/tmp/mt5-acc-b/terminal64.exe",
            login=2,
            password="p",
            server="srv",
            primary=False,
            lot=LotRuleFromTrade(),
            entry_take_profit="tp1",
        ),
    ]

    def fake_partial(ticket, **kwargs):
        if ticket == 202:
            return MT5ManageResult(
                ok=False,
                message="Không tìm thấy position ticket=202",
                kind="none",
            )
        return MT5ManageResult(ok=True, message=f"partial {ticket}", kind="position")

    monkeypatch.setattr("automation_tool.mt5_multi.mt5_close_position_partial", fake_partial)
    monkeypatch.setattr(
        "automation_tool.mt5_multi.mt5_ticket_is_open_position",
        lambda ticket, **kwargs: (True, f"ticket={ticket} position mở"),
    )

    summary = mt5_partial_close_tp1_all_accounts(
        {"acc_tp2": 101, "acc_tp1": 202},
        accounts,
        sample_trade,
        dry_run=False,
    )

    assert summary.ok_all
    assert [r.ok for _, r in summary.results] == [True, True]
    assert "đã đóng tại TP1" in summary.results[1][1].message


def test_partial_close_tp1_skips_when_ticket_not_yet_position(
    sample_trade, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chỉ gọi chốt một phần khi ticket đã là position (đã khớp)."""
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
    ]
    calls: list[int] = []

    def fake_partial(ticket, **kwargs):
        calls.append(int(ticket))
        return MT5ManageResult(ok=True, message="partial", kind="position")

    monkeypatch.setattr("automation_tool.mt5_multi.mt5_close_position_partial", fake_partial)
    monkeypatch.setattr(
        "automation_tool.mt5_multi.mt5_ticket_is_open_position",
        lambda ticket, **kwargs: (
            False,
            "ticket=101 vẫn pending (chưa position)",
        ),
    )

    summary = mt5_partial_close_tp1_all_accounts(
        {"acc_a": 101},
        accounts,
        sample_trade,
        dry_run=False,
    )
    assert summary.ok_all
    assert calls == []
    assert "primary chưa có position" in summary.results[0][1].message
