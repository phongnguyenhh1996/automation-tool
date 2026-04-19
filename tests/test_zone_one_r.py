"""Tests for zone 1R (risk multiple) price helpers used by the watchlist daemon."""

from __future__ import annotations

from dataclasses import replace

import pytest

from automation_tool.mt5_openai_parse import ParsedTrade
from automation_tool.zone_one_r import (
    one_r_favorable_price,
    one_r_reached,
    risk_price_distance,
)


def _buy_limit() -> ParsedTrade:
    return ParsedTrade(
        symbol="XAUUSD",
        side="BUY",
        kind="LIMIT",
        price=2650.0,
        sl=2640.0,
        tp1=2680.0,
        tp2=None,
        lot=0.04,
        raw_line="",
    )


def _sell_limit() -> ParsedTrade:
    return ParsedTrade(
        symbol="XAUUSD",
        side="SELL",
        kind="LIMIT",
        price=2650.0,
        sl=2660.0,
        tp1=2620.0,
        tp2=None,
        lot=0.04,
        raw_line="",
    )


def test_risk_distance_buy() -> None:
    p = _buy_limit()
    assert risk_price_distance(p) == pytest.approx(10.0)
    assert one_r_favorable_price(p) == pytest.approx(2660.0)


def test_one_r_reached_buy() -> None:
    p = _buy_limit()
    assert not one_r_reached(p, 2659.0, eps=0.01)
    assert one_r_reached(p, 2660.0, eps=0.01)
    assert one_r_reached(p, 2665.0, eps=0.01)


def test_one_r_reached_sell() -> None:
    p = _sell_limit()
    assert not one_r_reached(p, 2641.0, eps=0.01)
    assert one_r_reached(p, 2640.0, eps=0.01)
    assert one_r_reached(p, 2635.0, eps=0.01)


def test_one_r_zero_risk_not_reached() -> None:
    p = replace(_buy_limit(), sl=2650.0)
    assert risk_price_distance(p) == 0.0
    assert not one_r_reached(p, 3000.0, eps=0.01)
