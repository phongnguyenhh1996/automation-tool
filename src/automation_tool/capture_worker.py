"""
Worker process for high-level chart capture.

This exists so `browser_service` can expose a high-level `capture_charts` RPC without
mixing Playwright async/sync APIs in the same event loop.

The worker:
- attaches to the long-lived browser service via CDP (from browser_service_state.json)
- runs `coinmap.capture_charts(..., reuse_browser_context=context)` using Playwright *sync* API
- prints one JSON object to stdout (single line), and exits non-zero on failure
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from automation_tool.browser_client import try_attach_playwright_via_service
from automation_tool.coinmap import capture_charts
from automation_tool.config import load_settings
from automation_tool.telegram_logging import setup_automation_logging


def _payload_from_cli(raw: str) -> dict[str, Any]:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"Invalid --payload JSON: {e}") from e
    if not isinstance(obj, dict):
        raise SystemExit("--payload must be a JSON object")
    return obj


def main(argv: list[str] | None = None) -> None:
    setup_automation_logging(load_settings())
    ap = argparse.ArgumentParser(prog="python -m automation_tool.capture_worker")
    ap.add_argument("--payload", required=True, help="JSON object payload for capture_charts RPC")
    ns = ap.parse_args(argv)

    payload = _payload_from_cli(str(ns.payload))

    coinmap_yaml = Path(str(payload.get("coinmap_yaml") or "")).expanduser()
    charts_dir_raw = payload.get("charts_dir")
    charts_dir = Path(str(charts_dir_raw)).expanduser() if charts_dir_raw else None
    storage_state_raw = payload.get("storage_state_path")
    storage_state_path = Path(str(storage_state_raw)).expanduser() if storage_state_raw else None

    email = payload.get("email")
    password = payload.get("password")
    tradingview_password = payload.get("tradingview_password")
    save_storage_state = bool(payload.get("save_storage_state", True))
    headless = bool(payload.get("headless", True))
    main_chart_symbol = payload.get("main_chart_symbol")

    # Optional flags exposed by capture_charts (for capture-many-like flows)
    enable_coinmap = payload.get("enable_coinmap")
    enable_tradingview = payload.get("enable_tradingview")
    clear_charts_before_capture = payload.get("clear_charts_before_capture")
    stamp_override = payload.get("stamp_override")
    set_global_active_symbol = bool(payload.get("set_global_active_symbol", True))

    with sync_playwright() as p:
        attached = try_attach_playwright_via_service(p, force=True)
        if attached is None:
            raise SystemExit("capture_worker: could not attach to browser service (missing/invalid state file?)")
        browser, context = attached
        try:
            paths = capture_charts(
                coinmap_yaml=coinmap_yaml,
                charts_dir=charts_dir,
                storage_state_path=storage_state_path,
                email=str(email) if email is not None else None,
                password=str(password) if password is not None else None,
                tradingview_password=str(tradingview_password) if tradingview_password is not None else None,
                save_storage_state=save_storage_state,
                headless=headless,
                reuse_browser_context=context,
                main_chart_symbol=str(main_chart_symbol) if main_chart_symbol is not None else None,
                set_global_active_symbol=set_global_active_symbol,
                enable_coinmap=enable_coinmap,
                enable_tradingview=enable_tradingview,
                clear_charts_before_capture=clear_charts_before_capture,
                stamp_override=str(stamp_override) if stamp_override is not None else None,
            )
        finally:
            try:
                browser.close()
            except Exception:
                pass

    out = {"ok": True, "paths": [str(p) for p in paths]}
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()

