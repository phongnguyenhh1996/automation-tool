from __future__ import annotations

import io
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx
from PIL import Image, ImageOps

_log = logging.getLogger(__name__)

OCR_SPACE_PARSE_URL = "https://api.ocr.space/parse/image"
FOOTPRINT_CHART_TYPE = "Bid/Ask Footprint"
_DEFAULT_SYMBOL = "COMEX:GC1!"
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_DATE_LINE_RE = re.compile(
    r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b|\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b",
    re.I,
)
_PAIR_LINE_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s*$")
_FOOTPRINT_OCR_ENGINE = "3"
_FOOTPRINT_OCR_TIMEOUT_S = 180.0
_GARBAGE_OCR_LINE_RE = re.compile(r"[@$=\\]|\\phi", re.I)


class FootprintOcrSkipped(Exception):
    """OCR did not yield any bid/ask pair lines — skip this candle."""


def footprint_interval_json_path(out_dir: Path, interval: str) -> Path:
    iv = (interval or "").strip().lower()
    return out_dir / f"footprint_bid_ask_{iv}.json"


def footprint_images_dir(
    charts_dir: Path,
    *,
    gocharting_yaml: Optional[Path] = None,
) -> Path:
    subdir = "footprint_images"
    try:
        from automation_tool.config import default_gocharting_config_path
        from automation_tool.gocharting_capture import load_gocharting_yaml

        yaml_path = gocharting_yaml or default_gocharting_config_path()
        cfg = load_gocharting_yaml(yaml_path)
        raw = cfg.get("footprint_screenshot")
        if isinstance(raw, dict):
            subdir = str(raw.get("output_subdir") or subdir).strip() or subdir
    except Exception:
        pass
    return charts_dir / subdir


def existing_footprint_bid_ask_json_paths(
    charts_dir: Path,
    *,
    gocharting_yaml: Optional[Path] = None,
    intervals: tuple[str, ...] = ("15m", "5m"),
) -> list[Path]:
    """Return on-disk OCR footprint JSON files (15m then 5m), skipping missing paths."""
    out_dir = footprint_images_dir(charts_dir, gocharting_yaml=gocharting_yaml)
    paths: list[Path] = []
    for interval in intervals:
        path = footprint_interval_json_path(out_dir, interval)
        if path.is_file():
            paths.append(path)
    return paths


def new_footprint_document(*, symbol: str, timeframe: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "type": FOOTPRINT_CHART_TYPE,
        "candles": [],
    }


def load_footprint_document(path: Path, *, symbol: str, timeframe: str) -> dict[str, Any]:
    if not path.is_file():
        return new_footprint_document(symbol=symbol, timeframe=timeframe)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"footprint JSON root must be object: {path}")
    doc = new_footprint_document(symbol=symbol, timeframe=timeframe)
    doc["symbol"] = str(raw.get("symbol") or symbol)
    doc["timeframe"] = str(raw.get("timeframe") or timeframe)
    doc["type"] = str(raw.get("type") or FOOTPRINT_CHART_TYPE)
    candles = raw.get("candles")
    if isinstance(candles, list):
        doc["candles"] = [c for c in candles if isinstance(c, dict)]
    return doc


