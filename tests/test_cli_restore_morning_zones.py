from __future__ import annotations

import json
from pathlib import Path

import pytest

from automation_tool.cli import _parser


def test_restore_morning_zones_rebuilds_shards_without_reconciling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from automation_tool import cli

    morning_path = tmp_path / "morning_full_analysis.json"
    zones_dir = tmp_path / "zones"
    morning_path.write_text(
        json.dumps(
            {
                "prices": [
                    {
                        "label": "plan_chinh",
                        "value": 4709.0,
                        "vung_cho": "4708–4710",
                        "hop_luu": 80,
                        "trade_line": "BUY LIMIT 4709.0 | SL 4699.0 | TP1 4720.0 | Lot 0.02",
                    },
                    {
                        "label": "plan_phu",
                        "value": 4729.0,
                        "vung_cho": "4728–4730",
                        "hop_luu": 70,
                        "trade_line": "SELL LIMIT 4729.0 | SL 4739.0 | TP1 4718.0 | Lot 0.02",
                    },
                    {
                        "label": "scalp",
                        "value": 4715.0,
                        "vung_cho": "4714–4716",
                        "hop_luu": 60,
                        "trade_line": "BUY LIMIT 4715.0 | SL 4709.0 | TP1 4720.0 | Lot 0.01",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    reconcile_calls = {"n": 0}

    monkeypatch.setattr("automation_tool.images.set_active_main_symbol_file", lambda _sym: None)
    monkeypatch.setattr(
        cli,
        "reconcile_daemon_plans_at_boot",
        lambda _zones_dir_arg: reconcile_calls.__setitem__("n", reconcile_calls["n"] + 1) or 2,
    )

    args = _parser().parse_args(
        [
            "restore-morning-zones",
            "--main-symbol",
            "XAUUSD",
            "--morning-json",
            str(morning_path),
            "--zones-json",
            str(zones_dir),
            "--slot",
            "sang",
        ]
    )

    cli.cmd_restore_morning_zones(args)

    assert reconcile_calls["n"] == 0
    shard = json.loads((zones_dir / "vung_plan_chinh_sang.json").read_text(encoding="utf-8"))
    assert shard["slot"] == "sang"
    assert shard["zone"]["label"] == "plan_chinh"
    assert shard["zone"]["source"] == "all"
    manifest = json.loads((zones_dir / "zones_manifest.json").read_text(encoding="utf-8"))
    assert manifest["last_write_slot"] == "sang"


def test_restore_morning_zones_requires_prices(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from automation_tool import cli

    morning_path = tmp_path / "morning_full_analysis.json"
    morning_path.write_text(json.dumps({"context": {"bias": "bullish"}}), encoding="utf-8")

    monkeypatch.setattr("automation_tool.images.set_active_main_symbol_file", lambda _sym: None)

    args = _parser().parse_args(
        [
            "restore-morning-zones",
            "--main-symbol",
            "XAUUSD",
            "--morning-json",
            str(morning_path),
            "--zones-json",
            str(tmp_path / "zones"),
        ]
    )

    with pytest.raises(SystemExit, match="không có `prices`"):
        cli.cmd_restore_morning_zones(args)


def test_restore_morning_zones_defaults_to_active_symbol(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from automation_tool import cli

    morning_path = tmp_path / "morning_full_analysis.json"
    morning_path.write_text(
        json.dumps(
            {
                "prices": [
                    {
                        "label": "plan_chinh",
                        "value": 2685.0,
                        "vung_cho": "2684–2686",
                        "hop_luu": 75,
                        "trade_line": "BUY LIMIT 2685.0 | SL 2678.0 | TP1 2692.0 | Lot 0.02",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("automation_tool.images.get_active_main_symbol", lambda: "USDJPY")
    monkeypatch.setattr(cli, "reconcile_daemon_plans_at_boot", lambda _zones_dir: 0)

    args = _parser().parse_args(
        [
            "restore-morning-zones",
            "--morning-json",
            str(morning_path),
            "--zones-json",
            str(tmp_path / "zones"),
        ]
    )

    cli.cmd_restore_morning_zones(args)

    shard = json.loads((tmp_path / "zones" / "vung_plan_chinh_sang.json").read_text(encoding="utf-8"))
    assert shard["symbol"] == "USDJPY"
