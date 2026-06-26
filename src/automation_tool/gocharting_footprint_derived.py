from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

DEFAULT_RL_MIN = 4.0
DEFAULT_STACKED_MIN_LEVELS = 3
DEFAULT_ABSORPTION_VOLUME_PCT = 0.25
DEFAULT_ABSORPTION_EXTREME_TICKS = 2
DEFAULT_ABSORPTION_SIDE_RATIO = 1.5
DEFAULT_TICK_SIZE = 0.1


@dataclass(frozen=True)
class DerivedConfig:
    imbalance_enabled: bool = True
    stacked_enabled: bool = True
    absorption_enabled: bool = True
    rl_min: float = DEFAULT_RL_MIN
    stacked_min_levels: int = DEFAULT_STACKED_MIN_LEVELS
    absorption_volume_pct: float = DEFAULT_ABSORPTION_VOLUME_PCT
    absorption_extreme_ticks: int = DEFAULT_ABSORPTION_EXTREME_TICKS
    absorption_side_ratio: float = DEFAULT_ABSORPTION_SIDE_RATIO
    tick_size: float = DEFAULT_TICK_SIZE


def footprint_derived_enabled(cfg: dict[str, Any]) -> bool:
    raw_env = os.getenv("FOOTPRINT_DERIVED_ENABLED", "").strip().lower()
    if raw_env in ("0", "false", "no", "off"):
        return False
    if raw_env in ("1", "true", "yes", "on"):
        return True
    ws = cfg.get("footprint_ws")
    if not isinstance(ws, dict):
        return False
    derived = ws.get("derived")
    if not isinstance(derived, dict):
        return True
    return bool(derived.get("enabled", True))


def derived_config_from_cfg(cfg: dict[str, Any], doc: dict[str, Any] | None = None) -> DerivedConfig:
    ws = cfg.get("footprint_ws") if isinstance(cfg.get("footprint_ws"), dict) else {}
    derived = ws.get("derived") if isinstance(ws.get("derived"), dict) else {}
    tick_size = _tick_size_from_doc(doc) if doc else DEFAULT_TICK_SIZE
    return DerivedConfig(
        imbalance_enabled=_bool_param(derived, "imbalance_enabled", True),
        stacked_enabled=_bool_param(derived, "stacked_enabled", True),
        absorption_enabled=_bool_param(derived, "absorption_enabled", True),
        rl_min=_float_param(derived, "rl_min", DEFAULT_RL_MIN),
        stacked_min_levels=_int_param(derived, "stacked_min_levels", DEFAULT_STACKED_MIN_LEVELS),
        absorption_volume_pct=_float_param(
            derived, "absorption_volume_pct", DEFAULT_ABSORPTION_VOLUME_PCT
        ),
        absorption_extreme_ticks=_int_param(
            derived, "absorption_extreme_ticks", DEFAULT_ABSORPTION_EXTREME_TICKS
        ),
        absorption_side_ratio=_float_param(
            derived, "absorption_side_ratio", DEFAULT_ABSORPTION_SIDE_RATIO
        ),
        tick_size=tick_size,
    )


