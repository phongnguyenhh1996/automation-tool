from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from automation_tool.gocharting_capture import (
    _capture_gocharting_in_context,
    _prepare_overview_chart,
    gocharting_detail_png_path,
)


@pytest.fixture
def gc_cfg() -> dict:
    return {
        "chart_page_url": "https://gocharting.com/terminal/chart/OpJogbwTR",
        "overview": {
            "refresh_button_id": "refresh-button",
            "zoom_out_button_id": "zoomOut-button",
            "zoom_out_clicks": 2,
            "zoom_click_delay_ms": 500,
            "settle_ms": 100,
        },
        "detail_chart": {
            "page_url": "https://gocharting.com/terminal/chart/orlk0N-Da",
            "refresh_button_id": "refresh-button",
            "zoom_in_button_id": "zoomIn-button",
            "zoom_clicks": 4,
            "zoom_click_delay_ms": 500,
            "go_to_date_button_id": "go-to-date-btn",
            "apply_button_selector": 'button:has-text("Apply")',
            "settle_ms": 100,
            "hours_back": {"5m": 2.5, "15m": 7},
            "history_steps": 3,
        },
        "capture_plan": [{"symbol": "DXY", "intervals": ["15m"]}],
        "symbols": {"DXY": {"export_label": "DXY"}},
        "symbol_search": {
            "input_id": "input-search-ticks-input",
            "results_id": "search-results",
        },
        "interval_panel": {"button_selector": 'button:has(div:text-is("{interval}"))'},
        "screenshot": {"open_button": "#user-screenshot-btn"},
        "csv_export": {"button_selector": "button.csv"},
    }


def test_prepare_overview_chart_clicks_refresh_and_zoom_out(monkeypatch) -> None:
    page = MagicMock()
    clicks: list[str] = []

    def fake_force_click_id(p, element_id, *, delay_ms=0):
        assert p is page
        clicks.append(element_id)

    monkeypatch.setattr(
        "automation_tool.gocharting_capture._force_click_id",
        fake_force_click_id,
    )
    _prepare_overview_chart(
        page,
        {
            "overview": {
                "refresh_button_id": "refresh-button",
                "zoom_out_button_id": "zoomOut-button",
                "zoom_out_clicks": 2,
                "zoom_click_delay_ms": 10,
                "settle_ms": 50,
            }
        },
    )
    assert clicks == ["refresh-button", "zoomOut-button", "zoomOut-button"]
    page.wait_for_timeout.assert_called()


def test_capture_gocharting_in_context_overview_then_detail(monkeypatch, tmp_path: Path, gc_cfg: dict) -> None:
    context = MagicMock()
    main_page = MagicMock()
    detail_page = MagicMock()
    context.new_page.side_effect = [main_page, detail_page]

    overview_calls: list[str] = []
    detail_zoom_saved: list[Path] = []

    monkeypatch.setattr(
        "automation_tool.gocharting_capture._maybe_login_gocharting",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "automation_tool.gocharting_capture._select_chart_symbol",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "automation_tool.gocharting_capture._select_interval",
        lambda *a, **k: None,
    )

    def fake_prepare(page, cfg):
        overview_calls.append("overview")

    monkeypatch.setattr(
        "automation_tool.gocharting_capture._prepare_overview_chart",
        fake_prepare,
    )

    def fake_png(page, cfg, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"png")
        if "detail_zoom" in dest.name:
            detail_zoom_saved.append(dest)

    monkeypatch.setattr("automation_tool.gocharting_capture._capture_png", fake_png)

    csv_calls: list[Path] = []

    def fake_csv(page, cfg, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("h\n1", encoding="utf-8")
        csv_calls.append(dest)

    monkeypatch.setattr("automation_tool.gocharting_capture._capture_csv", fake_csv)

    go_date_calls = 0

    def fake_go_back(page, cfg, *, dest, baseline, hours_back):
        nonlocal go_date_calls
        go_date_calls += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"png")
        return baseline if baseline is not None else (12, 0, "pm")

    monkeypatch.setattr(
        "automation_tool.gocharting_capture._go_to_date_and_capture_back",
        fake_go_back,
    )

    stamp = "20260617_120000"
    paths = _capture_gocharting_in_context(
        context,
        gc_cfg,
        charts_dir=tmp_path,
        email="a@b.com",
        password="pw",
        stamp=stamp,
        main_chart_symbol=None,
        capture_symbols=None,
        capture_intervals=None,
    )

    assert overview_calls == ["overview"]
    assert len(csv_calls) == 1
    assert go_date_calls == 3
    zoom = gocharting_detail_png_path(tmp_path, stamp, "DXY", "15m", "zoom")
    assert zoom in paths
    assert detail_zoom_saved == [zoom]
    main_page.close.assert_called_once()
    detail_page.close.assert_called_once()
    assert context.new_page.call_count == 2
