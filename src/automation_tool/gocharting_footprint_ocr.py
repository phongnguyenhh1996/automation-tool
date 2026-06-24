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


def parse_price_levels_from_overlay(
    words: list[dict[str, Any]],
    *,
    image_width: int,
    split_ratio: float = 0.42,
) -> list[dict[str, int]]:
    center_x = max(1, int(image_width * split_ratio))
    rows: dict[int, dict[str, Optional[tuple[int, int]]]] = {}

    for word in words:
        text = str(word.get("WordText") or "").strip().replace(",", "")
        if not _DIGITS_RE.match(text):
            continue
        try:
            value = int(text)
            left = int(word.get("Left") or 0)
            top = int(word.get("Top") or 0)
        except (TypeError, ValueError):
            continue
        bucket = (top // _ROW_BUCKET_PX) * _ROW_BUCKET_PX
        row = rows.setdefault(bucket, {"bid": None, "ask": None})
        side = "bid" if left < center_x else "ask"
        current = row[side]
        if current is None:
            row[side] = (value, abs(left - center_x))
            continue
        if abs(left - center_x) < current[1]:
            row[side] = (value, abs(left - center_x))

    levels: list[dict[str, int]] = []
    for bucket in sorted(rows.keys()):
        row = rows[bucket]
        bid = row["bid"][0] if row["bid"] is not None else 0
        ask = row["ask"][0] if row["ask"] is not None else 0
        if bid == 0 and ask == 0:
            continue
        levels.append({"bid": bid, "ask": ask})
    return levels


def parse_footprint_candle_from_ocr(
    ocr_payload: dict[str, Any],
    *,
    image_width: int,
    closed_candle_open: datetime,
    split_ratio: float = 0.42,
) -> dict[str, Any]:
    parsed_text = ""
    results = ocr_payload.get("ParsedResults") or []
    if results and isinstance(results[0], dict):
        parsed_text = str(results[0].get("ParsedText") or "")
    time_val = extract_time_hhmm_from_ocr_text(parsed_text) or closed_candle_time_hhmm(
        closed_candle_open
    )
    words = _iter_overlay_words(ocr_payload)
    price_levels = parse_price_levels_from_overlay(
        words,
        image_width=image_width,
        split_ratio=split_ratio,
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
    split_ratio: float = 0.42,
    delete_image_after: bool = True,
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
