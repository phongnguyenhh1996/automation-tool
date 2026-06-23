from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from openai import OpenAI

from automation_tool.gocharting_capture import (
    gocharting_detail_crop_width_thirds,
    load_gocharting_yaml,
)
from automation_tool.gocharting_image_crop import (
    GOCHARTING_IMAGE_WIDTH_THIRDS,
    gocharting_detail_openai_image_paths,
)
from automation_tool.images import (
    gocharting_detail_png_paths,
    gocharting_footprint_export_label,
    image_to_data_url,
    normalize_main_chart_symbol,
)
from automation_tool.openai_analysis_json import extract_json_object
from automation_tool.prompts import responses_input_messages

_log = logging.getLogger(__name__)

DEFAULT_FOOTPRINT_EXTRACT_MODEL = "gpt-5.4"
DEFAULT_FOOTPRINT_EXTRACT_REASONING_EFFORT = "medium"
FOOTPRINT_EXTRACT_INTERVALS = ("5m", "15m")
FOOTPRINT_CHART_TYPE = "Bid/Ask Footprint"

_INTERVAL_FILENAME_RE = re.compile(r"^(\d+)m$", re.I)
_DETAIL_BACK_RE = re.compile(r"_detail_back_(\d+)$")


def _detail_png_source_label(path: Path) -> str:
    stem = path.stem
    if stem.endswith("_detail_zoom"):
        return "detail zoom"
    m = _DETAIL_BACK_RE.search(stem)
    if m:
        return f"back {m.group(1)}"
    if "_detail_" in stem:
        return stem.rsplit("_detail_", 1)[-1].replace("_", " ")
    return stem


def interval_footprint_filename_slug(interval: str) -> str:
    """Map capture interval to output filename prefix (``5m`` → ``m5``, ``15m`` → ``m15``)."""
    iv = (interval or "").strip().lower()
    m = _INTERVAL_FILENAME_RE.match(iv)
    if m:
        return f"m{m.group(1)}"
    return iv.replace("/", "_") or "iv"


def resolve_instrument_slug(cfg: dict[str, Any], main_symbol: str) -> str:
    """Instrument slug for output filenames (e.g. ``GC1!`` from ``search_query``)."""
    sym = normalize_main_chart_symbol(main_symbol)
    symbols = cfg.get("symbols") or {}
    block = symbols.get(sym) if isinstance(symbols, dict) else None
    if isinstance(block, dict):
        q = (block.get("search_query") or "").strip()
        if q:
            return q
        label = (block.get("export_label") or "").strip()
        if label:
            return label
    return gocharting_footprint_export_label(sym)


FOOTPRINT_EXTRACT_SYSTEM_PROMPT = (
    "You extract structured JSON from GoCharting Bid/Ask Footprint chart screenshots. "
    "Return only a valid JSON object matching the requested schema. "
    "Do not include markdown, commentary, or analysis."
)


def resolve_gocharting_chart_info(
    cfg: dict[str, Any],
    main_symbol: str,
    interval: str,
) -> dict[str, str]:
    """Build ``chart_info`` for extracted JSON."""
    sym = normalize_main_chart_symbol(main_symbol)
    instrument_slug = resolve_instrument_slug(cfg, sym)
    symbols = cfg.get("symbols") or {}
    block = symbols.get(sym) if isinstance(symbols, dict) else None
    display_symbol = instrument_slug
    if isinstance(block, dict):
        q = (block.get("search_query") or "").strip()
        if q.upper().startswith("GC"):
            display_symbol = f"COMEX:{q}"
        elif q:
            display_symbol = q
    return {
        "symbol": display_symbol,
        "timeframe": interval.strip(),
        "type": FOOTPRINT_CHART_TYPE,
    }


def footprint_json_output_path(
    output_dir: Path,
    interval: str,
    instrument_slug: str,
) -> Path:
    iv = interval_footprint_filename_slug(interval)
    slug = (instrument_slug or "").strip()
    return output_dir / f"{iv}_{slug}_footprint.json"


def resolve_detail_png_paths(
    charts_dir: Path,
    stamp: str,
    export_label: str,
    interval: str,
) -> list[Path]:
    """All detail footprint PNGs for one GoCharting slot (zoom + back_N)."""
    return gocharting_detail_png_paths(charts_dir, stamp, export_label, interval)


