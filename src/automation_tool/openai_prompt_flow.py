from __future__ import annotations

"""
OpenAI Responses: user turns are short routing tags + context.

Full trading/output rules live in ``system-prompt.md`` at the repo root (loaded via
``automation_tool.prompts``). User messages carry mode tags like ``[FULL_ANALYSIS]``.
"""

import base64
import json
import logging
import os
import re
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, NamedTuple, Optional

from openai import OpenAI

from automation_tool.coinmap_openai_slim import (
    should_slim_coinmap_json_path,
    slim_coinmap_export_for_openai,
)
from automation_tool.chart_payload_validate import prepare_gocharting_csv_text
from automation_tool.prompts import load_last_filter, responses_input_messages
from automation_tool.images import (
    CHART_SLOT_COUNT,
    DEFAULT_MAIN_CHART_SYMBOL,
    GOCHARTING_GOLD_EXPORT_LABEL,
    OPENAI_PAYLOAD_MAX,
    ChartOpenAIPayload,
    _detail_back_step_from_stem,
    chunk_payloads,
    image_to_data_url,
    normalize_main_chart_symbol,
    openai_payloads_for_attachment_paths,
    ordered_chart_openai_payloads,
    read_main_chart_symbol,
)
from automation_tool.state_files import MORNING_FULL_ANALYSIS_FILENAME
from automation_tool.zones_state import format_intraday_update_time_line

_log = logging.getLogger(__name__)

DEFAULT_REASONING_EFFORT = "medium"
ALL_FLOW_REASONING_EFFORT = "high"

# GoCharting footprint for main gold pair: COMEX GC1! future, not spot XAUUSD.
GOCHARTING_GOLD_FUTURE_LABEL = "Gold Future (GC1!)"

_GOCHARTING_BID_ASK_HINT = (
    "GoCharting CSV export chỉ có OHLC, Volume, Delta, CVD theo nến — "
    "KHÔNG có BID/ASK theo từng price level (stacked BID/ASK, volume bid/ask từng mức, RL). "
    "Stacked BID/ASK, absorption, RL: đọc từ footprint_combined_15m.json và footprint_combined_5m.json "
    "(mỗi candle: ohlc + footprint[] với buy/sell volume theo price level), không suy từ CSV.\n"
)

_GOCHARTING_CHART_READ_GUIDE = (
    "Hướng dẫn đọc footprint GoCharting (JSON combined + overview PNG):\n"
    "- footprint[].buy.volume / footprint[].sell.volume = BID/ASK volume theo price level.\n"
    "- totals / ending_summary: delta, CVD, high/low theo nến.\n"
    "- ohlc trên mỗi candle khớp TS/V2; CSV GoCharting bổ sung CVD/delta khi cần.\n"
)

_GOCHARTING_TRADE_MANAGEMENT_SUFFIX = (
    "Stacked BID/ASK, absorption, RL: đọc từ footprint_combined_5m.json (footprint[]), không từ CSV.\n"
    f"{_GOCHARTING_CHART_READ_GUIDE}"
)


def _gocharting_main_footprint_label(main_symbol: str) -> str:
    sym = (main_symbol or "").strip().upper()
    if sym == "XAUUSD":
        return GOCHARTING_GOLD_FUTURE_LABEL
    return sym or DEFAULT_MAIN_CHART_SYMBOL


def _gocharting_gold_future_slot_note(path: Path) -> str:
    if f"_gocharting_{GOCHARTING_GOLD_EXPORT_LABEL}_" in path.name:
        return (
            "Instrument: Gold Future (GC1!) — COMEX gold futures footprint; "
            "not spot XAUUSD.\n"
        )
    return ""


def _gocharting_attachment_note(path: Path) -> str:
    """Slot-level note for GoCharting CSV only; PNG headers stay brief to avoid repetition."""
    parts: list[str] = []
    gold = _gocharting_gold_future_slot_note(path)
    if gold:
        parts.append(gold.rstrip())
    parts.append(_GOCHARTING_BID_ASK_HINT.rstrip())
    return "\n".join(parts) + "\n" if parts else ""


class PromptTwoStepResult(NamedTuple):
    """``first_text`` luôn rỗng; ``after_charts`` chứa toàn bộ output phân tích (một hoặc nhiều batch)."""

    first_text: str
    after_charts: str
    final_response_id: str

    def full_text(self) -> str:
        if not self.after_charts:
            return self.first_text
        return f"{self.first_text}\n\n---\n\n{self.after_charts}" if self.first_text else self.after_charts


def default_analysis_prompt(
    main_symbol: str | None = None,
    *,
    footprint_source: str = "coinmap",
) -> str:
    """
    Default user message for multimodal analysis.

    ``main_symbol`` is the main pair (TradingView/Coinmap); invalid/empty →
    ``DEFAULT_MAIN_CHART_SYMBOL``. Schema and rules are in ``system-prompt.md`` (``[FULL_ANALYSIS]`` → Schema A).
    """
    sym = DEFAULT_MAIN_CHART_SYMBOL
    if main_symbol and str(main_symbol).strip():
        try:
            sym = normalize_main_chart_symbol(str(main_symbol).strip())
        except ValueError:
            pass
    fp = (footprint_source or "coinmap").strip().lower()
    if fp == "gocharting":
        footprint_desc = (
            f"({CHART_SLOT_COUNT} slot chart; mỗi slot GoCharting: CSV orderflow + PNG overview):\n"
            "TradingView DXY (H4, H1, M15) → "
            f"TradingView {sym} (H4, H1, M15, M15 Session Liquidity Check / ICT Killzones, M5) "
            "(snapshot URL/PNG hoặc JSON OHLC tvdatafeed) → "
            "GoCharting DXY M15 (CSV; PNG overview) → "
            f"GoCharting {_gocharting_main_footprint_label(sym)} M15 và M5 "
            "(footprint hợp đồng tương lai vàng GC1! trên GoCharting; không phải spot XAUUSD; "
            "mỗi khung: CSV + PNG overview) → "
            f"MT5 spot {sym} OHLC (broker) nằm trong footprint_combined JSON "
            "(mỗi nến: ``XAUUSD_OHLC``) → "
            "footprint_combined_15m.json và footprint_combined_5m.json (WS: GC ohlc + footprint[] + spot OHLC, "
            "N nến mới nhất).\n"
            "Ưu tiên footprint_combined JSON cho stacked BID/ASK, absorption, RL theo price level; "
            "GoCharting CSV cho CVD/delta/volume theo nến; OHLC tvdatafeed khi cần cấu trúc giá TV.\n"
            f"{_GOCHARTING_CHART_READ_GUIDE}"
        )
    else:
        footprint_desc = (
            f"({CHART_SLOT_COUNT} slot chart; mỗi slot Coinmap có thể kèm JSON cm-api rồi ảnh fullscreen PNG ngay sau):\n"
            "TradingView DXY (H4, H1, M15) → "
            f"TradingView {sym} (H4, H1, M15, M15 Session Liquidity Check / ICT Killzones, M5) "
            "(snapshot URL/PNG hoặc JSON OHLC tvdatafeed) → "
            "Coinmap DXY M15 (JSON footprint; PNG fullscreen ngay sau nếu có) → "
            f"Coinmap {sym} M15 và M5 (mỗi khung: JSON; PNG ngay sau nếu có; hoặc merged JSON thay M15+M5, PNG M5 vẫn riêng) → "
            "footprint_bid_ask_15m.json và footprint_bid_ask_5m.json (nếu có — bid/ask GC1! theo price level).\n"
            "Ưu tiên đọc ảnh chart (Coinmap PNG, TradingView snapshot); khi dữ liệu không rõ, "
            "không đọc được trên chart, hoặc cần con số chính xác (order flow, CVD, VWAP, delta, OHLC) "
            "thì tra JSON cm-api / tvdatafeed tương ứng.\n"
        )
    return (
        "[FULL_ANALYSIS]\n"
        f"Cặp chính: {sym}.\n"
        f"Đính kèm theo thứ tự, tối đa {OPENAI_PAYLOAD_MAX} payload multimodal "
        f"{footprint_desc}"
    )


