from __future__ import annotations

import json
from pathlib import Path

import pytest

from automation_tool.chart_payload_validate import (
    gocharting_footprint_ws_json_path,
    list_invalid_chart_slots_for_stamp,
    require_valid_gocharting_footprint_ws_exports,
    validate_gocharting_footprint_ws_file,
)
from automation_tool.images import GOCHARTING_GOLD_EXPORT_LABEL, write_main_chart_symbol_marker


def test_validate_gocharting_footprint_ws_file(tmp_path: Path) -> None:
    path = tmp_path / "footprint_combined_5m.json"
    path.write_text(json.dumps({"candles": [{"time_gmt7": "x"}]}), encoding="utf-8")
    ok, reason = validate_gocharting_footprint_ws_file(path)
    assert ok is True
    assert reason == ""


def test_list_invalid_chart_slots_skips_dxy_when_footprint_ws(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    write_main_chart_symbol_marker(charts, "XAUUSD")
    fp_dir = charts / "footprint_images"
    fp_dir.mkdir()
    (fp_dir / "footprint_combined_15m.json").write_text(
        json.dumps({"candles": [{"time_gmt7": "a"}]}),
        encoding="utf-8",
    )
    (fp_dir / "footprint_combined_5m.json").write_text(
        json.dumps({"candles": [{"time_gmt7": "b"}]}),
        encoding="utf-8",
    )
    ws_cfg = {"footprint_ws": {"enabled": True}}
    bad = list_invalid_chart_slots_for_stamp(
        charts,
        "20260616_120000",
        gocharting_cfg=ws_cfg,
    )
    gc_bad = [x for x in bad if x.source == "gocharting"]
    assert not [x for x in gc_bad if x.symbol == "DXY"]
    assert not gc_bad


def test_require_valid_gocharting_footprint_ws_exports_missing(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    ws_cfg = {"footprint_ws": {"enabled": True}}
    with pytest.raises(SystemExit, match="footprint WS validation failed"):
        require_valid_gocharting_footprint_ws_exports(
            charts,
            ws_cfg,
            intervals=("5m",),
        )


def test_gocharting_footprint_ws_json_path(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    fp_dir = charts / "footprint_images"
    fp_dir.mkdir()
    path = gocharting_footprint_ws_json_path(charts, "5m")
    assert path == fp_dir / "footprint_combined_5m.json"
    assert GOCHARTING_GOLD_EXPORT_LABEL == "GC"
