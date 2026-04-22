"""
Write and validate Coinmap `*_merged.json` payloads for OpenAI (multi-TF or DXY 15m-only).

Called after per-timeframe API exports are saved to disk. See :mod:`market_merge_single`.
"""

from __future__ import annotations

import json
import re
import logging
from pathlib import Path
from typing import Any

from automation_tool.images import read_main_chart_symbol
from automation_tool.market_merge_single import (
    DEFAULT_SESSION_TZ,
    build_merged_analysis_from_files,
)

_log = logging.getLogger(__name__)

_SAFE_SLUG = re.compile(r"[^\w.-]+")


def _sym_slug(name: str) -> str:
    s = (name or "").strip()
    if not s:
        return "sym"
    return _SAFE_SLUG.sub("_", s).strip("_")[:40] or "sym"


def write_coinmap_merged_json(
    charts_dir: Path,
    stamp: str,
    *,
    raw_paths: list[Path],
    out_path: Path,
) -> Path:
    """Build analysis payload and write one merged JSON file."""
    payload = build_merged_analysis_from_files(
        raw_paths, session_timezone=DEFAULT_SESSION_TZ
    )
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _log.info("coinmap merged written: %s", out_path.resolve())
    return out_path


def write_merged_for_main_pair(
    charts_dir: Path,
    stamp: str,
) -> Path | None:
    """M15 + M5 for the main watchlist label (e.g. XAUUSD)."""
    main = read_main_chart_symbol(charts_dir)
    s15 = charts_dir / f"{stamp}_coinmap_{_sym_slug(main)}_15m.json"
    s5 = charts_dir / f"{stamp}_coinmap_{_sym_slug(main)}_5m.json"
    if not s15.is_file() or not s5.is_file():
        _log.warning("merged main: missing 15m or 5m | 15m=%s 5m=%s", s15, s5)
        return None
    out = charts_dir / f"{stamp}_coinmap_{_sym_slug(main)}_merged.json"
    return write_coinmap_merged_json(charts_dir, stamp, raw_paths=[s15, s5], out_path=out)


def write_merged_for_dxy(
    charts_dir: Path,
    stamp: str,
    *,
    export_label: str = "DXY",
) -> Path | None:
    """DXY: single 15m export, same ``coinmap_merged`` schema; ``frames`` only ``15m``."""
    s15 = charts_dir / f"{stamp}_coinmap_{_sym_slug(export_label)}_15m.json"
    if not s15.is_file():
        _log.warning("merged DXY: missing 15m | %s", s15)
        return None
    out = charts_dir / f"{stamp}_coinmap_{_sym_slug(export_label)}_merged.json"
    return write_coinmap_merged_json(
        charts_dir, stamp, raw_paths=[s15], out_path=out
    )


def run_coinmap_merged_writes(charts_dir: Path, stamp: str) -> dict[str, Path]:
    """
    After capture: write DXY and main ``*_merged.json`` if raw inputs exist.
    Returns map ``{"dxy"|"main" -> path}`` for each success.
    """
    out: dict[str, Path] = {}
    p_dxy = write_merged_for_dxy(charts_dir, stamp, export_label="DXY")
    if p_dxy is not None:
        out["dxy"] = p_dxy
    p_main = write_merged_for_main_pair(charts_dir, stamp)
    if p_main is not None:
        out["main"] = p_main
    return out


def validate_coinmap_merged_payload(data: dict[str, Any]) -> tuple[bool, str]:
    """``source``, top-level ``session_profile``, and ``frames`` (per-frame summary only)."""
    if not isinstance(data, dict):
        return False, "not an object"
    if data.get("source") != "coinmap_merged":
        return False, "source is not coinmap_merged"
    ss = data.get("session_start")
    if not isinstance(ss, str) or not ss.strip():
        return False, "session_start missing or empty"
    sp = data.get("session_profile")
    if not isinstance(sp, dict):
        return False, "session_profile missing or not an object at top level"
    for key in ("poc", "histogram"):
        if key not in sp:
            return False, f"session_profile missing {key!r}"
    fr = data.get("frames")
    if not isinstance(fr, dict) or not fr:
        return False, "frames missing or empty"
    tks = set(fr.keys())
    if "15m" not in tks:
        return False, "frames must include 15m"
    for k, v in fr.items():
        if not isinstance(v, dict) or "summary" not in v:
            return False, f"frames[{k!r}] missing summary"
    if tks == {"15m"}:
        return True, ""
    if tks >= {"15m", "5m"} and tks.issubset({"15m", "5m"}):
        return True, ""
    return False, f"unexpected frames keys: {sorted(tks)}"
