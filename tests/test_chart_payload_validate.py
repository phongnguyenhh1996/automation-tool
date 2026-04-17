"""Tests for chart JSON validation helpers."""

from __future__ import annotations

import json
from pathlib import Path

from automation_tool.chart_payload_validate import (
    ChartSlotIssue,
    coinmap_json_stem_matches_step,
    filter_coinmap_plan_for_retry_paths,
    list_invalid_chart_slots_for_stamp,
    validate_coinmap_export_payload,
    validate_tradingview_tvdatafeed_payload,
)
from automation_tool.images import write_main_chart_symbol_marker


def test_validate_coinmap_ok() -> None:
    ok, _ = validate_coinmap_export_payload(
        {
            "getcandlehistory": [{"t": 1}],
            "getorderflowhistory": [{"t": 2}],
            "getindicatorsvwap": [{"t": 3, "data": {}}],
        }
    )
    assert ok


def test_validate_coinmap_empty_key() -> None:
    ok, reason = validate_coinmap_export_payload(
        {
            "getcandlehistory": [],
            "getorderflowhistory": [{"t": 2}],
            "getindicatorsvwap": [{"t": 3}],
        }
    )
    assert not ok
    assert "getcandlehistory" in reason


def test_validate_tv_ok() -> None:
    ok, _ = validate_tradingview_tvdatafeed_payload({"bars": [{"open": 1}]})
    assert ok


def test_validate_tv_empty_bars() -> None:
    ok, reason = validate_tradingview_tvdatafeed_payload({"bars": []})
    assert not ok
    assert "bars" in reason


def test_coinmap_stem_matches_step(tmp_path: Path) -> None:
    stamp = "20260101_120000"
    step = {"symbol": "DXY", "interval": "15m", "watchlist_category": "forex 1"}
    assert coinmap_json_stem_matches_step(stamp, step, f"{stamp}_coinmap_DXY_15m")
    assert not coinmap_json_stem_matches_step(stamp, step, f"{stamp}_coinmap_XAUUSD_15m")


def test_filter_coinmap_plan_for_retry_paths() -> None:
    stamp = "20260101_120000"
    plan = [
        {"symbol": "DXY", "interval": "15m", "watchlist_category": None},
        {"symbol": "XAUUSD", "interval": "5m", "watchlist_category": None},
    ]
    targets = [Path(f"{stamp}_coinmap_DXY_15m.json")]
    out = filter_coinmap_plan_for_retry_paths(plan, stamp, targets)
    assert len(out) == 1 and out[0]["symbol"] == "DXY"


def test_list_invalid_slots_missing_json(tmp_path: Path) -> None:
    write_main_chart_symbol_marker(tmp_path, "XAUUSD")
    stamp = "20260101_120000"
    bad = list_invalid_chart_slots_for_stamp(tmp_path, stamp)
    assert len(bad) == 10
    assert all(isinstance(x, ChartSlotIssue) for x in bad)
    assert all("missing" in x.reason.lower() for x in bad)


def test_list_invalid_slots_coinmap_empty_arrays(tmp_path: Path) -> None:
    write_main_chart_symbol_marker(tmp_path, "XAUUSD")
    stamp = "20260101_120000"
    p = tmp_path / f"{stamp}_coinmap_DXY_15m.json"
    p.write_text(
        json.dumps(
            {
                "getcandlehistory": [],
                "getorderflowhistory": [{"x": 1}],
                "getindicatorsvwap": [{"x": 1}],
            }
        ),
        encoding="utf-8",
    )
    bad = list_invalid_chart_slots_for_stamp(tmp_path, stamp)
    names = [x.expected_path.name for x in bad]
    assert f"{stamp}_coinmap_DXY_15m.json" in names
    dxy_issue = next(x for x in bad if x.expected_path.name == f"{stamp}_coinmap_DXY_15m.json")
    assert "getcandlehistory" in dxy_issue.reason


def test_expected_tvdatafeed_path_matches_run_task_slug() -> None:
    from automation_tool.tvdatafeed_capture import _expected_tvdatafeed_out_path

    tv = {"interval_filename_slugs": {}}
    meta = {
        "file_sym_key": "DXY",
        "label": "1 giờ",
    }
    p = _expected_tvdatafeed_out_path(
        charts_dir=Path("/tmp"), stamp="s", tv=tv, meta=meta
    )
    assert p.name == "s_tradingview_DXY_1h.json"
