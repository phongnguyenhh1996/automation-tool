from __future__ import annotations

import asyncio

from automation_tool import coinmap_chart_async
from automation_tool.coinmap_chart_async import coinmap_warmup_tab_async


class _FakeLocator:
    def __init__(self) -> None:
        pass

    @property
    def first(self):
        return self

    async def wait_for(self, **_kwargs) -> None:
        raise TimeoutError("not visible")

    async def fill(self, *_args, **_kwargs) -> None:
        return None

    async def click(self, **_kwargs) -> None:
        return None

    async def count(self) -> int:
        return 0

    def locator(self, _sel: str):
        return self

    def get_by_text(self, *_args, **_kwargs):
        return self


class _FakeKeyboard:
    async def press(self, _key: str) -> None:
        return None


class _FakePage:
    def __init__(self, *, start_url: str = "https://coinmap.tech/chart") -> None:
        self.url = start_url
        self.gotos: list[str] = []

    async def goto(self, url: str, **_kwargs) -> None:
        self.gotos.append(url)
        self.url = url

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    async def wait_for_load_state(self, *_args, **_kwargs) -> None:
        return None

    @property
    def keyboard(self):
        return _FakeKeyboard()

    def locator(self, _sel: str):
        return _FakeLocator()


def test_warmup_opens_chart_first_without_login_when_session_active() -> None:
    page = _FakePage()

    async def run() -> None:
        await coinmap_warmup_tab_async(
            page,
            {"chart_page_url": "https://coinmap.tech/chart"},
            {"login_url": "https://coinmap.tech/login"},
            email="user@example.com",
            password="secret",
            settle_ms=10,
        )

    asyncio.run(run())
    assert page.gotos == ["https://coinmap.tech/chart"]
    assert page.url == "https://coinmap.tech/chart"


def test_warmup_calls_login_helper_after_chart_navigation(monkeypatch) -> None:
    page = _FakePage(start_url="https://coinmap.tech/login")
    login_calls: list[str] = []

    async def fake_login_if_needed(_page, _cd, _cfg, *, email, password, settle_ms, chart_url):
        login_calls.append(f"{email}:{password}:{chart_url}")

    monkeypatch.setattr(
        coinmap_chart_async,
        "_coinmap_maybe_login_if_needed_async",
        fake_login_if_needed,
    )

    async def run() -> None:
        await coinmap_warmup_tab_async(
            page,
            {"chart_page_url": "https://coinmap.tech/chart"},
            {"login_url": "https://coinmap.tech/login"},
            email="user@example.com",
            password="secret",
            settle_ms=10,
        )

    asyncio.run(run())
    assert page.gotos[0] == "https://coinmap.tech/chart"
    assert login_calls == ["user@example.com:secret:https://coinmap.tech/chart"]
