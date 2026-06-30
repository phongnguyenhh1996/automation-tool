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


def resolve_mt5_spot_payload(
    *,
    charts_dir: Path,
    logic_symbol: str,
    interval: str,
    count: int,
    chart_stamp: str | None = None,
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
                return payload
        except (OSError, json.JSONDecodeError):
            pass

    payload = fetch_mt5_spot_candles_payload(
        logic_symbol=logic_symbol,
        interval=interval,
        count=count,
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
    spot_index = build_mt5_spot_ohlc_index(mt5_payload)
    basis_index: dict[str, float] = {}
    for candle in candles:
        if not isinstance(candle, dict):
            continue
        time_key = str(candle.get("time_gmt7") or "").strip()
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


def _spot_footprint_request(
    logic_symbol: str,
    interval: str,
    *,
    doc: dict[str, Any],
) -> dict[str, Any]:
    from automation_tool.images import normalize_main_chart_symbol

    sym = normalize_main_chart_symbol(logic_symbol)
    iv = (interval or "").strip().lower()
    req = doc.get("request") if isinstance(doc.get("request"), dict) else {}
    out: dict[str, Any] = {"symbol": sym, "interval": iv}
    date = req.get("date")
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


_BAR_FLOW_PRICE_KEYS = (
    "open",
    "high",
    "low",
    "close",
    "vwap",
    "buy_vwap",
    "sell_vwap",
    "buyvwap",
    "sellvwap",
)


def _shift_bar_flow_prices(bar: dict[str, Any], basis: float, *, spot_tick: float) -> dict[str, Any]:
    out = dict(bar)
    for key in _BAR_FLOW_PRICE_KEYS:
        val = _float_or_none(out.get(key))
        if val is not None:
            out[key] = _shift_price(val, basis, spot_tick=spot_tick)
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
    return out


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
