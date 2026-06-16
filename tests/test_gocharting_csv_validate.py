from __future__ import annotations

from pathlib import Path

import pytest

from automation_tool.chart_payload_validate import (
    require_valid_gocharting_csv_paths,
    validate_gocharting_csv_file,
)


def test_validate_gocharting_csv_file_ok(tmp_path: Path) -> None:
    p = tmp_path / "sample.csv"
    p.write_text("Time,Open,High\n2026-01-01,1,2\n", encoding="utf-8")
    ok, reason = validate_gocharting_csv_file(p)
    assert ok is True
    assert reason == ""


def test_validate_gocharting_csv_file_empty(tmp_path: Path) -> None:
    p = tmp_path / "empty.csv"
    p.write_text("", encoding="utf-8")
    ok, reason = validate_gocharting_csv_file(p)
    assert ok is False
    assert "empty" in reason.lower()


def test_validate_gocharting_fixture() -> None:
    fixture = Path(__file__).resolve().parent / "fixtures" / "gocharting_sample.csv"
    ok, reason = validate_gocharting_csv_file(fixture)
    assert ok, reason


def test_require_valid_gocharting_csv_paths_exits(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("onlyheader\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="GoCharting CSV validation failed"):
        require_valid_gocharting_csv_paths([bad])
