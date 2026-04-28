"""
Thực thi lệnh qua MetaTrader5 (Python package ``MetaTrader5``).

**Mac (dev):** gói thường không cài được; dùng ``mt5-trade --dry-run`` hoặc ``--mt5-dry-run`` trên CLI tự động (mô phỏng).

**Windows VPS (prod):** cài MT5 terminal, đăng nhập sẵn tài khoản, giữ terminal mở; mặc định gửi lệnh thật. Nếu không đặt ``MT5_LOGIN`` / ``MT5_PASSWORD`` / ``MT5_SERVER``, gọi
``initialize()`` không đối số — bám phiên đăng nhập hiện có trong terminal (đúng với flow
“VPS đã cài và đăng nhập sẵn MetaTrader5”).
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Sequence

import automation_tool.config  # noqa: F401 — nạp .env khi import

from automation_tool.mt5_openai_parse import ParsedTrade, normalize_broker_xau_symbol

# Vào lệnh: nếu mt5.initialize (login/IPC) lỗi — chờ rồi thử lại trước khi bỏ cuộc.
_MT5_INIT_MAX_ATTEMPTS = 3
_MT5_INIT_RETRY_DELAY_SEC = 3.0
_MT5_SESSION_SWITCH_DELAYS_SEC = (2, 4, 6, 8)
_MT5_ORDER_SEND_MAX_ATTEMPTS = 3
_MT5_ORDER_SEND_RETRY_DELAY_MS = 200


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
    """Chuỗi plain text cho log Telegram sau ``execute_trade`` (routing chat do caller quyết định)."""
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


@dataclass
class MT5SessionResult:
    """Kết quả đảm bảo MT5 đang trỏ đúng terminal/account cần dùng."""

    ok: bool
    mt5: Any
    message: str
    reused: bool = False
    initialized: bool = False
    terminal_info: Any = None
    account_info: Any = None
    last_error: Optional[tuple[Any, ...]] = None


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

    term_path = (os.getenv("MT5_TERMINAL_PATH") or "").strip()
    if term_path and login_i and password_s and server_s:
        session = ensure_mt5_session(
            terminal_path=term_path,
            login=login_i,
            password=password_s,
            server=server_s,
            mt5=mt5,
        )
        lines.append(f"terminal_path: {term_path}")
        lines.append(session.message)
        ti = session.terminal_info
        ai = session.account_info
        if not session.ok:
            lines.append(f"mt5.initialize/verify thất bại: {format_last_error(mt5)}")
            return MT5LoginResult(ok=False, lines=lines)
        if ti is not None:
            lines.append(
                f"Terminal: build={getattr(ti, 'build', '?')} "
                f"name={getattr(ti, 'name', '?')!r}",
            )
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

    ti_current, ai_current = _read_mt5_session_info(mt5)
    if ai_current is not None and (
        not login_i
        or _mt5_account_matches(ai_current, login_i, server_s)
    ):
        lines.append("MT5 session hiện tại có account_info phù hợp — reuse.")
        if ti_current is not None:
            lines.append(
                f"Terminal: build={getattr(ti_current, 'build', '?')} "
                f"name={getattr(ti_current, 'name', '?')!r}",
            )
        lines.append("Đăng nhập OK — account_info:")
        lines.append(f"  login:    {ai_current.login}")
        lines.append(f"  server:   {ai_current.server}")
        lines.append(f"  company:  {getattr(ai_current, 'company', '')}")
        lines.append(f"  name:     {getattr(ai_current, 'name', '')}")
        lines.append(f"  currency: {ai_current.currency}")
        lines.append(f"  balance:  {ai_current.balance}")
        lines.append(f"  equity:   {ai_current.equity}")
        lines.append(f"  margin:   {getattr(ai_current, 'margin', '')}")
        lev = getattr(ai_current, "leverage", None)
        if lev is not None:
            lines.append(f"  leverage: 1:{lev}")
        lines.append(f"  trade_allowed: {getattr(ai_current, 'trade_allowed', '?')}")
        return MT5LoginResult(ok=True, lines=lines)

    if not mt5.initialize(**kwargs):
        lines.append(f"mt5.initialize thất bại: {format_last_error(mt5)}")
        return MT5LoginResult(ok=False, lines=lines)

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


def _norm_mt5_path(value: Any) -> str:
    s = str(value or "").strip().strip('"').strip("'")
    if not s:
        return ""
    return os.path.normcase(os.path.normpath(os.path.expanduser(s)))


def _mt5_terminal_path_matches(terminal_info: Any, terminal_path: str) -> bool:
    required = _norm_mt5_path(terminal_path)
    if not required:
        return True
    if terminal_info is None:
        return False
    required_dir = _norm_mt5_path(os.path.dirname(required))
    candidates = {
        _norm_mt5_path(getattr(terminal_info, "path", "")),
        _norm_mt5_path(getattr(terminal_info, "data_path", "")),
        _norm_mt5_path(getattr(terminal_info, "commondata_path", "")),
    }
    candidates.discard("")
    if required in candidates or required_dir in candidates:
        return True
    return any(_norm_mt5_path(os.path.dirname(c)) == required_dir for c in candidates)


def _mt5_account_matches(account_info: Any, login: Optional[int], server: Optional[str]) -> bool:
    if login is None and not server:
        return account_info is not None
    if account_info is None:
        return False
    if login is not None:
        try:
            if int(getattr(account_info, "login")) != int(login):
                return False
        except (TypeError, ValueError):
            return False
    if server:
        current_server = str(getattr(account_info, "server", "") or "").strip().lower()
        required_server = str(server or "").strip().lower()
        if current_server != required_server:
            return False
    return True


def _read_mt5_session_info(mt5: Any) -> tuple[Any, Any]:
    try:
        terminal_info = mt5.terminal_info()
    except Exception:
        terminal_info = None
    try:
        account_info = mt5.account_info()
    except Exception:
        account_info = None
    return terminal_info, account_info


def _mt5_session_matches(
    mt5: Any,
    *,
    terminal_path: str,
    login: Optional[int],
    server: Optional[str],
) -> tuple[bool, Any, Any]:
    terminal_info, account_info = _read_mt5_session_info(mt5)
    return (
        _mt5_terminal_path_matches(terminal_info, terminal_path)
        and _mt5_account_matches(account_info, login, server),
        terminal_info,
        account_info,
    )


def ensure_mt5_session(
    *,
    terminal_path: Optional[str],
    login: Optional[int] = None,
    password: Optional[str] = None,
    server: Optional[str] = None,
    mt5: Any = None,
    max_attempts: int = _MT5_INIT_MAX_ATTEMPTS,
    sleep_fn: Callable[[float], None] = time.sleep,
    delay_choice_fn: Callable[[Sequence[int]], int] = random.choice,
) -> MT5SessionResult:
    """
    Reuse phiên MT5 hiện tại nếu đã đúng terminal/account; chỉ switch khi thiếu/sai.

    Password không có trong ``account_info()`` nên chỉ dùng khi cần initialize lại.
    """
    mt5_mod = mt5 if mt5 is not None else _load_mt5()
    term_path = (terminal_path or "").strip()
    if not term_path:
        return MT5SessionResult(
            ok=False,
            mt5=mt5_mod,
            message="terminal_path bắt buộc để kết nối MT5 terminal (metatrader64.exe).",
            last_error=_last_error_tuple(mt5_mod),
        )

    login_i = int(login) if login is not None else None
    server_s = str(server or "").strip()
    password_s = str(password or "")
    matched, terminal_info, account_info = _mt5_session_matches(
        mt5_mod,
        terminal_path=term_path,
        login=login_i,
        server=server_s,
    )
    if matched:
        return MT5SessionResult(
            ok=True,
            mt5=mt5_mod,
            message="MT5 session hiện tại đúng terminal/account — reuse.",
            reused=True,
            terminal_info=terminal_info,
            account_info=account_info,
            last_error=_last_error_tuple(mt5_mod),
        )

    kwargs: dict[str, Any] = {}
    if login_i is not None and password_s and server_s:
        kwargs["login"] = login_i
        kwargs["password"] = password_s
        kwargs["server"] = server_s

    last_error: Optional[tuple[Any, ...]] = None
    attempts = max(1, int(max_attempts or 1))
    for attempt in range(1, attempts + 1):
        delay = delay_choice_fn(_MT5_SESSION_SWITCH_DELAYS_SEC)
        sleep_fn(float(delay))
        try:
            mt5_mod.shutdown()
        except Exception:
            pass

        if not mt5_mod.initialize(term_path, **kwargs):
            last_error = _last_error_tuple(mt5_mod)
            continue

        matched, terminal_info, account_info = _mt5_session_matches(
            mt5_mod,
            terminal_path=term_path,
            login=login_i,
            server=server_s,
        )
        if matched:
            return MT5SessionResult(
                ok=True,
                mt5=mt5_mod,
                message="MT5 initialize OK và verify đúng terminal/account.",
                initialized=True,
                terminal_info=terminal_info,
                account_info=account_info,
                last_error=_last_error_tuple(mt5_mod),
            )
        last_error = _last_error_tuple(mt5_mod)

    return MT5SessionResult(
        ok=False,
        mt5=mt5_mod,
        message=(
            f"mt5.initialize/verify thất bại sau {attempts} lần: "
            f"{format_last_error(mt5_mod)}"
        ),
        terminal_info=terminal_info,
        account_info=account_info,
        last_error=last_error,
    )


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
    rq = getattr(mt5, "TRADE_RETCODE_REQUOTE", None)
    pc = getattr(mt5, "TRADE_RETCODE_PRICE_CHANGED", None)
    po = getattr(mt5, "TRADE_RETCODE_PRICE_OFF", None)
    if (
        (rq is not None and rc == int(rq))
        or (pc is not None and rc == int(pc))
        or (po is not None and rc == int(po))
        or rc in (10004, 10020, 10021)
    ):
        return (
            " | Gợi ý: giá biến động nhanh (requote/price changed). "
            "Tăng deviation hoặc bật retry order_send với giá tick mới."
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


def symbol_uses_market_execution(mt5: Any, symbol: str) -> bool:
    """
    ``True`` khi symbol dùng ``SYMBOL_TRADE_EXECUTION_MARKET``.

    Theo MQL5, với ``TRADE_ACTION_DEAL`` trên kiểu này **không** đặt ``price`` trong
    request — broker khớp theo thị trường hiện tại.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        return False
    raw = getattr(info, "trade_exemode", None)
    if raw is None:
        return False
    try:
        exe = int(raw)
    except (TypeError, ValueError):
        return False
    market = int(getattr(mt5, "SYMBOL_TRADE_EXECUTION_MARKET", 2))
    return exe == market


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


