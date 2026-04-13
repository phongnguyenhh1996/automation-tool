"""TradingView .url snapshot files map to image_url payloads for OpenAI."""

from pathlib import Path

from automation_tool.images import ordered_chart_openai_payloads, write_main_chart_symbol_marker


def test_tradingview_url_over_png(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    write_main_chart_symbol_marker(charts, "XAUUSD")
    stamp = "20260101_120000"
    # First slot in order: tradingview DXY 1h
    url_file = charts / f"{stamp}_tradingview_DXY_1h.url"
    url_file.write_text("https://example.com/snap.png\n", encoding="utf-8")
    png_file = charts / f"{stamp}_tradingview_DXY_1h.png"
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
    png_file = charts / f"{stamp}_tradingview_DXY_1h.png"
    png_file.write_bytes(b"fakepng")

    payloads = ordered_chart_openai_payloads(charts, stamp=stamp)
    assert payloads
    k0, v0 = payloads[0]
    assert k0 == "image"
    assert v0 == png_file
