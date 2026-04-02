from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, NamedTuple

from openai import OpenAI

from automation_tool.coinmap_openai_slim import (
    should_slim_coinmap_json_path,
    slim_coinmap_export_for_openai,
)
from automation_tool.images import (
    chunk_payloads,
    image_to_data_url,
    ordered_chart_openai_payloads,
)


class PromptTwoStepResult(NamedTuple):
    """``first_text`` luôn rỗng; ``after_charts`` chứa toàn bộ output phân tích (một hoặc nhiều batch)."""

    first_text: str
    after_charts: str
    final_response_id: str

    def full_text(self) -> str:
        if not self.after_charts:
            return self.first_text
        return f"{self.first_text}\n\n---\n\n{self.after_charts}" if self.first_text else self.after_charts


DEFAULT_ANALYSIS_PROMPT = (
    "Chạy quy trình phân tích XAUUSD. "
    "Dữ liệu đính kèm theo thứ tự cố định (TradingView = ảnh; Coinmap = JSON export API nếu có, không thì ảnh): "
    "TradingView DXY (4h, 1h, 15m) → Coinmap USDINDEX 15m → "
    "TradingView XAUUSD (4h, 1h, 15m, 5m) → Coinmap XAUUSD (15m, 5m).\n\n"
    "Trả về một JSON object hợp lệ duy nhất (có thể bọc trong ```json). Schema gợi ý:\n"
    '- "out_chi_tiet": string (phân tích chi tiết)\n'
    '- "output_ngan_gon": string (tóm tắt)\n'
    '- "prices": [ {"label":"plan_chinh"|"plan_phu"|"scalp","value":number} ] — đủ 3 phần tử\n'
    '- "intraday_hanh_dong": "chờ" | "loại" | "VÀO LỆNH" hoặc null nếu không áp dụng\n'
    '- "trade_line": string — một dòng lệnh dạng BUY/SELL … | SL … | TP1 … | Lot … hoặc ""\n'
    '- "no_change": boolean — chỉ dùng rõ trong luồng update intraday; phân tích sáng có thể bỏ qua hoặc false\n'
)

# Tương thích ngược: gộp vào DEFAULT_ANALYSIS_PROMPT
DEFAULT_FIRST_PROMPT = DEFAULT_ANALYSIS_PROMPT
DEFAULT_FOLLOW_UP_PROMPT = ""


def _default_max_coinmap_json_chars() -> int:
    raw = os.getenv("COINMAP_JSON_MAX_CHARS", "").strip()
    if raw.isdigit():
        return max(0, int(raw))
    return 1_500_000


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


def _json_file_to_input_text(path: Path, *, max_chars: int) -> str:
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
    header = f"[Coinmap API export — file: {path.name}]\n"
    body = compact
    if max_chars > 0 and len(body) > max_chars:
        body = body[:max_chars] + f"\n… [truncated: {len(compact)} chars → {max_chars}; raise COINMAP_JSON_MAX_CHARS]"
    return header + body


