from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from automation_tool import coinmap


class _FakeKeyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    def press(self, key: str) -> None:
        self.pressed.append(key)


class _FakePage:
    def __init__(self) -> None:
        self.keyboard = _FakeKeyboard()
        self.timeouts: list[int] = []
        self.locator = MagicMock()

    def wait_for_timeout(self, ms: int) -> None:
        self.timeouts.append(ms)


def test_tradingview_symbol_locator_prefers_custom_selector() -> None:
    page = _FakePage()
    tv = {"symbol_list_item_selector": '[data-test="{symbol}"]'}
    coinmap._tradingview_symbol_locator(page, tv, "DXY")
    page.locator.assert_called_once_with('[data-test="DXY"]')


def test_tradingview_symbol_locator_uses_watchlist_row_when_no_custom() -> None:
    page = _FakePage()
    tv = {"watchlist_row_selector": '[data-symbol-short="{symbol}"]'}
    coinmap._tradingview_symbol_locator(page, tv, "DXY")
    page.locator.assert_called_once_with('[data-symbol-short="DXY"]')


def test_tradingview_symbol_locator_falls_back_to_symbol_name_prefix() -> None:
    page = _FakePage()
    tv = {"symbol_name_class_prefix": "symbolNameText-"}
    loc = coinmap._tradingview_symbol_locator(page, tv, "DXY")
    page.locator.assert_called_once_with('[class*="symbolNameText-"]')
    assert loc is page.locator.return_value.get_by_text.return_value.first


def test_maybe_dismiss_tradingview_blocking_overlay_escape_only() -> None:
    page = _FakePage()
    coinmap._maybe_dismiss_tradingview_blocking_overlay(page, {})
    assert page.keyboard.pressed == ["Escape"]
    assert page.timeouts == [250]


def test_maybe_dismiss_tradingview_blocking_overlay_disabled() -> None:
    page = _FakePage()
    coinmap._maybe_dismiss_tradingview_blocking_overlay(
        page, {"tradingview_blocking_overlay_dismiss_enabled": False}
    )
    assert page.keyboard.pressed == []


def test_tradingview_select_symbol_retries_with_force(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _FakePage()
    loc = MagicMock()
    loc.click.side_effect = [RuntimeError("intercept"), None]
    monkeypatch.setattr(coinmap, "_tradingview_symbol_locator", lambda _p, _tv, _s: loc)
    dismiss_calls: list[int] = []

    def _dismiss(_page, _tv) -> None:
        dismiss_calls.append(1)

    monkeypatch.setattr(coinmap, "_maybe_dismiss_tradingview_blocking_overlay", _dismiss)
    coinmap._tradingview_select_symbol(page, {}, "DXY")
    assert dismiss_calls == [1, 1]
    assert loc.click.call_count == 2
    assert loc.click.call_args_list[0].kwargs.get("force") is True
    assert loc.click.call_args_list[1].kwargs.get("force") is True
