from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from automation_tool.gocharting_capture import (
    _apply_detail_chart_browser_zoom,
    _capture_gocharting_in_context,
    _chart_load_ms,
    _detail_chart_browser_zoom_percent,
    _detail_chart_cfg_for_interval,
    _detail_chart_viewport,
    _gocharting_tick_search_already_set,
    _pan_detail_chart,
    _prepare_detail_chart,
    _prepare_overview_chart,
    _select_chart_symbol,
    _symbol_uses_dedicated_chart,
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
        },
        "chart_load_ms": 0,
        "detail_chart": {
            "page_url": "https://gocharting.com/terminal/chart/orlk0N-Da",
            "refresh_button_id": "refresh-button",
            "zoom_in_button_id": "zoomIn-button",
            "zoom_clicks": 2,
            "zoom_click_delay_ms": 500,
            "chart_root_id": "chart-root-0",
            "pan_start_x_ratio": 0.1,
            "pan_end_x_ratio": 0.9,
            "pan_y_ratio": 0.5,
            "history_steps": 3,
        },
        "capture_plan": [{"symbol": "XAUUSD", "intervals": ["15m"]}],
        "symbols": {
            "XAUUSD": {
                "export_label": "GC",
                "search_query": "GC1!",
            }
        },
        "symbol_search": {
            "input_id": "input-search-ticks-input",
            "results_id": "search-results",
        },
        "interval_panel": {"button_selector": 'button:has(div:text-is("{interval}"))'},
        "screenshot": {"open_button": "#user-screenshot-btn"},
        "csv_export": {"button_selector": "button.csv"},
        "viewport_width": 1440,
        "viewport_height": 810,
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
            }
        },
    )
    assert clicks == ["refresh-button", "zoomOut-button", "zoomOut-button"]


def test_chart_load_ms_global_and_section_override() -> None:
    cfg = {"chart_load_ms": 2000, "overview": {"chart_load_ms": 1500}}
    assert _chart_load_ms(cfg) == 2000
    assert _chart_load_ms(cfg, section="overview") == 1500
    assert _chart_load_ms({}) == 2000


@pytest.mark.parametrize(
    ("current", "query", "export_label", "expected"),
    [
        ("DXY", "EXNESS:DXY", "DXY", True),
        ("EXNESS:DXY", "EXNESS:DXY", "DXY", True),
        ("GC1!", "GC1!", "GC", True),
        ("EURUSD", "EXNESS:DXY", "DXY", False),
        ("", "GC1!", "GC", False),
        ("  dxy  ", "EXNESS:DXY", "DXY", True),
    ],
)
def test_gocharting_tick_search_already_set(
    current: str,
    query: str,
    export_label: str,
    expected: bool,
) -> None:
    assert (
        _gocharting_tick_search_already_set(
            current, query=query, export_label=export_label
        )
        is expected
    )


def test_select_chart_symbol_skips_when_input_already_matches(monkeypatch) -> None:
    page = MagicMock()
    search_input = MagicMock()
    search_input.input_value.return_value = "DXY"

    def fake_id_locator(p, element_id):
        assert element_id == "input-search-ticks-input"
        loc = MagicMock()
        loc.first = search_input
        return loc

    monkeypatch.setattr(
        "automation_tool.gocharting_capture._id_locator",
        fake_id_locator,
    )
    _select_chart_symbol(
        page,
        {
            "symbol_search": {
                "input_id": "input-search-ticks-input",
                "query_template": "EXNESS:{symbol}",
            }
        },
        {"export_label": "DXY"},
    )
    search_input.click.assert_not_called()


