from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from automation_tool.chart_payload_validate import list_invalid_chart_slots_for_stamp
from automation_tool.images import (
    GC_CHART_SLOT_COUNT,
    chart_image_order_for_gc,
    effective_chart_image_order,
    ensure_gc_manual_url_placeholders,
    ordered_chart_openai_payloads,
    wait_for_gc_manual_urls,
    write_gc_mode_marker,
    write_main_chart_symbol_marker,
)


def test_chart_image_order_for_gc() -> None:
    o = chart_image_order_for_gc("XAUUSD")
    assert len(o) == GC_CHART_SLOT_COUNT == 10
    assert ("gc_url", "GC", "15m") in o
    assert ("gc_url", "GC", "5m") in o
    assert all(x[0] != "coinmap" for x in o)
    assert ("tradingview", "DXY", "4h") in o
    assert ("tradingview", "XAUUSD", "5m") in o


def test_effective_chart_image_order_gc_marker(tmp_path: Path) -> None:
    write_main_chart_symbol_marker(tmp_path, "XAUUSD")
    write_gc_mode_marker(tmp_path)
    o = effective_chart_image_order(tmp_path)
    assert len(o) == 10
    assert ("gc_url", "GC", "15m") in o


def test_ordered_chart_openai_payloads_gc_urls(tmp_path: Path) -> None:
    write_main_chart_symbol_marker(tmp_path, "XAUUSD")
    write_gc_mode_marker(tmp_path)
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
        p = tmp_path / f"{stamp}_tradingview_{sym}_{iv}.url"
        p.write_text(f"https://example.invalid/{sym}_{iv}\n", encoding="utf-8")
    (tmp_path / "gc_m15.url").write_text("https://example.invalid/gc_m15\n", encoding="utf-8")
    (tmp_path / "gc_m5.url").write_text("https://example.invalid/gc_m5\n", encoding="utf-8")

    payloads = ordered_chart_openai_payloads(tmp_path, stamp=stamp)
    assert len(payloads) == 10
    urls = [p[1] for p in payloads if p[0] == "image_url"]
    assert "https://example.invalid/gc_m15" in urls
    assert "https://example.invalid/gc_m5" in urls


def test_ensure_gc_manual_url_placeholders_creates_empty(tmp_path: Path) -> None:
    p15, p5 = ensure_gc_manual_url_placeholders(tmp_path)
    assert p15.is_file() and p5.is_file()
    assert p15.read_text(encoding="utf-8") == ""
    assert p5.read_text(encoding="utf-8") == ""


def test_wait_for_gc_manual_urls_returns_when_filled(tmp_path: Path) -> None:
    ensure_gc_manual_url_placeholders(tmp_path)
    calls = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            (tmp_path / "gc_m15.url").write_text("https://a.example/m15\n", encoding="utf-8")
            (tmp_path / "gc_m5.url").write_text("https://a.example/m5\n", encoding="utf-8")

    with patch("automation_tool.images.time.sleep", side_effect=fake_sleep):
        wait_for_gc_manual_urls(tmp_path, poll_seconds=0.01)
    assert calls["n"] == 1


def test_list_invalid_gc_mode_tv_only_no_coinmap(tmp_path: Path) -> None:
    write_main_chart_symbol_marker(tmp_path, "XAUUSD")
    write_gc_mode_marker(tmp_path)
    stamp = "20260101_120000"
    bad = list_invalid_chart_slots_for_stamp(
        tmp_path, stamp, include_gc_url_slots=False
    )
    assert len(bad) == 8
    assert all(x.source == "tradingview" for x in bad)
    assert all(x.source != "coinmap" for x in bad)


def test_list_invalid_gc_urls_empty(tmp_path: Path) -> None:
    write_main_chart_symbol_marker(tmp_path, "XAUUSD")
    write_gc_mode_marker(tmp_path)
    stamp = "20260101_120000"
    ensure_gc_manual_url_placeholders(tmp_path)
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
        p = tmp_path / f"{stamp}_tradingview_{sym}_{iv}.url"
        p.write_text("https://example.invalid/x\n", encoding="utf-8")
    bad = list_invalid_chart_slots_for_stamp(tmp_path, stamp)
    assert len(bad) == 2
    assert all(x.source == "gc_url" for x in bad)


def test_list_invalid_gc_urls_ok(tmp_path: Path) -> None:
    write_main_chart_symbol_marker(tmp_path, "XAUUSD")
    write_gc_mode_marker(tmp_path)
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
        p = tmp_path / f"{stamp}_tradingview_{sym}_{iv}.url"
        p.write_text("https://example.invalid/x\n", encoding="utf-8")
    (tmp_path / "gc_m15.url").write_text("https://gc.example/m15\n", encoding="utf-8")
    (tmp_path / "gc_m5.url").write_text("https://gc.example/m5\n", encoding="utf-8")
    bad = list_invalid_chart_slots_for_stamp(tmp_path, stamp)
    assert bad == []
