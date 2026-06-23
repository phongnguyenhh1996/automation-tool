from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from automation_tool.gocharting_footprint_extract import (
    DEFAULT_FOOTPRINT_EXTRACT_MODEL,
    build_combined_footprint_extract_user_prompt,
    build_footprint_extract_user_prompt,
    extract_all_footprint_jsons,
    footprint_json_output_path,
    resolve_gocharting_chart_info,
    resolve_instrument_slug,
    validate_combined_footprint_extract_json,
    validate_footprint_extract_json,
    write_footprint_extract_json,
)
from automation_tool.gocharting_image_crop import GOCHARTING_IMAGE_WIDTH_THIRDS
from automation_tool.images import GOCHARTING_GOLD_EXPORT_LABEL


def _sample_payload(*, timeframe: str = "15m") -> dict:
    return {
        "chart_info": {
            "symbol": "COMEX:GC1!",
            "timeframe": timeframe,
            "type": "Bid/Ask Footprint",
        },
        "candles": [
            {
                "time": "10:00",
                "price_levels": [
                    {"bid": 2, "ask": 4, "attributes": []},
                    {"bid": 4, "ask": 16, "attributes": ["imbalance"]},
                ],
            }
        ],
    }


def _combined_sample_payload() -> dict:
    return {
        "5m": _sample_payload(timeframe="5m"),
        "15m": _sample_payload(timeframe="15m"),
    }


def _gocharting_cfg() -> dict:
    return {
        "symbols": {
            "XAUUSD": {
                "export_label": "GC",
                "search_query": "GC1!",
            },
            "DXY": {
                "export_label": "DXY",
            },
        }
    }


def _write_rgb_png(path: Path, width: int, height: int, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), color).save(path, format="PNG")


def test_default_model_is_gpt_5_4() -> None:
    assert DEFAULT_FOOTPRINT_EXTRACT_MODEL == "gpt-5.4"


def test_footprint_json_output_path() -> None:
    out = footprint_json_output_path(Path("/tmp/charts"), "5m", "GC1!")
    assert out == Path("/tmp/charts/m5_GC1!_footprint.json")
    out15 = footprint_json_output_path(Path("/tmp/charts"), "15m", "GC1!")
    assert out15 == Path("/tmp/charts/m15_GC1!_footprint.json")


def test_resolve_instrument_slug_xauusd() -> None:
    assert resolve_instrument_slug(_gocharting_cfg(), "XAUUSD") == "GC1!"


def test_interval_footprint_filename_slug() -> None:
    from automation_tool.gocharting_footprint_extract import interval_footprint_filename_slug

    assert interval_footprint_filename_slug("5m") == "m5"
    assert interval_footprint_filename_slug("15m") == "m15"


def test_resolve_gocharting_chart_info() -> None:
    info = resolve_gocharting_chart_info(_gocharting_cfg(), "XAUUSD", "15m")
    assert info == {
        "symbol": "COMEX:GC1!",
        "timeframe": "15m",
        "type": "Bid/Ask Footprint",
    }
    info5 = resolve_gocharting_chart_info(_gocharting_cfg(), "XAUUSD", "5m")
    assert info5["timeframe"] == "5m"


def test_validate_footprint_extract_json_accepts_valid() -> None:
    raw = _sample_payload()
    out = validate_footprint_extract_json(raw)
    assert out["chart_info"]["symbol"] == "COMEX:GC1!"
    assert out["candles"][0]["price_levels"][1]["attributes"] == ["imbalance"]


def test_validate_combined_footprint_extract_json() -> None:
    out = validate_combined_footprint_extract_json(_combined_sample_payload())
    assert set(out.keys()) == {"5m", "15m"}
    assert out["5m"]["chart_info"]["timeframe"] == "5m"
    assert out["15m"]["chart_info"]["timeframe"] == "15m"


def test_validate_footprint_extract_json_rejects_invalid() -> None:
    with pytest.raises(ValueError, match="chart_info"):
        validate_footprint_extract_json({"candles": []})
    with pytest.raises(ValueError, match="bid/ask"):
        validate_footprint_extract_json(
            {
                "chart_info": {
                    "symbol": "X",
                    "timeframe": "5m",
                    "type": "Bid/Ask Footprint",
                },
                "candles": [{"time": "10:00", "price_levels": [{"bid": "x", "ask": 1}]}],
            }
        )


def test_build_footprint_extract_user_prompt_includes_schema() -> None:
    prompt = build_footprint_extract_user_prompt(
        {"symbol": "COMEX:GC1!", "timeframe": "15m", "type": "Bid/Ask Footprint"}
    )
    assert "COMEX:GC1!" in prompt
    assert "price_levels" in prompt
    assert "imbalance" in prompt
    assert "horizontal panels" in prompt
    assert "Hướng dẫn đọc chart" not in prompt
    assert "#8FAF8E" not in prompt


def test_build_combined_footprint_extract_user_prompt() -> None:
    prompt = build_combined_footprint_extract_user_prompt(
        {
            "5m": {"symbol": "COMEX:GC1!", "timeframe": "5m", "type": "Bid/Ask Footprint"},
            "15m": {"symbol": "COMEX:GC1!", "timeframe": "15m", "type": "Bid/Ask Footprint"},
        }
    )
    assert '"5m"' in prompt
    assert '"15m"' in prompt
    assert "5m first, then 15m" in prompt
    assert "3 horizontal panels" in prompt