def _bool_param(raw: dict[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("0", "false", "no", "off"):
            return False
        if normalized in ("1", "true", "yes", "on"):
            return True
    return bool(value)


def _float_param(raw: dict[str, Any], key: str, default: float) -> float:
    value = raw.get(key)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_param(raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key)
    if value is None:
        return default
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def tick_size_from_footprint_doc(doc: dict[str, Any]) -> float:
    return _tick_size_from_doc(doc)


def _tick_size_from_doc(doc: dict[str, Any]) -> float:
    fp_day = doc.get("fp_day")
    if isinstance(fp_day, dict):
        try:
            price_precision = max(0, int(fp_day.get("price_precision") or 0))
        except (TypeError, ValueError):
            price_precision = 0
        for key in ("display_tick_size", "tick_size"):
            raw_val = fp_day.get(key)
            if raw_val is None:
                continue
            try:
                raw = float(raw_val)
            except (TypeError, ValueError):
                continue
            if raw <= 0:
                continue
            if price_precision:
                return raw / (10**price_precision)
            return raw / 10.0 if raw >= 10 else raw
    return DEFAULT_TICK_SIZE


def compute_level_rl(bid: int, ask: int) -> tuple[float | None, str | None]:
    """Return ``(RL, side)`` for one price level; ``(None, None)`` when both volumes are zero."""
    if bid == 0 and ask == 0:
        return None, None
    if bid == ask:
        return 1.0, None
    dominant = max(bid, ask)
    weaker = max(1, min(bid, ask))
    side = "BID" if bid > ask else "ASK"
    return round(dominant / weaker, 2), side


def _level_volumes(level: dict[str, Any]) -> tuple[float, int, int]:
    price = level.get("price")
    buy = level.get("buy") if isinstance(level.get("buy"), dict) else {}
    sell = level.get("sell") if isinstance(level.get("sell"), dict) else {}
    bid = int(buy.get("volume") or 0)
    ask = int(sell.get("volume") or 0)
    try:
        price_f = float(price) if price is not None else 0.0
    except (TypeError, ValueError):
        price_f = 0.0
    return price_f, bid, ask


def _parse_footprint_levels(candle: dict[str, Any]) -> list[dict[str, Any]]:
    footprint = candle.get("footprint")
    if not isinstance(footprint, list):
        return []
    out: list[dict[str, Any]] = []
    for level in footprint:
        if not isinstance(level, dict):
            continue
        price, bid, ask = _level_volumes(level)
        if price <= 0:
            continue
        rl, side = compute_level_rl(bid, ask)
        out.append(
            {
                "price": price,
                "bid": bid,
                "ask": ask,
                "rl": rl,
                "side": side,
                "total_vol": bid + ask,
            }
        )
    return out


def compute_imbalance_levels(
    levels: list[dict[str, Any]],
    *,
    rl_min: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for level in levels:
        rl = level.get("rl")
        side = level.get("side")
        if rl is None or side is None:
            continue
        if float(rl) < rl_min:
            continue
        out.append(
            {
                "price": level["price"],
                "bid": level["bid"],
                "ask": level["ask"],
                "rl": rl,
                "side": side,
            }
        )
    return out


def compute_stacked_in_candle(
    levels: list[dict[str, Any]],
    *,
    rl_min: float,
    stacked_min_levels: int,
) -> list[dict[str, Any]]:
    """Detect consecutive price levels (high→low) with same side and RL >= rl_min."""
    if stacked_min_levels <= 0 or len(levels) < stacked_min_levels:
        return []

    runs: list[dict[str, Any]] = []
    start = 0
    while start < len(levels):
        side = levels[start].get("side")
        rl = levels[start].get("rl")
        if side is None or rl is None or float(rl) < rl_min:
            start += 1
            continue
        end = start + 1
        while end < len(levels):
            next_side = levels[end].get("side")
            next_rl = levels[end].get("rl")
            if next_side != side or next_rl is None or float(next_rl) < rl_min:
                break
            end += 1
        count = end - start
        if count >= stacked_min_levels:
            block = levels[start:end]
            rl_values = [float(item["rl"]) for item in block if item.get("rl") is not None]
            runs.append(
                {
                    "side": side,
                    "prices": [item["price"] for item in block],
                    "level_count": count,
                    "rl_min": round(min(rl_values), 2) if rl_values else rl_min,
                    "rl_max": round(max(rl_values), 2) if rl_values else rl_min,
                }
            )
        start = end if end > start else start + 1
    return runs


def _volume_threshold(levels: list[dict[str, Any]], volume_pct: float) -> float:
    totals = [float(level.get("total_vol") or 0) for level in levels]
    positives = sorted(v for v in totals if v > 0)
    if not positives:
        return 0.0
    if volume_pct <= 0:
        return positives[0]
    if volume_pct >= 1:
        return positives[-1]
    idx = max(0, min(len(positives) - 1, int((1.0 - volume_pct) * (len(positives) - 1))))
    return positives[idx]


def _near_price(price: float, target: float, *, tick_size: float, extreme_ticks: int) -> bool:
    band = max(tick_size, 0.0) * max(0, extreme_ticks)
    return abs(price - target) <= band + 1e-9


def compute_absorption_for_candle(
    candle: dict[str, Any],
    levels: list[dict[str, Any]],
    *,
    cfg: DerivedConfig,
) -> list[dict[str, Any]]:
    ohlc = candle.get("ohlc")
    if not isinstance(ohlc, dict):
        return []
    try:
        low = float(ohlc.get("low"))
        high = float(ohlc.get("high"))
        close = float(ohlc.get("close"))
    except (TypeError, ValueError):
        return []
    if not levels:
        return []

    threshold = _volume_threshold(levels, cfg.absorption_volume_pct)
    out: list[dict[str, Any]] = []

    bid_candidates: list[dict[str, Any]] = []
    ask_candidates: list[dict[str, Any]] = []
    for level in levels:
        total = int(level.get("total_vol") or 0)
        if total < threshold or total <= 0:
            continue
        bid = int(level.get("bid") or 0)
        ask = int(level.get("ask") or 0)
        price = float(level.get("price") or 0)
        rl = level.get("rl")
        if rl is None:
            continue
        if (
            close > low
            and _near_price(price, low, tick_size=cfg.tick_size, extreme_ticks=cfg.absorption_extreme_ticks)
            and ask >= bid * cfg.absorption_side_ratio
        ):
            bid_candidates.append({**level, "rl": rl})
        if (
            close < high
            and _near_price(price, high, tick_size=cfg.tick_size, extreme_ticks=cfg.absorption_extreme_ticks)
            and bid >= ask * cfg.absorption_side_ratio
        ):
            ask_candidates.append({**level, "rl": rl})

    if bid_candidates:
        best = max(bid_candidates, key=lambda item: int(item.get("total_vol") or 0))
        out.append(_absorption_entry(best, side="BID"))
    if ask_candidates:
        best = max(ask_candidates, key=lambda item: int(item.get("total_vol") or 0))
        out.append(_absorption_entry(best, side="ASK"))
    return out


def _absorption_entry(level: dict[str, Any], *, side: str) -> dict[str, Any]:
    return {
        "side": side,
        "price": level["price"],
        "total_vol": int(level.get("total_vol") or 0),
        "bid": int(level.get("bid") or 0),
        "ask": int(level.get("ask") or 0),
        "rl": level.get("rl"),
    }


def level_imbalance_tag(
    rl: float | None,
    side: str | None,
    *,
    rl_min: float,
) -> str:
    """Return ``bid``, ``ask``, or ``""`` for footprint level export."""
    if rl is None or side is None or float(rl) < rl_min:
        return ""
    if side == "BID":
        return "bid"
    if side == "ASK":
        return "ask"
    return ""


def enrich_footprint_levels_in_candle(
    candle: dict[str, Any],
    *,
    cfg: DerivedConfig,
) -> list[Any]:
    """Attach ``rl`` and optional ``imbalance`` (``bid``/``ask``/``""``) to each ``footprint[]`` level."""
    footprint = candle.get("footprint")
    if not isinstance(footprint, list):
        return []
    enriched: list[Any] = []
    for level in footprint:
        if not isinstance(level, dict):
            enriched.append(level)
            continue
        _, bid, ask = _level_volumes(level)
        rl, side = compute_level_rl(bid, ask)
        block = dict(level)
        block["rl"] = rl
        if cfg.imbalance_enabled:
            block["imbalance"] = level_imbalance_tag(rl, side, rl_min=cfg.rl_min)
        enriched.append(block)
    return enriched


def compute_candle_orderflow(
    candle: dict[str, Any],
    *,
    cfg: DerivedConfig,
) -> dict[str, Any]:
    levels = _parse_footprint_levels(candle)
    out: dict[str, Any] = {}
    if cfg.stacked_enabled:
        out["stacked_in_candle"] = compute_stacked_in_candle(
            levels,
            rl_min=cfg.rl_min,
            stacked_min_levels=cfg.stacked_min_levels,
        )
    if cfg.absorption_enabled:
        out["absorption"] = compute_absorption_for_candle(candle, levels, cfg=cfg)
    return out


def enrich_footprint_combined_document(
    doc: dict[str, Any],
    *,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach ``orderflow`` per candle for OpenAI upload (does not mutate on-disk files)."""
    if not isinstance(doc, dict):
        return doc
    cfg_raw = cfg if isinstance(cfg, dict) else {}
    derived_cfg = derived_config_from_cfg(cfg_raw, doc)
    candles_raw = doc.get("candles")
    if not isinstance(candles_raw, list):
        return doc

    out = dict(doc)
    enriched: list[Any] = []
    for candle in candles_raw:
        if not isinstance(candle, dict):
            enriched.append(candle)
            continue
        block = dict(candle)
        block["footprint"] = enrich_footprint_levels_in_candle(block, cfg=derived_cfg)
        orderflow = compute_candle_orderflow(block, cfg=derived_cfg)
        if orderflow:
            block["orderflow"] = orderflow
        enriched.append(block)
    out["candles"] = enriched
    return out
