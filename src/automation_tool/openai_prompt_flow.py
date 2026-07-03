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
from automation_tool.prompts import responses_input_messages
from automation_tool.images import (
    CHART_SLOT_COUNT,
    DEFAULT_MAIN_CHART_SYMBOL,
    OPENAI_PAYLOAD_MAX,
    ChartOpenAIPayload,
    _detail_back_step_from_stem,
    chunk_payloads,
    image_to_data_url,
    normalize_main_chart_symbol,
    openai_payloads_for_attachment_paths,
    ordered_chart_openai_payloads,
    read_main_chart_symbol,
    split_openai_payloads_by_phase,
)
from automation_tool.state_files import MORNING_FULL_ANALYSIS_FILENAME
from automation_tool.zones_state import format_intraday_update_time_line

_log = logging.getLogger(__name__)

DEFAULT_REASONING_EFFORT = "medium"
ALL_FLOW_REASONING_EFFORT = "high"

# Main gold pair footprint: prepared spot JSON (footprint_{SYMBOL}_{iv}.json).
MAIN_SPOT_FOOTPRINT_LABEL = "XAUUSD spot"


def _prepared_footprint_filename(sym: str, interval: str) -> str:
    return f"footprint_{sym.strip().upper()}_{interval.strip().lower()}.json"


def _prepared_footprint_pair_desc(sym: str) -> str:
    return (
        f"{_prepared_footprint_filename(sym, '15m')} và "
        f"{_prepared_footprint_filename(sym, '5m')}"
    )


_PREPARED_FOOTPRINT_HINT = (
    "Footprint prepared JSON (spot broker prices): mỗi nến có ohlc, footprint[] "
    "(buy/sell volume từng level), bar_flow {delta, cum_delta, max_delta, min_delta, "
    "vwap, buy_volume, sell_volume}, orderflow.stacked_in_candle (BID/ASK stacked RL≥rl_min). "
    "Dữ liệu đã gộp trong footprint_XAUUSD_15m.json và footprint_XAUUSD_5m.json.\n"
)

_GOCHARTING_BID_ASK_HINT = (
    "Stacked BID/ASK, absorption: đọc từ "
    f"{_prepared_footprint_pair_desc('XAUUSD')} "
    "(footprint[] buy/sell volume từng level; bar_flow cho CVD/delta theo nến).\n"
)

_GOCHARTING_CHART_READ_GUIDE = (
    "Hướng dẫn đọc footprint prepared JSON:\n"
    "- footprint[]: buy/sell volume từng price level (giá spot broker).\n"
    "- bar_flow: delta, cum_delta, vwap = VWAP session tích lũy đến nến đó.\n"
    "- POC/VWAP: suy từ footprint[] và bar_flow.\n"
)

_GOCHARTING_TRADE_MANAGEMENT_SUFFIX = (
    f"Stacked BID/ASK, absorption: đọc từ {_prepared_footprint_filename('XAUUSD', '5m')} "
    "(candles[].orderflow nếu có; footprint[] buy/sell volume từng level).\n"
    f"{_GOCHARTING_CHART_READ_GUIDE}"
)

_PHAN_TICH_CHAM_DIEM_FULL_HINT = (
    "Trong `phan_tich_cham_diem` (Schema A): mỗi plan bắt buộc block `🏷️ Trạng thái vùng` "
    "(fresh/used + đã chạm trước đó chưa, mô tả phản ứng lần trước nếu có); "
    "nhóm Order Flow và Footprint phải có `→ Số liệu:` (con số CVD/delta, trap, stacked, "
    "absorption, VWAP/POC) rồi `→ Phân tích chấm điểm:` — xem định dạng trong system-prompt.md.\n"
)


def _gocharting_main_footprint_label(main_symbol: str) -> str:
    sym = (main_symbol or "").strip().upper()
    if sym == "XAUUSD":
        return MAIN_SPOT_FOOTPRINT_LABEL
    return sym or DEFAULT_MAIN_CHART_SYMBOL


