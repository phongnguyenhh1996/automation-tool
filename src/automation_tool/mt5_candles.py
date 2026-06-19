from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from automation_tool.mt5_accounts import (
    MT5AccountEntry,
    load_mt5_accounts_for_cli,
    primary_account,
)
from automation_tool.mt5_execute import ensure_mt5_session
from automation_tool.mt5_openai_parse import normalize_broker_xau_symbol
from automation_tool.mt5_manage import _load_mt5, _mt5_init_current_terminal

_log = logging.getLogger(__name__)

DEFAULT_MT5_SPOT_CANDLES_COUNT = 50
DEFAULT_MT5_SPOT_CANDLES_INTERVAL = "5m"
MT5_CANDLES_TIMEZONE = "Asia/Ho_Chi_Minh"
_VN_TZ = ZoneInfo(MT5_CANDLES_TIMEZONE)


def mt5_spot_candles_count() -> int:
    raw = os.getenv("MT5_SPOT_CANDLES_COUNT", "").strip()
    if raw.isdigit():
        return max(1, int(raw))
    return DEFAULT_MT5_SPOT_CANDLES_COUNT


def mt5_spot_candles_interval() -> str:
    raw = (os.getenv("MT5_SPOT_CANDLES_INTERVAL") or DEFAULT_MT5_SPOT_CANDLES_INTERVAL).strip().lower()
    return raw or DEFAULT_MT5_SPOT_CANDLES_INTERVAL


def mt5_spot_candles_json_stem(stamp: str, logic_symbol: str, interval: str) -> str:
    sym = (logic_symbol or "XAUUSD").strip().upper()
    iv_slug = (interval or "5m").strip().lower().replace(" ", "")
    return f"{stamp}_mt5_{sym}_{iv_slug}"


def mt5_spot_candles_json_path(
    charts_dir: Path,
    *,
    logic_symbol: str = "XAUUSD",
    interval: str | None = None,
    stamp: str | None = None,
) -> Path:
    from automation_tool.images import latest_chart_stamp

    st = stamp or latest_chart_stamp(charts_dir) or ""
    iv = interval or mt5_spot_candles_interval()
    return charts_dir / f"{mt5_spot_candles_json_stem(st, logic_symbol, iv)}.json"


def _interval_to_mt5_timeframe(mt5: Any, interval: str) -> Optional[int]:
    iv = (interval or "").strip().lower().replace(" ", "")
    mapping = {
        "1m": "TIMEFRAME_M1",
        "5m": "TIMEFRAME_M5",
        "15m": "TIMEFRAME_M15",
        "30m": "TIMEFRAME_M30",
        "1h": "TIMEFRAME_H1",
        "4h": "TIMEFRAME_H4",
        "1d": "TIMEFRAME_D1",
    }
    name = mapping.get(iv)
    if not name:
        return None
    tf = getattr(mt5, name, None)
    if tf is None:
        return None
    try:
        return int(tf)
    except (TypeError, ValueError):
        return None


def resolve_mt5_broker_symbol(
    logic_symbol: str,
    *,
    account_symbol_map: Optional[dict[str, str]] = None,
) -> str:
    key = (logic_symbol or "").strip().upper()
    if account_symbol_map and key in account_symbol_map:
        return account_symbol_map[key]
    return normalize_broker_xau_symbol(logic_symbol)


def _bar_open_vn_iso(t_unix: int) -> str:
    """Bar open time from MT5 Unix seconds → ISO string in Vietnam timezone."""
    return datetime.fromtimestamp(t_unix, tz=timezone.utc).astimezone(_VN_TZ).isoformat()


def _bar_records(rates: Any) -> list[dict[str, Any]]:
    if rates is None:
        return []
    out: list[dict[str, Any]] = []
    for bar in rates:
        t_raw = bar["time"]
        t = int(t_raw.item()) if hasattr(t_raw, "item") else int(t_raw)
        out.append(
            {
                "t": _bar_open_vn_iso(t),
                "open": float(bar["open"]),
                "high": float(bar["high"]),
                "low": float(bar["low"]),
                "close": float(bar["close"]),
                "tick_volume": int(bar["tick_volume"]),
            }
        )
    return out


