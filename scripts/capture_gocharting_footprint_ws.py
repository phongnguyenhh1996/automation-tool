#!/usr/bin/env python3
"""Capture GoCharting footprint via WebSocket and export JSON."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from automation_tool.gocharting_ws_capture import capture_footprint_ws  # noqa: E402
from automation_tool.gocharting_ws_decode import (  # noqa: E402
    FOOTPRINT_EXPORT_FORMAT_BID_ASK,
    FOOTPRINT_EXPORT_FORMATS,
)

_DEFAULT_CHART_URL = "https://gocharting.com/terminal/chart/GC435uijM"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Capture GoCharting footprint JSON via WebSocket")
    parser.add_argument("--url", default=_DEFAULT_CHART_URL)
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "gocharting.yaml")
    parser.add_argument(
        "--charts-dir",
        type=Path,
        default=ROOT / "data" / "XAUUSD" / "charts",
    )
    parser.add_argument("--interval", default="5m", help="Expected timeframe (default: 5m)")
    parser.add_argument("--wait-ms", type=int, default=30_000)
    parser.add_argument("--out", type=Path, help="Override output JSON path")
    parser.add_argument(
        "--format",
        choices=FOOTPRINT_EXPORT_FORMATS,
        default=FOOTPRINT_EXPORT_FORMAT_BID_ASK,
        help="bid_ask | raw | combined (raw + OHLC per candle)",
    )
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    dest = capture_footprint_ws(
        chart_url=args.url,
        gocharting_yaml=args.config,
        charts_dir=args.charts_dir,
        interval=args.interval.strip().lower(),
        wait_ms=args.wait_ms,
        headless=args.headless,
        out_path=args.out,
        export_format=args.format,
    )
    print(json.dumps({"output": str(dest)}, indent=2))


if __name__ == "__main__":
    main()