# Tương thích ngược: prompt mặc định khi cặp = XAUUSD
DEFAULT_ANALYSIS_PROMPT = default_analysis_prompt(DEFAULT_MAIN_CHART_SYMBOL)
DEFAULT_FIRST_PROMPT = DEFAULT_ANALYSIS_PROMPT
DEFAULT_FOLLOW_UP_PROMPT = ""


def _default_max_coinmap_json_chars() -> int:
    raw = os.getenv("COINMAP_JSON_MAX_CHARS", "").strip()
    if raw.isdigit():
        return max(0, int(raw))
    return 1_500_000


def _max_json_chars_for_path(path: Path, *, default_max: int) -> int:
    """Optional cap for ``morning_full_analysis.json``; Coinmap/TV use ``default_max``."""
    if path.name == MORNING_FULL_ANALYSIS_FILENAME:
        raw = os.getenv("MORNING_FULL_ANALYSIS_MAX_CHARS", "").strip()
        if raw.isdigit():
            return max(0, int(raw))
        return default_max
    return default_max


def _coinmap_openai_slim_enabled() -> bool:
    """
    Extra slim when *reading* JSON for the API. Default off: exports are already slimmed
    on disk when ``api_data_export.slim_export_on_disk`` is true. Set COINMAP_OPENAI_SLIM=true
    to slim again (e.g. old full-size files on disk).
    """
    raw = os.getenv("COINMAP_OPENAI_SLIM", "").strip().lower()
    if not raw:
        return False
    return raw not in ("0", "false", "no")


def _default_max_gocharting_csv_chars() -> int:
    raw = os.getenv("GOCHARTING_CSV_MAX_CHARS", "").strip()
    if raw.isdigit():
        return max(0, int(raw))
    return 1_500_000


def _max_csv_chars_for_path(path: Path, *, default_max: int) -> int:
    if "_gocharting_" in path.name:
        return default_max
    return default_max


def _csv_file_header_and_body(path: Path, *, max_chars: int) -> tuple[str, str]:
    body = prepare_gocharting_csv_text(path.read_text(encoding="utf-8"))
    note = _gocharting_attachment_note(path)
    header = f"[GoCharting orderflow CSV — file: {path.name}]\n{note}"
    if max_chars > 0 and len(body) > max_chars:
        body = body[:max_chars] + f"\n… [truncated: raise GOCHARTING_CSV_MAX_CHARS]"
    return header, body


def _json_file_header_and_body(
    path: Path,
    *,
    max_chars: int,
    chart_stamp: str | None = None,
) -> tuple[str, str]:
    """
    Header (input_text) + body string for upload. Slim only Coinmap paths when enabled;
    TradingView JSON is compacted but not passed through ``slim_coinmap_export_for_openai``.
    """
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
        if (
            _coinmap_openai_slim_enabled()
            and isinstance(data, dict)
            and should_slim_coinmap_json_path(path)
        ):
            data = slim_coinmap_export_for_openai(data, path=path)
        if (
            isinstance(data, dict)
            and path.name.startswith("footprint_combined_")
            and path.suffix.lower() == ".json"
        ):
            from automation_tool.gocharting_ws_decode import (
                footprint_ws_max_candles_from_cfg,
                trim_footprint_document,
            )
            from automation_tool.images import _default_gocharting_cfg

            cfg = _default_gocharting_cfg()
            data = trim_footprint_document(
                data,
                max_candles=footprint_ws_max_candles_from_cfg(cfg),
            )
        if (
            isinstance(data, dict)
            and path.name.startswith("footprint_bid_ask_")
            and path.suffix.lower() == ".json"
        ):
            from automation_tool.gocharting_footprint_ocr import (
                charts_dir_from_footprint_json_path,
                enrich_footprint_bid_ask_document,
                resolve_gocharting_csv_for_footprint_json,
                trim_footprint_bid_ask_document,
            )

            data = trim_footprint_bid_ask_document(data)
            charts_dir = charts_dir_from_footprint_json_path(path)
            csv_path = resolve_gocharting_csv_for_footprint_json(
                path,
                charts_dir=charts_dir,
                stamp=chart_stamp,
            )
            if csv_path is not None:
                data = enrich_footprint_bid_ask_document(data, csv_path)
        compact = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    except json.JSONDecodeError:
        compact = raw
    if path.name == MORNING_FULL_ANALYSIS_FILENAME:
        header = f"[FULL_ANALYSIS snapshot — file: {path.name}]\n"
    elif "_tradingview_" in path.name:
        if "_15m_ict" in path.name:
            header = (
                "[TradingView M15 Session Liquidity Check / ICT Killzones "
                f"(use to check session liquidity pools/sweeps) — file: {path.name}]\n"
            )
        else:
            header = f"[TradingView OHLC (tvdatafeed) — file: {path.name}]\n"
    elif "_mt5_" in path.name:
        header = (
            f"[MT5 spot OHLC (broker execution price) — file: {path.name}]\n"
            "Instrument: spot XAUUSD on broker MT5 (not GC1! futures footprint).\n"
            "Bar times (`t`) and generated_at are Asia/Ho_Chi_Minh (UTC+7).\n"
        )
    elif path.name.startswith("footprint_combined_") and path.suffix.lower() == ".json":
        iv = path.stem.replace("footprint_combined_", "")
        header = (
            f"[GoCharting footprint combined — {iv} — file: {path.name}]\n"
            "Instrument: COMEX:GC1! futures. Each candle: time_gmt7, ohlc (GC1!), "
            "XAUUSD_OHLC (spot broker), footprint[] "
            "with buy/sell volume per price level (latest N candles from WS capture).\n"
        )
    elif path.name.startswith("footprint_bid_ask_") and path.suffix.lower() == ".json":
        iv = path.stem.replace("footprint_bid_ask_", "")
        header = (
            f"[GoCharting Bid/Ask footprint — {iv} — file: {path.name}]\n"
            "Instrument: COMEX:GC1! futures. Each candle: time (HH:MM) + price_levels "
            "[{bid, ask, price}, ...] top→bottom per closed bar "
            "(price from CSV High snapped to GoCharting Tick Manager block cluster, default 0.4).\n"
        )
    elif "_openai_coinmap_merged" in path.name or path.name.endswith("_merged.json"):
        header = f"[Coinmap merged analysis — file: {path.name}]\n"
    else:
        header = f"[Coinmap API export — file: {path.name}]\n"
    body = compact
    if max_chars > 0 and len(body) > max_chars:
        body = body[:max_chars] + f"\n… [truncated: {len(compact)} chars → {max_chars}; raise COINMAP_JSON_MAX_CHARS]"
    return header, body


