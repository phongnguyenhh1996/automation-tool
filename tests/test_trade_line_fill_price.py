from automation_tool.mt5_openai_parse import inject_filled_price_into_trade_line


def test_inject_filled_price_into_market_trade_line_preserves_precision_from_sl_tp1() -> None:
    tl = "BUY MARKET | SL 3363.5 | TP1 3354.50 | Lot 0.02"
    out = inject_filled_price_into_trade_line(tl, 3361.2)
    # max decimals among SL(1) and TP1(2) -> 2
    assert out == "BUY MARKET 3361.20 | SL 3363.5 | TP1 3354.50 | Lot 0.02"


def test_inject_filled_price_into_market_trade_line_keeps_tp2_when_present() -> None:
    tl = "SELL MARKET | SL 3363.50 | TP1 3354.5 | TP2 3350.50 | Lot 0.02"
    out = inject_filled_price_into_trade_line(tl, 3360.0)
    assert out == "SELL MARKET 3360.00 | SL 3363.50 | TP1 3354.5 | TP2 3350.50 | Lot 0.02"


def test_inject_does_not_change_when_trade_line_already_has_price() -> None:
    tl = "BUY MARKET 3360.0 | SL 3350.0 | TP1 3370.0 | Lot 0.01"
    assert inject_filled_price_into_trade_line(tl, 3361.2) == tl


def test_inject_does_not_change_when_price_invalid() -> None:
    tl = "BUY MARKET | SL 3363.5 | TP1 3354.5 | Lot 0.02"
    assert inject_filled_price_into_trade_line(tl, None) == tl
    assert inject_filled_price_into_trade_line(tl, 0) == tl