def test_select_chart_symbol_searches_when_input_differs(monkeypatch) -> None:
    page = MagicMock()
    search_input = MagicMock()
    search_input.input_value.return_value = "EURUSD"
    results_first = MagicMock()

    def fake_id_locator(p, element_id):
        if element_id == "input-search-ticks-input":
            loc = MagicMock()
            loc.first = search_input
            return loc
        if element_id == "search-results":
            loc = MagicMock()
            loc.locator.return_value.first = results_first
            return loc
        raise AssertionError(element_id)

    monkeypatch.setattr(
        "automation_tool.gocharting_capture._id_locator",
        fake_id_locator,
    )
    _select_chart_symbol(
        page,
        {
            "symbol_search": {
                "input_id": "input-search-ticks-input",
                "results_id": "search-results",
                "query_template": "EXNESS:{symbol}",
                "type_delay_ms": 0,
                "settle_ms": 0,
            }
        },
        {"export_label": "DXY"},
    )
    search_input.click.assert_called_once()
    search_input.fill.assert_called_once_with("EXNESS:DXY")
    results_first.click.assert_called_once()


def test_pan_detail_chart_drags_within_chart_root(monkeypatch) -> None:
    page = MagicMock()
    chart = MagicMock()
    chart.bounding_box.return_value = {"x": 100.0, "y": 50.0, "width": 1000.0, "height": 400.0}
    moves: list[tuple[float, float]] = []

    def fake_id_locator(p, element_id):
        assert element_id == "chart-root-0"
        loc = MagicMock()
        loc.first = chart
        return loc

    monkeypatch.setattr(
        "automation_tool.gocharting_capture._id_locator",
        fake_id_locator,
    )
    page.mouse.move.side_effect = lambda x, y, **kwargs: moves.append((x, y))

    _pan_detail_chart(
        page,
        {
            "detail_chart": {
                "chart_root_id": "chart-root-0",
                "pan_start_x_ratio": 0.1,
                "pan_end_x_ratio": 0.9,
                "pan_y_ratio": 0.5,
                "pan_drag_steps": 8,
            }
        },
    )

    assert moves[0] == (200.0, 250.0)
    page.mouse.down.assert_called_once()
    page.mouse.move.assert_called_with(1000.0, 250.0, steps=8)
    page.mouse.up.assert_called_once()


def test_detail_chart_viewport_defaults_to_double_session_width() -> None:
    assert _detail_chart_viewport({"viewport_width": 1440, "viewport_height": 810}) == (2880, 810)


def test_detail_chart_viewport_honors_detail_overrides() -> None:
    cfg = {
        "viewport_width": 1440,
        "viewport_height": 810,
        "detail_chart": {"viewport_width": 3200, "viewport_height": 900},
    }
    assert _detail_chart_viewport(cfg) == (3200, 900)


def test_detail_chart_cfg_for_interval_merges_by_interval() -> None:
    cfg = {
        "detail_chart": {
            "page_url": "https://example.com/detail",
            "zoom_clicks": 2,
            "history_steps": 2,
            "viewport_width": 4000,
            "viewport_height": 6000,
            "pan_start_x_ratio": 0.2,
            "by_interval": {
                "5m": {
                    "zoom_clicks": 3,
                    "viewport_height": 5500,
                    "pan_start_x_ratio": 0.15,
                },
                "15m": {
                    "zoom_clicks": 2,
                    "history_steps": 3,
                },
            },
        }
    }
    m5 = _detail_chart_cfg_for_interval(cfg, "5m")
    assert m5["zoom_clicks"] == 3
    assert m5["viewport_height"] == 5500
    assert m5["pan_start_x_ratio"] == 0.15
    assert m5["viewport_width"] == 4000
    assert "by_interval" not in m5

    m15 = _detail_chart_cfg_for_interval(cfg, "15m")
    assert m15["zoom_clicks"] == 2
    assert m15["history_steps"] == 3
    assert m15["viewport_height"] == 6000


def test_detail_chart_viewport_uses_interval_overrides() -> None:
    cfg = {
        "viewport_width": 1440,
        "viewport_height": 810,
        "detail_chart": {
            "viewport_width": 4000,
            "viewport_height": 6000,
            "by_interval": {
                "5m": {"viewport_height": 5500},
            },
        },
    }
    assert _detail_chart_viewport(cfg, "15m") == (4000, 6000)
    assert _detail_chart_viewport(cfg, "5m") == (4000, 5500)


