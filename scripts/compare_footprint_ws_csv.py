#!/usr/bin/env python3
"""Compare GoCharting WS bar_flow vs CSV export on overlapping candles."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from automation_tool.gocharting_gc_spot_convert import _parse_gc_csv_bar_flow_rows  # noqa: E402
from automation_tool.gocharting_ws_decode import (  # noqa: E402
    FOOTPRINT_EXPORT_FORMAT_RAW,
    bar_flow_from_ws_candle,
    decode_ws_frames_merged,
    document_timeframe,
    enrich_footprint_document_with_ws_bar_flow,
    pick_best_footprint_document,
    decode_ws_frames_dir,
    _price_precision_from_doc,
)

_COMPARE_FIELDS = (
    "delta",
    "max_delta",
    "min_delta",
    "buy_volume",
    "sell_volume",
    "volume",
    "vwap",
    "buyvwap",
    "sellvwap",
    "open",
    "high",
    "low",
    "close",
    "cum_delta",
)


def _load_ws_doc(path: Path) -> dict[str, Any]:
    if path.is_dir():
        doc = decode_ws_frames_merged(path)
        if doc is None:
            raw_docs = decode_ws_frames_dir(path, export_format=FOOTPRINT_EXPORT_FORMAT_RAW)
            doc = pick_best_footprint_document(raw_docs)
        if doc is None:
            raise SystemExit(f"No FOOTPRINT frames in {path}")
        return enrich_footprint_document_with_ws_bar_flow(doc)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid WS JSON: {path}")
    if not any(
        isinstance(c, dict) and isinstance(c.get("bar_flow"), dict)
        for c in (data.get("candles") or [])
    ):
        return enrich_footprint_document_with_ws_bar_flow(data)
    return data


def _values_close(a: Any, b: Any, *, tol: float = 0.6) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return a == b


def compare_ws_csv(
    ws_doc: dict[str, Any],
    csv_path: Path,
    *,
    tol: float = 0.6,
) -> dict[str, Any]:
    csv_rows = _parse_gc_csv_bar_flow_rows(csv_path.read_text(encoding="utf-8"))
    pp = _price_precision_from_doc(ws_doc)

    ws_by_time: dict[str, dict[str, Any]] = {}
    for candle in ws_doc.get("candles") or []:
        if not isinstance(candle, dict):
            continue
        time_key = str(candle.get("time_gmt7") or "").strip()
        if not time_key:
            continue
        bar_flow = candle.get("bar_flow")
        if isinstance(bar_flow, dict):
            ws_bar = bar_flow
        else:
            ws_bar = bar_flow_from_ws_candle(candle, price_precision=pp)
        ws_by_time[time_key] = ws_bar

    overlap = sorted(set(ws_by_time.keys()) & set(csv_rows.keys()))
    field_stats: dict[str, dict[str, int]] = {
        f: {"match": 0, "mismatch": 0, "csv_missing": 0, "ws_missing": 0}
        for f in _COMPARE_FIELDS
    }
    mismatches: list[dict[str, Any]] = []

    for time_key in overlap:
        ws_bar = ws_by_time[time_key]
        csv_bar = csv_rows[time_key]
        for field in _COMPARE_FIELDS:
            cv, wv = csv_bar.get(field), ws_bar.get(field)
            if cv is None and wv is None:
                continue
            if cv is None:
                field_stats[field]["csv_missing"] += 1
            elif wv is None:
                field_stats[field]["ws_missing"] += 1
            elif _values_close(cv, wv, tol=tol):
                field_stats[field]["match"] += 1
            else:
                field_stats[field]["mismatch"] += 1
                if len(mismatches) < 20:
                    mismatches.append(
                        {
                            "time_gmt7": time_key,
                            "field": field,
                            "csv": cv,
                            "ws": wv,
                        }
                    )

    csv_empty = [
        k
        for k, row in csv_rows.items()
        if row.get("delta") is None and row.get("buy_volume") is None
    ]
    ws_can_fill = [k for k in csv_empty if k in ws_by_time and ws_by_time[k].get("delta") is not None]

    return {
        "ws_source": ws_doc.get("symbol"),
        "ws_timeframe": document_timeframe(ws_doc),
        "csv_path": str(csv_path),
        "ws_candles": len(ws_by_time),
        "csv_rows": len(csv_rows),
        "overlap": len(overlap),
        "overlap_range": [overlap[0], overlap[-1]] if overlap else [],
        "field_stats": field_stats,
        "mismatches_sample": mismatches,
        "csv_rows_missing_bar_flow": len(csv_empty),
        "ws_can_fill_csv_gaps": len(ws_can_fill),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare WS bar_flow vs GoCharting CSV")
    parser.add_argument(
        "--ws",
        type=Path,
        required=True,
        help="WS JSON file or ws_frames_* directory",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        required=True,
        help="GoCharting GC CSV export",
    )
    parser.add_argument("--tol", type=float, default=0.6, help="Numeric tolerance")
    parser.add_argument("--out", type=Path, help="Write JSON report")
    args = parser.parse_args()

    ws_doc = _load_ws_doc(args.ws)
    report = compare_ws_csv(ws_doc, args.csv, tol=args.tol)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"Wrote {args.out}")
    print(text)


if __name__ == "__main__":
    main()
