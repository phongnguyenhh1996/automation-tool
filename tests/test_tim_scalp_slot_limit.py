from __future__ import annotations

from pathlib import Path

import pytest

from automation_tool.state_files import (
    TimScalpRunState,
    get_tim_scalp_success_count,
    increment_tim_scalp_success,
    read_tim_scalp_run_state,
    write_tim_scalp_run_state,
)
from automation_tool.telegram_listen import _tim_scalp_run_allowed


def test_get_tim_scalp_success_count_resets_on_new_slot_key(tmp_path: Path) -> None:
    p = tmp_path / "tim_scalp_run_state.json"
    write_tim_scalp_run_state(
        TimScalpRunState(slot_key="2026-06-11-sang", slot="sang", success_count=2),
        path=p,
    )
    assert get_tim_scalp_success_count(slot_key="2026-06-11-sang", path=p) == 2
    assert get_tim_scalp_success_count(slot_key="2026-06-11-chieu", path=p) == 0


def test_increment_tim_scalp_success_only_counts_success(tmp_path: Path) -> None:
    p = tmp_path / "tim_scalp_run_state.json"
    assert increment_tim_scalp_success(slot="sang", slot_key="2026-06-11-sang", path=p) == 1
    assert increment_tim_scalp_success(slot="sang", slot_key="2026-06-11-sang", path=p) == 2
    st = read_tim_scalp_run_state(p)
    assert st is not None
    assert st.success_count == 2


def test_tim_scalp_run_allowed_toi_has_no_limit() -> None:
    allowed, msg = _tim_scalp_run_allowed("toi", "2026-06-11-toi")
    assert allowed is True
    assert msg == ""


def test_tim_scalp_run_allowed_blocks_after_two_successes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "tim_scalp_run_state.json"
    write_tim_scalp_run_state(
        TimScalpRunState(slot_key="2026-06-11-chieu", slot="chieu", success_count=2),
        path=p,
    )
    monkeypatch.setattr(
        "automation_tool.telegram_listen.get_tim_scalp_success_count",
        lambda *, slot_key: get_tim_scalp_success_count(slot_key=slot_key, path=p),
    )

    allowed, msg = _tim_scalp_run_allowed("chieu", "2026-06-11-chieu")
    assert allowed is False
    assert "2/2" in msg
    assert "Chiều" in msg


def test_tim_scalp_run_allowed_allows_first_success_in_sang() -> None:
    allowed, msg = _tim_scalp_run_allowed("sang", "2026-06-11-sang")
    assert allowed is True
    assert msg == ""