def _json_text_to_base64_data_url(body: str) -> str:
    """
    Convert a JSON string (already compacted/truncated) to a base64 data URL suitable for
    OpenAI Responses ``input_file.file_data``.
    """
    raw = body.encode("utf-8")
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:application/json;base64,{b64}"


def _prepare_json_headers_bodies(
    paths: list[Path],
    *,
    max_json_chars: int,
    chart_stamp: str | None = None,
) -> list[tuple[str, str]]:
    """Return ``(header, body)`` per path, same order as ``paths``."""
    if not paths:
        return []
    if len(paths) == 1:
        p0 = paths[0]
        mx0 = _max_json_chars_for_path(p0, default_max=max_json_chars)
        return [_json_file_header_and_body(p0, max_chars=mx0, chart_stamp=chart_stamp)]
    n = len(paths)
    workers = min(n, max(4, (os.cpu_count() or 2) * 2))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(
            ex.map(
                lambda pp: _json_file_header_and_body(
                    pp,
                    max_chars=_max_json_chars_for_path(pp, default_max=max_json_chars),
                    chart_stamp=chart_stamp,
                ),
                paths,
            )
        )


def _filter_valid_chart_payloads(
    payloads: list[ChartOpenAIPayload],
) -> list[ChartOpenAIPayload]:
    """Keep json/image files on disk and ``image_url`` https strings."""
    out: list[ChartOpenAIPayload] = []
    for k, p in payloads:
        if k == "image_url":
            if isinstance(p, str) and p.strip().lower().startswith("http"):
                out.append((k, p.strip()))
        elif isinstance(p, Path) and p.is_file():
            out.append((k, p))
    return out


def _image_paths_to_data_urls(paths: list[Path]) -> dict[Path, str]:
    """
    Encode ảnh sang data URL; nhiều file thì đọc + base64 song song (I/O-bound).
    Trùng path chỉ encode một lần.
    """
    if not paths:
        return {}
    unique = list(dict.fromkeys(paths))
    if len(unique) == 1:
        p0 = unique[0]
        return {p0: image_to_data_url(p0)}
    n = len(unique)
    workers = min(n, max(4, (os.cpu_count() or 2) * 2))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        urls = ex.map(image_to_data_url, unique)
    return dict(zip(unique, urls))


def _gocharting_detail_png_attachment_header(path: Path) -> Optional[str]:
    if path.suffix.lower() != ".png" or "_gocharting_" not in path.name:
        return None
    if "_detail_" not in path.name:
        return None
    from automation_tool.gocharting_image_crop import (
        GOCHARTING_IMAGE_WIDTH_THIRDS,
        gocharting_detail_crop_part_index,
    )

    stem = path.stem
    part = gocharting_detail_crop_part_index(path)
    if part is not None:
        base_stem = re.sub(r"_part\d+$", "", stem)
        if base_stem.endswith("_detail_zoom"):
            kind = "detail footprint zoomed in (current session)"
        else:
            back_step = _detail_back_step_from_stem(base_stem)
            if back_step is not None:
                kind = f"detail footprint — history pan step {back_step}"
            else:
                kind = "detail footprint"
        return (
            f"[GoCharting {kind} — panel {part}/{GOCHARTING_IMAGE_WIDTH_THIRDS} "
            f"— file: {path.name}]\n"
        )
    if stem.endswith("_detail_zoom"):
        kind = "detail footprint zoomed in (current session)"
    else:
        back_step = _detail_back_step_from_stem(stem)
        if back_step is not None:
            kind = f"detail footprint — history pan step {back_step}"
        else:
            kind = "detail footprint"
    return f"[GoCharting {kind} — file: {path.name}]\n"


def _gocharting_png_attachment_header(path: Path) -> Optional[str]:
    if path.suffix.lower() != ".png" or "_gocharting_" not in path.name:
        return None
    if "_detail_" in path.name:
        return _gocharting_detail_png_attachment_header(path)
    return f"[GoCharting chart screenshot — file: {path.name}]\n"


def _coinmap_png_attachment_header(path: Path) -> Optional[str]:
    if path.suffix.lower() != ".png" or "_coinmap_" not in path.name:
        return None
    return f"[Coinmap fullscreen chart — file: {path.name}]\n"


def _should_attach_last_filter(prompt: str) -> bool:
    """Attach ``last_filter.md`` for modes that publish zone plans."""
    p = (prompt or "").strip().upper()
    return p.startswith("[FULL_ANALYSIS]") or "[INTRADAY_UPDATE]" in p


def _last_filter_input_parts() -> list[dict[str, Any]]:
    try:
        body = load_last_filter()
    except FileNotFoundError:
        return []
    text = (body or "").strip()
    if not text:
        return []
    return [
        {
            "type": "input_text",
            "text": f"[Zone filter rules — last_filter.md]\n{text}\n",
        }
    ]


def _user_content_with_last_filter(prompt: str) -> str | list[dict[str, Any]]:
    """Plain-text user turn, optionally expanded to multimodal parts with last_filter."""
    text = (prompt or "").strip()
    if not text:
        return text
    if not _should_attach_last_filter(text):
        return text
    parts = _last_filter_input_parts()
    if not parts:
        return text
    return [{"type": "input_text", "text": text}, *parts]


def _build_mixed_chart_user_content(
    prompt: str,
    payloads: list[ChartOpenAIPayload],
    *,
    max_json_chars: int,
    chart_stamp: str | None = None,
) -> list[dict[str, Any]]:
    json_paths = [p for k, p in payloads if k == "json" and isinstance(p, Path)]
    csv_paths = [p for k, p in payloads if k == "csv" and isinstance(p, Path)]
    json_queue = iter(
        _prepare_json_headers_bodies(
            json_paths,
            max_json_chars=max_json_chars,
            chart_stamp=chart_stamp,
        )
    )
    mx_csv = _default_max_gocharting_csv_chars()
    csv_queue = iter(
        [
            _csv_file_header_and_body(
                p, max_chars=_max_csv_chars_for_path(p, default_max=mx_csv)
            )
            for p in csv_paths
        ]
    )
    parts: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    if _should_attach_last_filter(prompt):
        parts.extend(_last_filter_input_parts())
    image_paths = [p for k, p in payloads if k == "image" and isinstance(p, Path)]
    data_urls = _image_paths_to_data_urls(image_paths)
    for kind, p in payloads:
        if kind == "json":
            assert isinstance(p, Path)
            h, body = next(json_queue)
            parts.append({"type": "input_text", "text": h})
            if should_slim_coinmap_json_path(p):
                parts.append(
                    {
                        "type": "input_file",
                        "filename": p.name,
                        "file_data": _json_text_to_base64_data_url(body),
                    }
                )
            else:
                parts.append({"type": "input_text", "text": body})
        elif kind == "csv":
            assert isinstance(p, Path)
            h, body = next(csv_queue)
            parts.append({"type": "input_text", "text": h})
            parts.append({"type": "input_text", "text": body})
        elif kind == "image_url":
            parts.append(
                {
                    "type": "input_image",
                    "image_url": str(p),
                    "detail": "auto",
                }
            )
        else:
            assert isinstance(p, Path)
            hdr = _gocharting_png_attachment_header(p) or _coinmap_png_attachment_header(p)
            if hdr:
                parts.append({"type": "input_text", "text": hdr})
            parts.append(
                {
                    "type": "input_image",
                    "image_url": data_urls[p],
                    "detail": "auto",
                }
            )
    return parts


