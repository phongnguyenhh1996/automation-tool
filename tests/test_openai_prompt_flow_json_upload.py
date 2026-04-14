"""Tests for chart JSON → Cloudinary raw + OpenAI Responses ``input_file`` ``file_url``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from automation_tool.openai_prompt_flow import (
    _build_mixed_chart_user_content,
    _json_paths_to_headers_and_urls,
)
from automation_tool.cloudinary_json import purge_json_attachment_folder


def test_build_mixed_content_json_uses_input_file_url(tmp_path: Path) -> None:
    j = tmp_path / "stamp_tradingview_XAUUSD_1h.json"
    j.write_text('{"ohlc":[]}', encoding="utf-8")
    url = "https://res.cloudinary.com/demo/raw/upload/v1/f/x.json"

    with patch(
        "automation_tool.openai_prompt_flow.upload_json_bytes_for_responses",
        return_value=url,
    ) as up:
        parts = _build_mixed_chart_user_content(
            "prompt text",
            [("json", j)],
            max_json_chars=100_000,
        )
    up.assert_called_once()
    assert parts[0] == {"type": "input_text", "text": "prompt text"}
    assert parts[1]["type"] == "input_text"
    assert "TradingView" in parts[1]["text"]
    assert j.name in parts[1]["text"]
    assert parts[2] == {"type": "input_file", "file_url": url}


def test_two_json_files_upload_order_and_count(tmp_path: Path) -> None:
    j1 = tmp_path / "a_tradingview_a.json"
    j2 = tmp_path / "b_coinmap_x.json"
    j1.write_text('{"a":1}', encoding="utf-8")
    j2.write_text('{"b":2}', encoding="utf-8")

    with patch(
        "automation_tool.openai_prompt_flow.upload_json_bytes_for_responses",
        side_effect=[
            "https://example.com/1.json",
            "https://example.com/2.json",
        ],
    ) as up:
        parts = _build_mixed_chart_user_content(
            "p",
            [("json", j1), ("json", j2)],
            max_json_chars=100_000,
        )
    assert up.call_count == 2
    assert parts[0] == {"type": "input_text", "text": "p"}
    assert parts[1]["type"] == "input_text" and "TradingView" in parts[1]["text"]
    assert parts[2] == {"type": "input_file", "file_url": "https://example.com/1.json"}
    assert parts[3]["type"] == "input_text" and "Coinmap" in parts[3]["text"]
    assert parts[4] == {"type": "input_file", "file_url": "https://example.com/2.json"}


def test_json_paths_to_headers_single_path_no_nested_executor(tmp_path: Path) -> None:
    j = tmp_path / "c_coinmap_footprint.json"
    j.write_text("{}", encoding="utf-8")

    with patch(
        "automation_tool.openai_prompt_flow.upload_json_bytes_for_responses",
        return_value="https://example.com/solo.json",
    ) as up:
        out = _json_paths_to_headers_and_urls([j], max_json_chars=10_000)
    assert len(out) == 1
    assert out[0][1] == "https://example.com/solo.json"
    up.assert_called_once()


def test_purge_json_folder_calls_cloudinary_api() -> None:
    with patch(
        "automation_tool.cloudinary_json.ensure_cloudinary_config",
        lambda: None,
    ), patch(
        "automation_tool.cloudinary_json.cloudinary.api.delete_resources_by_prefix"
    ) as d:
        d.return_value = {"deleted": {"a": "deleted", "b": "deleted"}, "partial": {}}
        n = purge_json_attachment_folder()
    assert n == 2
    d.assert_called_once()
    call_kw = d.call_args
    assert call_kw[0][0]  # prefix non-empty
    assert call_kw[1].get("resource_type") == "raw"
