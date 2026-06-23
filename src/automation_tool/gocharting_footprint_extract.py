from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

from openai import OpenAI

from automation_tool.gocharting_capture import load_gocharting_yaml
from automation_tool.images import (
    gocharting_detail_png_paths,
    gocharting_footprint_export_label,
    image_to_data_url,
    normalize_main_chart_symbol,
)
from automation_tool.openai_analysis_json import extract_json_object
from automation_tool.prompts import responses_input_messages

_log = logging.getLogger(__name__)

DEFAULT_FOOTPRINT_EXTRACT_MODEL = "gpt-5.4-mini"
FOOTPRINT_EXTRACT_INTERVALS = ("5m", "15m")
FOOTPRINT_CHART_TYPE = "Bid/Ask Footprint"

_INTERVAL_FILENAME_RE = re.compile(r"^(\d+)m$", re.I)


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


def build_footprint_extract_user_prompt(chart_info: dict[str, str]) -> str:
    symbol = chart_info.get("symbol", "")
    timeframe = chart_info.get("timeframe", "")
    chart_type = chart_info.get("type", FOOTPRINT_CHART_TYPE)
    return (
        "Extract Bid/Ask Footprint data from the attached GoCharting detail chart image(s) "
        f"into JSON for {symbol} {timeframe}.\n\n"
        "Return ONLY a JSON object with this structure:\n"
        "{\n"
        '  "chart_info": {\n'
        f'    "symbol": "{symbol}",\n'
        f'    "timeframe": "{timeframe}",\n'
        f'    "type": "{chart_type}"\n'
        "  },\n"
        '  "candles": [\n'
        "    {\n"
        '      "time": "HH:MM",\n'
        '      "price_levels": [\n'
        '        { "bid": 0, "ask": 0, "attributes": [] }\n'
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Rules:\n"
        "- ``bid`` and ``ask`` are non-negative integers read from each price level cell.\n"
        "- ``price_levels`` ordered from highest price to lowest price (top to bottom on chart).\n"
        '- ``attributes`` is a list of strings; use ``["imbalance"]`` when the cell is marked as imbalance.\n'
        "- If multiple images are attached for the same timeframe, merge all visible candles by ``time``, "
        "deduplicate, and sort ``candles`` chronologically.\n"
        "- Output JSON only, no markdown fences or extra text."
    )


def _build_image_user_content(prompt: str, png_paths: list[Path]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    for p in png_paths:
        parts.append(
            {
                "type": "input_image",
                "image_url": image_to_data_url(p),
                "detail": "auto",
            }
        )
    return parts


def validate_footprint_extract_json(data: Any) -> dict[str, Any]:
    """Validate and normalize footprint extraction JSON."""
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


def write_footprint_extract_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    path.write_text(raw, encoding="utf-8")


def extract_gocharting_footprint_json(
    *,
    api_key: str,
    png_paths: list[Path],
    chart_info: dict[str, str],
    model: str = DEFAULT_FOOTPRINT_EXTRACT_MODEL,
    store: bool = True,
    include: Optional[list[str]] = None,
) -> dict[str, Any]:
    """One OpenAI vision call for a single interval's detail PNG set."""
    if not png_paths:
        raise ValueError("Need at least one detail PNG path")
    for p in png_paths:
        if not p.is_file():
            raise FileNotFoundError(f"Detail PNG not found: {p}")

    prompt = build_footprint_extract_user_prompt(chart_info)
    content = _build_image_user_content(prompt, png_paths)

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

    _log.info(
        "gocharting-footprint-extract: OpenAI call | timeframe=%s images=%d model=%s",
        chart_info.get("timeframe"),
        len(png_paths),
        create_kw["model"],
    )
    response = client.responses.create(**create_kw)
    raw_text = (response.output_text or "").strip()
    parsed = extract_json_object(raw_text)
    if parsed is None:
        snippet = raw_text[:500] if raw_text else "(empty)"
        raise ValueError(
            f"Could not parse JSON from OpenAI output for {chart_info.get('timeframe')}: {snippet!r}"
        )
    return validate_footprint_extract_json(parsed)


def _extract_one_interval(
    *,
    interval: str,
    png_paths: list[Path],
    output_path: Path,
    api_key: str,
    cfg: dict[str, Any],
    main_symbol: str,
    model: str,
    store: bool,
    include: Optional[list[str]],
) -> tuple[str, Path]:
    chart_info = resolve_gocharting_chart_info(cfg, main_symbol, interval)
    data = extract_gocharting_footprint_json(
        api_key=api_key,
        png_paths=png_paths,
        chart_info=chart_info,
        model=model,
        store=store,
        include=include,
    )
    write_footprint_extract_json(output_path, data)
    _log.info(
        "gocharting-footprint-extract: wrote %s (%d candles)",
        output_path,
        len(data.get("candles") or []),
    )
    return interval, output_path


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
) -> dict[str, Path]:
    """
    Extract M5 + M15 footprint JSON in parallel.

    Returns mapping ``interval`` → output path (``5m``, ``15m``).
    """
    cfg = load_gocharting_yaml(gocharting_yaml)
    sym = normalize_main_chart_symbol(main_symbol)
    export_label = gocharting_footprint_export_label(sym)
    instrument_slug = resolve_instrument_slug(cfg, sym)

    jobs: list[tuple[str, list[Path], Path]] = []
    for interval in FOOTPRINT_EXTRACT_INTERVALS:
        png_paths = resolve_detail_png_paths(charts_dir, stamp, export_label, interval)
        if not png_paths:
            raise FileNotFoundError(
                f"No {interval} detail PNGs for stamp {stamp!r} under {charts_dir} "
                f"(expected {stamp}_gocharting_{export_label}_{interval}_detail_*.png)"
            )
        out_path = footprint_json_output_path(output_dir, interval, instrument_slug)
        jobs.append((interval, png_paths, out_path))

    results: dict[str, Path] = {}
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {
            ex.submit(
                _extract_one_interval,
                interval=interval,
                png_paths=png_paths,
                output_path=out_path,
                api_key=api_key,
                cfg=cfg,
                main_symbol=sym,
                model=model,
                store=store,
                include=include,
            ): interval
            for interval, png_paths, out_path in jobs
        }
        for fut in as_completed(futures):
            interval = futures[fut]
            try:
                iv, path = fut.result()
                results[iv] = path
            except BaseException as e:
                errors.append(f"{interval}: {e}")

    if errors:
        raise RuntimeError(
            "GoCharting footprint extraction failed:\n" + "\n".join(errors)
        )
    return results


def load_gocharting_config(path: Path) -> dict[str, Any]:
    """Public alias for tests and CLI."""
    return load_gocharting_yaml(path)
