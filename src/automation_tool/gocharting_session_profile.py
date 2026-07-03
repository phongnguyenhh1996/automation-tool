"""ETH session profile: POC / VAH / VAL from aggregated WS footprint (GoCharting Value Area)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from automation_tool.gocharting_ws_decode import (
    _footprint_side_volume,
    candle_sort_datetime,
    _is_eth_session_open_candle,
    scaled_price,
)

DEFAULT_VALUE_AREA_PCT = 0.7
VALUE_AREA_EXPANSION_SYMMETRIC = "symmetric"
VALUE_AREA_EXPANSION_GOCHARTING_ASYMMETRIC = "gocharting_asymmetric"
VALUE_AREA_EXPANSION_MODES = (
    VALUE_AREA_EXPANSION_SYMMETRIC,
    VALUE_AREA_EXPANSION_GOCHARTING_ASYMMETRIC,
)


def session_profile_enabled(cfg: dict[str, Any]) -> bool:
    ws = cfg.get("footprint_ws")
    if not isinstance(ws, dict):
        return True
    sp = ws.get("session_profile")
    if not isinstance(sp, dict):
        return True
    return bool(sp.get("enabled", True))


def session_profile_value_area_pct(cfg: dict[str, Any]) -> float:
    ws = cfg.get("footprint_ws") if isinstance(cfg.get("footprint_ws"), dict) else {}
    sp = ws.get("session_profile") if isinstance(ws.get("session_profile"), dict) else {}
    raw = sp.get("value_area_pct", DEFAULT_VALUE_AREA_PCT)
    try:
        pct = float(raw)
        if 0 < pct <= 1:
            return pct
    except (TypeError, ValueError):
        pass
    return DEFAULT_VALUE_AREA_PCT


def session_profile_expansion_mode(cfg: dict[str, Any]) -> str:
    ws = cfg.get("footprint_ws") if isinstance(cfg.get("footprint_ws"), dict) else {}
    sp = ws.get("session_profile") if isinstance(ws.get("session_profile"), dict) else {}
    mode = str(sp.get("expansion_mode") or VALUE_AREA_EXPANSION_SYMMETRIC).strip().lower()
    if mode in VALUE_AREA_EXPANSION_MODES:
        return mode
    return VALUE_AREA_EXPANSION_SYMMETRIC


def session_profile_use_raw_ticks(cfg: dict[str, Any]) -> bool:
    ws = cfg.get("footprint_ws") if isinstance(cfg.get("footprint_ws"), dict) else {}
    sp = ws.get("session_profile") if isinstance(ws.get("session_profile"), dict) else {}
    if sp.get("use_raw_ticks") is False:
        return False
    return True


def session_profile_value_area_enabled(cfg: dict[str, Any]) -> bool:
    ws = cfg.get("footprint_ws") if isinstance(cfg.get("footprint_ws"), dict) else {}
    sp = ws.get("session_profile") if isinstance(ws.get("session_profile"), dict) else {}
    return bool(sp.get("value_area_enabled", True))


def split_candles_into_eth_sessions(candles: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split sorted candles into COMEX ETH sessions (open 05:00 GMT+7)."""
    ordered = sorted(
        [c for c in candles if isinstance(c, dict)],
        key=candle_sort_datetime,
    )
    if not ordered:
        return []

    sessions: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    prev_dt: datetime | None = None
    for candle in ordered:
        dt = candle_sort_datetime(candle)
        if (
            current
            and prev_dt is not None
            and _is_eth_session_open_candle(dt)
            and not _is_eth_session_open_candle(prev_dt)
        ):
            sessions.append(current)
            current = []
        current.append(candle)
        prev_dt = dt
    if current:
        sessions.append(current)
    return sessions


