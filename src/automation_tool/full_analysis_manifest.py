"""
Build a JSON manifest of FULL_ANALYSIS chart slots and footprint readiness.

Used by AI agents after ``capture-full-analysis --gocharting`` to know which
files to read and whether analysis can proceed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from automation_tool.chart_payload_validate import list_invalid_chart_slots_for_stamp
from automation_tool.config import default_charts_dir, default_gocharting_config_path, default_vector_store_knowledge_dir
from automation_tool.gocharting_capture import load_gocharting_yaml
from automation_tool.vector_store_sync import (
    knowledge_is_ready,
    knowledge_manifest_path,
    list_knowledge_text_paths,
)
from automation_tool.images import (
    effective_chart_image_order,
    existing_footprint_combined_json_paths,
    gocharting_interval_csv_path,
    gocharting_png_path_for_csv,
    latest_chart_stamp,
    read_main_chart_symbol,
)


def _slot_key(source: str, symbol: str, interval: str) -> tuple[str, str, str]:
    return (source, symbol, interval)


def _resolve_tradingview_paths(charts_dir: Path, stamp: str, sym: str, iv: str) -> list[Path]:
    from automation_tool.images import tradingview_file_symbol

    iv_slug = re.sub(r"[^\w]+", "_", iv).strip("_")[:20] or "iv"
    file_sym = tradingview_file_symbol(sym)
    jp = charts_dir / f"{stamp}_tradingview_{file_sym}_{iv_slug}.json"
    up = charts_dir / f"{stamp}_tradingview_{file_sym}_{iv_slug}.url"
    pp = charts_dir / f"{stamp}_tradingview_{file_sym}_{iv_slug}.png"
    if jp.is_file():
        return [jp]
    if up.is_file():
        try:
            raw = up.read_text(encoding="utf-8").strip().splitlines()
            line = (raw[0] if raw else "").strip()
            if line.startswith("http://") or line.startswith("https://"):
                return [up]
        except OSError:
            pass
    if pp.is_file():
        return [pp]
    return []


def _resolve_gocharting_paths(
    charts_dir: Path,
    stamp: str,
    sym: str,
    iv: str,
    *,
    gocharting_yaml: Path | None = None,
) -> list[Path]:
    from automation_tool.config import default_gocharting_config_path
    from automation_tool.gocharting_capture import load_gocharting_yaml
    from automation_tool.gocharting_ws_decode import footprint_ws_enabled
    from automation_tool.images import GOCHARTING_GOLD_EXPORT_LABEL

    gc_yaml = gocharting_yaml or default_gocharting_config_path()
    cfg = load_gocharting_yaml(gc_yaml)
    if footprint_ws_enabled(cfg) and sym.upper() == GOCHARTING_GOLD_EXPORT_LABEL:
        from automation_tool.chart_payload_validate import gocharting_footprint_ws_json_path

        fp = gocharting_footprint_ws_json_path(charts_dir, iv, gocharting_yaml=gc_yaml)
        return [fp] if fp.is_file() else []
    cp = gocharting_interval_csv_path(charts_dir, sym, iv, stamp=stamp)
    if cp is None:
        return []
    paths: list[Path] = [cp]
    png = gocharting_png_path_for_csv(cp)
    if png is not None:
        paths.append(png)
    return paths


def _resolve_slot_paths(
    charts_dir: Path,
    stamp: str,
    source: str,
    sym: str,
    iv: str,
    *,
    gocharting_yaml: Path | None = None,
) -> list[Path]:
    if source == "tradingview":
        return _resolve_tradingview_paths(charts_dir, stamp, sym, iv)
    if source == "gocharting":
        return _resolve_gocharting_paths(
            charts_dir,
            stamp,
            sym,
            iv,
            gocharting_yaml=gocharting_yaml,
        )
    jp = charts_dir / f"{stamp}_{source}_{sym}_{iv}.json"
    pp = charts_dir / f"{stamp}_{source}_{sym}_{iv}.png"
    out: list[Path] = []
    if jp.is_file():
        out.append(jp)
    if pp.is_file():
        out.append(pp)
    return out


def build_full_analysis_manifest(
    charts_dir: Path,
    *,
    stamp: Optional[str] = None,
    gocharting_yaml: Optional[Path] = None,
    legacy: bool = False,
    knowledge_dir: Optional[Path] = None,
    vector_store_ids: Optional[list[str]] = None,
) -> dict[str, Any]:
    """
    Return manifest dict with slot order, resolved paths, footprint JSON, readiness.
    """
    charts_dir = Path(charts_dir)
    gc_yaml = gocharting_yaml or default_gocharting_config_path()
    st = stamp or latest_chart_stamp(charts_dir)
    main_sym = read_main_chart_symbol(charts_dir)

    if not st:
        return {
            "stamp": None,
            "main_symbol": main_sym,
            "charts_dir": str(charts_dir.resolve()),
            "slots": [],
            "footprint_json": [],
            "ready_for_analysis": False,
            "error": "no capture stamp found under charts_dir",
        }

    order = effective_chart_image_order(charts_dir, stamp=st)
    invalid = list_invalid_chart_slots_for_stamp(
        charts_dir,
        st,
        gocharting_cfg=load_gocharting_yaml(gc_yaml),
    )
    invalid_map = {_slot_key(i.source, i.symbol, i.interval): i for i in invalid}

    slots: list[dict[str, Any]] = []
    for idx, (src, sym, iv) in enumerate(order, start=1):
        key = _slot_key(src, sym, iv)
        issue = invalid_map.get(key)
        resolved_paths = _resolve_slot_paths(
            charts_dir,
            st,
            src,
            sym,
            iv,
            gocharting_yaml=gc_yaml,
        )
        if issue is not None:
            status = "stale" if "stale" in issue.reason.lower() else "missing"
            reason = issue.reason
            if not resolved_paths:
                resolved_paths = [issue.expected_path]
        elif resolved_paths:
            status = "ok"
            reason = ""
        else:
            status = "missing"
            reason = "no files resolved for slot"
        slots.append(
            {
                "index": idx,
                "source": src,
                "symbol": sym,
                "interval": iv,
                "resolved_paths": [str(p.resolve()) for p in resolved_paths],
                "status": status,
                "reason": reason,
            }
        )

    footprint_paths = [
        str(p.resolve())
        for p in existing_footprint_combined_json_paths(charts_dir, gocharting_yaml=gc_yaml)
    ]
    footprint_ok = len(footprint_paths) >= 2
    slots_ok = all(s["status"] == "ok" for s in slots)

    manifest: dict[str, Any] = {
        "stamp": st,
        "main_symbol": main_sym,
        "charts_dir": str(charts_dir.resolve()),
        "slots": slots,
        "footprint_json": footprint_paths,
        "ready_for_analysis": slots_ok and footprint_ok,
    }

    if legacy:
        kdir = Path(knowledge_dir or default_vector_store_knowledge_dir())
        k_paths = [str(p.resolve()) for p in list_knowledge_text_paths(kdir)]
        k_ready = knowledge_is_ready(kdir, vector_store_ids=vector_store_ids)
        manifest.update(
            {
                "analysis_mode": "legacy",
                "knowledge_source": "openai_vector_store",
                "knowledge_dir": str(kdir.resolve()),
                "knowledge_manifest": str(knowledge_manifest_path(kdir).resolve()),
                "knowledge_files": k_paths,
                "knowledge_ready": k_ready,
                "ready_for_analysis": manifest["ready_for_analysis"] and k_ready,
            }
        )
    else:
        manifest["analysis_mode"] = "playbook"

    return manifest


def manifest_to_json(manifest: dict[str, Any], *, indent: int = 2) -> str:
    return json.dumps(manifest, ensure_ascii=False, indent=indent)


def default_charts_dir_for_manifest(charts_dir: Optional[Path] = None) -> Path:
    return charts_dir if charts_dir is not None else default_charts_dir()
