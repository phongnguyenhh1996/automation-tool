from __future__ import annotations

from automation_tool.cli import (
    _intraday_tradingview_interval_specs,
    _intraday_tradingview_openai_slugs,
)


def test_intraday_tradingview_specs_update_default() -> None:
    specs = _intraday_tradingview_interval_specs(include_m15_regular=False)
    assert [s["slug"] for s in specs] == ["15m_ict", "5m"]
    assert specs[0]["indicator_profile"] == "ict_killzones"


def test_intraday_tradingview_specs_optional_plain_m15() -> None:
    specs = _intraday_tradingview_interval_specs(include_m15_regular=True)
    assert [s["slug"] for s in specs] == ["15m", "15m_ict", "5m"]
    assert "indicator_profile" not in specs[0]
    assert specs[1]["indicator_profile"] == "ict_killzones"


def test_intraday_tradingview_openai_slugs() -> None:
    assert _intraday_tradingview_openai_slugs(include_m15_regular=False) == ("15m_ict", "5m")
    assert _intraday_tradingview_openai_slugs(include_m15_regular=True) == (
        "15m",
        "15m_ict",
        "5m",
    )