def _gocharting_gold_future_slot_note(path: Path) -> str:
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
    footprint_source: str = "gocharting",
) -> str:
    """
    Default user message for multimodal analysis (GoCharting footprint workflow).

    ``main_symbol`` is the main pair (TradingView / MT5 spot); invalid/empty →
    ``DEFAULT_MAIN_CHART_SYMBOL``. Schema and rules are in ``system-prompt.md`` (``[FULL_ANALYSIS]`` → Schema A).
    """
    sym = DEFAULT_MAIN_CHART_SYMBOL
    if main_symbol and str(main_symbol).strip():
        try:
            sym = normalize_main_chart_symbol(str(main_symbol).strip())
        except ValueError:
            pass
    footprint_desc = (
        f"({CHART_SLOT_COUNT} slot chart):\n"
        "TradingView DXY (H4, H1, M15) → "
        f"TradingView {sym} (H4, H1, M15, M15 Session Liquidity Check / ICT Killzones, M5) "
        "(snapshot URL/PNG hoặc JSON OHLC tvdatafeed) → "
        "GoCharting DXY M15 (PNG overview) → "
        f"Footprint spot {sym}: {_prepared_footprint_pair_desc(sym)} "
        "(ohlc spot + footprint[] buy/sell volume + bar_flow delta/CVD/VWAP).\n"
        f"{_PREPARED_FOOTPRINT_HINT}"
        f"{_GOCHARTING_CHART_READ_GUIDE}"
    )
    _ = footprint_source  # legacy callers may pass this; prompts are GoCharting-only
    return (
        "[FULL_ANALYSIS]\n"
        f"Cặp chính: {sym}.\n"
        f"Đính kèm theo thứ tự, tối đa {OPENAI_PAYLOAD_MAX} payload multimodal "
        f"{footprint_desc}"
        f"{_PHAN_TICH_CHAM_DIEM_FULL_HINT}"
    )


# Tương thích ngược: prompt mặc định khi cặp = XAUUSD
DEFAULT_ANALYSIS_PROMPT = default_analysis_prompt(DEFAULT_MAIN_CHART_SYMBOL)
DEFAULT_FIRST_PROMPT = DEFAULT_ANALYSIS_PROMPT
DEFAULT_FOLLOW_UP_PROMPT = ""


def _resolved_main_symbol(main_symbol: str | None = None) -> str:
    sym = DEFAULT_MAIN_CHART_SYMBOL
    if main_symbol and str(main_symbol).strip():
        try:
            sym = normalize_main_chart_symbol(str(main_symbol).strip())
        except ValueError:
            pass
    return sym


def full_analysis_structure_prompt(main_symbol: str | None = None) -> str:
    """Batch 1/2: TradingView + DXY GoCharting overview PNG — structure, POI, candidate zones."""
    sym = _resolved_main_symbol(main_symbol)
    return (
        "[FULL_ANALYSIS — BƯỚC 1/2: CẤU TRÚC GIÁ]\n"
        f"Cặp chính: {sym}.\n"
        "Đính kèm (theo thứ tự):\n"
        "- TradingView: DXY H4, H1, M15 → "
        f"{sym} H4, H1, M15, M15 Session Liquidity Check / ICT Killzones, M5 "
        "(snapshot URL/PNG hoặc JSON OHLC tvdatafeed)\n"
        "- GoCharting DXY M15: **chỉ PNG overview** (orderflow chart macro — không có CSV)\n"
        "Nhiệm vụ: phân tích cấu trúc giá (DXY macro bias + trend/POI cặp chính) và liệt kê "
        "các **vùng có cấu trúc tốt** (OB/FVG/HL, premium-discount hợp lệ) làm candidate cho batch 2.\n"
        "KHÔNG chấm điểm footprint (chưa có footprint prepared JSON cặp chính).\n"
        "Trả duy nhất một block ```json với object:\n"
        "{\n"
        '  "step": 1,\n'
        '  "context": {\n'
        '    "DXY": {"H4": "...", "H1": "...", "M15": "..."},\n'
        f'    "{sym}": {{"H4": "...", "H1": "..."}}\n'
        "  },\n"
        '  "structure_notes": "...",\n'
        '  "m15_plan_draft": "...",\n'
        '  "m5_entry_module": "...",\n'
        '  "candidate_zones": [\n'
        "    {\n"
        '      "direction": "BUY|SELL",\n'
        '      "price_hint": 0.0,\n'
        '      "poi_type": "OB|FVG|HL|...",\n'
        '      "rationale": "..."\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "candidate_zones: không gán label plan_chinh/plan_phu/scalp — batch 2 phân loại sau footprint.\n"
    )