def _merge_model(common: dict[str, Any], model: str | None) -> None:
    m = (model or "").strip()
    if not m:
        raise ValueError(
            "OpenAI model is required (set OPENAI_MODEL or pass --model). "
            "Dashboard prompt objects are no longer used."
        )
    common["model"] = m


def _payload_counts(payloads: Sequence[ChartOpenAIPayload]) -> tuple[int, int, int, int]:
    json_count = sum(1 for kind, _ in payloads if kind == "json")
    csv_count = sum(1 for kind, _ in payloads if kind == "csv")
    image_count = sum(1 for kind, _ in payloads if kind == "image")
    image_url_count = sum(1 for kind, _ in payloads if kind == "image_url")
    return json_count, csv_count, image_count, image_url_count


def _log_openai_send(
    *,
    flow: str,
    batch_index: int,
    total_batches: int,
    payloads: Sequence[ChartOpenAIPayload],
    model: str | None,
    chained: bool,
) -> None:
    json_count, csv_count, image_count, image_url_count = _payload_counts(payloads)
    _log.info(
        (
            "OpenAI: đã gửi data lên OpenAI | flow=%s batch=%d/%d "
            "payloads=%d json=%d csv=%d image=%d image_url=%d model=%s chained=%s"
        ),
        flow,
        batch_index,
        total_batches,
        len(payloads),
        json_count,
        csv_count,
        image_count,
        image_url_count,
        (model or "").strip() or "?",
        chained,
    )


def _log_openai_receive(
    *,
    flow: str,
    batch_index: int,
    total_batches: int,
    response_id: str,
    output_text: str,
) -> None:
    _log.info(
        (
            "OpenAI: đã nhận data từ OpenAI | flow=%s batch=%d/%d "
            "response_id=%s output_chars=%d"
        ),
        flow,
        batch_index,
        total_batches,
        response_id,
        len(output_text or ""),
    )


def _log_openai_error(
    *,
    flow: str,
    batch_index: int,
    total_batches: int,
    payloads: Sequence[ChartOpenAIPayload],
) -> None:
    json_count, csv_count, image_count, image_url_count = _payload_counts(payloads)
    _log.exception(
        (
            "OpenAI: lỗi khi gửi/nhận data từ OpenAI | flow=%s batch=%d/%d "
            "payloads=%d json=%d csv=%d image=%d image_url=%d"
        ),
        flow,
        batch_index,
        total_batches,
        len(payloads),
        json_count,
        csv_count,
        image_count,
        image_url_count,
    )


def run_analysis_responses_flow(
    *,
    api_key: str,
    charts_dir: Path,
    analysis_prompt: str,
    max_images_per_call: int,
    vector_store_ids: list[str],
    store: bool,
    include: list[str],
    reasoning_summary: str = "auto",
    reasoning_effort: str | None = DEFAULT_REASONING_EFFORT,
    chart_paths: list[Path] | None = None,
    chart_payloads: list[ChartOpenAIPayload] | None = None,
    max_coinmap_json_chars: int | None = None,
    on_first_model_text: Optional[Callable[[str], None]] = None,
    purge_json_attachment_storage: bool = False,
    purge_openai_user_data_files: bool | None = None,
    model: str | None = None,
    chart_stamp: str | None = None,
) -> PromptTwoStepResult:
    """
    Một lần (hoặc nhiều batch nếu quá nhiều ảnh): user message multimodal với ``analysis_prompt``
    + chart payloads, không còn bước text-only tách biệt.

    ``after_charts`` chứa toàn bộ output; ``first_text`` luôn ``""``.

    ``on_first_model_text`` (tuỳ chọn): gọi với text assistant của **batch đầu tiên**
    (khi có multimodal); dùng cho VÀO LỆNH + MT5 / cập nhật ``last_alert_prices``.

    ``purge_json_attachment_storage`` / ``purge_openai_user_data_files``: legacy flags from the
    former Cloudinary-based JSON attachment path. JSON is now attached inline (text) or as
    base64 ``input_file.file_data`` (Coinmap raw exports), so these are effectively no-ops.
    """
    if not (analysis_prompt or "").strip():
        analysis_prompt = default_analysis_prompt(read_main_chart_symbol(charts_dir))
    client = OpenAI(api_key=api_key)
    tools: list[dict[str, Any]] = []
    if vector_store_ids:
        tools.append(
            {
                "type": "file_search",
                "vector_store_ids": list(vector_store_ids),
            }
        )

    reasoning: dict[str, Any] = {"summary": reasoning_summary}
    _eff = (reasoning_effort or "").strip()
    if _eff:
        reasoning["effort"] = _eff

    common: dict[str, Any] = {
        "store": store,
        "include": include,
        "reasoning": reasoning,
    }
    if tools:
        common["tools"] = tools
    _merge_model(common, model)

    mx_json = (
        max_coinmap_json_chars
        if max_coinmap_json_chars is not None
        else _default_max_coinmap_json_chars()
    )

    if chart_payloads is not None:
        payloads = _filter_valid_chart_payloads(list(chart_payloads))
    elif chart_paths is not None:
        payloads = [("image", p) for p in chart_paths if p.is_file()]
    else:
        payloads = ordered_chart_openai_payloads(charts_dir)

    if not payloads:
        empty_payloads: list[ChartOpenAIPayload] = []
        _log_openai_send(
            flow="analysis",
            batch_index=1,
            total_batches=1,
            payloads=empty_payloads,
            model=model,
            chained=False,
        )
        try:
            r = client.responses.create(
                **common,
                input=responses_input_messages(
                    user_content=_user_content_with_last_filter(analysis_prompt.strip()),
                ),
            )
        except Exception:
            _log_openai_error(
                flow="analysis",
                batch_index=1,
                total_batches=1,
                payloads=empty_payloads,
            )
            raise
        out = (r.output_text or "").strip()
        _log_openai_receive(
            flow="analysis",
            batch_index=1,
            total_batches=1,
            response_id=r.id,
            output_text=out,
        )
        if on_first_model_text is not None and out:
            on_first_model_text(out)
        return PromptTwoStepResult(first_text="", after_charts=out, final_response_id=r.id)

    chunks = chunk_payloads(payloads, max_images_per_call)
    assistant_parts: list[str] = []
    prev_id: str | None = None
    total = len(chunks)

    for bi, batch in enumerate(chunks):
        if total == 1:
            p_text = analysis_prompt
        else:
            n_img = sum(1 for k, _ in batch if k != "json")
            n_json = sum(1 for k, _ in batch if k == "json")
            p_text = (
                f"{analysis_prompt}\n\n"
                f"(Batch {bi + 1} of {total}: {n_img} image(s)/URL(s), {n_json} JSON block(s).)"
            )
        try:
            content = _build_mixed_chart_user_content(
                p_text, batch, max_json_chars=mx_json, chart_stamp=chart_stamp
            )
            kwargs: dict[str, Any] = {
                **common,
                "input": responses_input_messages(user_content=content),
            }
            if prev_id is not None:
                kwargs["previous_response_id"] = prev_id
            _log_openai_send(
                flow="analysis",
                batch_index=bi + 1,
                total_batches=total,
                payloads=batch,
                model=model,
                chained=prev_id is not None,
            )
            r = client.responses.create(**kwargs)
        except Exception:
            _log_openai_error(
                flow="analysis",
                batch_index=bi + 1,
                total_batches=total,
                payloads=batch,
            )
            raise
        prev_id = r.id
        chunk_text = (r.output_text or "").strip()
        _log_openai_receive(
            flow="analysis",
            batch_index=bi + 1,
            total_batches=total,
            response_id=prev_id,
            output_text=chunk_text,
        )
        assistant_parts.append(chunk_text)
        if bi == 0 and on_first_model_text is not None and chunk_text:
            on_first_model_text(chunk_text)

    after = "\n\n---\n\n".join(assistant_parts)
    assert prev_id is not None
    return PromptTwoStepResult(
        first_text="", after_charts=after, final_response_id=prev_id
    )


