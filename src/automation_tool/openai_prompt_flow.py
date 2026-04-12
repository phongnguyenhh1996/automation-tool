from __future__ import annotations

"""
OpenAI Responses: user turns are short routing tags + context.

Full trading/output rules live in the OpenAI Prompt (``OPENAI_PROMPT_ID``) — keep that
prompt in sync with ``system-prompt.md`` at the repo root. Do not duplicate schema here.
"""

import io
import json
import logging
import os
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
        "Đính kèm theo thứ tự (TradingView = JSON; Coinmap = JSON): "
        "TradingView DXY (H4, H1, M15) → Coinmap USDINDEX Footprint 15m → "
        f"TradingView {sym} (H4, H1, M15, M5) → Coinmap {sym} (Footprint M15, M5).\n"
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


# Files API: ``expires_after`` must be 3600–2592000 seconds per OpenAI docs.
_FILE_EXPIRES_AFTER_SECONDS = 86400  # 1 day


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
    if "_tradingview_" in path.name:
        header = f"[TradingView OHLC (tvdatafeed) — file: {path.name}]\n"
    else:
        header = f"[Coinmap API export — file: {path.name}]\n"
    body = compact
    if max_chars > 0 and len(body) > max_chars:
        body = body[:max_chars] + f"\n… [truncated: {len(compact)} chars → {max_chars}; raise COINMAP_JSON_MAX_CHARS]"
    return header, body


def _upload_user_data_json(client: OpenAI, path: Path, body: str) -> str:
    raw = body.encode("utf-8")
    bio = io.BytesIO(raw)
    fo = client.files.create(
        file=(path.name, bio),
        purpose="user_data",
        expires_after={"anchor": "created_at", "seconds": _FILE_EXPIRES_AFTER_SECONDS},
    )
    _log.info(
        "[openai files] %s → %s (%d B, user_data, expires 1d)",
        path.name,
        fo.id,
        len(raw),
    )
    return fo.id


