"""MT5 Telegram: zone label → user-facing Vietnamese lines."""

from automation_tool.telegram_bot import (
    mt5_zone_entry_line_vn,
    mt5_zone_label_display_vn,
    send_mt5_execution_log_to_ngan_gon_chat,
    send_trade_management_reason_notice,
)


def test_mt5_zone_entry_line_vn_known_labels():
    assert mt5_zone_entry_line_vn("plan_chinh") == 'Đã vào lệnh cho "Plan chính".'
    assert mt5_zone_entry_line_vn("PLAN_PHU") == 'Đã vào lệnh cho "Plan phụ".'
    assert mt5_zone_entry_line_vn("scalp") == 'Đã vào lệnh cho "Scalp".'


def test_mt5_zone_entry_line_vn_with_session_slot():
    assert (
        mt5_zone_entry_line_vn("scalp", session_slot="chieu")
        == 'Đã vào lệnh cho "Scalp - Chiều".'
    )
    assert (
        mt5_zone_entry_line_vn("plan_chinh", session_slot="sang")
        == 'Đã vào lệnh cho "Plan chính - Sáng".'
    )


def test_mt5_zone_entry_line_vn_absent_or_unknown():
    assert mt5_zone_entry_line_vn(None) is None
    assert mt5_zone_entry_line_vn("") is None
    assert mt5_zone_entry_line_vn("other") is None


def test_mt5_zone_label_display_vn_known_labels():
    assert mt5_zone_label_display_vn("plan_chinh") == "Plan chính"
    assert mt5_zone_label_display_vn("PLAN_PHU") == "Plan phụ"
    assert mt5_zone_label_display_vn("scalp") == "Scalp"


def test_mt5_zone_label_display_vn_absent_or_unknown():
    assert mt5_zone_label_display_vn(None) is None
    assert mt5_zone_label_display_vn("") is None
    assert mt5_zone_label_display_vn("other") is None


def test_send_trade_management_reason_notice_formats_plan_slot(monkeypatch):
    calls: list[dict] = []

    def fake_send(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("automation_tool.telegram_bot.send_message", fake_send)
    send_trade_management_reason_notice(
        bot_token="token",
        telegram_python_bot_chat_id="123",
        zone_label="plan_chinh",
        session_slot="sang",
        action="chinh_trade_line",
        reason="Footprint còn thuận hướng nên dời SL về BE.",
        trade_line="BUY LIMIT 4709.0 | SL 4709.0 | TP1 4740.0 | Lot 0.04",
    )

    assert len(calls) == 1
    assert "Quản lý lệnh (Plan chính - Sáng): Chỉnh trade line" in calls[0]["text"]
    assert "Footprint còn thuận hướng nên dời SL về BE." in calls[0]["text"]
    assert "trade_line_moi: BUY LIMIT 4709.0" in calls[0]["text"]


def test_chinh_trade_line_success_message_is_not_entry_notice(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr("automation_tool.telegram_bot.send_message", lambda **kwargs: sent.append(kwargs))
    monkeypatch.setattr("automation_tool.telegram_bot.random.choice", lambda _items: "ENTRY TEMPLATE")

    send_mt5_execution_log_to_ngan_gon_chat(
        bot_token="token",
        telegram_chat_id="main",
        telegram_python_bot_chat_id="python",
        telegram_log_chat_id=None,
        source="tp1-followup-chinh",
        text="modified_sltp",
        execution_ok=True,
        zone_label="plan_chinh",
        trade_line="BUY LIMIT 4709.0 | SL 4709.0 | TP1 4740.0 | Lot 0.04",
        session_slot="sang",
        action="chinh_trade_line",
    )

    assert len(sent) == 1
    assert sent[0]["chat_id"] == "main"
    assert "Đã chỉnh lệnh cho \"Plan chính - Sáng\"." in sent[0]["text"]
    assert "Đã vào lệnh cho" not in sent[0]["text"]
    assert "ENTRY TEMPLATE" not in sent[0]["text"]
