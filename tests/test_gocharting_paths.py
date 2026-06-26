from __future__ import annotations

from pathlib import Path

from PIL import Image

from automation_tool.gocharting_capture import (
    GOCHARTING_ALL_FLOW_WS_DETAIL_BACK_STEPS,
    GOCHARTING_UPDATE_SCALP_DETAIL_HISTORY_STEPS,
    gocharting_export_stem,
)
from automation_tool.gocharting_image_crop import GOCHARTING_IMAGE_WIDTH_THIRDS
from automation_tool.images import (
    GOCHARTING_GOLD_EXPORT_LABEL,
    GOCHARTING_DETAIL_PNG_PER_SLOT,
    append_footprint_json_paths,
    chart_image_order_for_main_symbol,
    extend_openai_payloads_with_footprint_json,
    footprint_source_for_stamp,
    gocharting_detail_png_paths,
    gocharting_detail_zoom_png_path_for_csv,
    gocharting_footprint_export_label,
    gocharting_interval_csv_path,
    gocharting_main_interval_csv_path,
    gocharting_png_path_for_csv,
    latest_chart_stamp,
    openai_payload_max_for_order,
    ordered_chart_images,
    ordered_chart_openai_payloads,
)


def _write_rgb_png(path: Path, width: int = 300, height: int = 100) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), (0, 128, 255)).save(path, format="PNG")


def test_gocharting_export_stem() -> None:
    assert gocharting_export_stem("20260616_120000", "GC", "15m") == (
        "20260616_120000_gocharting_GC_15m"
    )


def test_update_scalp_gocharting_detail_history_steps() -> None:
    assert GOCHARTING_UPDATE_SCALP_DETAIL_HISTORY_STEPS == 0
    assert GOCHARTING_ALL_FLOW_WS_DETAIL_BACK_STEPS == 1


def test_gocharting_footprint_export_label() -> None:
    assert gocharting_footprint_export_label("XAUUSD") == GOCHARTING_GOLD_EXPORT_LABEL
    assert gocharting_footprint_export_label("EURUSD") == "EURUSD"


def test_gocharting_detail_png_per_slot_is_tripled() -> None:
    assert GOCHARTING_DETAIL_PNG_PER_SLOT == 12
    assert GOCHARTING_DETAIL_PNG_PER_SLOT == 4 * GOCHARTING_IMAGE_WIDTH_THIRDS


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
    detail_cfg = {
        "detail_chart": {"page_url": "https://example.com/detail", "crop_width_thirds": True},
        "footprint_ws": {"enabled": False},
    }
    for sym, iv in (("DXY", "15m"), (GOCHARTING_GOLD_EXPORT_LABEL, "15m"), (GOCHARTING_GOLD_EXPORT_LABEL, "5m")):
        csv_p = charts / f"{stamp}_gocharting_{sym}_{iv}.csv"
        png_p = charts / f"{stamp}_gocharting_{sym}_{iv}.png"
        csv_p.write_text("Time,Open\n1,2\n", encoding="utf-8")
        png_p.write_bytes(b"x")
        if sym != "DXY":
            for suffix in ("zoom", "back_1", "back_2", "back_3"):
                _write_rgb_png(
                    charts / f"{stamp}_gocharting_{sym}_{iv}_detail_{suffix}.png"
                )
    payloads = ordered_chart_openai_payloads(charts, stamp=stamp, gocharting_cfg=detail_cfg)
    kinds = [k for k, _ in payloads]
    assert kinds.count("csv") == 3
    assert kinds.count("image") == 3 + 2 * GOCHARTING_DETAIL_PNG_PER_SLOT