def run_prompt_two_step_flow(
    *,
    api_key: str,
    charts_dir: Path,
    first_prompt: str,
    follow_up_prompt: str,
    max_images_per_call: int,
    vector_store_ids: list[str],
    store: bool,
    include: list[str],
    reasoning_summary: str = "auto",
    chart_paths: list[Path] | None = None,
    chart_payloads: list[ChartOpenAIPayload] | None = None,
    max_coinmap_json_chars: int | None = None,
    purge_json_attachment_storage: bool = False,
    purge_openai_user_data_files: bool | None = None,
    model: str | None = None,
) -> PromptTwoStepResult:
    """
    Tương thích ngược: gộp ``first_prompt`` và ``follow_up_prompt`` thành một ``analysis_prompt``.
    """
    a = (first_prompt or "").strip()
    b = (follow_up_prompt or "").strip()
    if b:
        analysis_prompt = f"{a}\n\n{b}" if a else b
    else:
        analysis_prompt = a or default_analysis_prompt(read_main_chart_symbol(charts_dir))
    return run_analysis_responses_flow(
        api_key=api_key,
        charts_dir=charts_dir,
        analysis_prompt=analysis_prompt,
        max_images_per_call=max_images_per_call,
        vector_store_ids=vector_store_ids,
        store=store,
        include=include,
        reasoning_summary=reasoning_summary,
        chart_paths=chart_paths,
        chart_payloads=chart_payloads,
        max_coinmap_json_chars=max_coinmap_json_chars,
        on_first_model_text=None,
        purge_json_attachment_storage=purge_json_attachment_storage,
        purge_openai_user_data_files=purge_openai_user_data_files,
        model=model,
    )


DEFAULT_UPDATE_PROMPT_TEMPLATE = (
    "[INTRADAY_UPDATE]\n"
    "Cập nhật intraday: lần đầu sau [FULL_ANALYSIS] kèm morning_full_analysis.json + Coinmap M5 "
    "(CLI ``update`` không đính kèm Coinmap M15; có thể kèm TradingView 15m ICT + TV M5).\n"
)

_INTRADAY_UPDATE_PLAN_HINT = (
    "Chủ động tìm setup mới/cập nhật sau khi so sánh với plan cũ; nếu chỉ có 1 hoặc 2 plan đủ chất lượng "
    "thì trả đúng 1 hoặc 2 phần tử trong `prices`, không cần cố tạo đủ 3 plan mới.\n"
)

_MORNING_CONTEXT_HINT = (
    "Bắt buộc đọc object `context` trong morning_full_analysis.json: nhánh `DXY` "
    "(H4, H1, M15, Footprint_M15) cho DXY / macro bias; nhánh cặp chính (key = đúng mã symbol, "
    "vd. `XAUUSD`) với H4 và H1; xem đây là snapshot chi tiết bias buổi sáng trước khi đánh giá "
    "footprint intraday.\n"
)

_SCALP_UPDATE_PLAN_HINT = (
    "Nhiệm vụ chính: tìm **1 plan đẹp nhất** trong phiên hiện tại (hop_luu >= 60, "
    "có đủ hợp lưu M5 để vào lệnh nhanh). "
    "Không cần đánh giá lại plan cũ; chỉ tập trung vào plan mới đủ chất lượng. "
    "Nếu có 2–3 plan đủ chất lượng thì có thể trả thêm, nhưng không bắt buộc. "
    "Bắt buộc dùng label dạng `scalp_<id>` cho mỗi plan trong `prices` "
    "(ví dụ: `scalp_1`, `scalp_2`, `scalp_3`). "
    "Không dùng label `plan_chinh` hay `plan_phu` cho luồng scalp này.\n"
)

_COINMAP_LEGACY_PNG_HINT = (
    " (mỗi khung: JSON footprint; ảnh PNG fullscreen Coinmap ngay sau JSON tương ứng nếu có)."
)

_INTRADAY_CHART_READ_PRIORITY_HINT = (
    "Ưu tiên đọc ảnh chart (Coinmap PNG, TradingView snapshot nếu có); JSON Coinmap chỉ khi "
    "trên chart không rõ, không đọc được, hoặc cần con số chính xác (order flow, CVD, VWAP, delta, OHLC).\n"
)

_GOCHARTING_INTRADAY_CHART_READ_PRIORITY_HINT = (
    "Ưu tiên footprint_combined JSON cho stacked BID/ASK, absorption, RL theo price level; "
    "CSV GoCharting chỉ có CVD/delta/volume theo nến — không có BID/ASK theo price level. "
    "TradingView snapshot khi cần cấu trúc giá / liquidity.\n"
    f"{_GOCHARTING_CHART_READ_GUIDE}"
)

_INTRADAY_UPDATE_SUFFIX = _INTRADAY_CHART_READ_PRIORITY_HINT + _INTRADAY_UPDATE_PLAN_HINT
_SCALP_UPDATE_SUFFIX = _INTRADAY_CHART_READ_PRIORITY_HINT + _SCALP_UPDATE_PLAN_HINT
_GOCHARTING_SCALP_UPDATE_SUFFIX = (
    _GOCHARTING_INTRADAY_CHART_READ_PRIORITY_HINT + _SCALP_UPDATE_PLAN_HINT
)


def is_first_intraday_update_after_all(
    *,
    last_response_id: str | None,
    last_all_response_id: str | None,
) -> bool:
    """
    True when ``last_response_id`` still matches the last ``all`` response id — first ``update`` run
    should attach ``morning_full_analysis.json`` and start a new thread (no ``previous_response_id``).
    """
    a = (last_all_response_id or "").strip()
    c = (last_response_id or "").strip()
    if not a or not c:
        return False
    return c == a


