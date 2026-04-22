"""
Merge multiple Coinmap per-timeframe JSON exports into one analysis-ready structure.

Compatible with current exports (candle + orderflow; optional vwap, cvd) and future
fields. See project plan: ``coinmap_merged`` OpenAI payload (``source: coinmap_merged``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

# --- Constants -----------------------------------------------------------------

DEFAULT_SESSION_TZ = "Asia/Ho_Chi_Minh"
DEFAULT_SESSION_START_HOUR = 5
SOURCE_MERGED = "coinmap_merged"
EPS_VWAP_AT = 1e-4
DEFAULT_RECENT_15M = 24
DEFAULT_RECENT_5M = 60
DEFAULT_RECENT_FOOTPRINT = 12
DEFAULT_HISTOGRAM_MAX = 40  # 40+20+20 around POC/VAL/VAH when compacting; plan says 120 cap before trim


@dataclass
class AnalysisPayloadOptions:
    """Options for :func:`build_analysis_payload`."""

    recent_15m: int = DEFAULT_RECENT_15M
    recent_5m: int = DEFAULT_RECENT_5M
    recent_footprint_per_tf: int = DEFAULT_RECENT_FOOTPRINT
    histogram_max: int = 120
    histogram_around_poc: int = 40
    histogram_near_val: int = 20
    histogram_near_vah: int = 20
    vah_val_volume_fraction: float = 0.70
    per_interval_recent: Optional[dict[str, int]] = None


_INTERVAL_MINUTES: dict[str, int] = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "45m": 45,
    "1h": 60,
    "2h": 120,
    "3h": 180,
    "4h": 240,
    "1d": 1440,
}


def _interval_minutes(iv: str) -> int:
    """Minutes per bar for ordering timeframes (aligns with Coinmap interval strings)."""
    s = (iv or "").strip().lower()
    if s in _INTERVAL_MINUTES:
        return _INTERVAL_MINUTES[s]
    m = re.match(r"^(\d+)m$", s)
    if m:
        return int(m.group(1))
    m = re.match(r"^(\d+)h$", s)
    if m:
        return int(m.group(1)) * 60
    m = re.match(r"^(\d+)d$", s)
    if m:
        return int(m.group(1)) * 1440
    return 10**9


def _smallest_timeframe(timeframes: Sequence[str]) -> str:
    """Smallest bar size (e.g. ``5m`` when both ``5m`` and ``15m`` present)."""
    tfs = [str(x).strip() for x in timeframes if str(x).strip()]
    if not tfs:
        return ""
    return min(tfs, key=lambda x: (_interval_minutes(x), x))


def _recent_n_for_interval(iv: str, opt: AnalysisPayloadOptions) -> int:
    pr = opt.per_interval_recent
    if pr and iv in pr:
        return max(0, int(pr[iv]))
    if iv == "5m":
        return opt.recent_5m
    if iv == "15m":
        return opt.recent_15m
    return opt.recent_15m


# --- JSON load / raw_bundle ----------------------------------------------------


def _as_list(x: Any) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else []


def _num(x: Any) -> Any:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return x
    if isinstance(x, str) and x.strip():
        try:
            return float(x) if "." in x else int(x)
        except ValueError:
            return None
    return None


def _parse_ts(s: str) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    s2 = s.strip()
    try:
        if s2.endswith("Z"):
            s2 = s2[:-1] + "+00:00"
        return datetime.fromisoformat(s2)
    except ValueError:
        return None


def load_coinmap_json_file(path: str | Path) -> dict[str, Any]:
    """Load a single Coinmap API export JSON file."""
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Expected object at root: {p}")
    return raw


def extract_vwap_bands_from_row(item: dict[str, Any]) -> dict[str, Any]:
    """
    VWAP rows may be flat or nested under ``data`` (see ``docs/coinmap-chart-json.md``).
    Returns dict with vwap, sd, topBand1..3, botBand1..3 (None if missing).
    """
    src: dict[str, Any] = dict(item)
    nested = item.get("data")
    if isinstance(nested, dict):
        for k, v in nested.items():
            if k not in src or src.get(k) in (None, 0) and v not in (None, 0, ""):
                src[k] = v
    out: dict[str, Any] = {}
    for k in (
        "vwap",
        "sd",
        "topBand1",
        "botBand1",
        "topBand2",
        "botBand2",
        "topBand3",
        "botBand3",
    ):
        out[k] = _num(src.get(k))
    return out


def _vn_session_anchor_local_datetime(
    ref_utc: datetime,
    *,
    tz_name: str,
    start_hour: int,
) -> datetime:
    """
    The most recent ``start_hour``:00 in ``tz_name`` that is not after ``ref_utc``
    (same instant), timezone-aware in ``tz_name``.
    """
    try:
        tz = ZoneInfo((tz_name or "").strip() or DEFAULT_SESSION_TZ)
    except Exception:
        tz = ZoneInfo(DEFAULT_SESSION_TZ)
    local = ref_utc.astimezone(tz)
    h = max(0, min(23, int(start_hour)))
    anchor = local.replace(hour=h, minute=0, second=0, microsecond=0)
    if local < anchor:
        anchor = anchor - timedelta(days=1)
    return anchor


def _infer_session_start_iso(
    *,
    session_start: Optional[str],
    analysis_time: Optional[str],
    gen_times: list[datetime],
    session_timezone: str,
    start_hour: int = DEFAULT_SESSION_START_HOUR,
) -> Optional[str]:
    """Return explicit ``session_start`` or default 05:00 session open in ``session_timezone``."""
    if session_start is not None and str(session_start).strip():
        return str(session_start).strip()
    ref: Optional[datetime] = None
    if analysis_time:
        ref = _parse_ts(analysis_time)
    if ref is None and gen_times:
        ref = max(gen_times)
    if ref is None:
        ref = datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    anchor = _vn_session_anchor_local_datetime(
        ref, tz_name=session_timezone, start_hour=start_hour
    )
    return anchor.isoformat()


def build_raw_bundle(
    file_paths: list[str | Path],
    *,
    analysis_time: Optional[str] = None,
    session_start: Optional[str] = None,
    session_timezone: str = DEFAULT_SESSION_TZ,
    session_start_hour: int = DEFAULT_SESSION_START_HOUR,
) -> dict[str, Any]:
    """
    Load several per-interval Coinmap files into the internal ``raw_bundle`` structure.

    ``file_paths`` — one file per timeframe; each must have ``symbol``, ``interval``,
    and API keys (or empty lists).
    """
    files: list[dict[str, Any]] = []
    for fp in file_paths:
        files.append(load_coinmap_json_file(fp))

    if not files:
        raise ValueError("file_paths is empty")

    sym0 = (files[0].get("symbol") or "").strip()
    for f in files[1:]:
        s = (f.get("symbol") or "").strip()
        if s and sym0 and s != sym0:
            raise ValueError(f"Symbol mismatch: {sym0!r} vs {s!r}")
    if not sym0:
        sym0 = "UNKNOWN"

    timeframes: list[str] = []
    for f in files:
        iv = (f.get("interval") or "").strip()
        if not iv:
            continue
        if iv not in timeframes:
            timeframes.append(iv)

    gen_times: list[datetime] = []
    for f in files:
        ga = f.get("generated_at")
        if isinstance(ga, str):
            t = _parse_ts(ga)
            if t is not None:
                gen_times.append(t)
    if analysis_time is None and gen_times:
        analysis_time = max(gen_times).isoformat()

    session_start = _infer_session_start_iso(
        session_start=session_start,
        analysis_time=analysis_time,
        gen_times=gen_times,
        session_timezone=session_timezone,
        start_hour=session_start_hour,
    )

    meta_by_tf: dict[str, Any] = {}
    candle_history_by_tf: dict[str, list] = {}
    orderflow_history_by_tf: dict[str, list] = {}
    vwap_by_tf: dict[str, list] = {}
    cvd_by_tf: dict[str, list] = {}

    for f in files:
        iv = (f.get("interval") or "").strip()
        if not iv:
            continue
        meta_by_tf[iv] = {
            "generated_at": f.get("generated_at"),
            "stamp": f.get("stamp"),
            "watchlist_category": f.get("watchlist_category"),
            "interval": iv,
        }
        candle_history_by_tf[iv] = _as_list(f.get("getcandlehistory"))
        orderflow_history_by_tf[iv] = _as_list(f.get("getorderflowhistory"))
        vwap_by_tf[iv] = _as_list(f.get("getindicatorsvwap"))
        cvd_by_tf[iv] = _as_list(f.get("getcandlehistorycvd"))

    return {
        "symbol": sym0,
        "analysis_time": analysis_time,
        "session_start": session_start,
        "session_timezone": session_timezone,
        "timeframes": timeframes,
        "meta_by_tf": meta_by_tf,
        "candle_history_by_tf": candle_history_by_tf,
        "orderflow_history_by_tf": orderflow_history_by_tf,
        "vwap_by_tf": vwap_by_tf,
        "cvd_by_tf": cvd_by_tf,
    }


def _index_by_t(items: list[dict[str, Any]], key: str = "t") -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        t = it.get(key)
        if t is None:
            continue
        try:
            tk = int(t)
        except (TypeError, ValueError):
            continue
        out[tk] = it
    return out


def _empty_footprint_summary() -> dict[str, Any]:
    return {
        "price_levels": 0,
        "max_volume_price": None,
        "max_volume": 0,
        "total_footprint_volume": 0,
        "buy_imbalance_count": 0,
        "sell_imbalance_count": 0,
        "delta_from_footprint": 0,
        "poc_candle": None,
    }


def _normalize_footprint_aggs(aggs: list) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for a in aggs:
        if not isinstance(a, dict):
            continue
        price = _num(a.get("tp"))
        v = _num(a.get("v")) or 0.0
        bv = _num(a.get("bv")) or 0.0
        sv = _num(a.get("sv")) or 0.0
        try:
            p = float(price) if price is not None else None
        except (TypeError, ValueError):
            p = None
        if p is None:
            continue
        out.append(
            {
                "price": p,
                "volume": int(round(v)) if v == int(v) else v,
                "buy_volume": int(round(bv)) if bv == int(bv) else bv,
                "sell_volume": int(round(sv)) if sv == int(sv) else sv,
            }
        )
    return out


def compute_footprint_summary(aggs: list) -> dict[str, Any]:
    """
    Per plan §8. Empty aggs returns zeros/None; still a valid summary dict.
    """
    if not aggs:
        return _empty_footprint_summary()
    n_levels = 0
    best_p: Any = None
    best_v = 0.0
    tot_v = 0.0
    buy_imb = 0
    sell_imb = 0
    delta_sum = 0.0
    for a in aggs:
        if not isinstance(a, dict):
            continue
        p = _num(a.get("tp"))
        v = _num(a.get("v")) or 0.0
        bv = _num(a.get("bv")) or 0.0
        sv = _num(a.get("sv")) or 0.0
        if p is None:
            continue
        n_levels += 1
        tot_v += v
        delta_sum += bv - sv
        if bv > sv:
            buy_imb += 1
        elif sv > bv:
            sell_imb += 1
        if v >= best_v:
            best_v = v
            best_p = p
    poc = best_p
    if isinstance(best_p, float) and best_p == int(best_p):
        poc = int(best_p)
    return {
        "price_levels": n_levels,
        "max_volume_price": best_p,
        "max_volume": int(round(best_v)) if best_v == int(best_v) else best_v,
        "total_footprint_volume": int(round(tot_v)) if tot_v == int(tot_v) else tot_v,
        "buy_imbalance_count": buy_imb,
        "sell_imbalance_count": sell_imb,
        "delta_from_footprint": int(round(delta_sum)) if delta_sum == int(delta_sum) else delta_sum,
        "poc_candle": poc,
    }


def _candle_ohlc_from_getcandle(ch: dict[str, Any]) -> dict[str, Any]:
    return {
        "t": int(ch.get("t")) if ch.get("t") is not None else 0,
        "ct": int(ch["ct"]) if ch.get("ct") is not None else None,
        "interval": (ch.get("i") or ch.get("interval") or "") or None,
        "o": _num(ch.get("o")),
        "h": _num(ch.get("h")),
        "l": _num(ch.get("l")),
        "c": _num(ch.get("c")),
        "v": _num(ch.get("v")),
        "bv": _num(ch.get("bv")),
        "sv": _num(ch.get("sv")),
        "d": _num(ch.get("d")),
        "d_max": _num(ch.get("dMax") if "dMax" in ch else ch.get("d_max")),
        "d_min": _num(ch.get("dMin") if "dMin" in ch else ch.get("d_min")),
        "n": _num(ch.get("n")),
    }


def _build_normalized_candles_for_tf(
    interval: str,
    raw: dict[str, Any],
) -> list[dict[str, Any]]:
    ch_list = _as_list(raw.get("candle_history_by_tf", {}).get(interval))
    of_list = _as_list(raw.get("orderflow_history_by_tf", {}).get(interval))
    vw_list = _as_list(raw.get("vwap_by_tf", {}).get(interval))
    cvd_list = _as_list(raw.get("cvd_by_tf", {}).get(interval))

    of_by_t = _index_by_t([x for x in of_list if isinstance(x, dict)], "t")
    vw_by_t: dict[int, dict[str, Any]] = {}
    for it in vw_list:
        if not isinstance(it, dict):
            continue
        t = it.get("t")
        if t is None:
            continue
        try:
            tk = int(t)
        except (TypeError, ValueError):
            continue
        bands = extract_vwap_bands_from_row(it)
        bands_out = {k: bands.get(k) for k in (
            "topBand1", "botBand1", "topBand2", "botBand2", "topBand3", "botBand3"
        )}
        if not any(x is not None for x in bands_out.values()) and "vwap" not in str(it):
            # still keep structure
            pass
        vw_by_t[tk] = {
            "vwap": bands.get("vwap"),
            "sd": bands.get("sd"),
            "vwap_bands": {k: bands.get(k) for k in (
                "topBand1", "botBand1", "topBand2", "botBand2", "topBand3", "botBand3"
            )},
        }

    cvd_by_t: dict[int, float | None] = {}
    for it in cvd_list:
        if not isinstance(it, dict):
            continue
        t = it.get("t")
        if t is None:
            continue
        try:
            tk = int(t)
        except (TypeError, ValueError):
            continue
        cvd_by_t[tk] = _num(it.get("cvd"))

    candles: list[dict[str, Any]] = []
    for ch in ch_list:
        if not isinstance(ch, dict):
            continue
        t0 = ch.get("t")
        if t0 is None:
            continue
        try:
            tk = int(t0)
        except (TypeError, ValueError):
            continue
        ohlc = _candle_ohlc_from_getcandle(ch)
        ohlc["interval"] = interval
        cvd = cvd_by_t.get(tk)
        ohlc["cvd"] = cvd
        vbr = vw_by_t.get(tk, {})
        ohlc["vwap"] = vbr.get("vwap")
        ohlc["sd"] = vbr.get("sd")
        vb = vbr.get("vwap_bands")
        if vb and not any(x is not None for x in vb.values()):
            ohlc["vwap_bands"] = None
        else:
            ohlc["vwap_bands"] = vb
        ofbar = of_by_t.get(tk) or {}
        aggs = _as_list(ofbar.get("aggs") if isinstance(ofbar, dict) else None)
        ohlc["footprint"] = _normalize_footprint_aggs(aggs)
        ohlc["footprint_summary"] = compute_footprint_summary(aggs)
        candles.append(ohlc)

    candles.sort(key=lambda c: c.get("t", 0))
    return candles


def _sum_aggs_by_price(frames_candles: list[dict[str, Any]]) -> dict[float, dict[str, float]]:
    acc: dict[float, dict[str, float]] = {}
    for c in frames_candles:
        for x in c.get("footprint") or []:
            p = x.get("price")
            if p is None:
                continue
            try:
                key = float(p)
            except (TypeError, ValueError):
                continue
            b = acc.setdefault(
                key,
                {"vol": 0.0, "buy_volume": 0.0, "sell_volume": 0.0},
            )
            b["vol"] += float(x.get("volume") or 0)
            b["buy_volume"] += float(x.get("buy_volume") or 0)
            b["sell_volume"] += float(x.get("sell_volume") or 0)
    return acc


def _poc_vah_val_from_acc(
    acc: dict[float, dict[str, float]], *, target_frac: float = 0.70
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    POC = price with max total volume. VAH/VAL: expand from POC by preferring
    the side (left/right on sorted price list) with larger *single-bin* volume
    until cumulated volume reaches ``target_frac`` of session total. VAH/VAL = max/min
    of selected price set. Documented in code to match plan §9.
    """
    if not acc:
        return None, None, None
    tot = sum(b["vol"] for b in acc.values())
    if tot <= 0:
        return None, None, None
    target = target_frac * tot
    poc = max(acc.keys(), key=lambda k: acc[k]["vol"])
    prices_sorted = sorted(acc.keys())
    if poc not in acc:
        return None, None, None
    selected: set[float] = {poc}
    cum = acc[poc]["vol"]
    idx_map = {p: i for i, p in enumerate(prices_sorted)}

    def left_price(p: float) -> Optional[float]:
        i = idx_map[p]
        if i > 0:
            return prices_sorted[i - 1]
        return None

    def right_price(p: float) -> Optional[float]:
        i = idx_map[p]
        if i + 1 < len(prices_sorted):
            return prices_sorted[i + 1]
        return None

    def expand_candidates() -> tuple[Optional[float], Optional[float]]:
        smin, smax = min(selected), max(selected)
        return left_price(smin), right_price(smax)

    while cum < target - 1e-9:
        lo, hi = expand_candidates()
        if lo is None and hi is None:
            break
        if lo is None:
            selected.add(hi)
            cum += acc[hi]["vol"] if hi is not None else 0.0
            continue
        if hi is None:
            selected.add(lo)
            cum += acc[lo]["vol"] if lo is not None else 0.0
            continue
        vl = acc[lo]["vol"]
        vh = acc[hi]["vol"]
        if vl >= vh:
            selected.add(lo)
            cum += vl
        else:
            selected.add(hi)
            cum += vh
    vah, val = max(selected), min(selected)
    return poc, vah, val


