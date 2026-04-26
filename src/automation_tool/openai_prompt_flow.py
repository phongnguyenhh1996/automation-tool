from __future__ import annotations

"""
OpenAI Responses: user turns are short routing tags + context.

Full trading/output rules live in the OpenAI Prompt (``OPENAI_PROMPT_ID``) — keep that
prompt in sync with ``system-prompt.md`` at the repo root. Do not duplicate schema here.
"""

import json
import logging
import os
import base64
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, NamedTuple, Optional

from openai import OpenAI

from automation_tool.coinmap_openai_slim import (
    should_slim_coinmap_json_path,
    slim_coinmap_export_for_openai,
)
from automation_tool.images import (
    DEFAULT_MAIN_CHART_SYMBOL,
    ChartOpenAIPayload,
    chunk_payloads,
    image_to_data_url,
    normalize_main_chart_symbol,
    ordered_chart_openai_payloads,
    read_main_chart_symbol,
)
from automation_tool.state_files import MORNING_FULL_ANALYSIS_FILENAME
from automation_tool.zones_state import format_intraday_update_time_line

_log = logging.getLogger(__name__)


class PromptTwoStepResult(NamedTuple):
    """``first_text`` luôn rỗng; ``after_charts`` chứa toàn bộ output phân tích (một hoặc nhiều batch)."""

    first_text: str
    after_charts: str
    final_response_id: str

    def full_text(self) -> str:
        if not self.after_charts:
            return self.first_text
        return f"{self.first_text}\n\n---\n\n{self.after_charts}" if self.first_text else self.after_charts


