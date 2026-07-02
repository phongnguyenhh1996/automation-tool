from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
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


DEFAULT_MT5_FOOTPRINT_RANGE_PADDING_BARS = 10


def mt5_footprint_range_padding_bars() -> int:
    raw = os.getenv("MT5_FOOTPRINT_RANGE_PADDING_BARS", "").strip()
    if raw.isdigit():
        return max(0, int(raw))
    return DEFAULT_MT5_FOOTPRINT_RANGE_PADDING_BARS


def mt5_footprint_range_utc_bounds(
    lo: datetime,
    hi: datetime,
    *,
    interval: str,
    padding_bars: int | None = None,
) -> tuple[datetime, datetime, str, str]:
    """UTC ``copy_rates_range`` bounds with ``padding_bars`` before/after footprint window."""
    bars = padding_bars if padding_bars is not None else mt5_footprint_range_padding_bars()
    bar_minutes = _interval_minutes(interval)
    pad = timedelta(minutes=bar_minutes * bars)
    date_from = lo.replace(tzinfo=_VN_TZ) - pad
    # +1 bar past ``hi`` so the last footprint open is included in MT5 range.
    date_to = hi.replace(tzinfo=_VN_TZ) + pad + timedelta(minutes=bar_minutes)
    return (
        date_from.astimezone(timezone.utc),
        date_to.astimezone(timezone.utc),
        date_from.isoformat(),
        date_to.isoformat(),
    )


def _interval_minutes(interval: str) -> int:
    iv = (interval or "").strip().lower().replace(" ", "")
    mapping = {
        "1m": 1,
        "5m": 5,
        "15m": 15,
        "30m": 30,
        "1h": 60,
        "4h": 240,
        "1d": 1440,
    }
    return mapping.get(iv, 5)


def footprint_candle_time_bounds(
    candles: list[dict[str, Any]],
) -> tuple[datetime, datetime] | None:
    """Earliest/latest footprint bar open (naive GMT+7 wall time) from candle keys."""
    from automation_tool.gocharting_footprint_ocr import parse_footprint_candle_datetime
    from automation_tool.gocharting_ws_decode import parse_proto_candle_datetime

    dts: list[datetime] = []
    for candle in candles:
        if not isinstance(candle, dict):
            continue
        time_key = str(candle.get("time_gmt7") or candle.get("time") or "").strip()
        dt = parse_footprint_candle_datetime(time_key)
        if dt is None:
            date_raw = str(candle.get("date") or "").strip()
            if date_raw:
                try:
                    dt = (
                        parse_proto_candle_datetime(date_raw)
                        .astimezone(_VN_TZ)
                        .replace(tzinfo=None)
                    )
                except ValueError:
                    dt = None
        if dt is not None:
            dts.append(dt)
    if not dts:
        return None
    return min(dts), max(dts)


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
    footprint_candles: list[dict[str, Any]] | None = None,
) -> Optional[dict[str, Any]]:
    """Fetch OHLC bars from MT5 aligned to footprint candles when possible."""
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

    fetch_mode = "position"
    range_from: str | None = None
    range_to: str | None = None

    try:
        if not mt5.symbol_select(broker_symbol, True):
            _log.warning(
                "mt5_candles: symbol_select thất bại | logic=%s broker=%s",
                logic_symbol,
                broker_symbol,
            )
            return None

        rates = None
        bounds = (
            footprint_candle_time_bounds(footprint_candles)
            if footprint_candles
            else None
        )
        if bounds is not None:
            lo, hi = bounds
            padding_bars = mt5_footprint_range_padding_bars()
            date_from_utc, date_to_utc, range_from, range_to = mt5_footprint_range_utc_bounds(
                lo,
                hi,
                interval=iv,
                padding_bars=padding_bars,
            )
            rates = mt5.copy_rates_range(broker_symbol, tf, date_from_utc, date_to_utc)
            if rates is not None and len(rates) > 0:
                fetch_mode = "range"

        if rates is None or len(rates) == 0:
            fetch_mode = "position"
            range_from = None
            range_to = None
            rates = mt5.copy_rates_from_pos(broker_symbol, tf, 0, n)

        bars = _bar_records(rates)
        if not bars:
            _log.warning(
                "mt5_candles: 0 nến | logic=%s broker=%s interval=%s mode=%s",
                logic_symbol,
                broker_symbol,
                iv,
                fetch_mode,
            )
            return None
        payload: dict[str, Any] = {
            "source": "mt5",
            "symbol": (logic_symbol or "XAUUSD").strip().upper(),
            "broker_symbol": broker_symbol,
            "interval": iv,
            "timezone": MT5_CANDLES_TIMEZONE,
            "n_bars": len(bars),
            "n_bars_requested": n,
            "fetch_mode": fetch_mode,
            "generated_at": datetime.now(_VN_TZ).isoformat(),
            "bars": bars,
        }
        if range_from and range_to:
            payload["range_from"] = range_from
            payload["range_to"] = range_to
            payload["range_padding_bars"] = mt5_footprint_range_padding_bars()
        return payload
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
