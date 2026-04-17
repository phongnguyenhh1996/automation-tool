"""MT5 Telegram: zone label → 'Đã vào lệnh cho …' line (suffix after random emoji line)."""

from automation_tool.telegram_bot import mt5_zone_entry_line_vn, mt5_zone_label_display_vn


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