def _candle_ohlc_field(
    candle: dict[str, Any],
    key: str,
    *,
    price_precision: int,
) -> Optional[float]:
    """Resolve high/low/close from ``bar_flow``, ``ohlc``, or ``ending_summary``."""
    bar_flow = candle.get("bar_flow") if isinstance(candle.get("bar_flow"), dict) else {}
    if bar_flow.get(key) is not None:
        try:
            return float(bar_flow[key])
        except (TypeError, ValueError):
            pass

    ohlc = candle.get("ohlc") if isinstance(candle.get("ohlc"), dict) else {}
    if ohlc.get(key) is not None:
        try:
            return float(ohlc[key])
        except (TypeError, ValueError):
            pass

    es = candle.get("ending_summary") if isinstance(candle.get("ending_summary"), dict) else {}
    es_key = {"high": "high", "low": "low", "close": "close"}.get(key)
    if es_key and es.get(es_key) is not None:
        try:
            return scaled_price(int(es[es_key]), price_precision)
        except (TypeError, ValueError):
            pass
    return None


def _candle_volume(candle: dict[str, Any]) -> int:
    bar_flow = candle.get("bar_flow") if isinstance(candle.get("bar_flow"), dict) else {}
    vol = bar_flow.get("volume")
    if vol is not None:
        try:
            v = int(vol)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    buy = bar_flow.get("buy_volume")
    sell = bar_flow.get("sell_volume")
    if buy is not None or sell is not None:
        try:
            v = int(buy or 0) + int(sell or 0)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass

    totals = candle.get("totals") if isinstance(candle.get("totals"), dict) else {}
    overall = totals.get("overall") if isinstance(totals.get("overall"), dict) else {}
    if overall.get("volume") is not None:
        try:
            v = int(overall["volume"])
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass

    es = candle.get("ending_summary") if isinstance(candle.get("ending_summary"), dict) else {}
    try:
        buy = int(es.get("total_buy") or 0)
        sell = int(es.get("total_sell") or 0)
        v = buy + sell
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass

    footprint = candle.get("footprint")
    if isinstance(footprint, list):
        vol = 0
        for level in footprint:
            if not isinstance(level, dict):
                continue
            vol += _footprint_side_volume(level.get("buy")) + _footprint_side_volume(level.get("sell"))
        if vol > 0:
            return vol
    return 0


def _candle_average_price(candle: dict[str, Any], *, price_precision: int) -> Optional[float]:
    """Typical price AP = (H + L + C) / 3 per GoCharting session VWAP docs."""
    high = _candle_ohlc_field(candle, "high", price_precision=price_precision)
    low = _candle_ohlc_field(candle, "low", price_precision=price_precision)
    close = _candle_ohlc_field(candle, "close", price_precision=price_precision)
    if high is None or low is None or close is None:
        return None
    return (high + low + close) / 3.0


def bar_flow_typical_price_volume(bar_flow: dict[str, Any]) -> tuple[Optional[float], int]:
    """Typical price AP = (H+L+C)/3 and bar volume from a ``bar_flow`` block."""
    high = bar_flow.get("high")
    low = bar_flow.get("low")
    close = bar_flow.get("close")
    if close is None:
        close = bar_flow.get("vwap")
    if high is None or low is None or close is None:
        return None, 0
    try:
        vol = int(bar_flow.get("volume") or 0)
    except (TypeError, ValueError):
        return None, 0
    if vol <= 0:
        return None, 0
    ap = (float(high) + float(low) + float(close)) / 3.0
    return ap, vol


def accumulate_session_vwap_from_candle(
    sum_apv: float,
    sum_vol: int,
    candle: dict[str, Any],
    *,
    price_precision: int,
) -> tuple[float, int, Optional[float]]:
    """Update running session VWAP with one candle; returns ``(sum_apv, sum_vol, session_vwap)``."""
    vol = _candle_volume(candle)
    ap = _candle_average_price(candle, price_precision=price_precision)
    if ap is not None and vol > 0:
        sum_apv += ap * vol
        sum_vol += vol
    if sum_vol <= 0:
        return sum_apv, sum_vol, None
    precision = max(0, int(price_precision))
    return sum_apv, sum_vol, round(sum_apv / sum_vol, precision)


