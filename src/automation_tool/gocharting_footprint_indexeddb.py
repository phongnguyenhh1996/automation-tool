from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import Page

from automation_tool.gocharting_footprint_extract import (
    FOOTPRINT_CHART_TYPE,
    footprint_json_output_path,
    resolve_gocharting_chart_info,
    validate_footprint_extract_json,
    write_footprint_extract_json,
)
from automation_tool.proto import footprint_pb2

_log = logging.getLogger(__name__)


class GoChartingIndexedDBFootprintError(RuntimeError):
    """IndexedDB footprint extraction failed or returned no usable candles."""


_GOCHARTING_IDB_NAME = "GoChartingData"
_GOCHARTING_IDB_VERSION = 3
_BINARY_FOOTPRINT_STORE = "BinaryFootprint"
_DEFAULT_WAIT_MS = 45_000
_DEFAULT_POLL_MS = 1_500
_IMBALANCE_RATIO = 3.0

_READ_INDEXEDDB_JS = """
async () => {
    const db = await new Promise((resolve, reject) => {
        const req = indexedDB.open(%(db_name)r, %(db_version)d);
        req.onerror = () => reject(req.error || new Error('indexedDB.open failed'));
        req.onsuccess = () => resolve(req.result);
    });
    try {
        if (!db.objectStoreNames.contains(%(store)r)) {
            return [];
        }
        const tx = db.transaction(%(store)r, 'readonly');
        const store = tx.objectStore(%(store)r);
        const keys = await new Promise((resolve, reject) => {
            const req = store.getAllKeys();
            req.onsuccess = () => resolve(req.result || []);
            req.onerror = () => reject(req.error);
        });
        const values = await new Promise((resolve, reject) => {
            const req = store.getAll();
            req.onsuccess = () => resolve(req.result || []);
            req.onerror = () => reject(req.error);
        });
        const out = [];
        for (let i = 0; i < keys.length; i++) {
            const buf = values[i];
            if (!buf) continue;
            const bytes = new Uint8Array(buf);
            let binary = '';
            const chunk = 0x8000;
            for (let j = 0; j < bytes.length; j += chunk) {
                binary += String.fromCharCode.apply(null, bytes.subarray(j, j + chunk));
            }
            out.push({ key: String(keys[i]), data_b64: btoa(binary) });
        }
        return out;
    } finally {
        db.close();
    }
}
""" % {
    "db_name": _GOCHARTING_IDB_NAME,
    "db_version": _GOCHARTING_IDB_VERSION,
    "store": _BINARY_FOOTPRINT_STORE,
}


def resolve_footprint_symbol(entry: dict[str, Any]) -> str:
    """Ticker used in IndexedDB keys (e.g. ``GC1!``)."""
    q = str(entry.get("search_query") or "").strip()
    if q:
        return q
    return str(entry.get("export_label") or "").strip().upper()


def parse_footprint_indexeddb_key(key: str) -> Optional[dict[str, str]]:
    """
    Parse ``exchange:segment:symbol:interval:date:session`` keys from GoCharting cache.
    """
    parts = (key or "").split(":")
    if len(parts) < 6:
        return None
    return {
        "exchange": parts[0],
        "segment": parts[1],
        "symbol": parts[2],
        "interval": parts[3],
        "date": parts[4],
        "session": ":".join(parts[5:]),
    }


def footprint_key_matches(
    key: str,
    *,
    symbol: str,
    interval: str,
) -> bool:
    parsed = parse_footprint_indexeddb_key(key)
    if not parsed:
        return False
    sym = (symbol or "").strip().upper()
    iv = (interval or "").strip().lower()
    return parsed["symbol"].upper() == sym and parsed["interval"].lower() == iv


def read_binary_footprint_records(page: Page) -> list[dict[str, str]]:
    """Return ``[{key, data_b64}, ...]`` from GoCharting IndexedDB."""
    raw = page.evaluate(_READ_INDEXEDDB_JS)
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        data_b64 = str(item.get("data_b64") or "").strip()
        if key and data_b64:
            out.append({"key": key, "data_b64": data_b64})
    return out


