"""Session slots and shard filenames (VN HCM)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from pathlib import Path

from automation_tool.zones_paths import (
    label_from_shard_stem,
    resolve_session_slot_raw,
    session_slot_display_vn,
    session_slot_from_shard_path,
    session_slot_now_hcm,
    shard_filename,
    zone_id_for_shard,
)


def test_session_slot_boundaries_hcm() -> None:
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    assert session_slot_now_hcm(datetime(2026, 4, 16, 0, 0, tzinfo=tz)) == "sang"
    assert session_slot_now_hcm(datetime(2026, 4, 16, 12, 59, tzinfo=tz)) == "sang"
    assert session_slot_now_hcm(datetime(2026, 4, 16, 13, 0, tzinfo=tz)) == "chieu"
    assert session_slot_now_hcm(datetime(2026, 4, 16, 18, 59, tzinfo=tz)) == "chieu"
    assert session_slot_now_hcm(datetime(2026, 4, 16, 19, 0, tzinfo=tz)) == "toi"
    assert session_slot_now_hcm(datetime(2026, 4, 16, 23, 59, tzinfo=tz)) == "toi"


def test_shard_filename_and_zone_id() -> None:
    assert shard_filename("plan_chinh", "sang") == "vung_plan_chinh_sang.json"
    assert zone_id_for_shard("plan_chinh", "chieu") == "plan_chinh__chieu"


def test_session_slot_from_shard_path() -> None:
    assert session_slot_from_shard_path(Path("zones/vung_scalp_toi.json")) == "toi"
    assert session_slot_from_shard_path(Path("vung_plan_phu_chieu.json")) == "chieu"


def test_label_from_shard_stem() -> None:
    assert label_from_shard_stem("vung_plan_chinh_sang") == "plan_chinh"
    assert label_from_shard_stem("vung_scalp_toi") == "scalp"


def test_session_slot_display_vn() -> None:
    assert session_slot_display_vn("sang") == "Sáng"
    assert session_slot_display_vn("chieu") == "Chiều"
    assert session_slot_display_vn("toi") == "Tối"
    assert session_slot_display_vn(None) is None
    assert session_slot_display_vn("other") is None


def test_resolve_session_slot_raw() -> None:
    assert resolve_session_slot_raw(zone_session_slot="sang") == "sang"
    assert resolve_session_slot_raw(zone_session_slot="CHIEU") == "chieu"
    p = Path("zones/vung_scalp_toi.json")
    assert resolve_session_slot_raw(shard_path=p) == "toi"
    assert resolve_session_slot_raw(zone_session_slot="chieu", shard_path=p) == "chieu"
