from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from google.protobuf.json_format import MessageToDict

from automation_tool.gocharting_footprint_extract import FOOTPRINT_CHART_TYPE
from automation_tool.gocharting_footprint_ocr import (
    DEFAULT_FOOTPRINT_BLOCK_MULTIPLIER,
    DEFAULT_FOOTPRINT_TICK_SIZE,
    format_footprint_candle_time,
    snap_to_gocharting_chart_block,
)
from automation_tool.proto import footprint_pb2 as pb
from automation_tool.proto import ohlc_bars_pb2 as ob

_log = logging.getLogger(__name__)

WS_BINARY_MARKER = ord("m")
_TZ_GMT7 = timezone(timedelta(hours=7))
_PROTO_DATE_OFFSET_RE = re.compile(r"([+-])(\d{2}):?(\d{2})$")
FOOTPRINT_EXPORT_FORMAT_BID_ASK = "bid_ask"
FOOTPRINT_EXPORT_FORMAT_RAW = "raw"
FOOTPRINT_EXPORT_FORMAT_COMBINED = "combined"
FOOTPRINT_EXPORT_FORMATS = (
    FOOTPRINT_EXPORT_FORMAT_BID_ASK,
    FOOTPRINT_EXPORT_FORMAT_RAW,
    FOOTPRINT_EXPORT_FORMAT_COMBINED,
)


def parse_ws_binary_envelope(raw: bytes) -> Optional[tuple[str, bytes]]:
    """
    GoCharting WS binary frame::

        'm' + 3 zero bytes + uint8(type_len) + type_str + protobuf_payload
    """
    if len(raw) < 6 or raw[0] != WS_BINARY_MARKER:
        return None
    if raw[1:4] != b"\x00\x00\x00":
        return None
    type_len = raw[4]
    if type_len <= 0 or 5 + type_len > len(raw):
        return None
    type_str = raw[5 : 5 + type_len].decode("ascii", errors="replace")
    payload = raw[5 + type_len :]
    return type_str, payload


def decode_footprint_for_date_response(payload: bytes) -> pb.FootPrintForDateResponse:
    msg = pb.FootPrintForDateResponse()
    msg.ParseFromString(payload)
    return msg


def parse_proto_candle_datetime(date_str: str) -> datetime:
    """Parse GoCharting candle date (ISO with optional numeric offset)."""
    s = (date_str or "").strip()
    if not s:
        raise ValueError("empty candle date")
    tzinfo = timezone.utc
    m = _PROTO_DATE_OFFSET_RE.search(s)
    if m:
        sign, hours, minutes = m.groups()
        delta = timedelta(hours=int(hours), minutes=int(minutes))
        if sign == "-":
            delta = -delta
        tzinfo = timezone(delta)
        s = s[: m.start()]
    if s.endswith("Z"):
        s = s[:-1]
        tzinfo = timezone.utc
    dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    return dt.replace(tzinfo=tzinfo)


def proto_candle_time_key(date_str: str) -> str:
    return datetime_to_footprint_time_key(parse_proto_candle_datetime(date_str))


def datetime_to_footprint_time_key(dt: datetime) -> str:
    local = dt.astimezone(_TZ_GMT7).replace(tzinfo=None)
    return format_footprint_candle_time(local)


def display_symbol_from_request(req: pb.FootPrintForDateRequest) -> str:
    exchange = (req.exchange or "").strip()
    segment = (req.segment or "").strip()
    symbol = (req.symbol or "").strip()
    if exchange and symbol and segment.upper() == "FUTURE":
        return f"{exchange}:{symbol}"
    if exchange and segment and symbol:
        return f"{exchange}:{segment}:{symbol}"
    return symbol or exchange or "UNKNOWN"


def scaled_price(level: int, price_precision: int) -> float:
    precision = max(0, int(price_precision))
    return round(int(level) / (10**precision), precision)


def footprint_raw_json_path(out_dir: Path, interval: str) -> Path:
    iv = (interval or "").strip().lower()
    return out_dir / f"footprint_raw_{iv}.json"


def footprint_combined_json_path(out_dir: Path, interval: str) -> Path:
    iv = (interval or "").strip().lower()
    return out_dir / f"footprint_combined_{iv}.json"


def decode_ohlc_bar_result(payload: bytes) -> ob.OHLCBarResult:
    msg = ob.OHLCBarResult()
    msg.ParseFromString(payload)
    return msg


