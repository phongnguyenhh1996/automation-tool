from __future__ import annotations

import json
from pathlib import Path

from automation_tool.cli import _parser
from automation_tool.openai_footprint_vector_store import with_gocharting_footprint_vector_store_ids


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


def test_all_runs_second_flow_with_dedicated_vector_channel_and_all2_shards(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from automation_tool import cli

    chart_json = tmp_path / "20260514_090000_coinmap_XAUUSD_5m.json"
    chart_json.write_text("{}", encoding="utf-8")
    zones_dir = tmp_path / "zones"
    openai_calls: list[dict[str, object]] = []
    telegram_calls: list[dict[str, object]] = []
    response_ids: list[str] = []

    payload = json.dumps(
        {
            "prices": [
                {
                    "label": "plan_chinh",
                    "value": 4709.0,
                    "vung_cho": "4708–4710",
                    "side": "BUY",
                    "hop_luu": 80,
                    "trade_line": "BUY LIMIT 4709.0 | SL 4699.0 | TP1 4720.0 | Lot 0.02",
                },
                {
                    "label": "plan_phu",
                    "value": 4729.0,
                    "vung_cho": "4728–4730",
                    "side": "SELL",
                    "hop_luu": 70,
                    "trade_line": "SELL LIMIT 4729.0 | SL 4739.0 | TP1 4718.0 | Lot 0.02",
                },
                {
                    "label": "scalp",
                    "value": 4715.0,
                    "vung_cho": "4714–4716",
                    "side": "BUY",
                    "hop_luu": 60,
                    "trade_line": "BUY LIMIT 4715.0 | SL 4709.0 | TP1 4720.0 | Lot 0.01",
                },
            ]
        },
        ensure_ascii=False,
    )

    class Settings:
        coinmap_email = "user@example.com"
        coinmap_password = "secret"
        tradingview_password = "tv-secret"
        openai_vector_store_ids = ["vs_primary"]
        telegram_bot_token = "bot-token"
        telegram_chat_id = "-100111"
        telegram_output_ngan_gon_chat_id = None
        telegram_parse_mode = None
        telegram_python_bot_chat_id = None

    class OpenAIResult:
        def __init__(self, final_response_id: str) -> None:
            self.final_response_id = final_response_id
            self.after_charts = payload

        def full_text(self) -> str:
            return self.after_charts

    def fake_run_openai_flow(*_args, **kwargs):
        openai_calls.append(kwargs)
        return OpenAIResult(f"resp-{len(openai_calls)}")

    def fake_send_openai_output_to_telegram(**kwargs):
        telegram_calls.append(kwargs)

    monkeypatch.setattr(cli, "load_settings", lambda: Settings())
    monkeypatch.setattr(cli, "session_slot_now_hcm", lambda: "sang")
    monkeypatch.setattr(cli, "zones_dir_from_cli_path", lambda _path: zones_dir)
    monkeypatch.setattr(cli, "stop_daemon_plans_in_zones", lambda _zones_dir: None)
    monkeypatch.setattr(cli, "clear_zones_directory", lambda _zones_dir: 0)
    monkeypatch.setattr(cli, "capture_charts", lambda **_kwargs: [chart_json])
    monkeypatch.setattr(cli, "stamp_from_capture_paths", lambda _paths: "20260514_090000")
    monkeypatch.setattr(cli, "list_invalid_chart_slots_for_stamp", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cli, "require_openai", lambda _settings: None)
    monkeypatch.setattr(cli, "require_telegram", lambda _settings: None)
    monkeypatch.setattr(cli, "ordered_chart_openai_payloads", lambda _charts_dir: [("json", chart_json)])
    monkeypatch.setattr(cli, "_warn_if_incomplete_chart_payloads", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_run_openai_flow", fake_run_openai_flow)
    monkeypatch.setattr(cli, "send_openai_output_to_telegram", fake_send_openai_output_to_telegram)
    def fake_write_last_response_id(response_id, path=None):
        if path is None:
            response_ids.append(response_id)
            return
        Path(path).write_text(str(response_id), encoding="utf-8")

    monkeypatch.setattr(cli, "write_last_response_id", fake_write_last_response_id)
    monkeypatch.setattr(cli, "write_last_all_response_id", lambda _response_id: None)
    monkeypatch.setattr(cli, "write_morning_full_analysis", lambda _obj: None)
    monkeypatch.setattr(cli, "write_morning_baseline_prices", lambda _trip: None)
    monkeypatch.setattr(cli, "resolved_openai_model", lambda _settings, model: model)
    monkeypatch.setattr(
        "automation_tool.ea_neverdie_zone_publish.maybe_publish_neverdie_after_cli",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(cli, "sync_accounts_all2_json", lambda _path: None)

    args = _parser().parse_args(
        [
            "all",
            "--charts-dir",
            str(tmp_path),
            "--prompt",
            "test prompt",
        ]
    )
    cli.cmd_all(args)

    assert len(openai_calls) == 2
    assert openai_calls[0]["vector_store_ids"] == with_gocharting_footprint_vector_store_ids(
        ["vs_primary"]
    )
    assert openai_calls[1]["vector_store_ids"] == with_gocharting_footprint_vector_store_ids(
        ["vs_69fa9d55f3b48191b4aea51214b880d6"]
    )
    assert telegram_calls[1]["chat_id"] == "-1003996623506"
    assert (zones_dir / "vung_plan_chinh_sang-2.json").is_file()
    shard = json.loads((zones_dir / "vung_plan_chinh_sang-2.json").read_text(encoding="utf-8"))
    assert shard["zone"]["source"] == "all-2"
    assert response_ids == ["resp-1"]


def test_all_2_standalone_uses_existing_charts_without_capture(monkeypatch, tmp_path: Path) -> None:
    from automation_tool import cli

    stamp = "20260514_090000"
    chart_json = tmp_path / f"{stamp}_coinmap_XAUUSD_5m.json"
    chart_json.write_text("{}", encoding="utf-8")
    openai_calls: list[dict[str, object]] = []
    telegram_calls: list[dict[str, object]] = []
    capture_called = {"n": 0}
    payload = json.dumps(
        {
            "prices": [
                {
                    "label": "plan_chinh",
                    "value": 4709.0,
                    "vung_cho": "4708–4710",
                    "side": "BUY",
                    "hop_luu": 80,
                    "trade_line": "BUY LIMIT 4709.0 | SL 4699.0 | TP1 4720.0 | Lot 0.02",
                },
            ]
        },
        ensure_ascii=False,
    )

    class Settings:
        openai_vector_store_ids = ["vs_primary"]
        telegram_bot_token = "bot-token"
        telegram_chat_id = "-100111"
        telegram_output_ngan_gon_chat_id = None
        telegram_parse_mode = None

    class OpenAIResult:
        final_response_id = "resp-2"
        after_charts = payload

        def full_text(self) -> str:
            return self.after_charts

    def fake_capture(**_kwargs):
        capture_called["n"] += 1
        return []

    def fake_run_openai_flow(*_args, **kwargs):
        openai_calls.append(kwargs)
        return OpenAIResult()

    def fake_send_openai_output_to_telegram(**kwargs):
        telegram_calls.append(kwargs)

    monkeypatch.setattr(cli, "load_settings", lambda: Settings())
    monkeypatch.setattr(cli, "require_openai", lambda _settings: None)
    monkeypatch.setattr(cli, "require_telegram", lambda _settings: None)
    monkeypatch.setattr(cli, "capture_charts", fake_capture)
    monkeypatch.setattr(cli, "_run_openai_flow", fake_run_openai_flow)
    monkeypatch.setattr(cli, "send_openai_output_to_telegram", fake_send_openai_output_to_telegram)
    monkeypatch.setattr(cli, "resolved_openai_model", lambda _settings, model: model)
    monkeypatch.setattr(cli, "sync_accounts_all2_json", lambda _path: None)
    zones_dir = tmp_path / "zones"
    write_calls: list[dict[str, object]] = []

    def fake_write_zones_for_slot(**kwargs):
        write_calls.append(kwargs)

    monkeypatch.setattr(cli, "zones_dir_from_cli_path", lambda _path: zones_dir)
    monkeypatch.setattr(cli, "session_slot_now_hcm", lambda: "sang")
    monkeypatch.setattr(cli, "write_zones_for_slot", fake_write_zones_for_slot)
    monkeypatch.setattr(
        cli,
        "ordered_chart_openai_payloads",
        lambda _charts_dir, stamp=None: [("json", chart_json)],
    )
    monkeypatch.setattr(cli, "_warn_if_incomplete_chart_payloads", lambda *_a, **_k: None)

    args = _parser().parse_args(
        [
            "all-2",
            "--charts-dir",
            str(tmp_path),
            "--stamp",
            stamp,
            "--prompt",
            "retry flow 2",
        ]
    )

    cli.cmd_all_2(args)

    assert capture_called["n"] == 0
    assert len(openai_calls) == 1
    assert openai_calls[0]["vector_store_ids"] == with_gocharting_footprint_vector_store_ids(
        ["vs_69fa9d55f3b48191b4aea51214b880d6"]
    )
    assert telegram_calls[0]["chat_id"] == "-1003996623506"
    assert len(write_calls) == 1
    assert write_calls[0]["shard_suffix"] == "-2"
    assert write_calls[0]["zones"][0].source == "all-2"
