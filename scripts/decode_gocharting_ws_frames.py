#!/usr/bin/env python3
"""Decode captured GoCharting WS frames → footprint JSON."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from automation_tool.gocharting_ws_decode import (  # noqa: E402
    FOOTPRINT_EXPORT_FORMATS,
    FOOTPRINT_EXPORT_FORMAT_BID_ASK,
    FOOTPRINT_EXPORT_FORMAT_COMBINED,
    FOOTPRINT_EXPORT_FORMAT_RAW,
    decode_ws_frames_dir,
    decode_ws_frames_merged,
    document_timeframe,
    pick_best_footprint_document,
    write_footprint_document,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Decode GoCharting WS footprint frames to JSON")
    parser.add_argument(
        "frames_dir",
        type=Path,
        help="Directory with ws_frames_* *_recv.bin files",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Output JSON path (default: stdout summary only)",
    )
    parser.add_argument(
        "--format",
        choices=FOOTPRINT_EXPORT_FORMATS,
        default=FOOTPRINT_EXPORT_FORMAT_BID_ASK,
        help="bid_ask | raw | combined (raw + OHLC per candle)",
    )
    args = parser.parse_args()

    if args.format in (FOOTPRINT_EXPORT_FORMAT_RAW, FOOTPRINT_EXPORT_FORMAT_COMBINED):
        best = decode_ws_frames_merged(args.frames_dir, export_format=args.format)
        if best is None:
            raise SystemExit(f"No FOOTPRINT frames found in {args.frames_dir}")
        tf = document_timeframe(best) or "?"
        extra = ""
        if args.format == FOOTPRINT_EXPORT_FORMAT_COMBINED:
            extra = f", ohlc_matched={best.get('ohlc_matched', 0)}/{len(best.get('candles') or [])}"
        print(
            f"Decoded merged output: {len(best['candles'])} candles "
            f"({best.get('symbol')} {tf}, format={args.format}{extra})"
        )
    else:
        docs = decode_ws_frames_dir(args.frames_dir, export_format=args.format)
        if not docs:
            raise SystemExit(f"No FOOTPRINT frames found in {args.frames_dir}")
        best = pick_best_footprint_document(docs)
        assert best is not None
        tf = document_timeframe(best) or "?"
        print(
            f"Decoded {len(docs)} FOOTPRINT frame(s); best has "
            f"{len(best['candles'])} candles ({best.get('symbol')} {tf}, format={args.format})"
        )

    if args.out:
        write_footprint_document(args.out, best)
        print(f"Wrote {args.out}")
    elif args.format == FOOTPRINT_EXPORT_FORMAT_BID_ASK:
        docs = decode_ws_frames_dir(args.frames_dir, export_format=args.format)
        for i, doc in enumerate(docs, 1):
            print(f"  frame {i}: {len(doc['candles'])} candles")


if __name__ == "__main__":
    main()
