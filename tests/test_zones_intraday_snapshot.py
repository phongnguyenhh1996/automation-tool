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
                vung_cho="1.0–2.0",
                side="BUY",
                hop_luu=80,
                trade_line="BUY LIMIT 1.5 | SL 1.0 | TP1 2.0 | Lot 0.01",
                status="vung_cho",
            ),
            Zone(
                id="plan_phu",
                label="plan_phu",
                vung_cho="2.5–3.0",
                side="SELL",
                hop_luu=70,
                trade_line="",
                status="cham",
            ),
            Zone(
                id="scalp",
                label="scalp",
                vung_cho="3.0–4.0",
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
    t = build_intraday_update_user_text(None)
    assert "[INTRADAY_UPDATE]" in t
    assert "Thời gian hiện tại" in t
    assert "vùng chờ plan_chinh: (chưa có)" in t
    assert "vùng chờ plan_phu: (chưa có)" in t
    assert "vùng chờ scalp: (chưa có)" in t
    assert "M15" in t and "M5" in t
    assert "phan_tich_update" in t
    assert "Trạng thái các vùng" not in t


def test_build_intraday_update_user_text_uses_vung_cho_from_zones_state() -> None:
    st = ZonesState(
        symbol="XAUUSD",
        zones=[
            Zone(
                id="plan_chinh",
                label="plan_chinh",
                vung_cho="4700.0–4710.0",
                side="BUY",
                hop_luu=80,
                trade_line="",
                status="vung_cho",
            ),
            Zone(
                id="plan_phu",
                label="plan_phu",
                vung_cho="4600.0–4605.0",
                side="SELL",
                hop_luu=70,
                trade_line="",
                status="vung_cho",
            ),
            Zone(
                id="scalp",
                label="scalp",
                vung_cho="4720.0–4722.0",
                side="BUY",
                hop_luu=60,
                trade_line="",
                status="vung_cho",
            ),
        ],
    )
    t = build_intraday_update_user_text(st)
    assert "vùng chờ plan_chinh: 4700.0–4710.0" in t
    assert "vùng chờ plan_phu: 4600.0–4605.0" in t
    assert "vùng chờ scalp: 4720.0–4722.0" in t


def test_format_intraday_update_time_line() -> None:
    from automation_tool.zones_state import format_intraday_update_time_line

    line = format_intraday_update_time_line()
    assert line.startswith("Thời gian hiện tại (Asia/Ho_Chi_Minh):")
    assert len(line) > 30
