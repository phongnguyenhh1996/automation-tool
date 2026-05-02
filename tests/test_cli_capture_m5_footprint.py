from __future__ import annotations

from pathlib import Path

from automation_tool.cli import _parser


def test_capture_m5_footprint_parser_defaults_to_update_config() -> None:
    args = _parser().parse_args(["capture-m5-footprint"])

    assert args.command == "capture-m5-footprint"
    assert args.config is None
    assert args.charts_dir is None
    assert args.storage_state is None
    assert args.no_save_storage is False
    assert args.headed is False
    assert args.main_symbol is None


def test_capture_m5_footprint_captures_only_coinmap_m5_and_writes_merged(
    monkeypatch, tmp_path: Path
) -> None:
    from automation_tool import cli

    raw = tmp_path / "20260501_233000_coinmap_XAUUSD_5m.json"
    raw.write_text("[]", encoding="utf-8")
    merged = tmp_path / "20260501_233000_coinmap_XAUUSD_5m_openai_coinmap_merged.json"
    calls: dict[str, object] = {}

    class Settings:
        coinmap_email = "user@example.com"
        coinmap_password = "secret"
        tradingview_password = "tv-secret"

    def fake_capture_charts(**kwargs):
        calls["capture"] = kwargs
        return [raw]

    def fake_write_openai_merged(path):
        calls["merged_raw"] = path
        merged.write_text('{"source":"coinmap_merged"}', encoding="utf-8")
        return merged

    monkeypatch.setattr(cli, "load_settings", lambda: Settings())
    monkeypatch.setattr(cli, "capture_charts", fake_capture_charts)
    monkeypatch.setattr(
        cli,
        "write_openai_coinmap_merged_from_raw_export",
        fake_write_openai_merged,
        raising=False,
    )

    args = _parser().parse_args(["capture-m5-footprint", "--charts-dir", str(tmp_path)])
    cli.cmd_capture_m5_footprint(args)

    capture = calls["capture"]
    assert isinstance(capture, dict)
    assert capture["charts_dir"] == tmp_path
    assert capture["enable_coinmap"] is True
    assert capture["enable_tradingview"] is False
    assert capture["clear_charts_before_capture"] is True
    assert capture["coinmap_capture_intervals"] == ("5m",)
    assert calls["merged_raw"] == raw