def _ohlc_fields(candle: ob.Candle, *, price_precision: int) -> dict[str, Any]:
    pp = max(0, int(price_precision))
    return {
        "open": scaled_price(candle.open, pp),
        "high": scaled_price(candle.high, pp),
        "low": scaled_price(candle.low, pp),
        "close": scaled_price(candle.close, pp),
        "volume": int(candle.volume),
        "oi": int(candle.oi),
    }


def intraday_ohlc_bars_to_candles(
    bars: ob.IntradayOHLCBars,
    *,
    session_date: str,
) -> list[dict[str, Any]]:
    if not bars.start:
        return []
    pp = int(bars.price_precision or 0)
    base = parse_proto_candle_datetime(bars.start)
    out: list[dict[str, Any]] = []
    for candle in bars.candles:
        dt = base + timedelta(minutes=int(candle.offset))
        time_key = datetime_to_footprint_time_key(dt)
        out.append(
            {
                "date": dt.isoformat(),
                "time_gmt7": time_key,
                "session_date": session_date,
                "offset_minutes": int(candle.offset),
                "ohlc": _ohlc_fields(candle, price_precision=pp),
            }
        )
    return out


def ohlc_bar_result_to_document(
    msg: ob.OHLCBarResult,
    *,
    ws_type: Optional[str] = None,
) -> dict[str, Any]:
    candles: list[dict[str, Any]] = []
    for session_date, bars in msg.intraday_candles.items():
        candles.extend(
            intraday_ohlc_bars_to_candles(bars, session_date=str(session_date))
        )
    candles.sort(key=lambda row: row.get("time_gmt7") or "")
    return {
        "ws_type": ws_type or "",
        "zone": msg.zone,
        "offset_in": msg.offset_in,
        "count": int(msg.count),
        "candles": candles,
    }


def decode_ws_ohlc_frame(raw: bytes) -> Optional[dict[str, Any]]:
    parsed = parse_ws_binary_envelope(raw)
    if parsed is None:
        return None
    type_str, payload = parsed
    if "TS/V2" not in type_str and not type_str.startswith("TS/"):
        return None
    msg = decode_ohlc_bar_result(payload)
    return ohlc_bar_result_to_document(msg, ws_type=type_str)


