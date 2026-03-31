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
    """``after_charts`` is empty when there are no chart images (step 2 skipped)."""

    first_text: str
    after_charts: str

    def full_text(self) -> str:
        if not self.after_charts:
            return self.first_text
        return f"{self.first_text}\n\n---\n\n{self.after_charts}"


DEFAULT_FIRST_PROMPT = "chạy quy trình phân tích xauusd"
DEFAULT_FOLLOW_UP_PROMPT = (
    "Đây là dữ liệu theo thứ tự cố định (TradingView = ảnh; Coinmap = JSON export API nếu có, không thì ảnh): "
    "TradingView DXY (4h, 1h, 15m) → Coinmap USDINDEX 15m → "
    "TradingView XAUUSD (4h, 1h, 15m, 5m) → Coinmap XAUUSD (15m, 5m). "
    "Phân tích tiếp theo quy trình."
)


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
    Step 1: text-only user input (Responses API stores state when ``store`` is True).
    Step 2+: multimodal user message(s) chained with ``previous_response_id``.

    Returns step-1 text and step-2+ text separately (use ``.full_text()`` for stdout;
    Telegram should use only ``after_charts`` when non-empty).
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

    r1 = client.responses.create(
        **common,
        input=first_prompt,
    )
    text1 = (r1.output_text or "").strip()
    prev_id = r1.id

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
        return PromptTwoStepResult(first_text=text1, after_charts="")

    chunks = chunk_payloads(payloads, max_images_per_call)
    assistant_parts: list[str] = []
    total = len(chunks)

    for bi, batch in enumerate(chunks):
        if total == 1:
            p_text = follow_up_prompt
        else:
            n_img = sum(1 for k, _ in batch if k != "json")
            n_json = sum(1 for k, _ in batch if k == "json")
            p_text = (
                f"{follow_up_prompt}\n\n"
                f"(Batch {bi + 1} of {total}: {n_img} image(s), {n_json} Coinmap JSON block(s).)"
            )
        content = _build_mixed_chart_user_content(
            p_text, batch, max_json_chars=mx_json
        )
        r = client.responses.create(
            **common,
            previous_response_id=prev_id,
            input=[
                {
                    "type": "message",
                    "role": "user",
                    "content": content,
                }
            ],
        )
        prev_id = r.id
        assistant_parts.append((r.output_text or "").strip())

    after = "\n\n---\n\n".join(assistant_parts)
    return PromptTwoStepResult(first_text=text1, after_charts=after)