def write_footprint_document(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_candle_to_footprint_document(
    path: Path,
    candle: dict[str, Any],
    *,
    symbol: str,
    timeframe: str,
) -> dict[str, Any]:
    doc = load_footprint_document(path, symbol=symbol, timeframe=timeframe)
    time_key = str(candle.get("time") or "").strip()
    candles: list[dict[str, Any]] = [
        c for c in doc.get("candles", []) if str(c.get("time") or "").strip() != time_key
    ]
    candles.append(candle)
    candles.sort(key=lambda c: str(c.get("time") or ""))
    doc["candles"] = candles
    write_footprint_document(path, doc)
    return doc


def preprocess_footprint_clip_image(image: Image.Image) -> Image.Image:
    return ImageOps.autocontrast(image.convert("L"), cutoff=1)


def preprocess_footprint_clip_path(image_path: Path) -> Image.Image:
    with Image.open(image_path) as raw:
        return preprocess_footprint_clip_image(raw)


def _pil_to_png_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def ocr_space_parse_pil_image(
    image: Image.Image,
    *,
    filename: str,
    api_key: str,
    timeout_s: float = _FOOTPRINT_OCR_TIMEOUT_S,
) -> dict[str, Any]:
    return ocr_space_parse_bytes(
        _pil_to_png_bytes(image),
        filename=filename,
        api_key=api_key,
        timeout_s=timeout_s,
    )


def ocr_space_parse_bytes(
    image_bytes: bytes,
    *,
    filename: str,
    api_key: str,
    timeout_s: float = _FOOTPRINT_OCR_TIMEOUT_S,
    ocr_engine: str = _FOOTPRINT_OCR_ENGINE,
) -> dict[str, Any]:
    key = (api_key or "").strip()
    if not key:
        raise ValueError("OCR_SPACE_API_KEY is required for footprint OCR")

    with httpx.Client(timeout=timeout_s) as client:
        resp = client.post(
            OCR_SPACE_PARSE_URL,
            headers={"apikey": key},
            files={"file": (filename, image_bytes, "image/png")},
            data={
                "language": "eng",
                "isOverlayRequired": "true",
                "isTable": "true",
                "scale": "true",
                "OCREngine": str(ocr_engine),
                "filetype": "PNG",
            },
        )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("IsErroredOnProcessing"):
        raise RuntimeError(
            f"OCR.space error: {payload.get('ErrorMessage') or payload.get('ErrorDetails')}"
        )
    results = payload.get("ParsedResults") or []
    if not results:
        raise RuntimeError("OCR.space returned no ParsedResults")
    first = results[0]
    if str(first.get("FileParseExitCode")) != "1":
        raise RuntimeError(
            f"OCR.space parse failed: {first.get('ErrorMessage') or first.get('ErrorDetails')}"
        )
    return payload


def extract_time_hhmm_from_ocr_text(text: str) -> Optional[str]:
    matches = list(_TIME_RE.finditer(text or ""))
    if not matches:
        return None
    last = matches[-1]
    hour = int(last.group(1))
    minute = int(last.group(2))
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def closed_candle_time_hhmm(closed_candle_open: datetime) -> str:
    return closed_candle_open.strftime("%H:%M")


def _iter_overlay_lines(ocr_payload: dict[str, Any]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for block in ocr_payload.get("ParsedResults") or []:
        overlay = block.get("TextOverlay")
        if not isinstance(overlay, dict):
            continue
        for line in overlay.get("Lines") or []:
            if isinstance(line, dict):
                lines.append(line)
    return lines


def _line_top(line: dict[str, Any]) -> int:
    try:
        if line.get("MinTop") is not None:
            return int(float(line["MinTop"]))
    except (TypeError, ValueError):
        pass
    tops: list[int] = []
    for word in line.get("Words") or []:
        if isinstance(word, dict):
            try:
                tops.append(int(word.get("Top") or 0))
            except (TypeError, ValueError):
                continue
    return min(tops) if tops else 0


def _line_text(line: dict[str, Any]) -> str:
    return str(line.get("LineText") or "").strip()


def _footer_cutoff_top(lines: list[dict[str, Any]]) -> Optional[int]:
    cutoffs: list[int] = []
    for line in lines:
        text = _line_text(line)
        if _DATE_LINE_RE.search(text):
            cutoffs.append(_line_top(line))
    if not cutoffs:
        return None
    return min(cutoffs) - 4


def _is_footer_or_time_line(
    line: dict[str, Any],
    *,
    footer_cutoff_top: Optional[int] = None,
) -> bool:
    text = _line_text(line)
    if "@" in text:
        return True
    if _DATE_LINE_RE.search(text):
        return True
    if footer_cutoff_top is not None and _line_top(line) >= footer_cutoff_top:
        return True
    return False


def _is_garbage_ocr_line(line: str) -> bool:
    text = (line or "").strip()
    if not text:
        return True
    if _GARBAGE_OCR_LINE_RE.search(text):
        return True
    if _DATE_LINE_RE.search(text):
        return True
    return False


def _bid_ask_from_pair_line_text(text: str) -> Optional[dict[str, int]]:
    match = _PAIR_LINE_RE.match((text or "").strip())
    if not match:
        return None
    return {"bid": int(match.group(1)), "ask": int(match.group(2))}


def parse_price_levels_from_parsed_text(text: str) -> list[dict[str, int]]:
    levels: list[dict[str, int]] = []
    for line in (text or "").splitlines():
        if _is_garbage_ocr_line(line):
            continue
        level = _bid_ask_from_pair_line_text(line)
        if level is not None:
            levels.append(level)
    return levels


def parse_price_levels_from_text_lines(
    lines: list[dict[str, Any]],
) -> list[dict[str, int]]:
    footer_cutoff = _footer_cutoff_top(lines)
    levels: list[dict[str, int]] = []
    for line in sorted(lines, key=_line_top):
        if _is_footer_or_time_line(line, footer_cutoff_top=footer_cutoff):
            continue
        level = _bid_ask_from_pair_line_text(_line_text(line))
        if level is not None:
            levels.append(level)
    return levels


def parse_price_levels_from_overlay(
    *,
    lines: Optional[list[dict[str, Any]]] = None,
    parsed_text: str = "",
) -> list[dict[str, int]]:
    """Only accept OCR lines matching ``bid ask`` (two integers per row)."""
    if lines:
        text_line_levels = parse_price_levels_from_text_lines(lines)
        if text_line_levels:
            return text_line_levels
    return parse_price_levels_from_parsed_text(parsed_text)


def parse_footprint_candle_from_clip_image(
    image_path: Path,
    *,
    api_key: str,
    closed_candle_open: datetime,
    image_width: int,
    split_ratio: float = 0.5,
) -> dict[str, Any]:
    del image_width, split_ratio
    ocr_image = preprocess_footprint_clip_path(image_path)
    payload = ocr_space_parse_pil_image(
        ocr_image,
        filename=image_path.name,
        api_key=api_key,
    )
    lines = _iter_overlay_lines(payload)
    parsed_text = ""
    results = payload.get("ParsedResults") or []
    if results and isinstance(results[0], dict):
        parsed_text = str(results[0].get("ParsedText") or "")
    price_levels = parse_price_levels_from_overlay(lines=lines, parsed_text=parsed_text)
    if not price_levels:
        raise FootprintOcrSkipped(
            f"no bid/ask pair lines in OCR for {image_path.name}"
        )
    _log.info(
        "footprint OCR: parsed %s | size=%s levels=%d",
        image_path.name,
        ocr_image.size,
        len(price_levels),
    )
    return {
        "time": closed_candle_time_hhmm(closed_candle_open),
        "price_levels": price_levels,
    }


def parse_footprint_candle_from_ocr(
    ocr_payload: dict[str, Any],
    *,
    image_width: int,
    closed_candle_open: datetime,
    split_ratio: float = 0.5,
) -> dict[str, Any]:
    del image_width, split_ratio
    parsed_text = ""
    results = ocr_payload.get("ParsedResults") or []
    if results and isinstance(results[0], dict):
        parsed_text = str(results[0].get("ParsedText") or "")
    time_val = extract_time_hhmm_from_ocr_text(parsed_text) or closed_candle_time_hhmm(
        closed_candle_open
    )
    lines = _iter_overlay_lines(ocr_payload)
    price_levels = parse_price_levels_from_overlay(lines=lines, parsed_text=parsed_text)
    if not price_levels:
        raise FootprintOcrSkipped("no bid/ask pair lines in OCR payload")
    return {"time": time_val, "price_levels": price_levels}


def process_footprint_clip_image(
    image_path: Path,
    *,
    ocr_api_key: str,
    closed_candle_open: datetime,
    image_width: int,
    out_json_path: Path,
    symbol: str,
    timeframe: str,
    split_ratio: float = 0.5,
    delete_image_after: bool = False,
) -> Optional[tuple[dict[str, Any], dict[str, Any]]]:
    """
    OCR clip PNG → append candle to interval JSON → optionally delete PNG.

    Returns ``(candle, full_document)`` or ``None`` if OCR has no valid pair lines.
    """
    try:
        candle = parse_footprint_candle_from_clip_image(
            image_path,
            api_key=ocr_api_key,
            closed_candle_open=closed_candle_open,
            image_width=image_width,
            split_ratio=split_ratio,
        )
    except FootprintOcrSkipped as exc:
        _log.warning("footprint OCR: skip candle | %s", exc)
        return None
    doc = append_candle_to_footprint_document(
        out_json_path,
        candle,
        symbol=symbol,
        timeframe=timeframe,
    )
    _log.info(
        "footprint OCR: %s → %s | time=%s levels=%d",
        image_path.name,
        out_json_path.name,
        candle["time"],
        len(candle["price_levels"]),
    )
    if delete_image_after:
        try:
            image_path.unlink()
            _log.debug("footprint OCR: deleted screenshot %s", image_path.name)
        except OSError:
            _log.warning("footprint OCR: could not delete screenshot %s", image_path, exc_info=True)
    return candle, doc
