from __future__ import annotations

from pathlib import Path

import pytest

from automation_tool.coinmap import apply_main_chart_symbol_to_config, load_coinmap_yaml
from automation_tool.config import default_coinmap_config_path
from automation_tool.images import (
    chart_image_order_for_main_symbol,
    clear_main_chart_symbol_marker,
    coinmap_main_pair_5m_json_path,
    effective_chart_image_order,
    normalize_main_chart_symbol,
    read_main_chart_symbol,
    write_main_chart_symbol_marker,
)


def test_normalize_main_chart_symbol() -> None:
    assert normalize_main_chart_symbol("  usdjpy  ") == "USDJPY"
    with pytest.raises(ValueError):
        normalize_main_chart_symbol("AB")


def test_apply_main_chart_symbol_to_config() -> None:
    cfg = load_coinmap_yaml(default_coinmap_config_path())
    out = apply_main_chart_symbol_to_config(cfg, "USDJPY")
    cd = out["chart_download"]["capture_plan"]
    assert any(
        isinstance(r, dict) and r.get("symbol") == "USDJPY" for r in cd
    )
    assert not any(
        isinstance(r, dict) and r.get("symbol") == "XAUUSD" for r in cd
    )
    tv = out["tradingview_capture"]
    assert "USDJPY" in (tv.get("chart_url") or "")
    tv_plan = tv.get("capture_plan") or []
    syms = [r.get("symbol") for r in tv_plan if isinstance(r, dict)]
    assert "USDJPY" in syms
    assert "XAUUSD" not in syms


def test_chart_image_order_for_main_symbol() -> None:
    o = chart_image_order_for_main_symbol("EURUSD")
    assert ("tradingview", "EURUSD", "5m") in o
    assert ("coinmap", "EURUSD", "5m") in o
    assert all("XAUUSD" not in x for x in o)


def test_marker_roundtrip(tmp_path: Path) -> None:
    clear_main_chart_symbol_marker(tmp_path)
    assert read_main_chart_symbol(tmp_path) == "XAUUSD"
    write_main_chart_symbol_marker(tmp_path, "USDJPY")
    assert read_main_chart_symbol(tmp_path) == "USDJPY"
    assert len(effective_chart_image_order(tmp_path)) == len(
        chart_image_order_for_main_symbol("USDJPY")
    )


def test_coinmap_main_pair_5m_json_path_uses_marker(tmp_path: Path) -> None:
    write_main_chart_symbol_marker(tmp_path, "USDJPY")
    p = tmp_path / "20260101_120000_coinmap_USDJPY_5m.json"
    p.write_text("{}", encoding="utf-8")
    got = coinmap_main_pair_5m_json_path(tmp_path)
    assert got == p
