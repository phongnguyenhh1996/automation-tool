"""Tests for first-response VÀO LỆNH label picking."""

from automation_tool.first_response_trade import plan_label_nearest_trade_entry
from automation_tool.mt5_openai_parse import ParsedTrade


def test_plan_label_nearest_limit_entry() -> None:
    p = ParsedTrade(
        symbol="XAUUSDm",
        side="SELL",
        kind="LIMIT",
        price=2650.0,
        sl=2655.0,
        tp1=2640.0,
        tp2=None,
        lot=0.02,
        raw_line="",
    )
    triple = (2650.0, 2660.0, 2640.0)
    assert plan_label_nearest_trade_entry(p, triple) == "plan_chinh"


def test_plan_label_nearest_market_uses_midpoint() -> None:
    p = ParsedTrade(
        symbol="XAUUSDm",
        side="BUY",
        kind="MARKET",
        price=None,
        sl=2640.0,
        tp1=2660.0,
        tp2=None,
        lot=0.01,
        raw_line="",
    )
    triple = (2645.0, 2650.0, 2655.0)
    # mid (sl+tp1)/2 = 2650 → nearest plan_phu
    assert plan_label_nearest_trade_entry(p, triple) == "plan_phu"