def test_ordered_chart_openai_payloads_gocharting_ws_skips_detail(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    (charts / ".main_chart_symbol").write_text("XAUUSD\n", encoding="utf-8")
    stamp = "20260616_120000"
    ws_cfg = {"footprint_ws": {"enabled": True}}
    fp_dir = charts / "footprint_images"
    fp_dir.mkdir()
    for iv in ("15m", "5m"):
        (fp_dir / f"footprint_combined_{iv}.json").write_text(
            '{"symbol":"COMEX:GC1!","candles":[]}\n', encoding="utf-8"
        )
    for sym, iv in (("DXY", "15m"), (GOCHARTING_GOLD_EXPORT_LABEL, "15m"), (GOCHARTING_GOLD_EXPORT_LABEL, "5m")):
        csv_p = charts / f"{stamp}_gocharting_{sym}_{iv}.csv"
        png_p = charts / f"{stamp}_gocharting_{sym}_{iv}.png"
        csv_p.write_text("Time,Open\n1,2\n", encoding="utf-8")
        png_p.write_bytes(b"x")
        if sym != "DXY":
            _write_rgb_png(charts / f"{stamp}_gocharting_{sym}_{iv}_detail_zoom.png")
    payloads = ordered_chart_openai_payloads(charts, stamp=stamp, gocharting_cfg=ws_cfg)
    kinds = [k for k, _ in payloads]
    assert kinds.count("csv") == 3
    assert kinds.count("image") == 3
    assert all("_detail_" not in p.name for k, p in payloads if k == "image")


def test_ordered_chart_openai_payloads_gocharting_ws_includes_zoom_and_one_back(
    tmp_path: Path,
) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    (charts / ".main_chart_symbol").write_text("XAUUSD\n", encoding="utf-8")
    stamp = "20260616_120000"
    ws_cfg = {"footprint_ws": {"enabled": True}}
    sym = GOCHARTING_GOLD_EXPORT_LABEL
    for slot_sym, iv in (("DXY", "15m"), (sym, "15m"), (sym, "5m")):
        csv_p = charts / f"{stamp}_gocharting_{slot_sym}_{iv}.csv"
        png_p = charts / f"{stamp}_gocharting_{slot_sym}_{iv}.png"
        csv_p.write_text("Time,Open\n1,2\n", encoding="utf-8")
        png_p.write_bytes(b"x")
    for iv in ("15m", "5m"):
        _write_rgb_png(charts / f"{stamp}_gocharting_{sym}_{iv}_detail_zoom.png")
        _write_rgb_png(charts / f"{stamp}_gocharting_{sym}_{iv}_detail_back_1.png")
        _write_rgb_png(charts / f"{stamp}_gocharting_{sym}_{iv}_detail_back_2.png")

    payloads = ordered_chart_openai_payloads(
        charts,
        stamp=stamp,
        gocharting_cfg=ws_cfg,
        gocharting_detail_max_back_steps=GOCHARTING_ALL_FLOW_WS_DETAIL_BACK_STEPS,
    )
    image_names = [p.name for k, p in payloads if k == "image"]
    for iv in ("15m", "5m"):
        assert f"{stamp}_gocharting_{sym}_{iv}_detail_zoom_part1.png" in image_names
        assert f"{stamp}_gocharting_{sym}_{iv}_detail_back_1_part1.png" in image_names
        assert f"{stamp}_gocharting_{sym}_{iv}_detail_back_2_part1.png" not in image_names


def test_openai_payload_max_gocharting_order() -> None:
    order = chart_image_order_for_main_symbol("XAUUSD", footprint_source="gocharting")
    detail_cfg = {"footprint_ws": {"enabled": False}}
    assert openai_payload_max_for_order(order, gocharting_cfg=detail_cfg) == 38
    ws_cfg = {"footprint_ws": {"enabled": True}}
    assert openai_payload_max_for_order(order, gocharting_cfg=ws_cfg) == 14
    assert (
        openai_payload_max_for_order(
            order,
            gocharting_cfg=ws_cfg,
            gocharting_detail_max_back_steps=GOCHARTING_ALL_FLOW_WS_DETAIL_BACK_STEPS,
        )
        == 26
    )


def test_ordered_chart_images_gocharting_includes_detail_crop_panels(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    (charts / ".main_chart_symbol").write_text("XAUUSD\n", encoding="utf-8")
    stamp = "20260616_120000"
    sym = GOCHARTING_GOLD_EXPORT_LABEL
    for slot_sym, iv in (("DXY", "15m"), (sym, "15m"), (sym, "5m")):
        csv_p = charts / f"{stamp}_gocharting_{slot_sym}_{iv}.csv"
        csv_p.write_text("Time,Open\n1,2\n", encoding="utf-8")
    for iv in ("15m", "5m"):
        ov = charts / f"{stamp}_gocharting_{sym}_{iv}.png"
        ov.write_bytes(b"ov")
        for suffix in ("zoom", "back_1", "back_2", "back_3"):
            _write_rgb_png(charts / f"{stamp}_gocharting_{sym}_{iv}_detail_{suffix}.png")
    (charts / f"{stamp}_gocharting_DXY_15m.png").write_bytes(b"dxy")
    paths = ordered_chart_images(
        charts,
        stamp=stamp,
        gocharting_cfg={"footprint_ws": {"enabled": False}},
    )
    names = [p.name for p in paths]
    assert f"{stamp}_gocharting_DXY_15m.png" in names
    for iv in ("15m", "5m"):
        assert f"{stamp}_gocharting_{sym}_{iv}.png" in names
        assert f"{stamp}_gocharting_{sym}_{iv}_detail_zoom_part1.png" in names
        assert f"{stamp}_gocharting_{sym}_{iv}_detail_back_1_part3.png" in names
    dxy_idx = names.index(f"{stamp}_gocharting_DXY_15m.png")
    m15_ov_idx = names.index(f"{stamp}_gocharting_{sym}_15m.png")
    m15_zoom_part_idx = names.index(f"{stamp}_gocharting_{sym}_15m_detail_zoom_part1.png")
    assert dxy_idx < m15_ov_idx < m15_zoom_part_idx


def test_ordered_chart_images_gocharting_no_crop(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    (charts / ".main_chart_symbol").write_text("XAUUSD\n", encoding="utf-8")
    stamp = "20260616_120000"
    sym = GOCHARTING_GOLD_EXPORT_LABEL
    for slot_sym, iv in (("DXY", "15m"), (sym, "15m"), (sym, "5m")):
        csv_p = charts / f"{stamp}_gocharting_{slot_sym}_{iv}.csv"
        csv_p.write_text("Time,Open\n1,2\n", encoding="utf-8")
    for iv in ("15m", "5m"):
        ov = charts / f"{stamp}_gocharting_{sym}_{iv}.png"
        ov.write_bytes(b"ov")
        for suffix in ("zoom", "back_1"):
            _write_rgb_png(charts / f"{stamp}_gocharting_{sym}_{iv}_detail_{suffix}.png")
    (charts / f"{stamp}_gocharting_DXY_15m.png").write_bytes(b"dxy")
    paths = ordered_chart_images(
        charts,
        stamp=stamp,
        gocharting_cfg={
            "detail_chart": {"crop_width_thirds": False},
            "footprint_ws": {"enabled": False},
        },
    )
    names = [p.name for p in paths]
    assert f"{stamp}_gocharting_{sym}_15m_detail_zoom.png" in names
    assert f"{stamp}_gocharting_{sym}_15m_detail_zoom_part1.png" not in names


def test_extend_openai_payloads_with_footprint_combined_json(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    fp_dir = charts / "footprint_images"
    fp_dir.mkdir(parents=True)
    combined = fp_dir / "footprint_combined_5m.json"
    combined.write_text('{"candles":[]}\n', encoding="utf-8")
    payloads = extend_openai_payloads_with_footprint_json(
        [],
        charts,
        gocharting_cfg={"footprint_ws": {"enabled": True}},
    )
    assert len(payloads) == 1
    assert payloads[0] == ("json", combined)


def test_append_footprint_json_paths_prefers_combined(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    fp_dir = charts / "footprint_images"
    fp_dir.mkdir(parents=True)
    combined = fp_dir / "footprint_combined_15m.json"
    combined.write_text('{"candles":[]}\n', encoding="utf-8")
    bid_ask = fp_dir / "footprint_bid_ask_15m.json"
    bid_ask.write_text('{"candles":[]}\n', encoding="utf-8")
    paths = append_footprint_json_paths(
        [],
        charts,
        gocharting_cfg={"footprint_ws": {"enabled": True}},
    )
    assert combined in paths
    assert bid_ask not in paths


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
