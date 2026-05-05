"""Huỷ lệnh chờ / đóng position MetaTrader5 theo ticket (sau review TP1)."""

from __future__ import annotations

import os
import math
from dataclasses import dataclass
from typing import Any, Literal, Optional

import automation_tool.config  # noqa: F401 — load .env

from automation_tool.mt5_execute import (
    ensure_mt5_session,
    _ensure_symbol,
    _filling_for_symbol,
    _is_retryable_order_send_retcode,
    _load_mt5,
    _order_type_for_pending,
    _refresh_market_order_price,
    format_last_error,
    resolve_mt5_trade_symbol,
    symbol_uses_market_execution,
)
from automation_tool.mt5_openai_parse import ParsedTrade


@dataclass
class MT5ManageResult:
    ok: bool
    message: str
    kind: Optional[Literal["pending", "position", "none"]] = None


def _mt5_init(
    terminal_path: Optional[str] = None,
    login: Optional[int] = None,
    password: Optional[str] = None,
    server: Optional[str] = None,
) -> Any:
    """
    ``initialize()`` — ưu tiên ``login``/``password``/``server`` nếu đủ ba giá trị;
    không thì đọc ``MT5_*`` từ env (hành vi cũ).
    """
    term_path = (terminal_path or "").strip()
    if not term_path:
        return None
    if login is not None and password and server:
        login_i: Optional[int] = int(login)
        password_s = password
        server_s = server
    else:
        login_i = int(os.getenv("MT5_LOGIN", "0") or "0")
        password_s = os.getenv("MT5_PASSWORD") or ""
        server_s = os.getenv("MT5_SERVER") or ""
    session = ensure_mt5_session(
        terminal_path=term_path,
        login=login_i if login_i else None,
        password=password_s,
        server=server_s,
    )
    if not session.ok:
        return None
    return session.mt5


def _mt5_init_current_terminal() -> Any:
    """
    ``initialize()`` không đối số — bám phiên MetaTrader đang mở (đã login sẵn trong terminal).
    Dùng chung với daemon đọc giá; **không** gọi ``shutdown`` ở caller nếu muốn giữ phiên.
    """
    mt5 = _load_mt5()
    try:
        if mt5.terminal_info() is not None and mt5.account_info() is not None:
            return mt5
    except Exception:
        pass
    if not mt5.initialize():
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


def _send_market_close_with_retry(
    mt5: Any,
    request: dict[str, Any],
    *,
    attempts: int = 3,
) -> Any:
    ret = None
    max_attempts = max(1, int(attempts))
    for attempt in range(max_attempts):
        ret = mt5.order_send(request)
        if ret is None or _is_done(mt5, ret):
            return ret
        rc = getattr(ret, "retcode", None)
        if attempt >= max_attempts - 1 or not _is_retryable_order_send_retcode(mt5, rc):
            return ret
        ok, _note = _refresh_market_order_price(mt5, request)
        if not ok:
            return ret
    return ret


def mt5_cancel_pending_order(
    ticket: int,
    *,
    dry_run: bool = False,
    terminal_path: Optional[str] = None,
    login: Optional[int] = None,
    password: Optional[str] = None,
    server: Optional[str] = None,
    shutdown_after: bool = True,
    terminal_session_only: bool = False,
) -> MT5ManageResult:
    """``TRADE_ACTION_REMOVE`` cho order ticket (lệnh chờ).

    ``terminal_session_only=True``: chỉ dùng acc đang login sẵn trong terminal (``initialize()`` không đối số).

    ``shutdown_after`` giữ lại để tương thích caller cũ; helper mới không đóng phiên sau thao tác.
    """
    if dry_run:
        return MT5ManageResult(
            ok=True,
            message=f"[DRY-RUN] Sẽ huỷ pending order ticket={ticket}",
            kind="pending",
        )
    if terminal_session_only:
        mt5 = _mt5_init_current_terminal()
    else:
        mt5 = _mt5_init(terminal_path, login, password, server)
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
        if shutdown_after:
            pass


