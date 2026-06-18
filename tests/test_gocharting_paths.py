from __future__ import annotations

from pathlib import Path

from automation_tool.gocharting_capture import gocharting_export_stem
from automation_tool.images import (
    GOCHARTING_GOLD_EXPORT_LABEL,
    GOCHARTING_DETAIL_PNG_PER_SLOT,
    chart_image_order_for_main_symbol,
    footprint_source_for_stamp,
    gocharting_detail_png_paths,
    gocharting_detail_zoom_png_path_for_csv,
    gocharting_footprint_export_label,
    gocharting_interval_csv_path,
    gocharting_main_interval_csv_path,
    gocharting_png_path_for_csv,
    latest_chart_stamp,
    openai_payload_max_for_order,
    ordered_chart_openai_payloads,
)


def test_gocharting_export_stem() -> None:
    assert gocharting_export_stem("20260616_120000", "GC", "15m") == (
        "20260616_120000_gocharting_GC_15m"
    )


def test_gocharting_footprint_export_label() -> None:
    assert gocharting_footprint_export_label("XAUUSD") == GOCHARTING_GOLD_EXPORT_LABEL
    assert gocharting_footprint_export_label("EURUSD") == "EURUSD"


def test_footprint_source_detection(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    stamp = "20260616_120000"
    (charts / f"{stamp}_gocharting_{GOCHARTING_GOLD_EXPORT_LABEL}_5m.csv").write_text(
        "h\n1", encoding="utf-8"
    )
    assert footprint_source_for_stamp(charts, stamp=stamp) == "gocharting"
    assert footprint_source_for_stamp(charts / "missing") == "coinmap"


def test_gocharting_path_helpers(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    (charts / ".main_chart_symbol").write_text("XAUUSD\n", encoding="utf-8")
    stamp = "20260616_120000"
    csv_p = charts / f"{stamp}_gocharting_{GOCHARTING_GOLD_EXPORT_LABEL}_15m.csv"
    png_p = charts / f"{stamp}_gocharting_{GOCHARTING_GOLD_EXPORT_LABEL}_15m.png"
    csv_p.write_text("Time,Open\n1,2\n", encoding="utf-8")
    png_p.write_bytes(b"png")
    detail_p = charts / f"{stamp}_gocharting_{GOCHARTING_GOLD_EXPORT_LABEL}_15m_detail_zoom.png"
    detail_p.write_bytes(b"detail")
    assert gocharting_main_interval_csv_path(charts, "15m", stamp=stamp) == csv_p
    assert gocharting_interval_csv_path(charts, GOCHARTING_GOLD_EXPORT_LABEL, "15m", stamp=stamp) == csv_p
    assert gocharting_png_path_for_csv(csv_p) == png_p
    assert gocharting_detail_zoom_png_path_for_csv(csv_p) == detail_p
    assert latest_chart_stamp(charts) == stamp


def test_ordered_chart_openai_payloads_gocharting(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    (charts / ".main_chart_symbol").write_text("XAUUSD\n", encoding="utf-8")
    stamp = "20260616_120000"
    for sym, iv in (("DXY", "15m"), (GOCHARTING_GOLD_EXPORT_LABEL, "15m"), (GOCHARTING_GOLD_EXPORT_LABEL, "5m")):
        csv_p = charts / f"{stamp}_gocharting_{sym}_{iv}.csv"
        png_p = charts / f"{stamp}_gocharting_{sym}_{iv}.png"
        csv_p.write_text("Time,Open\n1,2\n", encoding="utf-8")
        png_p.write_bytes(b"x")
        if sym != "DXY":
            for suffix in ("zoom", "back_1", "back_2", "back_3"):
                dp = charts / f"{stamp}_gocharting_{sym}_{iv}_detail_{suffix}.png"
                dp.write_bytes(b"d")
    payloads = ordered_chart_openai_payloads(charts, stamp=stamp)
    kinds = [k for k, _ in payloads]
    assert kinds.count("csv") == 3
    assert kinds.count("image") == 3 + 2 * GOCHARTING_DETAIL_PNG_PER_SLOT


def test_openai_payload_max_gocharting_order() -> None:
    order = chart_image_order_for_main_symbol("XAUUSD", footprint_source="gocharting")
    assert openai_payload_max_for_order(order) == 22


def test_gocharting_detail_png_paths_order(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    stamp = "20260616_120000"
    sym = GOCHARTING_GOLD_EXPORT_LABEL
    iv = "5m"
    for name in (
        f"{stamp}_gocharting_{sym}_{iv}_detail_back_3.png",
        f"{stamp}_gocharting_{sym}_{iv}_detail_zoom.png",
        f"{stamp}_gocharting_{sym}_{iv}_detail_back_1.png",
        f"{stamp}_gocharting_{sym}_{iv}_detail_back_2.png",
    ):
        (charts / name).write_bytes(b"x")
    paths = gocharting_detail_png_paths(charts, stamp, sym, iv)
    assert [p.name for p in paths] == [
        f"{stamp}_gocharting_{sym}_{iv}_detail_zoom.png",
        f"{stamp}_gocharting_{sym}_{iv}_detail_back_1.png",
        f"{stamp}_gocharting_{sym}_{iv}_detail_back_2.png",
        f"{stamp}_gocharting_{sym}_{iv}_detail_back_3.png",
    ]
