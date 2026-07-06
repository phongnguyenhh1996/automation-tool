"""Live MT5 XAU price for scalp SL/TP — không dùng giá gold futures từ Telegram."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from automation_tool.mt5_accounts import reference_price_for_lot
from automation_tool.mt5_candles import resolve_mt5_broker_symbol
from automation_tool.mt5_execute import (
    MT5ExecutionResult,
    _ensure_symbol,
    _is_mt5_trade_success_retcode,
    execute_trade,
    format_last_error,
)
from automation_tool.mt5_openai_parse import ParsedTrade

from exec_line import DEFAULT_SL_POINTS, DEFAULT_TP_POINTS, scalp_market_sl_tp

_log = logging.getLogger(__name__)

XAU_LOGIC = "XAUUSD"
XAU_BROKER_CANDIDATES = ("XAUUSD", "XAUUSDm", "XAUUSDc")
_DEFAULT_MT5_MAGIC = 2222222


def xau_symbol_candidates(account_symbol_map: Optional[dict[str, str]] = None) -> list[str]:
    out: list[str] = []
    mapped = resolve_mt5_broker_symbol(XAU_LOGIC, account_symbol_map=account_symbol_map)
    for cand in (mapped, *XAU_BROKER_CANDIDATES):
        name = (cand or "").strip()
        if name and name not in out:
            out.append(name)
    return out


def resolve_xau_symbol_on_mt5(
    mt5: Any,
    account_symbol_map: Optional[dict[str, str]] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Thử XAUUSD / XAUUSDm / XAUUSDc (+ symbol_map) trên broker."""
    last_err: Optional[str] = None
    for cand in xau_symbol_candidates(account_symbol_map):
        sym, err = _ensure_symbol(mt5, cand)
        if sym:
            return sym, None
        last_err = err
    return None, last_err or "Không tìm thấy XAUUSD/XAUUSDm/XAUUSDc trên broker"


def fetch_market_entry_price(mt5: Any, resolved_symbol: str, side: str) -> tuple[float, Optional[str]]:
    side_lit: str = "BUY" if side == "BUY" else "SELL"
    stub = ParsedTrade(
        symbol=resolved_symbol,
        side=side_lit,  # type: ignore[arg-type]
        kind="MARKET",
        price=None,
        sl=0.0,
        tp1=0.0,
        tp2=None,
        lot=0.01,
        raw_line="",
    )
    return reference_price_for_lot(mt5, resolved_symbol, stub)


def build_scalp_trade_live(
    parsed: dict[str, Any],
    *,
    lot: float,
    mt5: Any,
    account_symbol_map: Optional[dict[str, str]] = None,
    sl_points: float = DEFAULT_SL_POINTS,
    tp_points: float = DEFAULT_TP_POINTS,
) -> tuple[ParsedTrade, float]:
    """
    SL/TP ±N giá từ bid/ask MT5 hiện tại (BUY=ask, SELL=bid).
    Trả về ``(trade, live_entry)``.
    """
    side = str(parsed.get("side") or "")
    sym, err = resolve_xau_symbol_on_mt5(mt5, account_symbol_map)
    if not sym:
        raise RuntimeError(err or "Không resolve được symbol XAU trên MT5")

    entry, price_err = fetch_market_entry_price(mt5, sym, side)
    if entry <= 0:
        raise RuntimeError(price_err or f"symbol_info_tick({sym!r}) không có giá")

    sl, tp1 = scalp_market_sl_tp(entry, side, sl_points=sl_points, tp_points=tp_points)
    side_lit: str = "BUY" if side == "BUY" else "SELL"
    trade = ParsedTrade(
        symbol=sym,
        side=side_lit,  # type: ignore[arg-type]
        kind="MARKET",
        price=None,
        sl=sl,
        tp1=tp1,
        tp2=None,
        lot=float(lot),
        raw_line=str(parsed.get("raw_line") or ""),
    )
    return trade, entry


def _mt5_magic() -> int:
    raw = (os.getenv("MT5_MAGIC") or "").strip()
    if not raw:
        return _DEFAULT_MT5_MAGIC
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_MT5_MAGIC