def test_write_footprint_extract_json(tmp_path: Path) -> None:
    path = tmp_path / "m5_GC1!_footprint.json"
    data = _sample_payload(timeframe="5m")
    write_footprint_extract_json(path, data)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["chart_info"]["timeframe"] == "5m"


def _write_detail_pngs(charts: Path, stamp: str, iv: str) -> None:
    sym = GOCHARTING_GOLD_EXPORT_LABEL
    for suffix in ("zoom", "back_1", "back_2"):
        p = charts / f"{stamp}_gocharting_{sym}_{iv}_detail_{suffix}.png"
        _write_rgb_png(p, 300, 100, (0, 128, 255))


def _count_input_images(create_kwargs: dict) -> int:
    messages = create_kwargs.get("input") or []
    count = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "input_image":
                count += 1
    return count


@patch("automation_tool.gocharting_footprint_extract.OpenAI")
def test_extract_all_footprint_jsons_single_request(mock_openai_cls, tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    (charts / ".main_chart_symbol").write_text("XAUUSD\n", encoding="utf-8")
    stamp = "20260623_120000"
    _write_detail_pngs(charts, stamp, "5m")
    _write_detail_pngs(charts, stamp, "15m")

    cfg_path = tmp_path / "gocharting.yaml"
    cfg_path.write_text(
        "symbols:\n  XAUUSD:\n    export_label: GC\n    search_query: GC1!\n",
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    def _fake_create(**kwargs: object) -> MagicMock:
        captured["kwargs"] = kwargs
        resp = MagicMock()
        resp.output_text = json.dumps(_combined_sample_payload())
        return resp

    mock_client = MagicMock()
    mock_client.responses.create.side_effect = _fake_create
    mock_openai_cls.return_value = mock_client

    results = extract_all_footprint_jsons(
        api_key="test-key",
        charts_dir=charts,
        output_dir=charts,
        stamp=stamp,
        main_symbol="XAUUSD",
        gocharting_yaml=cfg_path,
        model=DEFAULT_FOOTPRINT_EXTRACT_MODEL,
        store=False,
        include=[],
    )

    assert set(results.keys()) == {"5m", "15m"}
    assert results["5m"] == charts / "m5_GC1!_footprint.json"
    assert results["15m"] == charts / "m15_GC1!_footprint.json"
    assert results["5m"].is_file()
    assert results["15m"].is_file()
    assert mock_client.responses.create.call_count == 1

    source_images = 6  # 3 per interval × 2 intervals
    assert _count_input_images(captured["kwargs"]) == source_images * GOCHARTING_IMAGE_WIDTH_THIRDS

    m5 = json.loads(results["5m"].read_text(encoding="utf-8"))
    m15 = json.loads(results["15m"].read_text(encoding="utf-8"))
    assert m5["chart_info"]["timeframe"] == "5m"
    assert m15["chart_info"]["timeframe"] == "15m"


@patch("automation_tool.gocharting_footprint_extract.OpenAI")
def test_extract_all_footprint_jsons_no_crop(mock_openai_cls, tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    (charts / ".main_chart_symbol").write_text("XAUUSD\n", encoding="utf-8")
    stamp = "20260623_120000"
    _write_detail_pngs(charts, stamp, "5m")
    _write_detail_pngs(charts, stamp, "15m")

    cfg_path = tmp_path / "gocharting.yaml"
    cfg_path.write_text(
        "detail_chart:\n  crop_width_thirds: false\n"
        "symbols:\n  XAUUSD:\n    export_label: GC\n    search_query: GC1!\n",
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    def _fake_create(**kwargs: object) -> MagicMock:
        captured["kwargs"] = kwargs
        resp = MagicMock()
        resp.output_text = json.dumps(_combined_sample_payload())
        return resp

    mock_client = MagicMock()
    mock_client.responses.create.side_effect = _fake_create
    mock_openai_cls.return_value = mock_client

    extract_all_footprint_jsons(
        api_key="test-key",
        charts_dir=charts,
        output_dir=charts,
        stamp=stamp,
        main_symbol="XAUUSD",
        gocharting_yaml=cfg_path,
        model=DEFAULT_FOOTPRINT_EXTRACT_MODEL,
        store=False,
        include=[],
    )

    source_images = 6
    assert _count_input_images(captured["kwargs"]) == source_images
    prompt_text = str(captured["kwargs"])
    assert "horizontal panels" not in prompt_text


def test_extract_all_footprint_jsons_missing_png(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    cfg_path = tmp_path / "gocharting.yaml"
    cfg_path.write_text(
        "symbols:\n  XAUUSD:\n    export_label: GC\n    search_query: GC1!\n",
        encoding="utf-8",
    )
    with pytest.raises(FileNotFoundError, match="5m detail PNGs"):
        extract_all_footprint_jsons(
            api_key="test-key",
            charts_dir=charts,
            output_dir=charts,
            stamp="20260623_120000",
            main_symbol="XAUUSD",
            gocharting_yaml=cfg_path,
        )
