#!/usr/bin/env python3
"""Auto-detect six scalp footprint patterns on GoCharting footprint JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from candle_confirm import filter_confirmed
from footprint_loader import load_footprint_json
from patterns import PATTERNS, detect_patterns


def _default_combined_paths(charts_dir: Path) -> list[Path]:
    base = charts_dir / "footprint_images"
    return [
        base / "footprint_combined_5m.json",
        base / "footprint_combined_15m.json",
    ]


def _format_signal_text(sig: dict) -> str:
    tp = sig.get("take_profit") or []
    tp_str = f"{tp[0]}–{tp[1]}" if len(tp) == 2 else str(tp)
    bf = sig.get("bar_flow") or {}
    side = sig.get("side") or ("BUY" if sig.get("direction") == "long" else "SELL")
    order = sig.get("order") or f"{side} {sig.get('entry_type', '').upper()} @ {sig.get('entry_price')}"
    lines = [
        f">>> {side} <<<  {order}",
        f"  pattern: {sig['pattern_id']} — {sig['pattern_name']}",
        f"  time:    {sig.get('time_gmt7', '')}",
        f"  bar:     #{sig.get('bar_index')}  tf: {sig.get('timeframe')}",
        f"  flow:    delta={bf.get('delta', 0):+.0f}  cot_low={bf.get('cot_low', 0):+.0f}  cot_high={bf.get('cot_high', 0):+.0f}",
        f"  note:    {sig.get('entry_hint')}",
        f"  SL:      {sig.get('stop_loss')}  TP: {tp_str}",
    ]
    metrics = sig.get("metrics") or {}
    tier = metrics.get("candle_tier")
    if tier:
        lines.append(f"  candle:  tier {tier} (footprint + candle confirmed)")
    extra = {k: v for k, v in metrics.items() if k not in ("delta", "cot_low", "cot_high", "candle_tier")}
    if extra:
        lines.append(f"  extra:  {extra}")
    return "\n".join(lines)


def run_on_file(
    path: Path,
    *,
    latest_only: bool,
    pattern_ids: set[str] | None,
    as_json: bool,
    confirmed_only: bool,
) -> list[dict]:
    doc = load_footprint_json(path)
    signals = detect_patterns(
        doc["candles"],
        interval=doc["interval"],
        pattern_ids=pattern_ids,
        latest_only=latest_only,
    )
    if confirmed_only:
        signals = filter_confirmed(signals, doc["candles"], interval=doc["interval"])
    out = [s.to_dict() for s in signals]
    if as_json:
        payload = {
            "source": doc["path"],
            "symbol": doc["symbol"],
            "interval": doc["interval"],
            "candle_count": doc["candle_count"],
            "signal_count": len(out),
            "signals": out,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"\n{'=' * 72}")
        print(f"{path.name}  ({doc['interval']}, {doc['candle_count']} candles)  →  {len(out)} signal(s)")
        print(f"{'=' * 72}")
        if not out:
            print("  (no patterns matched)")
        for sig in out:
            print(_format_signal_text(sig))
            print()
    return out


def main(argv: list[str] | None = None) -> int:
    pattern_ids = {p.id for p in PATTERNS}
    parser = argparse.ArgumentParser(
        description="Detect six scalp footprint patterns on GoCharting JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Patterns:
  cot_trap_short      SHORT limit @ bar high     (5m, 15m)
  sell_stack_short    SHORT limit @ bar high     (15m)
  sell_climax_short   SHORT limit @ 50%% retrace  (15m)
  exhaustion_long     LONG  market @ close       (5m)
  v_reversal_long     LONG  market @ close       (5m, 2-bar)
  sweep_absorb_long   LONG  market @ close       (5m)

Examples:
  python scripts/scalp_footprint/detect.py data/XAUUSD/charts/footprint_images/footprint_combined_5m.json
  python scripts/scalp_footprint/detect.py --latest --charts-dir data/XAUUSD/charts
  python scripts/scalp_footprint/detect.py -f combined_5m.json combined_15m.json --json
        """,
    )
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        help="Footprint JSON file(s). If omitted, uses footprint_combined_5m/15m under --charts-dir.",
    )
    parser.add_argument(
        "--charts-dir",
        type=Path,
        default=None,
        help="Charts root (default: data/<active>/charts from coinmap or data/XAUUSD/charts)",
    )
    parser.add_argument(
        "--latest",
        action="store_true",
        help="Only evaluate the last closed candle",
    )
    parser.add_argument(
        "--pattern",
        action="append",
        dest="patterns",
        metavar="ID",
        choices=sorted(pattern_ids),
        help="Restrict to pattern id (repeatable)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON instead of human-readable text",
    )
    parser.add_argument(
        "--confirmed",
        action="store_true",
        help="M5: keep only footprint signals confirmed by candle structure (higher win rate)",
    )
    parser.add_argument(
        "--list-patterns",
        action="store_true",
        help="List pattern ids and exit",
    )
    args = parser.parse_args(argv)

    if args.list_patterns:
        for p in PATTERNS:
            tfs = ", ".join(sorted(p.timeframes))
            print(f"{p.id:<22} {p.direction.value:<6} {p.entry_type.value:<6} [{tfs}]  {p.description}")
        return 0

    paths = list(args.files)
    if not paths:
        charts_dir = args.charts_dir
        if charts_dir is None:
            try:
                from automation_tool.config import load_settings

                sym = load_settings().main_chart_symbol
                charts_dir = Path("data") / sym / "charts"
            except Exception:
                charts_dir = Path("data/XAUUSD/charts")
        paths = [p for p in _default_combined_paths(charts_dir) if p.is_file()]
        if not paths:
            print("No footprint JSON files found. Pass file path(s) or set --charts-dir.", file=sys.stderr)
            return 1

    selected = set(args.patterns) if args.patterns else None
    total = 0
    for path in paths:
        if not path.is_file():
            print(f"Skip missing file: {path}", file=sys.stderr)
            continue
        total += len(
            run_on_file(
                path,
                latest_only=args.latest,
                pattern_ids=selected,
                as_json=args.json,
                confirmed_only=args.confirmed,
            )
        )

    if not args.json and len(paths) > 1:
        print(f"Total signals across {len(paths)} file(s): {total}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