def mt5_close_position(
    ticket: int,
    *,
    dry_run: bool = False,
    terminal_path: Optional[str] = None,
    login: Optional[int] = None,
    password: Optional[str] = None,
    server: Optional[str] = None,
) -> MT5ManageResult:
    """Đóng toàn bộ volume của position ``ticket`` (market close)."""
    if dry_run:
        return MT5ManageResult(
            ok=True,
            message=f"[DRY-RUN] Sẽ đóng position ticket={ticket}",
            kind="position",
        )
    mt5 = _mt5_init(terminal_path, login, password, server)
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

        req: dict[str, Any] = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym,
            "volume": vol,
            "type": otype,
            "position": int(ticket),
            "deviation": 20,
            "magic": int(getattr(pos, "magic", 2222222)),
            "comment": "tp1-review-close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        if not symbol_uses_market_execution(mt5, sym):
            req["price"] = price
        r = _send_market_close_with_retry(mt5, req)
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
        pass


def _volume_digits(step: float) -> int:
    text = f"{step:.10f}".rstrip("0").rstrip(".")
    if "." not in text:
        return 0
    return len(text.split(".", 1)[1])


def _normalize_close_volume(raw_volume: float, *, volume_min: float, volume_step: float) -> float:
    step = float(volume_step) if volume_step and volume_step > 0 else 0.01
    min_vol = float(volume_min) if volume_min and volume_min > 0 else step
    units = math.floor((float(raw_volume) + 1e-12) / step)
    vol = units * step
    if vol < min_vol - 1e-12:
        return 0.0
    return round(vol, _volume_digits(step))


def mt5_close_position_partial(
    ticket: int,
    *,
    fraction: float = 0.5,
    expected_initial_volume: Optional[float] = None,
    dry_run: bool = False,
    terminal_path: Optional[str] = None,
    login: Optional[int] = None,
    password: Optional[str] = None,
    server: Optional[str] = None,
) -> MT5ManageResult:
    """Đóng một phần position ``ticket`` bằng lệnh ngược chiều market.

    ``expected_initial_volume`` giúp idempotent: nếu volume hiện tại đã nhỏ hơn hoặc bằng
    runner còn lại sau khi đã chốt ``fraction`` thì không gửi thêm lệnh đóng.
    """
    pct = max(0.0, min(1.0, float(fraction)))
    if pct <= 0.0 or pct >= 1.0:
        return MT5ManageResult(
            ok=False,
            message=f"fraction không hợp lệ cho partial close: {fraction}",
            kind="position",
        )
    if dry_run:
        return MT5ManageResult(
            ok=True,
            message=f"[DRY-RUN] Sẽ đóng {pct:.0%} position ticket={ticket}",
            kind="position",
        )
    mt5 = _mt5_init(terminal_path, login, password, server)
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
        info = mt5.symbol_info(sym)
        volume_step = float(getattr(info, "volume_step", 0.01) or 0.01) if info is not None else 0.01
        volume_min = float(getattr(info, "volume_min", volume_step) or volume_step) if info is not None else volume_step
        if expected_initial_volume is not None and float(expected_initial_volume) > 0:
            runner_target = float(expected_initial_volume) * (1.0 - pct)
            tolerance = max(volume_step, 1e-9) / 2.0
            if vol <= runner_target + tolerance:
                return MT5ManageResult(
                    ok=True,
                    message=(
                        f"Đã chốt một phần trước đó hoặc chỉ còn runner "
                        f"ticket={ticket} symbol={sym} volume={vol:g}"
                    ),
                    kind="position",
                )

        close_vol = _normalize_close_volume(
            vol * pct,
            volume_min=volume_min,
            volume_step=volume_step,
        )
        if close_vol <= 0:
            return MT5ManageResult(
                ok=True,
                message=(
                    f"Volume quá nhỏ để chốt {pct:.0%}, bỏ qua partial close: ticket={ticket} "
                    f"volume={vol:g} min={volume_min:g} step={volume_step:g}"
                ),
                kind="position",
            )

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

        req: dict[str, Any] = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym,
            "volume": close_vol,
            "type": otype,
            "position": int(ticket),
            "deviation": 20,
            "magic": int(getattr(pos, "magic", 2222222)),
            "comment": "tp1-partial-close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        if not symbol_uses_market_execution(mt5, sym):
            req["price"] = price
        r = _send_market_close_with_retry(mt5, req)
        if r is None:
            return MT5ManageResult(
                ok=False,
                message=f"order_send PARTIAL_CLOSE trả None. {format_last_error(mt5)}",
                kind="position",
            )
        if not _is_done(mt5, r):
            return MT5ManageResult(
                ok=False,
                message=f"Chốt một phần thất bại: retcode={getattr(r, 'retcode', None)}",
                kind="position",
            )
        return MT5ManageResult(
            ok=True,
            message=f"Đã chốt {pct:.0%} position ticket={ticket} symbol={sym} volume={close_vol:g}",
            kind="position",
        )
    finally:
        pass


