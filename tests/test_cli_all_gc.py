from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from automation_tool.cli import _parser


def test_all_gc_capture_tv_only_and_waits_for_urls(monkeypatch, tmp_path: Path) -> None:
    from automation_tool import cli

    tv_url = tmp_path / "20260514_090000_tradingview_DXY_4h.url"
    tv_url.write_text("https://example.invalid/dxy\n", encoding="utf-8")
    calls: dict[str, object] = {}

    class Settings:
        coinmap_email = "user@example.com"
        coinmap_password = "secret"
        tradingview_password = "tv-secret"

    class OpenAIResult:
        final_response_id = "resp-1"
        after_charts = ""

        def full_text(self) -> str:
            return "ok"

    def fake_capture_charts(**kwargs):
        calls["capture"] = kwargs
        return [tv_url]

    def fake_wait(charts_dir, *, poll_seconds=30.0):
        calls["wait_poll"] = poll_seconds
        (charts_dir / "gc_m15.url").write_text("https://gc.example/m15\n", encoding="utf-8")
        (charts_dir / "gc_m5.url").write_text("https://gc.example/m5\n", encoding="utf-8")

    monkeypatch.setattr(cli, "load_settings", lambda: Settings())
    monkeypatch.setattr(cli, "capture_charts", fake_capture_charts)
    monkeypatch.setattr(cli, "stamp_from_capture_paths", lambda _paths: "20260514_090000")
    monkeypatch.setattr(cli, "list_invalid_chart_slots_for_stamp", lambda *_a, **_k: [])
    monkeypatch.setattr(cli, "require_openai", lambda _settings: None)
    monkeypatch.setattr(cli, "ordered_chart_openai_payloads", lambda _charts_dir: [("image_url", "https://x")])
    monkeypatch.setattr(cli, "_warn_if_incomplete_chart_payloads", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "_run_openai_flow", lambda *_a, **_k: OpenAIResult())
    monkeypatch.setattr(cli, "_run_all_second_flow", lambda *_a, **_k: OpenAIResult())
    monkeypatch.setattr(cli, "write_last_response_id", lambda _response_id: None)
    monkeypatch.setattr(cli, "write_last_all_response_id", lambda _response_id: None)
    monkeypatch.setattr(cli, "resolved_openai_model", lambda _settings, model: model)
    monkeypatch.setattr(cli, "zones_dir_from_cli_path", lambda _path: tmp_path / "zones")
    with patch.object(cli, "wait_for_gc_manual_urls", side_effect=fake_wait):
        args = _parser().parse_args(
            [
                "all",
                "--gc",
                "--gc-poll-seconds",
                "5",
                "--charts-dir",
                str(tmp_path),
                "--no-telegram",
                "--no-clear-zones-state",
            ]
        )
        cli.cmd_all(args)

    capture = calls.get("capture")
    assert isinstance(capture, dict)
    assert capture.get("enable_coinmap") is False
    assert (tmp_path / ".gc_mode").is_file()
    assert calls.get("wait_poll") == 5.0
