"""Tests for reload-before-screenshot chart refresh."""

from __future__ import annotations

from unittest.mock import MagicMock, call

from automation_tool.coinmap import _coinmap_reload_chart_page_before_screenshot


def test_reload_chart_page_before_screenshot_only_refreshes(monkeypatch) -> None:
    page = MagicMock()
    cd = {"reload_before_screenshot_settle_ms": 1500}
    calls: list[str] = []

    monkeypatch.setattr(
        "automation_tool.coinmap._maybe_dismiss_coinmap_symbol_search_modal",
        lambda *a, **k: calls.append("dismiss_modal"),
    )
    monkeypatch.setattr(
        "automation_tool.coinmap._maybe_switch_to_dark_mode",
        lambda *a, **k: calls.append("dark_mode"),
    )
    monkeypatch.setattr(
        "automation_tool.coinmap._maybe_dismiss_light_theme_modal",
        lambda *a, **k: calls.append("dismiss_light"),
    )

    _coinmap_reload_chart_page_before_screenshot(page, cd, settle_ms=2000)

    page.reload.assert_called_once_with(wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout.assert_called_once_with(1500)
    assert calls == ["dismiss_modal", "dark_mode", "dismiss_light", "dismiss_modal"]
