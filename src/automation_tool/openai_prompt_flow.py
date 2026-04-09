from __future__ import annotations

import json
import os
from collections.abc import Callable
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
    Prompt text mặc định cho bước phân tích multimodal; ``main_symbol`` là cặp chính (TradingView/Coinmap).
    Nếu None hoặc không hợp lệ → ``DEFAULT_MAIN_CHART_SYMBOL``.

    Chỉ khi cặp là **XAUUSD** mới yêu cầu JSON có ``output_ngan_gon``; cặp khác: không thêm key đó.
    """
    sym = DEFAULT_MAIN_CHART_SYMBOL
    if main_symbol and str(main_symbol).strip():
        try:
            sym = normalize_main_chart_symbol(str(main_symbol).strip())
        except ValueError:
            pass
    xauusd = sym == "XAUUSD"
    json_schema_header = (
        "Trả về một JSON object hợp lệ duy nhất (có thể bọc trong ```json). Schema gợi ý:\n"
        '- "out_chi_tiet": string (phân tích chi tiết)\n'
    )
    if xauusd:
        json_schema_header += '- "output_ngan_gon": string (tóm tắt)\n'
    else:
        json_schema_header += (
            "- **Không** thêm key `output_ngan_gon`; "
            f"dùng đủ thông tin trong `out_chi_tiet` cho {sym}.\n"
        )
    tail = (
        "Trong output_ngan_gon: sau mỗi khối PLAN CHÍNH VÙNG CHỜ / PLAN PHỤ VÙNG CHỜ / SCALP VÙNG thêm một dòng lệnh tham khảo (pipe) để vào tay. "
        "Lot tham khảo: USDJPY = giá/(10×SL pip); XAUUSD = 1/SL_giá (SL_giá = |entry−SL| theo giá).\n"
    )
    if not xauusd:
        tail = tail.replace("output_ngan_gon", "out_chi_tiet")
    return (
        f"Chạy quy trình phân tích {sym}. "
        "Dữ liệu đính kèm theo thứ tự cố định (TradingView = ảnh; Coinmap = JSON export API nếu có, không thì ảnh): "
        "TradingView DXY (4h, 1h, 15m) → Coinmap USDINDEX 15m → "
        f"TradingView {sym} (4h, 1h, 15m, 5m) → Coinmap {sym} (15m, 5m).\n\n"
        + json_schema_header
        + '- "prices": đúng 3 phần tử, mỗi phần tử:\n'
        '  {"label":"plan_chinh"|"plan_phu"|"scalp","value":number,'
        '"range_low":number,"range_high":number,'
        '"hop_luu":integer 0–100,"trade_line":string} — '
        "Trong đó `range_low`/`range_high` là biên dưới/biên trên của vùng chờ. Ví dụ vùng 4709.0–4705.0 "
        "thì range_low=4705.0, range_high=4709.0. "
        "``hop_luu`` = điểm hợp lưu của vùng đó. "
        "``trade_line`` = **một dòng pipe MT5 bắt buộc, chuỗi không rỗng** cho đúng vùng "
        "(BUY/SELL LIMIT|STOP|MARKET … | SL … | TP1 … | Lot …); **không** được bỏ key, **không** được `""`. "
        "Mỗi vùng luôn có ít nhất một dòng lệnh tham khảo pipe đầy đủ.\n"
        '- "intraday_hanh_dong": "chờ" | "loại" | "VÀO LỆNH" hoặc null (tuỳ chọn; tool auto-MT5 sáng dùng ``hop_luu`` + ``trade_line`` trong ``prices``)\n'
        '- "trade_line" (gốc JSON): có thể `""` chỉ khi cả 3 phần tử trong ``prices`` đã có ``trade_line`` không rỗng như trên.\n'
        '- "no_change": boolean — chỉ dùng rõ trong luồng update intraday; phân tích sáng có thể bỏ qua hoặc false\n\n'
        + "YÊU CẦU ĐỊNH DẠNG BẮT BUỘC:\n"
        "- Nếu output có `out_chi_tiet` và/hoặc `output_ngan_gon` thì **phải trả đúng format y như mẫu trong file** "
        "`output.md` (đúng header, emoji, thứ tự mục, và cách viết trade_line).\n\n"
        + tail
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
    parts: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    image_paths = [p for k, p in payloads if k == "image" and isinstance(p, Path)]
    data_urls = _image_paths_to_data_urls(image_paths)
    for kind, p in payloads:
        if kind == "json":
            assert isinstance(p, Path)
            parts.append(
                {
                    "type": "input_text",
                    "text": _json_file_to_input_text(p, max_chars=max_json_chars),
                }
            )
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
    "Dựa trên dữ liệu footprint XAUUSD M5 mới, phân tích và đưa ra nhận định.\n"
    "Trả về một JSON object hợp lệ duy nhất (có thể bọc trong ```json). "
    "- **Không** thêm key `out_chi_tiet` hay `output_ngan_gon` — không cần phân tích dài/tóm tắt văn bản ở bước cập nhật này.\n"
    '- "no_change": true nếu ba vùng giá không đổi so với baseline; false nếu có thay đổi.\n'
    '- "prices": đúng 3 phần tử, mỗi phần tử:\n'
    '  {{"label":"plan_chinh"|"plan_phu"|"scalp","value":number,'
    '"range_low":number,"range_high":number,'
    '"hop_luu":integer 0–100,"trade_line":string}} — '
    "Trong đó `range_low`/`range_high` là biên dưới/biên trên của vùng chờ. Ví dụ vùng 4709.0–4705.0 "
    "thì range_low=4705.0, range_high=4709.0. "
    "hop_luu = điểm hợp lưu. **BẮT BUỘC:** mỗi phần tử phải có `trade_line` là chuỗi **không rỗng** "
    "(một dòng pipe MT5 đầy đủ: BUY/SELL LIMIT|STOP|MARKET … | SL … | TP1 … | Lot …); không `""`, không bỏ key. "
    "Kể cả no_change hoặc chờ — vẫn điền dòng pipe tham khảo cho từng vùng.\n"
    '- "intraday_hanh_dong": "chờ" | "loại" | "VÀO LỆNH" hoặc null (tuỳ chọn; auto-MT5 intraday dùng hop_luu + trade_line trong prices).\n'
    '- "trade_line" (gốc): có thể `""` nếu đã điền đủ trade_line không rỗng trong cả 3 phần tử prices.'
)

# TradingView tab Nhật ký: giá chạm → Coinmap M5 + OpenAI (intraday).
JOURNAL_INTRADAY_FIRST_USER_TEMPLATE = (
    "Cảnh báo TradingView đã kích hoạt tại mức giá {touched_price}, hãy phân tích đưa ra nhận định"
    "Đính kèm dữ liệu Coinmap XAUUSD khung M5 mới nhất.\n"
    "Trả về một JSON object duy nhất:\n"
    "- **Không** thêm key `out_chi_tiet` hay `output_ngan_gon`"
    "intraday_hanh_dong + prices (và trade_line gốc tùy chọn).\n"
    '- "intraday_hanh_dong": "chờ" | "loại" | "VÀO LỆNH"\n'
    '- "prices": đúng 3 phần tử, mỗi phần tử:\n'
    '  {{"label":"plan_chinh"|"plan_phu"|"scalp","value":number,'
    '"range_low":number,"range_high":number,'
    '"hop_luu":integer 0–100,"trade_line":string}} — '
    "Trong đó `range_low`/`range_high` là biên dưới/biên trên của vùng chờ (vd 4709.0–4705.0 "
    "thì range_low=4705.0, range_high=4709.0). "
    "**BẮT BUỘC:** mỗi phần tử có `trade_line` không rỗng (một dòng pipe MT5 đầy đủ cho đúng vùng); "
    "không `""`, không bỏ key. hop_luu = điểm hợp lưu. \n"
    '- "trade_line" (gốc): có thể `""` nếu cả 3 phần tử prices đã có trade_line không rỗng; '
    'hoặc một dòng pipe tóm tắt nếu cần.\n'
    "Không dùng giá trị khác cho intraday_hanh_dong trong luồng này."
)

JOURNAL_INTRADAY_RETRY_USER_TEMPLATE = (
    "Tiếp tục **đánh giá** sau {wait_minutes} phút: vẫn theo dõi mức đã chạm {touched_price}.\n"
    "Đính kèm Coinmap XAUUSD M5 mới.\n"
    "Cùng schema JSON như tin đầu: intraday_hanh_dong; "
    "prices[3] — mỗi phần tử bắt buộc trade_line không rỗng (pipe MT5 đầy đủ); "
    "mỗi phần tử có thêm range_low/range_high (biên dưới/biên trên của vùng chờ); "
    "trade_line gốc tùy chọn nếu đã đủ trong prices[]. "
    "**Không** thêm `out_chi_tiet` hay `output_ngan_gon`."
)

# Sau khi giá last realtime chạm TP1 (vùng đang ``cho_tp1``).
TP1_POST_TOUCH_USER_TEMPLATE = (
    "Giá đã chạm mức **TP1** của lệnh đang theo dõi.\n, lệnh trade_line còn hợp lệ không"
    "Vùng (label): {plan_label}\n"
    "Dòng lệnh hiện tại (trade_line): {trade_line}\n"
    "Giá khi đánh giá: {last_price}\n"
    "Mức TP1 từ trade_line: {tp1_price}\n\n"
    "Đính kèm Coinmap khung M5 mới nhất.\n"
    "Trả về **một JSON object** duy nhất (có thể bọc ```json):\n"
    '- "sau_tp1_hanh_dong": "loại" | "chinh_trade_line"\n'
    '- "trade_line_moi": string — bắt buộc nếu sau_tp1_hanh_dong là "chinh_trade_line" '
    "(một dòng pipe MT5 đầy đủ: BUY/SELL … | SL … | TP1 … | Lot …); "
    'nếu "loại" thì có thể "".\n'
    "- **Không** thêm key `out_chi_tiet` hay `output_ngan_gon` — chỉ cần quyết định sau TP1 "
    "(sau_tp1_hanh_dong + trade_line_moi khi chỉnh dòng lệnh).\n"
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
