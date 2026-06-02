from __future__ import annotations

from automation_tool.coinmap import (
    _tradingview_indicator_loading_markers,
    _tradingview_texts_have_indicator_loading,
    _wait_tradingview_indicators_loaded,
)


def test_indicator_loading_markers_default_vietnamese_and_english() -> None:
    assert _tradingview_indicator_loading_markers({}) == ["đang tải", "loading..."]


def test_indicator_loading_markers_custom_from_config() -> None:
    tv = {"indicator_loading_texts": ["ĐANG TẢI", "  ", "calculating"]}
    assert _tradingview_indicator_loading_markers(tv) == ["đang tải", "calculating"]


def test_texts_have_indicator_loading_detects_vietnamese_legend() -> None:
    tv: dict = {}
    texts = [
        "LuxAlgo - Smart Money Concepts (Historical, Colored, All, All, tiny, All, All, small, 50, 5, 5, Atr, High/Low, 3, 0.1, tiny, , 1, —, —, —) đang tải..."
    ]
    assert _tradingview_texts_have_indicator_loading(texts, tv) is True


def test_texts_have_indicator_loading_false_when_ready() -> None:
    tv: dict = {}
    texts = ["LuxAlgo - Smart Money Concepts", "VSA Volume"]
    assert _tradingview_texts_have_indicator_loading(texts, tv) is False


class _FakePage:
    def __init__(self, legend_texts: list[list[str]]) -> None:
        self._legend_texts = legend_texts
        self.timeouts: list[int] = []

    def wait_for_timeout(self, ms: int) -> None:
        self.timeouts.append(ms)


def test_wait_for_indicators_loaded_polls_until_loading_gone(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_legend(_page, _tv):
        calls["n"] += 1
        if calls["n"] < 3:
            return ["LuxAlgo đang tải..."]
        return ["LuxAlgo - Smart Money Concepts"]

    monkeypatch.setattr(
        "automation_tool.coinmap._tradingview_list_legend_item_texts",
        fake_legend,
    )
    page = _FakePage([])
    tv = {"indicator_loading_poll_ms": 10, "indicator_loading_settle_ms": 20}
    _wait_tradingview_indicators_loaded(page, tv)
    assert calls["n"] == 3
    assert page.timeouts == [10, 10, 20]


def test_wait_for_indicators_loaded_skipped_when_disabled() -> None:
    page = _FakePage([])
    tv = {"indicator_loading_wait_disabled": True}
    _wait_tradingview_indicators_loaded(page, tv)
    assert page.timeouts == []
