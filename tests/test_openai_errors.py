from __future__ import annotations

import sys
from types import ModuleType

from automation_tool.openai_errors import format_openai_exception


def test_format_openai_exception_insufficient_quota_uses_friendly_billing_notice(
    monkeypatch,
) -> None:
    class FakeAPIError(Exception):
        pass

    class FakeAuthenticationError(FakeAPIError):
        pass

    class FakeRateLimitError(FakeAPIError):
        def __init__(self, message: str, *, body: object = None) -> None:
            super().__init__(message)
            self.body = body

    fake_openai = ModuleType("openai")
    fake_openai.APIError = FakeAPIError
    fake_openai.AuthenticationError = FakeAuthenticationError
    fake_openai.RateLimitError = FakeRateLimitError
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    exc = FakeRateLimitError(
        "429 insufficient_quota",
        body={"error": {"code": "insufficient_quota"}},
    )
    msg = format_openai_exception(exc)

    assert msg is not None
    assert "Hết quota OpenAI" in msg
    assert "Hết thóc rồi, không gáy được nữa" in msg
    assert "https://platform.openai.com/account/billing" in msg