def full_analysis_footprint_prompt(main_symbol: str | None = None) -> str:
    """Batch 2/2: XAUUSD spot footprint only (no DXY) — scoring and full Schema A."""
    sym = _resolved_main_symbol(main_symbol)
    return (
        "[FULL_ANALYSIS — BƯỚC 2/2: FOOTPRINT & KẾT LUẬN]\n"
        f"Cặp chính: {sym}.\n"
        "Tiếp nối bước 1: dùng `context` và `candidate_zones` từ response trước "
        "(cùng thread OpenAI). DXY macro bias đã xác định ở bước 1 — **không** đính kèm lại DXY.\n"
        f"Đính kèm footprint spot **{sym}**:\n"
        f"- {_prepared_footprint_pair_desc(sym)} "
        "(footprint[] buy/sell volume từng level + bar_flow delta/CVD/VWAP).\n"
        f"{_PREPARED_FOOTPRINT_HINT}"
        f"{_GOCHARTING_CHART_READ_GUIDE}"
        "Nhiệm vụ (playbook §1.3 bước 5–8):\n"
        "- Footprint XAUUSD M15/M5: trap, CVD ≥3 nến, stacked, absorption, VWAP/POC\n"
        "- Filters (anti-sweep, RR, session)\n"
        "- Chấm điểm hop_luu theo mục 0.3 (4 nhóm)\n"
        "- Trả đầy đủ Schema A: phan_tich_cham_diem + output_ngan_gon + prices[] + intraday_hanh_dong\n"
        f"{_PHAN_TICH_CHAM_DIEM_FULL_HINT}"
    )


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