def build_intraday_update_user_text(
    *,
    first_after_all: bool = False,
    coinmap_attachment_mode: str = "merged",
) -> str:
    """
    User message for ``coinmap-automation update``: thời gian + nhiệm vụ (không nhúng baseline vùng chờ).

    * ``coinmap_attachment_mode="merged"`` (default): một file ``*_coinmap_<MAIN>_merged.json``
      (schema ``coinmap_merged``: ``frames`` 15m + 5m, ``session_profile`` chung).
    * ``coinmap_attachment_mode="merged_m5"``: file ``coinmap_merged`` chỉ có khung **5m** trong
      ``frames`` (build từ raw M5 qua ``write_openai_coinmap_merged_from_raw_export``).
    * ``coinmap_attachment_mode="legacy"``: như trước — file M15 và M5 tách riêng.
    * ``coinmap_attachment_mode="m5_only"``: chỉ footprint Coinmap **M5** (không M15, không merged).

    * ``first_after_all=True``: morning JSON + Coinmap (merged hoặc hai file raw).
    * ``first_after_all=False``: chỉ Coinmap (merged hoặc M15+M5); nối chuỗi ``[INTRADAY_UPDATE]``.
    """
    time_line = format_intraday_update_time_line()
    mode = str(coinmap_attachment_mode or "merged").strip().lower()
    m5_only = mode == "m5_only"
    merged_m5 = mode in ("merged_m5", "merged_m5_only")
    merged = not m5_only and not merged_m5 and mode != "legacy"

    if merged_m5:
        if first_after_all:
            return (
                "[INTRADAY_UPDATE]\n"
                f"{time_line}"
                "Phân tích buổi sáng (Schema A) nằm trong file **morning_full_analysis.json** đính kèm đầu tiên.\n"
                "Đính kèm **hai** file JSON theo thứ tự: **(1)** morning_full_analysis.json, **(2)** một file "
                "**Coinmap merged** cho cặp chính (cùng schema ``coinmap_merged`` nhưng ``frames`` chỉ có "
                "khung **5m**; không đính kèm M15).\n"
                f"{_MORNING_CONTEXT_HINT}"
                f"{_INTRADAY_UPDATE_SUFFIX}"
            )
        return (
            "[INTRADAY_UPDATE]\n"
            f"{time_line}"
            "Tiếp tục chuỗi phản hồi sau lần [INTRADAY_UPDATE] trước.\n"
            "Đính kèm **một** file JSON: **Coinmap merged** cho cặp chính (schema ``coinmap_merged`` chỉ có "
            "khung **5m**; không M15).\n"
            f"{_INTRADAY_UPDATE_SUFFIX}"
        )

    if m5_only:
        if first_after_all:
            return (
                "[INTRADAY_UPDATE]\n"
                f"{time_line}"
                "Phân tích buổi sáng (Schema A) nằm trong file **morning_full_analysis.json** đính kèm đầu tiên.\n"
                "Đính kèm **hai** file JSON theo thứ tự: **(1)** morning_full_analysis.json, **(2)** một file "
                "**Coinmap M5** (footprint cặp chính; không đính kèm M15).\n"
                f"{_MORNING_CONTEXT_HINT}"
                f"{_INTRADAY_UPDATE_SUFFIX}"
            )
        return (
            "[INTRADAY_UPDATE]\n"
            f"{time_line}"
            "Tiếp tục chuỗi phản hồi sau lần [INTRADAY_UPDATE] trước.\n"
            "Đính kèm **một** file JSON: **Coinmap M5** (footprint cặp chính; không M15).\n"
            f"{_INTRADAY_UPDATE_SUFFIX}"
        )

    if first_after_all:
        if merged:
            return (
                "[INTRADAY_UPDATE]\n"
                f"{time_line}"
                "Phân tích buổi sáng (Schema A) nằm trong file **morning_full_analysis.json** đính kèm đầu tiên.\n"
                "Đính kèm **hai** file JSON theo thứ tự: **(1)** morning_full_analysis.json, **(2)** một file "
                "**Coinmap merged** cho cặp chính (cùng schema ``coinmap_merged``: khung 15m và 5m trong ``frames``, "
                "footprint và summary theo từng khung).\n"
                f"{_MORNING_CONTEXT_HINT}"
                f"{_INTRADAY_UPDATE_SUFFIX}"
            )
        return (
            "[INTRADAY_UPDATE]\n"
            f"{time_line}"
            "Phân tích buổi sáng (Schema A) nằm trong file **morning_full_analysis.json** đính kèm đầu tiên.\n"
            "Đính kèm **ba** file JSON theo thứ tự: **(1)** morning_full_analysis.json, **(2)** M15, **(3)** M5 "
            "(footprint cặp chính).\n"
            f"{_MORNING_CONTEXT_HINT}"
            f"{_INTRADAY_UPDATE_SUFFIX}"
        )

    if merged:
        return (
            "[INTRADAY_UPDATE]\n"
            f"{time_line}"
            "Tiếp tục chuỗi phản hồi sau lần [INTRADAY_UPDATE] trước.\n"
            "Đính kèm **một** file JSON: **Coinmap merged** cho cặp chính (15m và 5m trong cùng file).\n"
            f"{_INTRADAY_UPDATE_SUFFIX}"
        )
    return (
        "[INTRADAY_UPDATE]\n"
        f"{time_line}"
        "Tiếp tục chuỗi phản hồi sau lần [INTRADAY_UPDATE] trước.\n"
        "Đính kèm **hai** file JSON theo thứ tự: **(1) M15**, **(2) M5** (footprint cặp chính).\n"
        f"{_INTRADAY_UPDATE_SUFFIX}"
    )

