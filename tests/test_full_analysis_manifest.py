from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from automation_tool.cli import cmd_capture_full_analysis
from automation_tool.full_analysis_manifest import build_full_analysis_manifest, manifest_to_json
from automation_tool.images import GOCHARTING_GOLD_EXPORT_LABEL, write_main_chart_symbol_marker


def _valid_gocharting_csv(path: Path) -> None:
    path.write_text("Time,Open,High,Low,Close\n1,2,3,4,5\n", encoding="utf-8")


def _write_full_gocharting_capture(charts: Path, stamp: str = "20260629_120000") -> None:
    write_main_chart_symbol_marker(charts, "XAUUSD")
    for sym, intervals in (
        ("DXY", ("4h", "1h", "15m")),
        ("XAUUSD", ("4h", "1h", "15m", "15m_ict", "5m")),
    ):
        for iv in intervals:
            (charts / f"{stamp}_tradingview_{sym}_{iv}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    for sym, iv in (("DXY", "15m"), (GOCHARTING_GOLD_EXPORT_LABEL, "15m"), (GOCHARTING_GOLD_EXPORT_LABEL, "5m")):
        csv_p = charts / f"{stamp}_gocharting_{sym}_{iv}.csv"
        _valid_gocharting_csv(csv_p)
        (charts / f"{stamp}_gocharting_{sym}_{iv}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    fp_dir = charts / "footprint_images"
    fp_dir.mkdir(parents=True, exist_ok=True)
    for iv in ("15m", "5m"):
        doc = {"symbol": "GC1!", "interval": iv, "candles": [{"time_gmt7": "t", "footprint": [], "ohlc": {}}]}
        (fp_dir / f"footprint_combined_{iv}.json").write_text(json.dumps(doc), encoding="utf-8")


def test_build_manifest_ready_for_analysis(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    stamp = "20260629_120000"
    _write_full_gocharting_capture(charts, stamp)

    manifest = build_full_analysis_manifest(charts, stamp=stamp)
    assert manifest["stamp"] == stamp
    assert manifest["main_symbol"] == "XAUUSD"
    assert len(manifest["slots"]) == 11
    assert all(s["status"] == "ok" for s in manifest["slots"])
    assert len(manifest["footprint_json"]) == 2
    assert manifest["ready_for_analysis"] is True

    parsed = json.loads(manifest_to_json(manifest))
    assert parsed["ready_for_analysis"] is True


def test_build_manifest_missing_slots(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    write_main_chart_symbol_marker(charts, "XAUUSD")

    manifest = build_full_analysis_manifest(charts, stamp="20260629_120000")
    assert manifest["ready_for_analysis"] is False
    assert any(s["status"] != "ok" for s in manifest["slots"])
    assert manifest["footprint_json"] == []


def test_build_manifest_no_stamp(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    manifest = build_full_analysis_manifest(charts)
    assert manifest["stamp"] is None
    assert manifest["ready_for_analysis"] is False
    assert "error" in manifest


def test_build_manifest_legacy_requires_knowledge(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    stamp = "20260629_120000"
    _write_full_gocharting_capture(charts, stamp)

    manifest = build_full_analysis_manifest(
        charts,
        stamp=stamp,
        legacy=True,
        knowledge_dir=tmp_path / "knowledge",
        vector_store_ids=["vs_test"],
    )
    assert manifest["analysis_mode"] == "legacy"
    assert manifest["knowledge_ready"] is False
    assert manifest["ready_for_analysis"] is False


def test_build_manifest_legacy_with_synced_knowledge(tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    stamp = "20260629_120000"
    _write_full_gocharting_capture(charts, stamp)

    knowledge = tmp_path / "knowledge"
    files_dir = knowledge / "files"
    files_dir.mkdir(parents=True)
    local = files_dir / "vs_test__file-1__rules.md"
    local.write_text("# rules", encoding="utf-8")
    (knowledge / "manifest.json").write_text(
        json.dumps(
            {
                "ready": True,
                "vector_store_ids": ["vs_test"],
                "files": [
                    {
                        "vector_store_id": "vs_test",
                        "file_id": "file-1",
                        "filename": "rules.md",
                        "local_path": str(local),
                        "skipped": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    manifest = build_full_analysis_manifest(
        charts,
        stamp=stamp,
        legacy=True,
        knowledge_dir=knowledge,
        vector_store_ids=["vs_test"],
    )
    assert manifest["knowledge_ready"] is True
    assert manifest["ready_for_analysis"] is True
    assert len(manifest["knowledge_files"]) == 1


def test_cmd_capture_full_analysis_no_openai(tmp_path: Path, monkeypatch) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    write_main_chart_symbol_marker(charts, "XAUUSD")
    stamp = "20260629_130000"

    def fake_capture_charts(**kwargs):
        assert kwargs.get("enable_coinmap") is False
        assert kwargs.get("enable_tradingview") is True
        assert kwargs.get("main_chart_symbol") is None
        p = charts / f"{stamp}_tradingview_DXY_4h.png"
        p.write_bytes(b"x")
        return [p]

    def fake_capture_gocharting(**kwargs):
        assert kwargs.get("main_chart_symbol") is None
        _write_full_gocharting_capture(charts, stamp)
        return list(charts.glob(f"{stamp}_*"))

    monkeypatch.setattr("automation_tool.cli.capture_charts", fake_capture_charts)
    monkeypatch.setattr("automation_tool.cli.capture_gocharting", fake_capture_gocharting)
    monkeypatch.setattr(
        "automation_tool.cli.load_settings",
        lambda: MagicMock(
            coinmap_email="a",
            coinmap_password="b",
            gocharting_email="gc@x.com",
            gocharting_password="pw",
            tradingview_password="tv",
        ),
    )

    openai_called = False

    def fake_require_openai(*_args, **_kwargs):
        nonlocal openai_called
        openai_called = True

    monkeypatch.setattr("automation_tool.cli.require_openai", fake_require_openai)

    args = Namespace(
        gocharting=True,
        config=tmp_path / "coinmap.yaml",
        charts_dir=charts,
        storage_state=None,
        no_save_storage=True,
        headed=False,
        gocharting_config=None,
    )

    cmd_capture_full_analysis(args)
    assert openai_called is False


def test_cmd_capture_full_analysis_requires_gocharting(tmp_path: Path) -> None:
    args = Namespace(gocharting=False)
    with pytest.raises(SystemExit, match="requires --gocharting"):
        cmd_capture_full_analysis(args)
