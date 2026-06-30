from __future__ import annotations

import base64
import mimetypes
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Optional, Tuple, Union

from automation_tool.gocharting_image_crop import (
    gocharting_detail_openai_image_paths,
    resolve_gocharting_detail_crop_width_thirds,
)

# OpenAI multimodal slot: ``json`` / ``csv`` → Path; ``image`` → PNG; ``image_url`` → https string.
ChartOpenAIPayload = Tuple[str, Union[Path, str]]

# Per-charts marker; global active pair is ``data/.main_chart_symbol`` (see ``get_active_main_symbol``).
MAIN_CHART_SYMBOL_FILENAME = ".main_chart_symbol"
GLOBAL_MAIN_CHART_SYMBOL_FILENAME = ".main_chart_symbol"
DEFAULT_MAIN_CHART_SYMBOL = "XAUUSD"
# GoCharting footprint filename slug for main gold pair (GC1! future, not spot XAUUSD).
GOCHARTING_GOLD_EXPORT_LABEL = "GC"


def gocharting_footprint_export_label(main_sym: str) -> str:
    """Filename symbol for main-pair GoCharting exports (``XAUUSD`` → ``GC``)."""
    m = (main_sym or "").strip().upper() or DEFAULT_MAIN_CHART_SYMBOL
    if m == DEFAULT_MAIN_CHART_SYMBOL:
        return GOCHARTING_GOLD_EXPORT_LABEL
    return m


def footprint_bid_ask_openai_payloads(charts_dir: Path) -> list[ChartOpenAIPayload]:
    """OCR footprint JSON from ``footprint-gocharting-screenshot`` daemon (if on disk)."""
    from automation_tool.gocharting_footprint_ocr import existing_footprint_bid_ask_json_paths

    return [("json", path) for path in existing_footprint_bid_ask_json_paths(charts_dir)]


def existing_footprint_combined_json_paths(
    charts_dir: Path,
    *,
    gocharting_yaml: Optional[Path] = None,
    intervals: tuple[str, ...] = ("15m", "5m"),
) -> list[Path]:
    """Return on-disk WS combined footprint JSON files (15m then 5m), skipping missing paths."""
    from automation_tool.gocharting_footprint_ocr import footprint_images_dir
    from automation_tool.gocharting_ws_decode import footprint_combined_json_path

    out_dir = footprint_images_dir(charts_dir, gocharting_yaml=gocharting_yaml)
    paths: list[Path] = []
    for interval in intervals:
        path = footprint_combined_json_path(out_dir, interval)
        if path.is_file():
            paths.append(path)
    return paths


def footprint_combined_openai_payloads(charts_dir: Path) -> list[ChartOpenAIPayload]:
    """WS combined footprint JSON from ``footprint_ws`` capture (if on disk)."""
    return [("json", path) for path in existing_footprint_combined_json_paths(charts_dir)]


def existing_prepared_footprint_json_paths(
    charts_dir: Path,
    *,
    logic_symbol: str | None = None,
    intervals: tuple[str, ...] = ("15m", "5m"),
) -> list[Path]:
    """Return on-disk prepared footprint JSON files (15m then 5m)."""
    from automation_tool.gocharting_gc_spot_convert import prepared_footprint_json_path

    sym = (logic_symbol or read_main_chart_symbol(charts_dir)).strip().upper()
    paths: list[Path] = []
    for interval in intervals:
        path = prepared_footprint_json_path(charts_dir, sym, interval)
        if path.is_file():
            paths.append(path)
    return paths


def prepared_footprint_openai_payloads(charts_dir: Path) -> list[ChartOpenAIPayload]:
    """Prepared spot footprint JSON (``footprint_{SYMBOL}_{iv}.json``) for OpenAI."""
    return [("json", path) for path in existing_prepared_footprint_json_paths(charts_dir)]


