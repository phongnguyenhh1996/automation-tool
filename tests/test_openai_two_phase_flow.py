from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from automation_tool.openai_prompt_flow import run_full_analysis_two_phase_flow


def _touch(path: Path, text: str = "{}") -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_two_phase_flow_chains_previous_response_id(tmp_path: Path) -> None:
    tv_json = _touch(tmp_path / "20260629_165354_tradingview_DXY_4h.json")
    gc_csv = _touch(tmp_path / "20260629_165354_gocharting_DXY_15m.csv", "time\n")
    gc_png = tmp_path / "20260629_165354_gocharting_DXY_15m.png"
    gc_png.write_bytes(b"\x89PNG")
    fp_dir = tmp_path / "footprint_images"
    fp_dir.mkdir()
    fp_json = _touch(fp_dir / "footprint_combined_15m.json")

    payloads = [
        ("json", tv_json),
        ("csv", gc_csv),
        ("image", gc_png),
        ("json", fp_json),
    ]

    client = MagicMock()
    client.responses.create.side_effect = [
        MagicMock(output_text='{"step":1,"context":{}}', id="resp-phase1"),
        MagicMock(output_text='{"prices":[]}', id="resp-phase2"),
    ]

    with patch(
        "automation_tool.openai_prompt_flow.OpenAI",
        return_value=client,
    ), patch(
        "automation_tool.openai_prompt_flow.prepare_footprint_json_for_openai",
        side_effect=lambda _path, data, **_: data,
    ):
        out = run_full_analysis_two_phase_flow(
            api_key="sk-test",
            charts_dir=tmp_path,
            structure_prompt="[FULL_ANALYSIS — BƯỚC 1/2]",
            footprint_prompt="[FULL_ANALYSIS — BƯỚC 2/2]",
            max_images_per_call=100,
            vector_store_ids=[],
            store=True,
            include=[],
            chart_payloads=payloads,
            model="gpt-5.2",
        )

    assert client.responses.create.call_count == 2
    call1 = client.responses.create.call_args_list[0].kwargs
    call2 = client.responses.create.call_args_list[1].kwargs
    assert "previous_response_id" not in call1
    assert call2["previous_response_id"] == "resp-phase1"

    content1 = call1["input"][1]["content"]
    content2 = call2["input"][1]["content"]
    assert "tradingview_DXY_4h" in content1[0]["text"] or any(
        "tradingview" in (c.get("text") or "") for c in content1 if c.get("type") == "input_text"
    )
    assert any(
        "gocharting" in (c.get("text") or "").lower() or "footprint_combined" in (c.get("text") or "")
        for c in content2
        if c.get("type") == "input_text"
    )

    assert '{"step":1' in out.first_text
    assert out.after_charts == '{"prices":[]}'
    assert out.final_response_id == "resp-phase2"


def test_two_phase_flow_requires_both_phases(tmp_path: Path) -> None:
    tv_json = _touch(tmp_path / "20260629_165354_tradingview_DXY_4h.json")
    with patch("automation_tool.openai_prompt_flow.OpenAI"):
        with pytest.raises(ValueError, match="no footprint payloads"):
            run_full_analysis_two_phase_flow(
                api_key="sk-test",
                charts_dir=tmp_path,
                structure_prompt="p1",
                footprint_prompt="p2",
                max_images_per_call=100,
                vector_store_ids=[],
                store=True,
                include=[],
                chart_payloads=[("json", tv_json)],
                model="gpt-5.2",
            )
