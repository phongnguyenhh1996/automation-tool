"""Session slots and shard filenames (VN HCM)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from automation_tool.zones_paths import (
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