def mt5_latest_position_ticket(
    symbol: str,
    *,
    magic: Optional[int] = None,
    terminal_path: Optional[str] = None,
    login: Optional[int] = None,
    password: Optional[str] = None,
    server: Optional[str] = None,
) -> Optional[int]:
    """Ticket position mới nhất cho ``symbol`` (lọc magic nếu có)."""
    mt5 = _mt5_init(
        terminal_path=terminal_path,
        login=login,
        password=password,
        server=server,
    )
    if mt5 is None:
        return None
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


def mt5_ticket_still_open(
    ticket: int,
    *,
    dry_run: bool = False,
    terminal_path: Optional[str] = None,
    login: Optional[int] = None,
    password: Optional[str] = None,
    server: Optional[str] = None,
) -> tuple[bool, str]:
    """
    ``True`` nếu ``ticket`` vẫn là lệnh chờ *hoặc* position đang mở trên MT5.

    ``False`` nếu không còn (đã khớp + đóng, chốt TP, huỷ, v.v.) — không gọi follow-up TP1.

    Khi ``dry_run``: luôn coi như còn (không kết nối MT5).

    Khi ``mt5.initialize`` thất bại: trả ``True`` (tiếp tục follow-up; không chặn vì lỗi mạng).
    """
    if dry_run:
        return True, "[DRY-RUN] bỏ qua kiểm tra ticket"
    if ticket <= 0:
        return False, f"ticket không hợp lệ: {ticket}"
    mt5 = _mt5_init(
        terminal_path=terminal_path,
        login=login,
        password=password,
        server=server,
    )
    if mt5 is None:
        return True, "mt5.initialize thất bại — tiếp tục follow-up (không xác nhận được ticket)"
    has_order = any(int(o.ticket) == int(ticket) for o in (mt5.orders_get() or []))
    has_pos = any(int(p.ticket) == int(ticket) for p in (mt5.positions_get() or []))
    if has_order:
        return True, f"ticket={ticket} còn (pending order)"
    if has_pos:
        return True, f"ticket={ticket} còn (position mở)"
    return False, f"ticket={ticket} không còn (đã khớp đóng/chốt hoặc huỷ)"


def mt5_ticket_is_open_position(
    ticket: int,
    *,
    dry_run: bool = False,
    terminal_path: Optional[str] = None,
    login: Optional[int] = None,
    password: Optional[str] = None,
    server: Optional[str] = None,
) -> tuple[bool, str]:
    """
    ``True`` chỉ khi ``ticket`` là **position** đang mở (không tính lệnh chờ pending).

    Dùng trước khi dispatch follow-up 1R: chỉ chạy khi lệnh đã khớp thành position.

    ``dry_run``: coi như đạt (không gọi MT5).

    ``mt5.initialize`` thất bại: trả ``True`` (không chặn — cùng triết lý :func:`mt5_ticket_still_open`).
    """
    if dry_run:
        return True, "[DRY-RUN] bỏ qua kiểm tra position"
    if ticket <= 0:
        return False, f"ticket không hợp lệ: {ticket}"
    mt5 = _mt5_init(
        terminal_path=terminal_path,
        login=login,
        password=password,
        server=server,
    )
    if mt5 is None:
        return True, "mt5.initialize thất bại — tiếp tục (không xác nhận position)"
    has_order = any(int(o.ticket) == int(ticket) for o in (mt5.orders_get() or []))
    has_pos = any(int(p.ticket) == int(ticket) for p in (mt5.positions_get() or []))
    if has_pos:
        return True, f"ticket={ticket} còn (position mở)"
    if has_order:
        return False, f"ticket={ticket} vẫn pending (chưa position) — bỏ qua R1 follow-up"
    return False, f"ticket={ticket} không còn position/pending"


