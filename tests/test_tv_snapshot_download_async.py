from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from automation_tool.coinmap_tradingview_async import tv_snapshot_download_capture_async


class _FakeDownload:
    suggested_filename = "chart.png"

    async def save_as(self, path: str | Path) -> None:
        Path(path).write_bytes(b"")

    async def path(self):
        return None


class _FakePage:
    def __init__(self) -> None:
        self.keyboard = MagicMock()
        self.keyboard.press = AsyncMock()
        self.mouse = MagicMock()
        self.mouse.click = AsyncMock()

    async def bring_to_front(self) -> None:
        return None

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    @asynccontextmanager
    async def expect_download(self, **_kwargs):
        info = MagicMock()

        async def _value():
            return _FakeDownload()

        info.value = _value()
        yield info


def test_tv_snapshot_download_async_empty_png_retry_does_not_crash(tmp_path: Path) -> None:
    async def run() -> Path:
        return await tv_snapshot_download_capture_async(
            _FakePage(),
            {
                "tradingview_snapshot_download_empty_max_retries": 1,
                "tradingview_snapshot_download_empty_retry_delay_ms": 0,
                "tradingview_snapshot_download_pickup_timeout_ms": 0,
                "after_tradingview_snapshot_download_ms": 0,
            },
            tmp_path,
            "stamp",
            "XAUUSD",
            "15m",
        )

    dest = asyncio.run(run())
    assert dest == tmp_path / "stamp_tradingview_XAUUSD_15m.png"
