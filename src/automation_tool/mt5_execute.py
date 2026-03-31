"""
Thực thi lệnh qua MetaTrader5 (Python package ``MetaTrader5``).

**Mac (dev):** gói thường không cài được; dùng ``mt5-trade`` không ``--execute`` (dry-run).

**Windows VPS (prod):** cài MT5 terminal, đăng nhập sẵn tài khoản, giữ terminal mở; chạy script
với ``--execute``. Nếu không đặt ``MT5_LOGIN`` / ``MT5_PASSWORD`` / ``MT5_SERVER``, gọi
``initialize()`` không đối số — bám phiên đăng nhập hiện có trong terminal (đúng với flow
“VPS đã cài và đăng nhập sẵn MetaTrader5”).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from automation_tool.mt5_openai_parse import ParsedTrade


@dataclass
class MT5ExecutionResult:
    ok: bool
    message: str
    retcode: Optional[int] = None
    order: Optional[int] = None
    deal: Optional[int] = None
    request: Optional[dict[str, Any]] = None


def _load_mt5():
    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError as e:
        raise SystemExit(
            "Cần cài MetaTrader5: pip install MetaTrader5\n"
            "Lưu ý: gói chính thức chủ yếu hỗ trợ Windows với terminal MT5 đã cài.\n"
            "Tham khảo: https://www.mql5.com/en/docs/python_metatrader5"
        ) from e
    return mt5


def _filling_for_symbol(mt5: Any, symbol: str) -> int:
    info = mt5.symbol_info(symbol)
    if info is None:
        return mt5.ORDER_FILLING_IOC
    mode = int(info.filling_mode)
    if mode & int(mt5.SYMBOL_FILLING_IOC):
        return mt5.ORDER_FILLING_IOC
    if mode & int(mt5.SYMBOL_FILLING_FOK):
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def _order_type_for_pending(trade: ParsedTrade, mt5: Any) -> int:
    side = trade.side
    kind = trade.kind
    if kind == "LIMIT":
        if side == "BUY":
            return mt5.ORDER_TYPE_BUY_LIMIT
        return mt5.ORDER_TYPE_SELL_LIMIT
    if kind == "STOP":
        if side == "BUY":
            return mt5.ORDER_TYPE_BUY_STOP
        return mt5.ORDER_TYPE_SELL_STOP
    raise ValueError(f"Loại lệnh không hỗ trợ pending: {kind}")


def _ensure_symbol(mt5: Any, symbol: str) -> Optional[str]:
    if not mt5.symbol_select(symbol, True):
        return f"Không chọn được symbol {symbol!r} (không có trong Market Watch?)."
    return None


def build_request(
    mt5: Any,
    trade: ParsedTrade,
    *,
    deviation: int = 20,
    magic: int = 2222222,
    comment: str = "openai-auto",
) -> dict[str, Any]:
    sym = trade.symbol
    err = _ensure_symbol(mt5, sym)
    if err:
        raise RuntimeError(err)

    filling = _filling_for_symbol(mt5, sym)
    if trade.kind == "MARKET":
        tick = mt5.symbol_info_tick(sym)
        if tick is None:
            raise RuntimeError(f"Không lấy được tick cho {sym}.")
        price = tick.ask if trade.side == "BUY" else tick.bid
        otype = mt5.ORDER_TYPE_BUY if trade.side == "BUY" else mt5.ORDER_TYPE_SELL
        return {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym,
            "volume": trade.lot,
            "type": otype,
            "price": price,
            "sl": trade.sl,
            "tp": trade.tp1,
            "deviation": deviation,
            "magic": magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }

    if trade.price is None:
        raise RuntimeError("LIMIT/STOP cần giá entry.")
    otype = _order_type_for_pending(trade, mt5)
    return {
        "action": mt5.TRADE_ACTION_PENDING,
        "symbol": sym,
        "volume": trade.lot,
        "type": otype,
        "price": trade.price,
        "sl": trade.sl,
        "tp": trade.tp1,
        "deviation": deviation,
        "magic": magic,
        "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }


def _env_int(name: str, default: int = 0) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    return int(raw)


def execute_trade(
    trade: ParsedTrade,
    *,
    login: Optional[int] = None,
    password: Optional[str] = None,
    server: Optional[str] = None,
    dry_run: bool = True,
    deviation: int = 20,
    magic: Optional[int] = None,
    log_tp2: bool = True,
) -> MT5ExecutionResult:
    """
    Gửi lệnh qua MetaTrader5. MT5 chỉ có một TP trên lệnh; TP2 được in ra nếu có.

    Credentials: env ``MT5_LOGIN``, ``MT5_PASSWORD``, ``MT5_SERVER`` hoặc tham số.
    """
    login_i = login if login is not None else _env_int("MT5_LOGIN", 0)
    password_s = password if password is not None else (os.getenv("MT5_PASSWORD") or "")
    server_s = server if server is not None else (os.getenv("MT5_SERVER") or "")
    mag = magic if magic is not None else _env_int("MT5_MAGIC", 2222222)

    extra = ""
    if trade.tp2 is not None and log_tp2:
        extra = f" (TP2={trade.tp2} — MT5 chỉ đặt TP1 trên lệnh; đóng một phần tại TP2 thủ công hoặc EA riêng)"

    if dry_run:
        req_preview: dict[str, Any] = {
            "symbol": trade.symbol,
            "side": trade.side,
            "kind": trade.kind,
            "price": trade.price,
            "sl": trade.sl,
            "tp1": trade.tp1,
            "tp2": trade.tp2,
            "lot": trade.lot,
        }
        return MT5ExecutionResult(
            ok=True,
            message=f"[DRY-RUN] Sẽ gửi: {req_preview}{extra}",
            request=req_preview,
        )

    mt5 = _load_mt5()
    kwargs: dict[str, Any] = {}
    if login_i and password_s and server_s:
        kwargs["login"] = login_i
        kwargs["password"] = password_s
        kwargs["server"] = server_s

    if not mt5.initialize(**kwargs):
        err = mt5.last_error()
        return MT5ExecutionResult(
            ok=False,
            message=f"mt5.initialize thất bại: {err}",
        )

    try:
        try:
            request = build_request(
                mt5,
                trade,
                deviation=deviation,
                magic=mag,
            )
        except RuntimeError as e:
            return MT5ExecutionResult(ok=False, message=str(e))

        chk = mt5.order_check(request)
        if chk is None:
            return MT5ExecutionResult(
                ok=False,
                message=f"order_check failed: {mt5.last_error()}",
                request=request,
            )
        chk_rc = getattr(chk, "retcode", None)
        if chk_rc is not None and chk_rc != mt5.TRADE_RETCODE_DONE:
            return MT5ExecutionResult(
                ok=False,
                message=f"order_check retcode={chk_rc} balance={getattr(chk, 'balance', None)}",
                request=request,
            )
        ret = mt5.order_send(request)
        if ret is None:
            return MT5ExecutionResult(
                ok=False,
                message=f"order_send None: {mt5.last_error()}",
                request=request,
            )
        rc = getattr(ret, "retcode", None)
        ok_codes = {mt5.TRADE_RETCODE_DONE}
        placed = getattr(mt5, "TRADE_RETCODE_PLACED", None)
        if placed is not None:
            ok_codes.add(placed)
        if rc not in ok_codes:
            return MT5ExecutionResult(
                ok=False,
                message=f"Lỗi retcode={rc} comment={getattr(ret, 'comment', '')!r}{extra}",
                retcode=int(rc) if rc is not None else None,
                request=request,
            )
        return MT5ExecutionResult(
            ok=True,
            message=f"OK: order={getattr(ret, 'order', None)} deal={getattr(ret, 'deal', None)}{extra}",
            retcode=int(rc) if rc is not None else None,
            order=getattr(ret, "order", None),
            deal=getattr(ret, "deal", None),
            request=request,
        )
    finally:
        mt5.shutdown()
