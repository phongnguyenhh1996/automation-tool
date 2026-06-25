from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from automation_tool.chart_payload_validate import validate_gocharting_csv_file
from automation_tool.gocharting_capture import (
    _capture_csv,
    _capture_png,
    _gocharting_png_is_empty,
)


@pytest.fixture
def gc_cfg() -> dict:
    return {
        "chart_load_ms": 0,
        "empty_download_max_retries": 2,
        "empty_download_retry_delay_ms": 0,
        "screenshot": {
            "open_button": "#user-screenshot-btn",
            "download_button": "button.download",
            "popup_escape_presses": 1,
        },
        "csv_export": {"button_selector": "button.csv"},
    }


def test_gocharting_png_is_empty(tmp_path: Path) -> None:
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    ok, reason = _gocharting_png_is_empty(empty)
    assert ok is True
    assert "empty" in reason.lower()

    nonempty = tmp_path / "ok.png"
    nonempty.write_bytes(b"\x89PNG")
    ok, reason = _gocharting_png_is_empty(nonempty)
    assert ok is False
    assert reason == ""


def test_capture_csv_retries_until_nonempty(tmp_path: Path, gc_cfg: dict, monkeypatch) -> None:
    dest = tmp_path / "sample.csv"
    attempts = {"n": 0}

    def fake_save_download(page, click_fn, path, timeout_ms) -> None:
        attempts["n"] += 1
        click_fn()
        if attempts["n"] < 3:
            path.write_text("", encoding="utf-8")
        else:
            path.write_text("Time,Open\n2026-01-01,1\n", encoding="utf-8")

    page = MagicMock()
    monkeypatch.setattr(
        "automation_tool.gocharting_capture._save_download",
        fake_save_download,
    )
    monkeypatch.setattr(
        "automation_tool.gocharting_capture.prepare_gocharting_csv_file",
        lambda path, **kw: False,
    )

    _capture_csv(page, gc_cfg, dest)

    assert attempts["n"] == 3
    ok, _ = validate_gocharting_csv_file(dest)
    assert ok is True


def test_capture_png_retries_until_nonempty(tmp_path: Path, gc_cfg: dict, monkeypatch) -> None:
    dest = tmp_path / "sample.png"
    attempts = {"n": 0}

    def fake_save_download(page, click_fn, path, timeout_ms) -> None:
        attempts["n"] += 1
        click_fn()
        if attempts["n"] < 2:
            path.write_bytes(b"")
        else:
            path.write_bytes(b"\x89PNG")

    page = MagicMock()
    open_loc = MagicMock()
    dl_loc = MagicMock()
    page.locator.side_effect = lambda sel: open_loc if "screenshot" in sel else dl_loc

    monkeypatch.setattr(
        "automation_tool.gocharting_capture._save_download",
        fake_save_download,
    )

    _capture_png(page, gc_cfg, dest)

    assert attempts["n"] == 2
    assert dest.stat().st_size > 0
    assert open_loc.first.click.call_count == 2
    assert page.keyboard.press.call_args_list == [call("Escape"), call("Escape")]
