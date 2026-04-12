from __future__ import annotations

from automation_tool.openai_prompt_flow import build_intraday_update_user_text
from automation_tool.zones_state import (
    Zone,
    ZonesState,
    format_zones_snapshot_for_intraday_update,
)


def test_format_zones_snapshot_empty() -> None:
    s = format_zones_snapshot_for_intraday_update(None)
    assert "Thời gian hiện tại" in s
    assert "Chưa có snapshot" in s


def test_format_zones_snapshot_grouped() -> None:
    st = ZonesState(
        symbol="XAUUSD",
        zones=[
            Zone(
                id="plan_chinh",
                label="plan_chinh",
                range_low=1.0,
                range_high=2.0,
                alert_price=1.5,
                side="BUY",
                hop_luu=80,
                trade_line="BUY LIMIT 1.5 | SL 1.0 | TP1 2.0 | Lot 0.01",
                status="vung_cho",
            ),
            Zone(
                id="plan_phu",
                label="plan_phu",
                range_low=2.5,
                range_high=3.0,
                alert_price=2.8,
                side="SELL",
                hop_luu=70,
                trade_line="",
                status="cham",
            ),
            Zone(
                id="scalp",
                label="scalp",
                range_low=3.0,
                range_high=4.0,
                alert_price=3.5,
                side="SELL",
                status="loai",
            ),
        ],
    )
    s = format_zones_snapshot_for_intraday_update(st)
    assert "Tóm tắt theo label" in s
    assert "vùng plan_chinh vẫn đang là vùng chờ" in s
    assert "vùng plan_phu đã chạm và vẫn đang chờ" in s
    assert "vùng scalp đã loại" in s
    assert "Chi tiết" in s
    assert "status=vung_cho" in s and "status=loai" in s and "status=cham" in s


def test_build_intraday_update_user_text_contains_baseline_and_tasks() -> None:
    t = build_intraday_update_user_text(1.0, 2.0, 3.0, zones_snapshot="Z\n")
    assert "[INTRADAY_UPDATE]" in t
    assert "plan_chinh: 1.0" in t
    assert "M15" in t and "M5" in t
    assert "Z" in t
