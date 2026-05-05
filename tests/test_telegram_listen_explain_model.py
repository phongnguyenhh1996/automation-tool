from __future__ import annotations

from automation_tool.telegram_listen import _explain_followup_model


def test_explain_followup_model_uses_mini_even_when_override_is_set() -> None:
    assert _explain_followup_model("gpt-5.4") == "gpt-5.4-mini"