def _is_retryable_order_send_retcode(mt5: Any, retcode: Optional[int]) -> bool:
    if retcode is None:
        return False
    try:
        rc = int(retcode)
    except (TypeError, ValueError):
        return False
    rq = getattr(mt5, "TRADE_RETCODE_REQUOTE", None)
    pc = getattr(mt5, "TRADE_RETCODE_PRICE_CHANGED", None)
    po = getattr(mt5, "TRADE_RETCODE_PRICE_OFF", None)
    if rq is not None and rc == int(rq):
        return True
    if pc is not None and rc == int(pc):
        return True
    if po is not None and rc == int(po):
        return True
    return rc in (10004, 10020, 10021)


def _refresh_market_order_price(mt5: Any, request: dict[str, Any]) -> tuple[bool, str]:
    """
    Với lệnh market (DEAL), cập nhật ``request['price']`` theo tick mới nhất
    (chỉ khi broker không dùng ``SYMBOL_TRADE_EXECUTION_MARKET`` — khi đó không đặt price).

    Trả về (cho phép tiếp tục retry hay không, ghi chú).
    """
    action = request.get("action")
    if action != mt5.TRADE_ACTION_DEAL:
        return False, "not DEAL action"
    symbol = str(request.get("symbol") or "").strip()
    if not symbol:
        return False, "missing symbol in request"
    if symbol_uses_market_execution(mt5, symbol):
        return True, "SYMBOL_TRADE_EXECUTION_MARKET: không đặt price (retry không đổi request)"
    order_type = request.get("type")
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False, f"symbol_info_tick({symbol!r}) trả về None"
    if order_type == mt5.ORDER_TYPE_BUY:
        new_price = getattr(tick, "ask", None)
        leg = "ask"
    elif order_type == mt5.ORDER_TYPE_SELL:
        new_price = getattr(tick, "bid", None)
        leg = "bid"
    else:
        return False, f"not market BUY/SELL type={order_type!r}"
    try:
        p = float(new_price)
    except (TypeError, ValueError):
        return False, f"tick.{leg} không hợp lệ: {new_price!r}"
    if p <= 0.0:
        return False, f"tick.{leg} <= 0: {p}"
    old = request.get("price")
    request["price"] = p
    return True, f"refresh price {old!r} -> {p!r} ({leg})"


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
    *,
    account_symbol_map: Optional[dict[str, str]] = None,
) -> tuple[Optional[ParsedTrade], Optional[str]]:
    """
    Chuẩn hóa symbol (override + ``symbol_map`` từng acc hoặc XAUUSD→XAUUSDm) và ``symbol_select`` trên broker.
    Dùng trước khi tính lot theo ``contract_size`` (multi-account).
    """
    t = resolve_mt5_trade_symbol(
        trade,
        symbol_override,
        account_symbol_map=account_symbol_map,
    )
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
    # MT5 chỉ có 1 TP trên 1 lệnh. Nếu có TP2 thì đặt TP=TP2 ngay khi vào lệnh.
    tp_effective = trade.tp2 if trade.tp2 is not None else trade.tp1
    if trade.kind == "MARKET":
        omit_price = symbol_uses_market_execution(mt5, sym)
        price: Optional[float] = None
        if not omit_price:
            tick = mt5.symbol_info_tick(sym)
            if tick is None:
                raise RuntimeError(
                    f"symbol_info_tick({sym!r}) trả về None. {format_last_error(mt5)}",
                )
            price = float(tick.ask if trade.side == "BUY" else tick.bid)
        otype = mt5.ORDER_TYPE_BUY if trade.side == "BUY" else mt5.ORDER_TYPE_SELL
        req: dict[str, Any] = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym,
            "volume": trade.lot,
            "type": otype,
            "sl": trade.sl,
            "tp": tp_effective,
            "deviation": deviation,
            "magic": magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        if not omit_price and price is not None:
            req["price"] = price
        return req

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
        "tp": tp_effective,
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


