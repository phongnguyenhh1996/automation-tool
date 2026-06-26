from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import re
import time
from datetime import datetime
from pathlib import Path
from collections.abc import Iterable
from typing import Any, Optional

import httpx
from PIL import Image

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
_DIGITS_RE = re.compile(r"^\d+$")
_ROW_BUCKET_PX = 10
_FOOTPRINT_OCR_ENGINE = "3"
_FOOTPRINT_OCR_TIMEOUT_S = 180.0
_FOOTPRINT_OCR_BATCH_DELAY_S = 5.0
_GARBAGE_OCR_LINE_RE = re.compile(r"[@$=\\]|\\phi", re.I)
_NO_TEXT_DETECTED_RE = re.compile(r"\[No text detected\]", re.I)
_FOOTPRINT_CLIP_PNG_RE = re.compile(
    r"^(\d{8})_(\d{1,2})h(\d{1,2})m_(\d+m)\.png$",
    re.I,
)
FOOTPRINT_CANDLE_TZ_SUFFIX = " GMT+0700"
_TIME_ONLY_RE = re.compile(r"^\d{1,2}:\d{2}$")
_LEGACY_ISO_MINUTE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})$")
_WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTH_NAMES = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_GOCHARTING_DATETIME_RE = re.compile(
    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"(\d{1,2})\s+"
    r"(\d{4})\s+"
    r"(\d{1,2}):(\d{2}):\d{2}",
    re.I,
)
_FOOTPRINT_CANONICAL_TIME_RE = re.compile(
    r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"(\d{1,2})\s+"
    r"(\d{4})\s+"
    r"(\d{1,2}):(\d{2}):(\d{2})\s+GMT\+0700$",
    re.I,
)
_MONTH_ABBR = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


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


def parse_footprint_clip_png_name(filename: str) -> Optional[tuple[datetime, str]]:
    """Parse ``{YYYYMMDD}_{H}h{M}m_{interval}.png`` → (closed_candle_open, interval)."""
    match = _FOOTPRINT_CLIP_PNG_RE.match(Path(filename).name)
    if not match:
        return None
    date_part, hour_s, minute_s, interval = match.groups()
    try:
        closed_open = datetime.strptime(date_part, "%Y%m%d").replace(
            hour=int(hour_s),
            minute=int(minute_s),
            second=0,
            microsecond=0,
        )
    except ValueError:
        return None
    return closed_open, interval.lower()


def list_footprint_clip_pngs(
    out_dir: Path,
    *,
    intervals: Optional[set[str]] = None,
) -> list[tuple[Path, datetime, str]]:
    """Return footprint clip PNGs sorted by interval then closed-candle open time."""
    if not out_dir.is_dir():
        return []
    items: list[tuple[Path, datetime, str]] = []
    for path in out_dir.glob("*.png"):
        parsed = parse_footprint_clip_png_name(path.name)
        if parsed is None:
            continue
        closed_open, interval = parsed
        if intervals is not None and interval not in intervals:
            continue
        items.append((path, closed_open, interval))
    items.sort(key=lambda row: (row[2], row[1]))
    return items


def _hhmm_suffix_from_footprint_time(time_key: str) -> str:
    if _TIME_ONLY_RE.match(time_key):
        return time_key
    iso = _LEGACY_ISO_MINUTE_RE.match(time_key)
    if iso:
        return f"{int(iso.group(4)):02d}:{iso.group(5)}"
    hms = re.search(r"\b(\d{1,2}):(\d{2}):\d{2}\b", time_key)
    if hms:
        return f"{int(hms.group(1)):02d}:{hms.group(2)}"
    if " " in time_key:
        return time_key.rsplit(" ", 1)[-1]
    return time_key


def _is_legacy_hhmm_footprint_time(time_key: str) -> bool:
    return bool(_TIME_ONLY_RE.match(time_key))


def _is_legacy_footprint_time(time_key: str) -> bool:
    return bool(_TIME_ONLY_RE.match(time_key) or _LEGACY_ISO_MINUTE_RE.match(time_key))


