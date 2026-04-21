from __future__ import annotations

import json
import logging
from typing import Any, Optional

_log = logging.getLogger(__name__)

# Tin user-friendly khi OpenAI trả insufficient_quota (billing).
_OPENAI_BILLING_TELEGRAM_TITLE = "Hết quota OpenAI"
_OPENAI_BILLING_TELEGRAM_BODY = (
    "Tình hình là em OpenAI vừa gửi tối hậu thư: 'Hết thóc rồi, không gáy được nữa'. "
    "Sếp Cường xem nạp thêm ít năng lượng cho em nó tiếp tục cống hiến nhé."
)


def _nested_code(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if isinstance(err, dict):
        return err.get("code") if isinstance(err.get("code"), str) else None
    return None


def _openai_api_error_details(exc: BaseException) -> str:
    """Human-readable lines from OpenAI SDK errors (status, JSON body, str)."""
    lines: list[str] = []
    sc = getattr(exc, "status_code", None)
    if isinstance(sc, int):
        lines.append(f"HTTP status: {sc}")
    body = getattr(exc, "body", None)
    if body is not None:
        if isinstance(body, dict):
            try:
                lines.append("Response body: " + json.dumps(body, ensure_ascii=False))
            except (TypeError, ValueError):
                lines.append(f"Response body: {body!r}")
        else:
            lines.append(f"Response body: {body!r}")
    raw = str(exc).strip()
    if raw:
        lines.append(f"SDK / API message: {raw}")
    return "\n".join(lines)


def format_openai_exception(exc: BaseException) -> str | None:
    """
    If ``exc`` is a known OpenAI API error, return a short user-facing message.
    Otherwise return None (caller should re-raise or print the original traceback).
    """
    try:
        from openai import APIError, AuthenticationError, RateLimitError
    except ImportError:
        return None

    if isinstance(exc, AuthenticationError):
        base = "OpenAI authentication failed: check OPENAI_API_KEY in .env."
        extra = _openai_api_error_details(exc)
        return f"{base}\n{extra}" if extra else base

    if isinstance(exc, RateLimitError):
        code = _nested_code(getattr(exc, "body", None))
        raw = str(exc)
        if code == "insufficient_quota" or "insufficient_quota" in raw:
            return (
                "OpenAI returned 429 insufficient_quota: no credits left or billing is not set up.\n"
                "Add a payment method or buy credits: https://platform.openai.com/account/billing"
            )
        return (
            "OpenAI returned 429 (rate limit or quota). Wait and retry, or check usage limits.\n"
            f"Details: {raw}"
        )

    if isinstance(exc, APIError):
        return f"OpenAI API error ({getattr(exc, 'status_code', '?')}): {exc}"

    return None


def is_openai_insufficient_quota(exc: BaseException) -> bool:
    """True khi API trả 429 ``insufficient_quota`` (hết credit / chưa billing)."""
    try:
        from openai import RateLimitError
    except ImportError:
        return False
    if not isinstance(exc, RateLimitError):
        return False
    code = _nested_code(getattr(exc, "body", None))
    raw = str(exc)
    return code == "insufficient_quota" or "insufficient_quota" in raw


def _notify_openai_insufficient_quota_telegram(settings: Any) -> None:
    """Gửi tin tới ``TELEGRAM_PYTHON_BOT_CHAT_ID`` nếu có cấu hình."""
    try:
        from automation_tool.telegram_bot import send_user_friendly_notice
    except ImportError:
        return
    token = getattr(settings, "telegram_bot_token", None) or ""
    cid = getattr(settings, "telegram_python_bot_chat_id", None)
    if not (str(token).strip() and (cid or "").strip()):
        return
    try:
        send_user_friendly_notice(
            bot_token=str(token).strip(),
            chat_id=cid if isinstance(cid, str) else str(cid),
            title=_OPENAI_BILLING_TELEGRAM_TITLE,
            body=_OPENAI_BILLING_TELEGRAM_BODY,
        )
    except Exception as e:
        _log.warning("Không gửi Telegram (billing OpenAI): %s", e)


def re_raise_unless_openai(
    exc: BaseException,
    *,
    exit_on_openai: bool = True,
    settings: Optional[Any] = None,
) -> None:
    """
    For OpenAI API errors: exit with a friendly message (default), or log and return when
    ``exit_on_openai`` is False (long-running daemons that should keep running).
    For other errors: re-raise.

    Nếu truyền ``settings`` và lỗi là ``insufficient_quota``: gửi tin user-friendly tới
    ``TELEGRAM_PYTHON_BOT_CHAT_ID`` (một lần mỗi lần gọi hàm này khi gặp lỗi đó).
    """
    msg = format_openai_exception(exc)
    if msg is not None:
        if settings is not None and is_openai_insufficient_quota(exc):
            _notify_openai_insufficient_quota_telegram(settings)
        if exit_on_openai:
            raise SystemExit(msg) from None
        _log.warning("OpenAI API error (process continues): %s", msg)
        return
    raise exc