def test_detail_chart_browser_zoom_percent_defaults_to_125() -> None:
    assert _detail_chart_browser_zoom_percent({}) == 125
    assert _detail_chart_browser_zoom_percent({"detail_chart": {}}) == 125


def test_detail_chart_browser_zoom_percent_honors_overrides() -> None:
    cfg = {
        "detail_chart": {
            "browser_zoom_percent": 150,
            "by_interval": {"5m": {"browser_zoom_percent": 110}},
        }
    }
    assert _detail_chart_browser_zoom_percent(cfg) == 150
    assert _detail_chart_browser_zoom_percent(cfg, "5m") == 110
    assert _detail_chart_browser_zoom_percent(cfg, "15m") == 150


def test_apply_detail_chart_browser_zoom_uses_cdp() -> None:
    page = MagicMock()
    cdp = MagicMock()
    page.context.new_cdp_session.return_value = cdp

    _apply_detail_chart_browser_zoom(
        page,
        {"detail_chart": {"browser_zoom_percent": 125}},
    )

    page.context.new_cdp_session.assert_called_once_with(page)
    cdp.send.assert_called_once_with(
        "Emulation.setPageScaleFactor",
        {"pageScaleFactor": 1.25},
    )
    page.evaluate.assert_not_called()


def test_apply_detail_chart_browser_zoom_skips_at_100_percent() -> None:
    page = MagicMock()
    _apply_detail_chart_browser_zoom(
        page,
        {"detail_chart": {"browser_zoom_percent": 100}},
    )
    page.context.new_cdp_session.assert_not_called()
    page.evaluate.assert_not_called()


def test_prepare_detail_chart_browser_zoom_refresh_and_zoom_in(monkeypatch) -> None:
    page = MagicMock()
    calls: list[str] = []

    def fake_browser_zoom(p, cfg, *, interval=None):
        calls.append("browser_zoom")

    def fake_force_click_id(p, element_id, *, delay_ms=0):
        calls.append(element_id)

    monkeypatch.setattr(
        "automation_tool.gocharting_capture._apply_detail_chart_browser_zoom",
        fake_browser_zoom,
    )
    monkeypatch.setattr(
        "automation_tool.gocharting_capture._force_click_id",
        fake_force_click_id,
    )
    _prepare_detail_chart(
        page,
        {
            "detail_chart": {
                "browser_zoom_percent": 125,
                "refresh_button_id": "refresh-button",
                "zoom_in_button_id": "zoomIn-button",
                "zoom_clicks": 2,
                "zoom_click_delay_ms": 10,
            }
        },
    )
    assert calls == [
        "browser_zoom",
        "refresh-button",
        "zoomIn-button",
        "zoomIn-button",
    ]


