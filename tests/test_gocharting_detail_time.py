from __future__ import annotations

import pytest

from automation_tool.gocharting_capture import (
    gocharting_detail_back_suffix,
    gocharting_detail_back_suffixes,
    subtract_hours_12h,
)


@pytest.mark.parametrize(
    ("hour12", "minute", "am_pm", "hours", "expected"),
    [
        (3, 30, "pm", 3, (12, 30, "pm")),
        (10, 0, "am", 3, (7, 0, "am")),
        (2, 15, "am", 3, (11, 15, "pm")),
        (12, 0, "am", 1, (11, 0, "pm")),
        (12, 0, "pm", 12, (12, 0, "am")),
        (1, 0, "am", 2, (11, 0, "pm")),
    ],
)
def test_subtract_hours_12h(
    hour12: int,
    minute: int,
    am_pm: str,
    hours: int,
    expected: tuple[int, int, str],
) -> None:
    assert subtract_hours_12h(hour12, minute, am_pm, hours) == expected


def test_gocharting_detail_back_suffix_m5() -> None:
    assert gocharting_detail_back_suffix("5m", 1, hours_back=3) == "back_3h"
    assert gocharting_detail_back_suffix("5m", 3, hours_back=3) == "back_9h"


def test_gocharting_detail_back_suffix_m15() -> None:
    assert gocharting_detail_back_suffixes("15m", hours_back=7, steps=3) == [
        "back_7h",
        "back_14h",
        "back_21h",
    ]