def default_analysis_prompt(main_symbol: str | None = None) -> str:
    """
    Default user message for multimodal analysis.

    ``main_symbol`` is the main pair (TradingView/Coinmap); invalid/empty →
    ``DEFAULT_MAIN_CHART_SYMBOL``. Schema and rules are defined in Prompt Studio
    (``system-prompt.md``): ``[FULL_ANALYSIS]`` → Schema A.
    """
    sym = DEFAULT_MAIN_CHART_SYMBOL
    if main_symbol and str(main_symbol).strip():
        try:
            sym = normalize_main_chart_symbol(str(main_symbol).strip())
        except ValueError:
            pass
    return (
        "[FULL_ANALYSIS]\n"
        f"Cặp chính: {sym}.\n"
        "Đính kèm theo thứ tự (TradingView = JSON; Coinmap = JSON gộp nếu có _merged), "
        "9–10 dữ liệu: "
        "TradingView DXY (H4, H1, M15) → "
        f"TradingView {sym} (H4, H1, M15, M5) → Coinmap DXY (M15) → "
        f"Coinmap {sym} (M15 + M5 gộp nếu có file merged, hoặc 2 file riêng).\n"
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


def _json_file_header_and_body(path: Path, *, max_chars: int) -> tuple[str, str]:
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
        compact = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    except json.JSONDecodeError:
        compact = raw
    if path.name == MORNING_FULL_ANALYSIS_FILENAME:
        header = f"[FULL_ANALYSIS snapshot — file: {path.name}]\n"
    elif "_tradingview_" in path.name:
        header = f"[TradingView OHLC (tvdatafeed) — file: {path.name}]\n"
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
) -> list[tuple[str, str]]:
    """Return ``(header, body)`` per path, same order as ``paths``."""
    if not paths:
        return []
    if len(paths) == 1:
        p0 = paths[0]
        mx0 = _max_json_chars_for_path(p0, default_max=max_json_chars)
        return [_json_file_header_and_body(p0, max_chars=mx0)]
    n = len(paths)
    workers = min(n, max(4, (os.cpu_count() or 2) * 2))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(
            ex.map(
                lambda pp: _json_file_header_and_body(
                    pp,
                    max_chars=_max_json_chars_for_path(pp, default_max=max_json_chars),
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


def _build_mixed_chart_user_content(
    prompt: str,
    payloads: list[ChartOpenAIPayload],
    *,
    max_json_chars: int,
) -> list[dict[str, Any]]:
    json_paths = [p for k, p in payloads if k == "json" and isinstance(p, Path)]
    json_queue = iter(_prepare_json_headers_bodies(json_paths, max_json_chars=max_json_chars))
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
                # Keep TradingView JSON as plain text to avoid external storage dependencies.
                # (Coinmap JSON is attached as a file; non-Coinmap JSON stays inline.)
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
            parts.append(
                {
                    "type": "input_image",
                    "image_url": data_urls[p],
                    "detail": "auto",
                }
            )
    return parts


def _prompt_dict(prompt_id: str, prompt_version: str | None) -> dict[str, Any]:
    d: dict[str, Any] = {"id": prompt_id}
    if prompt_version:
        d["version"] = prompt_version
    return d


def _merge_model(common: dict[str, Any], model: str | None) -> None:
    if model and str(model).strip():
        common["model"] = str(model).strip()


def run_analysis_responses_flow(
    *,
    api_key: str,
    prompt_id: str,
    prompt_version: str | None,
    charts_dir: Path,
    analysis_prompt: str,
    max_images_per_call: int,
    vector_store_ids: list[str],
    store: bool,
    include: list[str],
    reasoning_summary: str = "auto",
    chart_paths: list[Path] | None = None,
    chart_payloads: list[ChartOpenAIPayload] | None = None,
    max_coinmap_json_chars: int | None = None,
    on_first_model_text: Optional[Callable[[str], None]] = None,
    purge_json_attachment_storage: bool = False,
    purge_openai_user_data_files: bool | None = None,
    model: str | None = None,
) -> PromptTwoStepResult:
    """
    Một lần (hoặc nhiều batch nếu quá nhiều ảnh): user message multimodal với ``analysis_prompt``
    + chart payloads, không còn bước text-only tách biệt.

    ``after_charts`` chứa toàn bộ output; ``first_text`` luôn ``""``.

    ``on_first_model_text`` (tuỳ chọn): gọi với text assistant của **batch đầu tiên**
    (khi có multimodal); dùng cho VÀO LỆNH + MT5 / cập nhật ``last_alert_prices``.

    ``purge_json_attachment_storage`` / ``purge_openai_user_data_files``: legacy flags from the
    former Cloudinary-based JSON attachment path. JSON is now attached inline (text) or as
    base64 ``input_file.file_data`` (Coinmap), so these are effectively no-ops.
    """
    if not (analysis_prompt or "").strip():
        analysis_prompt = default_analysis_prompt(read_main_chart_symbol(charts_dir))
    client = OpenAI(api_key=api_key)
    prompt = _prompt_dict(prompt_id, prompt_version)
    tools: list[dict[str, Any]] = []
    if vector_store_ids:
        tools.append(
            {
                "type": "file_search",
                "vector_store_ids": list(vector_store_ids),
            }
        )

    reasoning: dict[str, Any] = {"summary": reasoning_summary}

    common: dict[str, Any] = {
        "prompt": prompt,
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
        r = client.responses.create(**common, input=analysis_prompt.strip())
        out = (r.output_text or "").strip()
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
                f"(Batch {bi + 1} of {total}: {n_img} image(s), {n_json} Coinmap JSON block(s).)"
            )
        content = _build_mixed_chart_user_content(
            p_text, batch, max_json_chars=mx_json
        )
        kwargs: dict[str, Any] = {
            **common,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": content,
                }
            ],
        }
        if prev_id is not None:
            kwargs["previous_response_id"] = prev_id
        r = client.responses.create(**kwargs)
        prev_id = r.id
        chunk_text = (r.output_text or "").strip()
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
    prompt_id: str,
    prompt_version: str | None,
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
        prompt_id=prompt_id,
        prompt_version=prompt_version,
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
    "Cập nhật intraday: lần đầu sau [FULL_ANALYSIS] kèm morning_full_analysis.json + Coinmap merged (M15+M5).\n"
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
    * ``coinmap_attachment_mode="legacy"``: như trước — file M15 và M5 tách riêng.

    * ``first_after_all=True``: morning JSON + Coinmap (merged hoặc hai file raw).
    * ``first_after_all=False``: chỉ Coinmap (merged hoặc M15+M5); nối chuỗi ``[INTRADAY_UPDATE]``.
    """
    time_line = format_intraday_update_time_line()
    merged = str(coinmap_attachment_mode or "merged").strip().lower() != "legacy"

    if first_after_all:
        if merged:
            return (
                "[INTRADAY_UPDATE]\n"
                f"{time_line}"
                "Phân tích buổi sáng (Schema A) nằm trong file **morning_full_analysis.json** đính kèm đầu tiên.\n"
                "Đính kèm **hai** file JSON theo thứ tự: **(1)** morning_full_analysis.json, **(2)** một file "
                "**Coinmap merged** cho cặp chính (cùng schema ``coinmap_merged``: khung 15m và 5m trong ``frames``, "
                "footprint và summary theo từng khung).\n"
            )
        return (
            "[INTRADAY_UPDATE]\n"
            f"{time_line}"
            "Phân tích buổi sáng (Schema A) nằm trong file **morning_full_analysis.json** đính kèm đầu tiên.\n"
            "Đính kèm **ba** file JSON theo thứ tự: **(1)** morning_full_analysis.json, **(2)** M15, **(3)** M5 "
            "(footprint cặp chính).\n"
        )

    if merged:
        return (
            "[INTRADAY_UPDATE]\n"
            f"{time_line}"
            "Tiếp tục chuỗi phản hồi sau lần [INTRADAY_UPDATE] trước.\n"
            "Đính kèm **một** file JSON: **Coinmap merged** cho cặp chính (15m và 5m trong cùng file).\n"
        )
    return (
        "[INTRADAY_UPDATE]\n"
        f"{time_line}"
        "Tiếp tục chuỗi phản hồi sau lần [INTRADAY_UPDATE] trước.\n"
        "Đính kèm **hai** file JSON theo thứ tự: **(1) M15**, **(2) M5** (footprint cặp chính).\n"
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
    "Giá đã chạm mức TP1 của lệnh đang theo dõi; đánh giá Footprint M5 đính kèm (giữ hay thoát / chỉnh dòng lệnh).\n"
    "Vùng (label): {plan_label}\n"
    "Mức TP1 từ trade_line: {tp1_price}\n\n"
    "trade_line đã vào MT5: {trade_line_snip}\n\n"
)

# Daemon zones: giá đạt +1R (lợi nhuận bằng khoảng |Entry−SL|) khi đang ``cho_tp1`` (sau arm TP1).
R1_POST_TOUCH_USER_TEMPLATE = (
    "[TRADE_MANAGEMENT]\n"
    "Giá đã đạt mức 1R; "
    "đánh giá Footprint M5 đính kèm (giữ hay thoát / chỉnh dòng lệnh).\n"
    "Vùng (label): {plan_label}\n"
    "Entry tham chiếu: {entry_ref}\n"
    "Mức 1R (hướng có lợi): {r1_price}\n"
    "trade_line đã vào MT5: {trade_line_snip}\n\n"
)


def run_single_followup_responses(
    *,
    api_key: str,
    prompt_id: str,
    prompt_version: str | None,
    user_text: str,
    coinmap_json_paths: Sequence[Path],
    previous_response_id: str | None,
    morning_snapshot_path: Path | None = None,
    vector_store_ids: list[str],
    store: bool,
    include: list[str],
    reasoning_summary: str | None = "auto",
    reasoning_effort: str | None = None,
    max_coinmap_json_chars: int | None = None,
    model: str | None = None,
) -> tuple[str, str]:
    """
    One multimodal user turn: optional ``morning_snapshot_path`` + Coinmap JSON paths, uploaded in order
    (Coinmap: base64 ``input_file.file_data``; other JSON: inline text).

    If ``previous_response_id`` is ``None``, starts a **new** Responses thread (no chain).
    Otherwise chains to that id (intraday alert, TP1, etc.).

    ``reasoning_summary`` / ``reasoning_effort``: when ``reasoning_summary`` is ``None``, the request
    omits the ``reasoning`` field so the stored prompt (dashboard) controls reasoning. Otherwise
    ``reasoning`` is sent; non-empty ``reasoning_effort`` adds ``reasoning.effort``.
    """
    paths: list[Path] = []
    if morning_snapshot_path is not None:
        mp = morning_snapshot_path
        if not isinstance(mp, Path) or not mp.is_file():
            raise FileNotFoundError(f"Morning analysis JSON not found: {morning_snapshot_path}")
        paths.append(mp)
    paths.extend([p for p in coinmap_json_paths if isinstance(p, Path)])
    if not paths:
        raise ValueError("Need at least one JSON path (morning_snapshot_path and/or coinmap_json_paths)")
    for p in paths:
        if not p.is_file():
            raise FileNotFoundError(f"JSON attachment not found: {p}")

    client = OpenAI(api_key=api_key)
    prompt = _prompt_dict(prompt_id, prompt_version)
    tools: list[dict[str, Any]] = []
    if vector_store_ids:
        tools.append(
            {
                "type": "file_search",
                "vector_store_ids": list(vector_store_ids),
            }
        )

    common: dict[str, Any] = {
        "prompt": prompt,
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

    json_payloads: list[tuple[str, Path]] = [("json", p) for p in paths]
    content = _build_mixed_chart_user_content(
        user_text,
        json_payloads,
        max_json_chars=mx_json,
    )
    create_kw: dict[str, Any] = {
        **common,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": content,
            }
        ],
    }
    if previous_response_id is not None and str(previous_response_id).strip():
        create_kw["previous_response_id"] = str(previous_response_id).strip()
    r = client.responses.create(**create_kw)
    out = (r.output_text or "").strip()
    return out, r.id


def run_text_followup_responses(
    *,
    api_key: str,
    prompt_id: str,
    prompt_version: str | None,
    user_text: str,
    previous_response_id: str,
    vector_store_ids: list[str],
    store: bool,
    include: list[str],
    reasoning_summary: str = "auto",
    model: str | None = None,
) -> tuple[str, str]:
    """
    One text-only user turn chained to ``previous_response_id``.

    Returns ``(output_text, new_response_id)``.
    """
    client = OpenAI(api_key=api_key)
    prompt = _prompt_dict(prompt_id, prompt_version)
    tools: list[dict[str, Any]] = []
    if vector_store_ids:
        tools.append(
            {
                "type": "file_search",
                "vector_store_ids": list(vector_store_ids),
            }
        )

    reasoning: dict[str, Any] = {"summary": reasoning_summary}

    common: dict[str, Any] = {
        "prompt": prompt,
        "store": store,
        "include": include,
        "reasoning": reasoning,
    }
    if tools:
        common["tools"] = tools
    _merge_model(common, model)

    r = client.responses.create(
        **common,
        previous_response_id=previous_response_id,
        input=(user_text or "").strip(),
    )
    out = (r.output_text or "").strip()
    return out, r.id
