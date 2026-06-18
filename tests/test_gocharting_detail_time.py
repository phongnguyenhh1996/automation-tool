from __future__ import annotations

from automation_tool.gocharting_capture import (
    gocharting_detail_back_suffix,
    gocharting_detail_back_suffixes,
)


def test_gocharting_detail_back_suffix() -> None:
    assert gocharting_detail_back_suffix(1) == "back_1"
    assert gocharting_detail_back_suffix(2) == "back_2"
    assert gocharting_detail_back_suffix(3) == "back_3"


def test_gocharting_detail_back_suffixes() -> None:
    assert gocharting_detail_back_suffixes(steps=3) == [
        "back_1",
        "back_2",
        "back_3",
    ]
