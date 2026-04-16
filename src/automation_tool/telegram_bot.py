from __future__ import annotations

import html
import json
import logging
import random
import re
import sys
from dataclasses import dataclass
from typing import Any, Optional

import httpx

_log = logging.getLogger(__name__)

from automation_tool.openai_analysis_json import parse_analysis_from_openai_text

TELEGRAM_MAX_MESSAGE = 4096

_RE_OUTPUT_CHI_TIET = re.compile(r"\[OUTPUT_CHI_TIET\]", re.IGNORECASE)
_RE_OUTPUT_NGAN_GON = re.compile(r"\[OUTPUT_NGAN_GON\]", re.IGNORECASE)

_MT5_NGAN_GON_MESSAGES = (
    "Sát thủ săn mồi đã xuất kích – MT5 rực lửa, chờ lúa về kho! 🐯🎯💰🔥",
    "Không cảm xúc, không do dự – Tool đã vung đao, Market chỉ có khóc! ⚔️📉💸🚀",
    "Tín hiệu gọi tên, lệnh lên nòng – Tiền vào túi là chuyện sớm muộn! 💎📞📈🔥",
    "Market rung lắc kệ Market – Tool đã vào lệnh, ngồi mát ăn bát vàng thôi! 🌊🧘‍♂️💎💵",
    "Vít ga vào lệnh, chốt lãi xuyên màn đêm – Đẳng cấp công nghệ là đây! 🏎️💨💰🔥",
)

# Canonical zone keys (openai_analysis_json.ZONE_LABELS_ORDER) → hiển thị trong tin MT5.
_MT5_ZONE_LABEL_DISPLAY_VN: dict[str, str] = {
    "plan_chinh": "Plan chính",
    "plan_phu": "Plan phụ",
    "scalp": "Scalp",
}


def mt5_zone_label_display_vn(zone_label: Optional[str]) -> Optional[str]:
    """
    Chỉ tên hiển thị: ``plan_chinh`` → ``Plan chính`` (không thêm câu «Đã vào lệnh»).
    Dùng trong tin lỗi MT5 để nêu kế hoạch mà không gợi ý đã khớp lệnh.
    """
    if not zone_label or not str(zone_label).strip():
        return None
    key = str(zone_label).strip().lower()
    return _MT5_ZONE_LABEL_DISPLAY_VN.get(key)


def mt5_zone_entry_line_vn(zone_label: Optional[str]) -> Optional[str]:
    """
    Một dòng: ``Đã vào lệnh cho "Plan chính".`` (hoặc Plan phụ / Scalp) khi biết nhãn.
    Đặt **sau** câu emoji ngẫu nhiên trong tin MT5. Trả ``None`` nếu không map được.
    """
    if not zone_label or not str(zone_label).strip():
        return None
    key = str(zone_label).strip().lower()
    display = _MT5_ZONE_LABEL_DISPLAY_VN.get(key)
    if not display:
        return None
    return f'Đã vào lệnh cho "{display}".'


@dataclass(frozen=True)
class TelegramChunk:
    """One outbound Telegram message parsed from OpenAI JSON output."""

    text: str
    parse_mode: Optional[str]  # None = use caller default (e.g. TELEGRAM_PARSE_MODE)
    html_ready: bool = False  # True: skip markdown_like → HTML conversion for HTML mode

# Characters that must be escaped in MarkdownV2 outside of entities.
# https://core.telegram.org/bots/api#markdownv2-style
_MARKDOWN_V2_SPECIAL = r"_*[]()~`>#+-=|{}.!"


def line_to_telegram_html(line: str) -> str:
    """
    Balanced ``**...**`` → ``<b>...</b>`` via split (no regex — avoids broken/nested tags).

    If ``**`` is unbalanced, return the whole line HTML-escaped (no partial <b>).
    """
    parts = line.split("**")
    if len(parts) % 2 == 0:
        return html.escape(line)
    out: list[str] = []
    for i, seg in enumerate(parts):
        esc = html.escape(seg)
        if i % 2 == 1:
            if seg:
                out.append(f"<b>{esc}</b>")
        else:
            out.append(esc)
    return "".join(out)


