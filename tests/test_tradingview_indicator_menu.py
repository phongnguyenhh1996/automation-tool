from typing import Optional

from automation_tool import coinmap
from automation_tool.coinmap import (
    _tradingview_is_delete_indicator_label,
    _tradingview_open_context_menu_and_clear_indicators,
)


class _FakeMouse:
    def __init__(self) -> None:
        self.clicks: list[tuple[float, float, Optional[str]]] = []

    def click(self, x: float, y: float, button: Optional[str] = None) -> None:
        self.clicks.append((x, y, button))


class _FakeKeyboard:
    def __init__(self) -> None:
        self.presses: list[str] = []

    def press(self, key: str) -> None:
        self.presses.append(key)


class _FakeMenuRow:
    def __init__(self, page: "_FakeMenuPage") -> None:
        self.page = page

    def click(self, timeout: int, force: bool = False) -> None:
        self.page.row_clicks += 1
        self.page.delete_clicks += 1


class _FakeMenuLabel:
    def __init__(self, page: "_FakeMenuPage", text: str) -> None:
        self.page = page
        self.text = text

    def inner_text(self, timeout: int) -> str:
        return self.text

    def wait_for(self, state: str, timeout: int) -> None:
        return None

    def click(self, timeout: int, force: bool = False) -> None:
        self.page.label_clicks += 1
        self.page.delete_clicks += 1

    def locator(self, selector: str) -> _FakeMenuRow:
        assert "ancestor" in selector
        return _FakeMenuRow(self.page)


class _FakeMenuLabels:
    def __init__(self, page: "_FakeMenuPage", labels: list[str]) -> None:
        self.page = page
        self.labels = labels

    def count(self) -> int:
        return len(self.labels)

    def nth(self, index: int) -> _FakeMenuLabel:
        return _FakeMenuLabel(self.page, self.labels[index])

    @property
    def first(self) -> _FakeMenuLabel:
        return self.nth(0)


class _FakeMenuPage:
    def __init__(self, labels: list[str]) -> None:
        self.labels = labels
        self.mouse = _FakeMouse()
        self.keyboard = _FakeKeyboard()
        self.delete_clicks = 0
        self.row_clicks = 0
        self.label_clicks = 0
        self.timeouts: list[int] = []
        self.viewport_size = {"width": 1600, "height": 900}

    def locator(self, selector: str, has_text: Optional[str] = None) -> _FakeMenuLabels:
        labels = self.labels
        if has_text is not None:
            labels = [x for x in labels if has_text in x]
        return _FakeMenuLabels(self, labels)

    def wait_for_timeout(self, ms: int) -> None:
        self.timeouts.append(ms)


def test_delete_indicator_label_accepts_chart_delete_actions():
    assert _tradingview_is_delete_indicator_label("Xóa 1 chỉ báo")
    assert _tradingview_is_delete_indicator_label("Xoá 2 chỉ báo")
    assert _tradingview_is_delete_indicator_label("Remove 1 indicator")


def test_delete_indicator_label_rejects_favorite_actions():
    assert not _tradingview_is_delete_indicator_label("Xóa chỉ báo này khỏi mục yêu thích")
    assert not _tradingview_is_delete_indicator_label("Remove this indicator from favorites")


def test_clear_indicators_clicks_menuitem_row_not_label(monkeypatch):
    page = _FakeMenuPage(["Xóa 2 chỉ báo"])
    monkeypatch.setattr(coinmap, "_tradingview_chart_center_xy", lambda page, tv: (800.0, 120.0))
    monkeypatch.setattr(coinmap, "_tradingview_list_legend_item_texts", lambda page, tv: [])

    _tradingview_open_context_menu_and_clear_indicators(page, {})

    assert page.row_clicks == 1
    assert page.label_clicks == 0


def test_clear_indicators_retries_until_legend_has_no_indicators(monkeypatch):
    page = _FakeMenuPage(["Xóa 1 chỉ báo"])
    monkeypatch.setattr(coinmap, "_tradingview_chart_center_xy", lambda page, tv: (800.0, 120.0))
    monkeypatch.setattr(
        coinmap,
        "_tradingview_list_legend_item_texts",
        lambda page, tv: ["remaining indicator"] if page.delete_clicks < 2 else [],
    )

    _tradingview_open_context_menu_and_clear_indicators(page, {})

    assert page.delete_clicks == 2