def build_scalp_update_user_text(
    *,
    first_after_all: bool = False,
    coinmap_attachment_mode: str = "merged",
    footprint_source: str = "coinmap",
) -> str:
    """
    User message cho ``coinmap-automation update-scalp``: giống ``build_intraday_update_user_text``
    nhưng yêu cầu tìm plan đẹp nhất và dùng label ``scalp_<timeframe>``.

    * ``footprint_source="gocharting"``: CSV + PNG overview GoCharting M15/M5 + footprint_combined JSON.
    * ``coinmap_attachment_mode="merged"`` (default): file ``coinmap_merged`` đa khung (15m + 5m).
    * ``coinmap_attachment_mode="merged_m5"``: file ``coinmap_merged`` chỉ có khung **5m** trong
      ``frames`` (build từ raw M5 qua ``write_openai_coinmap_merged_from_raw_export``).
    * ``coinmap_attachment_mode="m5_only"``: footprint Coinmap **M5** raw (không merged, không M15).
    * ``coinmap_attachment_mode="legacy"``: file M15 và M5 tách riêng.
    """
    fp = (footprint_source or "coinmap").strip().lower()
    if fp == "gocharting":
        time_line = format_intraday_update_time_line()
        gc_hint = (
            f" (footprint {GOCHARTING_GOLD_FUTURE_LABEL} trên GoCharting — hợp đồng tương lai vàng GC1!, "
            "không phải spot XAUUSD; mỗi khung: CSV orderflow; PNG overview; "
            "cuối cùng footprint_combined_15m.json và footprint_combined_5m.json — "
            "GC ohlc + footprint[] + XAUUSD_OHLC spot broker, N nến mới nhất)."
        )
        if first_after_all:
            return (
                "[INTRADAY_UPDATE]\n"
                f"{time_line}"
                "Phân tích buổi sáng (Schema A) nằm trong file **morning_full_analysis.json** đính kèm đầu tiên.\n"
                "Đính kèm **ba** file theo thứ tự: **(1)** morning_full_analysis.json, **(2)** GoCharting M15 CSV, "
                f"**(3)** M5 CSV{gc_hint}\n"
                f"{_MORNING_CONTEXT_HINT}"
                f"{_GOCHARTING_SCALP_UPDATE_SUFFIX}"
            )
        return (
            "[INTRADAY_UPDATE]\n"
            f"{time_line}"
            "Tiếp tục chuỗi phản hồi sau lần [INTRADAY_UPDATE] trước.\n"
            f"Đính kèm **hai** file: **(1)** GoCharting M15 CSV, **(2)** M5 CSV "
            f"(footprint {GOCHARTING_GOLD_FUTURE_LABEL}; "
            "PNG overview ngay sau mỗi CSV nếu có; "
            "footprint_combined_15m.json và footprint_combined_5m.json gồm XAUUSD_OHLC).\n"
            f"{_GOCHARTING_SCALP_UPDATE_SUFFIX}"
        )

    time_line = format_intraday_update_time_line()
    mode = str(coinmap_attachment_mode or "merged").strip().lower()
    m5_only = mode == "m5_only"
    merged_m5 = mode in ("merged_m5", "merged_m5_only")
    merged = not m5_only and not merged_m5 and mode != "legacy"

    if merged_m5:
        if first_after_all:
            return (
                "[INTRADAY_UPDATE]\n"
                f"{time_line}"
                "Phân tích buổi sáng (Schema A) nằm trong file **morning_full_analysis.json** đính kèm đầu tiên.\n"
                "Đính kèm **hai** file JSON theo thứ tự: **(1)** morning_full_analysis.json, **(2)** một file "
                "**Coinmap merged** cho cặp chính (cùng schema ``coinmap_merged`` nhưng ``frames`` chỉ có "
                "khung **5m**; không đính kèm M15).\n"
                f"{_MORNING_CONTEXT_HINT}"
                f"{_SCALP_UPDATE_SUFFIX}"
            )
        return (
            "[INTRADAY_UPDATE]\n"
            f"{time_line}"
            "Tiếp tục chuỗi phản hồi sau lần [INTRADAY_UPDATE] trước.\n"
            "Đính kèm **một** file JSON: **Coinmap merged** cho cặp chính (schema ``coinmap_merged`` chỉ có "
            "khung **5m**; không M15).\n"
            f"{_SCALP_UPDATE_SUFFIX}"
        )

    if m5_only:
        if first_after_all:
            return (
                "[INTRADAY_UPDATE]\n"
                f"{time_line}"
                "Phân tích buổi sáng (Schema A) nằm trong file **morning_full_analysis.json** đính kèm đầu tiên.\n"
                "Đính kèm **hai** file JSON theo thứ tự: **(1)** morning_full_analysis.json, **(2)** một file "
                "**Coinmap M5** (footprint cặp chính; không đính kèm M15).\n"
                f"{_MORNING_CONTEXT_HINT}"
                f"{_SCALP_UPDATE_SUFFIX}"
            )
        return (
            "[INTRADAY_UPDATE]\n"
            f"{time_line}"
            "Tiếp tục chuỗi phản hồi sau lần [INTRADAY_UPDATE] trước.\n"
            "Đính kèm **một** file JSON: **Coinmap M5** (footprint cặp chính; không M15).\n"
            f"{_SCALP_UPDATE_SUFFIX}"
        )

    if first_after_all:
        if merged:
            return (
                "[INTRADAY_UPDATE]\n"
                f"{time_line}"
                "Phân tích buổi sáng (Schema A) nằm trong file **morning_full_analysis.json** đính kèm đầu tiên.\n"
                "Đính kèm **hai** file JSON theo thứ tự: **(1)** morning_full_analysis.json, **(2)** một file "
                "**Coinmap merged** cho cặp chính (cùng schema ``coinmap_merged``: khung 15m và 5m trong ``frames``, "
                "footprint và summary theo từng khung).\n"
                f"{_MORNING_CONTEXT_HINT}"
                f"{_SCALP_UPDATE_SUFFIX}"
            )
        return (
            "[INTRADAY_UPDATE]\n"
            f"{time_line}"
            "Phân tích buổi sáng (Schema A) nằm trong file **morning_full_analysis.json** đính kèm đầu tiên.\n"
            "Đính kèm **ba** file JSON theo thứ tự: **(1)** morning_full_analysis.json, **(2)** M15, **(3)** M5 "
            f"(footprint cặp chính{_COINMAP_LEGACY_PNG_HINT}\n"
            f"{_MORNING_CONTEXT_HINT}"
            f"{_SCALP_UPDATE_SUFFIX}"
        )

    if merged:
        return (
            "[INTRADAY_UPDATE]\n"
            f"{time_line}"
            "Tiếp tục chuỗi phản hồi sau lần [INTRADAY_UPDATE] trước.\n"
            "Đính kèm **một** file JSON: **Coinmap merged** cho cặp chính (15m và 5m trong cùng file).\n"
            f"{_SCALP_UPDATE_SUFFIX}"
        )
    return (
        "[INTRADAY_UPDATE]\n"
        f"{time_line}"
        "Tiếp tục chuỗi phản hồi sau lần [INTRADAY_UPDATE] trước.\n"
        "Đính kèm **hai** file JSON theo thứ tự: **(1) M15**, **(2) M5** "
        f"(footprint cặp chính{_COINMAP_LEGACY_PNG_HINT}\n"
        f"{_SCALP_UPDATE_SUFFIX}"
    )


# TradingView tab Nhật ký: giá chạm → Coinmap compact ``coinmap_merged`` (từ raw M5/M1) + OpenAI (intraday).
# Trả về Schema E: chỉ ``phan_tich_alert`` + ``intraday_hanh_dong``; nếu VÀO LỆNH, dùng trade_line theo baseline vùng.
JOURNAL_INTRADAY_FIRST_USER_TEMPLATE = (
    "[INTRADAY_ALERT]\n"
    "Cảnh báo TradingView đã kích hoạt tại mức giá {touched_price}.\n"
    "Đính kèm một file JSON **coinmap_merged** (footprint/summary theo khung M5 hoặc M1).\n"
)

JOURNAL_INTRADAY_RETRY_USER_TEMPLATE = (
    "[INTRADAY_ALERT]\n"
    "Tiếp tục đánh giá sau {wait_minutes} phút; vẫn theo dõi mức đã chạm {touched_price}.\n"
    "Đính kèm bản **coinmap_merged** mới (cùng định dạng).\n"
)

# Sau khi giá last realtime chạm TP1 (vùng đang ``cho_tp1``).
TP1_POST_TOUCH_USER_TEMPLATE = (
    "[TRADE_MANAGEMENT]\n"
    "Đánh giá Footprint M5 đính kèm (giữ hay thoát / chỉnh SL/TP).\n"
    "Vùng (label): {plan_label}\n"
    "{entry_side} entry: {entry_price}\n"
    "SL hiện tại: {current_sl}\n"
    "TP hiện tại: {current_tp}\n\n"
)

