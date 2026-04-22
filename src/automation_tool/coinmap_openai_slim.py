"""
Reduce Coinmap API export size (token limits, analysis focus).

Primary use: **writing to disk** in ``coinmap._write_coinmap_api_shot_json`` when
``api_data_export.slim_export_on_disk`` is true. The same function may be used when
embedding in OpenAI if ``COINMAP_OPENAI_SLIM=true``.

Rules (defaults, overridable via env):

* **15m**: 30 candles, 10–12 nearest footprints → default 30 bars + 11 orderflow bars.
* **5m**: 30–40 candles, 12–20 nearest footprints → default 35 bars + 16 orderflow bars.

Arrays are assumed **newest first** (Coinmap). VWAP rows are filtered to candle ``t`` values kept.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
        return int(raw)
    return default


def slim_limits_for_interval(interval: str) -> Optional[tuple[int, int]]:
    """
    Return (max_candles, max_orderflow_bars) or None if interval not slimmed by default.
    """
    iv = interval.strip().lower()
    if iv == "15m":
        return (
            _env_int("COINMAP_OPENAI_BARS_15M", 60),
            _env_int("COINMAP_OPENAI_FP_15M", 11),
        )
    if iv == "5m":
        return (
            _env_int("COINMAP_OPENAI_BARS_5M", 60),
            _env_int("COINMAP_OPENAI_FP_5M", 16),
        )
    return None


def _interval_from_json(data: dict[str, Any], path: Optional[Path]) -> str:
    iv = str(data.get("interval") or "").strip()
    if iv:
        return iv
    if path is not None:
        stem = path.stem
        if "_coinmap_" in stem:
            tail = stem.split("_coinmap_", 1)[-1]
            parts = tail.rsplit("_", 1)
            if len(parts) == 2 and parts[1]:
                return parts[1].strip()
    return ""


def slim_coinmap_export_for_openai(
    data: dict[str, Any],
    *,
    path: Optional[Path] = None,
) -> dict[str, Any]:
    """
    Return a shallow-safe copy of ``data`` with trimmed ``getcandlehistory``,
    ``getcandlehistorycvd`` (same bar cap as candles), ``getorderflowhistory``, and filtered
    ``getindicatorsvwap``. Other intervals unchanged.
    """
    if not isinstance(data, dict):
        return data
    interval = _interval_from_json(data, path)
    limits = slim_limits_for_interval(interval)
    if limits is None:
        return data
    n_candles, n_fp = limits

    out: dict[str, Any] = {}
    for k in ("generated_at", "stamp", "symbol", "interval", "watchlist_category"):
        if k in data:
            out[k] = data[k]

    ch = data.get("getcandlehistory")
    if isinstance(ch, list):
        out["getcandlehistory"] = ch[:n_candles]
    else:
        out["getcandlehistory"] = ch

    cvd = data.get("getcandlehistorycvd")
    if isinstance(cvd, list):
        out["getcandlehistorycvd"] = cvd[:n_candles]
    else:
        out["getcandlehistorycvd"] = cvd

    of = data.get("getorderflowhistory")
    if isinstance(of, list):
        out["getorderflowhistory"] = of[:n_fp]
    else:
        out["getorderflowhistory"] = of

    candle_ts: set[Any] = set()
    ch2 = out.get("getcandlehistory")
    if isinstance(ch2, list):
        for b in ch2:
            if isinstance(b, dict) and b.get("t") is not None:
                candle_ts.add(b["t"])

    vw = data.get("getindicatorsvwap")
    if isinstance(vw, list) and candle_ts:
        out["getindicatorsvwap"] = [
            row for row in vw if isinstance(row, dict) and row.get("t") in candle_ts
        ]
    else:
        out["getindicatorsvwap"] = vw

    return out


def should_slim_coinmap_json_path(path: Path) -> bool:
    n = path.name.lower()
    if path.suffix.lower() == ".json" and (
        n.endswith("_merged.json") or n.endswith("_openai_coinmap_merged.json")
    ):
        return False
    return path.suffix.lower() == ".json" and "_coinmap_" in path.name