def accumulate_session_vwap_state(
    sum_apv: float,
    sum_vol: int,
    bar_flow: dict[str, Any],
    *,
    price_precision: int,
) -> tuple[float, int, Optional[float]]:
    """Update running session VWAP with one bar; returns ``(sum_apv, sum_vol, session_vwap)``."""
    ap, vol = bar_flow_typical_price_volume(bar_flow)
    if ap is not None and vol > 0:
        sum_apv += ap * vol
        sum_vol += vol
    if sum_vol <= 0:
        return sum_apv, sum_vol, None
    precision = max(0, int(price_precision))
    return sum_apv, sum_vol, round(sum_apv / sum_vol, precision)


def apply_running_session_vwap_to_candles(
    candles: list[dict[str, Any]],
    *,
    price_precision: int,
) -> None:
    """Set each ``bar_flow.vwap`` to cumulative session VWAP at that bar (mutates in place)."""
    from automation_tool.gocharting_ws_decode import (
        _is_eth_session_open_candle,
        candle_sort_datetime,
    )

    sorted_candles = sorted(
        [c for c in candles if isinstance(c, dict)],
        key=candle_sort_datetime,
    )
    sum_apv = 0.0
    sum_vol = 0
    prev_dt: datetime | None = None
    for candle in sorted_candles:
        bar_flow = candle.get("bar_flow")
        if not isinstance(bar_flow, dict):
            continue
        dt = candle_sort_datetime(candle)
        if (
            prev_dt is not None
            and _is_eth_session_open_candle(dt)
            and not _is_eth_session_open_candle(prev_dt)
        ):
            sum_apv = 0.0
            sum_vol = 0
        sum_apv, sum_vol, session_vwap = accumulate_session_vwap_from_candle(
            sum_apv,
            sum_vol,
            candle,
            price_precision=price_precision,
        )
        if session_vwap is not None:
            bar_flow["vwap"] = session_vwap
        prev_dt = dt


def compute_session_vwap(
    candles: list[dict[str, Any]],
    *,
    price_precision: int = 1,
) -> dict[str, Any]:
    """
    Session VWAP = Σ(AP × V) / Σ(V) where AP = (H+L+C)/3 for each candle in the session.

    Matches GoCharting VWAP indicator: each bar contributes its typical price weighted by
    traded volume for the session period.
    """
    sum_apv = 0.0
    sum_vol = 0
    used = 0
    for candle in candles:
        if not isinstance(candle, dict):
            continue
        vol = _candle_volume(candle)
        if vol <= 0:
            continue
        ap = _candle_average_price(candle, price_precision=price_precision)
        if ap is None:
            continue
        sum_apv += ap * vol
        sum_vol += vol
        used += 1

    if sum_vol <= 0:
        return {}

    precision = max(0, int(price_precision))
    vwap = round(sum_apv / sum_vol, precision)
    return {
        "vwap": vwap,
        "session_volume": sum_vol,
        "vwap_candles": used,
    }


def _footprint_level_price(level: dict[str, Any]) -> Optional[float]:
    try:
        price = float(level.get("price") or 0)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def aggregate_session_footprint_rows(
    candles: list[dict[str, Any]],
    *,
    block_size: float = 0.0,
    price_precision: int = 1,
    tick_size: float = 0.1,
    block_multiplier: int = 1,
) -> list[tuple[float, int]]:
    """Sum buy+sell volume per price across session candles (high → low)."""
    from automation_tool.gocharting_ws_decode import _snap_footprint_price_to_block

    by_price: dict[float, int] = {}
    for candle in candles:
        footprint = candle.get("footprint")
        if not isinstance(footprint, list):
            continue
        for level in footprint:
            if not isinstance(level, dict):
                continue
            price = _footprint_level_price(level)
            if price is None:
                continue
            if block_size > 0:
                price = _snap_footprint_price_to_block(
                    price,
                    block_size,
                    price_precision=price_precision,
                    tick_size=tick_size,
                    block_multiplier=block_multiplier,
                )
            vol = _footprint_side_volume(level.get("buy")) + _footprint_side_volume(level.get("sell"))
            if vol <= 0:
                continue
            by_price[price] = by_price.get(price, 0) + vol

    return sorted(by_price.items(), key=lambda row: row[0], reverse=True)