def _connect_mt5_for_candles(
    account: Optional[MT5AccountEntry],
) -> tuple[Any | None, bool]:
    """
    Return ``(mt5_module, shutdown_after)``.

    When connected via ``ensure_mt5_session`` with explicit credentials, caller should shutdown.
    When reusing current terminal session, do not shutdown.
    """
    if account is not None:
        session = ensure_mt5_session(
            terminal_path=account.terminal_path,
            login=account.login,
            password=account.password,
            server=account.server,
        )
        if not session.ok:
            _log.warning("mt5_candles: không kết nối MT5 | %s", session.message)
            return None, False
        return session.mt5, not session.reused

    mt5 = _mt5_init_current_terminal()
    if mt5 is None:
        _log.warning("mt5_candles: không kết nối MT5 terminal hiện tại")
        return None, False
    return mt5, False


def fetch_mt5_spot_candles_payload(
    *,
    logic_symbol: str = "XAUUSD",
    interval: str | None = None,
    count: int | None = None,
    accounts_json: Path | None = None,
) -> Optional[dict[str, Any]]:
    """Fetch latest OHLC bars from MT5; ``None`` when MT5/symbol unavailable."""
    try:
        _load_mt5()
    except SystemExit:
        _log.warning("mt5_candles: MetaTrader5 package không có — bỏ qua export spot candles")
        return None

    iv = interval or mt5_spot_candles_interval()
    n = count if count is not None else mt5_spot_candles_count()

    accounts = load_mt5_accounts_for_cli(accounts_json)
    account: MT5AccountEntry | None = None
    symbol_map: dict[str, str] | None = None
    if accounts:
        account = primary_account(accounts)
        symbol_map = account.symbol_map or None

    mt5, shutdown_after = _connect_mt5_for_candles(account)
    if mt5 is None:
        return None

    broker_symbol = resolve_mt5_broker_symbol(logic_symbol, account_symbol_map=symbol_map)
    tf = _interval_to_mt5_timeframe(mt5, iv)
    if tf is None:
        _log.warning("mt5_candles: interval không hỗ trợ | interval=%s", iv)
        if shutdown_after:
            try:
                mt5.shutdown()
            except Exception:
                pass
        return None

    try:
        if not mt5.symbol_select(broker_symbol, True):
            _log.warning(
                "mt5_candles: symbol_select thất bại | logic=%s broker=%s",
                logic_symbol,
                broker_symbol,
            )
            return None
        rates = mt5.copy_rates_from_pos(broker_symbol, tf, 0, n)
        bars = _bar_records(rates)
        if not bars:
            _log.warning(
                "mt5_candles: 0 nến | logic=%s broker=%s interval=%s",
                logic_symbol,
                broker_symbol,
                iv,
            )
            return None
        return {
            "source": "mt5",
            "symbol": (logic_symbol or "XAUUSD").strip().upper(),
            "broker_symbol": broker_symbol,
            "interval": iv,
            "timezone": MT5_CANDLES_TIMEZONE,
            "n_bars": len(bars),
            "n_bars_requested": n,
            "generated_at": datetime.now(_VN_TZ).isoformat(),
            "bars": bars,
        }
    finally:
        if shutdown_after:
            try:
                mt5.shutdown()
            except Exception:
                pass


def export_mt5_spot_candles_json(
    *,
    charts_dir: Path,
    stamp: str,
    logic_symbol: str = "XAUUSD",
    interval: str | None = None,
    count: int | None = None,
    accounts_json: Path | None = None,
) -> Path | None:
    """
    Write ``{stamp}_mt5_{SYMBOL}_{interval}.json`` under ``charts_dir``.

    Returns path when written; ``None`` when MT5 data unavailable (non-fatal).
    """
    payload = fetch_mt5_spot_candles_payload(
        logic_symbol=logic_symbol,
        interval=interval,
        count=count,
        accounts_json=accounts_json,
    )
    if payload is None:
        return None
    iv = interval or mt5_spot_candles_interval()
    out = mt5_spot_candles_json_path(
        charts_dir,
        logic_symbol=logic_symbol,
        interval=iv,
        stamp=stamp,
    )
    charts_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    _log.info(
        "mt5_candles: đã ghi %s | symbol=%s broker=%s interval=%s n_bars=%s",
        out.name,
        payload.get("symbol"),
        payload.get("broker_symbol"),
        payload.get("interval"),
        payload.get("n_bars"),
    )
    return out
