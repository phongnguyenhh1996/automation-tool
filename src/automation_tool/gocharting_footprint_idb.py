"""Read GoCharting footprint protobuf cache from browser IndexedDB (``GoChartingData``)."""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from playwright.sync_api import Page

from automation_tool.gocharting_ws_decode import (
    FOOTPRINT_EXPORT_FORMAT_BID_ASK,
    FOOTPRINT_EXPORT_FORMAT_COMBINED,
    FOOTPRINT_EXPORT_FORMAT_RAW,
    decode_footprint_for_date_response,
    document_timeframe,
    footprint_response_to_document,
    footprint_response_to_raw_document,
    merge_footprint_raw_with_ohlc,
)

_log = logging.getLogger(__name__)

IDB_DB_NAME = "GoChartingData"
IDB_DB_VERSION = 3
IDB_STORE = "BinaryFootprint"

_TZ_GMT7 = timezone(timedelta(hours=7))

_READ_IDB_JS = """
async ({ dbName, dbVersion, storeName, date, interval, keySubstring }) => {
  const openDb = () => new Promise((resolve, reject) => {
    const req = indexedDB.open(dbName, dbVersion);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error || new Error("indexedDB.open failed"));
    req.onblocked = () => reject(new Error("indexedDB.open blocked"));
  });

  const db = await openDb();
  const tx = db.transaction(storeName, "readonly");
  const store = tx.objectStore(storeName);
  const keys = await new Promise((resolve, reject) => {
    const req = store.getAllKeys();
    req.onsuccess = () => resolve(req.result || []);
    req.onerror = () => reject(req.error || new Error("getAllKeys failed"));
  });

  const keyStrings = keys.map((k) => String(k));
  const matches = keyStrings.filter((key) => {
    if (date && !key.includes(`:${date}:`) && !key.endsWith(`:${date}`)) return false;
    if (interval) {
      const needle = `:${interval}:`;
      if (!key.includes(needle)) return false;
    }
    if (keySubstring && !key.includes(keySubstring)) return false;
    return true;
  });

  const entries = [];
  for (const key of matches) {
    const value = await new Promise((resolve, reject) => {
      const req = store.get(key);
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error || new Error(`get(${key}) failed`));
    });
    if (!value) continue;
    const bytes = value instanceof ArrayBuffer ? new Uint8Array(value) : new Uint8Array(value.buffer || value);
    let binary = "";
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    entries.push({ key, data_b64: btoa(binary), byte_len: bytes.length });
  }

  return { all_keys: keyStrings, matched_keys: matches, entries };
}
"""


def parse_idb_footprint_key(key: str) -> dict[str, str]:
    """Parse ``exchange:segment:symbol:interval:date:session``."""
    parts = (key or "").split(":")
    out: dict[str, str] = {"key": key}
    if len(parts) >= 6:
        out.update(
            {
                "exchange": parts[0],
                "segment": parts[1],
                "symbol": parts[2],
                "interval": parts[3],
                "date": parts[4],
                "session": parts[5],
            }
        )
    return out


def default_idb_lookup_dates(*, extra_days: int = 1) -> list[str]:
    """Return today and prior session dates (GMT+7) for IndexedDB lookup."""
    today = datetime.now(_TZ_GMT7).date()
    dates = [today.isoformat()]
    for offset in range(1, max(1, extra_days) + 1):
        dates.append((today - timedelta(days=offset)).isoformat())
    return dates


def footprint_idb_enabled(cfg: dict[str, Any]) -> bool:
    ws = cfg.get("footprint_ws")
    if not isinstance(ws, dict):
        return False
    idb = ws.get("idb")
    if isinstance(idb, dict):
        return bool(idb.get("enabled", True))
    return bool(ws.get("idb_enabled", True))


def footprint_idb_lookup_days(cfg: dict[str, Any]) -> int:
    ws = cfg.get("footprint_ws")
    if isinstance(ws, dict):
        idb = ws.get("idb")
        if isinstance(idb, dict) and idb.get("lookup_days") is not None:
            try:
                return max(1, int(idb["lookup_days"]))
            except (TypeError, ValueError):
                pass
    return 2


def decode_idb_footprint_bytes(
    payload: bytes,
    *,
    export_format: str,
    idb_key: str = "",
) -> dict[str, Any]:
    msg = decode_footprint_for_date_response(payload)
    ws_type = "INDEXEDDB"
    if idb_key:
        ws_type = f"INDEXEDDB/{idb_key}"
    if export_format in (FOOTPRINT_EXPORT_FORMAT_RAW, FOOTPRINT_EXPORT_FORMAT_COMBINED):
        doc = footprint_response_to_raw_document(msg, ws_type=ws_type)
    else:
        doc = footprint_response_to_document(msg)
    if idb_key:
        doc["idb_key"] = idb_key
    return doc


