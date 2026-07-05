"""Load and normalize GoCharting footprint JSON (flat or combined v2 format)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def to_num(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value).strip())


def normalize_footprint_level(level: dict[str, Any]) -> dict[str, float]:
    buy_raw = level.get("buy")
    sell_raw = level.get("sell")
    if isinstance(buy_raw, dict):
        buy = to_num(buy_raw.get("volume", buy_raw.get("trades", 0)))
    else:
        buy = to_num(buy_raw)
    if isinstance(sell_raw, dict):
        sell = to_num(sell_raw.get("volume", sell_raw.get("trades", 0)))
    else:
        sell = to_num(sell_raw)
    return {
        "price": to_num(level.get("price", 0)),
        "buy": buy,
        "sell": sell,
    }


def normalize_candle(raw: dict[str, Any]) -> dict[str, Any]:
    footprint = [normalize_footprint_level(lvl) for lvl in raw.get("footprint") or []]
    ohlc = {k: to_num(v) for k, v in (raw.get("ohlc") or {}).items()}
    bar_flow = {k: to_num(v) for k, v in (raw.get("bar_flow") or {}).items()}
    return {
        "date": raw.get("date"),
        "time_gmt7": raw.get("time_gmt7", ""),
        "ohlc": ohlc,
        "bar_flow": bar_flow,
        "footprint": footprint,
        "session_profile": raw.get("session_profile") or {},
    }


def load_footprint_json(path: Path | str) -> dict[str, Any]:
    path = Path(path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    candles = [normalize_candle(c) for c in doc.get("candles") or []]
    interval = (
        doc.get("interval")
        or (doc.get("request") or {}).get("interval")
        or _interval_from_filename(path)
        or ""
    )
    interval = str(interval).lower().strip()
    return {
        "path": str(path.resolve()),
        "symbol": doc.get("symbol", ""),
        "interval": interval,
        "candles": candles,
        "candle_count": len(candles),
    }


def _interval_from_filename(path: Path) -> str:
    name = path.name.lower()
    if "15m" in name:
        return "15m"
    if "5m" in name:
        return "5m"
    return ""