def mt5_ticket_current_sltp(
    ticket: int,
    *,
    dry_run: bool = False,
    terminal_path: Optional[str] = None,
    login: Optional[int] = None,
    password: Optional[str] = None,
    server: Optional[str] = None,
) -> tuple[Optional[float], Optional[float], str]:
    """
    Đọc SL/TP hiện tại theo ``ticket`` từ MT5.

    Ưu tiên position; nếu chưa có position thì thử pending order.
    """
    if dry_run:
        return None, None, "[DRY-RUN] bỏ qua đọc SL/TP"
    if ticket <= 0:
        return None, None, f"ticket không hợp lệ: {ticket}"
    mt5 = _mt5_init(
        terminal_path=terminal_path,
        login=login,
        password=password,
        server=server,
    )
    if mt5 is None:
        return None, None, "mt5.initialize thất bại"
    for p in mt5.positions_get() or []:
        if int(getattr(p, "ticket", 0)) != int(ticket):
            continue
        sl = float(getattr(p, "sl", 0.0) or 0.0)
        tp = float(getattr(p, "tp", 0.0) or 0.0)
        return sl, tp, f"ticket={ticket} position SL/TP"
    for o in mt5.orders_get() or []:
        if int(getattr(o, "ticket", 0)) != int(ticket):
            continue
        sl = float(getattr(o, "sl", 0.0) or 0.0)
        tp = float(getattr(o, "tp", 0.0) or 0.0)
        return sl, tp, f"ticket={ticket} pending SL/TP"
    return None, None, f"ticket={ticket} không còn position/pending"


def mt5_ticket_status_for_cutoff(
    ticket: int,
    *,
    dry_run: bool = False,
    terminal_path: Optional[str] = None,
    login: Optional[int] = None,
    password: Optional[str] = None,
    server: Optional[str] = None,
) -> tuple[Literal["pending", "position", "none", "error"], str]:
    """
    Phân loại ticket trên MT5 cho bước cắt giờ daemon-plan (lệnh chờ vs position vs đã hết).

    - Không truyền ``login``/``password``/``server``: chỉ xem **acc đang login** trong terminal
      (``initialize()`` không đối số). Không ``shutdown`` — giữ phiên chung với daemon đọc giá.
    - Có đủ ``login`` + ``password`` + ``server``: helper đảm bảo đúng account trước khi đọc.

    ``error`` = không kết nối được MT5 (cần thử lại).
    """
    if dry_run:
        return "none", "[DRY-RUN]"
    if ticket <= 0:
        return "none", f"ticket không hợp lệ: {ticket}"
    use_api = login is not None and bool(password) and bool(server)
    if use_api:
        mt5 = _mt5_init(
            terminal_path=terminal_path,
            login=login,
            password=password,
            server=server,
        )
        if mt5 is None:
            return "error", "mt5.initialize thất bại"
        has_order = any(int(o.ticket) == int(ticket) for o in (mt5.orders_get() or []))
        has_pos = any(int(p.ticket) == int(ticket) for p in (mt5.positions_get() or []))
    else:
        mt5 = _mt5_init_current_terminal()
        if mt5 is None:
            return "error", "mt5.initialize thất bại"
        has_order = any(int(o.ticket) == int(ticket) for o in (mt5.orders_get() or []))
        has_pos = any(int(p.ticket) == int(ticket) for p in (mt5.positions_get() or []))
    if has_order:
        return "pending", "lệnh chờ (pending)"
    if has_pos:
        return "position", "position đã khớp"
    return "none", "không còn pending/position"


def mt5_cancel_pending_or_close_position(
    ticket: int,
    *,
    dry_run: bool = False,
    terminal_path: Optional[str] = None,
    login: Optional[int] = None,
    password: Optional[str] = None,
    server: Optional[str] = None,
) -> MT5ManageResult:
    """Thử tìm pending ``ticket``; không có thì đóng position ``ticket``."""
    if dry_run:
        return MT5ManageResult(ok=True, message="[DRY-RUN] cancel/close", kind="none")
    mt5 = _mt5_init(terminal_path, login, password, server)
    if mt5 is None:
        return MT5ManageResult(ok=False, message="mt5.initialize thất bại", kind=None)
    has_order = any(int(o.ticket) == int(ticket) for o in (mt5.orders_get() or []))
    has_pos = any(int(p.ticket) == int(ticket) for p in (mt5.positions_get() or []))
    if has_order:
        return mt5_cancel_pending_order(
            ticket,
            dry_run=False,
            terminal_path=terminal_path,
            login=login,
            password=password,
            server=server,
        )
    if has_pos:
        return mt5_close_position(
            ticket,
            dry_run=False,
            terminal_path=terminal_path,
            login=login,
            password=password,
            server=server,
        )
    return MT5ManageResult(
        ok=False,
        message=f"Không có order/pending/position ticket={ticket}",
        kind="none",
    )


ChinhOutcome = Literal[
    "modified_sltp",
    "modified_pending",
    "dry_run",
    "ticket_missing",
    "incompatible_kind",
    "modify_failed",
]