def test_capture_gocharting_in_context_overview_then_detail(monkeypatch, tmp_path: Path, gc_cfg: dict) -> None:
    context = MagicMock()
    main_page = MagicMock()
    detail_page = MagicMock()
    context.new_page.side_effect = [main_page, detail_page]

    overview_calls: list[str] = []
    detail_prepare_calls: list[MagicMock] = []
    detail_zoom_saved: list[Path] = []

    def fake_prepare_detail(page, cfg):
        detail_prepare_calls.append(page)

    monkeypatch.setattr(
        "automation_tool.gocharting_capture._prepare_detail_chart",
        fake_prepare_detail,
    )
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

    def fake_png(page, cfg, dest, **kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"png")
        if "detail_zoom" in dest.name:
            detail_zoom_saved.append(dest)

    monkeypatch.setattr("automation_tool.gocharting_capture._capture_png", fake_png)

    csv_calls: list[Path] = []

    def fake_csv(page, cfg, dest, **kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("h\n1", encoding="utf-8")
        csv_calls.append(dest)

    monkeypatch.setattr("automation_tool.gocharting_capture._capture_csv", fake_csv)

    pan_calls = 0

    def fake_pan_back(page, cfg, *, dest):
        nonlocal pan_calls
        pan_calls += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"png")

    monkeypatch.setattr(
        "automation_tool.gocharting_capture._pan_detail_and_capture_back",
        fake_pan_back,
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
    assert pan_calls == 3
    zoom = gocharting_detail_png_path(tmp_path, stamp, "GC", "15m", "zoom")
    assert zoom in paths
    assert detail_zoom_saved == [zoom]
    detail_page.set_viewport_size.assert_called_once_with({"width": 2880, "height": 810})
    assert detail_prepare_calls == [detail_page]
    main_page.close.assert_called_once()
    detail_page.close.assert_called_once()
    assert context.new_page.call_count == 2


def test_capture_gocharting_in_context_detail_zoom_only_when_history_zero(
    monkeypatch, tmp_path: Path, gc_cfg: dict
) -> None:
    context = MagicMock()
    main_page = MagicMock()
    detail_page = MagicMock()
    context.new_page.side_effect = [main_page, detail_page]

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
    monkeypatch.setattr(
        "automation_tool.gocharting_capture._prepare_overview_chart",
        lambda *a, **k: None,
    )

    def fake_png(page, cfg, dest, **kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"png")

    monkeypatch.setattr("automation_tool.gocharting_capture._capture_png", fake_png)

    def fake_csv(page, cfg, dest, **kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("h\n1", encoding="utf-8")

    monkeypatch.setattr("automation_tool.gocharting_capture._capture_csv", fake_csv)

    pan_calls = 0

    def fake_pan_back(page, cfg, *, dest):
        nonlocal pan_calls
        pan_calls += 1

    monkeypatch.setattr(
        "automation_tool.gocharting_capture._pan_detail_and_capture_back",
        fake_pan_back,
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
        detail_history_steps=0,
    )

    assert pan_calls == 0
    zoom = gocharting_detail_png_path(tmp_path, stamp, "GC", "15m", "zoom")
    assert zoom in paths
    assert not any("_detail_back_" in p.name for p in paths)


def test_capture_gocharting_in_context_overview_capture_false_detail_only(
    monkeypatch, tmp_path: Path, gc_cfg: dict
) -> None:
    context = MagicMock()
    overview_png = MagicMock()
    overview_csv = MagicMock()
    monkeypatch.setattr(
        "automation_tool.gocharting_capture._capture_png",
        overview_png,
    )
    monkeypatch.setattr(
        "automation_tool.gocharting_capture._capture_csv",
        overview_csv,
    )

    detail_paths: list[Path] = []

    def fake_detail(*args, **kwargs):
        stamp = str(kwargs["stamp"])
        p = gocharting_detail_png_path(tmp_path, stamp, "GC", "5m", "zoom")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"png")
        detail_paths.append(p)
        return [p]

    monkeypatch.setattr(
        "automation_tool.gocharting_capture._capture_detail_footprint",
        fake_detail,
    )

    stamp = "20260617_130000"
    cfg = dict(gc_cfg)
    cfg["capture_plan"] = [{"symbol": "XAUUSD", "intervals": ["15m", "5m"]}]
    paths = _capture_gocharting_in_context(
        context,
        cfg,
        charts_dir=tmp_path,
        email="a@b.com",
        password="pw",
        stamp=stamp,
        main_chart_symbol=None,
        capture_symbols=("XAUUSD",),
        capture_intervals=("5m",),
        only_slots=[("GC", "5m")],
        detail_history_steps=0,
        overview_capture=False,
    )

    overview_png.assert_not_called()
    overview_csv.assert_not_called()
    assert len(paths) == 1
    assert paths[0].name.endswith("_gocharting_GC_5m_detail_zoom.png")


def test_capture_gocharting_in_context_skips_detail_when_symbol_disabled(
    monkeypatch, tmp_path: Path, gc_cfg: dict
) -> None:
    gc_cfg["capture_plan"] = [{"symbol": "DXY", "intervals": ["15m"]}]
    gc_cfg["symbols"] = {"DXY": {"export_label": "DXY", "detail_chart": False}}

    context = MagicMock()
    main_page = MagicMock()
    context.new_page.return_value = main_page

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
    monkeypatch.setattr(
        "automation_tool.gocharting_capture._prepare_overview_chart",
        lambda *a, **k: None,
    )

    def fake_png(page, cfg, dest, **kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"png")

    monkeypatch.setattr("automation_tool.gocharting_capture._capture_png", fake_png)

    def fake_csv(page, cfg, dest, **kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("h\n1", encoding="utf-8")

    monkeypatch.setattr("automation_tool.gocharting_capture._capture_csv", fake_csv)

    detail_called = False

    def fake_detail(*a, **k):
        nonlocal detail_called
        detail_called = True
        return []

    monkeypatch.setattr(
        "automation_tool.gocharting_capture._capture_detail_footprint",
        fake_detail,
    )

    paths = _capture_gocharting_in_context(
        context,
        gc_cfg,
        charts_dir=tmp_path,
        email="a@b.com",
        password="pw",
        stamp="20260617_120000",
        main_chart_symbol=None,
        capture_symbols=None,
        capture_intervals=None,
    )

    assert not detail_called
    assert len(paths) == 2
    assert context.new_page.call_count == 1


def test_symbol_uses_dedicated_chart() -> None:
    assert _symbol_uses_dedicated_chart(
        {"chart_page_url": "https://gocharting.com/terminal/chart/9mK0iRZGS"}
    )
    assert not _symbol_uses_dedicated_chart({"export_label": "DXY"})


def test_capture_gocharting_in_context_dxy_skips_symbol_search_with_dedicated_url(
    monkeypatch, tmp_path: Path, gc_cfg: dict
) -> None:
    gc_cfg["capture_plan"] = [
        {"symbol": "DXY", "intervals": ["15m"]},
        {"symbol": "XAUUSD", "intervals": ["15m"]},
    ]
    gc_cfg["symbols"] = {
        "DXY": {
            "export_label": "DXY",
            "detail_chart": False,
            "chart_page_url": "https://gocharting.com/terminal/chart/9mK0iRZGS",
        },
        "XAUUSD": {
            "export_label": "GC",
            "search_query": "GC1!",
        },
    }

    context = MagicMock()
    main_page = MagicMock()
    detail_page = MagicMock()
    context.new_page.side_effect = [main_page, detail_page]

    goto_urls: list[str] = []
    main_page.goto.side_effect = lambda url, **kwargs: goto_urls.append(url)

    select_calls: list[str] = []

    def fake_select(page, cfg, entry):
        select_calls.append(str(entry["export_label"]))

    monkeypatch.setattr(
        "automation_tool.gocharting_capture._maybe_login_gocharting",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "automation_tool.gocharting_capture._select_chart_symbol",
        fake_select,
    )
    monkeypatch.setattr(
        "automation_tool.gocharting_capture._select_interval",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "automation_tool.gocharting_capture._prepare_overview_chart",
        lambda *a, **k: None,
    )

    def fake_png(page, cfg, dest, **kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"png")

    monkeypatch.setattr("automation_tool.gocharting_capture._capture_png", fake_png)

    def fake_csv(page, cfg, dest, **kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("h\n1", encoding="utf-8")

    monkeypatch.setattr("automation_tool.gocharting_capture._capture_csv", fake_csv)
    monkeypatch.setattr(
        "automation_tool.gocharting_capture._capture_detail_footprint",
        lambda *a, **k: [],
    )

    _capture_gocharting_in_context(
        context,
        gc_cfg,
        charts_dir=tmp_path,
        email="a@b.com",
        password="pw",
        stamp="20260617_120000",
        main_chart_symbol=None,
        capture_symbols=None,
        capture_intervals=None,
    )

    assert goto_urls == [
        "https://gocharting.com/terminal/chart/9mK0iRZGS",
        "https://gocharting.com/terminal/chart/OpJogbwTR",
    ]
    assert select_calls == ["GC"]
