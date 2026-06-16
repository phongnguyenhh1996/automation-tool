"""
Validate on-disk chart artifacts for the OpenAI multimodal slot order (fixed slots; see images.py).

Coinmap exports must have non-empty lists for getcandlehistory, getorderflowhistory,
getindicatorsvwap (or merged JSON where used). TradingView: if ``.json`` exists it must
be valid tvdatafeed (non-empty ``bars``); otherwise a snapshot ``.url`` (https first line)
or ``.png`` satisfies the slot (same rules as ``ordered_chart_openai_payloads``).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from automation_tool.coinmap_merged import validate_coinmap_merged_payload
from automation_tool.images import (
    coinmap_merged_openai_files,
    effective_chart_image_order,
    read_main_chart_symbol,
)

COINMAP_OPENAI_KEYS: tuple[str, ...] = (
    "getcandlehistory",
    "getorderflowhistory",
    "getindicatorsvwap",
)

# Max lag between ``generated_at`` and newest ``getcandlehistory[0].ct`` (minutes).
COINMAP_MAX_LAG_MINUTES_BY_INTERVAL: dict[str, int] = {
    "5m": 15,
    "15m": 30,
}


def _coinmap_interval_from_payload(data: dict[str, Any]) -> str:
    iv = str(data.get("interval") or "").strip().lower()
    if iv:
        return iv
    candles = data.get("getcandlehistory")
    if isinstance(candles, list) and candles and isinstance(candles[0], dict):
        return str(candles[0].get("i") or "").strip().lower()
    return ""


def validate_coinmap_candle_freshness(data: dict[str, Any]) -> tuple[bool, str]:
    """
    Return (ok, reason). For 5m / 15m exports, newest candle must be within
    ``COINMAP_MAX_LAG_MINUTES_BY_INTERVAL`` of ``generated_at``.
    """
    iv = _coinmap_interval_from_payload(data)
    max_lag = COINMAP_MAX_LAG_MINUTES_BY_INTERVAL.get(iv)
    if max_lag is None:
        return True, ""

    gen_raw = data.get("generated_at")
    if not isinstance(gen_raw, str) or not gen_raw.strip():
        return False, "generated_at missing (required for candle freshness check)"

    candles = data.get("getcandlehistory")
    if not isinstance(candles, list) or not candles:
        return False, "getcandlehistory missing for freshness check"
    newest = candles[0]
    if not isinstance(newest, dict):
        return False, "getcandlehistory[0] is not an object"

    ct_raw = newest.get("ct")
    if ct_raw is None:
        t_raw = newest.get("t")
        if t_raw is None:
            return False, "getcandlehistory[0] missing t/ct"
        ct_ms = int(t_raw)
    else:
        ct_ms = int(ct_raw)

    try:
        gen = datetime.fromisoformat(gen_raw.replace("Z", "+00:00"))
    except ValueError:
        return False, f"generated_at invalid: {gen_raw!r}"
    if gen.tzinfo is None:
        gen = gen.replace(tzinfo=timezone.utc)

    ct_dt = datetime.fromtimestamp(ct_ms / 1000, tz=timezone.utc)
    lag_min = (gen - ct_dt).total_seconds() / 60.0
    if lag_min > max_lag:
        return (
            False,
            f"Coinmap data stale: newest candle lags generated_at by "
            f"{lag_min:.1f}m (max {max_lag}m for {iv})",
        )
    return True, ""


def validate_coinmap_export_payload(data: dict[str, Any]) -> tuple[bool, str]:
    """Return (ok, reason)."""
    for key in COINMAP_OPENAI_KEYS:
        val = data.get(key)
        if not isinstance(val, list) or len(val) == 0:
            return False, f"{key} missing, null, or empty list"
    ok, reason = validate_coinmap_candle_freshness(data)
    if not ok:
        return False, reason
    return True, ""


def validate_coinmap_json_file(path: Path) -> tuple[bool, str]:
    """Load a per-shot Coinmap export and run :func:`validate_coinmap_export_payload`."""
    data, err = _load_json(path)
    if err:
        return False, err
    return validate_coinmap_export_payload(data or {})


def validate_tradingview_tvdatafeed_payload(data: dict[str, Any]) -> tuple[bool, str]:
    """Return (ok, reason). Expect tvdatafeed JSON with ``bars`` list."""
    bars = data.get("bars")
    if not isinstance(bars, list) or len(bars) == 0:
        return False, "bars missing, null, or empty list"
    return True, ""


def _load_json(path: Path) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        return None, f"read error: {e}"
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e}"
    if not isinstance(obj, dict):
        return None, "root is not a JSON object"
    return obj, None


def _gocharting_csv_nonempty_lines(text: str) -> list[str]:
    return [ln for ln in text.strip().splitlines() if ln.strip()]


def _gocharting_csv_header_index(lines: Sequence[str]) -> Optional[int]:
    """First line with comma-separated columns (skips GoCharting branding lines)."""
    for i, ln in enumerate(lines):
        if "," in ln:
            return i
    return None


def normalize_gocharting_csv_text(text: str) -> str:
    """
    Drop leading non-CSV lines (e.g. ``www.gocharting.com``) from GoCharting exports.
  """
    lines = _gocharting_csv_nonempty_lines(text)
    hi = _gocharting_csv_header_index(lines)
    if hi is None:
        return text.strip()
    return "\n".join(lines[hi:]) + "\n"


DEFAULT_GOCHARTING_CSV_MAX_CANDLES = 150


def trim_gocharting_csv_candles(text: str, *, max_candles: int = DEFAULT_GOCHARTING_CSV_MAX_CANDLES) -> str:
    """
    Keep the header and at most ``max_candles`` newest data rows (exports are oldest-first).
    """
    normalized = normalize_gocharting_csv_text(text)
    if max_candles <= 0:
        return normalized
    lines = [ln for ln in normalized.splitlines() if ln.strip()]
    if len(lines) <= 1:
        return normalized
    header, data = lines[0], lines[1:]
    if len(data) <= max_candles:
        return normalized
    return "\n".join([header, *data[-max_candles:]]) + "\n"


def gocharting_csv_max_candles() -> int:
    raw = os.getenv("GOCHARTING_CSV_MAX_CANDLES", "").strip()
    if raw.isdigit():
        return max(1, int(raw))
    return DEFAULT_GOCHARTING_CSV_MAX_CANDLES


def prepare_gocharting_csv_text(text: str, *, max_candles: int | None = None) -> str:
    """Strip branding prefix and keep at most ``max_candles`` newest rows."""
    mc = max_candles if max_candles is not None else gocharting_csv_max_candles()
    return trim_gocharting_csv_candles(text, max_candles=mc)


def prepare_gocharting_csv_file(path: Path, *, max_candles: int | None = None) -> bool:
    """Normalize + trim on disk; return True when the file was rewritten."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return False
    prepared = prepare_gocharting_csv_text(raw, max_candles=max_candles)
    if prepared == raw:
        return False
    path.write_text(prepared, encoding="utf-8")
    return True


