"""Tests for chart JSON → OpenAI Responses (base64 file_data vs inline text)."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from automation_tool.state_files import MORNING_FULL_ANALYSIS_FILENAME

from automation_tool.openai_prompt_flow import (
    _build_mixed_chart_user_content,
    _csv_file_header_and_body,
    _gocharting_detail_png_attachment_header,
    _gocharting_png_attachment_header,
    _json_file_header_and_body,
    _prepare_json_headers_bodies,
    default_analysis_prompt,
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
            charts_dir=tmp_path,
            analysis_prompt="p",
            max_images_per_call=10,
            vector_store_ids=[],
            store=True,
            include=[],
            chart_payloads=[("json", j)],
            model="gpt-5.2",
            purge_json_attachment_storage=True,
        )

    purge.assert_not_called()
    api_input = client.responses.create.call_args.kwargs["input"]
    assert api_input[0]["role"] == "system"
    content = api_input[1]["content"]
    assert content[2]["type"] == "input_text"
    assert '"frames"' in content[2]["text"] or "{}" in content[2]["text"]


def test_run_analysis_logs_openai_send_and_receive(caplog, tmp_path: Path) -> None:
    j = tmp_path / "stamp_coinmap_XAUUSD_merged.json"
    j.write_text('{"frames":{}}', encoding="utf-8")
    client = MagicMock()
    client.responses.create.return_value = MagicMock(output_text="ok", id="resp-1")

    caplog.set_level(logging.INFO, logger="automation_tool.openai_prompt_flow")
    with patch(
        "automation_tool.openai_prompt_flow.OpenAI",
        return_value=client,
    ):
        run_analysis_responses_flow(
            api_key="sk-test",
            charts_dir=tmp_path,
            analysis_prompt="p",
            max_images_per_call=10,
            vector_store_ids=[],
            store=True,
            include=[],
            chart_payloads=[("json", j)],
            model="gpt-5.2",
        )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "OpenAI: đã gửi data lên OpenAI" in messages
    assert "OpenAI: đã nhận data từ OpenAI" in messages
    assert "response_id=resp-1" in messages
    assert "json=1" in messages


def test_run_analysis_logs_openai_errors(caplog, tmp_path: Path) -> None:
    j = tmp_path / "stamp_coinmap_XAUUSD_merged.json"
    j.write_text('{"frames":{}}', encoding="utf-8")
    client = MagicMock()
    client.responses.create.side_effect = RuntimeError("boom")

    caplog.set_level(logging.INFO, logger="automation_tool.openai_prompt_flow")
    with patch(
        "automation_tool.openai_prompt_flow.OpenAI",
        return_value=client,
    ), pytest.raises(RuntimeError, match="boom"):
        run_analysis_responses_flow(
            api_key="sk-test",
            charts_dir=tmp_path,
            analysis_prompt="p",
            max_images_per_call=10,
            vector_store_ids=[],
            store=True,
            include=[],
            chart_payloads=[("json", j)],
            model="gpt-5.2",
        )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "OpenAI: đã gửi data lên OpenAI" in messages
    assert "OpenAI: lỗi khi gửi/nhận data từ OpenAI" in messages
    assert "json=1" in messages


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


def test_default_analysis_prompt_describes_json_and_png() -> None:
    p = default_analysis_prompt("XAUUSD")
    assert "[FULL_ANALYSIS]" in p
    assert "100 payload" in p
    assert "PNG" in p
    assert "GoCharting DXY M15" in p
    assert "footprint_combined_15m.json" in p
    assert "Coinmap" not in p
    assert "#FF6600" in p
    assert "#FA6578" in p


def test_default_analysis_prompt_gocharting_bid_ask_on_detail() -> None:
    p = default_analysis_prompt("XAUUSD", footprint_source="gocharting")
    assert "KHÔNG có BID/ASK theo từng price level" in p
    assert "footprint_combined" in p
    assert "Coinmap" not in p


def test_gocharting_csv_header_notes_bid_ask(tmp_path: Path) -> None:
    csv_p = tmp_path / "stamp_gocharting_GC_5m.csv"
    csv_p.write_text("Time,Open,High,Low,Close,Volume,Delta,CVD\n2026-01-01,1,2,3,4,5,6,7\n")
    header, _ = _csv_file_header_and_body(csv_p, max_chars=10_000)
    assert "KHÔNG có BID/ASK theo từng price level" in header
    assert "footprint_combined" in header


def test_gocharting_detail_png_headers_are_brief(tmp_path: Path) -> None:
    zoom = tmp_path / "stamp_gocharting_GC_15m_detail_zoom.png"
    back = tmp_path / "stamp_gocharting_GC_15m_detail_back_1.png"
    zoom_part = tmp_path / "stamp_gocharting_GC_15m_detail_zoom_part2.png"
    overview = tmp_path / "stamp_gocharting_GC_15m.png"
    for p in (zoom, back, zoom_part, overview):
        p.write_bytes(b"png")

    zoom_hdr = _gocharting_detail_png_attachment_header(zoom)
    back_hdr = _gocharting_detail_png_attachment_header(back)
    part_hdr = _gocharting_detail_png_attachment_header(zoom_part)
    ov_hdr = _gocharting_png_attachment_header(overview)

    assert zoom_hdr is not None and "detail footprint zoomed in" in zoom_hdr
    assert back_hdr is not None and "history pan step 1" in back_hdr
    assert part_hdr is not None and "panel 2/3" in part_hdr
    assert ov_hdr is not None and "chart screenshot" in ov_hdr
    for hdr in (zoom_hdr, back_hdr, ov_hdr):
        assert "KHÔNG có BID/ASK" not in hdr
        assert "Instrument: Gold Future" not in hdr


def test_build_mixed_gocharting_detail_png_header_not_repeated(tmp_path: Path) -> None:
    csv_p = tmp_path / "stamp_gocharting_GC_15m.csv"
    zoom = tmp_path / "stamp_gocharting_GC_15m_detail_zoom.png"
    back = tmp_path / "stamp_gocharting_GC_15m_detail_back_1.png"
    csv_p.write_text("h\n1", encoding="utf-8")
    zoom.write_bytes(b"z")
    back.write_bytes(b"b")

    parts = _build_mixed_chart_user_content(
        "p",
        [("csv", csv_p), ("image", zoom), ("image", back)],
        max_json_chars=100_000,
    )
    texts = [p["text"] for p in parts if p.get("type") == "input_text"]
    bid_ask_mentions = [t for t in texts if "KHÔNG có BID/ASK" in t]
    assert len(bid_ask_mentions) == 1
    assert any("detail footprint zoomed in" in t for t in texts)
    assert any("history pan step 1" in t for t in texts)


def test_build_mixed_coinmap_json_then_png_header(tmp_path: Path) -> None:
    j = tmp_path / "stamp_coinmap_DXY_15m.json"
    png = tmp_path / "stamp_coinmap_DXY_15m.png"
    j.write_text("{}", encoding="utf-8")
    png.write_bytes(b"png")

    parts = _build_mixed_chart_user_content(
        "p",
        [("json", j), ("image", png)],
        max_json_chars=100_000,
    )
    assert parts[0] == {"type": "input_text", "text": "p"}
    assert "Coinmap API export" in parts[1]["text"]
    assert parts[2]["type"] == "input_file"
    assert "Coinmap fullscreen chart" in parts[3]["text"]
    assert png.name in parts[3]["text"]
    assert parts[4]["type"] == "input_image"


def test_build_mixed_full_analysis_prompt_then_chart(tmp_path: Path) -> None:
    j = tmp_path / "stamp_tradingview_XAUUSD_1h.json"
    j.write_text('{"ohlc":[]}', encoding="utf-8")
    prompt = default_analysis_prompt("XAUUSD")

    parts = _build_mixed_chart_user_content(
        prompt,
        [("json", j)],
        max_json_chars=100_000,
    )
    assert parts[0]["text"] == prompt
    assert parts[1]["type"] == "input_text"
    assert "TradingView" in parts[1]["text"]


def test_footprint_json_enriched_with_price_before_openai(tmp_path: Path) -> None:
    stamp = "20260101_120000"
    charts_dir = tmp_path / "charts"
    fp_dir = charts_dir / "footprint_images"
    fp_dir.mkdir(parents=True)
    csv_path = charts_dir / f"{stamp}_gocharting_GC_5m.csv"
    csv_path.write_text(
        "Time,Open,High,Low,Close\n"
        "2026-06-16 10:05:00,4200,4220,4200,4210\n",
        encoding="utf-8",
    )
    json_path = fp_dir / "footprint_bid_ask_5m.json"
    levels = [{"bid": i, "ask": i + 1} for i in range(10)]
    json_path.write_text(
        json.dumps(
            {
                "symbol": "COMEX:GC1!",
                "timeframe": "5m",
                "candles": [{"time": "10:05", "price_levels": levels}],
            }
        ),
        encoding="utf-8",
    )

    _header, body = _json_file_header_and_body(
        json_path,
        max_chars=100_000,
        chart_stamp=stamp,
    )
    payload = json.loads(body)
    prices = [lv["price"] for lv in payload["candles"][0]["price_levels"]]
    assert prices == [
        4220.0,
        4219.6,
        4219.2,
        4218.8,
        4218.4,
        4218.0,
        4217.6,
        4217.2,
        4216.8,
        4216.4,
    ]
    assert '"price":4220' in body or '"price": 4220' in body


def test_footprint_json_trimmed_to_100_candles_before_openai(tmp_path: Path) -> None:
    charts_dir = tmp_path / "charts"
    fp_dir = charts_dir / "footprint_images"
    fp_dir.mkdir(parents=True)
    json_path = fp_dir / "footprint_bid_ask_15m.json"
    candles = [{"time": f"{h:02d}:00", "price_levels": [{"bid": 1, "ask": 2}]} for h in range(120)]
    json_path.write_text(
        json.dumps(
            {
                "symbol": "COMEX:GC1!",
                "timeframe": "15m",
                "candles": candles,
            }
        ),
        encoding="utf-8",
    )

    _header, body = _json_file_header_and_body(json_path, max_chars=100_000)
    payload = json.loads(body)
    assert len(payload["candles"]) == 100
    assert payload["candles"][0]["time"] == "20:00"
    assert payload["candles"][-1]["time"] == "119:00"