def format_footprint_candle_time(closed_candle_open: datetime) -> str:
    """Canonical footprint candle ``time`` key (GoCharting CSV style, GMT+0700)."""
    return (
        f"{_WEEKDAY_ABBR[closed_candle_open.weekday()]} "
        f"{_MONTH_NAMES[closed_candle_open.month - 1]} "
        f"{closed_candle_open.day} "
        f"{closed_candle_open.year} "
        f"{closed_candle_open.strftime('%H:%M:%S')}"
        f"{FOOTPRINT_CANDLE_TZ_SUFFIX}"
    )


def canonical_footprint_time_key(time_key: str) -> str:
    """Normalize known dated footprint time strings to the canonical GMT+0700 key."""
    raw = (time_key or "").strip()
    if not raw:
        return ""
    if FOOTPRINT_CANDLE_TZ_SUFFIX in raw:
        return csv_time_to_footprint_key(raw)
    iso = _LEGACY_ISO_MINUTE_RE.match(raw)
    if iso:
        year, month, day, hour, minute = map(int, iso.groups())
        return format_footprint_candle_time(datetime(year, month, day, hour, minute))
    return ""


def parse_footprint_candle_datetime(time_key: str) -> Optional[datetime]:
    """Parse canonical GMT+0700 or legacy ISO-minute footprint ``time`` keys."""
    raw = (time_key or "").strip()
    if not raw or _TIME_ONLY_RE.match(raw):
        return None
    canonical = _FOOTPRINT_CANONICAL_TIME_RE.match(raw)
    if canonical:
        return datetime(
            int(canonical.group(3)),
            _MONTH_ABBR[canonical.group(1).lower()[:3]],
            int(canonical.group(2)),
            int(canonical.group(4)),
            int(canonical.group(5)),
            int(canonical.group(6)),
        )
    iso = _LEGACY_ISO_MINUTE_RE.match(raw)
    if iso:
        year, month, day, hour, minute = map(int, iso.groups())
        return datetime(year, month, day, hour, minute)
    gmt = _GOCHARTING_DATETIME_RE.search(raw)
    if gmt and FOOTPRINT_CANDLE_TZ_SUFFIX in raw:
        return datetime(
            int(gmt.group(4)),
            _MONTH_ABBR[gmt.group(1).lower()[:3]],
            int(gmt.group(2)),
            int(gmt.group(5)),
            int(gmt.group(6)),
        )
    return None


def footprint_candle_sort_key(candle: dict[str, Any]) -> tuple[int, datetime | int, str]:
    time_key = str(candle.get("time") or "").strip()
    if _TIME_ONLY_RE.match(time_key):
        hour, minute = map(int, time_key.split(":"))
        return (1, hour * 60 + minute, time_key)
    dt = parse_footprint_candle_datetime(time_key)
    if dt is not None:
        return (0, dt, time_key)
    return (2, datetime.max, time_key)


