"""Tests for [TRADE_MANAGEMENT] follow-up JSON parsing."""

from automation_tool.tp1_followup import parse_tp1_followup_decision


def test_parse_tp1_followup_decision_includes_reason() -> None:
    dec = parse_tp1_followup_decision(
        """```json
{
  "hanh_dong_quan_ly_lenh": "giu_nguyen",
  "trade_line_moi": "",
  "reason": "Giá giữ được VWAP và CVD chưa đảo chiều, nên tiếp tục giữ lệnh."
}
```"""
    )

    assert dec is not None
    assert dec.sau_tp1 == "giu_nguyen"
    assert dec.reason == "Giá giữ được VWAP và CVD chưa đảo chiều, nên tiếp tục giữ lệnh."
