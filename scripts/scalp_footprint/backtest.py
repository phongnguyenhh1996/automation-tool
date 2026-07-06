#!/usr/bin/env python3
"""Backtest all scalp footprint patterns on GoCharting footprint JSON."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from backtest_engine import run_backtest, summarize_results  # noqa: E402
from footprint_loader import load_footprint_json  # noqa: E402
from footprint_metrics import enrich_document  # noqa: E402
from patterns import PATTERNS, detect_patterns  # noqa: E402

PATTERN_IDS = tuple(p.id for p in PATTERNS)

# Last full trading week before Jul 6 2026 (Mon–Fri)
DEFAULT_FROM_DATE = "2026-06-29"
DEFAULT_TO_DATE = "2026-07-03"


def _default_paths(charts_dir: Path) -> list[Path]:
    base = charts_dir / "footprint_images"
    return [base / f"footprint_combined_{tf}.json" for tf in ("5m", "15m")]


def _candle_calendar_date(candle: dict[str, Any]) -> str:
    sd = candle.get("session_date")
    if sd:
        return str(sd).strip()[:10]
    raw = candle.get("date")
    if isinstance(raw, str) and len(raw) >= 10:
        return raw[:10]
    m = re.search(r"(\w{3}) (\w{3}) (\d+) (\d{4})", candle.get("time_gmt7", ""))
    if m:
        return datetime.strptime(" ".join(m.groups()), "%a %b %d %Y").date().isoformat()
    return ""


def filter_candles_by_date(
    candles: list[dict[str, Any]],
    *,
    from_date: str | None,
    to_date: str | None,
) -> list[dict[str, Any]]:
    if not from_date and not to_date:
        return candles
    lo = from_date or "0000-01-01"
    hi = to_date or "9999-12-31"
    return [c for c in candles if lo <= _candle_calendar_date(c) <= hi]


def _format_trade(r: dict) -> str:
    tp = r.get("take_profit")
    tier = (r.get("metrics") or {}).get("tier", "")
    tier_s = f" ({tier})" if tier else ""
    lines = [
        f"  [{r.get('outcome')}] {r.get('side')} {r.get('pattern_id')}{tier_s}",
        f"    entry #{r.get('bar_index')} @ {r.get('entry_price')}  →  exit #{r.get('exit_bar')} @ {r.get('exit_price')}",
        f"    PnL: {r.get('pnl'):+.2f}  ({r.get('bars_held')} bars)  SL={r.get('stop_loss')} TP={tp}",
        f"    {r.get('time_gmt7', '')}",
        f"    {r.get('entry_hint', '')}",
    ]
    return "\n".join(lines)


def run_file(
    path: Path,
    *,
    pattern_ids: set[str] | None,
    as_json: bool,
    max_bars: int,
    gocharting_yaml: Path | None,
    from_date: str | None,
    to_date: str | None,
) -> dict:
    doc = load_footprint_json(path)
    cfg: dict = {}
    if gocharting_yaml and gocharting_yaml.is_file():
        import yaml

        cfg = yaml.safe_load(gocharting_yaml.read_text(encoding="utf-8")) or {}

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    all_candles = enrich_document(raw, cfg=cfg, for_backtest=True)
    candles = filter_candles_by_date(all_candles, from_date=from_date, to_date=to_date)
    interval = doc["interval"] or raw.get("interval") or _interval_from_path(path)

    signals = detect_patterns(
        candles,
        interval=interval,
        pattern_ids=pattern_ids,
    )
    results = run_backtest(candles, signals, max_bars=max_bars)
    summary = summarize_results(results)
    payload = {
        "source": str(path.resolve()),
        "symbol": doc["symbol"],
        "interval": interval,
        "date_range": {"from": from_date, "to": to_date},
        "candle_count": len(candles),
        "signal_count": len(signals),
        "summary": summary,
        "trades": [r.to_dict() for r in results],
    }

    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        dr = f"{from_date or '…'} → {to_date or '…'}"
        print(f"\n{'=' * 72}")
        print(f"{path.name}  ({interval}, {len(candles)} candles, {dr})")
        print(f"{'=' * 72}")
        ov = summary["overall"]
        print(
            f"Overall: {ov['filled']} filled / {ov['no_fill']} no-fill | "
            f"W/L/T = {ov['wins']}/{ov['losses']}/{ov['timeouts']} | "
            f"win {ov['win_rate']}% | PnL {ov['total_pnl']:+.2f} pts"
        )
        for pid, st in summary["by_strategy"].items():
            nf = st.get("no_fill", 0)
            nf_s = f" / {nf} no-fill" if nf else ""
            print(
                f"  {pid}: {st['filled']} filled{nf_s} | win {st['win_rate']}% | "
                f"PnL {st['total_pnl']:+.2f} | avg {st['avg_pnl']:+.2f}"
            )
        if not results:
            print("  (no signals)")
        for r in results:
            print(_format_trade(r.to_dict()))
            print()

    return payload


def _interval_from_path(path: Path) -> str:
    name = path.stem.lower()
    if "15m" in name:
        return "15m"
    if "5m" in name:
        return "5m"
    return "5m"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backtest all scalp footprint patterns on combined footprint JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Patterns (6 setups):
  sell_stack_short, sell_climax_short                       (SHORT 15m)
  exhaustion_long, v_reversal_long, sweep_absorb_long       (LONG 5m)
  absorption_trap_long, absorption_trap_short               (LONG/SHORT 5m, A+)

Examples:
  python scripts/scalp_footprint/backtest.py
  python scripts/scalp_footprint/backtest.py --from 2026-06-29 --to 2026-07-03
  python scripts/scalp_footprint/backtest.py -f footprint_combined_5m.json --json
        """,
    )
    parser.add_argument("files", nargs="*", type=Path, help="Footprint JSON file(s)")
    parser.add_argument("--charts-dir", type=Path, default=None)
    parser.add_argument(
        "--pattern",
        action="append",
        dest="patterns",
        choices=PATTERN_IDS,
        help="Restrict to pattern id (repeatable)",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--max-bars", type=int, default=12, help="Max bars to hold (default 12)")
    parser.add_argument(
        "--from",
        dest="from_date",
        default=DEFAULT_FROM_DATE,
        help=f"Include candles from date YYYY-MM-DD (default {DEFAULT_FROM_DATE})",
    )
    parser.add_argument(
        "--to",
        dest="to_date",
        default=DEFAULT_TO_DATE,
        help=f"Include candles through date YYYY-MM-DD (default {DEFAULT_TO_DATE})",
    )
    parser.add_argument(
        "--all-dates",
        action="store_true",
        help="Use full file range (ignore --from/--to)",
    )
    parser.add_argument(
        "--gocharting-config",
        type=Path,
        default=Path("config/gocharting.yaml"),
        help="GoCharting YAML for derived orderflow params",
    )
    args = parser.parse_args(argv)

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
        paths = [p for p in _default_paths(charts_dir) if p.is_file()]

    if not paths:
        print("No footprint JSON found.", file=sys.stderr)
        return 1

    from_date = None if args.all_dates else args.from_date
    to_date = None if args.all_dates else args.to_date
    selected = set(args.patterns) if args.patterns else None

    for path in paths:
        if not path.is_file():
            print(f"Skip missing: {path}", file=sys.stderr)
            continue
        run_file(
            path,
            pattern_ids=selected,
            as_json=args.json,
            max_bars=args.max_bars,
            gocharting_yaml=args.gocharting_config,
            from_date=from_date,
            to_date=to_date,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