def markdown_like_to_telegram_html(text: str) -> str:
    """
    Turn common LLM markdown into Telegram HTML (subset: b, code).

    Handles: ``##``/``###`` headings, ``**bold**``, inline `` `code` ``.
    Escapes ``<``, ``>``, ``&`` everywhere else. Multiline ``**...**`` is OK.
    """
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    codes: list[str] = []

    def code_repl(m: re.Match[str]) -> str:
        codes.append(html.escape(m.group(1)))
        return f"\x00C{len(codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", code_repl, text)

    lines = text.split("\n")
    out_lines: list[str] = []
    for line in lines:
        hm = re.match(r"^(#{1,6})\s+(.*)$", line)
        if hm:
            title = hm.group(2).strip()
            # One <b> for the whole heading; inner `**` is rare — if present, strip # and
            # use same bold splitter without wrapping (avoids nested <b>).
            if "**" in title:
                out_lines.append(line_to_telegram_html(title))
            else:
                out_lines.append("<b>" + html.escape(title) + "</b>")
            continue
        out_lines.append(line_to_telegram_html(line))

    result = "\n".join(out_lines)
    for i, escaped_inner in enumerate(codes):
        result = result.replace(f"\x00C{i}\x00", "<code>" + escaped_inner + "</code>")
    return result


def escape_markdown_v2(text: str) -> str:
    """
    Make arbitrary UTF-8 text safe for Telegram ``MarkdownV2``.

    Escapes backslashes first, then reserved characters. Result renders as plain
    text (no inline bold/italic from unescaped ``*``/``_`` in the source).
    """
    text = text.replace("\\", "\\\\")
    for c in _MARKDOWN_V2_SPECIAL:
        text = text.replace(c, "\\" + c)
    return text


def _strip_json_code_fence(text: str) -> str:
    t = text.strip()
    if not t.startswith("```"):
        return t
    first_nl = t.find("\n")
    if first_nl == -1:
        return t
    body = t[first_nl + 1 :]
    body = body.rstrip()
    if body.endswith("```"):
        body = body[:-3].rstrip()
    return body