@dataclass
class MT5ChinhTradeLineResult:
    """Kết quả chỉnh trade line tại chỗ (SL/TP hoặc modify pending), không đóng + mở mới."""

    ok: bool
    message: str
    outcome: ChinhOutcome


def _management_tp_price(trade: ParsedTrade) -> float:
    """Khi quản lý/chỉnh lệnh, ưu tiên TP2 làm TP runner nếu model trả về TP2."""
    return float(trade.tp2) if trade.tp2 is not None else float(trade.tp1)


def _order_price_open_py(o: Any) -> float:
    v = getattr(o, "price_open", None)
    if v is not None:
        return float(v)
    v2 = getattr(o, "price", None)
    if v2 is not None:
        return float(v2)
    return 0.0


def _order_volume_initial_py(o: Any) -> float:
    v = getattr(o, "volume_initial", None)
    if v is not None:
        return float(v)
    v2 = getattr(o, "volume_current", None)
    if v2 is not None:
        return float(v2)
    return 0.0


def mt5_chinh_trade_line_inplace(
    ticket: int,
    new_trade: ParsedTrade,
    *,
    dry_run: bool = False,
    symbol_override: Optional[str] = None,
    account_symbol_map: Optional[dict[str, str]] = None,
    terminal_path: Optional[str] = None,
    login: Optional[int] = None,
    password: Optional[str] = None,
    server: Optional[str] = None,
) -> MT5ChinhTradeLineResult:
    """
    Đã khớp (position) → ``TRADE_ACTION_SLTP`` (chỉ SL/TP).
    Chưa khớp (pending) → ``TRADE_ACTION_MODIFY`` (giá / SL / TP / lot nếu đổi).

    Nếu ticket không còn, loại lệnh AI không khớp pending (LIMIT↔STOP), hoặc broker từ chối:
    trả ``outcome`` tương ứng để caller quyết định (đặt mới / huỷ rồi đặt).
    """
    nt = resolve_mt5_trade_symbol(
        new_trade,
        symbol_override,
        account_symbol_map=account_symbol_map,
    )

    if dry_run:
        tp_manage = _management_tp_price(nt)
        return MT5ChinhTradeLineResult(
            ok=True,
            message=f"[DRY-RUN] Sẽ SLTP/modify ticket={ticket} theo trade_line mới (SL={nt.sl} TP={tp_manage})",
            outcome="dry_run",
        )

    if int(ticket) <= 0:
        return MT5ChinhTradeLineResult(
            ok=False,
            message=f"ticket không hợp lệ: {ticket}",
            outcome="ticket_missing",
        )

    mt5 = _mt5_init(terminal_path, login, password, server)
    if mt5 is None:
        return MT5ChinhTradeLineResult(
            ok=False,
            message="mt5.initialize thất bại",
            outcome="modify_failed",
        )

    try:
        order_obj: Any = None
        for o in mt5.orders_get() or []:
            if int(o.ticket) == int(ticket):
                order_obj = o
                break

        pos_obj: Any = None
        if order_obj is None:
            for p in mt5.positions_get() or []:
                if int(p.ticket) == int(ticket):
                    pos_obj = p
                    break

        if order_obj is None and pos_obj is None:
            return MT5ChinhTradeLineResult(
                ok=False,
                message=f"Không tìm thấy pending/position ticket={ticket}",
                outcome="ticket_missing",
            )

        if pos_obj is not None:
            sym = str(pos_obj.symbol)
            sym2, err = _ensure_symbol(mt5, sym)
            if err or not sym2:
                return MT5ChinhTradeLineResult(
                    ok=False,
                    message=err or f"Không select được symbol {sym!r}",
                    outcome="modify_failed",
                )
            req: dict[str, Any] = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": sym2,
                "position": int(ticket),
                "sl": float(nt.sl),
                "tp": _management_tp_price(nt),
            }
            r = mt5.order_send(req)
            if r is None:
                return MT5ChinhTradeLineResult(
                    ok=False,
                    message=f"order_send SLTP trả None. {format_last_error(mt5)}",
                    outcome="modify_failed",
                )
            if not _is_done(mt5, r):
                return MT5ChinhTradeLineResult(
                    ok=False,
                    message=f"Sửa SL/TP position thất bại: retcode={getattr(r, 'retcode', None)}",
                    outcome="modify_failed",
                )
            return MT5ChinhTradeLineResult(
                ok=True,
                message=f"Đã sửa SL/TP position ticket={ticket} symbol={sym2} SL={nt.sl} TP={_management_tp_price(nt)}",
                outcome="modified_sltp",
            )

        o = order_obj
        sym_o = str(o.symbol)
        sym2, err = _ensure_symbol(mt5, sym_o)
        if err or not sym2:
            return MT5ChinhTradeLineResult(
                ok=False,
                message=err or f"Không select được symbol {sym_o!r}",
                outcome="modify_failed",
            )

        otype_existing = int(o.type)
        price_cur = _order_price_open_py(o)
        if price_cur <= 0:
            return MT5ChinhTradeLineResult(
                ok=False,
                message="Không đọc được giá lệnh chờ trên MT5",
                outcome="modify_failed",
            )

        if nt.kind in ("LIMIT", "STOP"):
            try:
                expected = int(_order_type_for_pending(nt, mt5))
            except ValueError:
                return MT5ChinhTradeLineResult(
                    ok=False,
                    message=f"Loại lệnh không hỗ trợ pending: {nt.kind}",
                    outcome="incompatible_kind",
                )
            if expected != otype_existing:
                return MT5ChinhTradeLineResult(
                    ok=False,
                    message=(
                        f"Loại lệnh AI ({nt.side} {nt.kind}) không khớp lệnh chờ trên MT5 "
                        f"(type={otype_existing}) — cần huỷ và đặt lại"
                    ),
                    outcome="incompatible_kind",
                )

        if nt.kind == "MARKET":
            price_mod = price_cur
        else:
            price_mod = float(nt.price) if nt.price is not None else price_cur

        vol_o = _order_volume_initial_py(o)
        req_m: dict[str, Any] = {
            "action": mt5.TRADE_ACTION_MODIFY,
            "order": int(ticket),
            "symbol": sym2,
            "price": price_mod,
            "sl": float(nt.sl),
            "tp": _management_tp_price(nt),
        }
        if vol_o > 0 and abs(float(nt.lot) - vol_o) > 1e-9:
            req_m["volume"] = float(nt.lot)

        r2 = mt5.order_send(req_m)
        if r2 is None:
            return MT5ChinhTradeLineResult(
                ok=False,
                message=f"order_send MODIFY trả None. {format_last_error(mt5)}",
                outcome="modify_failed",
            )
        if not _is_done(mt5, r2):
            return MT5ChinhTradeLineResult(
                ok=False,
                message=f"Modify lệnh chờ thất bại: retcode={getattr(r2, 'retcode', None)}",
                outcome="modify_failed",
            )
        return MT5ChinhTradeLineResult(
            ok=True,
            message=(
                f"Đã modify pending ticket={ticket} symbol={sym2} "
                f"price={price_mod} SL={nt.sl} TP={_management_tp_price(nt)}"
            ),
            outcome="modified_pending",
        )
    finally:
        pass


