from __future__ import annotations

from pathlib import Path

from automation_tool.gocharting_capture import gocharting_export_stem
from automation_tool.images import (
    footprint_source_for_stamp,
    gocharting_interval_csv_path,
    gocharting_main_interval_csv_path,
    gocharting_png_path_for_csv,
    latest_chart_stamp,
    ordered_chart_openai_payloads,
)


def test_gocharting_export_stem() -> None:
    assert gocharting_export_stem("20260616_120000", "XAUUSD", "15m") == (
        "20260616_120000_gocharting_XAUUSD_15m"
    )


def test_footprint_source_detection(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    stamp = "20260616_120000"
    (charts / f"{stamp}_gocharting_XAUUSD_5m.csv").write_text("h\n1", encoding="utf-8")
    assert footprint_source_for_stamp(charts, stamp=stamp) == "gocharting"
    assert footprint_source_for_stamp(charts / "missing") == "coinmap"


def test_gocharting_path_helpers(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    (charts / ".main_chart_symbol").write_text("XAUUSD\n", encoding="utf-8")
    stamp = "20260616_120000"
    csv_p = charts / f"{stamp}_gocharting_XAUUSD_15m.csv"
    png_p = charts / f"{stamp}_gocharting_XAUUSD_15m.png"
    csv_p.write_text("Time,Open\n1,2\n", encoding="utf-8")
    png_p.write_bytes(b"png")
    assert gocharting_main_interval_csv_path(charts, "15m", stamp=stamp) == csv_p
    assert gocharting_interval_csv_path(charts, "XAUUSD", "15m", stamp=stamp) == csv_p
    assert gocharting_png_path_for_csv(csv_p) == png_p
    assert latest_chart_stamp(charts) == stamp


def test_ordered_chart_openai_payloads_gocharting(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    (charts / ".main_chart_symbol").write_text("XAUUSD\n", encoding="utf-8")
    stamp = "20260616_120000"
    for sym, iv in (("DXY", "15m"), ("XAUUSD", "15m"), ("XAUUSD", "5m")):
        csv_p = charts / f"{stamp}_gocharting_{sym}_{iv}.csv"
        png_p = charts / f"{stamp}_gocharting_{sym}_{iv}.png"
        csv_p.write_text("Time,Open\n1,2\n", encoding="utf-8")
        png_p.write_bytes(b"x")
    payloads = ordered_chart_openai_payloads(charts, stamp=stamp)
    kinds = [k for k, _ in payloads]
    assert kinds.count("csv") == 3
    assert kinds.count("image") >= 3