def sort_footprint_candles(candles: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort footprint candles oldest-first by parsed datetime (not string order)."""
    return sorted(
        [c for c in candles if isinstance(c, dict)],
        key=footprint_candle_sort_key,
    )


def _footprint_time_already_in_set(closed_open: datetime, existing_times: set[str]) -> bool:
    canonical = format_footprint_candle_time(closed_open)
    if canonical in existing_times:
        return True
    legacy_keys = {
        closed_open.strftime("%Y-%m-%d %H:%M"),
        closed_open.strftime("%H:%M"),
    }
    if legacy_keys & existing_times:
        return True
    return any(canonical_footprint_time_key(time_key) == canonical for time_key in existing_times)


def _drop_superseded_legacy_hhmm_candles(
    candles: Iterable[dict[str, Any]],
    *,
    full_time_key: str,
) -> list[dict[str, Any]]:
    """Remove legacy ``HH:MM`` / ISO-minute rows replaced by a canonical candle."""
    canonical = canonical_footprint_time_key(full_time_key) or full_time_key
    hhmm_suffix = _hhmm_suffix_from_footprint_time(canonical)
    if not hhmm_suffix or _is_legacy_footprint_time(full_time_key):
        return [c for c in candles if isinstance(c, dict)]
    kept: list[dict[str, Any]] = []
    for candle in candles:
        if not isinstance(candle, dict):
            continue
        time_key = str(candle.get("time") or "").strip()
        other_canonical = canonical_footprint_time_key(time_key)
        if other_canonical and other_canonical == canonical:
            continue
        if _is_legacy_hhmm_footprint_time(time_key) and time_key == hhmm_suffix:
            continue
        kept.append(candle)
    return kept


def merge_footprint_candles_by_time(
    candles: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Deduplicate candles by ``time``; keep the row with more ``price_levels``."""
    by_time: dict[str, dict[str, Any]] = {}
    for candle in candles:
        raw_time = str(candle.get("time") or "").strip()
        if not raw_time:
            continue
        time_key = canonical_footprint_time_key(raw_time) or raw_time
        levels = candle.get("price_levels")
        level_count = len(levels) if isinstance(levels, list) else 0
        existing = by_time.get(time_key)
        stored = dict(candle)
        stored["time"] = time_key
        if existing is None:
            by_time[time_key] = stored
            continue
        existing_levels = existing.get("price_levels")
        existing_count = len(existing_levels) if isinstance(existing_levels, list) else 0
        if level_count > existing_count:
            by_time[time_key] = stored
    return sort_footprint_candles(by_time.values())


def footprint_candle_has_price_levels(candle: dict[str, Any]) -> bool:
    levels = candle.get("price_levels")
    return isinstance(levels, list) and len(levels) > 0


def existing_footprint_times_with_levels(doc: dict[str, Any]) -> set[str]:
    """Return candle times in ``doc`` that already have at least one price level."""
    times: set[str] = set()
    for candle in doc.get("candles", []):
        if not isinstance(candle, dict):
            continue
        time_key = str(candle.get("time") or "").strip()
        if time_key and footprint_candle_has_price_levels(candle):
            times.add(time_key)
    return times


def batch_ocr_footprint_clip_images(
    out_dir: Path,
    *,
    ocr_api_key: str,
    symbol: str,
    image_width: int,
    split_ratio: float = 0.5,
    intervals: tuple[str, ...] = ("15m", "5m"),
    delete_image_after: bool = False,
    ocr_delay_s: float = _FOOTPRINT_OCR_BATCH_DELAY_S,
) -> dict[str, dict[str, Any]]:
    """
    OCR footprint clip PNGs missing from on-disk interval JSON and merge results.

    Skips PNGs whose candle ``time`` already exists in the interval JSON with
    at least one ``price_level``. Times present but empty are OCR'd again.
    Waits ``ocr_delay_s`` between OCR.space calls to reduce 429 rate limits.
    Candles are sorted by ``time`` with no duplicates; on duplicate times the candle
    with more ``price_levels`` wins.
    """
    interval_set = {iv.strip().lower() for iv in intervals if (iv or "").strip()}
    png_items = list_footprint_clip_pngs(
        out_dir,
        intervals=interval_set or None,
    )

    existing_docs: dict[str, dict[str, Any]] = {}
    existing_times_with_levels: dict[str, set[str]] = {}
    for interval in interval_set:
        json_path = footprint_interval_json_path(out_dir, interval)
        doc = load_footprint_document(json_path, symbol=symbol, timeframe=interval)
        existing_docs[interval] = doc
        existing_times_with_levels[interval] = existing_footprint_times_with_levels(doc)

    new_candles_by_interval: dict[str, list[dict[str, Any]]] = {
        iv: [] for iv in interval_set
    }
    ocr_calls = 0

    for path, closed_open, interval in png_items:
        existing_times = existing_times_with_levels.get(interval, set())
        if _footprint_time_already_in_set(closed_open, existing_times):
            _log.debug(
                "footprint OCR batch: skip %s | time=%s already has price_levels in JSON",
                path.name,
                format_footprint_candle_time(closed_open),
            )
            continue

        if ocr_calls > 0 and ocr_delay_s > 0:
            _log.debug("footprint OCR batch: wait %.1fs before next OCR call", ocr_delay_s)
            time.sleep(ocr_delay_s)

        candles_by_interval = new_candles_by_interval.setdefault(interval, [])
        try:
            candle = parse_footprint_candle_from_clip_image(
                path,
                api_key=ocr_api_key,
                closed_candle_open=closed_open,
                image_width=image_width,
                split_ratio=split_ratio,
            )
        except FootprintOcrSkipped as exc:
            ocr_calls += 1
            _log.warning("footprint OCR batch: skip %s | %s", path.name, exc)
            continue
        ocr_calls += 1
        candles_by_interval.append(candle)
        if delete_image_after:
            try:
                path.unlink()
                _log.debug("footprint OCR batch: deleted %s", path.name)
            except OSError:
                _log.warning(
                    "footprint OCR batch: could not delete %s",
                    path,
                    exc_info=True,
                )

    docs: dict[str, dict[str, Any]] = {}
    write_intervals = interval_set or set(new_candles_by_interval)
    for interval in sorted(write_intervals):
        prior = [
            c for c in existing_docs.get(interval, {}).get("candles", []) if isinstance(c, dict)
        ]
        new_candles = new_candles_by_interval.get(interval, [])
        for candle in new_candles:
            prior = _drop_superseded_legacy_hhmm_candles(
                prior,
                full_time_key=str(candle.get("time") or "").strip(),
            )
        merged = merge_footprint_candles_by_time([*prior, *new_candles])
        doc = new_footprint_document(symbol=symbol, timeframe=interval)
        doc["candles"] = merged
        json_path = footprint_interval_json_path(out_dir, interval)
        write_footprint_document(json_path, doc)
        docs[interval] = doc
        added = len(new_candles_by_interval.get(interval, []))
        _log.info(
            "footprint OCR batch: %s → %d candles (+%d new from OCR)",
            json_path.name,
            len(merged),
            added,
        )
    return docs


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
        c
        for c in _drop_superseded_legacy_hhmm_candles(
            doc.get("candles", []),
            full_time_key=time_key,
        )
        if str(c.get("time") or "").strip() != time_key
    ]
    candles.append(candle)
    doc["candles"] = sort_footprint_candles(candles)
    write_footprint_document(path, doc)
    return doc