def persist_prepared_footprint_json_files(
    charts_dir: Path,
    *,
    chart_stamp: str | None = None,
    gocharting_cfg: dict | None = None,
) -> list[Path]:
    """
    Build and write ``footprint_{SYMBOL}_{iv}.json`` from raw WS + GC CSV + MT5 spot.

    Raises :class:`GcToSpotConversionError` when conversion cannot meet min_matched_ratio.
    """
    import json

    from automation_tool.gocharting_gc_spot_convert import (
        gc_to_spot_enabled,
        prepared_footprint_json_path,
    )
    from automation_tool.openai_prompt_flow import prepare_footprint_json_for_openai

    cfg = gocharting_cfg if gocharting_cfg is not None else _default_gocharting_cfg()
    if not gc_to_spot_enabled(cfg):
        return persist_openai_footprint_json_debug(
            charts_dir,
            stamp=chart_stamp or latest_chart_stamp(charts_dir) or "",
            chart_stamp=chart_stamp,
            gocharting_cfg=cfg,
        )

    sym = read_main_chart_symbol(charts_dir)
    if _footprint_ws_active(cfg):
        sources = existing_footprint_combined_json_paths(charts_dir)
    else:
        from automation_tool.gocharting_footprint_ocr import existing_footprint_bid_ask_json_paths

        sources = existing_footprint_bid_ask_json_paths(charts_dir)

    stamp_for_csv = (chart_stamp or latest_chart_stamp(charts_dir) or "").strip()
    written: list[Path] = []
    for src in sources:
        iv = ""
        if src.name.startswith("footprint_combined_"):
            iv = src.stem.replace("footprint_combined_", "")
        elif src.name.startswith("footprint_bid_ask_"):
            iv = src.stem.replace("footprint_bid_ask_", "")
        if not iv:
            continue
        try:
            raw = json.loads(src.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        prepared = prepare_footprint_json_for_openai(
            src,
            raw,
            chart_stamp=stamp_for_csv or None,
            gocharting_cfg=cfg,
            charts_dir=charts_dir,
        )
        dest = prepared_footprint_json_path(charts_dir, sym, iv)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(prepared, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(dest)
    return written


def extend_openai_payloads_with_footprint_bid_ask(
    payloads: list[ChartOpenAIPayload],
    charts_dir: Path,
) -> list[ChartOpenAIPayload]:
    extra = footprint_bid_ask_openai_payloads(charts_dir)
    if not extra:
        return payloads
    return [*payloads, *extra]


def extend_openai_payloads_with_footprint_json(
    payloads: list[ChartOpenAIPayload],
    charts_dir: Path,
    *,
    gocharting_cfg: dict | None = None,
) -> list[ChartOpenAIPayload]:
    """Append prepared or WS combined JSON when ``footprint_ws.enabled``."""
    from automation_tool.gocharting_gc_spot_convert import gc_to_spot_enabled

    cfg = gocharting_cfg if gocharting_cfg is not None else _default_gocharting_cfg()
    if _footprint_ws_active(cfg):
        if gc_to_spot_enabled(cfg):
            extra = prepared_footprint_openai_payloads(charts_dir)
        else:
            extra = footprint_combined_openai_payloads(charts_dir)
    else:
        extra = footprint_bid_ask_openai_payloads(charts_dir)
    if not extra:
        return payloads
    return [*payloads, *extra]


def _default_gocharting_cfg() -> dict:
    from automation_tool.config import default_gocharting_config_path
    from automation_tool.gocharting_capture import load_gocharting_yaml

    return load_gocharting_yaml(default_gocharting_config_path())


def _footprint_ws_active(gocharting_cfg: dict | None = None) -> bool:
    cfg = gocharting_cfg if gocharting_cfg is not None else _default_gocharting_cfg()
    ws = cfg.get("footprint_ws")
    if not isinstance(ws, dict):
        return False
    return bool(ws.get("enabled"))


def append_footprint_json_paths(
    paths: list[Path],
    charts_dir: Path,
    *,
    gocharting_cfg: dict | None = None,
) -> list[Path]:
    """Append prepared or WS combined footprint JSON paths when on disk."""
    from automation_tool.gocharting_gc_spot_convert import gc_to_spot_enabled

    cfg = gocharting_cfg if gocharting_cfg is not None else _default_gocharting_cfg()
    if _footprint_ws_active(cfg):
        if gc_to_spot_enabled(cfg):
            candidates = existing_prepared_footprint_json_paths(charts_dir)
        else:
            candidates = existing_footprint_combined_json_paths(charts_dir)
    else:
        from automation_tool.gocharting_footprint_ocr import existing_footprint_bid_ask_json_paths

        candidates = existing_footprint_bid_ask_json_paths(charts_dir)
    for path in candidates:
        if path not in paths:
            paths.append(path)
    return paths


def openai_footprint_debug_json_path(charts_dir: Path, stamp: str, source_stem: str) -> Path:
    """Enriched GoCharting footprint snapshot for debug under ``charts_dir``."""
    return charts_dir / f"{stamp}_{source_stem}.json"


def persist_openai_footprint_json_debug(
    charts_dir: Path,
    *,
    stamp: str,
    chart_stamp: str | None = None,
    gocharting_cfg: dict | None = None,
) -> list[Path]:
    """
    Write footprint JSON after OpenAI prep (trim/aggregate/enrich) to ``charts_dir``.

    Source files live under ``footprint_images/``; output is ``{stamp}_footprint_*_{iv}.json``
    in ``charts_dir`` for debugging ``all`` / ``update-scalp`` runs.
    """
    import json

    from automation_tool.openai_prompt_flow import prepare_footprint_json_for_openai

    cfg = gocharting_cfg if gocharting_cfg is not None else _default_gocharting_cfg()
    if _footprint_ws_active(cfg):
        sources = existing_footprint_combined_json_paths(charts_dir)
    else:
        from automation_tool.gocharting_footprint_ocr import existing_footprint_bid_ask_json_paths

        sources = existing_footprint_bid_ask_json_paths(charts_dir)
    stamp_for_csv = (chart_stamp or stamp).strip()
    written: list[Path] = []
    for src in sources:
        try:
            raw = json.loads(src.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        prepared = prepare_footprint_json_for_openai(
            src,
            raw,
            chart_stamp=stamp_for_csv or None,
            gocharting_cfg=cfg,
        )
        dest = openai_footprint_debug_json_path(charts_dir, stamp, src.stem)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(prepared, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(dest)
    return written


def normalize_main_chart_symbol(s: str) -> str:
    """Uppercase forex/crypto pair id for filenames (watchlist id on Coinmap / TV label)."""
    t = (s or "").strip().upper()
    if not re.match(r"^[A-Z0-9]{4,16}$", t):
        raise ValueError(
            f"main symbol must be 4-16 letters/digits (e.g. XAUUSD, USDJPY), got {s!r}"
        )
    return t


def get_active_main_symbol() -> str:
    """
    Active instrument for ``data/{{SYM}}/`` layout.

    1. ``AUTOMATION_MAIN_SYMBOL`` env
    2. ``data/.main_chart_symbol`` (written by capture / set_active_main_symbol_file)
    3. Legacy ``data/charts/.main_chart_symbol`` (pre per-symbol dirs)
    4. ``DEFAULT_MAIN_CHART_SYMBOL``
    """
    import os

    from automation_tool.config import default_data_dir

    env = (os.getenv("AUTOMATION_MAIN_SYMBOL") or "").strip()
    if env:
        try:
            return normalize_main_chart_symbol(env)
        except ValueError:
            pass

    root = default_data_dir()
    for rel in (GLOBAL_MAIN_CHART_SYMBOL_FILENAME,):
        marker = root / rel
        if marker.is_file():
            try:
                line = marker.read_text(encoding="utf-8").strip().splitlines()
                raw = line[0] if line else ""
                if raw:
                    return normalize_main_chart_symbol(raw)
            except (OSError, UnicodeError, ValueError):
                pass

    legacy = root / "charts" / MAIN_CHART_SYMBOL_FILENAME
    if legacy.is_file():
        try:
            line = legacy.read_text(encoding="utf-8").strip().splitlines()
            raw = line[0] if line else ""
            if raw:
                return normalize_main_chart_symbol(raw)
        except (OSError, UnicodeError, ValueError):
            pass

    return DEFAULT_MAIN_CHART_SYMBOL


def set_active_main_symbol_file(main_chart_symbol: Optional[str]) -> None:
    """
    Global pointer ``data/.main_chart_symbol`` so ``default_charts_dir()`` resolves to
    ``data/{{SYM}}/charts/``. Pass ``None`` to remove (active symbol defaults to XAUUSD).
    """
    from automation_tool.config import default_data_dir

    root = default_data_dir()
    marker = root / GLOBAL_MAIN_CHART_SYMBOL_FILENAME
    if main_chart_symbol is not None and str(main_chart_symbol).strip():
        sym = normalize_main_chart_symbol(main_chart_symbol)
        root.mkdir(parents=True, exist_ok=True)
        marker.write_text(sym + "\n", encoding="utf-8")
        try:
            from automation_tool.browser_client import try_tv_prewarm_reset

            try_tv_prewarm_reset(sym)
        except Exception:
            pass
    else:
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def chart_image_order_for_main_symbol(
    main_sym: str,
    *,
    footprint_source: str = "coinmap",
) -> tuple[tuple[str, str, str], ...]:
    """
    Filenames: ``{{stamp}}_tradingview_{{SYMBOL}}_{{interval}}`` or footprint ``coinmap`` / ``gocharting``.

    **11 slots (default full-analysis set):** DXY TV H4/H1/M15 → main TV H4/H1/M15
    → main TV M15 Session Liquidity Check / ICT Killzones → main TV M5
    → footprint DXY M15 → main M15/M5 (Coinmap JSON or GoCharting CSV).
    """
    m = normalize_main_chart_symbol(main_sym)
    fp = (footprint_source or "coinmap").strip().lower()
    if fp not in ("coinmap", "gocharting"):
        fp = "coinmap"
    footprint_sym = gocharting_footprint_export_label(m) if fp == "gocharting" else m
    return (
        ("tradingview", "DXY", "4h"),
        ("tradingview", "DXY", "1h"),
        ("tradingview", "DXY", "15m"),
        ("tradingview", m, "4h"),
        ("tradingview", m, "1h"),
        ("tradingview", m, "15m"),
        ("tradingview", m, "15m_ict"),
        ("tradingview", m, "5m"),
        (fp, "DXY", "15m"),
        (fp, footprint_sym, "15m"),
        (fp, footprint_sym, "5m"),
    )


# Backward compat: default order equals XAUUSD main pair.
CHART_IMAGE_ORDER: tuple[tuple[str, str, str], ...] = chart_image_order_for_main_symbol(
    DEFAULT_MAIN_CHART_SYMBOL
)

# Number of multimodal slots (must match ``chart_image_order_for_main_symbol`` length).
CHART_SLOT_COUNT = len(CHART_IMAGE_ORDER)


# Source detail PNGs per GC slot (zoom + 3 back); OpenAI gets 3× width panels each.
GOCHARTING_DETAIL_SOURCE_PNG_PER_SLOT = 4
GOCHARTING_DETAIL_PNG_PER_SLOT = (
    GOCHARTING_DETAIL_SOURCE_PNG_PER_SLOT * 3
)  # detail_zoom + back_* → part1..3 panels


def _detail_back_step_from_stem(stem: str) -> Optional[int]:
    m = re.search(r"_detail_back_(\d+)$", stem)
    if not m:
        return None
    return int(m.group(1))


def _gocharting_detail_png_per_slot(
    sym: str,
    *,
    gocharting_cfg: dict | None = None,
    gocharting_detail_max_back_steps: int | None = None,
) -> int:
    """DXY GoCharting uses overview only (no detail footprint tab)."""
    if sym.strip().upper() == "DXY":
        return 0
    if _footprint_ws_active(gocharting_cfg):
        if gocharting_detail_max_back_steps is None or gocharting_detail_max_back_steps <= 0:
            return 0
        source_count = 1 + int(gocharting_detail_max_back_steps)
        return source_count * 3
    return GOCHARTING_DETAIL_PNG_PER_SLOT


def openai_payload_max_for_order(
    order: tuple[tuple[str, str, str], ...],
    *,
    gocharting_cfg: dict | None = None,
    gocharting_detail_max_back_steps: int | None = None,
) -> int:
    """Upper bound when each footprint slot sends data file + PNG alongside other slot payloads."""
    return len(order) + sum(
        (
            1
            + _gocharting_detail_png_per_slot(
                sym,
                gocharting_cfg=gocharting_cfg,
                gocharting_detail_max_back_steps=gocharting_detail_max_back_steps,
            )
            if src == "gocharting"
            else 1
            if src == "coinmap"
            else 0
        )
        for src, sym, _ in order
    )


# Default ``--max-images-per-call`` (one API call; fits GoCharting full capture with detail crops).
OPENAI_PAYLOAD_MAX = 100


def read_main_chart_symbol(charts_dir: Optional[Path] = None) -> str:
    """
    Main pair for filename slots.

    If ``charts_dir`` is set: read that directory's ``.main_chart_symbol`` if present,
    else ``DEFAULT_MAIN_CHART_SYMBOL`` (no mixing with global ``data/.main_chart_symbol``).

    If ``charts_dir`` is ``None``: :func:`get_active_main_symbol`.
    """
    if charts_dir is not None:
        marker = charts_dir / MAIN_CHART_SYMBOL_FILENAME
        if marker.is_file():
            try:
                line = marker.read_text(encoding="utf-8").strip().splitlines()
                raw = line[0] if line else ""
                if raw:
                    return normalize_main_chart_symbol(raw)
            except (OSError, UnicodeError, ValueError):
                pass
        return DEFAULT_MAIN_CHART_SYMBOL
    return get_active_main_symbol()


def write_main_chart_symbol_marker(charts_dir: Path, symbol: str) -> None:
    """Persist main pair so OpenAI ordering matches captured filenames."""
    sym = normalize_main_chart_symbol(symbol)
    charts_dir.mkdir(parents=True, exist_ok=True)
    (charts_dir / MAIN_CHART_SYMBOL_FILENAME).write_text(sym + "\n", encoding="utf-8")


def clear_main_chart_symbol_marker(charts_dir: Path) -> None:
    """Remove marker so consumers use default XAUUSD (yaml default capture)."""
    p = charts_dir / MAIN_CHART_SYMBOL_FILENAME
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def footprint_source_for_stamp(charts_dir: Path, stamp: Optional[str] = None) -> str:
    """``gocharting`` when stamp has GoCharting CSV exports; else ``coinmap``."""
    if not charts_dir.is_dir():
        return "gocharting"
    st = stamp or latest_chart_stamp(charts_dir)
    if not st:
        return "gocharting"
    if any(charts_dir.glob(f"{st}_gocharting_*.csv")):
        return "gocharting"
    return "coinmap"


def effective_chart_image_order(
    charts_dir: Path, *, stamp: Optional[str] = None
) -> tuple[tuple[str, str, str], ...]:
    main_sym = read_main_chart_symbol(charts_dir)
    fp = footprint_source_for_stamp(charts_dir, stamp=stamp)
    return chart_image_order_for_main_symbol(main_sym, footprint_source=fp)


_STAMP_RE = re.compile(r"^(\d{8}_\d{6})_(?:tradingview|coinmap|gocharting|mt5)_")


def latest_chart_stamp(charts_dir: Path) -> Optional[str]:
    """Latest ``YYYYMMDD_HHMMSS`` prefix shared by tradingview/coinmap shots in ``charts_dir``."""
    if not charts_dir.is_dir():
        return None
    stamps: set[str] = set()
    for p in charts_dir.glob("*.png"):
        m = _STAMP_RE.match(p.name)
        if m:
            stamps.add(m.group(1))
    for p in charts_dir.glob("*.json"):
        m = _STAMP_RE.match(p.name)
        if m:
            stamps.add(m.group(1))
    for p in charts_dir.glob("*.url"):
        m = _STAMP_RE.match(p.name)
        if m:
            stamps.add(m.group(1))
    for p in charts_dir.glob("*.csv"):
        m = _STAMP_RE.match(p.name)
        if m:
            stamps.add(m.group(1))
    return max(stamps) if stamps else None


def stamp_from_capture_paths(paths: Sequence[Path]) -> Optional[str]:
    """Largest ``YYYYMMDD_HHMMSS`` prefix found on capture artifact filenames (e.g. returned by ``capture_charts``)."""
    stamps: set[str] = set()
    for p in paths:
        m = _STAMP_RE.match(p.name)
        if m:
            stamps.add(m.group(1))
    return max(stamps) if stamps else None


def gocharting_interval_csv_path(
    charts_dir: Path, symbol: str, interval: str, *, stamp: Optional[str] = None
) -> Optional[Path]:
    """``{{stamp}}_gocharting_{{symbol}}_{interval}.csv`` if on disk."""
    sym = (symbol or "").strip().upper()
    iv = (interval or "").strip()
    if not sym or not iv:
        return None
    st = stamp or latest_chart_stamp(charts_dir)
    if not st:
        return None
    iv_slug = re.sub(r"[^\w]+", "_", iv).strip("_")[:20] or "iv"
    p = charts_dir / f"{st}_gocharting_{sym}_{iv_slug}.csv"
    return p if p.is_file() else None


def gocharting_main_interval_csv_path(
    charts_dir: Path, interval: str, *, stamp: Optional[str] = None
) -> Optional[Path]:
    """Latest GoCharting CSV for main pair and interval (e.g. ``5m``, ``15m``)."""
    sym = gocharting_footprint_export_label(read_main_chart_symbol(charts_dir))
    return gocharting_interval_csv_path(charts_dir, sym, interval, stamp=stamp)


def gocharting_png_path_for_csv(csv_path: Path) -> Optional[Path]:
    """Sibling PNG for a GoCharting CSV export, if on disk."""
    if "_gocharting_" not in csv_path.name or csv_path.suffix.lower() != ".csv":
        return None
    pp = csv_path.with_suffix(".png")
    return pp if pp.is_file() else None


def gocharting_detail_zoom_png_path_for_csv(csv_path: Path) -> Optional[Path]:
    """``{stem}_detail_zoom.png`` sibling for a GoCharting CSV export, if on disk."""
    if "_gocharting_" not in csv_path.name or csv_path.suffix.lower() != ".csv":
        return None
    zoom = csv_path.parent / f"{csv_path.stem}_detail_zoom.png"
    return zoom if zoom.is_file() else None


def _gocharting_slot_parts_from_csv(csv_path: Path) -> Optional[tuple[Path, str, str, str]]:
    """Parse ``{stamp}_gocharting_{sym}_{iv}.csv`` → charts dir, stamp, sym, interval slug."""
    if "_gocharting_" not in csv_path.name or csv_path.suffix.lower() != ".csv":
        return None
    m = re.match(r"^(.+)_gocharting_(.+?)_(.+)\.csv$", csv_path.name, re.I)
    if not m:
        return None
    return csv_path.parent, m.group(1), m.group(2), m.group(3)


def gocharting_detail_png_paths_for_csv(csv_path: Path) -> list[Path]:
    """``detail_zoom`` then ``detail_back_*`` siblings for a GoCharting CSV export, if on disk."""
    slot = _gocharting_slot_parts_from_csv(csv_path)
    if slot is None:
        return []
    charts_dir, stamp, sym, iv = slot
    return gocharting_detail_png_paths(charts_dir, stamp, sym, iv)


def coinmap_main_pair_interval_json_path(
    charts_dir: Path, interval: str, *, stamp: Optional[str] = None
) -> Optional[Path]:
    """``{{stamp}}_coinmap_{{main_pair}}_{interval}.json`` (main pair from marker; ``interval`` e.g. ``5m``, ``15m``)."""
    sym = read_main_chart_symbol(charts_dir)
    iv = (interval or "").strip()
    if not iv:
        return None
    st = stamp or latest_chart_stamp(charts_dir)
    if not st:
        return None
    p = charts_dir / f"{st}_coinmap_{sym}_{iv}.json"
    return p if p.is_file() else None


def coinmap_main_pair_5m_json_path(
    charts_dir: Path, *, stamp: Optional[str] = None
) -> Optional[Path]:
    """Latest ``{{stamp}}_coinmap_{{main_pair}}_5m.json`` (main pair from marker or XAUUSD)."""
    return coinmap_main_pair_interval_json_path(charts_dir, "5m", stamp=stamp)


def coinmap_xauusd_5m_json_path(
    charts_dir: Path, *, stamp: Optional[str] = None
) -> Optional[Path]:
    """Backward compat: same as ``coinmap_main_pair_5m_json_path``."""
    return coinmap_main_pair_5m_json_path(charts_dir, stamp=stamp)


def coinmap_merged_openai_files(
    charts_dir: Path, stamp: str, main_sym: str
) -> tuple[Optional[Path], Optional[Path]]:
    """
    If present, paths to DXY and main-pair ``*_coinmap_*_merged.json`` (``coinmap_merged``).
    """
    m = (main_sym or "").strip() or DEFAULT_MAIN_CHART_SYMBOL
    dxy = charts_dir / f"{stamp}_coinmap_DXY_merged.json"
    mainp = charts_dir / f"{stamp}_coinmap_{m}_merged.json"
    d_ok = dxy if dxy.is_file() else None
    m_ok = mainp if mainp.is_file() else None
    return d_ok, m_ok


def gocharting_detail_png_paths(
    charts_dir: Path, stamp: str, sym: str, iv: str, *, max_back_steps: int | None = None
) -> list[Path]:
    """``detail_zoom`` then ``detail_back_*`` PNGs for one GoCharting slot, if on disk."""
    iv_slug = re.sub(r"[^\w]+", "_", iv).strip("_")[:20] or "iv"
    pattern = f"{stamp}_gocharting_{sym}_{iv_slug}_detail_*.png"
    paths = [p for p in charts_dir.glob(pattern) if p.is_file()]

    def _sort_key(p: Path) -> tuple[int, int | str]:
        stem = p.stem
        if stem.endswith("_detail_zoom"):
            return (0, 0)
        back_step = _detail_back_step_from_stem(stem)
        if back_step is not None:
            return (1, back_step)
        return (2, stem)

    ordered = sorted(paths, key=_sort_key)
    if max_back_steps is None:
        return ordered
    filtered: list[Path] = []
    for p in ordered:
        stem = p.stem
        if stem.endswith("_detail_zoom"):
            filtered.append(p)
            continue
        back_step = _detail_back_step_from_stem(stem)
        if back_step is not None and back_step <= max_back_steps:
            filtered.append(p)
    return filtered


def gocharting_detail_openai_png_paths(
    charts_dir: Path,
    stamp: str,
    sym: str,
    iv: str,
    *,
    crop_width_thirds: bool | None = None,
    max_back_steps: int | None = None,
) -> list[Path]:
    """Detail footprint crop panels for one GoCharting slot (for OpenAI payloads)."""
    if crop_width_thirds is None:
        crop_width_thirds = resolve_gocharting_detail_crop_width_thirds()
    out: list[Path] = []
    for dp in gocharting_detail_png_paths(
        charts_dir, stamp, sym, iv, max_back_steps=max_back_steps
    ):
        out.extend(
            gocharting_detail_openai_image_paths(dp, crop_width_thirds=crop_width_thirds)
        )
    return out


def gocharting_detail_openai_png_paths_for_csv(
    csv_path: Path,
    *,
    crop_width_thirds: bool | None = None,
    zoom_only: bool = False,
    max_back_steps: int | None = None,
) -> list[Path]:
    """Crop panels for detail PNG siblings of a GoCharting CSV export."""
    if crop_width_thirds is None:
        crop_width_thirds = resolve_gocharting_detail_crop_width_thirds()
    if zoom_only:
        zoom = gocharting_detail_zoom_png_path_for_csv(csv_path)
        detail_paths = [zoom] if zoom is not None else []
    else:
        slot = _gocharting_slot_parts_from_csv(csv_path)
        if slot is None:
            detail_paths = []
        else:
            charts_dir, stamp, sym, iv = slot
            detail_paths = gocharting_detail_png_paths(
                charts_dir, stamp, sym, iv, max_back_steps=max_back_steps
            )
    out: list[Path] = []
    for dp in detail_paths:
        out.extend(
            gocharting_detail_openai_image_paths(dp, crop_width_thirds=crop_width_thirds)
        )
    return out


def _append_gocharting_openai_payloads(
    out: list[ChartOpenAIPayload],
    *,
    charts_dir: Path,
    stamp: str,
    sym: str,
    iv: str,
    crop_width_thirds: bool | None = None,
    gocharting_cfg: dict | None = None,
    gocharting_detail_max_back_steps: int | None = None,
) -> None:
    from automation_tool.gocharting_gc_spot_convert import gc_to_spot_enabled

    cfg = gocharting_cfg if gocharting_cfg is not None else _default_gocharting_cfg()
    if sym.upper() == GOCHARTING_GOLD_EXPORT_LABEL and gc_to_spot_enabled(cfg):
        return

    iv_slug = re.sub(r"[^\w]+", "_", iv).strip("_")[:20] or "iv"
    cp = charts_dir / f"{stamp}_gocharting_{sym}_{iv_slug}.csv"
    pp = charts_dir / f"{stamp}_gocharting_{sym}_{iv_slug}.png"
    if cp.is_file():
        out.append(("csv", cp))
    if pp.is_file():
        out.append(("image", pp))
    ws_active = _footprint_ws_active(gocharting_cfg)
    if ws_active and gocharting_detail_max_back_steps is None:
        return
    max_back = gocharting_detail_max_back_steps if ws_active else None
    for dp in gocharting_detail_openai_png_paths(
        charts_dir,
        stamp,
        sym,
        iv,
        crop_width_thirds=crop_width_thirds,
        max_back_steps=max_back,
    ):
        out.append(("image", dp))


def _append_coinmap_openai_payloads(
    out: list[ChartOpenAIPayload],
    *,
    charts_dir: Path,
    stamp: str,
    sym: str,
    iv: str,
    json_path: Optional[Path] = None,
) -> None:
    """Append Coinmap JSON and/or PNG for one slot (both when present)."""
    jp = (
        json_path
        if json_path is not None
        else charts_dir / f"{stamp}_coinmap_{sym}_{iv}.json"
    )
    pp = charts_dir / f"{stamp}_coinmap_{sym}_{iv}.png"
    if jp.is_file():
        out.append(("json", jp))
    if pp.is_file():
        out.append(("image", pp))


_COINMAP_MERGED_JSON_RE = re.compile(
    r"^(?P<stamp>\d{8}_\d{6})_coinmap_(?P<sym>[^_]+)_merged\.json$"
)


def coinmap_png_path_for_json(json_path: Path) -> Optional[Path]:
    """Sibling fullscreen PNG for a per-interval Coinmap JSON export, if on disk."""
    if "_coinmap_" not in json_path.name or json_path.suffix.lower() != ".json":
        return None
    if json_path.name.endswith("_merged.json"):
        return None
    pp = json_path.with_suffix(".png")
    return pp if pp.is_file() else None


def _coinmap_merged_interval_png_paths(json_path: Path) -> list[Path]:
    """M15/M5 PNG paths for a ``*_coinmap_*_merged.json`` attachment (same as ``all`` slot order)."""
    m = _COINMAP_MERGED_JSON_RE.match(json_path.name)
    if not m:
        return []
    charts_dir = json_path.parent
    stamp = m.group("stamp")
    sym = m.group("sym")
    out: list[Path] = []
    for iv in ("15m", "5m"):
        pp = charts_dir / f"{stamp}_coinmap_{sym}_{iv}.png"
        if pp.is_file():
            out.append(pp)
    return out


def openai_payloads_for_attachment_paths(
    paths: Sequence[Path],
    *,
    crop_width_thirds: bool | None = None,
    gocharting_cfg: dict | None = None,
    gocharting_detail_zoom_only: bool = False,
    gocharting_detail_max_back_steps: int | None = None,
) -> list[ChartOpenAIPayload]:
    """
    Build OpenAI payloads from JSON/CSV attachment paths; for each Coinmap JSON, append
    sibling PNG immediately after (per-interval or M15+M5 for merged). For GoCharting CSV,
    append overview PNG then detail-zoom PNGs unless ``footprint_ws.enabled`` (unless
    ``gocharting_detail_max_back_steps`` is set while WS is active).
    """
    ws_active = _footprint_ws_active(gocharting_cfg)
    out: list[ChartOpenAIPayload] = []
    cfg = gocharting_cfg if gocharting_cfg is not None else _default_gocharting_cfg()
    from automation_tool.gocharting_gc_spot_convert import gc_to_spot_enabled, is_gocharting_main_pair_path

    for p in paths:
        if p.suffix.lower() == ".csv":
            if gc_to_spot_enabled(cfg) and is_gocharting_main_pair_path(p):
                continue
            out.append(("csv", p))
            pp = gocharting_png_path_for_csv(p)
            if pp is not None:
                out.append(("image", pp))
            if not ws_active:
                for dp in gocharting_detail_openai_png_paths_for_csv(
                    p,
                    crop_width_thirds=crop_width_thirds,
                    zoom_only=gocharting_detail_zoom_only,
                ):
                    out.append(("image", dp))
            elif (
                gocharting_detail_max_back_steps is not None
                and gocharting_detail_max_back_steps > 0
            ):
                for dp in gocharting_detail_openai_png_paths_for_csv(
                    p,
                    crop_width_thirds=crop_width_thirds,
                    max_back_steps=gocharting_detail_max_back_steps,
                ):
                    out.append(("image", dp))
            continue
        out.append(("json", p))
        if "_coinmap_" not in p.name or p.suffix.lower() != ".json":
            continue
        if p.name.endswith("_merged.json"):
            for pp in _coinmap_merged_interval_png_paths(p):
                out.append(("image", pp))
            continue
        pp = coinmap_png_path_for_json(p)
        if pp is not None:
            out.append(("image", pp))
    return out


def ordered_chart_openai_payloads(
    charts_dir: Path,
    *,
    stamp: Optional[str] = None,
    gocharting_cfg: dict | None = None,
    gocharting_detail_max_back_steps: int | None = None,
) -> list[ChartOpenAIPayload]:
    """
    Same slot order as ``effective_chart_image_order(charts_dir)`` (for OpenAI step 2).

    * **TradingView** — prefer ``.json`` (tvdatafeed OHLC) else ``.url`` (snapshot) else ``.png``.
    * **Coinmap** — attach ``.json`` (API export) when present, and **also** ``.png`` when present
      (JSON-only still works when screenshots are disabled).
    * When **merged** files exist (see :func:`coinmap_merged_openai_files`), DXY 15m uses
      ``DXY_merged.json``; main M15 + M5 collapse to a single ``{MAIN}_merged.json`` attachment
      (merged main M5 JSON slot skipped; PNG for M5 still attached when on disk).
    """
    if not charts_dir.is_dir():
        return []
    st = stamp or latest_chart_stamp(charts_dir)
    if not st:
        return []
    crop_width_thirds = resolve_gocharting_detail_crop_width_thirds(gocharting_cfg)
    main_sym = read_main_chart_symbol(charts_dir)
    dxy_merged, main_merged = coinmap_merged_openai_files(charts_dir, st, main_sym)
    order = effective_chart_image_order(charts_dir)
    out: list[ChartOpenAIPayload] = []
    for src, sym, iv in order:
        if src == "coinmap":
            if dxy_merged is not None and sym == "DXY" and iv == "15m":
                _append_coinmap_openai_payloads(
                    out,
                    charts_dir=charts_dir,
                    stamp=st,
                    sym=sym,
                    iv=iv,
                    json_path=dxy_merged,
                )
                continue
            if main_merged is not None and sym == main_sym and iv == "15m":
                _append_coinmap_openai_payloads(
                    out,
                    charts_dir=charts_dir,
                    stamp=st,
                    sym=sym,
                    iv=iv,
                    json_path=main_merged,
                )
                continue
            if main_merged is not None and sym == main_sym and iv == "5m":
                _append_coinmap_openai_payloads(
                    out, charts_dir=charts_dir, stamp=st, sym=sym, iv=iv, json_path=None
                )
                continue
            _append_coinmap_openai_payloads(
                out, charts_dir=charts_dir, stamp=st, sym=sym, iv=iv
            )
        elif src == "gocharting":
            _append_gocharting_openai_payloads(
                out,
                charts_dir=charts_dir,
                stamp=st,
                sym=sym,
                iv=iv,
                crop_width_thirds=crop_width_thirds,
                gocharting_cfg=gocharting_cfg,
                gocharting_detail_max_back_steps=gocharting_detail_max_back_steps,
            )
        else:
            jp = charts_dir / f"{st}_tradingview_{sym}_{iv}.json"
            up = charts_dir / f"{st}_tradingview_{sym}_{iv}.url"
            pp = charts_dir / f"{st}_tradingview_{sym}_{iv}.png"
            if jp.is_file():
                out.append(("json", jp))
            elif up.is_file():
                raw = up.read_text(encoding="utf-8").strip().splitlines()
                line = (raw[0] if raw else "").strip()
                if line.startswith("http://") or line.startswith("https://"):
                    out.append(("image_url", line))
                elif pp.is_file():
                    out.append(("image", pp))
            elif pp.is_file():
                out.append(("image", pp))
    _append_gocharting_mt5_spot_payload(out, charts_dir=charts_dir, stamp=st, gocharting_cfg=gocharting_cfg)
    return out


def mt5_spot_candles_json_path_for_stamp(
    charts_dir: Path,
    *,
    stamp: Optional[str] = None,
    logic_symbol: Optional[str] = None,
    interval: Optional[str] = None,
) -> Optional[Path]:
    """``{stamp}_mt5_{SYMBOL}_{interval}.json`` when on disk (GoCharting supplement)."""
    from automation_tool.mt5_candles import mt5_spot_candles_json_path

    st = stamp or latest_chart_stamp(charts_dir)
    if not st:
        return None
    sym = (logic_symbol or read_main_chart_symbol(charts_dir)).strip().upper()
    p = mt5_spot_candles_json_path(
        charts_dir,
        logic_symbol=sym,
        interval=interval,
        stamp=st,
    )
    return p if p.is_file() else None


def _append_gocharting_mt5_spot_payload(
    out: list[ChartOpenAIPayload],
    *,
    charts_dir: Path,
    stamp: str,
    gocharting_cfg: dict | None = None,
) -> None:
    """After GoCharting footprint slots, attach MT5 spot OHLC JSON when present (legacy)."""
    if footprint_source_for_stamp(charts_dir, stamp=stamp) != "gocharting":
        return
    if _footprint_ws_active(gocharting_cfg):
        return
    p = mt5_spot_candles_json_path_for_stamp(charts_dir, stamp=stamp)
    if p is not None:
        out.append(("json", p))


def ordered_chart_images(
    charts_dir: Path,
    *,
    stamp: Optional[str] = None,
    gocharting_cfg: dict | None = None,
    gocharting_detail_max_back_steps: int | None = None,
) -> list[Path]:
    """
    Return chart PNG paths in analysis order (same slot order as OpenAI payloads).

    For GoCharting footprint slots, append ``detail_zoom`` and ``detail_back_*`` PNGs after each
    overview PNG when present. Only includes files that exist. Uses latest stamp when omitted.
    """
    if not charts_dir.is_dir():
        return []
    st = stamp or latest_chart_stamp(charts_dir)
    if not st:
        return []
    crop_width_thirds = resolve_gocharting_detail_crop_width_thirds(gocharting_cfg)
    order = effective_chart_image_order(charts_dir)
    out: list[Path] = []
    for src, sym, iv in order:
        p = charts_dir / f"{st}_{src}_{sym}_{iv}.png"
        if p.is_file():
            out.append(p)
        if src == "gocharting":
            ws_active = _footprint_ws_active(gocharting_cfg)
            if ws_active and gocharting_detail_max_back_steps is None:
                continue
            max_back = gocharting_detail_max_back_steps if ws_active else None
            out.extend(
                gocharting_detail_openai_png_paths(
                    charts_dir,
                    st,
                    sym,
                    iv,
                    crop_width_thirds=crop_width_thirds,
                    max_back_steps=max_back,
                )
            )
    return out


def image_to_data_url(path: Path) -> str:
    raw = path.read_bytes()
    mime, _ = mimetypes.guess_type(str(path))
    if not mime:
        mime = "image/png"
    b64 = base64.standard_b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def list_chart_images(charts_dir: Path, patterns: tuple[str, ...] = ("*.png", "*.jpg", "*.jpeg", "*.webp")) -> list[Path]:
    if not charts_dir.is_dir():
        return []
    out: list[Path] = []
    for pat in patterns:
        out.extend(sorted(charts_dir.glob(pat)))
    return sorted(set(out), key=lambda p: p.name)


def chunk_image_paths(paths: list[Path], max_per_chunk: int) -> list[list[Path]]:
    if max_per_chunk <= 0:
        return [paths]
    return [paths[i : i + max_per_chunk] for i in range(0, len(paths), max_per_chunk)]


def chunk_payloads(
    payloads: list[ChartOpenAIPayload], max_per_chunk: int
) -> list[list[ChartOpenAIPayload]]:
    if max_per_chunk <= 0:
        return [payloads]
    return [payloads[i : i + max_per_chunk] for i in range(0, len(payloads), max_per_chunk)]


_FOOTPRINT_PAYLOAD_MARKERS = (
    "_gocharting_",
    "_coinmap_",
    "footprint_combined_",
    "footprint_bid_ask_",
    "footprint_xauusd_",
    "footprint_",
    "_mt5_",
)


def _is_gocharting_dxy_path(name: str) -> bool:
    return "_gocharting_dxy_" in name.lower()


def _is_gocharting_dxy_overview_png(kind: str, path: Path) -> bool:
    """Overview PNG only (no CSV, no detail_* panels) — sent in batch 1."""
    if kind != "image" or path.suffix.lower() != ".png":
        return False
    lower = path.name.lower()
    return "_gocharting_dxy_" in lower and "_detail_" not in lower


def _payload_phase(name: str) -> str | None:
    """``tradingview`` | ``footprint`` | None if unclassified."""
    lower = name.lower()
    if "_tradingview_" in lower:
        return "tradingview"
    if any(m in lower for m in _FOOTPRINT_PAYLOAD_MARKERS):
        return "footprint"
    return None


def split_openai_payloads_by_phase(
    payloads: list[ChartOpenAIPayload],
) -> tuple[list[ChartOpenAIPayload], list[ChartOpenAIPayload]]:
    """
    Split full-analysis payloads for 2-batch chained flow.

    Batch 1 (structure): TradingView + GoCharting DXY M15 overview PNG only.
    Batch 2 (footprint): GC/Coinmap footprint — excludes all GoCharting DXY CSV/PNG.
    """
    import logging

    log = logging.getLogger(__name__)
    structure: list[ChartOpenAIPayload] = []
    footprint: list[ChartOpenAIPayload] = []
    for kind, p in payloads:
        if kind == "image_url":
            structure.append((kind, p))
            continue
        if not isinstance(p, Path):
            log.warning("split_openai_payloads_by_phase: skip unclassified non-path payload kind=%s", kind)
            continue
        name = p.name
        if _is_gocharting_dxy_path(name):
            if _is_gocharting_dxy_overview_png(kind, p):
                structure.append((kind, p))
            else:
                log.debug(
                    "split_openai_payloads_by_phase: skip GoCharting DXY %s (batch 2 excludes DXY)",
                    name,
                )
            continue
        phase = _payload_phase(name)
        if phase == "tradingview":
            structure.append((kind, p))
        elif phase == "footprint":
            footprint.append((kind, p))
        else:
            log.warning(
                "split_openai_payloads_by_phase: skip unclassified file %s (kind=%s)",
                name,
                kind,
            )
    return structure, footprint