def mt5_cancel_all_pending_orders(
    *,
    dry_run: bool = False,
    terminal_path: Optional[str] = None,
    login: Optional[int] = None,
    password: Optional[str] = None,
    server: Optional[str] = None,
) -> tuple[int, int, list[str]]:
    """Huỷ **toàn bộ** pending orders (lệnh chờ) trong account MT5.

    Returns:
        ``(n_cancelled, n_failed, messages)`` — đếm lệnh huỷ thành công/thất bại và log chi tiết.
    """
    if dry_run:
        return 0, 0, ["[DRY-RUN] Sẽ huỷ tất cả pending orders"]
    mt5 = _mt5_init(terminal_path, login, password, server)
    if mt5 is None:
        return 0, 0, ["mt5.initialize thất bại"]
    orders = list(mt5.orders_get() or [])
    if not orders:
        return 0, 0, ["không có pending order"]
    n_ok = 0
    n_fail = 0
    msgs: list[str] = []
    for o in orders:
        ticket = int(getattr(o, "ticket", 0))
        if ticket <= 0:
            continue
        req = {"action": mt5.TRADE_ACTION_REMOVE, "order": ticket}
        r = mt5.order_send(req)
        if r is not None and int(getattr(r, "retcode", -1)) == int(mt5.TRADE_RETCODE_DONE):
            n_ok += 1
            msgs.append(f"ticket={ticket}: đã huỷ ✓")
        else:
            n_fail += 1
            comment = getattr(r, "comment", None) if r is not None else "order_send=None"
            rc = getattr(r, "retcode", None) if r is not None else None
            msgs.append(f"ticket={ticket}: thất bại ({comment or f'retcode={rc}'})")
    return n_ok, n_fail, msgs
