"""Convert COMEX GC footprint + CSV to spot XAUUSD for OpenAI upload."""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from automation_tool.chart_payload_validate import normalize_gocharting_csv_text
from automation_tool.gocharting_footprint_ocr import csv_time_to_footprint_key, format_footprint_candle_time

_log = logging.getLogger(__name__)

GOCHARTING_GC_EXPORT_LABEL = "GC"
_TZ_GMT7 = timezone(timedelta(hours=7))

DEFAULT_GC_TO_SPOT_TICK = 0.01
DEFAULT_GC_TO_SPOT_MIN_MATCHED_RATIO = 0.8

_PREPARED_FOOTPRINT_RE = re.compile(
    r"^footprint_([A-Z0-9]{4,16})_(\d+m)\.json$",
    re.IGNORECASE,
)
# WS export stems that also match the pattern above (e.g. footprint_combined_5m.json).
_PREPARED_FOOTPRINT_EXCLUDED_STEMS = frozenset({"COMBINED", "RAW"})

_CSV_PRICE_COLUMNS = (
    "Open",
    "High",
    "Low",
    "Close",
    "Vwap",
    "BuyVwap",
    "SellVwap",
)

_CSV_INT_COLUMNS = (
    ("Delta", "delta"),
    ("MaxDelta", "max_delta"),
    ("MinDelta", "min_delta"),
    ("CumDelta", "cum_delta"),
    ("BuyVolume", "buy_volume"),
    ("SellVolume", "sell_volume"),
)


class GcToSpotConversionError(RuntimeError):
    """GC → spot conversion failed (MT5, CSV, or match ratio)."""


def gc_to_spot_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    ws = cfg.get("footprint_ws")
    if not isinstance(ws, dict):
        return {}
    raw = ws.get("gc_to_spot")
    return raw if isinstance(raw, dict) else {}


def gc_to_spot_enabled(cfg: dict[str, Any]) -> bool:
    return bool(gc_to_spot_cfg(cfg).get("enabled"))


def gc_to_spot_skip_main_csv(cfg: dict[str, Any]) -> bool:
    sub = gc_to_spot_cfg(cfg)
    if not gc_to_spot_enabled(cfg):
        return False
    if sub.get("skip_main_csv") is False:
        return False
    return True


def gc_to_spot_skip_main_png(cfg: dict[str, Any]) -> bool:
    sub = gc_to_spot_cfg(cfg)
    if not gc_to_spot_enabled(cfg):
        return False
    if sub.get("skip_main_png") is False:
        return False
    return True


def gc_to_spot_spot_tick(cfg: dict[str, Any]) -> float:
    raw = gc_to_spot_cfg(cfg).get("spot_tick", DEFAULT_GC_TO_SPOT_TICK)
    try:
        tick = float(raw)
        return tick if tick > 0 else DEFAULT_GC_TO_SPOT_TICK
    except (TypeError, ValueError):
        return DEFAULT_GC_TO_SPOT_TICK


def gc_to_spot_price_precision(cfg: dict[str, Any]) -> int:
    """Decimal places for spot-shifted prices (from ``spot_tick``)."""
    tick = gc_to_spot_spot_tick(cfg)
    text = f"{tick:.10f}".rstrip("0")
    if "." not in text:
        return 0
    return len(text.split(".", 1)[1])


def gc_to_spot_min_matched_ratio(cfg: dict[str, Any]) -> float:
    raw = gc_to_spot_cfg(cfg).get("min_matched_ratio", DEFAULT_GC_TO_SPOT_MIN_MATCHED_RATIO)
    try:
        ratio = float(raw)
        if ratio <= 0:
            return DEFAULT_GC_TO_SPOT_MIN_MATCHED_RATIO
        return min(1.0, ratio)
    except (TypeError, ValueError):
        return DEFAULT_GC_TO_SPOT_MIN_MATCHED_RATIO


def prepared_footprint_json_stem(logic_symbol: str, interval: str) -> str:
    from automation_tool.images import normalize_main_chart_symbol

    sym = normalize_main_chart_symbol(logic_symbol)
    iv = (interval or "").strip().lower()
    return f"footprint_{sym}_{iv}"


def prepared_footprint_json_path(charts_dir: Path, logic_symbol: str, interval: str) -> Path:
    return charts_dir / f"{prepared_footprint_json_stem(logic_symbol, interval)}.json"


