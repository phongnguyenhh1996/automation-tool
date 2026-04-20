from __future__ import annotations

from automation_tool.openai_prompt_flow import is_first_intraday_update_after_all


def test_is_first_intraday_update_after_all_true_when_ids_match() -> None:
    assert is_first_intraday_update_after_all(
        last_response_id="resp_all_1",
        last_all_response_id="resp_all_1",
    )


def test_is_first_intraday_update_after_all_false_after_update() -> None:
    assert not is_first_intraday_update_after_all(
        last_response_id="resp_update_1",
        last_all_response_id="resp_all_1",
    )


def test_is_first_intraday_update_after_all_false_if_either_missing() -> None:
    assert not is_first_intraday_update_after_all(
        last_response_id=None,
        last_all_response_id="x",
    )
    assert not is_first_intraday_update_after_all(
        last_response_id="x",
        last_all_response_id=None,
    )
    assert not is_first_intraday_update_after_all(
        last_response_id="",
        last_all_response_id="y",
    )