def _session_profile_for_tf(
    interval: str,
    candles: list[dict[str, Any]],
    *,
    target_frac: float = 0.70,
) -> dict[str, Any]:
    acc = _sum_aggs_by_price(candles)
    hist: list[dict[str, Any]] = []
    for p in sorted(acc.keys()):
        b = acc[p]
        dv = b["buy_volume"] - b["sell_volume"]
        hist.append(
            {
                "price": p,
                "volume": int(b["vol"]) if b["vol"] == int(b["vol"]) else b["vol"],
                "buy_volume": int(b["buy_volume"])
                if b["buy_volume"] == int(b["buy_volume"])
                else b["buy_volume"],
                "sell_volume": int(b["sell_volume"])
                if b["sell_volume"] == int(b["sell_volume"])
                else b["sell_volume"],
                "delta": int(dv) if dv == int(dv) else dv,
            }
        )
    total_volume = int(sum(b["vol"] for b in acc.values())) if acc else 0
    price_levels = len(hist)
    poc, vah, val = _poc_vah_val_from_acc(acc, target_frac=target_frac)
    return {
        "poc": poc,
        "vah": vah,
        "val": val,
        "total_volume": total_volume,
        "price_levels": price_levels,
        "interval": interval,
        "histogram": hist,
    }


def _summary_for_tf(
    _interval: str,
    candles: list[dict[str, Any]],
    session_profile: dict[str, Any],
) -> dict[str, Any]:
    if not candles:
        return {
            "latest_price": None,
            "latest_vwap": None,
            "latest_cvd": None,
            "latest_delta": None,
            "latest_volume": None,
            "session_delta_sum": None,
            "session_buy_volume": None,
            "session_sell_volume": None,
            "session_total_volume": None,
            "above_or_below_vwap": None,
            "vwap_distance": None,
            "current_vs_poc": None,
            "distance_to_poc": None,
            "distance_to_vah": None,
            "distance_to_val": None,
        }
    last = candles[-1]
    lp, lvw, lcvd, ld, lv = last.get("c"), last.get("vwap"), last.get("cvd"), last.get("d"), last.get("v")
    sds = 0.0
    s_bv = 0.0
    s_sv = 0.0
    s_v = 0.0
    for c in candles:
        sds += float(c.get("d") or 0)
        s_bv += float(c.get("bv") or 0)
        s_sv += float(c.get("sv") or 0)
        s_v += float(c.get("v") or 0)

    def _vwap_state(price: Any, w: Any) -> Optional[str]:
        if price is None or w is None:
            return None
        try:
            p, wv = float(price), float(w)
        except (TypeError, ValueError):
            return None
        if abs(p - wv) <= EPS_VWAP_AT:
            return "at"
        return "above" if p > wv else "below"

    vdist: Optional[float]
    if lp is not None and lvw is not None:
        try:
            vdist = float(lp) - float(lvw)
        except (TypeError, ValueError):
            vdist = None
    else:
        vdist = None
    poc0 = session_profile.get("poc")
    vah0 = session_profile.get("vah")
    val0 = session_profile.get("val")

    def _d3(a: Any, b: Any) -> Optional[float]:
        if a is None or b is None:
            return None
        return float(a) - float(b)

    cp = float(lp) if lp is not None else None
    c_vs_p: Optional[str] = None
    if cp is not None and poc0 is not None:
        c_vs_p = (
            "at"
            if abs(cp - float(poc0)) <= EPS_VWAP_AT
            else ("above" if cp > float(poc0) else "below")
        )
    return {
        "latest_price": lp,
        "latest_vwap": lvw,
        "latest_cvd": lcvd,
        "latest_delta": ld,
        "latest_volume": lv,
        "session_delta_sum": int(sds) if sds == int(sds) else sds,
        "session_buy_volume": int(s_bv) if s_bv == int(s_bv) else s_bv,
        "session_sell_volume": int(s_sv) if s_sv == int(s_sv) else s_sv,
        "session_total_volume": int(s_v) if s_v == int(s_v) else s_v,
        "above_or_below_vwap": _vwap_state(lp, lvw),
        "vwap_distance": vdist,
        "current_vs_poc": c_vs_p,
        "distance_to_poc": _d3(lp, poc0),
        "distance_to_vah": _d3(lp, vah0),
        "distance_to_val": _d3(lp, val0),
    }