# Daemon zones: follow-up theo mốc R khi đang ``cho_tp1`` (sau arm TP1).
R1_POST_TOUCH_USER_TEMPLATE = (
    "[TRADE_MANAGEMENT]\n"
    "Giá đã đạt mức {r_level}R; "
    "đánh giá Footprint M5 đính kèm (giữ hay thoát / chỉnh SL/TP).\n"
    "Vùng (label): {plan_label}\n"
    "{entry_side} entry: {entry_price}\n"
    "Entry tham chiếu: {entry_ref}\n"
    "Mức {r_level}R (so với entry): {r_level_price}\n"
    "SL hiện tại: {current_sl}\n"
    "TP hiện tại: {current_tp}\n\n"
)

# Plan chính / plan phụ: ~5 phút sau khớp lệnh — GoCharting M5 detail, chỉ khuyến nghị (không MT5).
POST_FILL_MANAGEMENT_USER_TEMPLATE = (
    "[TRADE_MANAGEMENT]\n"
    "Lệnh đã khớp khoảng {minutes_after_fill} phút trước. "
    f"Đánh giá Footprint GoCharting M5 (footprint_combined_5m.json) đính kèm — {GOCHARTING_GOLD_FUTURE_LABEL} "
    "(hợp đồng tương lai vàng COMEX GC1! trên GoCharting; không phải spot XAUUSD MT5) "
    "(giữ hay thoát / chỉnh SL/TP).\n"
    f"{_GOCHARTING_TRADE_MANAGEMENT_SUFFIX}"
    "Đây là khuyến nghị tham khảo — người dùng tự quyết trên MT5.\n"
    "Vùng (label): {plan_label}\n"
    "{entry_side} entry: {entry_price}\n"
    "SL hiện tại: {current_sl}\n"
    "TP hiện tại: {current_tp}\n\n"
)


def run_single_followup_responses(
    *,
    api_key: str,
    user_text: str,
    coinmap_json_paths: Sequence[Path],
    extra_chart_payloads: Sequence[ChartOpenAIPayload] | None = None,
    previous_response_id: str | None,
    morning_snapshot_path: Path | None = None,
    vector_store_ids: list[str],
    store: bool,
    include: list[str],
    reasoning_summary: str | None = "auto",
    reasoning_effort: str | None = DEFAULT_REASONING_EFFORT,
    max_coinmap_json_chars: int | None = None,
    model: str | None = None,
    chart_stamp: str | None = None,
    gocharting_detail_zoom_only: bool = False,
) -> tuple[str, str]:
    """
    One multimodal user turn: optional ``morning_snapshot_path`` + Coinmap JSON paths,
    plus optional chart payloads, uploaded in order (Coinmap raw JSON: base64
    ``input_file.file_data``; sibling Coinmap PNG immediately after each Coinmap JSON
    when on disk; other JSON: inline ``input_text``; image payloads as images).

    If ``previous_response_id`` is ``None``, starts a **new** Responses thread (no chain).
    Otherwise chains to that id (intraday alert, TP1, etc.).

    ``reasoning_summary`` / ``reasoning_effort``: when ``reasoning_summary`` is ``None``, the request
    omits the ``reasoning`` field. Otherwise ``reasoning`` is sent; non-empty ``reasoning_effort``
    adds ``reasoning.effort``.
    """
    paths: list[Path] = []
    if morning_snapshot_path is not None:
        mp = morning_snapshot_path
        if not isinstance(mp, Path) or not mp.is_file():
            raise FileNotFoundError(f"Morning analysis JSON not found: {morning_snapshot_path}")
        paths.append(mp)
    paths.extend([p for p in coinmap_json_paths if isinstance(p, Path)])
    extra_payloads = _filter_valid_chart_payloads(list(extra_chart_payloads or []))
    if not paths and not extra_payloads:
        raise ValueError(
            "Need at least one attachment path/payload "
            "(morning_snapshot_path, coinmap_json_paths, and/or extra_chart_payloads)"
        )
    for p in paths:
        if not p.is_file():
            raise FileNotFoundError(f"Attachment not found: {p}")

    client = OpenAI(api_key=api_key)
    tools: list[dict[str, Any]] = []
    if vector_store_ids:
        tools.append(
            {
                "type": "file_search",
                "vector_store_ids": list(vector_store_ids),
            }
        )

    common: dict[str, Any] = {
        "store": store,
        "include": include,
    }
    if reasoning_summary is not None:
        reasoning: dict[str, Any] = {"summary": reasoning_summary}
        _eff = (reasoning_effort or "").strip()
        if _eff:
            reasoning["effort"] = _eff
        common["reasoning"] = reasoning
    if tools:
        common["tools"] = tools
    _merge_model(common, model)

    mx_json = (
        max_coinmap_json_chars
        if max_coinmap_json_chars is not None
        else _default_max_coinmap_json_chars()
    )

    from automation_tool.images import _default_gocharting_cfg

    json_payloads = openai_payloads_for_attachment_paths(
        paths,
        gocharting_cfg=_default_gocharting_cfg(),
        gocharting_detail_zoom_only=gocharting_detail_zoom_only,
    )
    content = _build_mixed_chart_user_content(
        user_text,
        json_payloads + extra_payloads,
        max_json_chars=mx_json,
        chart_stamp=chart_stamp,
    )
    create_kw: dict[str, Any] = {
        **common,
        "input": responses_input_messages(user_content=content),
    }
    if previous_response_id is not None and str(previous_response_id).strip():
        create_kw["previous_response_id"] = str(previous_response_id).strip()
    r = client.responses.create(**create_kw)
    out = (r.output_text or "").strip()
    return out, r.id


def run_text_followup_responses(
    *,
    api_key: str,
    user_text: str,
    previous_response_id: str,
    vector_store_ids: list[str],
    store: bool,
    include: list[str],
    reasoning_summary: str = "auto",
    model: str | None = None,
    image_paths: Sequence[Path] | None = None,
) -> tuple[str, str]:
    """
    One user turn chained to ``previous_response_id`` (text-only, or text + optional images).

    Returns ``(output_text, new_response_id)``.
    """
    client = OpenAI(api_key=api_key)
    tools: list[dict[str, Any]] = []
    if vector_store_ids:
        tools.append(
            {
                "type": "file_search",
                "vector_store_ids": list(vector_store_ids),
            }
        )

    reasoning: dict[str, Any] = {
        "summary": reasoning_summary,
        "effort": DEFAULT_REASONING_EFFORT,
    }

    common: dict[str, Any] = {
        "store": store,
        "include": include,
        "reasoning": reasoning,
    }
    if tools:
        common["tools"] = tools
    _merge_model(common, model)

    imgs = [p for p in (image_paths or []) if isinstance(p, Path) and p.is_file()]
    text = (user_text or "").strip()
    if imgs:
        payloads: list[ChartOpenAIPayload] = [("image", p) for p in imgs]
        user_content: str | list[dict[str, Any]] = _build_mixed_chart_user_content(
            text,
            payloads,
            max_json_chars=_default_max_coinmap_json_chars(),
        )
    else:
        user_content = text

    r = client.responses.create(
        **common,
        previous_response_id=previous_response_id,
        input=responses_input_messages(user_content=user_content),
    )
    out = (r.output_text or "").strip()
    return out, r.id