def build_scalp_market_entry_only(
    parsed: dict[str, Any],
    *,
    lot: float,
    mt5: Any,
    account_symbol_map: Optional[dict[str, str]] = None,
) -> ParsedTrade:
    """MARKET entry without SL/TP — gửi lệnh nhanh, gắn SL/TP sau khi khớp."""
    side = str(parsed.get("side") or "")
    sym, err = resolve_xau_symbol_on_mt5(mt5, account_symbol_map)
    if not sym:
        raise RuntimeError(err or "Không resolve được symbol XAU trên MT5")
    side_lit: str = "BUY" if side == "BUY" else "SELL"
    return ParsedTrade(
        symbol=sym,
        side=side_lit,  # type: ignore[arg-type]
        kind="MARKET",
        price=None,
        sl=0.0,
        tp1=0.0,
        tp2=None,
        lot=float(lot),
        raw_line=str(parsed.get("raw_line") or ""),
    )


def find_scalp_position_ticket(
    mt5: Any,
    *,
    symbol: str,
    magic: Optional[int] = None,
    preferred_ticket: Optional[int] = None,
) -> Optional[int]:
    """Ticket position sau market fill (ưu tiên ``preferred_ticket`` từ order_send)."""
    mg = _mt5_magic() if magic is None else int(magic)
    if preferred_ticket is not None and int(preferred_ticket) > 0:
        for p in mt5.positions_get(symbol=symbol) or []:
            if int(p.ticket) == int(preferred_ticket):
                return int(preferred_ticket)
    best: Optional[tuple[int, int]] = None
    for p in mt5.positions_get(symbol=symbol) or []:
        if int(getattr(p, "magic", 0) or 0) != mg:
            continue
        t = int(p.ticket)
        tm = int(getattr(p, "time", 0) or getattr(p, "time_msc", 0) or 0)
        if best is None or tm >= best[0]:
            best = (tm, t)
    return best[1] if best else None


def attach_scalp_sltp_to_position(
    mt5: Any,
    *,
    position_ticket: int,
    side: str,
    sl_points: float = DEFAULT_SL_POINTS,
    tp_points: float = DEFAULT_TP_POINTS,
) -> tuple[float, float, float]:
    """Đặt SL/TP từ ``price_open`` của position. Trả ``(entry, sl, tp)``."""
    pos = None
    for p in mt5.positions_get() or []:
        if int(p.ticket) == int(position_ticket):
            pos = p
            break
    if pos is None:
        raise RuntimeError(f"Không tìm thấy position ticket={position_ticket} để gắn SL/TP")

    sym = str(pos.symbol)
    sym2, err = _ensure_symbol(mt5, sym)
    if err or not sym2:
        raise RuntimeError(err or f"Không select được symbol {sym!r}")

    entry = float(getattr(pos, "price_open", 0) or 0)
    if entry <= 0:
        raise RuntimeError(f"price_open không hợp lệ cho position ticket={position_ticket}")

    sl, tp = scalp_market_sl_tp(entry, side, sl_points=sl_points, tp_points=tp_points)
    req = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": sym2,
        "position": int(position_ticket),
        "sl": float(sl),
        "tp": float(tp),
    }
    ret = mt5.order_send(req)
    if ret is None:
        raise RuntimeError(f"order_send SLTP trả None. {format_last_error(mt5)}")
    rc = getattr(ret, "retcode", None)
    if not _is_mt5_trade_success_retcode(mt5, rc):
        raise RuntimeError(
            f"Gắn SL/TP thất bại ticket={position_ticket} retcode={rc} "
            f"SL={sl} TP={tp}. {format_last_error(mt5)}"
        )
    _log.info(
        "Scalp SLTP attached ticket=%s entry=%s sl=%s tp=%s",
        position_ticket,
        entry,
        sl,
        tp,
    )
    return entry, sl, tp


