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
_DIGITS_RE = re.compile(r"^\d+$")
_PAIR_LINE_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s*$")
_FOOTPRINT_UPSCALE = 2
_FOOTPRINT_OCR_ENGINE = "3"


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
    preprocessed = preprocess_footprint_clip_image(path)
    return ocr_space_parse_pil_image(
        preprocessed,
        filename=path.name,
        api_key=api_key,
        timeout_s=timeout_s,
    )


def preprocess_footprint_clip_image(
    image_path: Path,
    *,
    footer_crop_ratio: float = 0.05,
) -> Image.Image:
    with Image.open(image_path) as raw:
        img = raw.convert("L")
    width, height = img.size
    crop_height = max(1, height - int(height * footer_crop_ratio))
    if crop_height < height:
        img = img.crop((0, 0, width, crop_height))
    img = ImageOps.autocontrast(img, cutoff=1)
    img = img.point(lambda px: 0 if px < 155 else 255)
    return img


def _pil_to_png_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def ocr_space_parse_pil_image(
    image: Image.Image,
    *,
    filename: str,
    api_key: str,
    timeout_s: float = 120.0,
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
    timeout_s: float = 120.0,
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
    if "@" in text:
        return True
    if _DATE_LINE_RE.search(text):
        return True
    digit_words = _digit_words_in_line(line)
    if len(digit_words) > 2:
        return True
    if footer_cutoff_top is not None and _line_top(line) >= footer_cutoff_top:
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


def _row_merge_threshold_y(tops: list[int]) -> int:
    if len(tops) < 2:
        return 8
    gaps = [tops[idx + 1] - tops[idx] for idx in range(len(tops) - 1) if tops[idx + 1] > tops[idx]]
    if not gaps:
        return 8
    gaps.sort()
    return max(4, min(14, gaps[max(0, len(gaps) // 5)]))


def _collapse_y_cluster(cluster: list[tuple[int, int]]) -> tuple[int, int]:
    avg_y = int(sum(item[0] for item in cluster) / len(cluster))
    return avg_y, cluster[-1][1]


def cluster_y_value_entries(entries: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not entries:
        return []
    sorted_entries = sorted(entries, key=lambda item: item[0])
    threshold = _row_merge_threshold_y([item[0] for item in sorted_entries])
    merged: list[tuple[int, int]] = []
    cluster = [sorted_entries[0]]
    for entry in sorted_entries[1:]:
        if entry[0] - cluster[-1][0] <= threshold:
            cluster.append(entry)
            continue
        merged.append(_collapse_y_cluster(cluster))
        cluster = [entry]
    merged.append(_collapse_y_cluster(cluster))
    return merged


def y_value_entries_from_ocr_payload(payload: dict[str, Any]) -> list[tuple[int, int]]:
    raw: list[tuple[int, int]] = []
    for word in _iter_overlay_words(payload):
        text = str(word.get("WordText") or "").strip().replace(",", "")
        if not _DIGITS_RE.match(text):
            continue
        try:
            top = int(word.get("Top") or 0)
            value = int(text)
        except (TypeError, ValueError):
            continue
        raw.append((top, value))
    return cluster_y_value_entries(raw)


def align_bid_ask_y_entries(
    bid_entries: list[tuple[int, int]],
    ask_entries: list[tuple[int, int]],
) -> list[dict[str, int]]:
    if not bid_entries and not ask_entries:
        return []

    all_tops = sorted([item[0] for item in bid_entries] + [item[0] for item in ask_entries])
    y_tol = _row_merge_threshold_y(all_tops)
    used_asks: set[int] = set()
    pairs: list[tuple[int, int, int]] = []

    for bid_y, bid in bid_entries:
        best_idx: Optional[int] = None
        best_dist = y_tol + 1
        for idx, (ask_y, ask) in enumerate(ask_entries):
            if idx in used_asks:
                continue
            dist = abs(bid_y - ask_y)
            if dist <= y_tol and dist < best_dist:
                best_dist = dist
                best_idx = idx
        if best_idx is not None:
            used_asks.add(best_idx)
            ask_y, ask = ask_entries[best_idx]
            pairs.append((min(bid_y, ask_y), bid, ask))
        else:
            pairs.append((bid_y, bid, 0))

    for idx, (ask_y, ask) in enumerate(ask_entries):
        if idx in used_asks:
            continue
        pairs.append((ask_y, 0, ask))

    pairs.sort(key=lambda item: item[0])
    return [{"bid": bid, "ask": ask} for _y, bid, ask in pairs if bid or ask]


def _upscale_for_ocr(image: Image.Image, factor: int = _FOOTPRINT_UPSCALE) -> Image.Image:
    width, height = image.size
    if width < 1 or height < 1:
        return image
    return image.resize((width * factor, height * factor), Image.Resampling.LANCZOS)


def _filter_footer_digit_words(
    words: list[dict[str, Any]],
    lines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    footer_cutoff = _footer_cutoff_top(lines)
    if footer_cutoff is None:
        return words
    filtered: list[dict[str, Any]] = []
    for word in words:
        try:
            top = int(word.get("Top") or 0)
        except (TypeError, ValueError):
            continue
        if top < footer_cutoff:
            filtered.append(word)
    return filtered


def _cluster_digit_words_into_rows(
    digit_words: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    if not digit_words:
        return []
    sorted_words = sorted(digit_words, key=lambda word: int(word.get("Top") or 0))
    tops = [int(word.get("Top") or 0) for word in sorted_words]
    threshold = _row_merge_threshold_y(tops)
    clusters: list[list[dict[str, Any]]] = [[sorted_words[0]]]
    cluster_centers = [tops[0]]
    for word in sorted_words[1:]:
        top = int(word.get("Top") or 0)
        best_idx: Optional[int] = None
        best_dist = threshold + 1
        for idx, center in enumerate(cluster_centers):
            dist = abs(top - center)
            if dist <= threshold and dist < best_dist:
                best_dist = dist
                best_idx = idx
        if best_idx is not None:
            clusters[best_idx].append(word)
            cluster_centers[best_idx] = int(
                sum(int(item.get("Top") or 0) for item in clusters[best_idx])
                / len(clusters[best_idx])
            )
            continue
        clusters.append([word])
        cluster_centers.append(top)
    return sorted(clusters, key=lambda row: int(row[0].get("Top") or 0))


def _parse_price_levels_from_word_clusters(
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
    levels: list[dict[str, int]] = []
    for row_words in _cluster_digit_words_into_rows(digit_words):
        level = _pair_bid_ask_from_digit_words(row_words, split_x=split_x)
        if level["bid"] == 0 and level["ask"] == 0:
            continue
        levels.append(level)
    return levels


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


def _parse_price_levels_from_word_buckets(
    words: list[dict[str, Any]],
    *,
    image_width: int,
    split_ratio: float,
) -> list[dict[str, int]]:
    return _parse_price_levels_from_word_clusters(
        words,
        image_width=image_width,
        split_ratio=split_ratio,
    )


def parse_price_levels_from_overlay(
    words: list[dict[str, Any]],
    *,
    image_width: int,
    split_ratio: float = 0.5,
    lines: Optional[list[dict[str, Any]]] = None,
    image_height: int = 0,
    parsed_text: str = "",
) -> list[dict[str, int]]:
    if lines:
        text_line_levels = parse_price_levels_from_text_lines(lines)
        if text_line_levels:
            return text_line_levels
    text_levels = parse_price_levels_from_parsed_text(parsed_text)
    if text_levels:
        return text_levels

    filtered_words = _filter_footer_digit_words(words, lines or [])
    levels = _parse_price_levels_from_word_clusters(
        filtered_words,
        image_width=image_width,
        split_ratio=split_ratio,
    )
    if levels:
        return levels
    if lines:
        line_levels = _parse_price_levels_from_lines(
            lines,
            image_width=image_width,
            split_ratio=split_ratio,
            image_height=image_height,
        )
        if line_levels:
            return line_levels
    return []


def parse_footprint_candle_from_clip_image(
    image_path: Path,
    *,
    api_key: str,
    closed_candle_open: datetime,
    image_width: int,
    split_ratio: float = 0.5,
) -> dict[str, Any]:
    preprocessed = preprocess_footprint_clip_image(image_path)
    ocr_image = _upscale_for_ocr(preprocessed)
    payload = ocr_space_parse_pil_image(
        ocr_image,
        filename=image_path.name,
        api_key=api_key,
    )
    words = _iter_overlay_words(payload)
    lines = _iter_overlay_lines(payload)
    image_height = _estimate_image_height(words, lines)
    parsed_text = ""
    results = payload.get("ParsedResults") or []
    if results and isinstance(results[0], dict):
        parsed_text = str(results[0].get("ParsedText") or "")
    ocr_width = ocr_image.size[0] or image_width
    price_levels = parse_price_levels_from_overlay(
        words,
        image_width=ocr_width,
        split_ratio=split_ratio,
        lines=lines,
        image_height=image_height,
        parsed_text=parsed_text,
    )
    if not price_levels:
        raise RuntimeError("OCR produced no bid/ask price levels")

    time_val = closed_candle_time_hhmm(closed_candle_open)
    return {"time": time_val, "price_levels": price_levels}


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
        parsed_text=parsed_text,
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
    candle = parse_footprint_candle_from_clip_image(
        image_path,
        api_key=ocr_api_key,
        closed_candle_open=closed_candle_open,
        image_width=image_width,
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