def _single_interval_schema_block(chart_info: dict[str, str]) -> str:
    symbol = chart_info.get("symbol", "")
    timeframe = chart_info.get("timeframe", "")
    chart_type = chart_info.get("type", FOOTPRINT_CHART_TYPE)
    return (
        f'    "{timeframe}": {{\n'
        '      "chart_info": {\n'
        f'        "symbol": "{symbol}",\n'
        f'        "timeframe": "{timeframe}",\n'
        f'        "type": "{chart_type}"\n'
        "      },\n"
        '      "candles": [\n'
        "        {\n"
        '          "time": "HH:MM",\n'
        '          "price_levels": [\n'
        '            { "bid": 0, "ask": 0, "attributes": [] }\n'
        "          ]\n"
        "        }\n"
        "      ]\n"
        "    }"
    )


def build_footprint_extract_user_prompt(
    chart_info: dict[str, str], *, crop_width_thirds: bool = True
) -> str:
    """User prompt for a single timeframe (used in tests and combined prompt)."""
    symbol = chart_info.get("symbol", "")
    timeframe = chart_info.get("timeframe", "")
    return (
        "Extract Bid/Ask Footprint data from the attached GoCharting detail chart image(s) "
        f"into JSON for {symbol} {timeframe}.\n\n"
        + _footprint_extract_rules_text(crop_width_thirds=crop_width_thirds)
    )


def build_combined_footprint_extract_user_prompt(
    chart_infos_by_interval: dict[str, dict[str, str]],
    *,
    crop_width_thirds: bool = True,
) -> str:
    """User prompt for M5 + M15 in one OpenAI request."""
    blocks = [
        _single_interval_schema_block(chart_infos_by_interval[iv])
        for iv in FOOTPRINT_EXTRACT_INTERVALS
        if iv in chart_infos_by_interval
    ]
    schema = "{\n" + ",\n".join(blocks) + "\n}"
    panel_line = (
        "Each source image is split into 3 horizontal panels (left, center, right).\n"
        if crop_width_thirds
        else ""
    )
    return (
        "Extract Bid/Ask Footprint data from the attached GoCharting detail chart images.\n"
        "Images are grouped by timeframe (5m first, then 15m).\n"
        f"{panel_line}\n"
        "Return ONLY one JSON object with top-level keys for each timeframe:\n"
        f"{schema}\n\n"
        + _footprint_extract_rules_text(crop_width_thirds=crop_width_thirds)
        + "\n- Top-level keys must be exactly \"5m\" and \"15m\".\n"
        "- Extract each timeframe only from its corresponding image group."
    )


def _footprint_extract_rules_text(*, crop_width_thirds: bool = True) -> str:
    panel_rule = (
        "- Each source image is provided as 3 horizontal panels (left → center → right). "
        "Read candles left-to-right across panels of the same source image before merging "
        "across source images.\n"
        if crop_width_thirds
        else "- Read candles left-to-right within each source image before merging across source images.\n"
    )
    return (
        "Rules:\n"
        "- ``bid`` and ``ask`` are non-negative integers read from each price level cell.\n"
        "- ``price_levels`` ordered from highest price to lowest price (top to bottom on chart).\n"
        '- ``attributes`` is a list of strings; use ``["imbalance"]`` when the cell is marked as imbalance.\n'
        + panel_rule
        + "- If multiple source images are attached for the same timeframe, merge all visible candles by ``time``, "
        "deduplicate, and sort ``candles`` chronologically.\n"
        "- Output JSON only, no markdown fences or extra text."
    )


