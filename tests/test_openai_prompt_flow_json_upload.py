"""Tests for OpenAI chart JSON → Files API + input_file."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from openai import OpenAIError

from automation_tool.openai_prompt_flow import (
    _FILE_EXPIRES_AFTER_SECONDS,
    _build_mixed_chart_user_content,
    _json_paths_to_headers_and_file_ids,
    delete_all_openai_user_data_files,
)


def test_build_mixed_content_json_uses_input_file(tmp_path: Path) -> None:
    j = tmp_path / "stamp_tradingview_XAUUSD_1h.json"
    j.write_text('{"ohlc":[]}', encoding="utf-8")
    client = MagicMock()
    client.files.create.return_value = MagicMock(id="file-abc")

    parts = _build_mixed_chart_user_content(
        "prompt text",
        [("json", j)],
        client=client,
        max_json_chars=100_000,
    )
    assert parts[0] == {"type": "input_text", "text": "prompt text"}
    assert parts[1]["type"] == "input_text"
    assert "TradingView" in parts[1]["text"]
    assert j.name in parts[1]["text"]
    assert parts[2] == {"type": "input_file", "file_id": "file-abc"}
    client.files.create.assert_called_once()
    kw = client.files.create.call_args.kwargs
    assert kw["purpose"] == "user_data"
    assert kw["expires_after"] == {
        "anchor": "created_at",
        "seconds": _FILE_EXPIRES_AFTER_SECONDS,
    }


def test_json_upload_retries_once_on_openai_error(tmp_path: Path) -> None:
    j = tmp_path / "retry_tradingview_x.json"
    j.write_text('{"ohlc":[]}', encoding="utf-8")
    client = MagicMock()
    client.files.create.side_effect = [
        OpenAIError("transient"),
        MagicMock(id="file-after-retry"),
    ]

    parts = _build_mixed_chart_user_content(
        "prompt text",
        [("json", j)],
        client=client,
        max_json_chars=100_000,
    )
    assert parts[2] == {"type": "input_file", "file_id": "file-after-retry"}
    assert client.files.create.call_count == 2


def test_json_upload_raises_after_second_openai_error(tmp_path: Path) -> None:
    j = tmp_path / "fail_twice.json"
    j.write_text("{}", encoding="utf-8")
    client = MagicMock()
    client.files.create.side_effect = [OpenAIError("a"), OpenAIError("b")]

    with pytest.raises(OpenAIError):
        _build_mixed_chart_user_content(
            "p",
            [("json", j)],
            client=client,
            max_json_chars=100_000,
        )
    assert client.files.create.call_count == 2


def test_two_json_files_upload_order_and_count(tmp_path: Path) -> None:
    j1 = tmp_path / "a_tradingview_a.json"
    j2 = tmp_path / "b_coinmap_x.json"
    j1.write_text('{"a":1}', encoding="utf-8")
    j2.write_text('{"b":2}', encoding="utf-8")
    client = MagicMock()
    client.files.create.side_effect = [
        MagicMock(id="id-1"),
        MagicMock(id="id-2"),
    ]

    parts = _build_mixed_chart_user_content(
        "p",
        [("json", j1), ("json", j2)],
        client=client,
        max_json_chars=100_000,
    )
    assert parts[0] == {"type": "input_text", "text": "p"}
    assert parts[1]["type"] == "input_text" and "TradingView" in parts[1]["text"]
    assert parts[2] == {"type": "input_file", "file_id": "id-1"}
    assert parts[3]["type"] == "input_text" and "Coinmap" in parts[3]["text"]
    assert parts[4] == {"type": "input_file", "file_id": "id-2"}
    assert client.files.create.call_count == 2


def test_json_paths_to_headers_single_path_no_nested_executor(tmp_path: Path) -> None:
    j = tmp_path / "c_coinmap_footprint.json"
    j.write_text("{}", encoding="utf-8")
    client = MagicMock()
    client.files.create.return_value = MagicMock(id="solo")

    out = _json_paths_to_headers_and_file_ids(client, [j], max_json_chars=10_000)
    assert len(out) == 1
    assert out[0][1] == "solo"
    client.files.create.assert_called_once()


def test_delete_all_user_data_lists_purpose_and_deletes_pages() -> None:
    client = MagicMock()
    p1 = MagicMock()
    p1.data = [MagicMock(id="f1"), MagicMock(id="f2")]
    p1.has_next_page.return_value = True
    p2 = MagicMock()
    p2.data = [MagicMock(id="f3")]
    p2.has_next_page.return_value = False
    p1.get_next_page.return_value = p2
    client.files.list.return_value = p1

    n = delete_all_openai_user_data_files(client)
    assert n == 3
    client.files.list.assert_called_once_with(
        purpose="user_data",
        limit=10_000,
        order="desc",
    )
    assert client.files.delete.call_count == 3
    client.files.delete.assert_any_call("f1")
    client.files.delete.assert_any_call("f2")
    client.files.delete.assert_any_call("f3")
