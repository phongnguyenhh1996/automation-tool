#!/usr/bin/env python3
"""Re-check all tracked scalp trades against latest footprint JSON."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent
if str(_SCRIPT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT))

from signal_tracker import (  # noqa: E402
    DEFAULT_MAX_HOLD_BARS,
    DEFAULT_TRADES_NAME,
    format_outcome_message,
    recheck_all_trades,
)
from watch import load_closed_candles  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile all trades in scalp_footprint_trades.json")
    parser.add_argument(
        "--charts-dir",
        type=Path,
        default=Path("data/XAUUSD/charts"),
    )
    parser.add_argument(
        "--trades-file",
        type=Path,
        default=None,
    )
    parser.add_argument("--max-hold-bars", type=int, default=DEFAULT_MAX_HOLD_BARS)
    args = parser.parse_args(argv)

    charts = args.charts_dir
    trades_path = args.trades_file or (charts / DEFAULT_TRADES_NAME)
    if not trades_path.is_file():
        print(f"No trades file: {trades_path}", file=sys.stderr)
        return 1

    candles_by_iv: dict[str, list] = {}
    for iv in ("5m", "15m"):
        fp = charts / "footprint_images" / f"footprint_combined_{iv}.json"
        if fp.is_file():
            candles_by_iv[iv] = load_closed_candles(fp, interval=iv)
            print(f"{fp.name}: {len(candles_by_iv[iv])} closed candles")

    trades = recheck_all_trades(
        trades_path,
        candles_by_iv,
        max_bars=args.max_hold_bars,
    )

    print(f"\n{'=' * 60}")
    print(f"Rechecked {len(trades)} trade(s) → saved {trades_path}")
    print(f"{'=' * 60}")
    for t in trades:
        sig = t.get("signal") or {}
        status = t.get("status")
        was = t.get("_was_status")
        changed = f" (was {was})" if was else ""
        print(f"\n[{status}]{changed} {sig.get('pattern_id')} @ {t.get('entry_time_gmt7', '')[:28]}")
        if status != "OPEN":
            print(format_outcome_message(t))
        else:
            print(
                f"  still OPEN  entry={t.get('entry_price')} SL={t.get('stop_loss')} "
                f"TP={t.get('take_profit')}  last_checked={t.get('last_checked_time_gmt7', '—')}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
