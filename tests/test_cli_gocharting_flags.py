from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from automation_tool.cli import cmd_all


def test_cmd_all_gocharting_calls_capture_gocharting(tmp_path: Path, monkeypatch) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    (charts / ".main_chart_symbol").write_text("XAUUSD\n", encoding="utf-8")

    gc_called = False
    gc_clear_before: bool | None = None

    def fake_capture_gocharting(**kwargs):
        nonlocal gc_called, gc_clear_before
        gc_called = True
        gc_clear_before = kwargs.get("clear_charts_before_capture")
        stamp = "20260616_120000"
        paths = []
        for sym, iv in (("DXY", "15m"), ("XAUUSD", "15m"), ("XAUUSD", "5m")):
            csv_p = charts / f"{stamp}_gocharting_{sym}_{iv}.csv"
            png_p = charts / f"{stamp}_gocharting_{sym}_{iv}.png"
            csv_p.write_text("Time,Open\n1,2\n", encoding="utf-8")
            png_p.write_bytes(b"x")
            paths.extend([csv_p, png_p])
        return paths

    monkeypatch.setattr("automation_tool.cli.capture_gocharting", fake_capture_gocharting)
    monkeypatch.setattr(
        "automation_tool.cli.load_settings",
        lambda: MagicMock(
            coinmap_email="a",
            coinmap_password="b",
            gocharting_email="gc@x.com",
            gocharting_password="pw",
            tradingview_password="tv",
            openai_api_key="sk-test",
            openai_model="gpt-test",
            telegram_bot_token="t",
            telegram_chat_id="c",
            telegram_log_chat_id=None,
            openai_responses_store=False,
            openai_responses_include=[],
        ),
    )

    args = Namespace(
        main_symbol="XAUUSD",
        zones_json=None,
        no_clear_zones_state=True,
        config=tmp_path / "coinmap.yaml",
        charts_dir=charts,
        storage_state=None,
        no_save_storage=True,
        headed=False,
        no_tradingview=True,
        no_telegram=True,
        prompt="[FULL_ANALYSIS] test",
        max_images_per_call=20,
        gocharting=True,
        gocharting_config=None,
        model=None,
        mt5_accounts_json=None,
    )

    with patch("automation_tool.cli.list_invalid_chart_slots_for_stamp", return_value=[]):
        with patch("automation_tool.cli._run_openai_flow") as mock_openai:
            mock_openai.return_value = MagicMock(
                full_text=lambda: "ok",
                final_response_id="r1",
                after_charts="",
            )
            with patch("automation_tool.cli._run_all_second_flow"):
                with patch("automation_tool.cli.send_capture_screenshots_to_log_chat", return_value=0):
                    cmd_all(args)

    assert gc_called is True
    assert gc_clear_before is True