def prepare_footprint_json_for_openai(
    path: Path,
    data: dict[str, Any],
    *,
    chart_stamp: str | None = None,
    gocharting_cfg: dict[str, Any] | None = None,
    charts_dir: Path | None = None,
) -> dict[str, Any]:
    """
    GoCharting footprint JSON after trim / aggregate / enrich — same document sent to OpenAI.
    Non-footprint paths are returned unchanged.
    Converted ``footprint_{SYMBOL}_{iv}.json`` that are already finalized are returned as-is.
    """
    from automation_tool.gocharting_gc_spot_convert import (
        build_basis_index,
        convert_footprint_combined_to_spot,
        enrich_prepared_footprint_from_gc_csv,
        enrich_prepared_footprint_from_ws_bar_flow,
        finalize_prepared_spot_footprint,
        footprint_has_ws_bar_flow,
        gc_to_spot_enabled,
        gc_to_spot_skip_main_csv,
        is_finalized_spot_footprint,
        is_prepared_footprint_path,
        parse_prepared_footprint_path,
        resolve_gc_csv_for_interval,
        resolve_mt5_spot_payload,
    )

    parsed_prepared = parse_prepared_footprint_path(path) if is_prepared_footprint_path(path) else None
    if parsed_prepared is not None:
        sym_early, _iv_early = parsed_prepared
        if is_finalized_spot_footprint(data, logic_symbol=sym_early):
            return data

    name = path.name
    is_combined_source = name.startswith("footprint_combined_") and path.suffix.lower() == ".json"
    is_stale_prepared = (
        parsed_prepared is not None
        and not is_finalized_spot_footprint(data, logic_symbol=parsed_prepared[0])
    )
    if is_combined_source or is_stale_prepared:
        from automation_tool.gocharting_ws_decode import (
            aggregate_footprint_combined_document,
            drop_forming_footprint_candle,
            footprint_ws_max_candles_from_cfg,
            slim_footprint_combined_for_openai,
            trim_footprint_document,
        )
        from automation_tool.gocharting_footprint_derived import (
            enrich_footprint_combined_document,
            enrich_prepared_footprint_stacked,
            footprint_derived_enabled,
        )
        from automation_tool.images import _default_gocharting_cfg, read_main_chart_symbol

        cfg = gocharting_cfg if gocharting_cfg is not None else _default_gocharting_cfg()
        if is_combined_source:
            iv = path.stem.replace("footprint_combined_", "")
        else:
            parsed = parse_prepared_footprint_path(path)
            if parsed is None:
                return data
            iv = parsed[1]
        out = drop_forming_footprint_candle(data, interval=iv)
        out = trim_footprint_document(
            out,
            max_candles=footprint_ws_max_candles_from_cfg(cfg),
        )
        out = aggregate_footprint_combined_document(out, cfg=cfg)
        if footprint_derived_enabled(cfg) and not gc_to_spot_enabled(cfg):
            out = enrich_footprint_combined_document(out, cfg=cfg)
        if gc_to_spot_enabled(cfg):
            cd = charts_dir
            if cd is None:
                parent = path.parent
                cd = parent.parent if parent.name == "footprint_images" else parent
            sym = read_main_chart_symbol(cd)
            candles = out.get("candles") if isinstance(out.get("candles"), list) else []
            mt5 = resolve_mt5_spot_payload(
                charts_dir=cd,
                logic_symbol=sym,
                interval=iv,
                count=len(candles),
                chart_stamp=chart_stamp,
                footprint_candles=[c for c in candles if isinstance(c, dict)],
            )
            out = convert_footprint_combined_to_spot(
                out,
                mt5_payload=mt5,
                cfg=cfg,
                logic_symbol=sym,
                interval=iv,
            )
            basis_index, _ = build_basis_index(
                [c for c in candles if isinstance(c, dict)],
                mt5,
            )
            if footprint_has_ws_bar_flow(out):
                out = enrich_prepared_footprint_from_ws_bar_flow(
                    out,
                    cfg=cfg,
                    basis_index=basis_index,
                )
            elif not gc_to_spot_skip_main_csv(cfg):
                csv_path = resolve_gc_csv_for_interval(cd, iv, chart_stamp=chart_stamp)
                out = enrich_prepared_footprint_from_gc_csv(
                    out,
                    csv_path,
                    cfg=cfg,
                    basis_index=basis_index,
                )
            out = finalize_prepared_spot_footprint(
                out,
                logic_symbol=sym,
                interval=iv,
            )
            out = enrich_prepared_footprint_stacked(out, cfg=cfg)
        return slim_footprint_combined_for_openai(out)
    if name.startswith("footprint_bid_ask_") and path.suffix.lower() == ".json":
        from automation_tool.gocharting_footprint_ocr import (
            charts_dir_from_footprint_json_path,
            enrich_footprint_bid_ask_document,
            resolve_gocharting_csv_for_footprint_json,
            trim_footprint_bid_ask_document,
        )

        out = trim_footprint_bid_ask_document(data)
        charts_dir = charts_dir_from_footprint_json_path(path)
        csv_path = resolve_gocharting_csv_for_footprint_json(
            path,
            charts_dir=charts_dir,
            stamp=chart_stamp,
        )
        if csv_path is not None:
            out = enrich_footprint_bid_ask_document(out, csv_path)
        return out
    return data


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
    from automation_tool.gocharting_gc_spot_convert import is_prepared_footprint_path

    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
        if (
            _coinmap_openai_slim_enabled()
            and isinstance(data, dict)
            and should_slim_coinmap_json_path(path)
        ):
            data = slim_coinmap_export_for_openai(data, path=path)
        if isinstance(data, dict) and (
            path.name.startswith("footprint_combined_")
            or path.name.startswith("footprint_bid_ask_")
        ):
            data = prepare_footprint_json_for_openai(
                path,
                data,
                chart_stamp=chart_stamp,
            )
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
            "Instrument: spot XAUUSD on broker MT5.\n"
            "Bar times (`t`) and generated_at are Asia/Ho_Chi_Minh (UTC+7).\n"
        )
    elif is_prepared_footprint_path(path):
        from automation_tool.gocharting_gc_spot_convert import parse_prepared_footprint_path

        parsed = parse_prepared_footprint_path(path)
        sym_label, iv = parsed if parsed else ("XAUUSD", "?")
        header = (
            f"[Footprint prepared — {iv} — file: {path.name}]\n"
            f"Instrument: spot {sym_label}. Each candle: time_gmt7, ohlc (spot broker), "
            "footprint[] (buy/sell volume per price block), "
            "bar_flow {delta, cum_delta, max_delta, min_delta, vwap (session cumulative), buy_volume, sell_volume}, "
            "orderflow.stacked_in_candle.\n"
        )
    elif path.name.startswith("footprint_combined_") and path.suffix.lower() == ".json":
        iv = path.stem.replace("footprint_combined_", "")
        header = (
            f"[Footprint prepared — {iv} — file: {path.name}]\n"
            f"Instrument: spot {DEFAULT_MAIN_CHART_SYMBOL}. Each candle: time_gmt7, ohlc, "
            "footprint[] (buy/sell volume per price block), "
            "bar_flow {delta, cum_delta, vwap (session cumulative), buy_volume, sell_volume}.\n"
        )
    elif path.name.startswith("footprint_bid_ask_") and path.suffix.lower() == ".json":
        iv = path.stem.replace("footprint_bid_ask_", "")
        header = (
            f"[Footprint bid/ask — {iv} — file: {path.name}]\n"
            f"Instrument: spot {DEFAULT_MAIN_CHART_SYMBOL}. Each candle: time (HH:MM) + price_levels "
            "[{bid, ask, price}, ...] top→bottom per closed bar.\n"
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


def _build_responses_common(
    *,
    vector_store_ids: list[str],
    store: bool,
    include: list[str],
    reasoning_summary: str,
    reasoning_effort: str | None,
    model: str | None,
) -> dict[str, Any]:
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
    return common


def _run_chained_payload_batches(
    *,
    client: OpenAI,
    common: dict[str, Any],
    analysis_prompt: str,
    payloads: list[ChartOpenAIPayload],
    max_images_per_call: int,
    max_json_chars: int,
    chart_stamp: str | None,
    flow: str,
    model: str | None,
    previous_response_id: str | None = None,
    on_first_model_text: Optional[Callable[[str], None]] = None,
    batch_prompt_suffix: bool = True,
) -> tuple[str | None, list[str]]:
    """
    Run one or more chained API batches for ``payloads``.

    Returns ``(final_response_id, assistant_text_parts)``.
    """
    chunks = chunk_payloads(payloads, max_images_per_call)
    assistant_parts: list[str] = []
    prev_id: str | None = previous_response_id
    total = len(chunks)
    first_batch = prev_id is None

    for bi, batch in enumerate(chunks):
        if total == 1 or not batch_prompt_suffix:
            p_text = analysis_prompt
        else:
            n_img = sum(1 for k, _ in batch if k not in ("json", "csv"))
            n_json = sum(1 for k, _ in batch if k == "json")
            n_csv = sum(1 for k, _ in batch if k == "csv")
            extra = []
            if n_json:
                extra.append(f"{n_json} JSON block(s)")
            if n_csv:
                extra.append(f"{n_csv} CSV block(s)")
            if n_img:
                extra.append(f"{n_img} image(s)/URL(s)")
            p_text = (
                f"{analysis_prompt}\n\n"
                f"(Batch {bi + 1} of {total}: {', '.join(extra)}.)"
            )
        try:
            content = _build_mixed_chart_user_content(
                p_text, batch, max_json_chars=max_json_chars, chart_stamp=chart_stamp
            )
            kwargs: dict[str, Any] = {
                **common,
                "input": responses_input_messages(user_content=content),
            }
            if prev_id is not None:
                kwargs["previous_response_id"] = prev_id
            _log_openai_send(
                flow=flow,
                batch_index=bi + 1,
                total_batches=total,
                payloads=batch,
                model=model,
                chained=prev_id is not None,
            )
            r = client.responses.create(**kwargs)
        except Exception:
            _log_openai_error(
                flow=flow,
                batch_index=bi + 1,
                total_batches=total,
                payloads=batch,
            )
            raise
        prev_id = r.id
        chunk_text = (r.output_text or "").strip()
        _log_openai_receive(
            flow=flow,
            batch_index=bi + 1,
            total_batches=total,
            response_id=prev_id,
            output_text=chunk_text,
        )
        assistant_parts.append(chunk_text)
        if first_batch and bi == 0 and on_first_model_text is not None and chunk_text:
            on_first_model_text(chunk_text)

    return prev_id, assistant_parts


def run_full_analysis_two_phase_flow(
    *,
    api_key: str,
    charts_dir: Path,
    structure_prompt: str,
    footprint_prompt: str,
    max_images_per_call: int,
    vector_store_ids: list[str],
    store: bool,
    include: list[str],
    reasoning_summary: str = "auto",
    reasoning_effort: str | None = DEFAULT_REASONING_EFFORT,
    chart_payloads: list[ChartOpenAIPayload],
    max_coinmap_json_chars: int | None = None,
    on_first_model_text: Optional[Callable[[str], None]] = None,
    model: str | None = None,
    chart_stamp: str | None = None,
) -> PromptTwoStepResult:
    """
    FULL_ANALYSIS in two chained phases: TradingView structure, then GoCharting footprint.

    ``first_text`` = batch 1 output; ``after_charts`` = final batch 2 output (Schema A).
    """
    client = OpenAI(api_key=api_key)
    common = _build_responses_common(
        vector_store_ids=vector_store_ids,
        store=store,
        include=include,
        reasoning_summary=reasoning_summary,
        reasoning_effort=reasoning_effort,
        model=model,
    )
    mx_json = (
        max_coinmap_json_chars
        if max_coinmap_json_chars is not None
        else _default_max_coinmap_json_chars()
    )
    payloads = _filter_valid_chart_payloads(list(chart_payloads))
    tv_payloads, fp_payloads = split_openai_payloads_by_phase(payloads)
    if not tv_payloads:
        raise ValueError(
            "two-phase FULL_ANALYSIS: no structure payloads "
            "(expected TradingView DXY/main slots; optional GoCharting DXY overview PNG)"
        )
    if not fp_payloads:
        raise ValueError(
            "two-phase FULL_ANALYSIS: no footprint payloads "
            "(expected footprint prepared JSON or Coinmap; DXY excluded)"
        )

    prev_id, phase1_parts = _run_chained_payload_batches(
        client=client,
        common=common,
        analysis_prompt=structure_prompt.strip(),
        payloads=tv_payloads,
        max_images_per_call=max_images_per_call,
        max_json_chars=mx_json,
        chart_stamp=chart_stamp,
        flow="analysis-two-phase-1",
        model=model,
        on_first_model_text=on_first_model_text,
        batch_prompt_suffix=False,
    )
    assert prev_id is not None
    text_1 = "\n\n---\n\n".join(phase1_parts)

    prev_id, phase2_parts = _run_chained_payload_batches(
        client=client,
        common=common,
        analysis_prompt=footprint_prompt.strip(),
        payloads=fp_payloads,
        max_images_per_call=max_images_per_call,
        max_json_chars=mx_json,
        chart_stamp=chart_stamp,
        flow="analysis-two-phase-2",
        model=model,
        previous_response_id=prev_id,
        batch_prompt_suffix=True,
    )
    assert prev_id is not None
    text_2 = "\n\n---\n\n".join(phase2_parts)

    return PromptTwoStepResult(
        first_text=text_1,
        after_charts=text_2,
        final_response_id=prev_id,
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
    common = _build_responses_common(
        vector_store_ids=vector_store_ids,
        store=store,
        include=include,
        reasoning_summary=reasoning_summary,
        reasoning_effort=reasoning_effort,
        model=model,
    )

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
                    user_content=analysis_prompt.strip(),
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

    prev_id, assistant_parts = _run_chained_payload_batches(
        client=client,
        common=common,
        analysis_prompt=analysis_prompt,
        payloads=payloads,
        max_images_per_call=max_images_per_call,
        max_json_chars=mx_json,
        chart_stamp=chart_stamp,
        flow="analysis",
        model=model,
        on_first_model_text=on_first_model_text,
        batch_prompt_suffix=True,
    )
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
    "Cập nhật intraday: lần đầu sau [FULL_ANALYSIS] kèm morning_full_analysis.json + "
    "footprint_XAUUSD_5m.json (có thể kèm TradingView 15m ICT + TV M5).\n"
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
    "Nhiệm vụ chính: tìm **1 scalp đẹp nhất** trong phiên hiện tại (hop_luu >= 60, "
    "có đủ hợp lưu M5 để vào lệnh nhanh). "
    "Không cần đánh giá lại scalp cũ; chỉ tập trung vào scalp mới đủ chất lượng. "
    "Nếu có 2–3 scalp đủ chất lượng thì có thể trả thêm, nhưng không bắt buộc. "
    "Bắt buộc dùng label dạng `scalp_<id>` cho mỗi scalp trong `prices` "
    "(ví dụ: `scalp_1`, `scalp_2`, `scalp_3`). "
    "Không dùng label `plan_chinh` hay `plan_phu` cho luồng scalp này.\n"
)

_GOCHARTING_INTRADAY_CHART_READ_PRIORITY_HINT = (
    f"Ưu tiên {_prepared_footprint_pair_desc('XAUUSD')}: footprint[] + bar_flow. "
    "TradingView snapshot khi cần cấu trúc giá / liquidity.\n"
    f"{_GOCHARTING_CHART_READ_GUIDE}"
)


def _gocharting_intraday_chart_read_priority_hint(
    *,
    m5_only: bool,
    sym: str = DEFAULT_MAIN_CHART_SYMBOL,
) -> str:
    fp = _prepared_footprint_filename(sym, "5m") if m5_only else _prepared_footprint_pair_desc(sym)
    return (
        f"Ưu tiên {fp}: footprint[] + bar_flow. "
        "TradingView snapshot khi cần cấu trúc giá / liquidity.\n"
        f"{_GOCHARTING_CHART_READ_GUIDE}"
    )


def _gocharting_intraday_attachment_line(*, m5_only: bool, sym: str = DEFAULT_MAIN_CHART_SYMBOL) -> str:
    fp = _prepared_footprint_filename(sym, "5m") if m5_only else _prepared_footprint_pair_desc(sym)
    if m5_only:
        return f"Footprint spot **{sym}**: **{fp}** (M5 only)."
    return f"Footprint spot **{sym}**: **{fp}**."


_INTRADAY_UPDATE_SUFFIX = _GOCHARTING_INTRADAY_CHART_READ_PRIORITY_HINT + _INTRADAY_UPDATE_PLAN_HINT
_SCALP_UPDATE_SUFFIX = _GOCHARTING_INTRADAY_CHART_READ_PRIORITY_HINT + _SCALP_UPDATE_PLAN_HINT
_GOCHARTING_SCALP_UPDATE_SUFFIX = _SCALP_UPDATE_SUFFIX


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
    User message for intraday update (GoCharting footprint workflow).

    ``coinmap_attachment_mode`` (legacy name): ``m5_only`` / ``merged_m5`` → chỉ M5;
    ``merged`` / ``legacy`` (default) → M15 + M5.
    """
    time_line = format_intraday_update_time_line()
    mode = str(coinmap_attachment_mode or "merged").strip().lower()
    m5_only = mode in ("m5_only", "merged_m5", "merged_m5_only")
    attach = _gocharting_intraday_attachment_line(m5_only=m5_only)

    if first_after_all:
        if m5_only:
            files = (
                "Đính kèm **hai** phần theo thứ tự: **(1)** morning_full_analysis.json, "
                f"**(2)** {attach}\n"
            )
        else:
            files = (
                "Đính kèm **ba** phần theo thứ tự: **(1)** morning_full_analysis.json, "
                f"**(2)** {_prepared_footprint_filename('XAUUSD', '15m')}, "
                f"**(3)** {_prepared_footprint_filename('XAUUSD', '5m')} "
                f"({attach})\n"
            )
        return (
            "[INTRADAY_UPDATE]\n"
            f"{time_line}"
            "Phân tích buổi sáng (Schema A) nằm trong file **morning_full_analysis.json** đính kèm đầu tiên.\n"
            f"{files}"
            f"{_MORNING_CONTEXT_HINT}"
            f"{_INTRADAY_UPDATE_SUFFIX}"
        )

    return (
        "[INTRADAY_UPDATE]\n"
        f"{time_line}"
        "Tiếp tục chuỗi phản hồi sau lần [INTRADAY_UPDATE] trước.\n"
        f"Đính kèm {attach}\n"
        f"{_INTRADAY_UPDATE_SUFFIX}"
    )


def build_scalp_update_user_text(
    *,
    first_after_all: bool = False,
    coinmap_attachment_mode: str = "merged",
    footprint_source: str = "gocharting",
) -> str:
    """
    User message cho ``update-scalp``: tìm scalp đẹp nhất, label ``scalp_<id>`` (GoCharting footprint).

    ``coinmap_attachment_mode``: ``m5_only`` / ``merged_m5`` → chỉ M5; mặc định M15 + M5.
    """
    _ = footprint_source  # legacy; prompts are GoCharting-only
    time_line = format_intraday_update_time_line()
    mode = str(coinmap_attachment_mode or "merged").strip().lower()
    m5_only = mode in ("m5_only", "merged_m5", "merged_m5_only")
    attach = _gocharting_intraday_attachment_line(m5_only=m5_only)
    suffix = (
        _gocharting_intraday_chart_read_priority_hint(m5_only=m5_only) + _SCALP_UPDATE_PLAN_HINT
    )

    if first_after_all:
        if m5_only:
            files = (
                "Đính kèm **hai** phần theo thứ tự: **(1)** morning_full_analysis.json, "
                f"**(2)** {attach}\n"
            )
        else:
            files = (
                "Đính kèm **ba** phần theo thứ tự: **(1)** morning_full_analysis.json, "
                f"**(2)** {_prepared_footprint_filename('XAUUSD', '15m')}, "
                f"**(3)** {_prepared_footprint_filename('XAUUSD', '5m')} "
                f"({attach})\n"
            )
        return (
            "[INTRADAY_UPDATE]\n"
            f"{time_line}"
            "Phân tích buổi sáng (Schema A) nằm trong file **morning_full_analysis.json** đính kèm đầu tiên.\n"
            f"{files}"
            f"{_MORNING_CONTEXT_HINT}"
            f"{suffix}"
        )

    return (
        "[INTRADAY_UPDATE]\n"
        f"{time_line}"
        "Tiếp tục chuỗi phản hồi sau lần [INTRADAY_UPDATE] trước.\n"
        f"Đính kèm {attach}\n"
        f"{suffix}"
    )


# TradingView tab Nhật ký: giá chạm → footprint_XAUUSD_*.json + OpenAI (intraday).
# Trả về Schema E: chỉ ``phan_tich_alert`` + ``intraday_hanh_dong``; nếu VÀO LỆNH, dùng trade_line theo baseline vùng.
JOURNAL_INTRADAY_FIRST_USER_TEMPLATE = (
    "[INTRADAY_ALERT]\n"
    "Cảnh báo TradingView đã kích hoạt tại mức giá {touched_price}.\n"
    f"Đính kèm **{_prepared_footprint_filename('XAUUSD', '5m')}** "
    "(spot ohlc + footprint[] + bar_flow).\n"
)

JOURNAL_INTRADAY_RETRY_USER_TEMPLATE = (
    "[INTRADAY_ALERT]\n"
    "Tiếp tục đánh giá sau {wait_minutes} phút; vẫn theo dõi mức đã chạm {touched_price}.\n"
    f"Đính kèm bản **{_prepared_footprint_filename('XAUUSD', '5m')}** mới.\n"
)

# Sau khi giá last realtime chạm TP1 (vùng đang ``cho_tp1``).
TP1_POST_TOUCH_USER_TEMPLATE = (
    "[TRADE_MANAGEMENT]\n"
    f"Đánh giá Footprint spot M5 ({_prepared_footprint_filename('XAUUSD', '5m')}) đính kèm "
    "(giữ hay thoát / chỉnh SL/TP).\n"
    f"{_GOCHARTING_TRADE_MANAGEMENT_SUFFIX}"
    "Vùng (label): {plan_label}\n"
    "{entry_side} entry: {entry_price}\n"
    "SL hiện tại: {current_sl}\n"
    "TP hiện tại: {current_tp}\n\n"
)

# Daemon zones: follow-up theo mốc R khi đang ``cho_tp1`` (sau arm TP1).
R1_POST_TOUCH_USER_TEMPLATE = (
    "[TRADE_MANAGEMENT]\n"
    "Giá đã đạt mức {r_level}R; "
    f"đánh giá Footprint spot M5 ({_prepared_footprint_filename('XAUUSD', '5m')}) đính kèm "
    "(giữ hay thoát / chỉnh SL/TP).\n"
    f"{_GOCHARTING_TRADE_MANAGEMENT_SUFFIX}"
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
    f"Đánh giá Footprint spot M5 ({_prepared_footprint_filename('XAUUSD', '5m')}) đính kèm "
    "(footprint spot XAUUSD; giá broker MT5) "
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
    gocharting_detail_max_back_steps: int | None = None,
    gocharting_cfg: dict | None = None,
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

    gc_cfg = gocharting_cfg if gocharting_cfg is not None else _default_gocharting_cfg()
    json_payloads = openai_payloads_for_attachment_paths(
        paths,
        gocharting_cfg=gc_cfg,
        gocharting_detail_zoom_only=gocharting_detail_zoom_only,
        gocharting_detail_max_back_steps=gocharting_detail_max_back_steps,
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
