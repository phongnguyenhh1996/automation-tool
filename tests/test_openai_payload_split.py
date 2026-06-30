from __future__ import annotations

from pathlib import Path

from automation_tool.images import split_openai_payloads_by_phase


def test_split_tradingview_urls_and_files(tmp_path: Path) -> None:
    tv_json = tmp_path / "20260629_165354_tradingview_DXY_4h.json"
    tv_json.write_text("{}", encoding="utf-8")
    tv_png = tmp_path / "20260629_165354_tradingview_XAUUSD_5m.png"
    tv_png.write_bytes(b"\x89PNG")

    payloads = [
        ("json", tv_json),
        ("image_url", "https://example.com/snapshot.png"),
        ("image", tv_png),
    ]
    structure, footprint = split_openai_payloads_by_phase(payloads)
    assert len(structure) == 3
    assert footprint == []


def test_split_gocharting_gc_and_footprint_json(tmp_path: Path) -> None:
    csv = tmp_path / "20260629_165354_gocharting_GC_15m.csv"
    csv.write_text("time,open\n", encoding="utf-8")
    png = tmp_path / "20260629_165354_gocharting_GC_15m.png"
    png.write_bytes(b"\x89PNG")
    detail = tmp_path / "20260629_165354_gocharting_GC_15m_detail_zoom.png"
    detail.write_bytes(b"\x89PNG")
    fp_dir = tmp_path / "footprint_images"
    fp_dir.mkdir()
    combined = fp_dir / "footprint_combined_15m.json"
    combined.write_text("{}", encoding="utf-8")
    coinmap = tmp_path / "20260629_165354_coinmap_XAUUSD_15m.json"
    coinmap.write_text("{}", encoding="utf-8")

    payloads = [
        ("csv", csv),
        ("image", png),
        ("image", detail),
        ("json", combined),
        ("json", coinmap),
    ]
    structure, footprint = split_openai_payloads_by_phase(payloads)
    assert structure == []
    assert len(footprint) == 5
    assert footprint[0][1] == csv
    assert footprint[-1][1] == coinmap


def test_split_prepared_footprint_json_only(tmp_path: Path) -> None:
    prepared = tmp_path / "footprint_XAUUSD_15m.json"
    prepared.write_text("{}", encoding="utf-8")
    coinmap = tmp_path / "20260629_165354_coinmap_XAUUSD_15m.json"
    coinmap.write_text("{}", encoding="utf-8")

    payloads = [
        ("json", prepared),
        ("json", coinmap),
    ]
    structure, footprint = split_openai_payloads_by_phase(payloads)
    assert structure == []
    assert len(footprint) == 2
    assert footprint[0][1] == prepared


def test_split_dxy_gocharting_overview_png_to_structure_csv_excluded(tmp_path: Path) -> None:
    dxy_csv = tmp_path / "20260629_165354_gocharting_DXY_15m.csv"
    dxy_csv.write_text("time\n", encoding="utf-8")
    dxy_png = tmp_path / "20260629_165354_gocharting_DXY_15m.png"
    dxy_png.write_bytes(b"\x89PNG")
    gc_csv = tmp_path / "20260629_165354_gocharting_GC_15m.csv"
    gc_csv.write_text("time\n", encoding="utf-8")

    payloads = [
        ("csv", dxy_csv),
        ("image", dxy_png),
        ("csv", gc_csv),
    ]
    structure, footprint = split_openai_payloads_by_phase(payloads)
    assert structure == [("image", dxy_png)]
    assert footprint == [("csv", gc_csv)]


def test_split_preserves_order_mixed(tmp_path: Path) -> None:
    tv2 = tmp_path / "20260629_165354_tradingview_XAUUSD_4h.json"
    tv2.write_text("{}", encoding="utf-8")
    gc_csv = tmp_path / "20260629_165354_gocharting_DXY_15m.csv"
    gc_csv.write_text("x\n", encoding="utf-8")
    dxy_png = tmp_path / "20260629_165354_gocharting_DXY_15m.png"
    dxy_png.write_bytes(b"\x89PNG")

    payloads = [
        ("image_url", "https://a.example/tv1"),
        ("csv", gc_csv),
        ("image", dxy_png),
        ("json", tv2),
    ]
    structure, footprint = split_openai_payloads_by_phase(payloads)
    assert [p for _, p in structure] == ["https://a.example/tv1", dxy_png, tv2]
    assert footprint == []