def _msg_body(d: dict[str, Any]) -> str | None:
    for k in ("text", "body", "content"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return None


def _chunk_from_message_dict(d: dict[str, Any], *, default_parse_mode: Optional[str]) -> TelegramChunk | None:
    body = _msg_body(d)
    if body is None:
        return None
    if "parse_mode" in d:
        pm = d.get("parse_mode")
        parse_mode: Optional[str]
        if pm is None or pm == "":
            parse_mode = None
        else:
            parse_mode = str(pm)
    else:
        parse_mode = default_parse_mode
    hr = bool(d.get("html_ready", False))
    return TelegramChunk(text=body, parse_mode=parse_mode, html_ready=hr)


def _try_parse_one_json_object(s: str) -> list[TelegramChunk] | None:
    """Return chunks if ``s`` is a JSON object with the Telegram contract; else None."""
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    inner: dict[str, Any]
    if "telegram" in data:
        tg = data["telegram"]
        if not isinstance(tg, dict):
            return None
        inner = tg
    else:
        inner = data

    default_pm: Optional[str] = None
    if "parse_mode" in inner and isinstance(inner.get("parse_mode"), str):
        default_pm = inner["parse_mode"] or None

    if "messages" in inner:
        raw_list = inner["messages"]
        if not isinstance(raw_list, list):
            return None
        out: list[TelegramChunk] = []
        for item in raw_list:
            if not isinstance(item, dict):
                return None
            ch = _chunk_from_message_dict(item, default_parse_mode=default_pm)
            if ch is None:
                return None
            out.append(ch)
        return out if out else None

    ch = _chunk_from_message_dict(inner, default_parse_mode=default_pm)
    return [ch] if ch else None


def parse_openai_telegram_payload(
    raw: str,
) -> list[TelegramChunk] | None:
    """
    Parse OpenAI step-2 output when the model returns JSON meant for Telegram.

    Supported shapes (also inside `` ```json `` fences):

    - ``{"telegram": {"text": "...", "parse_mode": "HTML", "html_ready": false}}``
    - ``{"telegram": {"messages": [{"text": "..."}, ...]}}``
    - ``{"text": "...", "parse_mode": "HTML"}`` (shorthand without ``telegram``)

    Per-message keys: ``text`` / ``body`` / ``content``. Optional ``html_ready``:
    if true and ``parse_mode`` is HTML, text is sent as-is (already valid Telegram HTML).

    If step 2 was split into several API batches, the model may emit one JSON per batch
    separated by ``\\n\\n---\\n\\n`` — those are merged in order.

    Returns ``None`` if the string is not structured JSON (caller should send it as
    legacy plain / markdown-like text).
    """
    if not raw or not raw.strip():
        return None

    raw_stripped = raw.strip()

    for candidate in (raw_stripped, _strip_json_code_fence(raw_stripped)):
        got = _try_parse_one_json_object(candidate)
        if got is not None:
            return got

    segments = [s.strip() for s in raw_stripped.split("\n\n---\n\n") if s.strip()]
    if len(segments) <= 1:
        return None

    merged: list[TelegramChunk] = []
    for seg in segments:
        got = None
        for candidate in (seg, _strip_json_code_fence(seg)):
            got = _try_parse_one_json_object(candidate)
            if got is not None:
                break
        if got is None:
            return None
        merged.extend(got)
    return merged if merged else None


def split_analysis_json_chi_tiet_ngan_gon(raw: str) -> tuple[str, str] | None:
    """
    Khi model trả JSON phân tích có ``out_chi_tiet`` / ``output_ngan_gon`` (không dùng marker).
    Trả ``(chi_tiet, ngan_gon)`` nếu có ít nhất một chuỗi không rỗng; ngược lại ``None``.
    """
    p = parse_analysis_from_openai_text(raw)
    if p is None:
        return None
    ct = (p.out_chi_tiet or "").strip()
    ng = (p.output_ngan_gon or "").strip()
    if not ct and not ng:
        return None
    return (ct, ng)


def split_output_chi_tiet_ngan_gon(raw: str) -> tuple[str, str] | None:
    """
    Parse model output that contains both markers::

        [OUTPUT_CHI_TIET]
        ... detailed text ...
        [OUTPUT_NGAN_GON]
        ... short summary ...

    Returns ``(chi_tiet, ngan_gon)`` with markers stripped, or ``None`` if either
    marker is missing (caller should fall back to legacy single-message send).
    """
    if not raw or not raw.strip():
        return None
    t = raw.replace("\r\n", "\n").replace("\r", "\n")
    m1 = _RE_OUTPUT_CHI_TIET.search(t)
    if not m1:
        return None
    after_first = t[m1.end() :]
    m2 = _RE_OUTPUT_NGAN_GON.search(after_first)
    if not m2:
        return None
    chi_tiet = after_first[: m2.start()].strip()
    ngan_gon = after_first[m2.end() :].strip()
    if not chi_tiet and not ngan_gon:
        return None
    return (chi_tiet, ngan_gon)


def internal_chat_id_for_t_me_c_link(chat_id: str) -> Optional[str]:
    """
    ``t.me/c/<internal>/<msg>`` uses the numeric id without the ``-100`` prefix
    for supergroups/channels (e.g. ``-1003428716385`` → ``3428716385``).
    Positive numeric ids (some setups) are passed through when all-digit.
    """
    s = chat_id.strip()
    if s.startswith("-100"):
        inner = s[4:]
        return inner if inner.isdigit() else None
    if s.isdigit() and len(s) >= 8:
        return s
    return None


def build_t_me_c_message_url(detail_chat_id: str, message_id: int) -> Optional[str]:
    internal = internal_chat_id_for_t_me_c_link(detail_chat_id)
    if not internal:
        return None
    return f"https://t.me/c/{internal}/{message_id}"


def _should_fallback_summary_to_main(err: RuntimeError) -> bool:
    """True when the summary channel is missing, wrong id, or bot cannot post there."""
    s = str(err).lower()
    return any(
        part in s
        for part in (
            "chat not found",
            "peer_id_invalid",
            "chat_id is empty",
            "bot is not a member",
            "have no rights to send",
        )
    )


def enrich_ngan_gon_with_detail_link(
    ngan_gon: str,
    *,
    detail_chat_id: str,
    detail_message_id: int,
) -> tuple[str, Optional[str], bool]:
    """
    Append a permalink to the detailed post. Returns
    ``(text, parse_mode, html_ready)``. When a link is added, body is converted
    to Telegram HTML and ``parse_mode`` is ``\"HTML\"`` with ``html_ready=True``
    so the link is always clickable (works even if the default global mode is MarkdownV2).
    """
    url = build_t_me_c_message_url(detail_chat_id, detail_message_id)
    if not url:
        return ngan_gon, None, False
    body = markdown_like_to_telegram_html(ngan_gon)
    link = f'<a href="{html.escape(url)}">📊 Xem phân tích chi tiết</a>'
    return body + "\n\n" + link, "HTML", True


def chunk_text(text: str, max_len: int = TELEGRAM_MAX_MESSAGE) -> list[str]:
    """Split for Telegram length limit; prefer ``\\n`` then space so HTML tags are less often torn."""
    if not text:
        return [""]
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_len, len(text))
        if end < len(text):
            nl = text.rfind("\n", start, end)
            if nl != -1 and nl > start:
                end = nl + 1
            else:
                sp = text.rfind(" ", start, end)
                if sp != -1 and sp > start:
                    end = sp + 1
        chunks.append(text[start:end])
        start = end
    return chunks


