"""Tests for chart JSON → OpenAI Responses (base64 file_data vs inline text)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from automation_tool.state_files import MORNING_FULL_ANALYSIS_FILENAME

from automation_tool.openai_prompt_flow import (
    _build_mixed_chart_user_content,
    _prepare_json_headers_bodies,
    run_analysis_responses_flow,
)
from automation_tool.cloudinary_json import purge_json_attachment_folder


def _decode_file_data_url(data_url: str) -> str:
    assert data_url.startswith("data:application/json;base64,")
    b64 = data_url.split(",", 1)[1]
    return base64.b64decode(b64).decode()


def test_build_mixed_tradingview_json_inline_text(tmp_path: Path) -> None:
    j = tmp_path / "stamp_tradingview_XAUUSD_1h.json"
    j.write_text('{"ohlc":[]}', encoding="utf-8")

    parts = _build_mixed_chart_user_content(
        "prompt text",
        [("json", j)],
        max_json_chars=100_000,
    )
    assert parts[0] == {"type": "input_text", "text": "prompt text"}
    assert parts[1]["type"] == "input_text"
    assert "TradingView" in parts[1]["text"]
    assert j.name in parts[1]["text"]
    assert parts[2] == {"type": "input_text", "text": '{"ohlc":[]}'}


def test_three_json_morning_m15_m5_order(tmp_path: Path) -> None:
    morning = tmp_path / MORNING_FULL_ANALYSIS_FILENAME
    j15 = tmp_path / "stamp_coinmap_XAUUSD_15m.json"
    j5 = tmp_path / "stamp_coinmap_XAUUSD_5m.json"
    morning.write_text('{"prices":[]}', encoding="utf-8")
    j15.write_text('{"m15":1}', encoding="utf-8")
    j5.write_text('{"m5":1}', encoding="utf-8")

    parts = _build_mixed_chart_user_content(
        "p",
        [("json", morning), ("json", j15), ("json", j5)],
        max_json_chars=100_000,
    )
    assert parts[0] == {"type": "input_text", "text": "p"}
    assert parts[1]["type"] == "input_text" and "FULL_ANALYSIS snapshot" in parts[1]["text"]
    assert parts[1]["text"] and morning.name in parts[1]["text"]
    assert parts[2] == {"type": "input_text", "text": '{"prices":[]}'}
    assert "Coinmap" in parts[3]["text"] and j15.name in parts[3]["text"]
    assert parts[4]["type"] == "input_file"
    assert parts[4]["filename"] == j15.name
    assert json.loads(_decode_file_data_url(parts[4]["file_data"])) == {"m15": 1}
    assert "Coinmap" in parts[5]["text"]
    assert parts[6]["type"] == "input_file"
    assert json.loads(_decode_file_data_url(parts[6]["file_data"])) == {"m5": 1}


def test_two_json_tv_inline_coinmap_raw_base64(tmp_path: Path) -> None:
    j1 = tmp_path / "a_tradingview_a.json"
    j2 = tmp_path / "b_coinmap_x.json"
    j1.write_text('{"a":1}', encoding="utf-8")
    j2.write_text('{"b":2}', encoding="utf-8")

    parts = _build_mixed_chart_user_content(
        "p",
        [("json", j1), ("json", j2)],
        max_json_chars=100_000,
    )
    assert parts[0] == {"type": "input_text", "text": "p"}
    assert parts[1]["type"] == "input_text" and "TradingView" in parts[1]["text"]
    assert parts[2] == {"type": "input_text", "text": '{"a":1}'}
    assert parts[3]["type"] == "input_text" and "Coinmap" in parts[3]["text"]
    assert parts[4]["type"] == "input_file"
    assert json.loads(_decode_file_data_url(parts[4]["file_data"])) == {"b": 2}


def test_prepare_json_headers_single_path(tmp_path: Path) -> None:
    j = tmp_path / "c_coinmap_footprint.json"
    j.write_text("{}", encoding="utf-8")

    out = _prepare_json_headers_bodies([j], max_json_chars=10_000)
    assert len(out) == 1
    h, body = out[0]
    assert "Coinmap" in h
    assert body == "{}"


def test_run_analysis_merged_json_inline_no_cloudinary_purge(tmp_path: Path) -> None:
    j = tmp_path / "stamp_coinmap_XAUUSD_merged.json"
    j.write_text('{"frames":{}}', encoding="utf-8")
    client = MagicMock()
    client.responses.create.return_value = MagicMock(output_text="ok", id="resp-1")

    with patch(
        "automation_tool.openai_prompt_flow.OpenAI",
        return_value=client,
    ), patch(
        "automation_tool.cloudinary_json.purge_json_attachment_folder",
    ) as purge:
        run_analysis_responses_flow(
            api_key="sk-test",
            prompt_id="prompt",
            prompt_version=None,
            charts_dir=tmp_path,
            analysis_prompt="p",
            max_images_per_call=10,
            vector_store_ids=[],
            store=True,
            include=[],
            chart_payloads=[("json", j)],
            purge_json_attachment_storage=True,
        )

    purge.assert_not_called()
    content = client.responses.create.call_args.kwargs["input"][0]["content"]
    assert content[2]["type"] == "input_text"
    assert '"frames"' in content[2]["text"] or "{}" in content[2]["text"]


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
