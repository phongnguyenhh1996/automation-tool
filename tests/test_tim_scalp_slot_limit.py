from __future__ import annotations

from pathlib import Path

from automation_tool.state_files import (
    TimScalpRunState,
    get_tim_scalp_success_count,
    increment_tim_scalp_success,
    read_tim_scalp_run_state,
    write_tim_scalp_run_state,
)


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