def wait_for_footprint_records(
    page: Page,
    *,
    symbol: str,
    interval: str,
    wait_ms: int = _DEFAULT_WAIT_MS,
    poll_ms: int = _DEFAULT_POLL_MS,
) -> list[dict[str, str]]:
    """Poll IndexedDB until footprint rows exist for symbol+interval or timeout."""
    if wait_ms <= 0:
        records = read_binary_footprint_records(page)
        return [r for r in records if footprint_key_matches(r["key"], symbol=symbol, interval=interval)]

    remaining = wait_ms
    while remaining > 0:
        records = read_binary_footprint_records(page)
        matched = [r for r in records if footprint_key_matches(r["key"], symbol=symbol, interval=interval)]
        if matched:
            _log.info(
                "gocharting-indexeddb: found %d footprint cache row(s) for %s %s",
                len(matched),
                symbol,
                interval,
            )
            return matched
        step = min(poll_ms, remaining)
        page.wait_for_timeout(step)
        remaining -= step

    _log.warning(
        "gocharting-indexeddb: no footprint cache for %s %s after %dms (keys=%s)",
        symbol,
        interval,
        wait_ms,
        [r["key"] for r in read_binary_footprint_records(page)][:10],
    )
    return []


def decode_footprint_for_date_response(data: bytes) -> footprint_pb2.FootPrintForDateResponse:
    msg = footprint_pb2.FootPrintForDateResponse()
    msg.ParseFromString(data)
    return msg


def _scale_int(value: int, precision: int) -> int:
    if precision <= 0:
        return int(value)
    scaled = int(value) / (10**precision)
    if scaled == int(scaled):
        return int(scaled)
    return int(round(scaled))


def _scale_price(level: int, fp_day: footprint_pb2.FootPrintDay) -> float:
    pp = int(fp_day.price_precision or 0)
    if pp <= 0:
        return float(level)
    return int(level) / (10**pp)


def _candle_time_label(date_str: str) -> str:
    raw = (date_str or "").strip()
    if not raw:
        return ""
    try:
        if "T" in raw:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.strftime("%H:%M")
    except ValueError:
        pass
    m = re.search(r"T(\d{2}:\d{2})", raw)
    if m:
        return m.group(1)
    m = re.search(r"(\d{2}:\d{2})", raw)
    if m:
        return m.group(1)
    return raw


def _level_attributes(bid: int, ask: int) -> list[str]:
    if bid <= 0 and ask <= 0:
        return []
    if bid > 0 and ask >= bid * _IMBALANCE_RATIO:
        return ["imbalance"]
    if ask > 0 and bid >= ask * _IMBALANCE_RATIO:
        return ["imbalance"]
    return []


def footprint_response_to_extract_payload(
    response: footprint_pb2.FootPrintForDateResponse,
    *,
    chart_info: dict[str, str],
) -> dict[str, Any]:
    fp_day = response.fp_day
    pp = int(fp_day.price_precision or 0)
    sp = int(fp_day.size_precision or 0)

    candles_out: list[dict[str, Any]] = []
    for candle in response.candles:
        time_label = _candle_time_label(candle.date)
        if not time_label:
            continue

        levels: list[dict[str, Any]] = []
        for fp in candle.footprint:
            bid = _scale_int(fp.buy.volume if fp.buy else 0, sp)
            ask = _scale_int(fp.sell.volume if fp.sell else 0, sp)
            if bid == 0 and ask == 0:
                continue
            levels.append(
                {
                    "bid": bid,
                    "ask": ask,
                    "attributes": _level_attributes(bid, ask),
                    "_price": _scale_price(fp.level, fp_day),
                }
            )

        if not levels:
            continue

        levels.sort(key=lambda x: float(x["_price"]), reverse=True)
        for lv in levels:
            del lv["_price"]

        candles_out.append({"time": time_label, "price_levels": levels})

    payload = {
        "chart_info": {
            "symbol": chart_info.get("symbol", ""),
            "timeframe": chart_info.get("timeframe", ""),
            "type": chart_info.get("type", FOOTPRINT_CHART_TYPE),
        },
        "candles": candles_out,
    }
    return validate_footprint_extract_json(payload)