def _build_combined_image_user_content(
    prompt: str,
    interval_png_paths: dict[str, list[Path]],
    *,
    crop_width_thirds: bool = True,
) -> tuple[list[dict[str, Any]], int, int]:
    parts: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    source_count = 0
    panel_count = 0
    for interval in FOOTPRINT_EXTRACT_INTERVALS:
        png_paths = interval_png_paths.get(interval) or []
        if not png_paths:
            continue
        names = ", ".join(p.name for p in png_paths)
        parts.append(
            {
                "type": "input_text",
                "text": f"[{interval} detail footprint source images — {names}]\n",
            }
        )
        for p in png_paths:
            source_count += 1
            source_label = _detail_png_source_label(p)
            crop_paths = gocharting_detail_openai_image_paths(
                p, crop_width_thirds=crop_width_thirds
            )
            for part_idx, crop_path in enumerate(crop_paths, start=1):
                panel_count += 1
                if crop_width_thirds and len(crop_paths) > 1:
                    label = (
                        f"[{interval} {source_label} part {part_idx}/"
                        f"{GOCHARTING_IMAGE_WIDTH_THIRDS} — {crop_path.name}]\n"
                    )
                else:
                    label = f"[{interval} {source_label} — {crop_path.name}]\n"
                parts.append(
                    {
                        "type": "input_text",
                        "text": label,
                    }
                )
                parts.append(
                    {
                        "type": "input_image",
                        "image_url": image_to_data_url(crop_path),
                        "detail": "auto",
                    }
                )
    return parts, source_count, panel_count


def validate_footprint_extract_json(data: Any) -> dict[str, Any]:
    """Validate and normalize footprint extraction JSON for one timeframe."""
    if not isinstance(data, dict):
        raise ValueError("Footprint JSON root must be an object")

    chart_info = data.get("chart_info")
    if not isinstance(chart_info, dict):
        raise ValueError("Missing or invalid chart_info object")

    for key in ("symbol", "timeframe", "type"):
        val = chart_info.get(key)
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"chart_info.{key} must be a non-empty string")

    candles_raw = data.get("candles")
    if not isinstance(candles_raw, list):
        raise ValueError("candles must be a list")

    candles: list[dict[str, Any]] = []
    for i, candle in enumerate(candles_raw):
        if not isinstance(candle, dict):
            raise ValueError(f"candles[{i}] must be an object")
        time_val = candle.get("time")
        if not isinstance(time_val, str) or not time_val.strip():
            raise ValueError(f"candles[{i}].time must be a non-empty string")
        levels_raw = candle.get("price_levels")
        if not isinstance(levels_raw, list):
            raise ValueError(f"candles[{i}].price_levels must be a list")

        price_levels: list[dict[str, Any]] = []
        for j, level in enumerate(levels_raw):
            if not isinstance(level, dict):
                raise ValueError(f"candles[{i}].price_levels[{j}] must be an object")
            try:
                bid = int(level.get("bid", 0))
                ask = int(level.get("ask", 0))
            except (TypeError, ValueError) as e:
                raise ValueError(
                    f"candles[{i}].price_levels[{j}].bid/ask must be integers"
                ) from e
            attrs_raw = level.get("attributes", [])
            if attrs_raw is None:
                attrs_raw = []
            if not isinstance(attrs_raw, list):
                raise ValueError(
                    f"candles[{i}].price_levels[{j}].attributes must be a list"
                )
            attributes = [str(a) for a in attrs_raw]
            price_levels.append({"bid": bid, "ask": ask, "attributes": attributes})

        candles.append({"time": time_val.strip(), "price_levels": price_levels})

    return {
        "chart_info": {
            "symbol": chart_info["symbol"].strip(),
            "timeframe": chart_info["timeframe"].strip(),
            "type": chart_info["type"].strip(),
        },
        "candles": candles,
    }


def validate_combined_footprint_extract_json(data: Any) -> dict[str, dict[str, Any]]:
    """Validate combined response with top-level ``5m`` and ``15m`` keys."""
    if not isinstance(data, dict):
        raise ValueError("Combined footprint JSON root must be an object")
    out: dict[str, dict[str, Any]] = {}
    for interval in FOOTPRINT_EXTRACT_INTERVALS:
        block = data.get(interval)
        if block is None:
            raise ValueError(f'Missing top-level key "{interval}" in combined footprint JSON')
        try:
            out[interval] = validate_footprint_extract_json(block)
        except ValueError as e:
            raise ValueError(f"{interval}: {e}") from e
        tf = out[interval]["chart_info"]["timeframe"]
        if tf != interval:
            raise ValueError(
                f'{interval}: chart_info.timeframe must be "{interval}", got {tf!r}'
            )
    return out