def parse_prepared_footprint_path(path: Path) -> Optional[tuple[str, str]]:
    m = _PREPARED_FOOTPRINT_RE.match(path.name)
    if not m:
        return None
    sym = m.group(1).upper()
    if sym in _PREPARED_FOOTPRINT_EXCLUDED_STEMS:
        return None
    return sym, m.group(2).lower()


def is_prepared_footprint_path(path: Path) -> bool:
    return parse_prepared_footprint_path(path) is not None


def is_gocharting_main_pair_path(path: Path) -> bool:
    """True for ``*_gocharting_GC_*`` CSV/PNG or main-pair prepared footprint JSON."""
    if is_prepared_footprint_path(path):
        return True
    lower = path.name.lower()
    token = f"_gocharting_{GOCHARTING_GC_EXPORT_LABEL.lower()}_"
    return token in lower


def _float_or_none(raw: Any) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _int_or_none(raw: Any) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def validate_match_ratio(matched: int, total: int, cfg: dict[str, Any], *, label: str) -> None:
    if total <= 0:
        raise GcToSpotConversionError(f"gc_to_spot: no candles to convert ({label})")
    ratio = matched / total
    min_ratio = gc_to_spot_min_matched_ratio(cfg)
    if ratio < min_ratio:
        raise GcToSpotConversionError(
            f"gc_to_spot: {label} matched {matched}/{total} "
            f"({ratio:.0%} < {min_ratio:.0%} min_matched_ratio)"
        )


def _mt5_payload_covers_footprint(
    payload: dict[str, Any],
    footprint_candles: list[dict[str, Any]] | None,
    *,
    min_overlap_ratio: float = 0.5,
) -> bool:
    """True when cached MT5 bars overlap enough footprint candle times."""
    if not footprint_candles:
        return True
    from automation_tool.gocharting_ws_decode import footprint_candle_time_key

    fp_keys = {
        footprint_candle_time_key(c)
        for c in footprint_candles
        if isinstance(c, dict)
    }
    fp_keys = {k for k in fp_keys if k}
    if not fp_keys:
        return True
    mt5_keys = set(build_mt5_spot_ohlc_index(payload).keys())
    overlap = len(fp_keys & mt5_keys)
    if overlap == 0:
        return False
    return (overlap / len(fp_keys)) >= min_overlap_ratio