def _mt5_initialize_kwargs_from_env() -> dict[str, Any]:
    """
    Tham số ``initialize`` khi có đủ ``MT5_LOGIN`` / ``MT5_PASSWORD`` / ``MT5_SERVER``.
    Rỗng nếu thiếu — dùng phiên terminal đang mở.
    """
    login_i = _env_int("MT5_LOGIN", 0)
    password_s = os.getenv("MT5_PASSWORD") or ""
    server_s = os.getenv("MT5_SERVER") or ""
    if login_i and password_s and server_s:
        return {"login": login_i, "password": password_s, "server": server_s}
    return {}


def _normalize_symbol_str(val: Optional[str]) -> str:
    if not val:
        return ""
    return str(val).strip().strip('"').strip("'")


def resolve_mt5_trade_symbol(
    trade: ParsedTrade,
    symbol_override: Optional[str],
    *,
    account_symbol_map: Optional[dict[str, str]] = None,
) -> ParsedTrade:
    """
    ``--mt5-symbol`` / ``--symbol`` (nếu có) thay symbol đã parse.

    Nếu ``account_symbol_map`` có entry cho symbol logic (uppercase), dùng đúng tên broker đó.
    Nếu không: giữ hành vi cũ ``XAUUSD`` → ``XAUUSDm`` (``normalize_broker_xau_symbol``).
    """
    ovr = _normalize_symbol_str(symbol_override)
    base_raw = ovr if ovr else trade.symbol
    base = (base_raw or "").strip()
    key = base.upper()
    if account_symbol_map and key in account_symbol_map:
        sym = account_symbol_map[key]
    else:
        sym = normalize_broker_xau_symbol(base)
    return replace(trade, symbol=sym)


