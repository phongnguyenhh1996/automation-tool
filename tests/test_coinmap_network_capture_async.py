from __future__ import annotations

import asyncio
import json

from automation_tool.coinmap_chart_async import CoinmapNetworkCaptureAsync


class _FakeResponse:
    url = "https://gw.coinmap.tech/cm-api/api/v1/getcandlehistory?symbol=XAUUSD"
    status = 200

    async def json(self):
        return [{"s": "XAUUSD", "i": "5m", "t": 1}]

    async def text(self):
        return "[]"


def test_network_capture_async_awaits_response_body() -> None:
    cap = CoinmapNetworkCaptureAsync(page=None, api_cd={})  # type: ignore[arg-type]

    async def run() -> None:
        await cap._on_response_async(_FakeResponse())

    asyncio.run(run())
    assert len(cap._records) == 1
    body = cap._records[0]["body"]
    assert isinstance(body, list)
    assert body[0]["s"] == "XAUUSD"
    json.dumps({"body": body})
