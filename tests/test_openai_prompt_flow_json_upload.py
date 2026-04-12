"""Tests for OpenAI chart JSON → Files API + input_file."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from automation_tool.openai_prompt_flow import (
    _FILE_EXPIRES_AFTER_SECONDS,
    _build_mixed_chart_user_content,
    _json_paths_to_headers_and_file_ids,
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
