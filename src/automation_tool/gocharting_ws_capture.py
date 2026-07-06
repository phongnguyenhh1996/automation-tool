from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import BrowserContext, Page

from automation_tool.gocharting_footprint_idb import (
    build_output_with_idb,
    capture_footprint_idb_documents,
    default_idb_lookup_dates,
    footprint_idb_enabled,
    footprint_idb_retry_wait_ms,
    idb_probe_to_documents,
    merge_footprint_documents,
    read_footprint_idb_on_page,
)
from automation_tool.gocharting_footprint_ws_request import request_footprint_dates_on_page
from automation_tool.gocharting_footprint_ocr import footprint_images_dir, footprint_interval_json_path
from automation_tool.gocharting_ws_decode import (
    FOOTPRINT_EXPORT_FORMAT_BID_ASK,
    FOOTPRINT_EXPORT_FORMAT_COMBINED,
    FOOTPRINT_EXPORT_FORMAT_RAW,
    build_ohlc_index,
    candle_sort_datetime,
    decode_ws_footprint_frame,
    decode_ws_ohlc_frame,
    document_timeframe,
    footprint_combined_json_path,
    footprint_document_request_date,
    footprint_last_candle_fresh,
    footprint_raw_json_path,
    footprint_ws_export_format,
    footprint_ws_extra_session_days,
    footprint_ws_extra_session_wait_ms,
    footprint_ws_interval_specs,
    footprint_ws_max_candles,
    footprint_ws_min_ready_candles,
    footprint_ws_poll_ms,
    footprint_ws_wait_ms,
    latest_closed_candle_open_for_interval,
    last_closed_candle_open,
    merge_footprint_raw_with_ohlc,
    merge_footprint_with_mt5_spot,
    merge_footprint_ws_documents,
    pick_best_footprint_document,
    prior_session_dates_to_request,
    trim_footprint_document,
    footprint_ws_mt5_spot_enabled,
    write_footprint_document,
)

_log = logging.getLogger(__name__)


class FootprintCaptureStaleError(RuntimeError):
    """Raised when captured footprint candles are older than the expected closed bar."""


def _frame_bytes(frame) -> bytes:
    payload = frame.payload if hasattr(frame, "payload") else frame
    if isinstance(payload, str):
        return payload.encode("utf-8", errors="replace")
    return bytes(payload)


def _resolve_output_path(
    *,
    fp_dir: Path,
    interval: str,
    export_format: str,
    out_path: Path | None,
) -> Path:
    if out_path is not None:
        return out_path
    if export_format == FOOTPRINT_EXPORT_FORMAT_COMBINED:
        return footprint_combined_json_path(fp_dir, interval)
    if export_format == FOOTPRINT_EXPORT_FORMAT_RAW:
        return footprint_raw_json_path(fp_dir, interval)
    return footprint_interval_json_path(fp_dir, interval)


def _attach_ws_listeners(
    page: Page,
    *,
    interval: str,
    export_format: str,
    footprint_docs: list[dict[str, Any]],
    ohlc_docs: list[dict[str, Any]],
) -> None:
    def on_websocket(ws) -> None:
        def on_recv(frame) -> None:
            raw = _frame_bytes(frame)
            fp_format = (
                FOOTPRINT_EXPORT_FORMAT_RAW
                if export_format
                in (FOOTPRINT_EXPORT_FORMAT_RAW, FOOTPRINT_EXPORT_FORMAT_COMBINED)
                else FOOTPRINT_EXPORT_FORMAT_BID_ASK
            )
            fp_doc = decode_ws_footprint_frame(raw, export_format=fp_format)
            if fp_doc is not None:
                if interval and document_timeframe(fp_doc) != interval:
                    return
                footprint_docs.append(fp_doc)
                _log.info(
                    "FOOTPRINT frame: %s %s date=%s candles=%d",
                    fp_doc.get("symbol"),
                    document_timeframe(fp_doc) or interval,
                    footprint_document_request_date(fp_doc) or "?",
                    len(fp_doc.get("candles") or []),
                )
            ohlc_doc = decode_ws_ohlc_frame(raw)
            if ohlc_doc is not None:
                ohlc_docs.append(ohlc_doc)
                _log.info(
                    "OHLC frame: %d bars (ws_type=%s)",
                    len(ohlc_doc.get("candles") or []),
                    ohlc_doc.get("ws_type"),
                )

        ws.on("framereceived", on_recv)

    page.on("websocket", on_websocket)


