from __future__ import annotations

from automation_tool.gocharting_ws_decode import (
    footprint_ws_enabled,
    footprint_ws_interval_specs,
    footprint_ws_max_candles,
)


def test_footprint_ws_enabled() -> None:
    assert footprint_ws_enabled({"footprint_ws": {"enabled": True}}) is True
    assert footprint_ws_enabled({"footprint_ws": {"enabled": False}}) is False
    assert footprint_ws_enabled({}) is False


def test_footprint_ws_max_candles_from_cfg() -> None:
    cfg = {"footprint_ws": {"max_candles": 50}}
    assert footprint_ws_max_candles(cfg) == 50


def test_footprint_ws_interval_specs() -> None:
    cfg = {
        "footprint_screenshot": {
            "intervals": {
                "5m": {"page_url": "https://gocharting.com/terminal/chart/GC435uijM"},
                "15m": {"page_url": "https://gocharting.com/terminal/chart/S0kcqfQKt"},
            }
        }
    }
    specs = footprint_ws_interval_specs(cfg)
    assert specs == [
        ("15m", "https://gocharting.com/terminal/chart/S0kcqfQKt"),
        ("5m", "https://gocharting.com/terminal/chart/GC435uijM"),
    ]
