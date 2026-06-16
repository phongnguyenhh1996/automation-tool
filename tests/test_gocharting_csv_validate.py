from __future__ import annotations

from pathlib import Path

import pytest

from automation_tool.chart_payload_validate import (
    normalize_gocharting_csv_file,
    normalize_gocharting_csv_text,
    prepare_gocharting_csv_file,
    require_valid_gocharting_csv_paths,
    trim_gocharting_csv_candles,
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


def test_trim_gocharting_csv_candles_keeps_newest() -> None:
    rows = [f"2026-01-01 {i:02d}:00,1,2" for i in range(200)]
    raw = "Date,Open,High\n" + "\n".join(rows) + "\n"
    out = trim_gocharting_csv_candles(raw, max_candles=150)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines[0] == "Date,Open,High"
    assert len(lines) == 151
    assert lines[1] == "2026-01-01 50:00,1,2"
    assert lines[-1] == "2026-01-01 199:00,1,2"


def test_trim_gocharting_csv_candles_noop_when_few_rows() -> None:
    raw = "Date,Open\n2026-01-01,1\n2026-01-02,2\n"
    out = trim_gocharting_csv_candles(raw, max_candles=150)
    assert out == normalize_gocharting_csv_text(raw)


def test_trim_gocharting_csv_candles_strips_branding() -> None:
    raw = (
        "www.gocharting.com\n"
        "Date,Open\n"
        + "\n".join(f"t{i},1" for i in range(160))
        + "\n"
    )
    out = trim_gocharting_csv_candles(raw, max_candles=150)
    assert "www.gocharting.com" not in out
    assert len([ln for ln in out.splitlines() if ln.strip()]) == 151


def test_normalize_gocharting_csv_file_rewrites(tmp_path: Path) -> None:
    p = tmp_path / "branded.csv"
    p.write_text(
        "www.gocharting.com\nDate,Open\n2026-01-01,1\n",
        encoding="utf-8",
    )
    assert normalize_gocharting_csv_file(p) is True
    assert p.read_text(encoding="utf-8").startswith("Date,Open\n")
    assert normalize_gocharting_csv_file(p) is False


def test_prepare_gocharting_csv_file_trims_on_disk(tmp_path: Path) -> None:
    p = tmp_path / "long.csv"
    rows = [f"2026-01-01 {i:02d}:00,1,2" for i in range(200)]
    p.write_text("Date,Open,High\n" + "\n".join(rows) + "\n", encoding="utf-8")
    assert prepare_gocharting_csv_file(p, max_candles=150) is True
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 151
    assert lines[1] == "2026-01-01 50:00,1,2"
    assert prepare_gocharting_csv_file(p, max_candles=150) is False