def send_message(
    *,
    bot_token: str,
    chat_id: str,
    text: str,
    parse_mode: Optional[str] = None,
    html_ready: bool = False,
    reply_to_message_id: Optional[int] = None,
    message_thread_id: Optional[int] = None,
    timeout: float = 120.0,
) -> Optional[int]:
    """
    Returns the ``message_id`` of the **first** chunk (for permalinks when the
    message was split). Returns ``None`` if the API response has no message id.
    """
    # Some model outputs (especially JSON-encoded strings) may contain literal
    # backslash sequences like "\\n" instead of real newlines. Telegram would
    # show those as the raw characters "\n". Normalize them before formatting.
    if text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    base = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    if parse_mode == "HTML":
        if not html_ready:
            text = markdown_like_to_telegram_html(text)
    elif parse_mode == "MarkdownV2":
        text = escape_markdown_v2(text)
    chunks = chunk_text(text)
    first_message_id: Optional[int] = None
    with httpx.Client(timeout=timeout) as client:
        for i, part in enumerate(chunks):
            data: dict[str, Any] = {"chat_id": chat_id, "text": part}
            if parse_mode:
                data["parse_mode"] = parse_mode
            if i == 0 and reply_to_message_id is not None:
                data["reply_to_message_id"] = int(reply_to_message_id)
            if message_thread_id is not None:
                data["message_thread_id"] = int(message_thread_id)
            r = client.post(base, data=data)
            if r.status_code != 200:
                body = r.text
                raise RuntimeError(f"Telegram sendMessage failed: {r.status_code} {body}")
            j = r.json()
            if not j.get("ok"):
                raise RuntimeError(f"Telegram API error: {j}")
            if i == 0:
                res = j.get("result")
                if isinstance(res, dict):
                    mid = res.get("message_id")
                    if isinstance(mid, int):
                        first_message_id = mid
    return first_message_id


def _send_plain_text_to_chat_id(
    *,
    bot_token: str,
    chat_id: Optional[str],
    text: str,
    log_context: str,
) -> None:
    """Gửi plain text tới một ``chat_id`` bất kỳ (không parse_mode)."""
    cid = (chat_id or "").strip()
    if not cid:
        return
    body = (text or "").strip()
    if not body:
        return
    try:
        send_message(bot_token=bot_token, chat_id=cid, text=body, parse_mode=None)
    except Exception as e:
        _log.warning("Không gửi Telegram (%s): %s", log_context, e)


def send_user_friendly_notice(
    *,
    bot_token: str,
    chat_id: Optional[str],
    title: str,
    body: str = "",
) -> None:
    """
    Tin ngắn tiếng Việt tới ``TELEGRAM_PYTHON_BOT_CHAT_ID`` (plain text, đã chunk qua ``send_message``).
    Để trống ``chat_id`` → không gửi.
    """
    t = (title or "").strip()
    b = (body or "").strip()
    if not t and not b:
        return
    lines: list[str] = ["🔔 Bước quan trọng"]
    if t:
        lines.append(t)
    if b:
        lines.append("")
        lines.append(b)
    full = "\n".join(lines)
    _send_plain_text_to_chat_id(
        bot_token=bot_token,
        chat_id=chat_id,
        text=full,
        log_context="TELEGRAM_PYTHON_BOT_CHAT_ID",
    )


def _send_plain_text_to_ngan_gon_chat(
    *,
    bot_token: str,
    output_ngan_gon_chat_id: Optional[str],
    text: str,
) -> None:
    """Gửi plain text tới ``TELEGRAM_OUTPUT_NGAN_GON_CHAT_ID`` (bản ngắn phân tích)."""
    _send_plain_text_to_chat_id(
        bot_token=bot_token,
        chat_id=output_ngan_gon_chat_id,
        text=text,
        log_context="TELEGRAM_OUTPUT_NGAN_GON_CHAT_ID",
    )