def _sum_volumes_above(index: int, count: int, volumes: list[int]) -> int:
    total = 0
    for offset in range(1, count + 1):
        idx = index - offset
        if idx < 0:
            break
        total += volumes[idx]
    return total


def _sum_volumes_below(index: int, count: int, volumes: list[int]) -> int:
    total = 0
    for offset in range(1, count + 1):
        idx = index + offset
        if idx >= len(volumes):
            break
        total += volumes[idx]
    return total


def compute_value_area_profile(
    rows: list[tuple[float, int]],
    *,
    value_area_pct: float = DEFAULT_VALUE_AREA_PCT,
    expansion_mode: str = VALUE_AREA_EXPANSION_SYMMETRIC,
) -> dict[str, Any]:
    """
    POC = max-volume row. Value Area expands until ≥ ``value_area_pct`` of session volume.

    ``symmetric``: compare 1 row above vs 1 row below (matches GoCharting chart VAH/VAL).
    ``gocharting_asymmetric``: compare 1 row above vs 2 rows below (per written docs).
    """
    if not rows:
        return {}
    prices = [p for p, _ in rows]
    volumes = [v for _, v in rows]
    total_volume = sum(volumes)
    if total_volume <= 0:
        return {}

    target = total_volume * value_area_pct
    poc_idx = max(range(len(rows)), key=lambda i: volumes[i])
    va_high = va_low = poc_idx
    va_volume = volumes[poc_idx]

    while va_volume < target:
        if expansion_mode == VALUE_AREA_EXPANSION_GOCHARTING_ASYMMETRIC:
            vol_above = _sum_volumes_above(va_high, 1, volumes)
            vol_below = _sum_volumes_below(va_low, 2, volumes)
            if vol_above <= 0 and vol_below <= 0:
                break
            if vol_below <= 0 or (vol_above > 0 and vol_above >= vol_below):
                next_high = va_high - 1
                if next_high < 0:
                    break
                va_high = next_high
                va_volume += volumes[va_high]
            else:
                added = False
                start_low = va_low
                for offset in (1, 2):
                    next_low = start_low + offset
                    if next_low >= len(volumes):
                        break
                    if volumes[next_low] <= 0:
                        continue
                    va_low = next_low
                    va_volume += volumes[va_low]
                    added = True
                if not added:
                    break
        else:
            vol_above = volumes[va_high - 1] if va_high > 0 else -1
            vol_below = volumes[va_low + 1] if va_low < len(volumes) - 1 else -1
            if vol_above <= 0 and vol_below <= 0:
                break
            if vol_below < 0 or (vol_above >= vol_below and vol_above >= 0):
                va_high -= 1
                va_volume += volumes[va_high]
            else:
                va_low += 1
                va_volume += volumes[va_low]

    return {
        "poc": prices[poc_idx],
        "vah": prices[va_high],
        "val": prices[va_low],
        "value_area_pct": value_area_pct,
        "total_volume": total_volume,
        "value_area_volume": va_volume,
        "poc_volume": volumes[poc_idx],
        "expansion_mode": expansion_mode,
    }


def build_session_profile(
    candles: list[dict[str, Any]],
    *,
    value_area_enabled: bool = True,
    value_area_pct: float = DEFAULT_VALUE_AREA_PCT,
    expansion_mode: str = VALUE_AREA_EXPANSION_SYMMETRIC,
    block_size: float = 0.0,
    price_precision: int = 1,
    tick_size: float = 0.1,
    block_multiplier: int = 1,
) -> dict[str, Any]:
    if not candles:
        return {}
    profile: dict[str, Any] = {}
    if value_area_enabled:
        rows = aggregate_session_footprint_rows(
            candles,
            block_size=block_size,
            price_precision=price_precision,
            tick_size=tick_size,
            block_multiplier=block_multiplier,
        )
        profile = compute_value_area_profile(
            rows,
            value_area_pct=value_area_pct,
            expansion_mode=expansion_mode,
        )
        if not profile:
            profile = {}

    vwap_block = compute_session_vwap(candles, price_precision=price_precision)
    if not vwap_block and not profile:
        return {}
    profile.update(vwap_block)

    first = candles[0]
    last = candles[-1]
    session_start = str(first.get("time_gmt7") or first.get("date") or "")
    session_end = str(last.get("time_gmt7") or last.get("date") or "")
    open_dt = candle_sort_datetime(first)
    profile.update(
        {
            "session_start": session_start,
            "session_end": session_end,
            "session_key": open_dt.strftime("%Y-%m-%d"),
            "candles": len(candles),
        }
    )
    return profile