def _idb_export_format(fmt: str) -> str:
    if fmt in (FOOTPRINT_EXPORT_FORMAT_RAW, FOOTPRINT_EXPORT_FORMAT_COMBINED):
        return FOOTPRINT_EXPORT_FORMAT_RAW
    return FOOTPRINT_EXPORT_FORMAT_BID_ASK


def _ws_ready_stats(footprint_docs: list[dict[str, Any]]) -> tuple[int, bool]:
    best = pick_best_footprint_document(footprint_docs)
    if best is None:
        return 0, False
    count = len(best.get("candles") or [])
    complete = bool(best.get("is_complete"))
    if not complete and footprint_docs:
        complete = any(bool(d.get("is_complete")) for d in footprint_docs if isinstance(d, dict))
    return count, complete


def _last_candle_open(candles: list[Any]) -> datetime | None:
    valid = [c for c in candles if isinstance(c, dict)]
    if not valid:
        return None
    last_open = candle_sort_datetime(max(valid, key=candle_sort_datetime))
    if last_open <= datetime.min:
        return None
    return last_open


def _ws_last_candle_open(
    footprint_docs: list[dict[str, Any]],
    *,
    interval: str | None = None,
    now: datetime | None = None,
) -> datetime | None:
    best = pick_best_footprint_document(footprint_docs)
    if best is None:
        return None
    iv = (interval or document_timeframe(best) or "").strip().lower()
    if iv:
        return last_closed_candle_open(best, interval=iv, now=now)
    candles = best.get("candles") or []
    if not isinstance(candles, list):
        return None
    return _last_candle_open(candles)