def send_mt5_execution_log_to_ngan_gon_chat(
    *,
    bot_token: str,
    telegram_chat_id: Optional[str] = None,
    source: str,
    text: str,
    execution_ok: bool,
    zone_label: Optional[str] = None,
    trade_line: Optional[str] = None,
) -> None:
    """
    Gửi thông báo sau thực thi MT5 (sau ``execute_trade`` / huỷ ticket) tới ``TELEGRAM_CHAT_ID``.

    Khi ``execution_ok`` là True: một câu cố định (chọn ngẫu nhiên); (tuỳ chọn) dòng
    ``Đã vào lệnh cho "Plan chính"`` / … khi ``zone_label`` khớp; (tuỳ chọn) ``trade_line``.
    Tham số ``text`` chỉ dùng để kiểm tra có nội dung — **không** đính kèm log chi tiết
    (tránh tin quá dài khi đã vào lệnh thành công).

    Khi ``execution_ok`` là False: **không** dùng câu chúc mừng / «Đã vào lệnh»; gửi cảnh báo
    và toàn bộ ``text`` (log chi tiết từ ``format_mt5_execution_for_telegram`` hoặc ``MT5ManageResult``).

    Plain text; không dùng parse_mode để tránh lỗi ký tự đặc biệt từ broker/API.
    """
    body = (text or "").strip()
    if not body:
        return
    main = (telegram_chat_id or "").strip()
    if not main:
        return

    if not execution_ok:
        lines: list[str] = [
            "⚠️ MT5: lệnh không thành công hoặc bị từ chối — chi tiết bên dưới.",
        ]
        src = (source or "").strip()
        if src:
            lines.append(f"Nguồn: {src}")
        zd = mt5_zone_label_display_vn(zone_label)
        if zd:
            lines.append(f'Vùng / kế hoạch: "{zd}".')
        tl = (trade_line or "").strip()
        if tl:
            lines.append(tl)
        lines.append("")
        lines.append(body)
        _send_plain_text_to_chat_id(
            bot_token=bot_token,
            chat_id=main,
            text="\n".join(lines),
            log_context="TELEGRAM_CHAT_ID",
        )
        return

    out = random.choice(_MT5_NGAN_GON_MESSAGES)
    zone_line = mt5_zone_entry_line_vn(zone_label)
    if zone_line:
        out = f"{out}\n\n{zone_line}"
    tl_ok = (trade_line or "").strip()
    if tl_ok:
        out = f"{out}\n\n{tl_ok}"
    _send_plain_text_to_chat_id(
        bot_token=bot_token,
        chat_id=main,
        text=out,
        log_context="TELEGRAM_CHAT_ID",
    )


def send_first_response_log_to_log_chat(
    *,
    bot_token: str,
    telegram_log_chat_id: Optional[str],
    source: str,
    text: str,
) -> None:
    """
    Log phản hồi đầu phân tích (giá / hop_luu / chọn vùng / lý do bỏ qua MT5) tới
    ``TELEGRAM_LOG_CHAT_ID``. Plain text.
    """
    body = (text or "").strip()
    if not body:
        return
    out = f"📊 Phản hồi đầu — {source}\n\n{body}"
    _send_plain_text_to_chat_id(
        bot_token=bot_token,
        chat_id=telegram_log_chat_id,
        text=out,
        log_context="TELEGRAM_LOG_CHAT_ID",
    )


# Backward-compatible alias (older call sites / docs).
def send_first_response_log_to_analysis_detail_chat(
    *,
    bot_token: str,
    telegram_analysis_detail_chat_id: Optional[str],
    source: str,
    text: str,
) -> None:
    send_first_response_log_to_log_chat(
        bot_token=bot_token,
        telegram_log_chat_id=telegram_analysis_detail_chat_id,
        source=source,
        text=text,
    )


def _intraday_alert_telegram_prefix(*, label: str, vung_cho: str) -> str:
    lab = (label or "").strip()
    vc = (vung_cho or "").strip()
    rest = " ".join(x for x in (lab, vc) if x)
    if rest:
        return f"Phản hồi khi chạm giá {rest}: "
    return "Phản hồi khi chạm giá: "