def _build_mixed_chart_user_content(
    prompt: str,
    payloads: list[tuple[str, Path]],
    *,
    max_json_chars: int,
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    for kind, p in payloads:
        if kind == "json":
            parts.append(
                {
                    "type": "input_text",
                    "text": _json_file_to_input_text(p, max_chars=max_json_chars),
                }
            )
        else:
            parts.append(
                {
                    "type": "input_image",
                    "image_url": image_to_data_url(p),
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
    chart_payloads: list[tuple[str, Path]] | None = None,
    max_coinmap_json_chars: int | None = None,
) -> PromptTwoStepResult:
    """
    Một lần (hoặc nhiều batch nếu quá nhiều ảnh): user message multimodal với ``analysis_prompt``
    + chart payloads, không còn bước text-only tách biệt.

    ``after_charts`` chứa toàn bộ output; ``first_text`` luôn ``""``.
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

    mx_json = (
        max_coinmap_json_chars
        if max_coinmap_json_chars is not None
        else _default_max_coinmap_json_chars()
    )

    if chart_payloads is not None:
        payloads = [(k, p) for k, p in chart_payloads if p.is_file()]
    elif chart_paths is not None:
        payloads = [("image", p) for p in chart_paths if p.is_file()]
    else:
        payloads = ordered_chart_openai_payloads(charts_dir)

    if not payloads:
        r = client.responses.create(**common, input=analysis_prompt.strip())
        out = (r.output_text or "").strip()
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
        assistant_parts.append((r.output_text or "").strip())

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
    chart_payloads: list[tuple[str, Path]] | None = None,
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
        analysis_prompt = a or DEFAULT_ANALYSIS_PROMPT
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
    )


DEFAULT_UPDATE_PROMPT_TEMPLATE = (
    "Dựa trên dữ liệu XAUUSD M5 mới, so sánh với phân tích sáng.\n"
    "Baseline 3 mức (plan_chinh, plan_phu, scalp): {p1}, {p2}, {p3}\n\n"
    "Trả về một JSON object duy nhất:\n"
    '- "no_change": true nếu ba vùng giá không đổi so với baseline; false nếu có thay đổi.\n'
    '- Nếu no_change là false: "prices" phải có đủ 3 phần tử với label plan_chinh, plan_phu, scalp và value mới.\n'
    '- Nếu no_change là true: có thể đặt no_change true và không cần mô tả lại section markdown; '
    "optional: out_chi_tiet / output_ngan_gon ngắn.\n"
    '- Các field khác (intraday_hanh_dong, trade_line) để null hoặc "" nếu không dùng.'
)

# TradingView tab Nhật ký: giá chạm → Coinmap M5 + OpenAI (intraday).
JOURNAL_INTRADAY_FIRST_USER_TEMPLATE = (
    "Cảnh báo TradingView đã kích hoạt tại mức giá {touched_price} "
    "(một trong ba vùng chờ: {p1}, {p2}, {p3}).\n"
    "Dòng Nhật ký TradingView: {journal_line}\n\n"
    "Đính kèm dữ liệu Coinmap XAUUSD khung M5 mới nhất.\n"
    "Trả về một JSON object duy nhất:\n"
    '- "intraday_hanh_dong": "chờ" | "loại" | "VÀO LỆNH"\n'
    '- "trade_line": luôn phải có key này. '
    'Một dòng pipe không rỗng khi intraday_hanh_dong là "VÀO LỆNH" '
    "(BUY/SELL LIMIT|STOP|MARKET … | SL … | TP1 … | Lot …) để có thể gửi MT5; "
    'nếu "chờ" hoặc "loại" thì đặt trade_line là chuỗi rỗng "".\n'
    '- "output_ngan_gon", "out_chi_tiet": tùy cần.\n'
    "Không dùng giá trị khác cho intraday_hanh_dong trong luồng này."
)

JOURNAL_INTRADAY_RETRY_USER_TEMPLATE = (
    "Tiếp tục đánh giá sau {wait_minutes} phút: vẫn theo dõi mức đã chạm {touched_price}.\n"
    "Bối cảnh Nhật ký (lần kích hoạt): {journal_line}\n\n"
    "Đính kèm Coinmap XAUUSD M5 mới.\n"
    "Cùng schema JSON như tin nhắn trước: luôn có key trade_line "
    '(rỗng nếu "chờ" hoặc "loại"); intraday_hanh_dong chỉ "chờ", "loại", hoặc "VÀO LỆNH"; '
    'nếu "VÀO LỆNH" thì trade_line phải là một dòng lệnh pipe hợp lệ (không rỗng) cho MT5.'
)


def run_single_followup_responses(
    *,
    api_key: str,
    prompt_id: str,
    prompt_version: str | None,
    user_text: str,
    coinmap_json_path: Path,
    previous_response_id: str,
    vector_store_ids: list[str],
    store: bool,
    include: list[str],
    reasoning_summary: str = "auto",
    max_coinmap_json_chars: int | None = None,
) -> tuple[str, str]:
    """
    One multimodal user turn chained to ``previous_response_id`` (intraday update).

    Returns ``(output_text, new_response_id)``.
    """
    if not coinmap_json_path.is_file():
        raise FileNotFoundError(f"Coinmap JSON not found: {coinmap_json_path}")

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

    content = _build_mixed_chart_user_content(
        user_text,
        [("json", coinmap_json_path)],
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
