from __future__ import annotations

from automation_tool.prompts import (
    default_last_filter_path,
    default_system_prompt_path,
    load_last_filter,
    load_system_prompt,
    responses_input_messages,
)


def test_load_system_prompt_from_repo_root() -> None:
    text = load_system_prompt()
    assert "<system_role>" in text
    assert "[FULL_ANALYSIS]" in text


def test_responses_input_puts_system_first() -> None:
    msgs = responses_input_messages(user_content="[FULL_ANALYSIS]\nhi")
    assert msgs[0]["role"] == "system"
    assert msgs[1] == {"role": "user", "content": "[FULL_ANALYSIS]\nhi"}


def test_responses_input_multimodal_user() -> None:
    parts = [{"type": "input_text", "text": "p"}]
    msgs = responses_input_messages(user_content=parts, system_prompt="SYS")
    assert msgs[0]["content"] == "SYS"
    assert msgs[1]["type"] == "message"
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == parts


def test_default_system_prompt_path_is_repo_file() -> None:
    p = default_system_prompt_path()
    assert p.name == "system-prompt.md"
    assert p.is_file()


def test_load_last_filter_from_repo_root() -> None:
    text = load_last_filter()
    assert "# last_filter" in text
    assert "fresh" in text.lower()
    assert "mitigated" in text.lower()


def test_default_last_filter_path_is_repo_file() -> None:
    p = default_last_filter_path()
    assert p.name == "last_filter.md"
    assert p.is_file()