def preprocess_footprint_clip_image(image: Image.Image) -> Image.Image:
    # Grayscale only — autocontrast/thresholding can erase faint footprint digits.
    return image.convert("L")


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
    """Canonical footprint candle ``time`` key (GoCharting CSV style, GMT+0700)."""
    return format_footprint_candle_time(closed_candle_open)


def csv_time_to_footprint_key(time_str: str) -> str:
    """Normalize CSV / GoCharting time strings to canonical ``... GMT+0700`` key."""
    raw = (time_str or "").strip()
    if not raw:
        return ""
    gmt = _GOCHARTING_DATETIME_RE.search(raw)
    if gmt:
        month = _MONTH_ABBR[gmt.group(1).lower()[:3]]
        day = int(gmt.group(2))
        year = int(gmt.group(3))
        hour = int(gmt.group(4))
        minute = int(gmt.group(5))
        second = 0
        sec_match = re.search(r"\b(\d{1,2}):(\d{2}):(\d{2})\b", raw)
        if sec_match:
            second = int(sec_match.group(3))
        return format_footprint_candle_time(datetime(year, month, day, hour, minute, second))
    std = re.search(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})", raw)
    if std:
        year, month, day, hour, minute = std.groups()
        return format_footprint_candle_time(
            datetime(int(year), int(month), int(day), int(hour), int(minute))
        )
    return csv_time_to_hhmm(raw)


def csv_time_to_hhmm(time_str: str) -> str:
    """``2026-06-16 10:05:00`` or GoCharting ``Tue Jun 23 2026 10:00:00 GMT+0700`` → ``10:05`` / ``10:00``."""
    raw = (time_str or "").strip()
    if not raw:
        return ""
    hms = re.search(r"\b(\d{1,2}):(\d{2}):\d{2}\b", raw)
    if hms:
        hour = int(hms.group(1))
        minute = int(hms.group(2))
        if hour <= 23 and minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    if " " in raw:
        clock = raw.split(" ", 1)[1]
        parts = clock.split(":")
        if len(parts) >= 2:
            try:
                hour = int(parts[0])
                minute = int(parts[1][:2])
                if hour <= 23 and minute <= 59:
                    return f"{hour:02d}:{minute:02d}"
            except ValueError:
                pass
    hm = _TIME_RE.search(raw)
    if hm:
        hour = int(hm.group(1))
        minute = int(hm.group(2))
        if hour <= 23 and minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    return ""