def build_ohlc_index(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map ``time_gmt7`` → ``ohlc`` dict; later WS batches overwrite earlier ones."""
    index: dict[str, dict[str, Any]] = {}
    for doc in docs:
        for candle in doc.get("candles") or []:
            if not isinstance(candle, dict):
                continue
            time_key = str(candle.get("time_gmt7") or "").strip()
            ohlc = candle.get("ohlc")
            if time_key and isinstance(ohlc, dict):
                index[time_key] = ohlc
    return index


_COMBINED_DOC_DROP_KEYS = ("is_complete",)
_COMBINED_CANDLE_DROP_KEYS = ("ending_summary", "max", "min")
_OPENAI_DOC_DROP_KEYS = ("fp_day", "ws_type", "version")
_OPENAI_CANDLE_DROP_KEYS = ("totals",)


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


def slim_footprint_level_for_openai(level: dict[str, Any]) -> dict[str, Any]:
    """Compact one ``footprint[]`` row: drop ``level``, buy/sell → volume ints."""
    row: dict[str, Any] = {}
    if level.get("price") is not None:
        row["price"] = level["price"]
    row["buy"] = _footprint_side_volume(level.get("buy"))
    row["sell"] = _footprint_side_volume(level.get("sell"))
    for key in ("rl", "imbalance", "side", "total_vol", "bid", "ask"):
        if key in level:
            row[key] = level[key]
    return row


def slim_footprint_combined_for_openai(doc: dict[str, Any]) -> dict[str, Any]:
    """Token-efficient footprint combined JSON for OpenAI upload."""
    if not isinstance(doc, dict):
        return doc
    out = dict(doc)
    for key in _OPENAI_DOC_DROP_KEYS:
        out.pop(key, None)
    candles_raw = out.get("candles")
    if not isinstance(candles_raw, list):
        return out
    slim_candles: list[Any] = []
    for candle in candles_raw:
        if not isinstance(candle, dict):
            slim_candles.append(candle)
            continue
        block = dict(candle)
        for key in _OPENAI_CANDLE_DROP_KEYS:
            block.pop(key, None)
        footprint = block.get("footprint")
        if isinstance(footprint, list):
            block["footprint"] = [
                slim_footprint_level_for_openai(lvl)
                if isinstance(lvl, dict)
                else lvl
                for lvl in footprint
            ]
        slim_candles.append(block)
    out["candles"] = slim_candles
    return out


def slim_footprint_combined_document(doc: dict[str, Any]) -> dict[str, Any]:
    """Drop verbose protobuf fields from combined export (keeps raw export unchanged)."""
    out = dict(doc)
    for key in _COMBINED_DOC_DROP_KEYS:
        out.pop(key, None)
    candles_raw = out.get("candles")
    if not isinstance(candles_raw, list):
        return out
    slim_candles: list[dict[str, Any]] = []
    for candle in candles_raw:
        if not isinstance(candle, dict):
            continue
        block = dict(candle)
        for key in _COMBINED_CANDLE_DROP_KEYS:
            block.pop(key, None)
        slim_candles.append(block)
    out["candles"] = slim_candles
    return out


def merge_footprint_raw_with_ohlc(
    footprint_doc: dict[str, Any],
    ohlc_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    merged = dict(footprint_doc)
    candles_out: list[dict[str, Any]] = []
    matched = 0
    for candle in footprint_doc.get("candles") or []:
        if not isinstance(candle, dict):
            continue
        block = dict(candle)
        time_key = str(block.get("time_gmt7") or "").strip()
        ohlc = ohlc_index.get(time_key)
        block["ohlc"] = ohlc
        if ohlc is not None:
            matched += 1
        candles_out.append(block)
    merged["candles"] = candles_out
    merged["ohlc_matched"] = matched
    merged["ohlc_available"] = len(ohlc_index)
    return slim_footprint_combined_document(merged)


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


def merge_footprint_with_mt5_spot(
    footprint_doc: dict[str, Any],
    mt5_payload: dict[str, Any],
) -> dict[str, Any]:
    """Attach ``mt5_spot_ohlc`` per candle keyed by ``time_gmt7``."""
    spot_index = build_mt5_spot_ohlc_index(mt5_payload)
    merged = dict(footprint_doc)
    candles_out: list[dict[str, Any]] = []
    matched = 0
    for candle in footprint_doc.get("candles") or []:
        if not isinstance(candle, dict):
            continue
        block = dict(candle)
        time_key = str(block.get("time_gmt7") or "").strip()
        spot = spot_index.get(time_key)
        block["mt5_spot_ohlc"] = spot
        if spot is not None:
            matched += 1
        candles_out.append(block)
    merged["candles"] = candles_out
    merged["mt5_spot"] = {
        "symbol": mt5_payload.get("symbol"),
        "broker_symbol": mt5_payload.get("broker_symbol"),
        "interval": mt5_payload.get("interval"),
        "timezone": mt5_payload.get("timezone"),
        "matched": matched,
        "available": len(spot_index),
    }
    return merged


def footprint_ws_block_multiplier(cfg: dict[str, Any]) -> int:
    """GoCharting Tick Manager block multiplier (1 = per-tick WS data, unchanged)."""
    ws = cfg.get("footprint_ws")
    if not isinstance(ws, dict):
        return 1
    try:
        return max(1, int(ws.get("block_multiplier", DEFAULT_FOOTPRINT_BLOCK_MULTIPLIER)))
    except (TypeError, ValueError):
        return DEFAULT_FOOTPRINT_BLOCK_MULTIPLIER


def footprint_ws_block_size(cfg: dict[str, Any], doc: dict[str, Any]) -> float:
    """Cluster height for WS footprint aggregation: ``tick_size × block_multiplier``."""
    ws = cfg.get("footprint_ws") if isinstance(cfg.get("footprint_ws"), dict) else {}
    if ws.get("block_size") is not None:
        try:
            return float(ws["block_size"])
        except (TypeError, ValueError):
            pass
    multiplier = footprint_ws_block_multiplier(cfg)
    if multiplier <= 1:
        return 0.0
    from automation_tool.gocharting_footprint_derived import tick_size_from_footprint_doc

    tick_raw = ws.get("tick_size")
    if tick_raw is not None:
        try:
            tick = float(tick_raw)
        except (TypeError, ValueError):
            tick = tick_size_from_footprint_doc(doc)
    else:
        tick = tick_size_from_footprint_doc(doc)
    if tick <= 0:
        tick = DEFAULT_FOOTPRINT_TICK_SIZE
    return tick * multiplier


def _price_precision_from_doc(doc: dict[str, Any]) -> int:
    fp_day = doc.get("fp_day")
    if not isinstance(fp_day, dict):
        return 1
    try:
        return max(0, int(fp_day.get("price_precision") or 0))
    except (TypeError, ValueError):
        return 1


def _footprint_ws_tick_size(cfg: dict[str, Any], doc: dict[str, Any]) -> float:
    ws = cfg.get("footprint_ws") if isinstance(cfg.get("footprint_ws"), dict) else {}
    tick_raw = ws.get("tick_size")
    if tick_raw is not None:
        try:
            tick = float(tick_raw)
            if tick > 0:
                return tick
        except (TypeError, ValueError):
            pass
    from automation_tool.gocharting_footprint_derived import tick_size_from_footprint_doc

    tick = tick_size_from_footprint_doc(doc)
    if tick > 0:
        return tick
    return DEFAULT_FOOTPRINT_TICK_SIZE


def _snap_footprint_price_to_block(
    price: float,
    block_size: float,
    *,
    price_precision: int,
    tick_size: float = DEFAULT_FOOTPRINT_TICK_SIZE,
    block_multiplier: int = DEFAULT_FOOTPRINT_BLOCK_MULTIPLIER,
) -> float:
    snapped = snap_to_gocharting_chart_block(
        price,
        block_size,
        tick_size=tick_size,
        block_multiplier=block_multiplier,
    )
    return round(snapped, max(0, price_precision))


def _price_to_proto_level(price: float, price_precision: int) -> int:
    precision = max(0, int(price_precision))
    return round(price * (10**precision))


def aggregate_footprint_levels(
    levels: list[dict[str, Any]],
    *,
    block_size: float,
    price_precision: int,
    tick_size: float = DEFAULT_FOOTPRINT_TICK_SIZE,
    block_multiplier: int = DEFAULT_FOOTPRINT_BLOCK_MULTIPLIER,
) -> list[dict[str, Any]]:
    """Merge per-tick WS levels into GoCharting chart block clusters (sum buy/sell per row)."""
    if block_size <= 0 or not levels:
        return levels

    buckets: dict[float, dict[str, Any]] = {}
    for level in levels:
        if not isinstance(level, dict):
            continue
        raw_price = level.get("price")
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        block_price = _snap_footprint_price_to_block(
            price,
            block_size,
            price_precision=price_precision,
            tick_size=tick_size,
            block_multiplier=block_multiplier,
        )
        bucket = buckets.get(block_price)
        if bucket is None:
            bucket = {
                "price": block_price,
                "buy": {"trades": 0, "volume": 0},
                "sell": {"trades": 0, "volume": 0},
            }
            buckets[block_price] = bucket
        for side in ("buy", "sell"):
            src = level.get(side) if isinstance(level.get(side), dict) else {}
            bucket[side]["trades"] += int(src.get("trades") or 0)
            bucket[side]["volume"] += int(src.get("volume") or 0)

    if not buckets:
        return levels

    out: list[dict[str, Any]] = []
    for block_price in sorted(buckets, reverse=True):
        row = buckets[block_price]
        out.append(
            {
                "level": _price_to_proto_level(block_price, price_precision),
                "price": block_price,
                "buy": dict(row["buy"]),
                "sell": dict(row["sell"]),
            }
        )
    return out


def aggregate_footprint_combined_document(
    doc: dict[str, Any],
    *,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-bucket WS ``footprint[]`` by block size before OpenAI upload (on-disk file unchanged)."""
    if not isinstance(doc, dict):
        return doc
    cfg_raw = cfg if isinstance(cfg, dict) else {}
    block_size = footprint_ws_block_size(cfg_raw, doc)
    if block_size <= 0:
        return doc

    price_precision = _price_precision_from_doc(doc)
    tick_size = _footprint_ws_tick_size(cfg_raw, doc)
    block_multiplier = footprint_ws_block_multiplier(cfg_raw)
    out = dict(doc)
    candles_out: list[Any] = []
    for candle in doc.get("candles") or []:
        if not isinstance(candle, dict):
            candles_out.append(candle)
            continue
        block = dict(candle)
        footprint = block.get("footprint")
        if isinstance(footprint, list):
            block["footprint"] = aggregate_footprint_levels(
                footprint,
                block_size=block_size,
                price_precision=price_precision,
                tick_size=tick_size,
                block_multiplier=block_multiplier,
            )
        candles_out.append(block)
    out["candles"] = candles_out
    return out


def footprint_ws_mt5_spot_enabled(cfg: dict[str, Any]) -> bool:
    ws = cfg.get("footprint_ws")
    if not isinstance(ws, dict):
        return False
    if not footprint_ws_enabled(cfg):
        return False
    if ws.get("mt5_spot") is False:
        return False
    return True


def decode_ws_frames_merged(
    frames_dir: Path,
    *,
    export_format: str = FOOTPRINT_EXPORT_FORMAT_COMBINED,
) -> Optional[dict[str, Any]]:
    """Decode FOOTPRINT + TS/V2 OHLCV frames and merge by ``time_gmt7``."""
    if not frames_dir.is_dir():
        return None
    footprint_docs: list[dict[str, Any]] = []
    ohlc_docs: list[dict[str, Any]] = []
    for path in sorted(frames_dir.glob("*_recv.bin")):
        raw = path.read_bytes()
        fp = decode_ws_footprint_frame(raw, export_format=FOOTPRINT_EXPORT_FORMAT_RAW)
        if fp is not None:
            footprint_docs.append(fp)
        ohlc = decode_ws_ohlc_frame(raw)
        if ohlc is not None:
            ohlc_docs.append(ohlc)
    footprint_doc = pick_best_footprint_document(footprint_docs)
    if footprint_doc is None:
        return None
    if export_format == FOOTPRINT_EXPORT_FORMAT_RAW:
        return footprint_doc
    ohlc_index = build_ohlc_index(ohlc_docs)
    if export_format == FOOTPRINT_EXPORT_FORMAT_COMBINED:
        return merge_footprint_raw_with_ohlc(footprint_doc, ohlc_index)
    return footprint_doc


def footprint_response_to_raw_document(
    msg: pb.FootPrintForDateResponse,
    *,
    ws_type: Optional[str] = None,
) -> dict[str, Any]:
    """Full ``FootPrintForDateResponse`` as JSON with decoded prices per level."""
    price_precision = int(msg.fp_day.price_precision or 0)
    req = msg.request
    candles: list[dict[str, Any]] = []
    for candle in sorted(msg.candles, key=lambda c: c.date):
        footprint_levels: list[dict[str, Any]] = []
        for fp in sorted(candle.footprint, key=lambda f: f.level, reverse=True):
            footprint_levels.append(
                {
                    "level": int(fp.level),
                    "price": scaled_price(fp.level, price_precision),
                    "buy": {
                        "trades": int(fp.buy.trades),
                        "volume": int(fp.buy.volume),
                    },
                    "sell": {
                        "trades": int(fp.sell.trades),
                        "volume": int(fp.sell.volume),
                    },
                }
            )
        block: dict[str, Any] = {
            "date": candle.date,
            "time_gmt7": proto_candle_time_key(candle.date) if candle.date else "",
            "ending_summary": MessageToDict(
                candle.ending_summary, preserving_proto_field_name=True
            ),
            "totals": MessageToDict(candle.totals, preserving_proto_field_name=True),
            "max": MessageToDict(candle.max, preserving_proto_field_name=True),
            "min": MessageToDict(candle.min, preserving_proto_field_name=True),
            "footprint": footprint_levels,
        }
        candles.append(block)

    return {
        "ws_type": ws_type or "",
        "symbol": display_symbol_from_request(req),
        "request": MessageToDict(req, preserving_proto_field_name=True),
        "fp_day": MessageToDict(msg.fp_day, preserving_proto_field_name=True),
        "is_complete": bool(msg.is_complete),
        "version": int(msg.version),
        "candles": candles,
    }


def footprint_response_to_document(
    msg: pb.FootPrintForDateResponse,
    *,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
) -> dict[str, Any]:
    """Map ``FootPrintForDateResponse`` to flat OCR-style footprint JSON."""
    req = msg.request
    doc_symbol = (symbol or display_symbol_from_request(req)).strip()
    doc_timeframe = (timeframe or (req.interval or "")).strip()
    candles: list[dict[str, Any]] = []
    for candle in sorted(msg.candles, key=lambda c: c.date):
        price_levels: list[dict[str, Any]] = []
        for fp in sorted(candle.footprint, key=lambda f: f.level, reverse=True):
            price_levels.append(
                {
                    "bid": int(fp.buy.volume),
                    "ask": int(fp.sell.volume),
                }
            )
        candles.append(
            {
                "time": proto_candle_time_key(candle.date),
                "price_levels": price_levels,
            }
        )
    return {
        "symbol": doc_symbol,
        "timeframe": doc_timeframe,
        "type": FOOTPRINT_CHART_TYPE,
        "candles": candles,
    }


def footprint_raw_document_to_bid_ask(
    raw_doc: dict[str, Any],
    *,
    block_multiplier: int = 1,
    tick_size: float = DEFAULT_FOOTPRINT_TICK_SIZE,
    include_price: bool = False,
) -> dict[str, Any]:
    """Convert on-disk ``footprint_raw_*.json`` to flat bid/ask JSON."""
    req = raw_doc.get("request") if isinstance(raw_doc.get("request"), dict) else {}
    price_precision = _price_precision_from_doc(raw_doc)
    block_size = tick_size * block_multiplier if block_multiplier > 1 else 0.0

    candles_out: list[dict[str, Any]] = []
    for candle in raw_doc.get("candles") or []:
        if not isinstance(candle, dict):
            continue
        footprint = candle.get("footprint")
        if not isinstance(footprint, list):
            continue
        levels = [lvl for lvl in footprint if isinstance(lvl, dict)]
        if block_multiplier > 1:
            levels = aggregate_footprint_levels(
                levels,
                block_size=block_size,
                price_precision=price_precision,
                tick_size=tick_size,
                block_multiplier=block_multiplier,
            )
        price_levels: list[dict[str, Any]] = []
        for fp in sorted(levels, key=lambda f: float(f.get("price") or 0), reverse=True):
            buy = fp.get("buy") if isinstance(fp.get("buy"), dict) else {}
            sell = fp.get("sell") if isinstance(fp.get("sell"), dict) else {}
            row: dict[str, Any] = {
                "bid": int(buy.get("volume") or 0),
                "ask": int(sell.get("volume") or 0),
            }
            if include_price and fp.get("price") is not None:
                row["price"] = fp.get("price")
            price_levels.append(row)
        candles_out.append(
            {
                "time": str(candle.get("time_gmt7") or "").strip(),
                "price_levels": price_levels,
            }
        )

    return {
        "symbol": str(raw_doc.get("symbol") or "").strip(),
        "timeframe": str(req.get("interval") or "").strip(),
        "type": FOOTPRINT_CHART_TYPE,
        "block_multiplier": block_multiplier,
        "candles": candles_out,
    }


def decode_ws_footprint_frame(
    raw: bytes,
    *,
    export_format: str = FOOTPRINT_EXPORT_FORMAT_BID_ASK,
) -> Optional[dict[str, Any]]:
    """Decode one WS recv frame when it carries ``FOOTPRINT`` protobuf data."""
    parsed = parse_ws_binary_envelope(raw)
    if parsed is None:
        return None
    type_str, payload = parsed
    if "FOOTPRINT" not in type_str.upper():
        return None
    msg = decode_footprint_for_date_response(payload)
    if export_format == FOOTPRINT_EXPORT_FORMAT_RAW:
        return footprint_response_to_raw_document(msg, ws_type=type_str)
    return footprint_response_to_document(msg)


def decode_ws_frames_dir(
    frames_dir: Path,
    *,
    export_format: str = FOOTPRINT_EXPORT_FORMAT_BID_ASK,
) -> list[dict[str, Any]]:
    """Decode all ``*_recv.bin`` footprint frames under a sniff directory."""
    if not frames_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(frames_dir.glob("*_recv.bin")):
        doc = decode_ws_footprint_frame(path.read_bytes(), export_format=export_format)
        if doc is not None:
            out.append(doc)
    return out


def footprint_document_request_date(doc: dict[str, Any]) -> str:
    req = doc.get("request")
    if isinstance(req, dict):
        date = str(req.get("date") or "").strip()
        if date:
            return date
    return ""


def footprint_ws_extra_session_days(cfg: dict[str, Any]) -> int:
    """How many prior session dates to request via WS before ``max_candles`` trim."""
    ws = cfg.get("footprint_ws")
    if isinstance(ws, dict):
        raw = ws.get("extra_session_days")
        if raw is not None:
            try:
                return max(0, int(raw))
            except (TypeError, ValueError):
                pass
    return 1


def footprint_ws_extra_session_wait_ms(cfg: dict[str, Any]) -> int:
    ws = cfg.get("footprint_ws")
    if isinstance(ws, dict):
        raw = ws.get("extra_session_wait_ms")
        if raw is not None:
            try:
                return max(1000, int(raw))
            except (TypeError, ValueError):
                pass
    return footprint_ws_wait_ms(cfg)


def prior_session_dates_to_request(
    footprint_docs: list[dict[str, Any]],
    *,
    extra_days: int = 1,
) -> list[str]:
    """Return prior session dates missing from ``footprint_docs`` (oldest first)."""
    from datetime import date, datetime, timedelta, timezone

    if extra_days <= 0:
        return []

    existing = {
        footprint_document_request_date(d)
        for d in footprint_docs
        if footprint_document_request_date(d)
    }
    if existing:
        anchor = max(existing)
    else:
        anchor = datetime.now(timezone(timedelta(hours=7))).date().isoformat()

    anchor_date = date.fromisoformat(anchor)
    out: list[str] = []
    for offset in range(extra_days, 0, -1):
        prior = (anchor_date - timedelta(days=offset)).isoformat()
        if prior not in existing:
            out.append(prior)
    return out


def merge_footprint_ws_documents(docs: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Merge WS/IDB footprint docs: one session per ``request.date``, candles by ``time_gmt7``."""
    usable = [d for d in docs if isinstance(d, dict) and d.get("candles")]
    if not usable:
        return None
    if len(usable) == 1:
        return usable[0]

    by_date: dict[str, dict[str, Any]] = {}
    for doc in usable:
        rd = footprint_document_request_date(doc) or f"__anon_{id(doc)}"
        by_date[rd] = doc

    merged: dict[str, Any] = dict(usable[-1])
    by_time: dict[str, dict[str, Any]] = {}
    session_dates: list[str] = []
    for rd in sorted(by_date.keys()):
        if rd.startswith("__anon_"):
            continue
        session_dates.append(rd)
        for candle in by_date[rd].get("candles") or []:
            if not isinstance(candle, dict):
                continue
            time_key = str(candle.get("time_gmt7") or candle.get("time") or "").strip()
            if time_key:
                by_time[time_key] = candle

    merged["candles"] = sorted(
        by_time.values(),
        key=lambda c: str(c.get("time_gmt7") or c.get("time") or ""),
    )
    merged["ws_merged_from"] = len(usable)
    merged["ws_session_dates"] = session_dates
    return merged


def pick_best_footprint_document(docs: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Prefer merged multi-session docs, else the document with the most candles."""
    merged = merge_footprint_ws_documents(docs)
    if merged is not None and len(merged.get("ws_session_dates") or []) > 1:
        return merged
    if not docs:
        return None
    return max(docs, key=lambda d: len(d.get("candles") or []))


def document_timeframe(doc: dict[str, Any]) -> str:
    tf = doc.get("timeframe")
    if isinstance(tf, str) and tf.strip():
        return tf.strip().lower()
    req = doc.get("request")
    if isinstance(req, dict):
        interval = req.get("interval")
        if isinstance(interval, str) and interval.strip():
            return interval.strip().lower()
    return ""


def write_footprint_document(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


DEFAULT_FOOTPRINT_WS_MAX_CANDLES = 50
DEFAULT_FOOTPRINT_WS_WAIT_MS = 30_000


def footprint_ws_enabled(cfg: dict[str, Any]) -> bool:
    ws = cfg.get("footprint_ws")
    if not isinstance(ws, dict):
        return False
    return bool(ws.get("enabled"))


def footprint_ws_max_candles(cfg: dict[str, Any]) -> int:
    return footprint_ws_max_candles_from_cfg(cfg)


def footprint_ws_wait_ms(cfg: dict[str, Any]) -> int:
    ws = cfg.get("footprint_ws")
    if isinstance(ws, dict):
        raw = ws.get("wait_ms")
        if raw is not None:
            try:
                return max(1000, int(raw))
            except (TypeError, ValueError):
                pass
    return DEFAULT_FOOTPRINT_WS_WAIT_MS


def footprint_ws_export_format(cfg: dict[str, Any]) -> str:
    ws = cfg.get("footprint_ws")
    if isinstance(ws, dict):
        raw = str(ws.get("export_format") or "").strip().lower()
        if raw in FOOTPRINT_EXPORT_FORMATS:
            return raw
    return FOOTPRINT_EXPORT_FORMAT_COMBINED


def footprint_ws_interval_specs(cfg: dict[str, Any]) -> list[tuple[str, str]]:
    """Return ``[(interval, page_url), ...]`` from ``footprint_screenshot.intervals``."""
    fp = cfg.get("footprint_screenshot")
    if not isinstance(fp, dict):
        return []
    intervals = fp.get("intervals")
    if not isinstance(intervals, dict):
        return []
    out: list[tuple[str, str]] = []
    for key, value in intervals.items():
        iv = str(key).strip().lower()
        if not iv:
            continue
        if not isinstance(value, dict):
            continue
        url = str(value.get("page_url") or "").strip()
        if url:
            out.append((iv, url))
    out.sort(key=lambda pair: (0 if pair[0] == "15m" else 1, pair[0]))
    return out


def footprint_ws_max_candles_from_cfg(cfg: dict[str, Any]) -> int:
    """``footprint_ws.max_candles`` with env ``FOOTPRINT_WS_MAX_CANDLES`` override."""
    raw_env = os.getenv("FOOTPRINT_WS_MAX_CANDLES", "").strip()
    if raw_env.isdigit():
        return max(1, int(raw_env))
    ws = cfg.get("footprint_ws")
    if isinstance(ws, dict):
        raw = ws.get("max_candles")
        if raw is not None:
            try:
                return max(1, int(raw))
            except (TypeError, ValueError):
                pass
    return DEFAULT_FOOTPRINT_WS_MAX_CANDLES


def _interval_minutes_from_str(interval: str) -> int:
    iv = (interval or "").strip().lower()
    if iv.endswith("m"):
        return max(1, int(iv[:-1]))
    raise ValueError(f"unsupported interval {interval!r}")


def _forming_candle_open(now: datetime, interval_min: int) -> datetime:
    """Floor naive GMT+7 ``now`` to the open of the current forming candle."""
    floored_minute = (now.minute // interval_min) * interval_min
    return now.replace(minute=floored_minute, second=0, microsecond=0)


def _last_candle_time_key(candle: dict[str, Any]) -> str:
    for key in ("time_gmt7", "time"):
        raw = candle.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


def _is_forming_footprint_candle(
    last_time_key: str,
    *,
    forming_open: datetime,
) -> bool | None:
    """Return True if forming, False if closed, None if unparsable."""
    from automation_tool.gocharting_footprint_ocr import parse_footprint_candle_datetime

    if re.match(r"^\d{1,2}:\d{2}$", last_time_key):
        hour, minute = map(int, last_time_key.split(":"))
        last_open = forming_open.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return last_open >= forming_open

    dt = parse_footprint_candle_datetime(last_time_key)
    if dt is None:
        return None
    last_open = dt.replace(second=0, microsecond=0)
    return last_open >= forming_open


def drop_forming_footprint_candle(
    doc: dict[str, Any],
    *,
    interval: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Remove the trailing candle if it is still forming (not yet closed)."""
    candles_raw = doc.get("candles")
    if not isinstance(candles_raw, list) or len(candles_raw) <= 1:
        return doc

    iv = (interval or document_timeframe(doc) or "").strip().lower()
    if not iv:
        if doc.get("is_complete") is False:
            out = dict(doc)
            out["candles"] = candles_raw[:-1]
            return out
        return doc

    try:
        interval_min = _interval_minutes_from_str(iv)
    except ValueError:
        if doc.get("is_complete") is False:
            out = dict(doc)
            out["candles"] = candles_raw[:-1]
            return out
        return doc

    ref = now if now is not None else datetime.now(_TZ_GMT7)
    if ref.tzinfo is not None:
        ref = ref.astimezone(_TZ_GMT7).replace(tzinfo=None)
    forming_open = _forming_candle_open(ref, interval_min)

    last = candles_raw[-1]
    if not isinstance(last, dict):
        return doc

    forming = _is_forming_footprint_candle(
        _last_candle_time_key(last),
        forming_open=forming_open,
    )
    if forming is True or (forming is None and doc.get("is_complete") is False):
        out = dict(doc)
        out["candles"] = candles_raw[:-1]
        return out
    return doc


def trim_footprint_document(
    doc: dict[str, Any],
    *,
    max_candles: int | None = None,
) -> dict[str, Any]:
    """Keep at most ``max_candles`` newest footprint candles (on-disk list is oldest-first)."""
    mc = max_candles if max_candles is not None else DEFAULT_FOOTPRINT_WS_MAX_CANDLES
    if mc <= 0:
        return doc
    candles_raw = doc.get("candles")
    if not isinstance(candles_raw, list) or len(candles_raw) <= mc:
        return doc
    out = dict(doc)
    out["candles"] = candles_raw[-mc:]
    return out