def write_footprint_extract_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    path.write_text(raw, encoding="utf-8")


def extract_all_footprint_jsons(
    *,
    api_key: str,
    charts_dir: Path,
    output_dir: Path,
    stamp: str,
    main_symbol: str,
    gocharting_yaml: Path,
    model: str = DEFAULT_FOOTPRINT_EXTRACT_MODEL,
    store: bool = True,
    include: Optional[list[str]] = None,
    reasoning_effort: str = DEFAULT_FOOTPRINT_EXTRACT_REASONING_EFFORT,
) -> dict[str, Path]:
    """
    Extract M5 + M15 footprint JSON in one OpenAI request; write two files.

    Returns mapping ``interval`` → output path (``5m``, ``15m``).
    """
    cfg = load_gocharting_yaml(gocharting_yaml)
    crop_width_thirds = gocharting_detail_crop_width_thirds(cfg)
    sym = normalize_main_chart_symbol(main_symbol)
    export_label = gocharting_footprint_export_label(sym)
    instrument_slug = resolve_instrument_slug(cfg, sym)

    interval_png_paths: dict[str, list[Path]] = {}
    output_paths: dict[str, Path] = {}
    chart_infos: dict[str, dict[str, str]] = {}

    for interval in FOOTPRINT_EXTRACT_INTERVALS:
        png_paths = resolve_detail_png_paths(charts_dir, stamp, export_label, interval)
        if not png_paths:
            raise FileNotFoundError(
                f"No {interval} detail PNGs for stamp {stamp!r} under {charts_dir} "
                f"(expected {stamp}_gocharting_{export_label}_{interval}_detail_*.png)"
            )
        for p in png_paths:
            if not p.is_file():
                raise FileNotFoundError(f"Detail PNG not found: {p}")
        interval_png_paths[interval] = png_paths
        output_paths[interval] = footprint_json_output_path(
            output_dir, interval, instrument_slug
        )
        chart_infos[interval] = resolve_gocharting_chart_info(cfg, sym, interval)

    prompt = build_combined_footprint_extract_user_prompt(
        chart_infos, crop_width_thirds=crop_width_thirds
    )
    content, source_images, crop_panels = _build_combined_image_user_content(
        prompt, interval_png_paths, crop_width_thirds=crop_width_thirds
    )

    client = OpenAI(api_key=api_key)
    create_kw: dict[str, Any] = {
        "model": (model or DEFAULT_FOOTPRINT_EXTRACT_MODEL).strip(),
        "input": responses_input_messages(
            user_content=content,
            system_prompt=FOOTPRINT_EXTRACT_SYSTEM_PROMPT,
        ),
        "store": store,
    }
    if include:
        create_kw["include"] = list(include)
    _eff = (reasoning_effort or "").strip()
    if _eff:
        create_kw["reasoning"] = {"summary": "auto", "effort": _eff}

    _log.info(
        "gocharting-footprint-extract: OpenAI call | intervals=%s source_images=%d crop_panels=%d model=%s reasoning_effort=%s",
        ",".join(FOOTPRINT_EXTRACT_INTERVALS),
        source_images,
        crop_panels,
        create_kw["model"],
        _eff or "(none)",
    )
    response = client.responses.create(**create_kw)
    raw_text = (response.output_text or "").strip()
    parsed = extract_json_object(raw_text)
    if parsed is None:
        snippet = raw_text[:500] if raw_text else "(empty)"
        raise ValueError(f"Could not parse JSON from OpenAI output: {snippet!r}")

    validated = validate_combined_footprint_extract_json(parsed)
    results: dict[str, Path] = {}
    for interval in FOOTPRINT_EXTRACT_INTERVALS:
        out_path = output_paths[interval]
        write_footprint_extract_json(out_path, validated[interval])
        _log.info(
            "gocharting-footprint-extract: wrote %s (%d candles)",
            out_path,
            len(validated[interval].get("candles") or []),
        )
        results[interval] = out_path
    return results


def load_gocharting_config(path: Path) -> dict[str, Any]:
    """Public alias for tests and CLI."""
    return load_gocharting_yaml(path)
