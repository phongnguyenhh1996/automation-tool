"""
Thực thi lệnh qua MetaTrader5 (Python package ``MetaTrader5``).

**Mac (dev):** gói thường không cài được; dùng ``mt5-trade --dry-run`` hoặc ``--mt5-dry-run`` trên CLI tự động (mô phỏng).

**Windows VPS (prod):** cài MT5 terminal, đăng nhập sẵn tài khoản, giữ terminal mở; mặc định gửi lệnh thật. Nếu không đặt ``MT5_LOGIN`` / ``MT5_PASSWORD`` / ``MT5_SERVER``, gọi
``initialize()`` không đối số — bám phiên đăng nhập hiện có trong terminal (đúng với flow
“VPS đã cài và đăng nhập sẵn MetaTrader5”).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any, Optional

import automation_tool.config  # noqa: F401 — nạp .env khi import

from automation_tool.mt5_openai_parse import ParsedTrade, normalize_broker_xau_symbol


@dataclass
class MT5ExecutionResult:
    ok: bool
    message: str
    retcode: Optional[int] = None
    order: Optional[int] = None
    deal: Optional[int] = None
    # Multi-account: id trong ``accounts.json`` (optional)
    account_id: Optional[str] = None
    request: Optional[dict[str, Any]] = None
    # Tuple (code, message) từ mt5.last_error() sau thao tác lỗi (nếu có)
    last_error: Optional[tuple[Any, ...]] = None
    # Cấu trúc server trả về (order_send / order_check) khi có
    trade_check: Optional[dict[str, Any]] = None
    trade_result: Optional[dict[str, Any]] = None
    # Symbol thực tế dùng sau khi áp CLI và chuẩn hóa XAUUSD → XAUUSDm
    resolved_symbol: Optional[str] = None


def format_mt5_execution_for_telegram(ex: MT5ExecutionResult) -> str:
    """Chuỗi plain text cho log Telegram (TELEGRAM_CHAT_ID sau ``execute_trade``)."""
    parts: list[str] = []
    if ex.account_id:
        parts.append(f"[{ex.account_id}]")
    parts.append(ex.message)
    if ex.resolved_symbol:
        parts.append(f"Symbol: {ex.resolved_symbol}")
    if ex.request is not None:
        parts.append(f"request: {ex.request}")
    if ex.retcode is not None:
        parts.append(f"retcode: {ex.retcode}")
    if ex.order is not None:
        parts.append(f"order: {ex.order}")
    if ex.deal is not None:
        parts.append(f"deal: {ex.deal}")
    if ex.last_error is not None:
        parts.append(f"last_error: {ex.last_error}")
    if ex.trade_check is not None:
        parts.append(f"trade_check: {ex.trade_check}")
    if ex.trade_result is not None:
        parts.append(f"trade_result: {ex.trade_result}")
    return "\n".join(parts)


@dataclass
class MT5LoginResult:
    """Kết quả ``mt5-login``: initialize + ``account_info``."""

    ok: bool
    lines: list[str]


def check_mt5_login(
    login: Optional[int] = None,
    password: Optional[str] = None,
    server: Optional[str] = None,
) -> MT5LoginResult:
    """
    Gọi ``initialize()`` (session terminal hoặc ``MT5_*`` trong env), in phiên bản gói,
    terminal, và ``account_info``. Luôn ``shutdown()`` ở cuối.
    """
    mt5 = _load_mt5()
    login_i = login if login is not None else _env_int("MT5_LOGIN", 0)
    password_s = password if password is not None else (os.getenv("MT5_PASSWORD") or "")
    server_s = server if server is not None else (os.getenv("MT5_SERVER") or "")

    lines: list[str] = [
        f"MetaTrader5 (pip): {mt5.__version__}",
    ]
    kwargs: dict[str, Any] = {}
    if login_i and password_s and server_s:
        kwargs["login"] = login_i
        kwargs["password"] = password_s
        kwargs["server"] = server_s
        lines.append(
            f"Chế độ: đăng nhập qua API (login={login_i}, server={server_s!r}).",
        )
    else:
        lines.append(
            "Chế độ: initialize() không đối số — dùng phiên MT5 đang mở (đã login trong terminal).",
        )

    if not mt5.initialize(**kwargs):
        lines.append(f"mt5.initialize thất bại: {format_last_error(mt5)}")
        return MT5LoginResult(ok=False, lines=lines)

    try:
        ti = mt5.terminal_info()
        if ti is not None:
            lines.append(
                f"Terminal: build={getattr(ti, 'build', '?')} "
                f"name={getattr(ti, 'name', '?')!r}",
            )
        ai = mt5.account_info()
        if ai is None:
            lines.append(
                f"account_info trả về None — chưa có tài khoản kết nối hoặc lỗi. "
                f"{format_last_error(mt5)}",
            )
            return MT5LoginResult(ok=False, lines=lines)

        lines.append("Đăng nhập OK — account_info:")
        lines.append(f"  login:    {ai.login}")
        lines.append(f"  server:   {ai.server}")
        lines.append(f"  company:  {getattr(ai, 'company', '')}")
        lines.append(f"  name:     {getattr(ai, 'name', '')}")
        lines.append(f"  currency: {ai.currency}")
        lines.append(f"  balance:  {ai.balance}")
        lines.append(f"  equity:   {ai.equity}")
        lines.append(f"  margin:   {getattr(ai, 'margin', '')}")
        lev = getattr(ai, "leverage", None)
        if lev is not None:
            lines.append(f"  leverage: 1:{lev}")
        lines.append(f"  trade_allowed: {getattr(ai, 'trade_allowed', '?')}")
        return MT5LoginResult(ok=True, lines=lines)
    finally:
        mt5.shutdown()


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


def _last_error_tuple(mt5: Any) -> Optional[tuple[Any, ...]]:
    try:
        t = mt5.last_error()
        if t is None:
            return None
        if isinstance(t, tuple) and len(t) == 0:
            return None
        return t
    except Exception:
        return None


def format_last_error(mt5: Any) -> str:
    """Chuỗi mô tả ``mt5.last_error()`` (thường là ``(code, message)``)."""
    t = _last_error_tuple(mt5)
    if t is None:
        return "last_error=None"
    if isinstance(t, tuple) and len(t) >= 2:
        code, msg = t[0], t[1]
        return f"code={code} message={msg!r}"
    return f"last_error={t!r}"


def _format_last_error_if_meaningful(mt5: Any) -> str:
    """Không nối ``(1, 'Success')`` vào cuối thông báo lỗi nghiệp vụ — dễ gây hiểu nhầm."""
    t = _last_error_tuple(mt5)
    if t is None or len(t) < 2:
        return ""
    try:
        code = int(t[0])
    except (TypeError, ValueError):
        return f" | {format_last_error(mt5)}"
    msg_l = str(t[1]).lower()
    if code == 1 and "success" in msg_l:
        return ""
    return f" | {format_last_error(mt5)}"


def _retcode_label(mt5: Any, code: Optional[int]) -> str:
    if code is None:
        return "None"
    try:
        ic = int(code)
    except (TypeError, ValueError):
        return str(code)
    for name in dir(mt5):
        if not name.startswith("TRADE_RETCODE_"):
            continue
        try:
            if int(getattr(mt5, name)) == ic:
                return f"{name}({ic})"
        except (TypeError, ValueError):
            continue
    return str(ic)


def _is_mt5_trade_success_retcode(mt5: Any, rc: Optional[int]) -> bool:
    """
    ``order_check`` / ``order_send`` thành công thường là ``TRADE_RETCODE_DONE`` (10009)
    hoặc ``TRADE_RETCODE_PLACED``.

    Một số bản MetaTrader5 Python / broker trả ``retcode=0`` kèm ``comment='Done'`` —
    nếu chỉ so với ``TRADE_RETCODE_DONE`` sẽ từ chối nhầm và không gọi ``order_send``.
    """
    if rc is None:
        return False
    try:
        irc = int(rc)
    except (TypeError, ValueError):
        return False
    try:
        if irc == int(mt5.TRADE_RETCODE_DONE):
            return True
    except (TypeError, ValueError):
        pass
    if irc == 0:
        return True
    placed = getattr(mt5, "TRADE_RETCODE_PLACED", None)
    if placed is not None:
        try:
            if irc == int(placed):
                return True
        except (TypeError, ValueError):
            pass
    return False


def _trade_retcode_hint(mt5: Any, retcode: Optional[int]) -> str:
    """Gợi ý tiếng Việt cho retcode phổ biến (AutoTrading, margin, …)."""
    if retcode is None:
        return ""
    try:
        rc = int(retcode)
    except (TypeError, ValueError):
        return ""
    dis_at = getattr(mt5, "TRADE_RETCODE_CLIENT_DISABLES_AT", None)
    if (dis_at is not None and rc == int(dis_at)) or rc == 10027:
        return (
            " | Gợi ý: AutoTrading/Algo Trading đang TẮT — trong MT5 bật nút \"Algo Trading\" "
            "trên thanh công cụ (biểu tượng play trong tam giác), hoặc "
            "Tools → Options → Expert Advisors → bật \"Allow Algo Trading\" / giao dịch thuật toán, "
            "rồi chạy lại lệnh."
        )
    nm = getattr(mt5, "TRADE_RETCODE_NO_MONEY", None)
    if nm is not None and rc == int(nm):
        return (
            " | Gợi ý: không đủ tiền/margin (NO_MONEY) — thử giảm lot, "
            "kiểm tra leverage & margin trong MT5 (Symbol properties), nạp thêm balance, "
            "hoặc đóng lệnh/vị thế đang chiếm margin."
        )
    if rc == 10019:
        return " | Gợi ý: retcode 10019 = không đủ margin/free margin cho yêu cầu này."
    return ""


def _object_fields_dict(obj: Any, names: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in names:
        if hasattr(obj, name):
            try:
                out[name] = getattr(obj, name)
            except Exception as exc:  # noqa: BLE001
                out[name] = f"<{exc}>"
    return out


def _trade_check_dict(chk: Any) -> dict[str, Any]:
    return _object_fields_dict(
        chk,
        (
            "retcode",
            "balance",
            "equity",
            "profit",
            "margin",
            "margin_free",
            "margin_level",
            "comment",
        ),
    )


def _trade_result_dict(ret: Any) -> dict[str, Any]:
    return _object_fields_dict(
        ret,
        (
            "retcode",
            "deal",
            "order",
            "volume",
            "price",
            "bid",
            "ask",
            "comment",
            "request_id",
            "retcode_external",
        ),
    )


# Bitmask ``symbol_info.filling_mode`` (MQL5 ENUM_SYMBOL_FILLING_MODE). Gói Python
# thường không có ``SYMBOL_FILLING_*`` trên module — chỉ dùng số cho phép AND.
_SYM_FILL_FOK = 1
_SYM_FILL_IOC = 2
_SYM_FILL_RETURN = 4


def _filling_for_symbol(mt5: Any, symbol: str) -> int:
    info = mt5.symbol_info(symbol)
    if info is None:
        return int(mt5.ORDER_FILLING_IOC)
    mode = int(info.filling_mode)
    if mode & _SYM_FILL_IOC:
        return int(mt5.ORDER_FILLING_IOC)
    if mode & _SYM_FILL_FOK:
        return int(mt5.ORDER_FILLING_FOK)
    if mode & _SYM_FILL_RETURN:
        return int(mt5.ORDER_FILLING_RETURN)
    return int(mt5.ORDER_FILLING_RETURN)


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


def _suggest_symbol_names(mt5: Any, wanted: str) -> str:
    """Một vài tên symbol trên server gần với ``wanted`` (ví dụ XAUUSD → XAUUSDm)."""
    try:
        raw = mt5.symbols_get()
        if not raw:
            return "symbols_get() rỗng."
        names = [s.name for s in raw if hasattr(s, "name")]
        wu = wanted.upper()
        # Ưu tiên: chứa đủ chuỗi, hoặc cùng prefix 6 ký tự (XAUUSD / XAUUSDm)
        pref = wu[:6] if len(wu) >= 6 else wu
        hit = [n for n in names if wu in n.upper() or (pref and pref in n.upper())]
        hit = sorted(set(hit))[:25]
        if hit:
            return "symbol trên server gần giống: " + ", ".join(hit)
        return f"không lọc được tên gần {wanted!r} (tổng {len(names)} symbol)."
    except Exception as exc:  # noqa: BLE001
        return f"symbols_get lỗi: {exc}"


def _is_call_failed(last_err: Optional[tuple[Any, ...]]) -> bool:
    if not last_err or len(last_err) < 2:
        return False
    code, msg = last_err[0], last_err[1]
    try:
        ic = int(code)
    except (TypeError, ValueError):
        ic = code
    if ic == -1:
        return True
    m = str(msg).lower()
    return "call failed" in m or "terminal" in m


def _symbol_candidates(symbol: str) -> list[str]:
    """
    Candidate symbol names to try on broker.

    Primary use-case: gold symbol differences across brokers, e.g. ``XAUUSD`` vs ``XAUUSDm``.
    We keep the input first, then try common single-letter suffix toggle.
    """
    s = (symbol or "").strip()
    if not s:
        return []
    out: list[str] = [s]
    up = s.upper()
    # Common broker suffix: "m"
    if up.endswith("M") and len(s) > 1:
        out.append(s[:-1])
    else:
        out.append(s + "m")
    # De-dup while preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for c in out:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def _ensure_symbol(mt5: Any, symbol: str) -> tuple[Optional[str], Optional[str]]:
    ti = mt5.terminal_info()
    if ti is None:
        return (
            None,
            "terminal_info() trả về None — Python không nối được terminal MT5. "
            f"{format_last_error(mt5)}",
        )
    if getattr(ti, "connected", True) is False:
        return (
            None,
            "terminal connected=False — chưa nối server trong MT5 (kiểm tra đăng nhập / mạng). "
            f"{format_last_error(mt5)}",
        )

    for cand in _symbol_candidates(symbol):
        info = mt5.symbol_info(cand)
        if info is None:
            continue
        if not mt5.symbol_select(cand, True):
            le = _last_error_tuple(mt5)
            base = f"symbol_select({cand!r}) thất bại. {format_last_error(mt5)}"
            if _is_call_failed(le):
                base += (
                    " | Gợi ý lỗi IPC (code=-1 / Terminal: Call failed): "
                    "để MT5 đang mở và đã login; bật AutoTrading; chạy Python cùng user/session với terminal; "
                    "thử chạy terminal không portable; tắt chặn từ antivirus/RDP."
                )
            else:
                base += " | Thử thêm symbol vào Market Watch thủ công trong MT5."
            return None, base
        return cand, None

    # None of the candidates exists on broker.
    wanted = symbol
    sug = _suggest_symbol_names(mt5, wanted)
    tail = _format_last_error_if_meaningful(mt5)
    tried = ", ".join(repr(s) for s in _symbol_candidates(wanted))
    return (
        None,
        f"symbol_info({wanted!r}) không tồn tại trên broker — không thể giao dịch tên này. "
        f"Đã thử: {tried}. "
        f"Đối chiếu tên trong Market Watch; có thể ``--symbol <tên đúng>``. "
        f"{sug}{tail}",
    )


def resolve_trade_symbol_on_broker(
    mt5: Any,
    trade: ParsedTrade,
    symbol_override: Optional[str],
) -> tuple[Optional[ParsedTrade], Optional[str]]:
    """
    Chuẩn hóa symbol (override + XAUUSDm) và ``symbol_select`` trên broker.
    Dùng trước khi tính lot theo ``contract_size`` (multi-account).
    """
    t = resolve_mt5_trade_symbol(trade, symbol_override)
    sym, err = _ensure_symbol(mt5, t.symbol)
    if err or not sym:
        return None, err or "Không resolve được symbol."
    return replace(t, symbol=sym), None


def build_request(
    mt5: Any,
    trade: ParsedTrade,
    *,
    deviation: int = 20,
    magic: int = 2222222,
    comment: str = "openai-auto",
) -> dict[str, Any]:
    sym, err = _ensure_symbol(mt5, trade.symbol)
    if err or not sym:
        raise RuntimeError(err or "Không resolve được symbol để trade.")

    filling = _filling_for_symbol(mt5, sym)
    if trade.kind == "MARKET":
        tick = mt5.symbol_info_tick(sym)
        if tick is None:
            raise RuntimeError(
                f"symbol_info_tick({sym!r}) trả về None. {format_last_error(mt5)}",
            )
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


def _normalize_symbol_str(val: Optional[str]) -> str:
    if not val:
        return ""
    return str(val).strip().strip('"').strip("'")


def resolve_mt5_trade_symbol(trade: ParsedTrade, symbol_override: Optional[str]) -> ParsedTrade:
    """
    ``--symbol`` CLI (nếu có) thay symbol đã parse; sau đó ``XAUUSD`` → ``XAUUSDm`` (broker).
    """
    ovr = _normalize_symbol_str(symbol_override)
    base = ovr if ovr else trade.symbol
    sym = normalize_broker_xau_symbol(base)
    return replace(trade, symbol=sym)


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
    symbol_override: Optional[str] = None,
    lot_override: Optional[float] = None,
    account_id: Optional[str] = None,
) -> MT5ExecutionResult:
    """
    Gửi lệnh qua MetaTrader5. MT5 chỉ có một TP trên lệnh; TP2 được in ra nếu có.

    Credentials: env ``MT5_LOGIN``, ``MT5_PASSWORD``, ``MT5_SERVER`` hoặc tham số.

    Symbol: ``symbol_override`` (CLI ``--symbol``) nếu có, không thì symbol đã parse; luôn chuẩn hóa ``XAUUSD`` → ``XAUUSDm``.
    Lot: ``lot_override`` ghi đè volume từ file (tiện test với lot nhỏ).

    ``account_id``: nhãn đa tài khoản (log/Telegram).
    """
    trade = resolve_mt5_trade_symbol(trade, symbol_override)
    if lot_override is not None:
        trade = replace(trade, lot=float(lot_override))

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
            account_id=account_id,
            request=req_preview,
            resolved_symbol=trade.symbol,
        )

    mt5 = _load_mt5()
    kwargs: dict[str, Any] = {}
    if login_i and password_s and server_s:
        kwargs["login"] = login_i
        kwargs["password"] = password_s
        kwargs["server"] = server_s

    if not mt5.initialize(**kwargs):
        le = _last_error_tuple(mt5)
        return MT5ExecutionResult(
            ok=False,
            message=f"mt5.initialize thất bại: {format_last_error(mt5)}",
            account_id=account_id,
            last_error=le,
            resolved_symbol=trade.symbol,
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
            le = _last_error_tuple(mt5)
            return MT5ExecutionResult(
                ok=False,
                message=f"{e}",
                account_id=account_id,
                last_error=le,
                resolved_symbol=trade.symbol,
            )

        chk = mt5.order_check(request)
        if chk is None:
            le = _last_error_tuple(mt5)
            return MT5ExecutionResult(
                ok=False,
                message=f"order_check trả về None. {format_last_error(mt5)}",
                account_id=account_id,
                last_error=le,
                request=request,
                resolved_symbol=str(request.get("symbol") or trade.symbol),
            )
        chk_rc = getattr(chk, "retcode", None)
        chk_d = _trade_check_dict(chk)
        if chk_rc is not None and not _is_mt5_trade_success_retcode(mt5, chk_rc):
            le = _last_error_tuple(mt5)
            hint = _trade_retcode_hint(mt5, chk_rc)
            return MT5ExecutionResult(
                ok=False,
                message=(
                    f"order_check không đạt: retcode={_retcode_label(mt5, chk_rc)} "
                    f"trade_check={chk_d!r} {format_last_error(mt5)}{hint}"
                ),
                account_id=account_id,
                retcode=int(chk_rc) if chk_rc is not None else None,
                last_error=le,
                trade_check=chk_d,
                request=request,
                resolved_symbol=str(request.get("symbol") or trade.symbol),
            )
        ret = mt5.order_send(request)
        if ret is None:
            le = _last_error_tuple(mt5)
            return MT5ExecutionResult(
                ok=False,
                message=f"order_send trả về None. {format_last_error(mt5)}",
                account_id=account_id,
                last_error=le,
                request=request,
                resolved_symbol=str(request.get("symbol") or trade.symbol),
            )
        rc = getattr(ret, "retcode", None)
        rd = _trade_result_dict(ret)
        if not _is_mt5_trade_success_retcode(mt5, rc):
            le = _last_error_tuple(mt5)
            hint = _trade_retcode_hint(mt5, rc)
            return MT5ExecutionResult(
                ok=False,
                message=(
                    f"order_send thất bại: retcode={_retcode_label(mt5, rc)} "
                    f"trade_result={rd!r} {format_last_error(mt5)}{hint}{extra}"
                ),
                account_id=account_id,
                retcode=int(rc) if rc is not None else None,
                last_error=le,
                trade_result=rd,
                request=request,
                resolved_symbol=str(request.get("symbol") or trade.symbol),
            )
        rc_int = int(rc) if rc is not None else None
        return MT5ExecutionResult(
            ok=True,
            message=(
                f"OK: {_retcode_label(mt5, rc)} order={getattr(ret, 'order', None)} "
                f"deal={getattr(ret, 'deal', None)} trade_result={rd!r}{extra}"
            ),
            account_id=account_id,
            retcode=rc_int,
            order=getattr(ret, "order", None),
            deal=getattr(ret, "deal", None),
            request=request,
            trade_result=rd,
            resolved_symbol=str(request.get("symbol") or trade.symbol),
        )
    finally:
        mt5.shutdown()
