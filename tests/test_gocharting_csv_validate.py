from __future__ import annotations

from pathlib import Path

import pytest

from automation_tool.chart_payload_validate import (
    normalize_gocharting_csv_file,
    normalize_gocharting_csv_text,
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


def test_validate_gocharting_csv_skips_branding_prefix(tmp_path: Path) -> None:
    p = tmp_path / "branded.csv"
    p.write_text(
        "www.gocharting.com\n\n"
        "Date,Open,High,Low,Close\n"
        '"Mon Apr 06 2026 04:30:00 GMT+0700","100.1","100.2","100.0","100.15"\n',
        encoding="utf-8",
    )
    ok, reason = validate_gocharting_csv_file(p)
    assert ok, reason


def test_normalize_gocharting_csv_text_strips_prefix() -> None:
    raw = (
        "www.gocharting.com\n\n"
        "Date,Open,High\n"
        '"Mon Apr 06 2026 04:30:00 GMT+0700","100.1","100.2"\n'
    )
    out = normalize_gocharting_csv_text(raw)
    assert out.startswith("Date,Open,High\n")
    assert "www.gocharting.com" not in out


def test_normalize_gocharting_csv_file_rewrites(tmp_path: Path) -> None:
    p = tmp_path / "branded.csv"
    p.write_text(
        "www.gocharting.com\nDate,Open\n2026-01-01,1\n",
        encoding="utf-8",
    )
    assert normalize_gocharting_csv_file(p) is True
    assert p.read_text(encoding="utf-8").startswith("Date,Open\n")
    assert normalize_gocharting_csv_file(p) is False
