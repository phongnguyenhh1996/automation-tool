"""
Worker process for multi-symbol capture (Coinmap all symbols → TradingView all symbols).

Runs in a subprocess spawned by ``browser_service`` for ``METHOD_CAPTURE_MANY`` RPC,
mirroring ``cli.cmd_capture_many`` but attaching via CDP like ``capture_worker``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from automation_tool.browser_client import try_attach_playwright_via_service
from automation_tool.coinmap import capture_charts
from automation_tool.config import symbol_data_dir


def _payload_from_cli(raw: str) -> dict[str, Any]:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"Invalid --payload JSON: {e}") from e
    if not isinstance(obj, dict):
        raise SystemExit("--payload must be a JSON object")
    return obj


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m automation_tool.capture_many_worker")
    ap.add_argument("--payload", required=True, help="JSON object for capture-many RPC")
    ns = ap.parse_args(argv)

    payload = _payload_from_cli(str(ns.payload))

    raw_syms = payload.get("symbols")
    if not isinstance(raw_syms, list) or not raw_syms:
        raise SystemExit("capture_many_worker: symbols must be a non-empty JSON array")
    symbols = [str(s).strip().upper() for s in raw_syms if str(s).strip()]
    if not symbols:
        raise SystemExit("capture_many_worker: no valid symbols after trim")

    coinmap_yaml = Path(str(payload.get("coinmap_yaml") or "")).expanduser()
    storage_state_raw = payload.get("storage_state_path")
    storage_state_path = Path(str(storage_state_raw)).expanduser() if storage_state_raw else None

    email = payload.get("email")
    password = payload.get("password")
    tradingview_password = payload.get("tradingview_password")
    save_storage_state = bool(payload.get("save_storage_state", True))
    headless = bool(payload.get("headless", True))

    stamps: dict[str, str] = {sym: time.strftime("%Y%m%d_%H%M%S") for sym in symbols}
    all_paths: list[Path] = []

    with sync_playwright() as p:
        attached = try_attach_playwright_via_service(p, force=True)
        if attached is None:
            raise SystemExit("capture_many_worker: could not attach to browser service (missing/invalid state file?)")
        browser, context = attached
        try:
            for sym in symbols:
                charts_dir = symbol_data_dir(sym) / "charts"
                paths = capture_charts(
                    coinmap_yaml=coinmap_yaml,
                    charts_dir=charts_dir,
                    storage_state_path=storage_state_path,
                    email=str(email) if email is not None else None,
                    password=str(password) if password is not None else None,
                    tradingview_password=str(tradingview_password) if tradingview_password is not None else None,
                    save_storage_state=False,
                    headless=headless,
                    reuse_browser_context=context,
                    main_chart_symbol=sym,
                    set_global_active_symbol=False,
                    enable_coinmap=True,
                    enable_tradingview=False,
                    clear_charts_before_capture=True,
                    stamp_override=stamps[sym],
                )
                all_paths.extend(paths)

            for sym in symbols:
                charts_dir = symbol_data_dir(sym) / "charts"
                paths = capture_charts(
                    coinmap_yaml=coinmap_yaml,
                    charts_dir=charts_dir,
                    storage_state_path=storage_state_path,
                    email=str(email) if email is not None else None,
                    password=str(password) if password is not None else None,
                    tradingview_password=str(tradingview_password) if tradingview_password is not None else None,
                    save_storage_state=False,
                    headless=headless,
                    reuse_browser_context=context,
                    main_chart_symbol=sym,
                    set_global_active_symbol=False,
                    enable_coinmap=False,
                    enable_tradingview=True,
                    clear_charts_before_capture=False,
                    stamp_override=stamps[sym],
                )
                all_paths.extend(paths)

            if save_storage_state and storage_state_path:
                storage_state_path.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(storage_state_path))
        finally:
            try:
                browser.close()
            except Exception:
                pass

    out = {"ok": True, "paths": [str(p) for p in all_paths]}
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
