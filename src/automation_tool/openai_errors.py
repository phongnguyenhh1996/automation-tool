from __future__ import annotations

import json
from typing import Any


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


def re_raise_unless_openai(exc: BaseException) -> None:
    """Print a friendly message and exit(1) for OpenAI API errors; re-raise others."""
    msg = format_openai_exception(exc)
    if msg is not None:
        raise SystemExit(msg) from None
    raise exc
