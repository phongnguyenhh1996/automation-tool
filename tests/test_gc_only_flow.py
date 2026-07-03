from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from automation_tool.cli import _all_flow_openai_prompts, _gc_only_gocharting_cfg, cmd_all
from automation_tool.coinmap import apply_main_chart_symbol_to_config, load_coinmap_yaml
from automation_tool.config import default_coinmap_config_path
from automation_tool.gocharting_capture import _filter_capture_plan
from automation_tool.images import (
    GC1_MAIN_SYMBOL,
    extend_openai_payloads_with_footprint_json,
    write_main_chart_symbol_marker,
)
from automation_tool.openai_prompt_flow import (
    default_analysis_prompt,
    full_analysis_footprint_prompt,
    full_analysis_structure_prompt,
)


def test_gc_only_gocharting_cfg_disables_convert(tmp_path: Path) -> None:
    yaml_path = tmp_path / "gocharting.yaml"
    yaml_path.write_text(
        "footprint_ws:\n  enabled: true\n  gc_to_spot:\n    enabled: true\n  mt5_spot: true\n",
        encoding="utf-8",
    )
    cfg = _gc_only_gocharting_cfg(yaml_path)
    assert cfg["footprint_ws"]["gc_to_spot"]["enabled"] is False
    assert cfg["footprint_ws"]["mt5_spot"] is False


def test_gc_only_prompts_include_rules_equivalence() -> None:
    p = default_analysis_prompt(GC1_MAIN_SYMBOL, gc_native_footprint=True)
    assert "GC1!" in p
    assert "Áp dụng hết mọi quy tắc" in p
    assert "footprint_combined" in p
    assert "spot XAUUSD" not in p.lower() or "không dùng giá spot" in p

    s = full_analysis_structure_prompt(GC1_MAIN_SYMBOL, gc_native_footprint=True)
    assert "GC1!" in s
    assert "Áp dụng hết mọi quy tắc" in s

    f = full_analysis_footprint_prompt(GC1_MAIN_SYMBOL, gc_native_footprint=True)
    assert "footprint_combined" in f
    assert "plan_chinh/plan_phu/scalp" in f
    assert "Footprint XAUUSD" not in f


def test_all_flow_openai_prompts_gc_only(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    write_main_chart_symbol_marker(charts, GC1_MAIN_SYMBOL)
    args = Namespace(gc_only=True, prompt=None, no_all_two_phase=False)
    two_phase, prompt_all, structure, footprint = _all_flow_openai_prompts(args, charts)
    assert two_phase is True
    assert prompt_all == ""
    assert structure is not None and "GC1!" in structure
    assert footprint is not None and "footprint_combined" in footprint


def test_extend_payloads_gc_native_footprint(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    fp_dir = charts / "footprint_images"
    fp_dir.mkdir(parents=True)
    (fp_dir / "footprint_combined_15m.json").write_text('{"symbol":"COMEX:GC1!","candles":[]}\n', encoding="utf-8")
    (fp_dir / "footprint_combined_5m.json").write_text('{"symbol":"COMEX:GC1!","candles":[]}\n', encoding="utf-8")
    cfg = {"footprint_ws": {"enabled": True, "gc_to_spot": {"enabled": False}}}
    out = extend_openai_payloads_with_footprint_json([], charts, gocharting_cfg=cfg)
    names = [p.name for kind, p in out if kind == "json"]
    assert "footprint_combined_15m.json" in names
    assert "footprint_combined_5m.json" in names


def test_apply_main_chart_symbol_gc1() -> None:
    cfg = load_coinmap_yaml(default_coinmap_config_path())
    out = apply_main_chart_symbol_to_config(cfg, GC1_MAIN_SYMBOL)
    tv_plan = out["tradingview_capture"]["capture_plan"]
    syms = [r.get("symbol") for r in tv_plan if isinstance(r, dict)]
    assert GC1_MAIN_SYMBOL in syms
    assert "XAUUSD" not in syms
    assert any(r.get("symbol") == "DXY" for r in tv_plan if isinstance(r, dict))


def test_gocharting_filter_xauusd_alias_for_gc1() -> None:
    cfg = {
        "capture_plan": [
            {"symbol": "XAUUSD", "intervals": ["15m", "5m"]},
            {"symbol": "DXY", "intervals": ["15m"]},
        ],
        "symbols": {
            "XAUUSD": {"export_label": "GC"},
            "GC1!": {"export_label": "GC"},
            "DXY": {"export_label": "DXY"},
        },
    }
    plan = _filter_capture_plan(
        cfg,
        capture_symbols=("DXY", GC1_MAIN_SYMBOL),
        capture_intervals=None,
        main_chart_symbol=GC1_MAIN_SYMBOL,
    )
    syms = [sym for _step, sym, _ivs in plan]
    assert "XAUUSD" in syms
    assert "DXY" in syms


def test_cmd_all_gc_only_skips_side_effects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """gc-only aborts early on missing capture; verify zones/last_response not touched when mocked through."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    args = Namespace(
        gc_only=True,
        gocharting=True,
        gocharting_config=None,
        main_symbol=None,
        config=None,
        charts_dir=tmp_path / "charts",
        storage_state=None,
        no_save_storage=True,
        headed=False,
        no_tradingview=True,
        no_telegram=True,
        no_clear_zones_state=False,
        zones_json=None,
        max_images_per_call=100,
        prompt=None,
        no_all_two_phase=False,
        model=None,
        mt5_accounts_json=None,
    )
    with patch("automation_tool.cli.load_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            openai_api_key="sk-test",
            coinmap_email="",
            coinmap_password="",
            tradingview_password="",
            gocharting_email="e",
            gocharting_password="p",
            telegram_bot_token="",
            telegram_chat_id="",
            telegram_log_chat_id="",
            telegram_output_ngan_gon_chat_id="",
            telegram_parse_mode="HTML",
        )
        with patch("automation_tool.cli.capture_gocharting", return_value=[]):
            with patch("automation_tool.cli.clear_zones_directory") as mock_clear:
                with patch("automation_tool.cli.write_last_response_id") as mock_last:
                    with pytest.raises(SystemExit, match="No chart artifacts"):
                        cmd_all(args)
                    mock_clear.assert_not_called()
                    mock_last.assert_not_called()