def parse_gocharting_csv_ohlc_by_hhmm(text: str) -> dict[str, dict[str, float]]:
    """Map footprint time key → ``{high, low}``; duplicate times keep the last row."""
    from automation_tool.chart_payload_validate import normalize_gocharting_csv_text

    normalized = normalize_gocharting_csv_text(text)
    by_time: dict[str, dict[str, float]] = {}
    reader = csv.DictReader(io.StringIO(normalized))
    for row in reader:
        if not row:
            continue
        time_raw = row.get("Time") or row.get("Date") or row.get("time") or ""
        time_key = csv_time_to_footprint_key(str(time_raw))
        if not time_key:
            continue
        try:
            high = float(row.get("High") or row.get("high"))
            low = float(row.get("Low") or row.get("low"))
        except (TypeError, ValueError):
            continue
        by_time[time_key] = {"high": high, "low": low}
    return by_time


def _ohlc_index_by_hhmm(
    ohlc_by_time: dict[str, dict[str, float]],
) -> dict[str, dict[str, float]]:
    """Legacy ``HH:MM`` index for candles written before canonical GMT+0700 keys."""
    by_hhmm: dict[str, dict[str, float]] = {}
    for time_key, ohlc in ohlc_by_time.items():
        by_hhmm[_hhmm_suffix_from_footprint_time(time_key)] = ohlc
    return by_hhmm


def _lookup_ohlc_for_candle_time(
    time_key: str,
    ohlc_by_time: dict[str, dict[str, float]],
    ohlc_by_hhmm: dict[str, dict[str, float]],
) -> Optional[dict[str, float]]:
    canonical = canonical_footprint_time_key(time_key)
    if canonical:
        ohlc = ohlc_by_time.get(canonical)
        if ohlc is not None:
            return ohlc
    ohlc = ohlc_by_time.get(time_key)
    if ohlc is not None:
        return ohlc
    if _is_legacy_footprint_time(time_key):
        return ohlc_by_hhmm.get(_hhmm_suffix_from_footprint_time(time_key))
    return None


DEFAULT_FOOTPRINT_BLOCK_SIZE = 0.4


def footprint_block_size(*, gocharting_yaml: Optional[Path] = None) -> float:
    """GoCharting Tick Manager cluster height: ``tick_size × block_multiplier`` (default 0.4)."""
    try:
        from automation_tool.config import default_gocharting_config_path
        from automation_tool.gocharting_capture import load_gocharting_yaml

        yaml_path = gocharting_yaml or default_gocharting_config_path()
        cfg = load_gocharting_yaml(yaml_path)
        raw = cfg.get("footprint_screenshot")
        if isinstance(raw, dict):
            if raw.get("block_size") is not None:
                return float(raw["block_size"])
            tick = float(raw.get("tick_size", 0.1))
            block = float(raw.get("block_multiplier", 4))
            return tick * block
    except Exception:
        pass
    return DEFAULT_FOOTPRINT_BLOCK_SIZE


def snap_high_to_footprint_block(high: float, block_size: float) -> float:
    """Snap CSV high down to the top footprint cluster boundary."""
    if block_size <= 0:
        return round(high, 1)
    top = math.floor((high + 1e-9) / block_size) * block_size
    return round(top, 1)


def compute_footprint_level_prices(
    high: float,
    n: int,
    *,
    block_size: float = DEFAULT_FOOTPRINT_BLOCK_SIZE,
) -> list[float]:
    """Top→bottom prices: CSV high snapped to block grid, then ``- i × block_size``."""
    if n <= 0:
        return []
    top = snap_high_to_footprint_block(high, block_size)
    return [round(top - i * block_size, 1) for i in range(n)]


DEFAULT_FOOTPRINT_OPENAI_MAX_CANDLES = 100


def footprint_openai_max_candles() -> int:
    raw = os.getenv("FOOTPRINT_OPENAI_MAX_CANDLES", "").strip()
    if raw.isdigit():
        return max(1, int(raw))
    return DEFAULT_FOOTPRINT_OPENAI_MAX_CANDLES


def trim_footprint_bid_ask_document(
    doc: dict[str, Any],
    *,
    max_candles: int | None = None,
) -> dict[str, Any]:
    """Keep at most ``max_candles`` newest footprint candles (on-disk list is oldest-first)."""
    mc = max_candles if max_candles is not None else footprint_openai_max_candles()
    if mc <= 0:
        return doc
    candles_raw = doc.get("candles")
    if not isinstance(candles_raw, list) or len(candles_raw) <= mc:
        return doc
    out = dict(doc)
    out["candles"] = candles_raw[-mc:]
    return out