def execute_scalp_market_fast(
    parsed: dict[str, Any],
    *,
    lot: float,
    dry_run: bool,
    sl_points: float,
    tp_points: float,
    terminal_path: Optional[str] = None,
    login: Optional[int] = None,
    password: Optional[str] = None,
    server: Optional[str] = None,
    account_symbol_map: Optional[dict[str, str]] = None,
    account_id: Optional[str] = None,
    order_comment: Optional[str] = None,
) -> tuple[MT5ExecutionResult, float, float, float]:
    """
    Phase 1: MARKET không SL/TP. Phase 2: đọc ``price_open`` position → gắn SL/TP ±N giá.
    """
    from automation_tool.mt5_execute import ensure_mt5_session

    side = str(parsed.get("side") or "")
    comment = order_comment or f"scalp-{parsed.get('pattern_id', '')}"[:31]

    if dry_run:
        session = ensure_mt5_session(
            terminal_path=terminal_path,
            login=login,
            password=password,
            server=server,
        )
        if session.ok:
            trade = build_scalp_market_entry_only(
                parsed,
                lot=lot,
                mt5=session.mt5,
                account_symbol_map=account_symbol_map,
            )
            entry, _ = fetch_market_entry_price(session.mt5, trade.symbol, side)
            sl, tp = scalp_market_sl_tp(entry, side, sl_points=sl_points, tp_points=tp_points)
            msg = (
                f"[DRY-RUN] MARKET {side} {trade.symbol} lot={lot} (no SL/TP on entry) → "
                f"then SLTP entry≈{entry} SL={sl} TP={tp}"
            )
            return (
                MT5ExecutionResult(
                    ok=True,
                    message=msg,
                    account_id=account_id,
                    resolved_symbol=trade.symbol,
                ),
                entry,
                sl,
                tp,
            )
        entry = float(parsed.get("entry_price") or 0)
        sl, tp = scalp_market_sl_tp(entry, side, sl_points=sl_points, tp_points=tp_points)
        return (
            MT5ExecutionResult(
                ok=True,
                message=f"[DRY-RUN] MARKET (session fallback) entry≈{entry} SL={sl} TP={tp}",
                account_id=account_id,
            ),
            entry,
            sl,
            tp,
        )

    session = ensure_mt5_session(
        terminal_path=terminal_path,
        login=login,
        password=password,
        server=server,
    )
    if not session.ok:
        return (
            MT5ExecutionResult(
                ok=False,
                message=session.message,
                account_id=account_id,
                last_error=session.last_error,
            ),
            0.0,
            0.0,
            0.0,
        )

    trade = build_scalp_market_entry_only(
        parsed,
        lot=lot,
        mt5=session.mt5,
        account_symbol_map=account_symbol_map,
    )
    ex = execute_trade(
        trade,
        terminal_path=terminal_path,
        login=login,
        password=password,
        server=server,
        dry_run=False,
        lot_override=lot,
        take_profit_target="tp1",
        take_profit_override=0.0,
        log_tp2=False,
        order_comment=comment,
        account_id=account_id,
        account_symbol_map=account_symbol_map,
    )
    if not ex.ok:
        return ex, 0.0, 0.0, 0.0

    pos_ticket = find_scalp_position_ticket(
        session.mt5,
        symbol=trade.symbol,
        preferred_ticket=ex.order,
    )
    if pos_ticket is None:
        return (
            MT5ExecutionResult(
                ok=False,
                message=(
                    f"MARKET OK nhưng không tìm thấy position để gắn SL/TP "
                    f"(symbol={trade.symbol} order={ex.order})"
                ),
                account_id=account_id,
                order=ex.order,
                deal=ex.deal,
                resolved_symbol=trade.symbol,
            ),
            0.0,
            0.0,
            0.0,
        )

    try:
        entry, sl, tp = attach_scalp_sltp_to_position(
            session.mt5,
            position_ticket=pos_ticket,
            side=side,
            sl_points=sl_points,
            tp_points=tp_points,
        )
    except Exception as e:
        return (
            MT5ExecutionResult(
                ok=False,
                message=f"MARKET filled ticket={pos_ticket} nhưng SLTP failed: {e!r}",
                account_id=account_id,
                order=ex.order,
                deal=ex.deal,
                resolved_symbol=trade.symbol,
            ),
            0.0,
            0.0,
            0.0,
        )

    return (
        MT5ExecutionResult(
            ok=True,
            message=f"{ex.message}\n→ SLTP position={pos_ticket} entry={entry} SL={sl} TP={tp}",
            account_id=account_id,
            order=ex.order,
            deal=ex.deal,
            retcode=ex.retcode,
            resolved_symbol=trade.symbol,
        ),
        entry,
        sl,
        tp,
    )
