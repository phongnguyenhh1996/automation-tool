from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx

_log = logging.getLogger(__name__)

OCR_SPACE_PARSE_URL = "https://api.ocr.space/parse/image"
FOOTPRINT_CHART_TYPE = "Bid/Ask Footprint"
_DEFAULT_SYMBOL = "COMEX:GC1!"
_ROW_BUCKET_PX = 10
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_DATE_LINE_RE = re.compile(
    r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b|\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b",
    re.I,
)
_DIGITS_RE = re.compile(r"^\d+$")


def footprint_interval_json_path(out_dir: Path, interval: str) -> Path:
    iv = (interval or "").strip().lower()
    return out_dir / f"footprint_bid_ask_{iv}.json"


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


def ocr_space_parse_image(
    image_path: Path,
    *,
    api_key: str,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"OCR image not found: {path}")
    key = (api_key or "").strip()
    if not key:
        raise ValueError("OCR_SPACE_API_KEY is required for footprint OCR")

    with path.open("rb") as fh:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(
                OCR_SPACE_PARSE_URL,
                headers={"apikey": key},
                files={"file": (path.name, fh, "image/png")},
                data={
                    "language": "eng",
                    "isOverlayRequired": "true",
                    "isTable": "true",
                    "scale": "true",
                    "OCREngine": "2",
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


def _iter_overlay_words(ocr_payload: dict[str, Any]) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    for block in ocr_payload.get("ParsedResults") or []:
        overlay = block.get("TextOverlay")
        if not isinstance(overlay, dict):
            continue
        for line in overlay.get("Lines") or []:
            if not isinstance(line, dict):
                continue
            for word in line.get("Words") or []:
                if isinstance(word, dict):
                    words.append(word)
    return words


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


def _word_center_x(word: dict[str, Any]) -> int:
    left = int(word.get("Left") or 0)
    width = int(word.get("Width") or 0)
    return left + width // 2


def _digit_words_in_line(line: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for word in line.get("Words") or []:
        if not isinstance(word, dict):
            continue
        text = str(word.get("WordText") or "").strip().replace(",", "")
        if not _DIGITS_RE.match(text):
            continue
        try:
            value = int(text)
        except ValueError:
            continue
        out.append({**word, "_value": value})
    return out


def _estimate_image_height(
    words: list[dict[str, Any]],
    lines: list[dict[str, Any]],
) -> int:
    bottoms: list[int] = []
    for word in words:
        try:
            top = int(word.get("Top") or 0)
            height = int(word.get("Height") or 0)
        except (TypeError, ValueError):
            continue
        bottoms.append(top + height)
    for line in lines:
        bottoms.append(_line_top(line) + 20)
    return max(bottoms) if bottoms else 0


def _footer_cutoff_top(lines: list[dict[str, Any]]) -> Optional[int]:
    cutoffs: list[int] = []
    for line in lines:
        text = _line_text(line)
        if _DATE_LINE_RE.search(text):
            cutoffs.append(_line_top(line))
            continue
        if len(_digit_words_in_line(line)) > 2:
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
    if _DATE_LINE_RE.search(text):
        return True
    digit_words = _digit_words_in_line(line)
    if len(digit_words) > 2:
        return True
    if footer_cutoff_top is not None and _line_top(line) >= footer_cutoff_top:
        return True
    return False


def estimate_footprint_split_x(
    digit_words: list[dict[str, Any]],
    image_width: int,
    *,
    fallback_ratio: float = 0.5,
) -> int:
    if len(digit_words) < 2:
        return max(1, int(image_width * fallback_ratio))
    xs = sorted(_word_center_x(word) for word in digit_words)
    best_gap = 0
    split = image_width * fallback_ratio
    for idx in range(len(xs) - 1):
        gap = xs[idx + 1] - xs[idx]
        if gap > best_gap:
            best_gap = gap
            split = (xs[idx] + xs[idx + 1]) / 2
    if best_gap < image_width * 0.15:
        return max(1, int(image_width * fallback_ratio))
    return max(1, int(split))


def _pair_bid_ask_from_digit_words(
    digit_words: list[dict[str, Any]],
    *,
    split_x: int,
) -> dict[str, int]:
    if len(digit_words) >= 2:
        left_word, right_word = sorted(digit_words, key=_word_center_x)[:2]
        return {"bid": int(left_word["_value"]), "ask": int(right_word["_value"])}
    word = digit_words[0]
    value = int(word["_value"])
    if _word_center_x(word) < split_x:
        return {"bid": value, "ask": 0}
    return {"bid": 0, "ask": value}


def _parse_price_levels_from_lines(
    lines: list[dict[str, Any]],
    *,
    image_width: int,
    split_ratio: float,
    image_height: int,
) -> list[dict[str, int]]:
    footer_cutoff = _footer_cutoff_top(lines)
    _ = image_height
    candle_lines: list[tuple[int, list[dict[str, Any]]]] = []
    digit_words_all: list[dict[str, Any]] = []
    for line in lines:
        if _is_footer_or_time_line(line, footer_cutoff_top=footer_cutoff):
            continue
        digit_words = _digit_words_in_line(line)
        if not digit_words or len(digit_words) > 2:
            continue
        candle_lines.append((_line_top(line), digit_words))
        digit_words_all.extend(digit_words)
    if not candle_lines:
        return []

    split_x = estimate_footprint_split_x(
        digit_words_all,
        image_width,
        fallback_ratio=split_ratio,
    )
    levels: list[dict[str, int]] = []
    for _top, digit_words in sorted(candle_lines, key=lambda item: item[0]):
        level = _pair_bid_ask_from_digit_words(digit_words, split_x=split_x)
        if level["bid"] == 0 and level["ask"] == 0:
            continue
        levels.append(level)
    return levels


def _row_bucket_px(words: list[dict[str, Any]]) -> int:
    heights: list[int] = []
    for word in words:
        text = str(word.get("WordText") or "").strip().replace(",", "")
        if not _DIGITS_RE.match(text):
            continue
        try:
            heights.append(max(8, int(word.get("Height") or 12)))
        except (TypeError, ValueError):
            continue
    if not heights:
        return _ROW_BUCKET_PX
    heights.sort()
    median = heights[len(heights) // 2]
    return max(8, int(median * 0.85))


def _parse_price_levels_from_word_buckets(
    words: list[dict[str, Any]],
    *,
    image_width: int,
    split_ratio: float,
) -> list[dict[str, int]]:
    digit_words: list[dict[str, Any]] = []
    for word in words:
        text = str(word.get("WordText") or "").strip().replace(",", "")
        if not _DIGITS_RE.match(text):
            continue
        try:
            digit_words.append({**word, "_value": int(text)})
        except (TypeError, ValueError):
            continue
    if not digit_words:
        return []

    split_x = estimate_footprint_split_x(
        digit_words,
        image_width,
        fallback_ratio=split_ratio,
    )
    bucket_px = _row_bucket_px(words)
    rows: dict[int, list[dict[str, Any]]] = {}
    for word in digit_words:
        top = int(word.get("Top") or 0)
        bucket = (top // bucket_px) * bucket_px
        rows.setdefault(bucket, []).append(word)

    levels: list[dict[str, int]] = []
    for bucket in sorted(rows.keys()):
        level = _pair_bid_ask_from_digit_words(rows[bucket], split_x=split_x)
        if level["bid"] == 0 and level["ask"] == 0:
            continue
        levels.append(level)
    return levels


def parse_price_levels_from_overlay(
    words: list[dict[str, Any]],
    *,
    image_width: int,
    split_ratio: float = 0.5,
    lines: Optional[list[dict[str, Any]]] = None,
    image_height: int = 0,
) -> list[dict[str, int]]:
    if lines:
        levels = _parse_price_levels_from_lines(
            lines,
            image_width=image_width,
            split_ratio=split_ratio,
            image_height=image_height,
        )
        if levels:
            return levels
    return _parse_price_levels_from_word_buckets(
        words,
        image_width=image_width,
        split_ratio=split_ratio,
    )


def parse_footprint_candle_from_ocr(
    ocr_payload: dict[str, Any],
    *,
    image_width: int,
    closed_candle_open: datetime,
    split_ratio: float = 0.5,
) -> dict[str, Any]:
    parsed_text = ""
    results = ocr_payload.get("ParsedResults") or []
    if results and isinstance(results[0], dict):
        parsed_text = str(results[0].get("ParsedText") or "")
    time_val = extract_time_hhmm_from_ocr_text(parsed_text) or closed_candle_time_hhmm(
        closed_candle_open
    )
    words = _iter_overlay_words(ocr_payload)
    lines = _iter_overlay_lines(ocr_payload)
    image_height = _estimate_image_height(words, lines)
    price_levels = parse_price_levels_from_overlay(
        words,
        image_width=image_width,
        split_ratio=split_ratio,
        lines=lines,
        image_height=image_height,
    )
    if not price_levels:
        raise RuntimeError("OCR produced no bid/ask price levels")
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
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    OCR clip PNG → append candle to interval JSON → optionally delete PNG.

    Returns ``(candle, full_document)``.
    """
    ocr_payload = ocr_space_parse_image(image_path, api_key=ocr_api_key)
    candle = parse_footprint_candle_from_ocr(
        ocr_payload,
        image_width=image_width,
        closed_candle_open=closed_candle_open,
        split_ratio=split_ratio,
    )
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
