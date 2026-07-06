"""Per-candle footprint metrics for scalp strategy detection."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from automation_tool.gocharting_footprint_derived import (  # noqa: E402
    DerivedConfig,
    compute_candle_orderflow,
    derived_config_from_cfg,
    tick_size_from_footprint_doc,
)
from automation_tool.gocharting_ws_decode import enrich_footprint_document_with_ws_bar_flow  # noqa: E402

from footprint_loader import normalize_candle  # noqa: E402


def candle_poc(candle: dict[str, Any]) -> float | None:
    levels = candle.get("footprint") or []
    if not levels:
        return None
    best = max(levels, key=lambda lvl: float(lvl.get("buy", 0)) + float(lvl.get("sell", 0)))
    return float(best.get("price", 0))


def candle_volume(candle: dict[str, Any]) -> float:
    ohlc = candle.get("ohlc") or {}
    if ohlc.get("volume"):
        return float(ohlc["volume"])
    bf = candle.get("bar_flow") or {}
    return float(bf.get("volume") or bf.get("buy_volume", 0) + bf.get("sell_volume", 0))


def candle_delta(candle: dict[str, Any]) -> float:
    return float((candle.get("bar_flow") or {}).get("delta", 0))


def is_bull(candle: dict[str, Any]) -> bool:
    o, c = candle["ohlc"]["open"], candle["ohlc"]["close"]
    return c > o


def is_bear(candle: dict[str, Any]) -> bool:
    o, c = candle["ohlc"]["open"], candle["ohlc"]["close"]
    return c < o


def bar_range(candle: dict[str, Any]) -> float:
    h, l = candle["ohlc"]["high"], candle["ohlc"]["low"]
    return h - l


def body_mid(candle: dict[str, Any]) -> float:
    o, c = candle["ohlc"]["open"], candle["ohlc"]["close"]
    return (o + c) / 2.0


def has_absorption_at(candle: dict[str, Any], *, side: str) -> bool:
    """``side`` is BID (absorption at low) or ASK (absorption at high)."""
    for item in (candle.get("orderflow") or {}).get("absorption") or []:
        if str(item.get("side", "")).upper() == side.upper():
            return True
    return False


def max_stacked_side(candle: dict[str, Any], *, side: str) -> int:
    """Longest consecutive stacked run for BID or ASK in this candle."""
    runs = (candle.get("orderflow") or {}).get("stacked_in_candle") or []
    best = 0
    for run in runs:
        if str(run.get("side", "")).upper() != side.upper():
            continue
        best = max(best, int(run.get("level_count") or 0))
    return best


def delta_weakening_same_sign(deltas: list[float], *, min_first: float = 40.0) -> bool:
    """Same-sign deltas fading in magnitude (e.g. +220 → +110 → +35)."""
    if len(deltas) < 3:
        return False
    if all(d > 0 for d in deltas):
        return deltas[0] >= min_first and deltas[0] > deltas[1] > deltas[2] > 0
    if all(d < 0 for d in deltas):
        return abs(deltas[0]) >= min_first and deltas[0] < deltas[1] < deltas[2] < 0
    return False


def enrich_document(
    doc: dict[str, Any],
    *,
    cfg: dict[str, Any] | None = None,
    for_backtest: bool = False,
) -> list[dict[str, Any]]:
    """Normalize candles, attach bar_flow, orderflow, and per-bar POC."""
    cfg_raw = cfg or {}
    raw_doc = dict(doc)
    if not any(isinstance(c, dict) and c.get("bar_flow") for c in raw_doc.get("candles") or []):
        raw_doc = enrich_footprint_document_with_ws_bar_flow(raw_doc, cfg=cfg_raw)

    derived_cfg = derived_config_from_cfg(cfg_raw, raw_doc)
    if for_backtest:
        derived_cfg = DerivedConfig(
            imbalance_enabled=True,
            stacked_enabled=True,
            absorption_enabled=True,
            rl_min=derived_cfg.rl_min,
            stacked_min_levels=max(2, derived_cfg.stacked_min_levels),
            absorption_volume_pct=derived_cfg.absorption_volume_pct,
            absorption_extreme_ticks=derived_cfg.absorption_extreme_ticks,
            absorption_side_ratio=derived_cfg.absorption_side_ratio,
            tick_size=tick_size_from_footprint_doc(raw_doc),
        )

    out: list[dict[str, Any]] = []
    for raw in raw_doc.get("candles") or []:
        c = normalize_candle(raw)
        orderflow = compute_candle_orderflow(c, cfg=derived_cfg)
        if orderflow:
            c["orderflow"] = orderflow
        poc = candle_poc(c)
        if poc is not None:
            c["poc"] = poc
        out.append(c)
    return out


def session_volume_median(candles: list[dict[str, Any]]) -> float:
    vols = sorted(candle_volume(c) for c in candles if candle_volume(c) > 0)
    if not vols:
        return 100.0
    return vols[len(vols) // 2]


def range_mid(candle: dict[str, Any]) -> float:
    h, l = candle["ohlc"]["high"], candle["ohlc"]["low"]
    return (h + l) / 2.0
