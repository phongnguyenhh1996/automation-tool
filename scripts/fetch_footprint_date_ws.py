#!/usr/bin/env python3
"""Fetch GoCharting footprint for explicit session date(s) via WebSocket and save proof JSON."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from automation_tool.config import default_storage_state_path, load_all_dotenv  # noqa: E402
from automation_tool.gocharting_capture import _maybe_login_gocharting, load_gocharting_yaml  # noqa: E402
from automation_tool.gocharting_footprint_ws_request import request_footprint_dates_on_page  # noqa: E402
from automation_tool.gocharting_ws_decode import (  # noqa: E402
    FOOTPRINT_EXPORT_FORMAT_RAW,
    decode_ws_footprint_frame,
    document_timeframe,
    footprint_document_request_date,
    merge_footprint_ws_documents,
    write_footprint_document,
)
from automation_tool.gocharting_ws_capture import _frame_bytes  # noqa: E402
from automation_tool.playwright_browser import close_browser_and_context, launch_chrome_context  # noqa: E402

_log = logging.getLogger(__name__)
_DEFAULT_URL = "https://gocharting.com/terminal/chart/GC435uijM"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Fetch footprint for session date via WS")
    parser.add_argument("--date", default="2026-07-01", help="Session date YYYY-MM-DD")
    parser.add_argument("--also-today", action="store_true", help="Also request today's session date")
    parser.add_argument("--url", default=_DEFAULT_URL)
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "gocharting.yaml")
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--wait-after-request-ms", type=int, default=45_000)
    parser.add_argument("--out", type=Path, help="Output JSON path")
    parser.add_argument("--proof", type=Path, help="WS evidence summary JSON")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    load_all_dotenv()
    os.environ.pop("PLAYWRIGHT_CHROME_USER_DATA_DIR", None)
    cfg = load_gocharting_yaml(args.config)
    email = os.getenv("GOCHARTING_EMAIL", "")
    password = os.getenv("GOCHARTING_PASSWORD", "")
    storage = default_storage_state_path()
    iv = args.interval.strip().lower()
    target = args.date.strip()

    dates = [target]
    if args.also_today:
        from datetime import datetime, timedelta, timezone

        today = datetime.now(timezone(timedelta(hours=7))).date().isoformat()
        if today not in dates:
            dates.append(today)

    footprint_docs: list[dict] = []
    ws_sent_footprint: list[dict] = []
    ws_recv_footprint: list[dict] = []

    fp_cfg = cfg.get("footprint_screenshot") or {}
    iv_cfg = (fp_cfg.get("intervals") or {}).get(iv) or {}
    vw = int(iv_cfg.get("viewport_width") or 500)
    vh = int(iv_cfg.get("viewport_height") or 1500)

    with sync_playwright() as p:
        browser, ctx = launch_chrome_context(
            p,
            headless=args.headless,
            storage_state_path=storage if storage.is_file() else None,
            viewport_width=vw,
            viewport_height=vh,
        )
        page = ctx.new_page()

        def on_websocket(ws) -> None:
            def on_sent(frame) -> None:
                raw = _frame_bytes(frame)
                try:
                    obj = json.loads(raw.decode("utf-8"))
                except Exception:
                    return
                if obj.get("command") == "FOOTPRINT/V2" and isinstance(obj.get("payload"), dict):
                    ws_sent_footprint.append(obj)

            def on_recv(frame) -> None:
                raw = _frame_bytes(frame)
                doc = decode_ws_footprint_frame(raw, export_format=FOOTPRINT_EXPORT_FORMAT_RAW)
                if doc is None:
                    return
                if iv and document_timeframe(doc) != iv:
                    return
                footprint_docs.append(doc)
                ws_recv_footprint.append(
                    {
                        "request_date": footprint_document_request_date(doc),
                        "candles": len(doc.get("candles") or []),
                        "is_complete": doc.get("is_complete"),
                        "ws_type": doc.get("ws_type"),
                        "first_candle": (doc.get("candles") or [{}])[0].get("time_gmt7"),
                        "last_candle": (doc.get("candles") or [{}])[-1].get("time_gmt7"),
                    }
                )
                _log.info(
                    "WS RECV FOOTPRINT request_date=%s candles=%d complete=%s",
                    footprint_document_request_date(doc),
                    len(doc.get("candles") or []),
                    doc.get("is_complete"),
                )

            ws.on("framesent", on_sent)
            ws.on("framereceived", on_recv)

        page.on("websocket", on_websocket)

        page.goto(args.url, wait_until="domcontentloaded", timeout=120_000)
        _maybe_login_gocharting(page, cfg, email, password)
        page.wait_for_timeout(8000)

        req_result = request_footprint_dates_on_page(page, dates, interval=iv)
        _log.info("worker request: %s", req_result)
        page.wait_for_timeout(args.wait_after_request_ms)

        merged = merge_footprint_ws_documents(footprint_docs)
        session_dates = sorted(
            {footprint_document_request_date(d) for d in footprint_docs if footprint_document_request_date(d)}
        )

        proof = {
            "target_date": target,
            "dates_requested_via_worker": dates,
            "worker_result": req_result,
            "ws_footprint_sent": ws_sent_footprint,
            "ws_footprint_recv": ws_recv_footprint,
            "session_dates_in_ws_recv": session_dates,
            "target_date_found_in_ws": target in session_dates,
            "total_candles_merged": len(merged.get("candles") or []) if merged else 0,
        }

        proof_path = args.proof or (
            ROOT / "data" / "network_sniff" / f"footprint_ws_proof_{target}_{iv}.json"
        )
        proof_path.parent.mkdir(parents=True, exist_ok=True)
        proof_path.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        if merged is None or target not in session_dates:
            print(json.dumps(proof, indent=2))
            raise SystemExit(
                f"FAIL: no WS footprint frame with request.date={target}. proof={proof_path}"
            )

        target_doc = next(
            d for d in footprint_docs if footprint_document_request_date(d) == target
        )
        out_path = args.out or (
            ROOT / "data" / "network_sniff" / f"footprint_ws_{iv}_{target}.json"
        )
        write_footprint_document(out_path, target_doc)

        print(
            json.dumps(
                {
                    "success": True,
                    "target_date": target,
                    "candles": len(target_doc.get("candles") or []),
                    "is_complete": target_doc.get("is_complete"),
                    "output": str(out_path),
                    "proof": str(proof_path),
                    "ws_sent_dates": [
                        (s.get("payload") or {}).get("dates") for s in ws_sent_footprint
                    ],
                },
                indent=2,
            )
        )
        close_browser_and_context(browser, ctx)


if __name__ == "__main__":
    main()
