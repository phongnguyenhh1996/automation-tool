"""Huỷ lệnh chờ / đóng position MetaTrader5 theo ticket (sau review TP1)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal, Optional

import automation_tool.config  # noqa: F401 — load .env

from automation_tool.mt5_execute import _filling_for_symbol, _load_mt5, format_last_error


@dataclass
class MT5ManageResult:
    ok: bool
    message: str
    kind: Optional[Literal["pending", "position", "none"]] = None


def _mt5_init() -> Any:
    mt5 = _load_mt5()
    kwargs: dict[str, Any] = {}
    login_i = int(os.getenv("MT5_LOGIN", "0") or "0")
    password_s = os.getenv("MT5_PASSWORD") or ""
    server_s = os.getenv("MT5_SERVER") or ""
    if login_i and password_s and server_s:
        kwargs["login"] = login_i
        kwargs["password"] = password_s
        kwargs["server"] = server_s
    if not mt5.initialize(**kwargs):
        return None
    return mt5


def _is_done(mt5: Any, ret: Any) -> bool:
    rc = getattr(ret, "retcode", None)
    if rc is None:
        return False
    try:
        return int(rc) == int(mt5.TRADE_RETCODE_DONE)
    except (TypeError, ValueError):
        return False


def mt5_cancel_pending_order(ticket: int, *, dry_run: bool = False) -> MT5ManageResult:
    """``TRADE_ACTION_REMOVE`` cho order ticket (lệnh chờ)."""
    if dry_run:
        return MT5ManageResult(
            ok=True,
            message=f"[DRY-RUN] Sẽ huỷ pending order ticket={ticket}",
            kind="pending",
        )
    mt5 = _mt5_init()
    if mt5 is None:
        return MT5ManageResult(ok=False, message="mt5.initialize thất bại", kind=None)
    try:
        req = {"action": mt5.TRADE_ACTION_REMOVE, "order": int(ticket)}
        r = mt5.order_send(req)
        if r is None:
            return MT5ManageResult(
                ok=False,
                message=f"order_send REMOVE trả None. {format_last_error(mt5)}",
                kind="pending",
            )
        if not _is_done(mt5, r):
            return MT5ManageResult(
                ok=False,
                message=f"Huỷ pending thất bại: retcode={getattr(r, 'retcode', None)}",
                kind="pending",
            )
        return MT5ManageResult(
            ok=True,
            message=f"Đã huỷ pending order ticket={ticket}",
            kind="pending",
        )
    finally:
        mt5.shutdown()


def mt5_close_position(ticket: int, *, dry_run: bool = False) -> MT5ManageResult:
    """Đóng toàn bộ volume của position ``ticket`` (market close)."""
    if dry_run:
        return MT5ManageResult(
            ok=True,
            message=f"[DRY-RUN] Sẽ đóng position ticket={ticket}",
            kind="position",
        )
    mt5 = _mt5_init()
    if mt5 is None:
        return MT5ManageResult(ok=False, message="mt5.initialize thất bại", kind=None)
    try:
        pos = None
        for p in mt5.positions_get() or []:
            if int(p.ticket) == int(ticket):
                pos = p
                break
        if pos is None:
            return MT5ManageResult(
                ok=False,
                message=f"Không tìm thấy position ticket={ticket}",
                kind="none",
            )
        sym = pos.symbol
        vol = float(pos.volume)
        filling = _filling_for_symbol(mt5, sym)
        tick = mt5.symbol_info_tick(sym)
        if tick is None:
            return MT5ManageResult(ok=False, message=f"symbol_info_tick({sym!r}) None", kind="position")

        if int(pos.type) == int(mt5.POSITION_TYPE_BUY):
            otype = mt5.ORDER_TYPE_SELL
            price = float(tick.bid)
        else:
            otype = mt5.ORDER_TYPE_BUY
            price = float(tick.ask)

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym,
            "volume": vol,
            "type": otype,
            "position": int(ticket),
            "price": price,
            "deviation": 20,
            "magic": int(getattr(pos, "magic", 2222222)),
            "comment": "tp1-review-close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        r = mt5.order_send(req)
        if r is None:
            return MT5ManageResult(
                ok=False,
                message=f"order_send CLOSE trả None. {format_last_error(mt5)}",
                kind="position",
            )
        if not _is_done(mt5, r):
            return MT5ManageResult(
                ok=False,
                message=f"Đóng position thất bại: retcode={getattr(r, 'retcode', None)}",
                kind="position",
            )
        return MT5ManageResult(
            ok=True,
            message=f"Đã đóng position ticket={ticket} symbol={sym}",
            kind="position",
        )
    finally:
        mt5.shutdown()


def mt5_latest_position_ticket(
    symbol: str,
    *,
    magic: Optional[int] = None,
) -> Optional[int]:
    """Ticket position mới nhất cho ``symbol`` (lọc magic nếu có)."""
    mt5 = _mt5_init()
    if mt5 is None:
        return None
    try:
        sym = symbol
        pos_list = mt5.positions_get(symbol=sym) or []
        best: Optional[tuple[int, int]] = None  # (time, ticket)
        mg = magic if magic is not None else int(os.getenv("MT5_MAGIC", "2222222") or "2222222")
        for p in pos_list:
            if int(getattr(p, "magic", 0)) != int(mg):
                continue
            t = int(p.ticket)
            tm = int(getattr(p, "time", 0) or getattr(p, "time_msc", 0) or 0)
            if best is None or tm >= best[0]:
                best = (tm, t)
        return best[1] if best else None
    finally:
        mt5.shutdown()


def mt5_cancel_pending_or_close_position(ticket: int, *, dry_run: bool = False) -> MT5ManageResult:
    """Thử tìm pending ``ticket``; không có thì đóng position ``ticket``."""
    if dry_run:
        return MT5ManageResult(ok=True, message="[DRY-RUN] cancel/close", kind="none")
    mt5 = _mt5_init()
    if mt5 is None:
        return MT5ManageResult(ok=False, message="mt5.initialize thất bại", kind=None)
    try:
        has_order = any(int(o.ticket) == int(ticket) for o in (mt5.orders_get() or []))
        has_pos = any(int(p.ticket) == int(ticket) for p in (mt5.positions_get() or []))
    finally:
        mt5.shutdown()
    if has_order:
        return mt5_cancel_pending_order(ticket, dry_run=False)
    if has_pos:
        return mt5_close_position(ticket, dry_run=False)
    return MT5ManageResult(
        ok=False,
        message=f"Không có order/pending/position ticket={ticket}",
        kind="none",
    )