def footprint_capture_session_dates(
    footprint_docs: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> list[str]:
    """Calendar dates to subscribe: today (GMT+7) plus dates seen in WS frames."""
    ref = now
    if ref is None:
        ref = datetime.now(timezone(timedelta(hours=7))).replace(tzinfo=None)
    dates: list[str] = [ref.date().isoformat()]
    for doc in footprint_docs:
        rd = footprint_document_request_date(doc)
        if rd and rd not in dates:
            dates.append(rd)
    return dates


def _assert_output_fresh(
    output_doc: dict[str, Any],
    *,
    interval: str,
    expected_closed_open: datetime,
    wait_source: str,
    now: datetime | None = None,
) -> None:
    last_open = last_closed_candle_open(output_doc, interval=interval, now=now)
    if footprint_last_candle_fresh(last_open, expected_closed_open):
        return
    raise FootprintCaptureStaleError(
        f"Footprint capture stale for {interval}: last_closed={last_open} "
        f"expected>={expected_closed_open} wait_source={wait_source}"
    )


def footprint_ws_data_ready(
    *,
    footprint_docs: list[dict[str, Any]],
    idb_candle_count: int,
    min_candles: int,
    interval: str = "",
    idb_last_open: datetime | None = None,
    expected_closed_open: datetime | None = None,
    now: datetime | None = None,
) -> bool:
    """True when IndexedDB or WebSocket has enough fresh footprint candles to proceed."""
    idb_fresh = footprint_last_candle_fresh(idb_last_open, expected_closed_open)
    if idb_candle_count >= min_candles and idb_fresh:
        return True
    ws_count, ws_complete = _ws_ready_stats(footprint_docs)
    iv = interval.strip().lower() if interval else None
    ws_last = _ws_last_candle_open(
        footprint_docs,
        interval=iv if expected_closed_open and iv else None,
        now=now,
    )
    ws_fresh = footprint_last_candle_fresh(ws_last, expected_closed_open)
    if ws_count < min_candles:
        return False
    if expected_closed_open is None:
        return ws_complete
    return ws_fresh


def _idb_snapshot_on_page(
    page: Page,
    *,
    cfg: dict[str, Any],
    interval: str,
    export_format: str,
    lookup_dates: list[str],
) -> tuple[int, datetime | None]:
    iv = interval.strip().lower()
    idb_fmt = _idb_export_format(export_format)
    docs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for session_date in lookup_dates:
        try:
            probe = read_footprint_idb_on_page(page, date=session_date, interval=iv)
        except Exception as exc:
            _log.debug("footprint_idb poll: read failed for %s: %s", session_date, exc)
            continue
        for doc in idb_probe_to_documents(probe, export_format=idb_fmt, interval=iv):
            key = str(doc.get("idb_key") or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            docs.append(doc)
    merged = merge_footprint_documents(docs)
    if not merged and iv:
        try:
            probe = read_footprint_idb_on_page(page, date=None, interval=iv)
        except Exception as exc:
            _log.debug("footprint_idb poll: interval-only read failed: %s", exc)
        else:
            for doc in idb_probe_to_documents(probe, export_format=idb_fmt, interval=iv):
                key = str(doc.get("idb_key") or "")
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                docs.append(doc)
            merged = merge_footprint_documents(docs)
    if not merged:
        return 0, None
    candles = merged.get("candles") or []
    if not isinstance(candles, list):
        return 0, None
    return len(candles), _last_candle_open(candles)


def _wait_for_footprint_data(
    page: Page,
    *,
    cfg: dict[str, Any],
    interval: str,
    export_format: str,
    footprint_docs: list[dict[str, Any]],
    max_wait_ms: int,
    min_candles: int,
    lookup_dates: list[str],
    require_fresh: bool = True,
    now: datetime | None = None,
) -> tuple[str, int, int]:
    """
    Poll IndexedDB (first) and WS buffers until ready or timeout.

    Returns ``(source, candle_count, elapsed_ms)`` where source is ``idb``, ``ws``, or ``timeout``.
    """
    poll_ms = footprint_ws_poll_ms(cfg)
    deadline = time.monotonic() + max(1000, max_wait_ms) / 1000.0
    start = time.monotonic()
    idb_enabled = footprint_idb_enabled(cfg)
    expected_closed_open: datetime | None = None
    if require_fresh:
        ref = now
        if ref is None:
            ref = datetime.now(timezone(timedelta(hours=7))).replace(tzinfo=None)
        expected_closed_open = latest_closed_candle_open_for_interval(ref, interval)

    while time.monotonic() < deadline:
        idb_count = 0
        idb_last: datetime | None = None
        if idb_enabled:
            idb_count, idb_last = _idb_snapshot_on_page(
                page,
                cfg=cfg,
                interval=interval,
                export_format=export_format,
                lookup_dates=lookup_dates,
            )

        ws_count, _ = _ws_ready_stats(footprint_docs)
        if footprint_ws_data_ready(
            footprint_docs=footprint_docs,
            idb_candle_count=idb_count,
            min_candles=min_candles,
            interval=interval,
            idb_last_open=idb_last,
            expected_closed_open=expected_closed_open,
            now=now,
        ):
            elapsed = int((time.monotonic() - start) * 1000)
            idb_ready = idb_count >= min_candles and footprint_last_candle_fresh(
                idb_last, expected_closed_open
            )
            if idb_ready:
                _log.info(
                    "footprint_ws: ready via IDB (%s) candles=%d last=%s expected=%s elapsed=%dms",
                    interval,
                    idb_count,
                    idb_last.isoformat() if idb_last else "?",
                    expected_closed_open.isoformat() if expected_closed_open else "?",
                    elapsed,
                )
                return "idb", idb_count, elapsed
            _log.info(
                "footprint_ws: ready via WS (%s) candles=%d elapsed=%dms",
                interval,
                ws_count,
                elapsed,
            )
            return "ws", ws_count, elapsed

        if (
            idb_count >= min_candles
            and expected_closed_open is not None
            and not footprint_last_candle_fresh(idb_last, expected_closed_open)
        ):
            _log.debug(
                "footprint_ws: IDB stale (%s) count=%d last=%s expected=%s",
                interval,
                idb_count,
                idb_last.isoformat() if idb_last else "?",
                expected_closed_open.isoformat(),
            )

        page.wait_for_timeout(poll_ms)

    ws_count, _ = _ws_ready_stats(footprint_docs)
    idb_count = 0
    idb_last = None
    if idb_enabled:
        idb_count, idb_last = _idb_snapshot_on_page(
            page,
            cfg=cfg,
            interval=interval,
            export_format=export_format,
            lookup_dates=lookup_dates,
        )
    elapsed = int((time.monotonic() - start) * 1000)
    best = max(ws_count, idb_count)
    _log.warning(
        "footprint_ws: wait timeout (%s) ws=%d idb=%d idb_last=%s expected=%s min=%d elapsed=%dms",
        interval,
        ws_count,
        idb_count,
        idb_last.isoformat() if idb_last else "?",
        expected_closed_open.isoformat() if expected_closed_open else "?",
        min_candles,
        elapsed,
    )
    return "timeout", best, elapsed


def _idb_lookup_dates_for_capture(cfg: dict[str, Any], extra_session_days: int) -> list[str]:
    if extra_session_days <= 0:
        return default_idb_lookup_dates(extra_days=0)
    from automation_tool.gocharting_footprint_idb import footprint_idb_lookup_days

    return default_idb_lookup_dates(extra_days=footprint_idb_lookup_days(cfg) - 1)


def _build_output_document(
    *,
    footprint_docs: list[dict[str, Any]],
    ohlc_docs: list[dict[str, Any]],
    export_format: str,
    interval: str,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    best_fp = merge_footprint_ws_documents(footprint_docs)
    if best_fp is None:
        raise RuntimeError(
            f"No FOOTPRINT/V2 WebSocket frames captured for {interval}. "
            "Try increasing footprint_ws.wait_ms or check login/chart URL."
        )
    if export_format == FOOTPRINT_EXPORT_FORMAT_COMBINED:
        ohlc_index = build_ohlc_index(ohlc_docs)
        return merge_footprint_raw_with_ohlc(best_fp, ohlc_index, cfg=cfg)
    return best_fp


def _enrich_with_mt5_spot(
    doc: dict[str, Any],
    *,
    cfg: dict[str, Any],
    charts_dir: Path,
    interval: str,
    main_symbol: str | None,
    mt5_accounts_json: Path | None,
) -> dict[str, Any]:
    if not footprint_ws_mt5_spot_enabled(cfg):
        return doc
    candles = doc.get("candles")
    if not isinstance(candles, list) or not candles:
        return doc
    from automation_tool.images import read_main_chart_symbol
    from automation_tool.mt5_candles import fetch_mt5_spot_candles_payload

    logic_symbol = (main_symbol or read_main_chart_symbol(charts_dir)).strip().upper()
    n_bars = len(candles)
    mt5_payload = fetch_mt5_spot_candles_payload(
        logic_symbol=logic_symbol,
        interval=interval,
        count=n_bars,
        accounts_json=mt5_accounts_json,
        footprint_candles=[c for c in candles if isinstance(c, dict)],
    )
    if mt5_payload is None:
        _log.warning(
            "footprint_ws: MT5 spot unavailable for %s %s (%d bars) — skipping merge",
            logic_symbol,
            interval,
            n_bars,
        )
        return doc
    enriched = merge_footprint_with_mt5_spot(doc, mt5_payload)
    spot_meta = enriched.get("mt5_spot") or {}
    matched = spot_meta.get("matched")
    available = spot_meta.get("available")
    _log.info(
        "footprint_ws: MT5 spot merged | %s %s matched=%s/%s bars mode=%s",
        logic_symbol,
        interval,
        matched,
        available,
        mt5_payload.get("fetch_mode"),
    )
    if matched == 0 and available:
        from automation_tool.gocharting_ws_decode import (
            footprint_candle_time_key,
            mt5_bar_time_to_footprint_key,
        )

        fp_sample = [
            footprint_candle_time_key(c)
            for c in candles[:3]
            if isinstance(c, dict)
        ]
        mt5_sample = [
            mt5_bar_time_to_footprint_key(str(b.get("t") or ""))
            for b in (mt5_payload.get("bars") or [])[:3]
            if isinstance(b, dict)
        ]
        _log.warning(
            "footprint_ws: MT5 spot time mismatch | footprint_sample=%s mt5_sample=%s range=%s..%s",
            fp_sample,
            mt5_sample,
            mt5_payload.get("range_from"),
            mt5_payload.get("range_to"),
        )
    return enriched


def _symbol_entry_for_footprint_ws(
    cfg: dict[str, Any],
    main_symbol: str | None,
) -> dict[str, Any]:
    from automation_tool.images import normalize_main_chart_symbol

    sym = normalize_main_chart_symbol((main_symbol or "XAUUSD").strip())
    symbols = cfg.get("symbols") or {}
    if isinstance(symbols, dict):
        block = symbols.get(sym)
        if isinstance(block, dict):
            return block
    return {}


def capture_footprint_ws_on_page(
    page: Page,
    *,
    cfg: dict[str, Any],
    charts_dir: Path,
    chart_url: str,
    interval: str,
    email: str,
    password: str,
    wait_ms: int | None = None,
    out_path: Path | None = None,
    export_format: str | None = None,
    max_candles: int | None = None,
    gocharting_yaml: Optional[Path] = None,
    main_symbol: str | None = None,
    mt5_accounts_json: Path | None = None,
    extra_session_days: int | None = None,
    stale_fallback: bool = False,
) -> Path:
    """Open ``chart_url`` on an existing page, capture WS footprint JSON, trim, write."""
    from automation_tool.gocharting_capture import _maybe_login_gocharting, _select_chart_symbol
    from automation_tool.gocharting_footprint_ws_request import _resolve_footprint_security

    iv = interval.strip().lower()
    fmt = export_format or footprint_ws_export_format(cfg)
    wait = wait_ms if wait_ms is not None else footprint_ws_wait_ms(cfg)
    mc = max_candles if max_candles is not None else footprint_ws_max_candles(cfg)
    min_ready = min(footprint_ws_min_ready_candles(cfg), mc)
    extra_days = (
        extra_session_days
        if extra_session_days is not None
        else footprint_ws_extra_session_days(cfg)
    )
    idb_lookup_dates = _idb_lookup_dates_for_capture(cfg, extra_days)

    fp_dir = footprint_images_dir(charts_dir, gocharting_yaml=gocharting_yaml)
    dest = _resolve_output_path(
        fp_dir=fp_dir,
        interval=iv,
        export_format=fmt,
        out_path=out_path,
    )

    footprint_docs: list[dict[str, Any]] = []
    ohlc_docs: list[dict[str, Any]] = []
    _attach_ws_listeners(
        page,
        interval=iv,
        export_format=fmt,
        footprint_docs=footprint_docs,
        ohlc_docs=ohlc_docs,
    )

    _log.info("footprint_ws: opening %s (%s)", chart_url, iv)
    page.goto(chart_url, wait_until="domcontentloaded", timeout=120_000)
    _maybe_login_gocharting(page, cfg, email, password)
    page.wait_for_timeout(2000)

    ws_security = _resolve_footprint_security(cfg)
    symbol_entry = _symbol_entry_for_footprint_ws(cfg, main_symbol)
    if symbol_entry.get("search_query"):
        _select_chart_symbol(page, cfg, symbol_entry)
        footprint_docs.clear()
        ohlc_docs.clear()
        page.wait_for_timeout(5000)
        _log.info(
            "footprint_ws: switched chart symbol to %r",
            symbol_entry.get("search_query"),
        )

    capture_now = datetime.now(timezone(timedelta(hours=7))).replace(tzinfo=None)
    expected_closed = latest_closed_candle_open_for_interval(capture_now, iv)
    subscribe_dates = footprint_capture_session_dates(footprint_docs, now=capture_now)
    sub_result = request_footprint_dates_on_page(
        page, subscribe_dates, interval=iv, security=ws_security
    )
    if not sub_result.get("ok"):
        _log.warning("footprint_ws: subscribeFootprint failed: %s", sub_result)
    else:
        _log.info("footprint_ws: subscribed footprint dates=%s (%s)", subscribe_dates, iv)
    page.wait_for_timeout(500)

    wait_source, _, _ = _wait_for_footprint_data(
        page,
        cfg=cfg,
        interval=iv,
        export_format=fmt,
        footprint_docs=footprint_docs,
        max_wait_ms=wait,
        min_candles=min_ready,
        lookup_dates=idb_lookup_dates,
        now=capture_now,
    )

    prior_dates: list[str] = []
    if extra_days > 0:
        prior_dates = prior_session_dates_to_request(footprint_docs, extra_days=extra_days)
        if prior_dates:
            _log.info("footprint_ws: requesting %d prior session date(s): %s", len(prior_dates), prior_dates)
            req_result = request_footprint_dates_on_page(
                page, prior_dates, interval=iv, security=ws_security
            )
            if not req_result.get("ok"):
                _log.warning("footprint_ws: prior session request failed: %s", req_result)
            extra_wait = footprint_ws_extra_session_wait_ms(cfg)
            _wait_for_footprint_data(
                page,
                cfg=cfg,
                interval=iv,
                export_format=fmt,
                footprint_docs=footprint_docs,
                max_wait_ms=extra_wait,
                min_candles=min_ready,
                lookup_dates=prior_dates + idb_lookup_dates,
                require_fresh=False,
            )

    if footprint_idb_enabled(cfg):
        _log.info("footprint_idb: workers attached: %d", len(page.workers))

    idb_docs: list[dict[str, Any]] = []
    if footprint_idb_enabled(cfg):
        idb_docs = capture_footprint_idb_documents(
            page,
            cfg=cfg,
            interval=iv,
            dates=idb_lookup_dates if extra_days <= 0 else None,
            export_format=_idb_export_format(fmt),
        )
        if prior_dates:
            found_dates = {
                footprint_document_request_date(doc)
                for doc in idb_docs
                if footprint_document_request_date(doc)
            }
            missing_dates = [d for d in prior_dates if d not in found_dates]
            retry_ms = footprint_idb_retry_wait_ms(cfg)
            if missing_dates and retry_ms > 0:
                _log.info(
                    "footprint_idb: prior session still missing %s — retry in %dms",
                    missing_dates,
                    retry_ms,
                )
                page.wait_for_timeout(retry_ms)
                retry_docs = capture_footprint_idb_documents(
                    page,
                    cfg=cfg,
                    interval=iv,
                    dates=missing_dates,
                    export_format=_idb_export_format(fmt),
                )
                seen_keys = {str(doc.get("idb_key") or "") for doc in idb_docs if doc.get("idb_key")}
                for doc in retry_docs:
                    key = str(doc.get("idb_key") or "")
                    if key and key in seen_keys:
                        continue
                    if key:
                        seen_keys.add(key)
                    idb_docs.append(doc)
                if retry_docs:
                    _log.info("footprint_idb: retry loaded %d document(s)", len(retry_docs))
        if idb_docs:
            _log.info("footprint_idb: loaded %d document(s) for merge", len(idb_docs))

    if idb_docs:
        output_doc = build_output_with_idb(
            footprint_docs=footprint_docs,
            ohlc_docs=ohlc_docs,
            idb_docs=idb_docs,
            export_format=fmt,
        )
    else:
        output_doc = _build_output_document(
            footprint_docs=footprint_docs,
            ohlc_docs=ohlc_docs,
            export_format=fmt,
            interval=iv,
            cfg=cfg,
        )
    output_doc = trim_footprint_document(output_doc, max_candles=mc)
    try:
        _assert_output_fresh(
            output_doc,
            interval=iv,
            expected_closed_open=expected_closed,
            wait_source=wait_source,
            now=capture_now,
        )
    except FootprintCaptureStaleError:
        if stale_fallback and dest.is_file():
            _log.warning(
                "footprint_ws: stale capture for %s — keeping existing %s (last_closed=%s expected>=%s)",
                iv,
                dest.name,
                last_closed_candle_open(output_doc, interval=iv, now=capture_now),
                expected_closed,
            )
            return dest
        raise
    output_doc = _enrich_with_mt5_spot(
        output_doc,
        cfg=cfg,
        charts_dir=charts_dir,
        interval=iv,
        main_symbol=main_symbol,
        mt5_accounts_json=mt5_accounts_json,
    )
    write_footprint_document(dest, output_doc)

    matched = output_doc.get("ohlc_matched")
    session_dates = output_doc.get("ws_session_dates") or []
    _log.info(
        "footprint_ws: wrote %s (%d candles, %s %s, format=%s%s%s)",
        dest.name,
        len(output_doc.get("candles") or []),
        output_doc.get("symbol"),
        document_timeframe(output_doc) or iv,
        fmt,
        f", sessions={session_dates}" if session_dates else "",
        f", ohlc_matched={matched}" if matched is not None else "",
    )
    return dest


def _persist_prepared_footprint_after_ws_capture(
    *,
    charts_dir: Path,
    cfg: dict[str, Any],
    chart_stamp: str | None,
    gocharting_yaml: Optional[Path],
    capture_intervals: tuple[str, ...] | None,
) -> list[Path]:
    """Write ``footprint_{SYMBOL}_{iv}.json`` right after WS combined capture."""
    from automation_tool.gocharting_gc_spot_convert import gc_to_spot_enabled
    from automation_tool.images import persist_prepared_footprint_json_files

    if not gc_to_spot_enabled(cfg):
        return []
    intervals = capture_intervals or tuple(
        iv.strip().lower()
        for iv, _url in footprint_ws_interval_specs(cfg)
        if iv.strip()
    )
    if not intervals:
        return []
    written = persist_prepared_footprint_json_files(
        charts_dir,
        chart_stamp=chart_stamp,
        gocharting_cfg=cfg,
        gocharting_yaml=gocharting_yaml,
        intervals=intervals,
    )
    if written:
        _log.info(
            "footprint_ws: đã ghi prepared spot JSON | %s",
            ", ".join(p.name for p in written),
        )
    return written


def _capture_footprint_ws_standalone(
    *,
    interval: str,
    page_url: str,
    cfg: dict[str, Any],
    charts_dir: Path,
    email: str,
    password: str,
    gocharting_yaml: Optional[Path],
    main_symbol: str | None,
    mt5_accounts_json: Path | None,
    extra_session_days: int | None,
    headless: bool,
) -> Path:
    """Launch an isolated browser and capture one interval (for parallel workers)."""
    from automation_tool.config import default_storage_state_path
    from automation_tool.playwright_browser import close_browser_and_context, launch_chrome_context
    from playwright.sync_api import sync_playwright

    storage = default_storage_state_path()
    vw = int(cfg.get("viewport_width", 1920))
    vh = int(cfg.get("viewport_height", 1080))

    with sync_playwright() as p:
        browser, context = launch_chrome_context(
            p,
            headless=headless,
            storage_state_path=storage if storage.is_file() else None,
            viewport_width=vw,
            viewport_height=vh,
        )
        page = context.new_page()
        try:
            return capture_footprint_ws_on_page(
                page,
                cfg=cfg,
                charts_dir=charts_dir,
                chart_url=page_url,
                interval=interval,
                email=email,
                password=password,
                gocharting_yaml=gocharting_yaml,
                main_symbol=main_symbol,
                mt5_accounts_json=mt5_accounts_json,
                extra_session_days=extra_session_days,
            )
        finally:
            try:
                page.close()
            except Exception:
                pass
            close_browser_and_context(browser, context)


def capture_footprint_ws_plan(
    context: BrowserContext | None,
    cfg: dict[str, Any],
    *,
    charts_dir: Path,
    email: str,
    password: str,
    gocharting_yaml: Optional[Path] = None,
    main_symbol: str | None = None,
    mt5_accounts_json: Path | None = None,
    capture_intervals: tuple[str, ...] | None = None,
    chart_stamp: str | None = None,
    parallel: bool = False,
    extra_session_days: int | None = None,
    headless: bool = True,
    stale_fallback: bool = False,
) -> list[Path]:
    """Capture WS footprint JSON for each ``footprint_screenshot.intervals`` entry."""
    specs = footprint_ws_interval_specs(cfg)
    if not specs:
        raise ValueError("footprint_screenshot.intervals with page_url required for footprint_ws")

    iv_filter = {i.strip().lower() for i in capture_intervals} if capture_intervals else None
    jobs: list[tuple[str, str]] = []
    for interval, page_url in specs:
        if iv_filter is not None and interval.strip().lower() not in iv_filter:
            continue
        jobs.append((interval, page_url))

    paths: list[Path] = []
    captured_intervals: list[str] = []

    if parallel and len(jobs) > 1:
        _log.info("footprint_ws: parallel capture for %s", [j[0] for j in jobs])
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {
                pool.submit(
                    _capture_footprint_ws_standalone,
                    interval=interval,
                    page_url=page_url,
                    cfg=cfg,
                    charts_dir=charts_dir,
                    email=email,
                    password=password,
                    gocharting_yaml=gocharting_yaml,
                    main_symbol=main_symbol,
                    mt5_accounts_json=mt5_accounts_json,
                    extra_session_days=extra_session_days,
                    headless=headless,
                    stale_fallback=stale_fallback,
                ): interval.strip().lower()
                for interval, page_url in jobs
            }
            for future in as_completed(futures):
                iv = futures[future]
                dest = future.result()
                paths.append(dest)
                captured_intervals.append(iv)
    else:
        if context is None:
            raise ValueError("BrowserContext required when parallel=False")
        for interval, page_url in jobs:
            page = context.new_page()
            try:
                dest = capture_footprint_ws_on_page(
                    page,
                    cfg=cfg,
                    charts_dir=charts_dir,
                    chart_url=page_url,
                    interval=interval,
                    email=email,
                    password=password,
                    gocharting_yaml=gocharting_yaml,
                    main_symbol=main_symbol,
                    mt5_accounts_json=mt5_accounts_json,
                    extra_session_days=extra_session_days,
                    stale_fallback=stale_fallback,
                )
                paths.append(dest)
                captured_intervals.append(interval.strip().lower())
            except FootprintCaptureStaleError as exc:
                _log.warning("footprint_ws: capture skipped for %s: %s", interval, exc)
            finally:
                try:
                    page.close()
                except Exception:
                    pass
    try:
        _persist_prepared_footprint_after_ws_capture(
            charts_dir=charts_dir,
            cfg=cfg,
            chart_stamp=chart_stamp,
            gocharting_yaml=gocharting_yaml,
            capture_intervals=tuple(captured_intervals),
        )
    except Exception as exc:
        _log.warning("footprint_ws: prepared spot export skipped: %s", exc)
    return paths


def capture_footprint_ws(
    *,
    chart_url: str,
    gocharting_yaml: Path,
    charts_dir: Path,
    interval: str,
    wait_ms: int,
    headless: bool,
    out_path: Path | None,
    export_format: str = FOOTPRINT_EXPORT_FORMAT_BID_ASK,
) -> Path:
    """Standalone CLI entry: launch browser, capture one interval, return output path."""
    from automation_tool.config import default_storage_state_path, load_all_dotenv
    from automation_tool.gocharting_capture import load_gocharting_yaml
    from automation_tool.playwright_browser import close_browser_and_context, launch_chrome_context
    from playwright.sync_api import sync_playwright

    load_all_dotenv()
    cfg = load_gocharting_yaml(gocharting_yaml)
    email = os.getenv("GOCHARTING_EMAIL", "")
    password = os.getenv("GOCHARTING_PASSWORD", "")
    storage = default_storage_state_path()

    with sync_playwright() as p:
        vw = int(cfg.get("viewport_width", 1920))
        vh = int(cfg.get("viewport_height", 1080))
        browser, context = launch_chrome_context(
            p,
            headless=headless,
            storage_state_path=storage if storage.is_file() else None,
            viewport_width=vw,
            viewport_height=vh,
        )
        page = context.new_page()
        try:
            dest = capture_footprint_ws_on_page(
                page,
                cfg=cfg,
                charts_dir=charts_dir,
                chart_url=chart_url,
                interval=interval.strip().lower(),
                email=email,
                password=password,
                wait_ms=wait_ms,
                out_path=out_path,
                export_format=export_format,
                gocharting_yaml=gocharting_yaml,
            )
        finally:
            try:
                page.close()
            except Exception:
                pass
            close_browser_and_context(browser, context)
    return dest