def normalize_gocharting_csv_file(path: Path) -> bool:
    """Backward compat alias for :func:`prepare_gocharting_csv_file`."""
    return prepare_gocharting_csv_file(path)


def validate_gocharting_csv_file(path: Path) -> tuple[bool, str]:
    """GoCharting export: non-empty CSV with a header row."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        return False, f"read error: {e}"
    text = raw.strip()
    if not text:
        return False, "empty CSV"
    lines = _gocharting_csv_nonempty_lines(text)
    if len(lines) < 2:
        return False, "CSV must have header + at least one data row"
    hi = _gocharting_csv_header_index(lines)
    if hi is None:
        return False, "CSV header missing comma-separated columns"
    if hi >= len(lines) - 1:
        return False, "CSV must have at least one data row"
    return True, ""


def _gocharting_slot_path(charts_dir: Path, stamp: str, sym: str, iv: str) -> Path:
    iv_slug = re.sub(r"[^\w]+", "_", iv).strip("_")[:20] or "iv"
    return charts_dir / f"{stamp}_gocharting_{sym}_{iv_slug}.csv"


def gocharting_raw_export_paths_for_stamp(charts_dir: Path, stamp: str) -> list[Path]:
    """Per-shot ``*_gocharting_*_{5m,15m}.csv`` for ``stamp``."""
    if not stamp or not charts_dir.is_dir():
        return []
    out: list[Path] = []
    for iv in ("15m", "5m"):
        for cp in sorted(charts_dir.glob(f"{stamp}_gocharting_*_{iv}.csv")):
            out.append(cp)
    dxy = charts_dir / f"{stamp}_gocharting_DXY_15m.csv"
    if dxy.is_file() and dxy not in out:
        out.insert(0, dxy)
    return out


def require_valid_gocharting_exports_for_stamp(charts_dir: Path, stamp: str) -> None:
    paths = gocharting_raw_export_paths_for_stamp(charts_dir, stamp)
    if not paths:
        raise SystemExit(
            f"No GoCharting CSV exports for stamp {stamp!r} under {charts_dir}."
        )
    require_valid_gocharting_csv_paths(paths)


def require_valid_gocharting_csv_paths(paths: Sequence[Path]) -> None:
    reasons: list[str] = []
    for cp in paths:
        ok, r = validate_gocharting_csv_file(cp)
        if not ok:
            reasons.append(f"{cp.name}: {r}")
    if reasons:
        raise SystemExit(f"GoCharting CSV validation failed: {'; '.join(reasons)}")


@dataclass(frozen=True)
class ChartSlotIssue:
    """One failed slot in fixed chart order."""

    source: str  # "coinmap" | "gocharting" | "tradingview"
    symbol: str
    interval: str
    expected_path: Path
    reason: str


def is_gocharting_stale_chart_issue(issue: ChartSlotIssue) -> bool:
    return "stale" in issue.reason.lower()


def _tradingview_slot_validation_issue(
    charts_dir: Path,
    stamp: str,
    sym: str,
    iv: str,
) -> Optional[ChartSlotIssue]:
    """
    None if the slot has a valid OpenAI artifact (tvdatafeed JSON, https snapshot URL, or PNG).
    """
    jp = charts_dir / f"{stamp}_tradingview_{sym}_{iv}.json"
    up = charts_dir / f"{stamp}_tradingview_{sym}_{iv}.url"
    pp = charts_dir / f"{stamp}_tradingview_{sym}_{iv}.png"

    if jp.is_file():
        data, err = _load_json(jp)
        if err:
            return ChartSlotIssue(
                source="tradingview",
                symbol=sym,
                interval=iv,
                expected_path=jp,
                reason=err,
            )
        ok, r = validate_tradingview_tvdatafeed_payload(data or {})
        if not ok:
            return ChartSlotIssue(
                source="tradingview",
                symbol=sym,
                interval=iv,
                expected_path=jp,
                reason=r,
            )
        return None

    if up.is_file():
        try:
            raw = up.read_text(encoding="utf-8").strip().splitlines()
        except OSError as e:
            return ChartSlotIssue(
                source="tradingview",
                symbol=sym,
                interval=iv,
                expected_path=up,
                reason=f"read error: {e}",
            )
        line = (raw[0] if raw else "").strip()
        if line.startswith("http://") or line.startswith("https://"):
            return None
        if pp.is_file():
            return None
        return ChartSlotIssue(
            source="tradingview",
            symbol=sym,
            interval=iv,
            expected_path=up,
            reason=".url first line is not http(s) and no fallback .png",
        )

    if pp.is_file():
        return None

    return ChartSlotIssue(
        source="tradingview",
        symbol=sym,
        interval=iv,
        expected_path=jp,
        reason="missing TradingView chart (.json, .url with https, or .png)",
    )


def _coinmap_raw_export_freshness_issues(
    charts_dir: Path,
    stamp: str,
    *,
    skip_paths: Optional[set[Path]] = None,
) -> list[ChartSlotIssue]:
    """Validate 5m/15m per-shot exports even when OpenAI uses ``*_merged.json``."""
    skip = skip_paths or set()
    issues: list[ChartSlotIssue] = []
    for iv in ("5m", "15m"):
        for jp in sorted(charts_dir.glob(f"{stamp}_coinmap_*_{iv}.json")):
            if jp in skip or jp.name.endswith("_merged.json"):
                continue
            data, err = _load_json(jp)
            if err:
                issues.append(
                    ChartSlotIssue(
                        source="coinmap",
                        symbol="",
                        interval=iv,
                        expected_path=jp,
                        reason=err,
                    )
                )
                continue
            ok, r = validate_coinmap_candle_freshness(data or {})
            if not ok:
                issues.append(
                    ChartSlotIssue(
                        source="coinmap",
                        symbol="",
                        interval=iv,
                        expected_path=jp,
                        reason=r,
                    )
                )
    return issues


def list_invalid_chart_slots_for_stamp(
    charts_dir: Path,
    stamp: str,
) -> list[ChartSlotIssue]:
    """
    Check each slot in ``effective_chart_image_order`` against what
    ``ordered_chart_openai_payloads`` would attach.

    Coinmap slots require ``.json`` (or merged paths) with valid payload.
    TradingView accepts ``.json`` (validated), else ``.url`` (https) or ``.png``.
    """
    if not stamp or not charts_dir.is_dir():
        return []
    main_sym = read_main_chart_symbol(charts_dir)
    dxy_m, main_m = coinmap_merged_openai_files(charts_dir, stamp, main_sym)
    order = effective_chart_image_order(charts_dir, stamp=stamp)
    issues: list[ChartSlotIssue] = []
    validated_export_paths: set[Path] = set()
    for src, sym, iv in order:
        if src == "gocharting":
            cp = _gocharting_slot_path(charts_dir, stamp, sym, iv)
            if not cp.is_file():
                issues.append(
                    ChartSlotIssue(
                        source=src,
                        symbol=sym,
                        interval=iv,
                        expected_path=cp,
                        reason="missing .csv (required for OpenAI validation)",
                    )
                )
                continue
            ok, r = validate_gocharting_csv_file(cp)
            if not ok:
                issues.append(
                    ChartSlotIssue(
                        source=src,
                        symbol=sym,
                        interval=iv,
                        expected_path=cp,
                        reason=r,
                    )
                )
            continue
        if src == "coinmap" and dxy_m is not None and sym == "DXY" and iv == "15m":
            jp = dxy_m
        elif src == "coinmap" and main_m is not None and sym == main_sym and iv == "15m":
            jp = main_m
        elif src == "coinmap" and main_m is not None and sym == main_sym and iv == "5m":
            continue
        elif src == "tradingview":
            tv_issue = _tradingview_slot_validation_issue(charts_dir, stamp, sym, iv)
            if tv_issue is not None:
                issues.append(tv_issue)
            continue
        else:
            jp = charts_dir / f"{stamp}_{src}_{sym}_{iv}.json"
        if not jp.is_file():
            issues.append(
                ChartSlotIssue(
                    source=src,
                    symbol=sym,
                    interval=iv,
                    expected_path=jp,
                    reason="missing .json (required for OpenAI validation)",
                )
            )
            continue
        data, err = _load_json(jp)
        if err:
            issues.append(
                ChartSlotIssue(
                    source=src,
                    symbol=sym,
                    interval=iv,
                    expected_path=jp,
                    reason=err,
                )
            )
            continue
        if src == "coinmap" and jp.name.endswith("_merged.json"):
            ok, r = validate_coinmap_merged_payload(data or {})
            if not ok:
                issues.append(
                    ChartSlotIssue(
                        source=src,
                        symbol=sym,
                        interval=iv,
                        expected_path=jp,
                        reason=r,
                    )
                )
        else:
            validated_export_paths.add(jp)
            ok, r = validate_coinmap_export_payload(data or {})
            if not ok:
                issues.append(
                    ChartSlotIssue(
                        source=src,
                        symbol=sym,
                        interval=iv,
                        expected_path=jp,
                        reason=r,
                    )
                )
    issues.extend(
        _coinmap_raw_export_freshness_issues(
            charts_dir, stamp, skip_paths=validated_export_paths
        )
    )
    return issues


def coinmap_raw_export_paths_for_stamp(charts_dir: Path, stamp: str) -> list[Path]:
    """Per-shot ``*_coinmap_*_{5m,15m}.json`` for ``stamp`` (excludes ``*_merged.json``)."""
    if not stamp or not charts_dir.is_dir():
        return []
    out: list[Path] = []
    for iv in ("15m", "5m"):
        for jp in sorted(charts_dir.glob(f"{stamp}_coinmap_*_{iv}.json")):
            if jp.name.endswith("_merged.json"):
                continue
            out.append(jp)
    return out


def is_coinmap_stale_chart_issue(issue: ChartSlotIssue) -> bool:
    return "stale" in issue.reason.lower()


def require_valid_coinmap_json_paths(paths: Sequence[Path]) -> None:
    """Exit-style guard for intraday flows (``update`` / ``update-scalp``)."""
    reasons: list[str] = []
    for jp in paths:
        ok, r = validate_coinmap_json_file(jp)
        if not ok:
            reasons.append(f"{jp.name}: {r}")
    if reasons:
        raise SystemExit(f"Coinmap JSON validation failed: {'; '.join(reasons)}")


def require_valid_coinmap_exports_for_stamp(charts_dir: Path, stamp: str) -> None:
    """
    Validate endpoint payloads + 5m/15m freshness for every raw Coinmap export in ``stamp``.
    Used by ``all``, ``all-2``, ``update``, and ``update-scalp``.
    """
    paths = coinmap_raw_export_paths_for_stamp(charts_dir, stamp)
    if not paths:
        raise SystemExit(
            f"No Coinmap 5m/15m JSON exports for stamp {stamp!r} under {charts_dir}."
        )
    require_valid_coinmap_json_paths(paths)


def filter_coinmap_plan_for_retry_paths(
    plan: list[dict[str, Any]],
    stamp: str,
    target_paths: list[Path],
) -> list[dict[str, Any]]:
    """Sub-plan for bearer re-export: only steps that write one of ``target_paths``."""
    stems = {p.stem for p in target_paths}
    out: list[dict[str, Any]] = []
    for step in plan:
        for st in stems:
            if coinmap_json_stem_matches_step(stamp, step, st):
                out.append(step)
                break
    return out


def coinmap_json_stem_matches_step(stamp: str, step: dict[str, Any], path_stem: str) -> bool:
    """
    True if ``step`` would write ``{path_stem}.json`` for this stamp
    (same rules as ``_run_bearer_request_api_only_flow``).
    """
    sym = step.get("symbol")
    interval = step.get("interval")
    if not isinstance(sym, str) or not isinstance(interval, str):
        return False
    sym = sym.strip()
    interval = interval.strip()
    if not sym or not interval:
        return False
    ex = step.get("export_symbol")
    label = (ex.strip() if isinstance(ex, str) and ex.strip() else sym)
    sym_slug = re.sub(r"[^\w.-]+", "_", label).strip("_")[:40] or "sym"
    iv_slug = re.sub(r"[^\w]+", "_", interval).strip("_")[:20] or "iv"
    expected = f"{stamp}_coinmap_{sym_slug}_{iv_slug}"
    return expected == path_stem