def send_phan_tich_alert_to_main_chat_if_any(
    *,
    bot_token: str,
    chat_id: str,
    raw_openai_text: str,
    default_parse_mode: Optional[str],
    no_telegram: bool,
    alert_label: str = "",
    alert_vung_cho: str = "",
) -> None:
    """
    [INTRADAY_ALERT] / Schema E: nếu JSON có ``phan_tich_alert``, gửi nội dung đó tới
    ``TELEGRAM_CHAT_ID`` (chat chính), kèm tiền tố ``Phản hồi khi chạm giá {label} {vung_cho}: ``.
    """
    if no_telegram or not (chat_id or "").strip():
        return
    payload = parse_analysis_from_openai_text(raw_openai_text)
    if payload is None:
        return
    body = (payload.phan_tich_alert or "").strip()
    if not body:
        return
    prefix = _intraday_alert_telegram_prefix(label=alert_label, vung_cho=alert_vung_cho)
    send_message(
        bot_token=bot_token,
        chat_id=chat_id.strip(),
        text=prefix + body,
        parse_mode=default_parse_mode,
    )


def send_openai_output_to_telegram(
    *,
    bot_token: str,
    chat_id: str,
    raw: str,
    default_parse_mode: Optional[str],
    summary_chat_id: Optional[str] = None,
    detail_chat_id: Optional[str] = None,
    reply_to_message_id: Optional[int] = None,
    message_thread_id: Optional[int] = None,
) -> None:
    """
    Send OpenAI multimodal step output: either structured JSON (see
    :func:`parse_openai_telegram_payload`) or legacy free-form text using
    ``default_parse_mode`` (same behavior as before JSON contract).

    If the text contains both detailed and short parts (markers or JSON) and at
    least one of ``detail_chat_id`` or ``summary_chat_id`` is set, the detailed
    block goes to ``detail_chat_id`` if set, else ``chat_id``; the short block
    goes to ``summary_chat_id`` if set, else ``chat_id``. The short message can
    include a link to the detailed message (``t.me/c/...``).
    """
    parsed = parse_openai_telegram_payload(raw)
    if parsed is None:
        d_opt = (detail_chat_id or "").strip()
        s_opt = (summary_chat_id or "").strip()
        want_split = bool(d_opt or s_opt)
        dual: tuple[str, str] | None = None
        if want_split:
            dual = split_analysis_json_chi_tiet_ngan_gon(raw)
            if dual is None:
                dual = split_output_chi_tiet_ngan_gon(raw)
        if dual is not None:
            chi_tiet, ngan_gon = dual
            detail_target = d_opt or chat_id
            summary_target = s_opt or chat_id
            detail_msg_id: Optional[int] = None
            if chi_tiet:
                detail_msg_id = send_message(
                    bot_token=bot_token,
                    chat_id=detail_target,
                    text=chi_tiet,
                    parse_mode=default_parse_mode,
                    html_ready=False,
                    reply_to_message_id=reply_to_message_id,
                    message_thread_id=message_thread_id,
                )
            if ngan_gon:
                text_out = ngan_gon
                pm_out: Optional[str] = default_parse_mode
                hr_out = False
                if detail_msg_id is not None:
                    text_out, pm2, hr_out = enrich_ngan_gon_with_detail_link(
                        ngan_gon,
                        detail_chat_id=detail_target,
                        detail_message_id=detail_msg_id,
                    )
                    if pm2 is not None:
                        pm_out = pm2
                target = summary_target.strip()
                try:
                    send_message(
                        bot_token=bot_token,
                        chat_id=target,
                        text=text_out,
                        parse_mode=pm_out,
                        html_ready=hr_out,
                        reply_to_message_id=reply_to_message_id,
                        message_thread_id=message_thread_id,
                    )
                except RuntimeError as e:
                    if not _should_fallback_summary_to_main(e):
                        raise
                    print(
                        f"Warning: could not send OUTPUT_NGAN_GON to summary chat "
                        f"({target!r}): {e}\n"
                        "Fix: add the bot to that channel/group and use the correct id (often -100... for "
                        "channels). Sending the short summary to TELEGRAM_CHAT_ID instead.",
                        file=sys.stderr,
                    )
                    send_message(
                        bot_token=bot_token,
                        chat_id=chat_id,
                        text=text_out,
                        parse_mode=pm_out,
                        html_ready=hr_out,
                        reply_to_message_id=reply_to_message_id,
                        message_thread_id=message_thread_id,
                    )
            return
        send_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text=raw,
            parse_mode=default_parse_mode,
            html_ready=False,
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
        )
        return
    for chunk in parsed:
        pm = chunk.parse_mode if chunk.parse_mode is not None else default_parse_mode
        send_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text=chunk.text,
            parse_mode=pm,
            html_ready=chunk.html_ready,
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
        )