def resolve_mt5_spot_payload(
    *,
    charts_dir: Path,
    logic_symbol: str,
    interval: str,
    count: int,
    chart_stamp: str | None = None,
    footprint_candles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from automation_tool.mt5_candles import fetch_mt5_spot_candles_payload, mt5_spot_candles_json_path

    cached = mt5_spot_candles_json_path(
        charts_dir,
        logic_symbol=logic_symbol,
        interval=interval,
        stamp=chart_stamp,
    )
    if cached.is_file():
        try:
            payload = json.loads(cached.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and payload.get("bars"):
                if _mt5_payload_covers_footprint(payload, footprint_candles):
                    return payload
                _log.info(
                    "gc_to_spot: bỏ MT5 cache %s — không khớp cửa sổ footprint hiện tại",
                    cached.name,
                )
        except (OSError, json.JSONDecodeError):
            pass

    payload = fetch_mt5_spot_candles_payload(
        logic_symbol=logic_symbol,
        interval=interval,
        count=count,
        footprint_candles=footprint_candles,
    )
    if payload is None:
        raise GcToSpotConversionError(
            f"gc_to_spot: MT5 spot unavailable for {logic_symbol} {interval} "
            f"({count} bars) — start MT5 terminal and ensure symbol is available"
        )
    return payload


def mt5_bar_time_to_footprint_key(iso_t: str) -> str:
    """Convert MT5 bar ``t`` (ISO Asia/Ho_Chi_Minh) to footprint ``time_gmt7`` key."""
    raw = (iso_t or "").strip()
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return ""
    if dt.tzinfo is not None:
        dt = dt.astimezone(_TZ_GMT7).replace(tzinfo=None)
    return format_footprint_candle_time(dt)


def build_mt5_spot_ohlc_index(mt5_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map footprint ``time_gmt7`` → MT5 spot OHLC for each bar."""
    index: dict[str, dict[str, Any]] = {}
    bars = mt5_payload.get("bars")
    if not isinstance(bars, list):
        return index
    for bar in bars:
        if not isinstance(bar, dict):
            continue
        time_key = mt5_bar_time_to_footprint_key(str(bar.get("t") or ""))
        if not time_key:
            continue
        index[time_key] = {
            "open": bar.get("open"),
            "high": bar.get("high"),
            "low": bar.get("low"),
            "close": bar.get("close"),
            "tick_volume": bar.get("tick_volume"),
        }
    return index


def build_basis_index(
    candles: list[dict[str, Any]],
    mt5_payload: dict[str, Any],
) -> tuple[dict[str, float], dict[str, dict[str, Any]]]:
    """Return ``(time_key → basis, time_key → spot ohlc)``."""
    from automation_tool.gocharting_ws_decode import footprint_candle_time_key

    spot_index = build_mt5_spot_ohlc_index(mt5_payload)
    basis_index: dict[str, float] = {}
    for candle in candles:
        if not isinstance(candle, dict):
            continue
        time_key = footprint_candle_time_key(candle)
        if not time_key:
            continue
        spot = spot_index.get(time_key)
        gc_ohlc = candle.get("ohlc")
        if not isinstance(spot, dict) or not isinstance(gc_ohlc, dict):
            continue
        gc_close = _float_or_none(gc_ohlc.get("close"))
        spot_close = _float_or_none(spot.get("close"))
        if gc_close is None or spot_close is None:
            continue
        basis_index[time_key] = spot_close - gc_close
    return basis_index, spot_index


def _shift_price(price: float, basis: float, *, spot_tick: float) -> float:
    precision = max(0, len(str(spot_tick).rstrip("0").split(".")[-1]) if "." in str(spot_tick) else 0)
    return round(price + basis, precision)


def _footprint_side_volume(side: Any) -> int:
    if isinstance(side, dict):
        try:
            return int(side.get("volume") or 0)
        except (TypeError, ValueError):
            return 0
    try:
        return int(side or 0)
    except (TypeError, ValueError):
        return 0


def _merge_shifted_footprint_levels(
    levels: list[dict[str, Any]],
    *,
    basis: float,
    spot_tick: float,
) -> list[dict[str, Any]]:
    buckets: dict[float, dict[str, int]] = {}
    extra: dict[float, dict[str, Any]] = {}
    for level in levels:
        if not isinstance(level, dict):
            continue
        price_raw = _float_or_none(level.get("price"))
        if price_raw is None:
            continue
        price = _shift_price(price_raw, basis, spot_tick=spot_tick)
        buy = _footprint_side_volume(level.get("buy"))
        sell = _footprint_side_volume(level.get("sell"))
        bucket = buckets.setdefault(price, {"buy": 0, "sell": 0})
        bucket["buy"] += buy
        bucket["sell"] += sell
        row_extra = extra.setdefault(price, {})
        for key in ("rl", "imbalance", "side", "total_vol", "bid", "ask"):
            if key in level and key not in row_extra:
                row_extra[key] = level[key]

    out: list[dict[str, Any]] = []
    for price in sorted(buckets.keys(), reverse=True):
        vols = buckets[price]
        row: dict[str, Any] = {"price": price, "buy": vols["buy"], "sell": vols["sell"]}
        row.update(extra.get(price, {}))
        out.append(row)
    return out


def convert_footprint_combined_to_spot(
    doc: dict[str, Any],
    *,
    mt5_payload: dict[str, Any],
    cfg: dict[str, Any],
    logic_symbol: str,
    interval: str,
) -> dict[str, Any]:
    candles_raw = doc.get("candles")
    if not isinstance(candles_raw, list) or not candles_raw:
        raise GcToSpotConversionError("gc_to_spot: footprint document has no candles")

    spot_tick = gc_to_spot_spot_tick(cfg)
    basis_index, spot_index = build_basis_index(
        [c for c in candles_raw if isinstance(c, dict)],
        mt5_payload,
    )
    validate_match_ratio(len(basis_index), len(candles_raw), cfg, label="MT5 basis")

    from automation_tool.images import normalize_main_chart_symbol

    sym = normalize_main_chart_symbol(logic_symbol)
    matched_basis = 0
    basis_values: list[float] = []
    candles_out: list[dict[str, Any]] = []

    for candle in candles_raw:
        if not isinstance(candle, dict):
            continue
        block = dict(candle)
        time_key = str(block.get("time_gmt7") or "").strip()
        basis = basis_index.get(time_key)
        spot_ohlc = spot_index.get(time_key)
        if basis is None or spot_ohlc is None:
            continue
        matched_basis += 1
        basis_values.append(basis)
        block["ohlc"] = {
            "open": spot_ohlc.get("open"),
            "high": spot_ohlc.get("high"),
            "low": spot_ohlc.get("low"),
            "close": spot_ohlc.get("close"),
            "volume": block.get("ohlc", {}).get("volume") if isinstance(block.get("ohlc"), dict) else None,
            "oi": block.get("ohlc", {}).get("oi") if isinstance(block.get("ohlc"), dict) else None,
        }
        block.pop("mt5_spot_ohlc", None)
        footprint = block.get("footprint")
        if isinstance(footprint, list):
            block["footprint"] = _merge_shifted_footprint_levels(
                footprint,
                basis=basis,
                spot_tick=spot_tick,
            )
        candles_out.append(block)

    validate_match_ratio(matched_basis, len(candles_raw), cfg, label="candle spot convert")

    out = dict(doc)
    out["candles"] = candles_out
    out["symbol"] = sym
    out["interval"] = (interval or "").strip().lower()
    out.pop("mt5_spot", None)
    out.pop("ohlc_matched", None)
    out.pop("ohlc_available", None)
    return out


def _newest_footprint_session_date(doc: dict[str, Any]) -> str | None:
    """Newest session date for prepared spot ``request.date`` (not oldest IDB merge key)."""
    session_dates = doc.get("ws_session_dates")
    if isinstance(session_dates, list) and session_dates:
        dated = [str(d).strip() for d in session_dates if str(d).strip()]
        if dated:
            return max(dated)
    profiles = doc.get("session_profiles")
    if isinstance(profiles, list):
        keys = [
            str(p.get("session_key")).strip()
            for p in profiles
            if isinstance(p, dict) and str(p.get("session_key") or "").strip()
        ]
        if keys:
            return max(keys)
    candles = doc.get("candles")
    if isinstance(candles, list):
        from automation_tool.gocharting_footprint_ocr import parse_footprint_candle_datetime

        dts = []
        for candle in candles:
            if not isinstance(candle, dict):
                continue
            time_key = str(candle.get("time_gmt7") or candle.get("time") or "").strip()
            dt = parse_footprint_candle_datetime(time_key)
            if dt is not None:
                dts.append(dt)
        if dts:
            return max(dts).strftime("%Y-%m-%d")
    req = doc.get("request") if isinstance(doc.get("request"), dict) else {}
    date = str(req.get("date") or "").strip()
    return date or None


def _spot_footprint_request(
    logic_symbol: str,
    interval: str,
    *,
    doc: dict[str, Any],
) -> dict[str, Any]:
    from automation_tool.images import normalize_main_chart_symbol

    sym = normalize_main_chart_symbol(logic_symbol)
    iv = (interval or "").strip().lower()
    out: dict[str, Any] = {"symbol": sym, "interval": iv}
    date = _newest_footprint_session_date(doc)
    if date:
        out["date"] = date
    return out


def is_finalized_spot_footprint(
    doc: dict[str, Any],
    *,
    logic_symbol: str | None = None,
) -> bool:
    """True when prepared footprint JSON is spot-only (no GC audit / merge metadata)."""
    if not isinstance(doc, dict):
        return False
    from automation_tool.images import normalize_main_chart_symbol

    sym = str(doc.get("symbol") or "").strip().upper()
    if logic_symbol:
        try:
            expected = normalize_main_chart_symbol(logic_symbol)
        except ValueError:
            return False
        if sym != expected:
            return False
    elif not sym or not re.match(r"^[A-Z0-9]{4,16}$", sym):
        return False
    for key in ("gc_to_spot", "source", "ohlc_matched", "ohlc_available", "mt5_spot"):
        if key in doc:
            return False
    req = doc.get("request")
    if not isinstance(req, dict):
        return True
    for key in ("exchange", "segment", "session"):
        if req.get(key):
            return False
    req_sym = str(req.get("symbol") or "").strip().upper()
    return not req_sym or req_sym == sym


def finalize_prepared_spot_footprint(
    doc: dict[str, Any],
    *,
    logic_symbol: str,
    interval: str,
) -> dict[str, Any]:
    """Strip GC provenance and normalize ``request`` for OpenAI / disk prepared export."""
    from automation_tool.images import normalize_main_chart_symbol

    out = dict(doc)
    sym = normalize_main_chart_symbol(logic_symbol)
    iv = (interval or "").strip().lower()
    out["symbol"] = sym
    out["interval"] = iv
    out["request"] = _spot_footprint_request(sym, iv, doc=doc)
    for key in ("source", "gc_to_spot", "ohlc_available", "ohlc_matched", "mt5_spot"):
        out.pop(key, None)
    return out


def _parse_gc_csv_bar_flow_rows(text: str) -> dict[str, dict[str, Any]]:
    normalized = normalize_gocharting_csv_text(text)
    by_time: dict[str, dict[str, Any]] = {}
    reader = csv.DictReader(io.StringIO(normalized))
    for row in reader:
        if not row:
            continue
        time_raw = row.get("Time") or row.get("Date") or row.get("time") or ""
        time_key = csv_time_to_footprint_key(str(time_raw))
        if not time_key:
            continue
        bar: dict[str, Any] = {}
        for col in _CSV_PRICE_COLUMNS:
            val = _float_or_none(row.get(col))
            if val is not None:
                bar[col.lower()] = val
        for src, dst in _CSV_INT_COLUMNS:
            val = _int_or_none(row.get(src))
            if val is not None:
                bar[dst] = val
        vol = _int_or_none(row.get("Volume"))
        if vol is not None:
            bar["volume"] = vol
        if bar:
            by_time[time_key] = bar
    return by_time


_BAR_FLOW_SHIFT_PRICE_KEYS = (
    "vwap",
    "session_vwap",
    "buy_vwap",
    "sell_vwap",
    "buyvwap",
    "sellvwap",
)
_BAR_FLOW_OHLC_KEYS = ("open", "high", "low", "close")


def _shift_bar_flow_prices(bar: dict[str, Any], basis: float, *, spot_tick: float) -> dict[str, Any]:
    out = dict(bar)
    for key in _BAR_FLOW_SHIFT_PRICE_KEYS:
        val = _float_or_none(out.get(key))
        if val is not None:
            out[key] = _shift_price(val, basis, spot_tick=spot_tick)
    for key in _BAR_FLOW_OHLC_KEYS:
        out.pop(key, None)
    return out


def _latest_basis_from_index(
    candles: list[dict[str, Any]],
    basis_index: dict[str, float],
) -> Optional[float]:
    """Basis from the newest candle that has an MT5 match."""
    from automation_tool.gocharting_ws_decode import candle_sort_datetime

    ordered = sorted(
        [c for c in candles if isinstance(c, dict)],
        key=candle_sort_datetime,
        reverse=True,
    )
    for candle in ordered:
        time_key = str(candle.get("time_gmt7") or "").strip()
        if time_key and time_key in basis_index:
            return basis_index[time_key]
    return None


def shift_footprint_session_levels_to_spot(
    doc: dict[str, Any],
    *,
    basis_index: dict[str, float],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """
    Shift session POC/VAH/VAL/VWAP by the **latest** candle basis.

    Per-candle ``bar_flow.session_vwap`` is shifted separately (each bar's own basis)
    in :func:`_shift_bar_flow_prices` during bar_flow enrich.
    """
    from automation_tool.gocharting_session_profile import shift_session_profile_prices

    candles_raw = doc.get("candles")
    if not isinstance(candles_raw, list) or not candles_raw:
        return doc

    latest_basis = _latest_basis_from_index(
        [c for c in candles_raw if isinstance(c, dict)],
        basis_index,
    )
    if latest_basis is None:
        return doc

    spot_tick = gc_to_spot_spot_tick(cfg)
    out = dict(doc)

    profiles = out.get("session_profiles")
    if isinstance(profiles, list):
        out["session_profiles"] = [
            shift_session_profile_prices(p, latest_basis, spot_tick=spot_tick)
            if isinstance(p, dict)
            else p
            for p in profiles
        ]

    candles_out: list[Any] = []
    for candle in candles_raw:
        if not isinstance(candle, dict):
            candles_out.append(candle)
            continue
        block = dict(candle)
        sp = block.get("session_profile")
        if isinstance(sp, dict):
            block["session_profile"] = shift_session_profile_prices(
                sp,
                latest_basis,
                spot_tick=spot_tick,
            )
        candles_out.append(block)
    out["candles"] = candles_out
    return out


def enrich_prepared_footprint_from_gc_csv(
    doc: dict[str, Any],
    csv_path: Path,
    *,
    cfg: dict[str, Any],
    basis_index: dict[str, float],
) -> dict[str, Any]:
    if not csv_path.is_file():
        raise GcToSpotConversionError(f"gc_to_spot: GC CSV not found: {csv_path}")

    try:
        csv_text = csv_path.read_text(encoding="utf-8")
    except OSError as e:
        raise GcToSpotConversionError(f"gc_to_spot: cannot read GC CSV {csv_path}: {e}") from e

    csv_rows = _parse_gc_csv_bar_flow_rows(csv_text)
    if not csv_rows:
        raise GcToSpotConversionError(f"gc_to_spot: GC CSV has no parseable rows: {csv_path.name}")

    spot_tick = gc_to_spot_spot_tick(cfg)
    candles_raw = doc.get("candles")
    if not isinstance(candles_raw, list):
        raise GcToSpotConversionError("gc_to_spot: document has no candles list")

    matched = 0
    candles_out: list[dict[str, Any]] = []
    for candle in candles_raw:
        if not isinstance(candle, dict):
            continue
        block = dict(candle)
        time_key = str(block.get("time_gmt7") or "").strip()
        csv_bar = csv_rows.get(time_key)
        basis = basis_index.get(time_key)
        if csv_bar is not None and basis is not None:
            block["bar_flow"] = _shift_bar_flow_prices(csv_bar, basis, spot_tick=spot_tick)
            matched += 1
        candles_out.append(block)

    validate_match_ratio(matched, len(candles_raw), cfg, label="CSV bar_flow merge")

    out = dict(doc)
    out["candles"] = candles_out
    return shift_footprint_session_levels_to_spot(out, basis_index=basis_index, cfg=cfg)


def enrich_prepared_footprint_from_ws_bar_flow(
    doc: dict[str, Any],
    *,
    cfg: dict[str, Any],
    basis_index: dict[str, float],
) -> dict[str, Any]:
    """Shift per-candle ``bar_flow`` (from WS ``ending_summary``) to spot prices."""
    spot_tick = gc_to_spot_spot_tick(cfg)
    candles_raw = doc.get("candles")
    if not isinstance(candles_raw, list):
        raise GcToSpotConversionError("gc_to_spot: document has no candles list")

    matched = 0
    candles_out: list[dict[str, Any]] = []
    for candle in candles_raw:
        if not isinstance(candle, dict):
            continue
        block = dict(candle)
        time_key = str(block.get("time_gmt7") or "").strip()
        ws_bar = block.get("bar_flow")
        basis = basis_index.get(time_key)
        if isinstance(ws_bar, dict) and basis is not None:
            block["bar_flow"] = _shift_bar_flow_prices(ws_bar, basis, spot_tick=spot_tick)
            matched += 1
        candles_out.append(block)

    validate_match_ratio(matched, len(candles_raw), cfg, label="WS bar_flow merge")
    out = dict(doc)
    out["candles"] = candles_out
    return shift_footprint_session_levels_to_spot(out, basis_index=basis_index, cfg=cfg)


def footprint_has_ws_bar_flow(doc: dict[str, Any]) -> bool:
    candles = doc.get("candles")
    if not isinstance(candles, list) or not candles:
        return False
    with_bar = sum(
        1 for c in candles if isinstance(c, dict) and isinstance(c.get("bar_flow"), dict)
    )
    return with_bar >= max(1, int(len(candles) * 0.5))


def resolve_gc_csv_for_interval(
    charts_dir: Path,
    interval: str,
    *,
    chart_stamp: str | None = None,
) -> Path:
    from automation_tool.images import gocharting_main_interval_csv_path

    path = gocharting_main_interval_csv_path(charts_dir, interval, stamp=chart_stamp)
    if path is None:
        raise GcToSpotConversionError(
            f"gc_to_spot: GC CSV missing for interval {interval} "
            f"(stamp={chart_stamp or 'latest'})"
        )
    return path