def _json_paths_to_headers_and_file_ids(
    client: OpenAI,
    paths: list[Path],
    *,
    max_json_chars: int,
) -> list[tuple[str, str]]:
    """``(header, file_id)`` per path, same order as ``paths``."""
    if not paths:
        return []
    n = len(paths)
    if n > 1:
        _log.info("[openai files] uploading %d JSON file(s) in parallel → OpenAI Files API", n)
    if n == 1:
        p0 = paths[0]
        h, b = _json_file_header_and_body(p0, max_chars=max_json_chars)
        return [(h, _upload_user_data_json(client, p0, b))]
    workers = min(n, max(4, (os.cpu_count() or 2) * 2))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        prepared = list(
            ex.map(
                lambda pp: _json_file_header_and_body(pp, max_chars=max_json_chars),
                paths,
            )
        )
    with ThreadPoolExecutor(max_workers=workers) as ex:
        file_ids = list(
            ex.map(
                lambda t: _upload_user_data_json(client, t[0], t[1][1]),
                zip(paths, prepared),
            )
        )
    return [(h, fid) for (h, _), fid in zip(prepared, file_ids)]


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
    client: OpenAI,
    max_json_chars: int,
) -> list[dict[str, Any]]:
    json_paths = [p for k, p in payloads if k == "json" and isinstance(p, Path)]
    json_queue = iter(
        _json_paths_to_headers_and_file_ids(
            client, json_paths, max_json_chars=max_json_chars
        )
    )
    parts: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    image_paths = [p for k, p in payloads if k == "image" and isinstance(p, Path)]
    data_urls = _image_paths_to_data_urls(image_paths)
    for kind, p in payloads:
        if kind == "json":
            assert isinstance(p, Path)
            h, fid = next(json_queue)
            parts.append({"type": "input_text", "text": h})
            parts.append({"type": "input_file", "file_id": fid})
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
) -> PromptTwoStepResult:
    """
    Một lần (hoặc nhiều batch nếu quá nhiều ảnh): user message multimodal với ``analysis_prompt``
    + chart payloads, không còn bước text-only tách biệt.

    ``after_charts`` chứa toàn bộ output; ``first_text`` luôn ``""``.

    ``on_first_model_text`` (tuỳ chọn): gọi với text assistant của **batch đầu tiên**
    (khi có multimodal); dùng cho VÀO LỆNH + MT5 / cập nhật ``last_alert_prices``.
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
            p_text, batch, client=client, max_json_chars=mx_json
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
    )


DEFAULT_UPDATE_PROMPT_TEMPLATE = (
    "[INTRADAY_UPDATE]\n"
    "So sánh baseline sáng với footprint XAUUSD M15 + M5 (JSON đính kèm theo thứ tự file).\n"
    "Baseline giá cảnh báo — plan_chinh: {p1}, plan_phu: {p2}, scalp: {p3}.\n"
)


def build_intraday_update_user_text(
    p1: float,
    p2: float,
    p3: float,
    *,
    zones_snapshot: str,
) -> str:
    """
    User message for ``coinmap-automation update``: baseline + zone snapshot + tasking.
    Coinmap JSON files (M15 then M5) are attached separately via Files API.
    """
    snap = (zones_snapshot or "").strip()
    if not snap.endswith("\n"):
        snap = snap + "\n"
    return (
        "[INTRADAY_UPDATE]\n"
        "So sánh **baseline sáng** với tình hình **hiện tại** dựa trên footprint Coinmap.\n"
        "Đính kèm **hai** file JSON theo thứ tự: **(1) M15**, **(2) M5**.\n"
        f"Baseline giá cảnh báo — plan_chinh: {p1}, plan_phu: {p2}, scalp: {p3}.\n"
        "\n"
        "--- Trạng thái các vùng chờ cho tới hiện tại ---\n"
        f"{snap}"
        "\n"
        "Yêu cầu phân tích:\n"
        "- Với các zone **đang chờ** (vung_cho): còn hợp lý với cấu trúc + order flow hiện tại không? "
        "Có nên giữ / điều chỉnh dữ liệu vùng chờ không?\n"
        "- Với zone **đã loại** hoặc **đã vào lệnh / done**: trên M15+M5 có **vùng thay thế** tốt hơn không?"
    )

# TradingView tab Nhật ký: giá chạm → Coinmap M5 + OpenAI (intraday).
JOURNAL_INTRADAY_FIRST_USER_TEMPLATE = (
    "[INTRADAY_ALERT]\n"
    "Cảnh báo TradingView đã kích hoạt tại mức giá {touched_price}.\n"
    "Đính kèm footprint Coinmap XAUUSD M5 mới nhất.\n"
)

JOURNAL_INTRADAY_RETRY_USER_TEMPLATE = (
    "[INTRADAY_ALERT]\n"
    "Tiếp tục đánh giá sau {wait_minutes} phút; vẫn theo dõi mức đã chạm {touched_price}.\n"
    "Đính kèm footprint Coinmap XAUUSD M5 mới.\n"
)

# Sau khi giá last realtime chạm TP1 (vùng đang ``cho_tp1``).
TP1_POST_TOUCH_USER_TEMPLATE = (
    "[TRADE_MANAGEMENT]\n"
    "Giá đã chạm mức TP1 của lệnh đang theo dõi; đánh giá Footprint M5 đính kèm (giữ hay thoát / chỉnh dòng lệnh).\n"
    "Vùng (label): {plan_label}\n"
    "Mức TP1 từ trade_line: {tp1_price}\n\n"
)


def run_single_followup_responses(
    *,
    api_key: str,
    prompt_id: str,
    prompt_version: str | None,
    user_text: str,
    coinmap_json_paths: Sequence[Path],
    previous_response_id: str,
    vector_store_ids: list[str],
    store: bool,
    include: list[str],
    reasoning_summary: str = "auto",
    max_coinmap_json_chars: int | None = None,
) -> tuple[str, str]:
    """
    One multimodal user turn chained to ``previous_response_id`` (intraday update).

    ``coinmap_json_paths``: one or more Coinmap export JSON paths, uploaded in order
    (same Files API + ``input_file`` pattern as multi-json in ``run_analysis_responses_flow``).

    Returns ``(output_text, new_response_id)``.
    """
    paths = [p for p in coinmap_json_paths if isinstance(p, Path)]
    if not paths:
        raise ValueError("coinmap_json_paths must contain at least one Path")
    for p in paths:
        if not p.is_file():
            raise FileNotFoundError(f"Coinmap JSON not found: {p}")

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

    mx_json = (
        max_coinmap_json_chars
        if max_coinmap_json_chars is not None
        else _default_max_coinmap_json_chars()
    )

    json_payloads: list[tuple[str, Path]] = [("json", p) for p in paths]
    content = _build_mixed_chart_user_content(
        user_text,
        json_payloads,
        client=client,
        max_json_chars=mx_json,
    )
    r = client.responses.create(
        **common,
        previous_response_id=previous_response_id,
        input=[
            {
                "type": "message",
                "role": "user",
                "content": content,
            }
        ],
    )
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

    r = client.responses.create(
        **common,
        previous_response_id=previous_response_id,
        input=(user_text or "").strip(),
    )
    out = (r.output_text or "").strip()
    return out, r.id