def enrich_footprint_bid_ask_document(
    doc: dict[str, Any],
    csv_path: Path,
    *,
    block_size: float | None = None,
) -> dict[str, Any]:
    """Add ``price`` to each ``price_levels`` item using CSV High + Tick Manager block size."""
    bs = block_size if block_size is not None else footprint_block_size()
    try:
        csv_text = csv_path.read_text(encoding="utf-8")
    except OSError as exc:
        _log.debug("footprint enrich: cannot read CSV %s: %s", csv_path, exc)
        return doc

    ohlc_by_time = parse_gocharting_csv_ohlc_by_hhmm(csv_text)
    if not ohlc_by_time:
        _log.debug("footprint enrich: no OHLC rows in %s", csv_path)
        return doc
    ohlc_by_hhmm = _ohlc_index_by_hhmm(ohlc_by_time)

    candles_raw = doc.get("candles")
    if not isinstance(candles_raw, list):
        return doc

    out = dict(doc)
    enriched_candles: list[dict[str, Any]] = []
    for candle in candles_raw:
        if not isinstance(candle, dict):
            enriched_candles.append(candle)
            continue
        time_key = str(candle.get("time") or "").strip()
        levels_raw = candle.get("price_levels")
        if not isinstance(levels_raw, list):
            enriched_candles.append(dict(candle))
            continue
        ohlc = _lookup_ohlc_for_candle_time(time_key, ohlc_by_time, ohlc_by_hhmm)
        if ohlc is None:
            _log.warning(
                "footprint enrich: no CSV row for candle time=%s in %s",
                time_key,
                csv_path.name,
            )
            enriched_candles.append(dict(candle))
            continue
        high = ohlc["high"]
        n = len(levels_raw)
        if n == 0:
            _log.warning("footprint enrich: empty price_levels for time=%s", time_key)
            enriched_candles.append(dict(candle))
            continue
        prices = compute_footprint_level_prices(high, n, block_size=bs)
        new_levels: list[dict[str, Any]] = []
        for i, level in enumerate(levels_raw):
            if not isinstance(level, dict):
                new_levels.append(level)
                continue
            new_level = dict(level)
            new_level["price"] = prices[i]
            new_levels.append(new_level)
        new_candle = dict(candle)
        new_candle["price_levels"] = new_levels
        enriched_candles.append(new_candle)
    out["candles"] = enriched_candles
    return out


def charts_dir_from_footprint_json_path(json_path: Path) -> Optional[Path]:
    """``charts_dir/footprint_images/footprint_bid_ask_*.json`` → ``charts_dir``."""
    parent = json_path.parent
    if parent.name == "footprint_images":
        return parent.parent
    return None


def resolve_gocharting_csv_for_footprint_json(
    json_path: Path,
    *,
    charts_dir: Optional[Path] = None,
    stamp: Optional[str] = None,
) -> Optional[Path]:
    if not json_path.name.startswith("footprint_bid_ask_") or json_path.suffix.lower() != ".json":
        return None
    iv = json_path.stem.replace("footprint_bid_ask_", "")
    if not iv:
        return None
    cd = charts_dir or charts_dir_from_footprint_json_path(json_path)
    if cd is None or not cd.is_dir():
        return None
    from automation_tool.images import gocharting_main_interval_csv_path

    return gocharting_main_interval_csv_path(cd, iv, stamp=stamp)


def _sanitize_parsed_text(text: str) -> str:
    raw = (text or "").strip()
    if not raw or _NO_TEXT_DETECTED_RE.search(raw):
        return ""
    return raw


def _price_levels_from_ocr_payload(
    payload: dict[str, Any],
    *,
    split_ratio: float = 0.5,
    image_width: Optional[int] = None,
) -> tuple[list[dict[str, int]], str, int]:
    lines = _iter_overlay_lines(payload)
    parsed_text = ""
    results = payload.get("ParsedResults") or []
    if results and isinstance(results[0], dict):
        parsed_text = _sanitize_parsed_text(str(results[0].get("ParsedText") or ""))
    ocr_width = image_width
    if ocr_width is None and lines:
        max_right = 0
        for word in _iter_overlay_words(lines):
            try:
                left = int(word.get("Left") or 0)
                width = int(word.get("Width") or 0)
            except (TypeError, ValueError):
                continue
            max_right = max(max_right, left + width)
        ocr_width = max(max_right, 1)
    price_levels = parse_price_levels_from_overlay(
        lines=lines,
        parsed_text=parsed_text,
        image_width=ocr_width,
        split_ratio=split_ratio,
    )
    return price_levels, parsed_text, len(lines)


