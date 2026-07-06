"""Persist open scalp signals and check TP/SL on subsequent watch cycles."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_TRADES_NAME = "scalp_footprint_trades.json"
DEFAULT_MAX_HOLD_BARS = 12

STATUS_OPEN = "OPEN"
STATUS_WIN = "WIN"
STATUS_LOSS = "LOSS"
STATUS_TIMEOUT = "TIMEOUT"


def trade_id_from_signal(sig: dict[str, Any]) -> str:
    return "|".join(
        [
            str(sig.get("timeframe") or ""),
            str(sig.get("time_gmt7") or ""),
            str(sig.get("pattern_id") or ""),
            str(sig.get("bar_index") or ""),
        ]
    )


def load_trades(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"trades": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"trades": []}
    if not isinstance(data, dict):
        return {"trades": []}
    data.setdefault("trades", [])
    return data


def save_trades(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _find_bar_by_time(candles: list[dict[str, Any]], time_gmt7: str) -> int | None:
    key = str(time_gmt7 or "").strip()
    if not key:
        return None
    for i, c in enumerate(candles):
        if str(c.get("time_gmt7") or "").strip() == key:
            return i
    return None


def _bar_close_outcome(
    *,
    is_long: bool,
    entry: float,
    sl: float,
    tp: float,
    o: float,
    h: float,
    l: float,
) -> tuple[str, float, str] | None:
    if is_long:
        sl_hit = l <= sl
        tp_hit = h >= tp
    else:
        sl_hit = h >= sl
        tp_hit = l <= tp

    if sl_hit and tp_hit:
        if (is_long and o <= sl) or (not is_long and o >= sl):
            pnl = sl - entry if is_long else entry - sl
            return STATUS_LOSS, sl, "SL hit (same bar as TP)"
        pnl = tp - entry if is_long else entry - tp
        return STATUS_WIN, tp, "TP hit (same bar as SL)"

    if sl_hit:
        return STATUS_LOSS, sl, "stop loss"

    if tp_hit:
        return STATUS_WIN, tp, "take profit"

    return None


def _pnl(is_long: bool, entry: float, exit_px: float) -> float:
    return exit_px - entry if is_long else entry - exit_px


def _eval_start_offset(sig: dict[str, Any]) -> int:
    """Bars to skip before SL/TP scan.

    MARKET @ signal close (exhaustion, sweep, …): entry when bar closes → check from *next* bar.
    absorption_trap @ confirm open: entry at bar open → check from same bar.
    """
    pid = str(sig.get("pattern_id") or "")
    if pid.startswith("absorption_trap"):
        return 0
    return 1


def open_trade_from_signal(sig: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    offset = _eval_start_offset(sig)
    return {
        "id": trade_id_from_signal(sig),
        "status": STATUS_OPEN,
        "signal": sig,
        "timeframe": sig.get("timeframe"),
        "entry_time_gmt7": sig.get("time_gmt7") or "",
        "entry_price": sig.get("entry_price"),
        "stop_loss": sig.get("stop_loss"),
        "take_profit": (sig.get("take_profit") or [None])[0],
        "direction": sig.get("direction"),
        "eval_start_offset": offset,
        "last_checked_time_gmt7": None,
        "opened_at": now,
        "closed_at": None,
        "exit_time_gmt7": None,
        "exit_price": None,
        "pnl": None,
        "bars_held": None,
        "detail": None,
    }


def register_open_trades(path: Path, signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Append new signals not already tracked (open or closed)."""
    data = load_trades(path)
    known = {t.get("id") for t in data["trades"] if isinstance(t, dict)}
    added: list[dict[str, Any]] = []
    for sig in signals:
        tid = trade_id_from_signal(sig)
        if tid in known:
            continue
        trade = open_trade_from_signal(sig)
        data["trades"].append(trade)
        known.add(tid)
        added.append(trade)
    if added:
        data["trades"] = data["trades"][-300:]
        save_trades(path, data)
    return added


def reset_trade_for_recheck(trade: dict[str, Any]) -> None:
    """Clear outcome fields so trade can be re-evaluated from scratch."""
    trade["status"] = STATUS_OPEN
    trade["last_checked_time_gmt7"] = None
    trade["closed_at"] = None
    trade["exit_time_gmt7"] = None
    trade["exit_price"] = None
    trade["pnl"] = None
    trade["bars_held"] = None
    trade["detail"] = None
    if trade.get("eval_start_offset") is None:
        trade["eval_start_offset"] = _eval_start_offset(trade.get("signal") or {})


def recheck_all_trades(
    path: Path,
    candles_by_interval: dict[str, list[dict[str, Any]]],
    *,
    max_bars: int = DEFAULT_MAX_HOLD_BARS,
) -> list[dict[str, Any]]:
    """Reconcile every trade in file against latest candles (fixes stale 0-bar wins)."""
    data = load_trades(path)
    reconciled: list[dict[str, Any]] = []

    for trade in data["trades"]:
        if not isinstance(trade, dict):
            continue
        before = trade.get("status")
        reset_trade_for_recheck(trade)
        iv = str(trade.get("timeframe") or "").lower()
        candles = candles_by_interval.get(iv) or []
        if not candles:
            trade["_recheck_note"] = "no candle data"
            reconciled.append(trade)
            continue
        _evaluate_one_trade(trade, candles, max_bars=max_bars)
        trade.pop("_recheck_note", None)
        after = trade.get("status")
        if before != after or (trade.get("bars_held") == 0 and after != STATUS_OPEN):
            trade["_was_status"] = before
        reconciled.append(trade)

    for trade in data["trades"]:
        if isinstance(trade, dict):
            trade.pop("_was_status", None)
            trade.pop("_recheck_note", None)

    save_trades(path, data)
    return reconciled


