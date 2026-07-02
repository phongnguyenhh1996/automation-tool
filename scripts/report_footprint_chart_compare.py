#!/usr/bin/env python3
"""Print GoCharting chart comparison sheet from captured footprint combined JSON."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from automation_tool.gocharting_capture import load_gocharting_yaml  # noqa: E402
from automation_tool.gocharting_ws_decode import (  # noqa: E402
    candle_sort_datetime,
    enrich_footprint_document_with_ws_bar_flow,
)

_TZ_GMT7 = timezone(timedelta(hours=7))


def _latest_combined_path(charts_dir: Path, interval: str) -> Path | None:
    fp_dir = charts_dir / "footprint_images"
    path = fp_dir / f"footprint_combined_{interval.strip().lower()}.json"
    return path if path.is_file() else None


def build_chart_compare_report(doc: dict, *, cfg: dict) -> dict:
    candles = sorted(
        [c for c in (doc.get("candles") or []) if isinstance(c, dict)],
        key=candle_sort_datetime,
    )
    if not candles:
        raise ValueError("no candles in document")

    last = candles[-1]
    sessions = doc.get("session_profiles") or []
    current_session = sessions[-1] if sessions else {}

    recent: list[dict] = []
    for c in candles[-10:]:
        bf = c.get("bar_flow") if isinstance(c.get("bar_flow"), dict) else {}
        ohlc = c.get("ohlc") if isinstance(c.get("ohlc"), dict) else {}
        sp = c.get("session_profile") if isinstance(c.get("session_profile"), dict) else {}
        recent.append(
            {
                "time_gmt7": c.get("time_gmt7"),
                "ohlc": ohlc,
                "bar_flow": {
                    k: bf.get(k)
                    for k in (
                        "delta",
                        "cum_delta",
                        "max_delta",
                        "min_delta",
                        "buy_volume",
                        "sell_volume",
                        "volume",
                        "vwap",
                        "buyvwap",
                        "sellvwap",
                    )
                },
                "session_profile": sp,
            }
        )

    return {
        "generated_at_gmt7": datetime.now(_TZ_GMT7).strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": doc.get("symbol"),
        "interval": doc.get("timeframe") or doc.get("interval"),
        "candle_count": len(candles),
        "session_profiles": sessions,
        "current_session": {
            "session_key": current_session.get("session_key"),
            "session_start": current_session.get("session_start"),
            "session_end": current_session.get("session_end"),
            "poc": current_session.get("poc"),
            "vah": current_session.get("vah"),
            "val": current_session.get("val"),
            "vwap": current_session.get("vwap"),
            "value_area_pct": current_session.get("value_area_pct"),
            "total_volume": current_session.get("total_volume"),
            "value_area_volume": current_session.get("value_area_volume"),
        },
        "latest_closed_candle": {
            "time_gmt7": last.get("time_gmt7"),
            "ohlc": last.get("ohlc"),
            "bar_flow": last.get("bar_flow"),
            "session_profile": last.get("session_profile"),
        },
        "recent_candles": recent,
        "gocharting_checklist": [
            "Mở chart GC1! 5m ETH trên GoCharting (cùng block_multiplier/tick như config).",
            "Session profile: so POC / VAH / VAL với Value Area trên chart.",
            "Per candle: so Delta, Cum Delta, Buy/Sell Vol, VWAP với CSV hoặc tooltip bar.",
            f"Block size chart = tick_size × block_multiplier = {cfg.get('footprint_ws', {}).get('tick_size', 0.1)} × {cfg.get('footprint_ws', {}).get('block_multiplier', 4)}.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build GoCharting chart compare report")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "gocharting.yaml")
    parser.add_argument(
        "--charts-dir",
        type=Path,
        default=ROOT / "data" / "XAUUSD" / "charts",
    )
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--json", type=Path, help="Input combined JSON (default: latest capture)")
    parser.add_argument("--out", type=Path, help="Write report JSON")
    args = parser.parse_args()

    cfg = load_gocharting_yaml(args.config)
    src = args.json or _latest_combined_path(args.charts_dir, args.interval)
    if src is None or not src.is_file():
        raise SystemExit(f"No footprint combined JSON found: {src}")

    doc = json.loads(src.read_text(encoding="utf-8"))
    if not any(isinstance(c, dict) and c.get("bar_flow") for c in (doc.get("candles") or [])):
        doc = enrich_footprint_document_with_ws_bar_flow(doc, cfg=cfg)

    report = build_chart_compare_report(doc, cfg=cfg)
    report["source_json"] = str(src)
    text = json.dumps(report, ensure_ascii=False, indent=2)

    out = args.out
    if out is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = ROOT / "data" / "network_sniff" / f"footprint_chart_compare_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")

    cs = report["current_session"]
    lc = report["latest_closed_candle"]
    bf = lc.get("bar_flow") or {}
    print(f"Source: {src}")
    print(f"Report: {out}")
    print()
    print("=== SESSION PROFILE (so với GoCharting Value Area) ===")
    print(f"  Session: {cs.get('session_key')}  ({cs.get('session_start')} → {cs.get('session_end')})")
    print(f"  POC: {cs.get('poc')}  |  VAH: {cs.get('vah')}  |  VAL: {cs.get('val')}  |  VWAP: {cs.get('vwap')}")
    print(f"  VA target: {float(cs.get('value_area_pct', 0.7)) * 100:.0f}%  |  session vol: {cs.get('total_volume')}")
    print()
    print("=== LATEST CANDLE ===")
    print(f"  Time: {lc.get('time_gmt7')}")
    ohlc = lc.get("ohlc") or {}
    print(f"  OHLC: O={ohlc.get('open')} H={ohlc.get('high')} L={ohlc.get('low')} C={ohlc.get('close')}")
    print(f"  Delta={bf.get('delta')}  CumDelta={bf.get('cum_delta')}  Vol={bf.get('volume')}")
    print(f"  VWAP={bf.get('vwap')}  BuyVWAP={bf.get('buyvwap')}  SellVWAP={bf.get('sellvwap')}")


if __name__ == "__main__":
    main()