def merge_footprint_documents(docs: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Merge multiple footprint docs (WS chunks or IDB days) by candle time."""
    usable = [d for d in docs if isinstance(d, dict) and d.get("candles")]
    if not usable:
        return None
    if len(usable) == 1:
        return usable[0]

    merged: dict[str, Any] = dict(usable[-1])
    by_time: dict[str, dict[str, Any]] = {}
    for doc in usable:
        for candle in doc.get("candles") or []:
            if not isinstance(candle, dict):
                continue
            time_key = str(
                candle.get("time_gmt7") or candle.get("time") or ""
            ).strip()
            if not time_key:
                continue
            by_time[time_key] = candle

    candles = sorted(by_time.values(), key=lambda c: str(c.get("time_gmt7") or c.get("time") or ""))
    merged["candles"] = candles
    merged["idb_merged_from"] = len(usable)
    return merged


def _merge_idb_probes(*probes: dict[str, Any]) -> dict[str, Any]:
    all_keys: list[str] = []
    matched_keys: list[str] = []
    entries_by_key: dict[str, dict[str, Any]] = {}
    for probe in probes:
        if not isinstance(probe, dict):
            continue
        for key in probe.get("all_keys") or []:
            ks = str(key)
            if ks not in all_keys:
                all_keys.append(ks)
        for key in probe.get("matched_keys") or []:
            ks = str(key)
            if ks not in matched_keys:
                matched_keys.append(ks)
        for entry in probe.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("key") or "")
            if key:
                entries_by_key[key] = entry
    return {
        "all_keys": all_keys,
        "matched_keys": matched_keys,
        "entries": list(entries_by_key.values()),
    }


def read_footprint_idb_on_page(
    page: Page,
    *,
    date: str | None = None,
    interval: str | None = None,
    key_substring: str | None = None,
) -> dict[str, Any]:
    """List keys and read matching ``BinaryFootprint`` entries from chart origin."""
    args = {
        "dbName": IDB_DB_NAME,
        "dbVersion": IDB_DB_VERSION,
        "storeName": IDB_STORE,
        "date": date,
        "interval": interval,
        "keySubstring": key_substring,
    }
    probes: list[dict[str, Any]] = []
    try:
        probes.append(page.evaluate(_READ_IDB_JS, args))
    except Exception as exc:
        _log.warning("footprint_idb: main-frame read failed: %s", exc)

    for worker in page.workers:
        try:
            probes.append(worker.evaluate(_READ_IDB_JS, args))
        except Exception as exc:
            _log.debug("footprint_idb: worker read failed: %s", exc)

    if not probes:
        return {"all_keys": [], "matched_keys": [], "entries": []}
    return _merge_idb_probes(*probes)


def idb_probe_to_documents(
    probe: dict[str, Any],
    *,
    export_format: str,
    interval: str | None = None,
) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for entry in probe.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "")
        meta = parse_idb_footprint_key(key)
        if interval and meta.get("interval", "").lower() != interval.strip().lower():
            continue
        b64 = entry.get("data_b64")
        if not isinstance(b64, str) or not b64:
            continue
        try:
            payload = base64.b64decode(b64)
            doc = decode_idb_footprint_bytes(payload, export_format=export_format, idb_key=key)
        except Exception as exc:
            _log.warning("footprint_idb: decode failed for %s: %s", key, exc)
            continue
        docs.append(doc)
        _log.info(
            "footprint_idb: %s %s date=%s candles=%d bytes=%s",
            doc.get("symbol"),
            document_timeframe(doc) or meta.get("interval"),
            meta.get("date"),
            len(doc.get("candles") or []),
            entry.get("byte_len"),
        )
    return docs


def capture_footprint_idb_documents(
    page: Page,
    *,
    cfg: dict[str, Any],
    interval: str,
    dates: list[str] | None = None,
    export_format: str,
    key_substring: str | None = None,
) -> list[dict[str, Any]]:
    """Read footprint cache for each date (and optional symbol filter) from IndexedDB."""
    if not footprint_idb_enabled(cfg):
        return []

    iv = interval.strip().lower()
    lookup_dates = dates or default_idb_lookup_dates(extra_days=footprint_idb_lookup_days(cfg))
    docs: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for session_date in lookup_dates:
        try:
            probe = read_footprint_idb_on_page(
                page,
                date=session_date,
                interval=iv,
                key_substring=key_substring,
            )
        except Exception as exc:
            _log.warning("footprint_idb: read failed for %s: %s", session_date, exc)
            continue

        all_keys = probe.get("all_keys") or []
        matched = probe.get("matched_keys") or []
        _log.info(
            "footprint_idb: date=%s interval=%s keys=%d matched=%d",
            session_date,
            iv,
            len(all_keys),
            len(matched),
        )
        if not matched and session_date == lookup_dates[0]:
            sample = [str(k) for k in all_keys[:8]]
            if sample:
                _log.info("footprint_idb: sample keys: %s", sample)

        for doc in idb_probe_to_documents(probe, export_format=export_format, interval=iv):
            key = str(doc.get("idb_key") or "")
            if key and key in seen_keys:
                continue
            if key:
                seen_keys.add(key)
            docs.append(doc)

    return docs


def build_output_with_idb(
    *,
    footprint_docs: list[dict[str, Any]],
    ohlc_docs: list[dict[str, Any]],
    idb_docs: list[dict[str, Any]],
    export_format: str,
) -> dict[str, Any]:
    """Prefer merged WS+IDB candles; fall back to IDB-only when WS empty."""
    from automation_tool.gocharting_ws_decode import build_ohlc_index, pick_best_footprint_document

    ws_best = pick_best_footprint_document(footprint_docs)
    merged_fp = merge_footprint_documents(([ws_best] if ws_best else []) + idb_docs)
    if merged_fp is None:
        raise RuntimeError(
            "No footprint data from WebSocket or IndexedDB. "
            "Open chart in a persistent Chrome profile or increase footprint_ws.wait_ms."
        )
    if export_format == FOOTPRINT_EXPORT_FORMAT_COMBINED:
        ohlc_index = build_ohlc_index(ohlc_docs)
        return merge_footprint_raw_with_ohlc(merged_fp, ohlc_index)
    if export_format == FOOTPRINT_EXPORT_FORMAT_RAW:
        return merged_fp
    # bid_ask: convert if raw-shaped doc came from IDB merge
    if merged_fp.get("request") and merged_fp.get("fp_day"):
        from automation_tool.gocharting_ws_decode import footprint_raw_document_to_bid_ask

        return footprint_raw_document_to_bid_ask(merged_fp)
    return merged_fp
