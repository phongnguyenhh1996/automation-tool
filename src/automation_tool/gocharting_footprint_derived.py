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
DEFAULT_VALUE_AREA_FRACTION = 0.70


@dataclass(frozen=True)
class DerivedConfig:
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


def compute_candle_orderflow(
    candle: dict[str, Any],
    *,
    cfg: DerivedConfig,
) -> dict[str, Any]:
    levels = _parse_footprint_levels(candle)
    imbalance = compute_imbalance_levels(levels, rl_min=cfg.rl_min)
    stacked = compute_stacked_in_candle(
        levels,
        rl_min=cfg.rl_min,
        stacked_min_levels=cfg.stacked_min_levels,
    )
    absorption = compute_absorption_for_candle(candle, levels, cfg=cfg)
    return {
        "imbalance_levels": imbalance,
        "stacked_in_candle": stacked,
        "absorption": absorption,
    }


def value_area_fraction_from_cfg(cfg: dict[str, Any]) -> float:
    ws = cfg.get("footprint_ws")
    if not isinstance(ws, dict):
        return DEFAULT_VALUE_AREA_FRACTION
    return _float_param(ws, "value_area_fraction", DEFAULT_VALUE_AREA_FRACTION)


def _normalized_footprint_level(level: dict[str, Any]) -> Optional[dict[str, Any]]:
    if not isinstance(level, dict):
        return None
    price = level.get("price")
    if price is None:
        return None
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    buy = level.get("buy") if isinstance(level.get("buy"), dict) else {}
    sell = level.get("sell") if isinstance(level.get("sell"), dict) else {}
    bv = float(buy.get("volume") or 0)
    sv = float(sell.get("volume") or 0)
    vol = bv + sv
    return {
        "price": p,
        "volume": int(vol) if vol == int(vol) else vol,
        "buy_volume": int(bv) if bv == int(bv) else bv,
        "sell_volume": int(sv) if sv == int(sv) else sv,
    }


def combined_candles_for_session_profile(
    candles: list[Any],
) -> list[dict[str, Any]]:
    """Map footprint_combined candles to the shape expected by ``_session_profile_for_tf``."""
    out: list[dict[str, Any]] = []
    for candle in candles:
        if not isinstance(candle, dict):
            continue
        fp: list[dict[str, Any]] = []
        for level in candle.get("footprint") or []:
            row = _normalized_footprint_level(level)
            if row is not None:
                fp.append(row)
        if fp:
            out.append({"footprint": fp})
    return out


def session_profile_from_combined_document(
    doc: dict[str, Any],
    *,
    interval: Optional[str] = None,
    value_area_fraction: float = DEFAULT_VALUE_AREA_FRACTION,
) -> dict[str, Any]:
    """Session POC/VAH/VAL from aggregated footprint (max-volume POC + 70% value area)."""
    from automation_tool.gocharting_ws_decode import document_timeframe
    from automation_tool.market_merge_single import _session_profile_for_tf

    iv = (interval or document_timeframe(doc) or "?").strip().lower()
    candles_raw = doc.get("candles")
    if not isinstance(candles_raw, list):
        candles_raw = []
    profile_candles = combined_candles_for_session_profile(candles_raw)
    sp = _session_profile_for_tf(iv, profile_candles, target_frac=value_area_fraction)
    return {
        "poc": sp.get("poc"),
        "vah": sp.get("vah"),
        "val": sp.get("val"),
        "total_volume": sp.get("total_volume"),
        "price_levels": sp.get("price_levels"),
        "interval": iv,
        "value_area_fraction": value_area_fraction,
        "candles_used": len(profile_candles),
    }


def attach_session_profile_to_combined_document(
    doc: dict[str, Any],
    *,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach top-level ``session_profile`` for on-disk ``footprint_combined_*.json``."""
    if not isinstance(doc, dict):
        return doc
    cfg_raw = cfg if isinstance(cfg, dict) else {}
    out = dict(doc)
    out["session_profile"] = session_profile_from_combined_document(
        out,
        value_area_fraction=value_area_fraction_from_cfg(cfg_raw),
    )
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
        block["orderflow"] = compute_candle_orderflow(block, cfg=derived_cfg)
        enriched.append(block)
    out["candles"] = enriched
    return out