def _footprint_ocr_images(image_path: Path) -> list[tuple[str, Image.Image]]:
    """Grayscale first; retry raw RGB if OCR yields no bid/ask rows."""
    with Image.open(image_path) as raw:
        rgb = raw.convert("RGB")
        gray = preprocess_footprint_clip_image(rgb)
    return [("grayscale", gray), ("rgb", rgb)]


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


def _iter_overlay_words(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    for line in lines:
        for word in line.get("Words") or []:
            if isinstance(word, dict):
                words.append(word)
    return words


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


def parse_price_levels_from_overlay_words(
    words: list[dict[str, Any]],
    *,
    image_width: int = 0,
    split_ratio: float = 0.5,
    footer_cutoff_top: Optional[int] = None,
) -> list[dict[str, int]]:
    """Pair left/right OCR words into bid/ask rows when digits are split by column."""
    del image_width, split_ratio
    rows: dict[int, list[tuple[int, int]]] = {}

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
        if footer_cutoff_top is not None and top >= footer_cutoff_top:
            continue
        bucket = (top // _ROW_BUCKET_PX) * _ROW_BUCKET_PX
        rows.setdefault(bucket, []).append((left, value))

    levels: list[dict[str, int]] = []
    for bucket in sorted(rows.keys()):
        cells = sorted(rows[bucket], key=lambda item: item[0])
        if not cells:
            continue
        bid = cells[0][1]
        ask = cells[-1][1] if len(cells) > 1 else 0
        if bid == 0 and ask == 0:
            continue
        levels.append({"bid": bid, "ask": ask})
    return levels


def parse_price_levels_from_overlay(
    *,
    lines: Optional[list[dict[str, Any]]] = None,
    parsed_text: str = "",
    image_width: Optional[int] = None,
    split_ratio: float = 0.5,
) -> list[dict[str, int]]:
    """Accept ``bid ask`` pair lines, else pair split left/right OCR words by column."""
    if lines:
        text_line_levels = parse_price_levels_from_text_lines(lines)
        if text_line_levels:
            return text_line_levels
    text_levels = parse_price_levels_from_parsed_text(parsed_text)
    if text_levels:
        return text_levels
    if lines:
        footer_cutoff = _footer_cutoff_top(lines)
        word_levels = parse_price_levels_from_overlay_words(
            _iter_overlay_words(lines),
            footer_cutoff_top=footer_cutoff,
        )
        if word_levels:
            return word_levels
    return []


def parse_footprint_candle_from_clip_image(
    image_path: Path,
    *,
    api_key: str,
    closed_candle_open: datetime,
    image_width: int,
    split_ratio: float = 0.5,
) -> dict[str, Any]:
    del image_width
    last_parsed_text = ""
    last_line_count = 0
    last_size: tuple[int, int] | None = None

    for attempt_label, ocr_image in _footprint_ocr_images(image_path):
        payload = ocr_space_parse_pil_image(
            ocr_image,
            filename=image_path.name,
            api_key=api_key,
        )
        price_levels, parsed_text, line_count = _price_levels_from_ocr_payload(
            payload,
            split_ratio=split_ratio,
            image_width=ocr_image.size[0],
        )
        last_parsed_text = parsed_text
        last_line_count = line_count
        last_size = ocr_image.size
        if price_levels:
            if attempt_label != "grayscale":
                _log.info(
                    "footprint OCR: %s succeeded on %s retry",
                    image_path.name,
                    attempt_label,
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

    raise FootprintOcrSkipped(
        f"no bid/ask pair lines in OCR for {image_path.name} "
        f"(overlay_lines={last_line_count}, parsed_text={last_parsed_text[:80]!r}, "
        f"size={last_size})"
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
        parsed_text = _sanitize_parsed_text(str(results[0].get("ParsedText") or ""))
    time_val = closed_candle_time_hhmm(closed_candle_open)
    price_levels, _, _ = _price_levels_from_ocr_payload(
        ocr_payload,
        split_ratio=split_ratio,
        image_width=image_width,
    )
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
