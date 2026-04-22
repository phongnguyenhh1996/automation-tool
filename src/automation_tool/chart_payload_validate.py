"""
Validate on-disk chart JSON for the OpenAI multimodal slot order (10 slots).

Coinmap exports must have non-empty lists for getcandlehistory, getorderflowhistory,
getindicatorsvwap. TradingView tvdatafeed exports must have a non-empty ``bars`` array.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

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

def validate_coinmap_export_payload(data: dict[str, Any]) -> tuple[bool, str]:
    """Return (ok, reason)."""
    for key in COINMAP_OPENAI_KEYS:
        val = data.get(key)
        if not isinstance(val, list) or len(val) == 0:
            return False, f"{key} missing, null, or empty list"
    return True, ""


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


@dataclass(frozen=True)
class ChartSlotIssue:
    """One failed slot in fixed chart order."""

    source: str  # "coinmap" | "tradingview"
    symbol: str
    interval: str
    expected_path: Path
    reason: str


def list_invalid_chart_slots_for_stamp(
    charts_dir: Path,
    stamp: str,
) -> list[ChartSlotIssue]:
    """
    Check each of the 10 ``effective_chart_image_order`` slots: require the same
    artifact as ``ordered_chart_openai_payloads`` (``.json`` preferred).

    Missing ``.json`` or invalid payload → issue (PNG/.url alone is invalid).
    """
    if not stamp or not charts_dir.is_dir():
        return []
    main_sym = read_main_chart_symbol(charts_dir)
    dxy_m, main_m = coinmap_merged_openai_files(charts_dir, stamp, main_sym)
    order = effective_chart_image_order(charts_dir)
    issues: list[ChartSlotIssue] = []
    for src, sym, iv in order:
        if src == "coinmap" and dxy_m is not None and sym == "DXY" and iv == "15m":
            jp = dxy_m
        elif src == "coinmap" and main_m is not None and sym == main_sym and iv == "15m":
            jp = main_m
        elif src == "coinmap" and main_m is not None and sym == main_sym and iv == "5m":
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
        elif src == "coinmap":
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
        else:
            ok, r = validate_tradingview_tvdatafeed_payload(data or {})
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
    return issues


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