def enrich_footprint_document_with_session_profiles(
    doc: dict[str, Any],
    *,
    cfg: dict[str, Any] | None = None,
    price_precision: int | None = None,
) -> dict[str, Any]:
    """Attach ``session_profiles`` on doc and ``session_profile`` on each candle."""
    if not isinstance(doc, dict):
        return doc
    cfg_raw = cfg if isinstance(cfg, dict) else {}
    if not session_profile_enabled(cfg_raw):
        return doc

    from automation_tool.gocharting_ws_decode import (
        _footprint_ws_tick_size,
        _price_precision_from_doc,
        footprint_ws_block_multiplier,
        footprint_ws_block_size,
    )

    candles_raw = doc.get("candles")
    if not isinstance(candles_raw, list) or not candles_raw:
        return doc

    block_size = footprint_ws_block_size(cfg_raw, doc)
    pp = price_precision if price_precision is not None else _price_precision_from_doc(doc)
    tick_size = _footprint_ws_tick_size(cfg_raw, doc)
    block_multiplier = footprint_ws_block_multiplier(cfg_raw)
    va_pct = session_profile_value_area_pct(cfg_raw)
    expansion_mode = session_profile_expansion_mode(cfg_raw)
    value_area_enabled = session_profile_value_area_enabled(cfg_raw)
    profile_block_size = 0.0 if session_profile_use_raw_ticks(cfg_raw) else block_size
    profile_block_multiplier = 1 if session_profile_use_raw_ticks(cfg_raw) else block_multiplier

    sessions = split_candles_into_eth_sessions(
        [c for c in candles_raw if isinstance(c, dict)]
    )
    profiles: list[dict[str, Any]] = []
    candle_profile: dict[str, dict[str, Any]] = {}
    for session_candles in sessions:
        profile = build_session_profile(
            session_candles,
            value_area_enabled=value_area_enabled,
            value_area_pct=va_pct,
            expansion_mode=expansion_mode,
            block_size=profile_block_size,
            price_precision=pp,
            tick_size=tick_size,
            block_multiplier=profile_block_multiplier,
        )
        if not profile:
            continue
        profiles.append(profile)
        snippet: dict[str, Any] = {}
        if value_area_enabled and profile.get("poc") is not None:
            snippet.update(
                {
                    "poc": profile["poc"],
                    "vah": profile["vah"],
                    "val": profile["val"],
                    "value_area_pct": profile["value_area_pct"],
                }
            )
        if profile.get("vwap") is not None:
            snippet["vwap"] = profile["vwap"]
        if not snippet:
            continue
        for candle in session_candles:
            time_key = str(candle.get("time_gmt7") or "").strip()
            if time_key:
                candle_profile[time_key] = snippet

    if not profiles:
        return doc

    candles_out: list[Any] = []
    for candle in candles_raw:
        if not isinstance(candle, dict):
            candles_out.append(candle)
            continue
        block = dict(candle)
        time_key = str(block.get("time_gmt7") or "").strip()
        if time_key and time_key in candle_profile:
            block["session_profile"] = dict(candle_profile[time_key])
        candles_out.append(block)

    out = dict(doc)
    out["candles"] = candles_out
    out["session_profiles"] = profiles
    return out


def shift_session_profile_prices(
    profile: dict[str, Any],
    basis: float,
    *,
    spot_tick: float,
) -> dict[str, Any]:
    from automation_tool.gocharting_gc_spot_convert import _shift_price

    out = dict(profile)
    for key in ("poc", "vah", "val", "vwap"):
        val = out.get(key)
        if val is not None:
            out[key] = _shift_price(float(val), basis, spot_tick=spot_tick)
    return out
