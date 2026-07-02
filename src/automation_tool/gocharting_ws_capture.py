from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import BrowserContext, Page

from automation_tool.gocharting_footprint_idb import (
    build_output_with_idb,
    capture_footprint_idb_documents,
    footprint_idb_enabled,
)
from automation_tool.gocharting_footprint_ws_request import request_footprint_dates_on_page
from automation_tool.gocharting_footprint_ocr import footprint_images_dir, footprint_interval_json_path
from automation_tool.gocharting_ws_decode import (
    FOOTPRINT_EXPORT_FORMAT_BID_ASK,
    FOOTPRINT_EXPORT_FORMAT_COMBINED,
    FOOTPRINT_EXPORT_FORMAT_RAW,
    build_ohlc_index,
    decode_ws_footprint_frame,
    decode_ws_ohlc_frame,
    document_timeframe,
    footprint_combined_json_path,
    footprint_document_request_date,
    footprint_raw_json_path,
    footprint_ws_export_format,
    footprint_ws_extra_session_days,
    footprint_ws_extra_session_wait_ms,
    footprint_ws_interval_specs,
    footprint_ws_max_candles,
    footprint_ws_wait_ms,
    merge_footprint_raw_with_ohlc,
    merge_footprint_with_mt5_spot,
    merge_footprint_ws_documents,
    prior_session_dates_to_request,
    trim_footprint_document,
    footprint_ws_mt5_spot_enabled,
    write_footprint_document,
)

_log = logging.getLogger(__name__)


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


def _build_output_document(
    *,
    footprint_docs: list[dict[str, Any]],
    ohlc_docs: list[dict[str, Any]],
    export_format: str,
    interval: str,
) -> dict[str, Any]:
    best_fp = merge_footprint_ws_documents(footprint_docs)
    if best_fp is None:
        raise RuntimeError(
            f"No FOOTPRINT/V2 WebSocket frames captured for {interval}. "
            "Try increasing footprint_ws.wait_ms or check login/chart URL."
        )
    if export_format == FOOTPRINT_EXPORT_FORMAT_COMBINED:
        ohlc_index = build_ohlc_index(ohlc_docs)
        return merge_footprint_raw_with_ohlc(best_fp, ohlc_index)
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
    _log.info(
        "footprint_ws: MT5 spot merged | %s %s matched=%s/%s bars",
        logic_symbol,
        interval,
        spot_meta.get("matched"),
        spot_meta.get("available"),
    )
    return enriched


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
) -> Path:
    """Open ``chart_url`` on an existing page, capture WS footprint JSON, trim, write."""
    from automation_tool.gocharting_capture import _maybe_login_gocharting

    iv = interval.strip().lower()
    fmt = export_format or footprint_ws_export_format(cfg)
    wait = wait_ms if wait_ms is not None else footprint_ws_wait_ms(cfg)
    mc = max_candles if max_candles is not None else footprint_ws_max_candles(cfg)

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
    page.wait_for_timeout(3000)
    _log.info("footprint_ws: waiting %dms for FOOTPRINT/V2 + TS/V2 (%s)...", wait, iv)
    page.wait_for_timeout(wait)

    extra_days = footprint_ws_extra_session_days(cfg)
    if extra_days > 0:
        prior_dates = prior_session_dates_to_request(footprint_docs, extra_days=extra_days)
        if prior_dates:
            _log.info("footprint_ws: requesting %d prior session date(s): %s", len(prior_dates), prior_dates)
            req_result = request_footprint_dates_on_page(page, prior_dates, interval=iv)
            if not req_result.get("ok"):
                _log.warning("footprint_ws: prior session request failed: %s", req_result)
            extra_wait = footprint_ws_extra_session_wait_ms(cfg)
            _log.info("footprint_ws: waiting %dms for prior session FOOTPRINT (%s)...", extra_wait, iv)
            page.wait_for_timeout(extra_wait)

    if footprint_idb_enabled(cfg):
        _log.info("footprint_idb: workers attached: %d", len(page.workers))

    idb_docs: list[dict[str, Any]] = []
    if footprint_idb_enabled(cfg):
        idb_docs = capture_footprint_idb_documents(
            page,
            cfg=cfg,
            interval=iv,
            export_format=(
                FOOTPRINT_EXPORT_FORMAT_RAW
                if fmt in (FOOTPRINT_EXPORT_FORMAT_RAW, FOOTPRINT_EXPORT_FORMAT_COMBINED)
                else FOOTPRINT_EXPORT_FORMAT_BID_ASK
            ),
        )
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
        )
    output_doc = trim_footprint_document(output_doc, max_candles=mc)
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


def capture_footprint_ws_plan(
    context: BrowserContext,
    cfg: dict[str, Any],
    *,
    charts_dir: Path,
    email: str,
    password: str,
    gocharting_yaml: Optional[Path] = None,
    main_symbol: str | None = None,
    mt5_accounts_json: Path | None = None,
    capture_intervals: tuple[str, ...] | None = None,
) -> list[Path]:
    """Capture WS footprint JSON for each ``footprint_screenshot.intervals`` entry."""
    specs = footprint_ws_interval_specs(cfg)
    if not specs:
        raise ValueError("footprint_screenshot.intervals with page_url required for footprint_ws")

    iv_filter = {i.strip().lower() for i in capture_intervals} if capture_intervals else None
    paths: list[Path] = []
    for interval, page_url in specs:
        if iv_filter is not None and interval.strip().lower() not in iv_filter:
            continue
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
            )
            paths.append(dest)
        finally:
            try:
                page.close()
            except Exception:
                pass
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
