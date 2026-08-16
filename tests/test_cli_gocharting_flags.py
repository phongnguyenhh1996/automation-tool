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
        fp_dir = charts / "footprint_images"
        fp_dir.mkdir(parents=True, exist_ok=True)
        stamp = "20260616_120000"
        paths = [charts / f"{stamp}_tradingview_XAUUSD_5m.png"]
        paths[0].write_bytes(b"x")
        for iv in ("15m", "5m"):
            p = fp_dir / f"footprint_combined_{iv}.json"
            p.write_text('{"candles":[{"time_gmt7":"Thu Jul 2 2026 05:00:00 GMT+0700"}]}\n', encoding="utf-8")
            paths.append(p)
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
        gc_only=False,
        model=None,
        mt5_accounts_json=None,
        all_two_phase=False,
        no_all_two_phase=False,
    )

    with patch("automation_tool.cli.list_invalid_chart_slots_for_stamp", return_value=[]):
        with patch("automation_tool.cli._persist_openai_footprint_json_debug"):
            with patch("automation_tool.cli.write_last_response_id"):
                with patch("automation_tool.cli.write_last_all_response_id"):
                    with patch("automation_tool.cli._run_all_second_flow") as mock_all2:
                        mock_all2.return_value = MagicMock(
                            final_response_id="r1",
                            after_charts="",
                        )
                        with patch(
                            "automation_tool.cli.send_capture_screenshots_to_log_chat",
                            return_value=0,
                        ):
                            cmd_all(args)
                        mock_all2.assert_called_once()

    assert gc_called is True
    assert gc_clear_before is True
