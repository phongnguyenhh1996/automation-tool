from __future__ import annotations

from pathlib import Path

from automation_tool.cli import _parser


def test_all_does_not_request_cloudinary_json_purge(monkeypatch, tmp_path: Path) -> None:
    from automation_tool import cli

    chart_json = tmp_path / "20260514_090000_coinmap_XAUUSD_5m.json"
    chart_json.write_text("{}", encoding="utf-8")
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

    def fake_run_openai_flow(*_args, **kwargs):
        calls["openai"] = kwargs
        return OpenAIResult()

    monkeypatch.setattr(cli, "load_settings", lambda: Settings())
    monkeypatch.setattr(cli, "capture_charts", lambda **_kwargs: [chart_json])
    monkeypatch.setattr(cli, "stamp_from_capture_paths", lambda _paths: "20260514_090000")
    monkeypatch.setattr(cli, "list_invalid_chart_slots_for_stamp", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cli, "require_openai", lambda _settings: None)
    monkeypatch.setattr(cli, "ordered_chart_openai_payloads", lambda _charts_dir: [("json", chart_json)])
    monkeypatch.setattr(cli, "_warn_if_incomplete_chart_payloads", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_run_openai_flow", fake_run_openai_flow)
    monkeypatch.setattr(cli, "write_last_response_id", lambda _response_id: None)
    monkeypatch.setattr(cli, "write_last_all_response_id", lambda _response_id: None)
    monkeypatch.setattr(cli, "resolved_openai_model", lambda _settings, model: model)
    monkeypatch.setattr(cli, "zones_dir_from_cli_path", lambda _path: tmp_path / "zones")

    args = _parser().parse_args(
        [
            "all",
            "--charts-dir",
            str(tmp_path),
            "--prompt",
            "test prompt",
            "--no-telegram",
            "--no-clear-zones-state",
        ]
    )
    cli.cmd_all(args)

    openai = calls["openai"]
    assert isinstance(openai, dict)
    assert openai.get("purge_json_attachment_storage", False) is False


def test_all_morning_clear_keeps_ea_neverdie_json(monkeypatch, tmp_path: Path) -> None:
    from automation_tool import cli

    chart_json = tmp_path / "20260514_090000_coinmap_XAUUSD_5m.json"
    chart_json.write_text("{}", encoding="utf-8")
    calls = {"clear_neverdie": 0}

    class Settings:
        coinmap_email = "user@example.com"
        coinmap_password = "secret"
        tradingview_password = "tv-secret"

    class OpenAIResult:
        final_response_id = "resp-1"
        after_charts = ""

        def full_text(self) -> str:
            return "ok"

    def fail_clear_neverdie(*_args, **_kwargs) -> None:
        calls["clear_neverdie"] += 1

    monkeypatch.setattr(cli, "load_settings", lambda: Settings())
    monkeypatch.setattr(cli, "session_slot_now_hcm", lambda: "sang")
    monkeypatch.setattr(cli, "zones_dir_from_cli_path", lambda _path: tmp_path / "zones")
    monkeypatch.setattr(cli, "stop_daemon_plans_in_zones", lambda _zones_dir: None)
    monkeypatch.setattr(cli, "clear_zones_directory", lambda _zones_dir: 0)
    monkeypatch.setattr(
        "automation_tool.ea_neverdie_zone_publish.clear_neverdie_before_all",
        fail_clear_neverdie,
    )
    monkeypatch.setattr(cli, "capture_charts", lambda **_kwargs: [chart_json])
    monkeypatch.setattr(cli, "stamp_from_capture_paths", lambda _paths: "20260514_090000")
    monkeypatch.setattr(cli, "list_invalid_chart_slots_for_stamp", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cli, "require_openai", lambda _settings: None)
    monkeypatch.setattr(cli, "ordered_chart_openai_payloads", lambda _charts_dir: [("json", chart_json)])
    monkeypatch.setattr(cli, "_warn_if_incomplete_chart_payloads", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_run_openai_flow", lambda *_args, **_kwargs: OpenAIResult())
    monkeypatch.setattr(cli, "write_last_response_id", lambda _response_id: None)
    monkeypatch.setattr(cli, "write_last_all_response_id", lambda _response_id: None)
    monkeypatch.setattr(cli, "resolved_openai_model", lambda _settings, model: model)

    args = _parser().parse_args(
        [
            "all",
            "--charts-dir",
            str(tmp_path),
            "--prompt",
            "test prompt",
            "--no-telegram",
        ]
    )
    cli.cmd_all(args)

    assert calls["clear_neverdie"] == 0
