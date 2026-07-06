#!/usr/bin/env python3
"""Capture multi-day GoCharting footprint WS for scalp backtest (spot-combined JSON)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
_SCALP = Path(__file__).resolve().parent
if str(_SCALP) not in sys.path:
    sys.path.insert(0, str(_SCALP))

from automation_tool.config import default_storage_state_path, load_all_dotenv  # noqa: E402
from automation_tool.gocharting_capture import _maybe_login_gocharting, load_gocharting_yaml  # noqa: E402
from automation_tool.gocharting_footprint_ws_request import request_footprint_dates_on_page  # noqa: E402
from automation_tool.gocharting_ws_capture import _build_output_document, _frame_bytes  # noqa: E402
from automation_tool.gocharting_ws_decode import (  # noqa: E402
    FOOTPRINT_EXPORT_FORMAT_COMBINED,
    FOOTPRINT_EXPORT_FORMAT_RAW,
    aggregate_footprint_combined_document,
    candle_sort_datetime,
    decode_ws_footprint_frame,
    decode_ws_ohlc_frame,
    document_timeframe,
    drop_forming_footprint_candle,
    trim_footprint_document,
    write_footprint_document,
)
from automation_tool.gocharting_gc_spot_convert import (  # noqa: E402
    GcToSpotConversionError,
    convert_footprint_combined_to_spot,
    finalize_prepared_spot_footprint,
    gc_to_spot_enabled,
    resolve_mt5_spot_payload,
)
from automation_tool.images import read_main_chart_symbol  # noqa: E402
from automation_tool.playwright_browser import close_browser_and_context, launch_chrome_context  # noqa: E402

_log = logging.getLogger(__name__)

_INTERVAL_URLS = {
    "5m": "https://gocharting.com/terminal/chart/GC435uijM",
    "15m": "https://gocharting.com/terminal/chart/S0kcqfQKt",
}


def _default_backtest_dates() -> list[str]:
    """Today (GMT+7), prior Friday, prior Thursday — skip weekend calendar days."""
    today = datetime.now(timezone(timedelta(hours=7))).date()
    out: list[str] = [today.isoformat()]
    d = today
    while len(out) < 3:
        d -= timedelta(days=1)
        if d.weekday() < 5:  # Mon–Fri
            out.append(d.isoformat())
    return sorted(set(out))


def _candle_calendar_dates(candles: list[dict]) -> set[str]:
    out: set[str] = set()
    for c in candles:
        sd = c.get("session_date")
        if sd:
            out.add(str(sd).strip()[:10])
            continue
        raw = c.get("date")
        if isinstance(raw, str) and len(raw) >= 10:
            out.add(raw[:10])
            continue
        dt = candle_sort_datetime(c)
        if dt > datetime.min.replace(tzinfo=None):
            out.add(dt.date().isoformat())
    return out


def capture_interval_sessions(
    *,
    interval: str,
    dates: list[str],
    cfg: dict,
    charts_dir: Path,
    wait_ms: int,
    max_candles: int,
    headless: bool,
) -> Path:
    iv = interval.strip().lower()
    chart_url = _INTERVAL_URLS.get(iv)
    if not chart_url:
        raise ValueError(f"unsupported interval: {interval}")

    fp_cfg = cfg.get("footprint_screenshot") or {}
    iv_cfg = (fp_cfg.get("intervals") or {}).get(iv) or {}
    vw = int(iv_cfg.get("viewport_width") or 500)
    vh = int(iv_cfg.get("viewport_height") or 1500)

    email = os.getenv("GOCHARTING_EMAIL", "")
    password = os.getenv("GOCHARTING_PASSWORD", "")
    storage = default_storage_state_path()

    footprint_docs: list[dict] = []
    ohlc_docs: list[dict] = []

    with sync_playwright() as p:
        browser, ctx = launch_chrome_context(
            p,
            headless=headless,
            storage_state_path=storage if storage.is_file() else None,
            viewport_width=vw,
            viewport_height=vh,
        )
        page = ctx.new_page()

        def on_websocket(ws) -> None:
            def on_recv(frame) -> None:
                raw = _frame_bytes(frame)
                fp_doc = decode_ws_footprint_frame(raw, export_format=FOOTPRINT_EXPORT_FORMAT_RAW)
                if fp_doc is not None:
                    if document_timeframe(fp_doc) == iv:
                        footprint_docs.append(fp_doc)
                        _log.info(
                            "FOOTPRINT %s candles=%d complete=%s",
                            iv,
                            len(fp_doc.get("candles") or []),
                            fp_doc.get("is_complete"),
                        )
                ohlc_doc = decode_ws_ohlc_frame(raw)
                if ohlc_doc is not None:
                    ohlc_docs.append(ohlc_doc)
                    _log.info("OHLC %s bars=%d", iv, len(ohlc_doc.get("candles") or []))

            ws.on("framereceived", on_recv)

        page.on("websocket", on_websocket)
        page.goto(chart_url, wait_until="domcontentloaded", timeout=120_000)
        _maybe_login_gocharting(page, cfg, email, password)
        page.wait_for_timeout(8000)

        req = request_footprint_dates_on_page(page, dates, interval=iv)
        _log.info("subscribe %s dates=%s result=%s", iv, dates, req)
        page.wait_for_timeout(wait_ms)

        close_browser_and_context(browser, ctx)

    merged = _build_output_document(
        footprint_docs=footprint_docs,
        ohlc_docs=ohlc_docs,
        export_format=FOOTPRINT_EXPORT_FORMAT_COMBINED,
        interval=iv,
        cfg=cfg,
    )
    if not merged.get("candles"):
        raise RuntimeError(f"No footprint candles for {iv}")

    candle_dates = _candle_calendar_dates(merged.get("candles") or [])
    min_expected = max(1, len(dates) // 2)
    if len(candle_dates) < min_expected and len(merged.get("candles") or []) < 80:
        raise RuntimeError(
            f"Sparse capture for {iv}: {len(merged.get('candles') or [])} candles, "
            f"calendar_dates={sorted(candle_dates)}, wanted={dates}"
        )
    merged["ws_session_dates"] = sorted(candle_dates) or list(dates)

    merged = drop_forming_footprint_candle(merged, interval=iv)
    merged = trim_footprint_document(merged, max_candles=max_candles)
    merged = aggregate_footprint_combined_document(merged, cfg=cfg)
    # bar_flow (incl. cot_high/cot_low) already set by _build_output_document → merge_footprint_raw_with_ohlc.
    # Do NOT re-enrich here: slim() drops ending_summary, so a second enrich zeroes COT fields.

    out_dir = charts_dir / "footprint_images"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / f"footprint_combined_{iv}.json"

    cfg_bt = dict(cfg)
    ws = dict(cfg_bt.get("footprint_ws") or {})
    ws["max_candles"] = max_candles
    cfg_bt["footprint_ws"] = ws

    final_doc = merged
    if gc_to_spot_enabled(cfg_bt):
        sym = read_main_chart_symbol(charts_dir)
        candles = final_doc.get("candles") or []
        try:
            mt5 = resolve_mt5_spot_payload(
                charts_dir=charts_dir,
                logic_symbol=sym,
                interval=iv,
                count=len(candles),
                footprint_candles=[c for c in candles if isinstance(c, dict)],
            )
            final_doc = convert_footprint_combined_to_spot(
                final_doc,
                mt5_payload=mt5,
                cfg=cfg_bt,
                logic_symbol=sym,
                interval=iv,
            )
            final_doc = finalize_prepared_spot_footprint(final_doc, logic_symbol=sym, interval=iv)
        except GcToSpotConversionError as exc:
            _log.warning("spot convert skipped (%s) — keeping GC futures prices", exc)

    write_footprint_document(raw_path, final_doc)

    _log.info(
        "Wrote %s — %d candles, sessions=%s",
        raw_path.name,
        len(final_doc.get("candles") or []),
        final_doc.get("ws_session_dates") or sorted(candle_dates),
    )
    return raw_path


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Capture multi-day footprint for scalp backtest")
    parser.add_argument(
        "--dates",
        nargs="+",
        help="Session dates YYYY-MM-DD (default: today + prior Thu/Fri)",
    )
    parser.add_argument("--intervals", nargs="+", default=["5m", "15m"])
    parser.add_argument("--charts-dir", type=Path, default=ROOT / "data" / "XAUUSD" / "charts")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "gocharting.yaml")
    parser.add_argument("--wait-ms", type=int, default=60_000)
    parser.add_argument("--max-candles", type=int, default=500)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args(argv)

    load_all_dotenv()
    os.environ.pop("PLAYWRIGHT_CHROME_USER_DATA_DIR", None)

    dates = args.dates or _default_backtest_dates()
    cfg = load_gocharting_yaml(args.config)

    written: list[str] = []
    for iv in args.intervals:
        path = capture_interval_sessions(
            interval=iv,
            dates=dates,
            cfg=cfg,
            charts_dir=args.charts_dir,
            wait_ms=args.wait_ms,
            max_candles=args.max_candles,
            headless=args.headless,
        )
        written.append(str(path))

    print(json.dumps({"dates": dates, "files": written}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