def merge_footprint_payloads(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    if not payloads:
        raise ValueError("No footprint payloads to merge")
    if len(payloads) == 1:
        return payloads[0]

    chart_info = payloads[0]["chart_info"]
    by_time: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        for candle in payload.get("candles") or []:
            t = str(candle.get("time") or "").strip()
            if not t:
                continue
            by_time[t] = candle

    def _sort_key(time_s: str) -> tuple[int, str]:
        try:
            hh, mm = time_s.split(":", 1)
            return int(hh) * 60 + int(mm), time_s
        except ValueError:
            return 99999, time_s

    candles = [by_time[k] for k in sorted(by_time.keys(), key=_sort_key)]
    merged = {
        "chart_info": chart_info,
        "candles": candles,
    }
    return validate_footprint_extract_json(merged)


def records_to_footprint_payload(
    records: list[dict[str, str]],
    *,
    chart_info: dict[str, str],
) -> Optional[dict[str, Any]]:
    if not records:
        return None

    payloads: list[dict[str, Any]] = []
    for rec in records:
        try:
            raw = base64.b64decode(rec["data_b64"])
            response = decode_footprint_for_date_response(raw)
        except Exception as exc:
            _log.warning(
                "gocharting-indexeddb: decode failed for key=%s: %s",
                rec.get("key"),
                exc,
            )
            continue
        if not response.candles:
            continue
        payloads.append(
            footprint_response_to_extract_payload(response, chart_info=chart_info)
        )

    if not payloads:
        return None
    return merge_footprint_payloads(payloads)


def extract_footprint_json_from_page(
    page: Page,
    *,
    entry: dict[str, Any],
    interval: str,
    chart_info: dict[str, str],
    wait_ms: int = _DEFAULT_WAIT_MS,
    poll_ms: int = _DEFAULT_POLL_MS,
) -> Optional[dict[str, Any]]:
    symbol = resolve_footprint_symbol(entry)
    records = wait_for_footprint_records(
        page,
        symbol=symbol,
        interval=interval,
        wait_ms=wait_ms,
        poll_ms=poll_ms,
    )
    return records_to_footprint_payload(records, chart_info=chart_info)


def _indexeddb_failure_message(
    *,
    symbol: str,
    interval: str,
    wait_ms: int,
    reason: str,
) -> str:
    return (
        f"GoCharting IndexedDB footprint failed for {symbol} {interval} "
        f"(waited {wait_ms}ms): {reason}"
    )


def require_footprint_json_path(path: Path, *, interval: str) -> dict[str, Any]:
    """Load footprint JSON from disk; raise if missing or has no candles."""
    if not path.is_file():
        raise GoChartingIndexedDBFootprintError(
            f"IndexedDB footprint JSON missing for {interval}: {path}"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoChartingIndexedDBFootprintError(
            f"IndexedDB footprint JSON unreadable for {interval}: {path}"
        ) from exc
    validated = validate_footprint_extract_json(data)
    if not validated.get("candles"):
        raise GoChartingIndexedDBFootprintError(
            f"IndexedDB footprint JSON has no candles for {interval}: {path.name}"
        )
    return validated


def write_footprint_json_from_page(
    page: Page,
    *,
    output_dir: Path,
    entry: dict[str, Any],
    interval: str,
    instrument_slug: str,
    chart_info: dict[str, str],
    wait_ms: int = _DEFAULT_WAIT_MS,
    required: bool = False,
) -> Optional[Path]:
    symbol = resolve_footprint_symbol(entry)
    payload = extract_footprint_json_from_page(
        page,
        entry=entry,
        interval=interval,
        chart_info=chart_info,
        wait_ms=wait_ms,
    )
    out_path = footprint_json_output_path(output_dir, interval, instrument_slug)
    if payload is None:
        if required:
            raise GoChartingIndexedDBFootprintError(
                _indexeddb_failure_message(
                    symbol=symbol,
                    interval=interval,
                    wait_ms=wait_ms,
                    reason="no matching BinaryFootprint rows in IndexedDB",
                )
            )
        return None

    if not payload.get("candles"):
        if required:
            raise GoChartingIndexedDBFootprintError(
                _indexeddb_failure_message(
                    symbol=symbol,
                    interval=interval,
                    wait_ms=wait_ms,
                    reason="footprint rows decoded but candles list is empty",
                )
            )
        return None

    write_footprint_extract_json(out_path, payload)
    _log.info(
        "gocharting-indexeddb: wrote %s (%d candles)",
        out_path,
        len(payload.get("candles") or []),
    )
    return out_path
