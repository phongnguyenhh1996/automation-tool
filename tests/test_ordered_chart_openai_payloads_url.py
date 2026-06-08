"""TradingView .url snapshot files map to image_url payloads for OpenAI."""

from pathlib import Path

from automation_tool.images import (
    coinmap_png_path_for_json,
    openai_payloads_for_attachment_paths,
    ordered_chart_openai_payloads,
    write_main_chart_symbol_marker,
)


def test_tradingview_url_over_png(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    write_main_chart_symbol_marker(charts, "XAUUSD")
    stamp = "20260101_120000"
    # First slot in order: tradingview DXY 4h
    url_file = charts / f"{stamp}_tradingview_DXY_4h.url"
    url_file.write_text("https://example.com/snap.png\n", encoding="utf-8")
    png_file = charts / f"{stamp}_tradingview_DXY_4h.png"
    png_file.write_bytes(b"fakepng")

    payloads = ordered_chart_openai_payloads(charts, stamp=stamp)
    assert payloads
    k0, v0 = payloads[0]
    assert k0 == "image_url"
    assert v0 == "https://example.com/snap.png"


def test_tradingview_png_when_no_url(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    write_main_chart_symbol_marker(charts, "XAUUSD")
    stamp = "20260101_120000"
    png_file = charts / f"{stamp}_tradingview_DXY_4h.png"
    png_file.write_bytes(b"fakepng")

    payloads = ordered_chart_openai_payloads(charts, stamp=stamp)
    assert payloads
    k0, v0 = payloads[0]
    assert k0 == "image"
    assert v0 == png_file


def test_coinmap_json_and_png_both_attached(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    write_main_chart_symbol_marker(charts, "XAUUSD")
    stamp = "20260101_120000"
    jp = charts / f"{stamp}_coinmap_DXY_15m.json"
    pp = charts / f"{stamp}_coinmap_DXY_15m.png"
    jp.write_text("{}", encoding="utf-8")
    pp.write_bytes(b"png")

    payloads = ordered_chart_openai_payloads(charts, stamp=stamp)
    # First 8 slots are TradingView (missing); coinmap DXY 15m is slot index 8
    coinmap_payloads = [p for p in payloads if "coinmap" in str(p[1])]
    assert len(coinmap_payloads) == 2
    assert coinmap_payloads[0] == ("json", jp)
    assert coinmap_payloads[1] == ("image", pp)


def test_openai_payloads_for_attachment_paths_interleaves_coinmap_png(tmp_path: Path) -> None:
    m15_j = tmp_path / "20260101_120000_coinmap_XAUUSD_15m.json"
    m15_p = tmp_path / "20260101_120000_coinmap_XAUUSD_15m.png"
    m5_j = tmp_path / "20260101_120000_coinmap_XAUUSD_5m.json"
    m5_p = tmp_path / "20260101_120000_coinmap_XAUUSD_5m.png"
    morning = tmp_path / "morning_full_analysis.json"
    for p in (m15_j, m5_j, morning):
        p.write_text("{}", encoding="utf-8")
    m15_p.write_bytes(b"15")
    m5_p.write_bytes(b"5")

    payloads = openai_payloads_for_attachment_paths([morning, m15_j, m5_j])
    assert payloads == [
        ("json", morning),
        ("json", m15_j),
        ("image", m15_p),
        ("json", m5_j),
        ("image", m5_p),
    ]
    assert coinmap_png_path_for_json(m15_j) == m15_p
