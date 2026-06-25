from __future__ import annotations

"""
Inline system prompts for OpenAI Responses API (replaces dashboard ``prompt`` objects).

Source of truth: ``system-prompt.md`` at the repo root. Override path with
``OPENAI_SYSTEM_PROMPT_PATH`` when needed.
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from automation_tool.config import _ROOT


def default_system_prompt_path() -> Path:
    raw = (os.getenv("OPENAI_SYSTEM_PROMPT_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _ROOT / "system-prompt.md"


@lru_cache(maxsize=4)
def _read_system_prompt_cached(path_str: str) -> str:
    path = Path(path_str)
    if not path.is_file():
        raise FileNotFoundError(
            f"System prompt not found: {path}. "
            "Expected system-prompt.md at the repo root or set OPENAI_SYSTEM_PROMPT_PATH."
        )
    return path.read_text(encoding="utf-8").strip()


def load_system_prompt(*, path: Path | None = None) -> str:
    """Load the trading advisor system prompt (cached by resolved path)."""
    p = path if path is not None else default_system_prompt_path()
    return _read_system_prompt_cached(str(p.resolve()))


def clear_system_prompt_cache() -> None:
    _read_system_prompt_cached.cache_clear()


def responses_input_messages(
    *,
    user_content: str | list[dict[str, Any]],
    system_prompt: str | None = None,
) -> list[dict[str, Any]]:
    """
    Build ``input`` for ``responses.create``: static system prompt first, then user turn.

    Multimodal user turns use ``type: message`` with a ``content`` parts list; plain text
    uses ``role`` / ``content`` only (Responses API accepts both).
    """
    sys_text = (system_prompt if system_prompt is not None else load_system_prompt()).strip()
    out: list[dict[str, Any]] = [{"role": "system", "content": sys_text}]
    if isinstance(user_content, str):
        text = user_content.strip()
        if text:
            out.append({"role": "user", "content": text})
    else:
        out.append({"type": "message", "role": "user", "content": user_content})
    return out