def evaluate_open_trades(
    path: Path,
    candles: list[dict[str, Any]],
    *,
    interval: str,
    max_bars: int = DEFAULT_MAX_HOLD_BARS,
) -> list[dict[str, Any]]:
    """Update OPEN trades for ``interval``; return list that just closed."""
    data = load_trades(path)
    closed_now: list[dict[str, Any]] = []
    iv = interval.strip().lower()
    changed = False

    for trade in data["trades"]:
        if not isinstance(trade, dict):
            continue
        if trade.get("status") != STATUS_OPEN:
            continue
        if str(trade.get("timeframe") or "").lower() != iv:
            continue

        updated = _evaluate_one_trade(trade, candles, max_bars=max_bars)
        if updated is None:
            continue
        changed = True
        if updated.get("status") != STATUS_OPEN:
            closed_now.append(updated)

    if changed:
        save_trades(path, data)
    return closed_now


def _trade_eval_start_offset(trade: dict[str, Any]) -> int:
    raw = trade.get("eval_start_offset")
    if raw is not None:
        return int(raw)
    return _eval_start_offset(trade.get("signal") or {})


def _evaluate_one_trade(
    trade: dict[str, Any],
    candles: list[dict[str, Any]],
    *,
    max_bars: int,
) -> dict[str, Any] | None:
    entry_time = str(trade.get("entry_time_gmt7") or "")
    entry_idx = _find_bar_by_time(candles, entry_time)
    if entry_idx is None:
        return None

    offset = _trade_eval_start_offset(trade)
    eval_start_idx = entry_idx + offset
    if eval_start_idx >= len(candles):
        return None

    entry = float(trade["entry_price"])
    sl = float(trade["stop_loss"])
    tp = float(trade["take_profit"])
    is_long = str(trade.get("direction") or "").lower() == "long"

    start = eval_start_idx
    last_checked = trade.get("last_checked_time_gmt7")
    if last_checked:
        lc_idx = _find_bar_by_time(candles, str(last_checked))
        if lc_idx is not None:
            start = max(eval_start_idx, lc_idx + 1)

    # max_bars holding period begins on first bar where position is live
    deadline_idx = eval_start_idx + max_bars - 1
    last_available = len(candles) - 1
    scan_end = min(last_available, deadline_idx)

    if start > scan_end:
        return None

    for j in range(start, scan_end + 1):
        ohlc = candles[j]["ohlc"]
        o, h, l = float(ohlc["open"]), float(ohlc["high"]), float(ohlc["low"])
        hit = _bar_close_outcome(is_long=is_long, entry=entry, sl=sl, tp=tp, o=o, h=h, l=l)
        trade["last_checked_time_gmt7"] = candles[j].get("time_gmt7")
        if hit is not None:
            status, exit_px, detail = hit
            trade.update(
                _closed_fields(
                    status=status,
                    exit_time=candles[j].get("time_gmt7", ""),
                    exit_price=exit_px,
                    entry=entry,
                    is_long=is_long,
                    eval_start_idx=eval_start_idx,
                    exit_idx=j,
                    detail=detail,
                )
            )
            return trade

    if scan_end == deadline_idx:
        last = candles[deadline_idx]
        trade.update(
            _closed_fields(
                status=STATUS_TIMEOUT,
                exit_time=last.get("time_gmt7", ""),
                exit_price=float(last["ohlc"]["close"]),
                entry=entry,
                is_long=is_long,
                eval_start_idx=eval_start_idx,
                exit_idx=deadline_idx,
                detail=f"timeout after {max_bars} bars",
            )
        )
        return trade

    return trade


def _closed_fields(
    *,
    status: str,
    exit_time: str,
    exit_price: float,
    entry: float,
    is_long: bool,
    eval_start_idx: int,
    exit_idx: int,
    detail: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "exit_time_gmt7": exit_time,
        "exit_price": round(exit_price, 2),
        "pnl": round(_pnl(is_long, entry, exit_price), 2),
        "bars_held": exit_idx - eval_start_idx + 1,
        "detail": detail,
    }


def format_outcome_message(trade: dict[str, Any]) -> str:
    sig = trade.get("signal") or {}
    status = trade.get("status") or ""
    emoji = {"WIN": "✅", "LOSS": "❌", "TIMEOUT": "⏱"}.get(status, "📌")
    side = sig.get("side") or ("BUY" if sig.get("direction") == "long" else "SELL")
    pnl = trade.get("pnl")
    pnl_s = f"{pnl:+.2f}" if pnl is not None else "?"
    lines = [
        f"{emoji} {status} — {side} {sig.get('pattern_id', '')}",
        f"  entry: {trade.get('entry_price')} @ {trade.get('entry_time_gmt7', '')}",
        f"  exit:  {trade.get('exit_price')} @ {trade.get('exit_time_gmt7', '')}",
        f"  PnL: {pnl_s} pts  ({trade.get('bars_held')} bars)  SL={trade.get('stop_loss')} TP={trade.get('take_profit')}",
        f"  {trade.get('detail', '')}",
    ]
    hint = sig.get("entry_hint")
    if hint:
        lines.append(f"  note: {hint}")
    return "\n".join(lines)
