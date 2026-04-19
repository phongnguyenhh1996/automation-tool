"""
Orchestration: nhiều tài khoản MT5 tuần tự (``accounts.json``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from automation_tool.mt5_accounts import (
    MT5AccountEntry,
    LotRuleFixed,
    LotRuleMaxNotionalUsd,
    compute_lot_override,
    compute_volume_for_max_notional_live,
    primary_account,
)
from automation_tool.mt5_execute import MT5ExecutionResult, execute_trade, format_mt5_execution_for_telegram
from automation_tool.mt5_manage import (
    MT5ChinhTradeLineResult,
    MT5ManageResult,
    mt5_cancel_pending_or_close_position,
    mt5_chinh_trade_line_inplace,
)
from automation_tool.mt5_openai_parse import ParsedTrade


@dataclass
class MT5MultiExecutionSummary:
    """Kết quả gửi lệnh qua tất cả tài khoản trong cấu hình."""

    results: list[MT5ExecutionResult] = field(default_factory=list)
    tickets_by_account_id: dict[str, int] = field(default_factory=dict)
    ok_all: bool = True

    def primary_ticket(self, accounts: list[MT5AccountEntry]) -> int:
        pid = primary_account(accounts).id
        return int(self.tickets_by_account_id.get(pid) or 0)


def _lot_override_for_entry(
    trade: ParsedTrade,
    acc: MT5AccountEntry,
    *,
    dry_run: bool,
    symbol_override: Optional[str],
) -> tuple[Optional[float], Optional[str]]:
    """Trả về (lot_override hoặc None để dùng trade.lot, ghi chú debug)."""
    rule = acc.lot
    if isinstance(rule, LotRuleFixed):
        return float(rule.volume), None
    if isinstance(rule, LotRuleMaxNotionalUsd):
        if dry_run:
            vol, hint = compute_lot_override(
                trade,
                rule,
                mt5=None,  # type: ignore[arg-type]
                resolved_symbol=trade.symbol,
                dry_run=True,
            )
            return vol, hint
        vol, hint = compute_volume_for_max_notional_live(
            trade,
            rule,
            login=acc.login,
            password=acc.password,
            server=acc.server,
            symbol_override=symbol_override,
        )
        return vol, hint
    return None, f"lot rule không hỗ trợ: {rule!r}"


def execute_trade_all_accounts(
    trade: ParsedTrade,
    accounts: list[MT5AccountEntry],
    *,
    dry_run: bool = True,
    symbol_override: Optional[str] = None,
    deviation: int = 20,
    magic: Optional[int] = None,
    log_tp2: bool = True,
) -> MT5MultiExecutionSummary:
    """
    Lần lượt đăng nhập từng tài khoản và gửi lệnh (``execute_trade`` đã shutdown sau mỗi lần).
    """
    out = MT5MultiExecutionSummary()
    for acc in accounts:
        lot_ov, _hint = _lot_override_for_entry(
            trade, acc, dry_run=dry_run, symbol_override=symbol_override
        )
        ex = execute_trade(
            trade,
            login=acc.login,
            password=acc.password,
            server=acc.server,
            dry_run=dry_run,
            deviation=deviation,
            magic=magic,
            log_tp2=log_tp2,
            symbol_override=symbol_override,
            lot_override=lot_ov,
            account_id=acc.id,
        )
        out.results.append(ex)
        if not ex.ok:
            out.ok_all = False
        tid = int(ex.order) if ex.order else 0
        if tid > 0:
            out.tickets_by_account_id[acc.id] = tid
    return out


def format_mt5_multi_for_telegram(summary: MT5MultiExecutionSummary) -> str:
    lines: list[str] = []
    for ex in summary.results:
        lines.append(format_mt5_execution_for_telegram(ex))
        lines.append("---")
    if lines and lines[-1] == "---":
        lines.pop()
    return "\n".join(lines)


@dataclass
class MT5MultiManageSummary:
    results: list[tuple[str, MT5ManageResult]] = field(default_factory=list)
    ok_all: bool = True


def mt5_cancel_pending_or_close_all_accounts(
    ticket_by_account_id: dict[str, int],
    accounts: list[MT5AccountEntry],
    *,
    dry_run: bool = False,
) -> MT5MultiManageSummary:
    """Hủy/đóng ticket trên từng acc (theo map đã lưu)."""
    summary = MT5MultiManageSummary()
    by_id = {a.id: a for a in accounts}
    for acc_id, ticket in ticket_by_account_id.items():
        acc = by_id.get(acc_id)
        if acc is None:
            summary.results.append(
                (
                    acc_id,
                    MT5ManageResult(
                        ok=False,
                        message=f"Không tìm thấy account id={acc_id!r} trong accounts.json",
                        kind=None,
                    ),
                )
            )
            summary.ok_all = False
            continue
        if int(ticket) <= 0:
            continue
        r = mt5_cancel_pending_or_close_position(
            int(ticket),
            dry_run=dry_run,
            login=acc.login,
            password=acc.password,
            server=acc.server,
        )
        summary.results.append((acc_id, r))
        if not r.ok:
            summary.ok_all = False
    return summary


def format_mt5_multi_manage_for_telegram(summary: MT5MultiManageSummary) -> str:
    parts: list[str] = []
    for acc_id, r in summary.results:
        parts.append(f"[{acc_id}] {r.message}")
    return "\n".join(parts)


@dataclass
class MT5MultiChinhSummary:
    """Kết quả chỉnh trade line tại chỗ trên từng tài khoản (SLTP / modify pending)."""

    results: list[tuple[str, MT5ChinhTradeLineResult]] = field(default_factory=list)
    ok_all_inplace: bool = True

    def all_ticket_missing(self) -> bool:
        if not self.results:
            return False
        return all(r.outcome == "ticket_missing" for _, r in self.results)


def mt5_chinh_trade_line_all_accounts(
    ticket_by_account_id: dict[str, int],
    accounts: list[MT5AccountEntry],
    new_parsed: ParsedTrade,
    *,
    dry_run: bool = False,
    symbol_override: Optional[str] = None,
) -> MT5MultiChinhSummary:
    """
    Mỗi ticket theo ``ticket_by_account_id`` — position → SLTP; pending → MODIFY.
    ``ok_all_inplace`` = mọi tài khoản đều thành công (hoặc dry_run) tại chỗ.
    """
    summary = MT5MultiChinhSummary()
    by_id = {a.id: a for a in accounts}
    for acc_id, ticket in ticket_by_account_id.items():
        acc = by_id.get(acc_id)
        if acc is None:
            summary.results.append(
                (
                    acc_id,
                    MT5ChinhTradeLineResult(
                        ok=False,
                        message=f"Không tìm thấy account id={acc_id!r} trong accounts.json",
                        outcome="modify_failed",
                    ),
                )
            )
            summary.ok_all_inplace = False
            continue
        if int(ticket) <= 0:
            summary.results.append(
                (
                    acc_id,
                    MT5ChinhTradeLineResult(
                        ok=False,
                        message=f"bỏ qua ticket không hợp lệ: {ticket}",
                        outcome="ticket_missing",
                    ),
                )
            )
            summary.ok_all_inplace = False
            continue
        r = mt5_chinh_trade_line_inplace(
            int(ticket),
            new_parsed,
            dry_run=dry_run,
            symbol_override=symbol_override,
            login=acc.login,
            password=acc.password,
            server=acc.server,
        )
        summary.results.append((acc_id, r))
        if not (
            r.ok
            and r.outcome
            in ("modified_sltp", "modified_pending", "dry_run")
        ):
            summary.ok_all_inplace = False
    return summary


def format_mt5_multi_chinh_for_telegram(summary: MT5MultiChinhSummary) -> str:
    parts: list[str] = []
    for acc_id, r in summary.results:
        parts.append(f"[{acc_id}] {r.message} ({r.outcome})")
    return "\n".join(parts)
