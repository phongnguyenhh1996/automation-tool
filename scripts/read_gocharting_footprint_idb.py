#!/usr/bin/env python3
"""Probe GoCharting IndexedDB (BinaryFootprint) and export footprint JSON for a session date."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from automation_tool.config import default_storage_state_path, load_all_dotenv  # noqa: E402
from automation_tool.gocharting_capture import _maybe_login_gocharting, load_gocharting_yaml  # noqa: E402
from automation_tool.gocharting_footprint_idb import (  # noqa: E402
    capture_footprint_idb_documents,
    default_idb_lookup_dates,
    footprint_idb_enabled,
    merge_footprint_documents,
    read_footprint_idb_on_page,
)
from automation_tool.gocharting_ws_decode import (  # noqa: E402
    FOOTPRINT_EXPORT_FORMAT_RAW,
    write_footprint_document,
)
from automation_tool.playwright_browser import close_browser_and_context, launch_chrome_context  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

_TZ_GMT7 = timezone(timedelta(hours=7))
_DEFAULT_CHART_URL = "https://gocharting.com/terminal/chart/GC435uijM"


def _default_yesterday() -> str:
    return (datetime.now(_TZ_GMT7).date() - timedelta(days=1)).isoformat()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Read GoCharting footprint from IndexedDB")
    parser.add_argument("--url", default=_DEFAULT_CHART_URL)
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "gocharting.yaml")
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--date", default=_default_yesterday(), help="Session date YYYY-MM-DD (default: yesterday GMT+7)")
    parser.add_argument("--list-only", action="store_true", help="Only list IDB keys, do not decode")
    parser.add_argument("--wait-ms", type=int, default=15_000, help="Wait after chart load for cache population")
    parser.add_argument("--out", type=Path, help="Write merged raw JSON here")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    load_all_dotenv()
    cfg = load_gocharting_yaml(args.config)
    email = os.getenv("GOCHARTING_EMAIL", "")
    password = os.getenv("GOCHARTING_PASSWORD", "")
    storage = default_storage_state_path()
    iv = args.interval.strip().lower()
    target_date = args.date.strip()

    with sync_playwright() as p:
        vw = int(cfg.get("viewport_width", 1920))
        vh = int(cfg.get("viewport_height", 1080))
        browser, context = launch_chrome_context(
            p,
            headless=args.headless,
            storage_state_path=storage if storage.is_file() else None,
            viewport_width=vw,
            viewport_height=vh,
        )
        page = context.new_page()
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=120_000)
            _maybe_login_gocharting(page, cfg, email, password)
            page.wait_for_timeout(args.wait_ms)
            _log.info("footprint_idb: workers attached: %d", len(page.workers))

            probe = read_footprint_idb_on_page(page, date=target_date, interval=iv)
            summary = {
                "date": target_date,
                "interval": iv,
                "all_keys_count": len(probe.get("all_keys") or []),
                "matched_keys": probe.get("matched_keys") or [],
                "sample_all_keys": (probe.get("all_keys") or [])[:20],
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2))

            if args.list_only:
                return

            if not footprint_idb_enabled(cfg):
                print("footprint_ws.idb disabled in config — decoding anyway for this script", file=sys.stderr)

            docs = capture_footprint_idb_documents(
                page,
                cfg=cfg,
                interval=iv,
                dates=[target_date],
                export_format=FOOTPRINT_EXPORT_FORMAT_RAW,
            )
            merged = merge_footprint_documents(docs)
            if merged is None:
                print(
                    f"No IndexedDB footprint for {target_date} ({iv}). "
                    "Try PLAYWRIGHT_CHROME_USER_DATA_DIR with a profile that opened this chart yesterday.",
                    file=sys.stderr,
                )
                sys.exit(1)

            out_path = args.out
            if out_path is None:
                out_dir = ROOT / "data" / "XAUUSD" / "charts" / "footprint_images"
                out_path = out_dir / f"footprint_idb_{iv}_{target_date}.json"
            write_footprint_document(out_path, merged)
            print(
                json.dumps(
                    {
                        "output": str(out_path),
                        "candles": len(merged.get("candles") or []),
                        "symbol": merged.get("symbol"),
                        "dates_checked": default_idb_lookup_dates(extra_days=0),
                    },
                    indent=2,
                )
            )
        finally:
            try:
                page.close()
            except Exception:
                pass
            close_browser_and_context(browser, context)


if __name__ == "__main__":
    main()