def _recent_candles_slice(candles: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    if n <= 0 or not candles:
        return []
    tail = candles[-n:]
    return [
        {
            "t": c.get("t"),
            "o": c.get("o"),
            "h": c.get("h"),
            "l": c.get("l"),
            "c": c.get("c"),
            "v": c.get("v"),
            "d": c.get("d"),
            "cvd": c.get("cvd"),
            "vwap": c.get("vwap"),
            "poc_candle": (c.get("footprint_summary") or {}).get("poc_candle"),
            "delta_from_footprint": (c.get("footprint_summary") or {}).get("delta_from_footprint"),
        }
        for c in tail
    ]


def _recent_footprint_candles(candles: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    if n <= 0 or not candles:
        return []
    out: list[dict[str, Any]] = []
    for c in candles[-n:]:
        out.append(
            {
                "t": c.get("t"),
                "c": c.get("c"),
                "d": c.get("d"),
                "cvd": c.get("cvd"),
                "vwap": c.get("vwap"),
                "footprint_summary": c.get("footprint_summary"),
                "footprint": c.get("footprint") or [],
            }
        )
    return out


def _compact_session_histogram_simple(
    sp: dict[str, Any], *, options: AnalysisPayloadOptions
) -> dict[str, Any]:
    """If histogram is long, keep full poc/vah/val + 40 around POC + 20 near val + 20 near vah (plan §15)."""
    hist = sp.get("histogram")
    if not isinstance(hist, list) or len(hist) <= options.histogram_max:
        return dict(sp)
    by_p: list[tuple[float, dict[str, Any]]] = []
    for x in hist:
        if isinstance(x, dict) and "price" in x:
            try:
                by_p.append((float(x["price"]), x))
            except (TypeError, ValueError):
                continue
    if not by_p:
        return dict(sp)
    by_p.sort(key=lambda t: t[0])
    keys = [p for p, _ in by_p]
    m = {p: d for p, d in by_p}
    pick: set[float] = set()
    poc, vah, val = sp.get("poc"), sp.get("vah"), sp.get("val")
    for anchor in (poc, vah, val):
        if not isinstance(anchor, (int, float)):
            continue
        af = float(anchor)
        ip = min(range(len(keys)), key=lambda j: abs(keys[j] - af))
        pick.add(keys[ip])
    if isinstance(poc, (int, float)):
        ip = min(range(len(keys)), key=lambda j: abs(keys[j] - float(poc)))
        a = max(0, ip - options.histogram_around_poc // 2)
        b = min(len(keys), a + options.histogram_around_poc)
        for j in range(a, b):
            pick.add(keys[j])
    for anchor, span in ((val, options.histogram_near_val), (vah, options.histogram_near_vah)):
        if not isinstance(anchor, (int, float)):
            continue
        af = float(anchor)
        ip = min(range(len(keys)), key=lambda j: abs(keys[j] - af))
        a = max(0, ip - span // 2)
        b = min(len(keys), a + span)
        for j in range(a, b):
            pick.add(keys[j])
    new_hist = [m[p] for p in sorted(pick) if p in m]
    s2 = dict(sp)
    s2["histogram"] = new_hist
    s2["price_levels"] = len(new_hist)
    return s2


def build_session_master(raw_bundle: dict[str, Any]) -> dict[str, Any]:
    """
    Full structure for inspection (per plan §12): metadata, ``meta_by_tf``,
    one shared ``session_profile`` (histogram/POC/VAH/VAL from the **smallest**
    timeframe only), and ``frames`` with per-TF summary, candles, recent slices.
    """
    tfs: list[str] = list(raw_bundle.get("timeframes") or [])
    symbol = (raw_bundle.get("symbol") or "UNKNOWN").strip() or "UNKNOWN"
    at = raw_bundle.get("analysis_time")
    st = raw_bundle.get("session_start")
    stz = raw_bundle.get("session_timezone") or DEFAULT_SESSION_TZ
    cdl_by_iv: dict[str, list[dict[str, Any]]] = {}
    for iv in tfs:
        cdl_by_iv[iv] = _build_normalized_candles_for_tf(iv, raw_bundle)
    smallest = _smallest_timeframe(tfs)
    fine_candles = cdl_by_iv.get(smallest, []) if smallest else []
    global_sp = _session_profile_for_tf(smallest or "?", fine_candles)
    frames: dict[str, Any] = {}
    for iv in tfs:
        cdl = cdl_by_iv[iv]
        su = _summary_for_tf(iv, cdl, global_sp)
        n_lat = (
            DEFAULT_RECENT_5M
            if iv == "5m"
            else DEFAULT_RECENT_15M
        )
        rc = _recent_candles_slice(cdl, n_lat)
        rf = _recent_footprint_candles(cdl, DEFAULT_RECENT_FOOTPRINT)
        frames[iv] = {
            "summary": su,
            "candles": cdl,
            "recent_candles": rc,
            "recent_footprint_candles": rf,
        }
    return {
        "symbol": symbol,
        "analysis_time": at,
        "session_start": st,
        "session_timezone": stz,
        "source": SOURCE_MERGED,
        "meta_by_tf": raw_bundle.get("meta_by_tf") or {},
        "session_profile": global_sp,
        "frames": frames,
    }


def build_analysis_payload(
    master: dict[str, Any],
    options: Optional[AnalysisPayloadOptions] = None,
) -> dict[str, Any]:
    """
    Compact OpenAI payload (per plan §11B / §12): metadata + shared
    ``session_profile`` + per-frame recent_candles / recent_footprint_candles.
    """
    opt = options or AnalysisPayloadOptions()
    out_frames: dict[str, Any] = {}
    m_frames = master.get("frames")
    if not isinstance(m_frames, dict):
        m_frames = {}
    sp_top = master.get("session_profile")
    sp_out: Any = sp_top
    if isinstance(sp_top, dict):
        iv0 = str(sp_top.get("interval") or "")
        sp_out = _compact_session_histogram_simple({**sp_top, "interval": iv0}, options=opt)
    for iv, fr in m_frames.items():
        if not isinstance(fr, dict):
            continue
        cdl = fr.get("candles") or []
        if not isinstance(cdl, list):
            cdl = []
        n_recent = _recent_n_for_interval(str(iv), opt)
        n_fp = opt.recent_footprint_per_tf
        out_frames[iv] = {
            "summary": fr.get("summary"),
            "recent_candles": _recent_candles_slice(cdl, n_recent),
            "recent_footprint_candles": _recent_footprint_candles(cdl, n_fp),
        }
    return {
        "symbol": master.get("symbol"),
        "analysis_time": master.get("analysis_time"),
        "session_start": master.get("session_start"),
        "session_timezone": master.get("session_timezone") or DEFAULT_SESSION_TZ,
        "source": SOURCE_MERGED,
        "meta_by_tf": master.get("meta_by_tf") or {},
        "session_profile": sp_out,
        "frames": out_frames,
    }


def build_merged_analysis_from_files(
    file_paths: Sequence[str | Path],
    *,
    analysis_time: Optional[str] = None,
    session_start: Optional[str] = None,
    session_timezone: str = DEFAULT_SESSION_TZ,
    session_start_hour: int = DEFAULT_SESSION_START_HOUR,
    options: Optional[AnalysisPayloadOptions] = None,
) -> dict[str, Any]:
    """
    Load export(s), build :func:`build_session_master` then :func:`build_analysis_payload`.
    One file → one timeframe in ``frames`` (e.g. DXY 15m only); two files (15m+5m) → main.

    When ``session_start`` is omitted, it defaults to ``session_start_hour`` (default 5)
    in ``session_timezone``, anchored to the same calendar session as ``analysis_time``
    (or newest ``generated_at`` in the files, or UTC now).
    """
    raw = build_raw_bundle(
        list(file_paths),
        analysis_time=analysis_time,
        session_start=session_start,
        session_timezone=session_timezone,
        session_start_hour=session_start_hour,
    )
    master = build_session_master(raw)
    return build_analysis_payload(master, options=options)


if __name__ == "__main__":
    import sys

    ex = [
        "e5f23901a6ef484686ebab62b08b1693_20260422_094510_coinmap_XAUUSD_15m.json",
        "e5f23901a6ef484686ebab62b08b1693_20260422_100246_coinmap_XAUUSD_5m.json",
    ]
    args = [Path(a) for a in (sys.argv[1:] or ex)]
    for p in args:
        if not p.is_file():
            print("skip (missing):", p, file=sys.stderr)
            args = [x for x in args if x.is_file()]
            break
    if not args:
        raise SystemExit("Pass 1+ Coinmap JSON paths")
    out = build_merged_analysis_from_files(args)
    print(json.dumps(out, ensure_ascii=False, indent=2)[:8000])
