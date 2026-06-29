from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

from automation_tool.coinmap import (
    _tradingview_download_png_is_empty,
    _tradingview_materialize_browser_download,
    _tradingview_newest_png_since,
)


def test_tradingview_download_png_is_empty(tmp_path: Path) -> None:
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    ok, reason = _tradingview_download_png_is_empty(empty)
    assert ok is True
    assert reason == "empty PNG"

    good = tmp_path / "good.png"
    good.write_bytes(b"\x89PNG\r\n\x1a\n")
    ok, reason = _tradingview_download_png_is_empty(good)
    assert ok is False


def test_tradingview_newest_png_since(tmp_path: Path) -> None:
    since = time.monotonic()
    old = tmp_path / "old.png"
    old.write_bytes(b"\x89PNG")
    time.sleep(0.02)
    new = tmp_path / "chart.png"
    new.write_bytes(b"\x89PNG\x00")
    found = _tradingview_newest_png_since(tmp_path, since_monotonic=since)
    assert found == new


def test_tradingview_materialize_picks_native_download(tmp_path: Path) -> None:
    download_dir = tmp_path / ".tv_downloads"
    download_dir.mkdir()
    dest = tmp_path / "out.png"
    native = download_dir / "OANDA_XAUUSD_15.png"
    native.write_bytes(b"\x89PNG\r\n\x1a\n\x00")

    page = MagicMock()
    download = MagicMock()
    download.suggested_filename.return_value = "OANDA_XAUUSD_15.png"
    download.path.side_effect = RuntimeError("no artifact")

    def _save_as(path: str) -> None:
        Path(path).write_bytes(b"")

    download.save_as.side_effect = _save_as

    ok = _tradingview_materialize_browser_download(
        page,
        download,
        dest,
        {},
        download_dir=download_dir,
        since_monotonic=time.monotonic() - 1,
        wait_ms=500,
    )
    assert ok is True
    assert dest.read_bytes().startswith(b"\x89PNG")
