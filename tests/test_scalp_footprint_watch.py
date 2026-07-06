"""Tests for scalp footprint watch scheduling."""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "scalp_footprint"))

from scheduling import (  # noqa: E402
    forming_candle_open,
    intervals_due,
    latest_closed_candle_open,
    next_close_trigger,
)


def test_forming_candle_open_5m() -> None:
    now = datetime(2026, 7, 3, 10, 7, 30)
    assert forming_candle_open(now, 5) == datetime(2026, 7, 3, 10, 5, 0)


def test_latest_closed_candle_open_5m() -> None:
    now = datetime(2026, 7, 3, 10, 7, 30)
    assert latest_closed_candle_open(now, 5) == datetime(2026, 7, 3, 10, 0, 0)


def test_intervals_due_after_buffer() -> None:
    # 10:06:30 — 90s after 10:05 close; 10:00-10:05 bar should be due for 5m
    now = datetime(2026, 7, 3, 10, 6, 30)
    due = intervals_due(now, buffer_sec=90, last_processed={})
    assert "5m" in due


def test_intervals_not_due_before_buffer() -> None:
    now = datetime(2026, 7, 3, 10, 1, 0)
    due = intervals_due(now, buffer_sec=90, last_processed={})
    assert due == []


def test_intervals_skip_already_processed() -> None:
    now = datetime(2026, 7, 3, 10, 6, 30)
    last = {"5m": latest_closed_candle_open(now, 5)}
    due = intervals_due(now, buffer_sec=90, last_processed=last)
    assert "5m" not in due


def test_next_close_trigger_future() -> None:
    now = datetime(2026, 7, 3, 10, 6, 0)
    nxt = next_close_trigger(now, 5, buffer_sec=90)
    assert nxt == datetime(2026, 7, 3, 10, 6, 30)


def test_15m_due_on_quarter_hour() -> None:
    now = datetime(2026, 7, 3, 10, 16, 30)
    due = intervals_due(now, buffer_sec=90, last_processed={})
    assert "15m" in due
    assert "5m" in due


def test_map_paths_by_interval_exact_match() -> None:
    from watch import _map_paths_by_interval

    charts = Path("data/XAUUSD/charts/footprint_images")
    paths = [
        charts / "footprint_combined_5m.json",
        charts / "footprint_combined_15m.json",
    ]
    mapped = _map_paths_by_interval(paths, ("5m", "15m"))
    assert mapped["5m"].name == "footprint_combined_5m.json"
    assert mapped["15m"].name == "footprint_combined_15m.json"
    assert mapped["5m"] != mapped["15m"]


def test_capture_footprint_headless_uses_native_gc_cfg(monkeypatch) -> None:
    from watch import capture_footprint_headless

    captured_cfg: dict = {}

    captured_kwargs: dict = {}

    def _fake_ws_plan(_ctx, cfg, **kwargs):
        captured_cfg.update(cfg)
        captured_kwargs.update(kwargs)
        return []

    yaml_path = Path("config/gocharting.yaml")
    monkeypatch.setattr(
        "automation_tool.gocharting_ws_capture.capture_footprint_ws_plan",
        _fake_ws_plan,
    )
    monkeypatch.setattr(
        "automation_tool.playwright_browser.launch_chrome_context",
        lambda *a, **k: (None, None),
    )
    monkeypatch.setattr(
        "automation_tool.playwright_browser.close_browser_and_context",
        lambda *a, **k: None,
    )
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: _FakePlaywright())

    capture_footprint_headless(
        intervals=("5m",),
        charts_dir=Path("data/XAUUSD/charts"),
        gocharting_yaml=yaml_path,
        headless=True,
    )
    assert captured_cfg["footprint_ws"]["gc_to_spot"]["enabled"] is False
    assert captured_cfg["footprint_ws"]["mt5_spot"] is False
    assert captured_cfg["footprint_ws"]["extra_session_days"] == 0
    assert captured_kwargs.get("extra_session_days") == 0


class _FakePlaywright:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