def execute_trade(
    trade: ParsedTrade,
    *,
    terminal_path: Optional[str] = None,
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
    account_symbol_map: Optional[dict[str, str]] = None,
    order_send_max_attempts: Optional[int] = None,
    order_send_retry_delay_ms: Optional[int] = None,
) -> MT5ExecutionResult:
    """
    Gửi lệnh qua MetaTrader5. MT5 chỉ có một TP trên lệnh; nếu có TP2 thì đặt TP=TP2.

    Credentials: env ``MT5_LOGIN``, ``MT5_PASSWORD``, ``MT5_SERVER`` hoặc tham số.

    Symbol: ``symbol_override`` (CLI) nếu có, không thì symbol đã parse; sau đó
    ``account_symbol_map`` (từ ``accounts.json``) hoặc mặc định ``XAUUSD`` → ``XAUUSDm``.
    Lot: ``lot_override`` ghi đè volume từ file (tiện test với lot nhỏ).

    ``account_id``: nhãn đa tài khoản (log/Telegram).

    Khởi tạo MT5: tối đa :data:`_MT5_INIT_MAX_ATTEMPTS` lần; giữa các lần thất bại chờ
    :data:`_MT5_INIT_RETRY_DELAY_SEC` giây (xử lý IPC timeout tạm thời).
    """
    trade = resolve_mt5_trade_symbol(
        trade,
        symbol_override,
        account_symbol_map=account_symbol_map,
    )
    if lot_override is not None:
        trade = replace(trade, lot=float(lot_override))

    login_i = login if login is not None else _env_int("MT5_LOGIN", 0)
    password_s = password if password is not None else (os.getenv("MT5_PASSWORD") or "")
    server_s = server if server is not None else (os.getenv("MT5_SERVER") or "")
    mag = magic if magic is not None else _env_int("MT5_MAGIC", 2222222)
    send_max_attempts_raw = (
        int(order_send_max_attempts)
        if order_send_max_attempts is not None
        else _env_int("MT5_ORDER_SEND_MAX_ATTEMPTS", _MT5_ORDER_SEND_MAX_ATTEMPTS)
    )
    send_max_attempts = max(1, send_max_attempts_raw)
    send_retry_delay_raw = (
        int(order_send_retry_delay_ms)
        if order_send_retry_delay_ms is not None
        else _env_int("MT5_ORDER_SEND_RETRY_DELAY_MS", _MT5_ORDER_SEND_RETRY_DELAY_MS)
    )
    send_retry_delay_sec = max(0.0, float(send_retry_delay_raw) / 1000.0)

    extra = ""
    if trade.tp2 is not None and log_tp2:
        extra = f" (TP2={trade.tp2} — sẽ đặt TP2 trên lệnh MT5)"

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

    session = ensure_mt5_session(
        terminal_path=terminal_path,
        login=login_i if login_i else None,
        password=password_s,
        server=server_s,
    )
    mt5 = session.mt5
    if not session.ok:
        return MT5ExecutionResult(
            ok=False,
            message=session.message,
            account_id=account_id,
            last_error=session.last_error,
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
        send_attempt = 1
        send_notes: list[str] = []
        ret = None
        while True:
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
            rc_try = getattr(ret, "retcode", None)
            if _is_mt5_trade_success_retcode(mt5, rc_try):
                break
            if (
                send_attempt >= send_max_attempts
                or not _is_retryable_order_send_retcode(mt5, rc_try)
            ):
                break
            refreshed, note = _refresh_market_order_price(mt5, request)
            if not refreshed:
                send_notes.append(
                    f"retry stop at attempt={send_attempt}: {note}",
                )
                break
            send_notes.append(
                f"retry {send_attempt + 1}/{send_max_attempts} after {_retcode_label(mt5, rc_try)}; {note}",
            )
            send_attempt += 1
            if send_retry_delay_sec > 0.0:
                time.sleep(send_retry_delay_sec)
        rc = getattr(ret, "retcode", None)
        rd = _trade_result_dict(ret)
        if not _is_mt5_trade_success_retcode(mt5, rc):
            le = _last_error_tuple(mt5)
            hint = _trade_retcode_hint(mt5, rc)
            retry_txt = f" retries={send_attempt}/{send_max_attempts}; notes={send_notes}" if send_attempt > 1 or send_notes else ""
            return MT5ExecutionResult(
                ok=False,
                message=(
                    f"order_send thất bại: retcode={_retcode_label(mt5, rc)} "
                    f"trade_result={rd!r}{retry_txt} {format_last_error(mt5)}{hint}{extra}"
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
                f"deal={getattr(ret, 'deal', None)} trade_result={rd!r}"
                f"{' retries=' + str(send_attempt) if send_attempt > 1 else ''}{extra}"
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
        pass


def _tick_price_or_none(tick: Any, leg: Literal["ask", "bid"]) -> Optional[float]:
    """Giá > 0 từ tick; ``None`` nếu thuộc tính thiếu, không hợp lệ hoặc ≤ 0."""
    raw = getattr(tick, leg, None)
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0.0 else None


def bid_price_from_tick(tick: Any) -> float:
    """
    Giá bid từ tick; nếu bid thiếu hoặc ≤0 thì fallback sang ask (cùng logic một phía “bán”).
    Dùng cho daemon giá (shared Last) và đồng bộ với đọc một mức thị trường.
    """
    ask = _tick_price_or_none(tick, "ask")
    bid = _tick_price_or_none(tick, "bid")
    if bid is not None:
        return bid
    if ask is not None:
        return ask
    return float(getattr(tick, "bid"))


def execution_price_from_tick(tick: Any, side: Literal["BUY", "SELL"]) -> float:
    """
    Giá thực thi một phía: BUY → ``ask`` (mua); SELL → ``bid`` (bán).
    Nếu phía ưu tiên thiếu hoặc ≤ 0, fallback sang phía kia (BUY → bid; SELL → ask).
    Khớp cách đặt giá market trong :func:`build_request`.
    """
    ask = _tick_price_or_none(tick, "ask")
    bid = _tick_price_or_none(tick, "bid")
    if side == "BUY":
        if ask is not None:
            return ask
        if bid is not None:
            return bid
        return float(getattr(tick, "ask"))
    if bid is not None:
        return bid
    if ask is not None:
        return ask
    return float(getattr(tick, "bid"))


class DaemonPlanMt5PriceSession:
    """
    Một phiên ``initialize`` + ``symbol_select``; gọi ``symbol_info_tick`` (daemon giá: :meth:`read_bid_price`).

    **Không** truyền ``login``/``password``/``server`` vào ``initialize``: chỉ đọc giá theo symbol,
    bám phiên MT5 đang mở (``initialize()`` không đối số). Tránh đăng nhập lại account primary
    và làm đổi phiên khi luồng khác đang gửi lệnh multi-account (account phụ).

    :meth:`reconnect` (dùng khi giá bid “đứng” quá lâu) gọi ``shutdown`` rồi ``initialize`` lại;
    nếu env có đủ ``MT5_*`` thì ưu tiên đăng nhập qua API, không thì bám terminal như lần đầu.

    ``daemon-plan`` không dùng class này nữa — đọc Last đã ghi shared memory; giữ :meth:`read_execution_price`
    cho tương thích / test.
    """

    def __init__(
        self,
        *,
        symbol_hint: str,
        symbol_override: Optional[str],
        dry_run: bool,
    ) -> None:
        ovr = _normalize_symbol_str(symbol_override)
        base = ovr if ovr else (symbol_hint or "").strip()
        self._symbol_hint = normalize_broker_xau_symbol(base or "XAUUSD")
        self._dry_run = bool(dry_run)
        self._mt5: Any = None
        self._resolved: Optional[str] = None
        self._last_error: Optional[str] = None

    def _build_init_kwargs(self) -> dict[str, Any]:
        # Giá bid/ask theo symbol không phụ thuộc account đang active; không ép login.
        return {}

    def _connect_with_init_kwargs(self, kwargs: dict[str, Any]) -> bool:
        if self._dry_run:
            self._last_error = "mt5_dry_run"
            return False
        try:
            mt5 = _load_mt5()
        except SystemExit as e:
            self._last_error = str(e)
            return False
        ti, ai = _read_mt5_session_info(mt5)
        if ti is not None and ai is not None:
            sym, err = _ensure_symbol(mt5, self._symbol_hint)
            if err or not sym:
                self._last_error = err or f"symbol_info({self._symbol_hint!r})"
                return False
            self._mt5 = mt5
            self._resolved = sym
            self._last_error = None
            return True
        if not mt5.initialize(**kwargs):
            self._last_error = f"mt5.initialize thất bại: {format_last_error(mt5)}"
            return False
        sym, err = _ensure_symbol(mt5, self._symbol_hint)
        if err or not sym:
            self._last_error = err or f"symbol_info({self._symbol_hint!r})"
            return False
        self._mt5 = mt5
        self._resolved = sym
        self._last_error = None
        return True

    def _ensure_connected(self) -> bool:
        if self._mt5 is not None:
            return True
        return self._connect_with_init_kwargs(self._build_init_kwargs())

    def reconnect(self) -> bool:
        """
        ``shutdown`` Python session rồi kết nối lại MT5 (đăng nhập API nếu có đủ env, không thì phiên terminal).
        Dùng khi bid không đổi trong khoảng thời gian dài (feed có thể kẹt).
        """
        time.sleep(float(random.choice(_MT5_SESSION_SWITCH_DELAYS_SEC)))
        self.shutdown()
        kwargs = _mt5_initialize_kwargs_from_env()
        return self._connect_with_init_kwargs(kwargs)

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def read_execution_price(self, side: Literal["BUY", "SELL"]) -> tuple[Optional[float], Optional[str]]:
        """
        Trả về ``(giá, lỗi)``. Giá là ask (BUY) hoặc bid (SELL).
        """
        if self._mt5 is None:
            if not self._ensure_connected():
                return None, self._last_error
        assert self._mt5 is not None and self._resolved is not None
        tick = self._mt5.symbol_info_tick(self._resolved)
        if tick is None:
            err = f"symbol_info_tick({self._resolved!r}) None. {format_last_error(self._mt5)}"
            return None, err
        return execution_price_from_tick(tick, side), None

    def read_bid_price(self) -> tuple[Optional[float], Optional[str]]:
        """
        Chỉ bid (và fallback ask nếu bid không hợp lệ) — dùng cho daemon giá ghi shared memory.
        """
        if self._mt5 is None:
            if not self._ensure_connected():
                return None, self._last_error
        assert self._mt5 is not None and self._resolved is not None
        tick = self._mt5.symbol_info_tick(self._resolved)
        if tick is None:
            err = f"symbol_info_tick({self._resolved!r}) None. {format_last_error(self._mt5)}"
            return None, err
        return bid_price_from_tick(tick), None

    def shutdown(self) -> None:
        if self._mt5 is not None:
            try:
                self._mt5.shutdown()
            except Exception:
                pass
            self._mt5 = None
            self._resolved = None

    @property
    def resolved_symbol(self) -> Optional[str]:
        return self._resolved
