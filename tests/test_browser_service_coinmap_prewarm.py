from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from automation_tool import coinmap_chart_async
from automation_tool.browser_service import BrowserServiceState, _CoinmapWarmTab


def test_coinmap_prewarm_sets_warm_tab(monkeypatch) -> None:
    async def run() -> None:
        service = BrowserServiceState()
        service._closing = False
        service._context = MagicMock()
        fake_page = MagicMock()
        service._context.new_page = AsyncMock(return_value=fake_page)

        async def fake_warmup(*_args, **_kwargs) -> None:
            return None

        monkeypatch.setattr(coinmap_chart_async, "coinmap_warmup_tab_async", fake_warmup)
        monkeypatch.setattr(
            "automation_tool.coinmap.load_coinmap_yaml",
            lambda _p: {
                "settle_ms": 100,
                "chart_download": {
                    "enabled": True,
                    "prewarm_enabled": True,
                    "chart_page_url": "https://coinmap.tech/chart",
                    "capture_plan": [{"symbol": "XAUUSD", "interval": "5m"}],
                    "api_data_export": {"enabled": True},
                },
            },
        )

        await service._prewarm_coinmap_tab_async()
        assert service._cm_warm is not None
        assert service._cm_warm.page is fake_page

    asyncio.run(run())


def test_ensure_cm_warm_tab_reprewarms_when_page_closed(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_prewarm() -> None:
        calls.append("prewarm")

    async def run() -> None:
        service = BrowserServiceState()
        closed_page = MagicMock()
        closed_page.is_closed.return_value = True
        service._cm_warm = _CoinmapWarmTab(closed_page, "XAUUSD", "5m")
        monkeypatch.setattr(service, "_prewarm_coinmap_tab_async", fake_prewarm)
        await service._ensure_cm_warm_tab()

    asyncio.run(run())
    assert calls == ["prewarm"]
