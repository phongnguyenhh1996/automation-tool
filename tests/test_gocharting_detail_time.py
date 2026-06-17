from __future__ import annotations

import pytest

from automation_tool.gocharting_capture import (
    gocharting_detail_back_suffix,
    gocharting_detail_back_suffixes,
    subtract_duration_12h,
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


def test_subtract_duration_12h_half_hour() -> None:
    assert subtract_duration_12h(3, 30, "pm", hours=2.5) == (1, 0, "pm")
    assert subtract_duration_12h(3, 30, "pm", hours=5.0) == (10, 30, "am")


def test_detail_back_offsets_from_shared_baseline_m15() -> None:
    """All intervals: step N subtracts N × hours_back from the same first-read baseline."""
    baseline = (3, 30, "pm")
    h, m, ap = baseline
    assert subtract_duration_12h(h, m, ap, hours=7) == (8, 30, "am")
    assert subtract_duration_12h(h, m, ap, hours=14) == (1, 30, "am")
    assert subtract_duration_12h(h, m, ap, hours=21) == (6, 30, "pm")


def test_gocharting_detail_back_suffix_m5() -> None:
    assert gocharting_detail_back_suffix("5m", 1, hours_back=2.5) == "back_2h30"
    assert gocharting_detail_back_suffix("5m", 2, hours_back=2.5) == "back_5h"
    assert gocharting_detail_back_suffix("5m", 3, hours_back=2.5) == "back_7h30"


def test_gocharting_detail_back_suffix_m15() -> None:
    assert gocharting_detail_back_suffixes("15m", hours_back=7, steps=3) == [
        "back_7h",
        "back_14h",
        "back_21h",
    ]
