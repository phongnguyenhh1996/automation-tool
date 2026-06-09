"""Tests for chart JSON validation helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from automation_tool.chart_payload_validate import (
    ChartSlotIssue,
    coinmap_json_stem_matches_step,
    coinmap_raw_export_paths_for_stamp,
    filter_coinmap_plan_for_retry_paths,
    is_coinmap_stale_chart_issue,
    list_invalid_chart_slots_for_stamp,
    require_valid_coinmap_exports_for_stamp,
    require_valid_coinmap_json_paths,
    validate_coinmap_candle_freshness,
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


def _fresh_coinmap_payload(*, interval: str, lag_minutes: float = 2.0) -> dict:
    now = datetime(2026, 6, 9, 9, 42, 0, tzinfo=timezone.utc)
    ct = now.timestamp() * 1000 - lag_minutes * 60 * 1000
    t = ct - 5 * 60 * 1000 + 1
    return {
        "generated_at": now.isoformat(),
        "interval": interval,
        "getcandlehistory": [{"t": int(t), "ct": int(ct), "i": interval}],
        "getorderflowhistory": [{"t": int(t)}],
        "getindicatorsvwap": [{"t": int(t), "data": {}}],
    }


def test_validate_coinmap_freshness_ok_5m() -> None:
    ok, _ = validate_coinmap_candle_freshness(_fresh_coinmap_payload(interval="5m", lag_minutes=10))
    assert ok


def test_validate_coinmap_freshness_stale_5m() -> None:
    ok, reason = validate_coinmap_candle_freshness(
        _fresh_coinmap_payload(interval="5m", lag_minutes=20)
    )
    assert not ok
    assert "stale" in reason
    assert "5m" in reason


def test_validate_coinmap_freshness_ok_15m() -> None:
    ok, _ = validate_coinmap_candle_freshness(
        _fresh_coinmap_payload(interval="15m", lag_minutes=25)
    )
    assert ok


def test_validate_coinmap_freshness_stale_15m() -> None:
    ok, reason = validate_coinmap_candle_freshness(
        _fresh_coinmap_payload(interval="15m", lag_minutes=35)
    )
    assert not ok
    assert "stale" in reason


def test_validate_coinmap_export_payload_includes_freshness() -> None:
    ok, reason = validate_coinmap_export_payload(
        _fresh_coinmap_payload(interval="5m", lag_minutes=20)
    )
    assert not ok
    assert "stale" in reason


def test_require_valid_coinmap_json_paths(tmp_path: Path) -> None:
    p = tmp_path / "x_coinmap_XAUUSD_5m.json"
    p.write_text(json.dumps(_fresh_coinmap_payload(interval="5m", lag_minutes=20)), encoding="utf-8")
    try:
        require_valid_coinmap_json_paths([p])
        raised = False
    except SystemExit as e:
        raised = True
        assert "stale" in str(e)
    assert raised


def test_coinmap_raw_export_paths_for_stamp(tmp_path: Path) -> None:
    stamp = "20260101_120000"
    for name in (
        f"{stamp}_coinmap_DXY_15m.json",
        f"{stamp}_coinmap_XAUUSD_15m.json",
        f"{stamp}_coinmap_XAUUSD_5m.json",
        f"{stamp}_coinmap_XAUUSD_merged.json",
    ):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    paths = coinmap_raw_export_paths_for_stamp(tmp_path, stamp)
    assert [p.name for p in paths] == [
        f"{stamp}_coinmap_DXY_15m.json",
        f"{stamp}_coinmap_XAUUSD_15m.json",
        f"{stamp}_coinmap_XAUUSD_5m.json",
    ]  # 15m exports before 5m


def test_require_valid_coinmap_exports_for_stamp_all_symbols(tmp_path: Path) -> None:
    stamp = "20260101_120000"
    for sym, iv, lag in (("DXY", "15m", 2), ("XAUUSD", "15m", 2), ("XAUUSD", "5m", 20)):
        p = tmp_path / f"{stamp}_coinmap_{sym}_{iv}.json"
        p.write_text(
            json.dumps(_fresh_coinmap_payload(interval=iv, lag_minutes=lag)),
            encoding="utf-8",
        )
    try:
        require_valid_coinmap_exports_for_stamp(tmp_path, stamp)
        raised = False
    except SystemExit as e:
        raised = True
        assert "stale" in str(e)
        assert "5m" in str(e)
    assert raised


def test_is_coinmap_stale_chart_issue() -> None:
    stale = ChartSlotIssue(
        source="coinmap",
        symbol="XAUUSD",
        interval="5m",
        expected_path=Path("x.json"),
        reason="Coinmap data stale: newest candle lags generated_at by 20.0m (max 15m for 5m)",
    )
    assert is_coinmap_stale_chart_issue(stale)
    ok = ChartSlotIssue(
        source="coinmap",
        symbol="XAUUSD",
        interval="5m",
        expected_path=Path("x.json"),
        reason="getcandlehistory missing, null, or empty list",
    )
    assert not is_coinmap_stale_chart_issue(ok)


def test_list_invalid_slots_coinmap_stale_raw_export(tmp_path: Path) -> None:
    write_main_chart_symbol_marker(tmp_path, "XAUUSD")
    stamp = "20260101_120000"
    for sym, iv, lag in (("XAUUSD", "5m", 20), ("XAUUSD", "15m", 2), ("DXY", "15m", 2)):
        p = tmp_path / f"{stamp}_coinmap_{sym}_{iv}.json"
        p.write_text(
            json.dumps(_fresh_coinmap_payload(interval=iv, lag_minutes=lag)),
            encoding="utf-8",
        )
        for suffix in ("4h", "1h"):
            tv = tmp_path / f"{stamp}_tradingview_{sym}_{suffix}.png"
            tv.write_bytes(b"\x89PNG\r\n\x1a\n")
        if sym == "XAUUSD":
            for suffix in ("15m_ict", "5m"):
                tv = tmp_path / f"{stamp}_tradingview_{sym}_{suffix}.png"
                tv.write_bytes(b"\x89PNG\r\n\x1a\n")
    bad = list_invalid_chart_slots_for_stamp(tmp_path, stamp)
    stale = [x for x in bad if "stale" in x.reason]
    assert len(stale) == 1
    assert stale[0].expected_path.name == f"{stamp}_coinmap_XAUUSD_5m.json"


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
    assert len(bad) == 11
    assert all(isinstance(x, ChartSlotIssue) for x in bad)
    assert all("missing" in x.reason.lower() for x in bad)


def test_tradingview_slots_skip_json_when_https_url(tmp_path: Path) -> None:
    write_main_chart_symbol_marker(tmp_path, "XAUUSD")
    stamp = "20260101_120000"
    for sym in ("DXY", "XAUUSD"):
        intervals = (
            ("4h", "1h", "15m", "15m_ict", "5m") if sym == "XAUUSD" else ("4h", "1h", "15m")
        )
        for iv in intervals:
            p = tmp_path / f"{stamp}_tradingview_{sym}_{iv}.url"
            p.write_text("https://example.invalid/snap\n", encoding="utf-8")
    bad = list_invalid_chart_slots_for_stamp(tmp_path, stamp)
    assert len(bad) == 3
    assert all(x.source == "coinmap" for x in bad)


def test_tradingview_slots_skip_json_when_png_only(tmp_path: Path) -> None:
    write_main_chart_symbol_marker(tmp_path, "XAUUSD")
    stamp = "20260101_120000"
    for sym, iv in (
        ("DXY", "4h"),
        ("DXY", "1h"),
        ("DXY", "15m"),
        ("XAUUSD", "4h"),
        ("XAUUSD", "1h"),
        ("XAUUSD", "15m"),
        ("XAUUSD", "15m_ict"),
        ("XAUUSD", "5m"),
    ):
        (tmp_path / f"{stamp}_tradingview_{sym}_{iv}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    bad = list_invalid_chart_slots_for_stamp(tmp_path, stamp)
    assert len(bad) == 3
    assert all(x.source == "coinmap" for x in bad)


def test_tradingview_json_still_validated_when_present(tmp_path: Path) -> None:
    write_main_chart_symbol_marker(tmp_path, "XAUUSD")
    stamp = "20260101_120000"
    p = tmp_path / f"{stamp}_tradingview_DXY_4h.json"
    p.write_text(json.dumps({"bars": []}), encoding="utf-8")
    bad = list_invalid_chart_slots_for_stamp(tmp_path, stamp)
    tv_bad = [x for x in bad if x.source == "tradingview" and x.expected_path == p]
    assert len(tv_bad) == 1
    assert "bars" in tv_bad[0].reason


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
