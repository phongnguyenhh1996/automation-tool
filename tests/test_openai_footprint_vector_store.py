from __future__ import annotations

from pathlib import Path

import pytest

from automation_tool.openai_footprint_vector_store import (
    FOOTPRINT_VECTOR_STORE_FILE_EXPIRES_SECONDS,
    GOCHARTING_FOOTPRINT_M15_VECTOR_STORE_ID,
    GOCHARTING_FOOTPRINT_M5_VECTOR_STORE_ID,
    GOCHARTING_FOOTPRINT_VECTOR_STORE_IDS,
    footprint_vector_store_id_for_interval,
    merge_vector_store_ids,
    upload_footprint_image_to_vector_store,
    with_gocharting_footprint_vector_store_ids,
)


def test_footprint_vector_store_id_for_interval() -> None:
    assert footprint_vector_store_id_for_interval("5m") == GOCHARTING_FOOTPRINT_M5_VECTOR_STORE_ID
    assert footprint_vector_store_id_for_interval("15m") == GOCHARTING_FOOTPRINT_M15_VECTOR_STORE_ID


def test_footprint_vector_store_id_for_interval_unknown() -> None:
    with pytest.raises(ValueError, match="no footprint vector store"):
        footprint_vector_store_id_for_interval("1h")


def test_merge_vector_store_ids_dedupes_preserving_order() -> None:
    merged = merge_vector_store_ids(
        ["vs_a", GOCHARTING_FOOTPRINT_M5_VECTOR_STORE_ID],
        extra=[GOCHARTING_FOOTPRINT_M5_VECTOR_STORE_ID, GOCHARTING_FOOTPRINT_M15_VECTOR_STORE_ID],
    )
    assert merged == [
        "vs_a",
        GOCHARTING_FOOTPRINT_M5_VECTOR_STORE_ID,
        GOCHARTING_FOOTPRINT_M15_VECTOR_STORE_ID,
    ]


def test_with_gocharting_footprint_vector_store_ids_appends_both() -> None:
    assert with_gocharting_footprint_vector_store_ids(["vs_primary"]) == [
        "vs_primary",
        *GOCHARTING_FOOTPRINT_VECTOR_STORE_IDS,
    ]


def test_upload_footprint_image_to_vector_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    image = tmp_path / "20250624_9h55m_5m.png"
    image.write_bytes(b"png")

    class _Files:
        def create(self, **kwargs):
            assert kwargs["purpose"] == "assistants"
            assert kwargs["expires_after"] == {
                "anchor": "created_at",
                "seconds": FOOTPRINT_VECTOR_STORE_FILE_EXPIRES_SECONDS,
            }

            class _File:
                id = "file_test123"

            return _File()

    class _VsFiles:
        def create_and_poll(self, **kwargs):
            assert kwargs["vector_store_id"] == GOCHARTING_FOOTPRINT_M5_VECTOR_STORE_ID
            assert kwargs["file_id"] == "file_test123"
            return kwargs

    class _VectorStores:
        files = _VsFiles()

    class _Client:
        files = _Files()
        vector_stores = _VectorStores()

    monkeypatch.setattr(
        "automation_tool.openai_footprint_vector_store.OpenAI",
        lambda api_key: _Client(),
    )

    file_id = upload_footprint_image_to_vector_store(
        api_key="sk-test",
        image_path=image,
        interval="5m",
    )
    assert file_id == "file_test123"


def test_upload_footprint_image_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        upload_footprint_image_to_vector_store(
            api_key="sk-test",
            image_path=tmp_path / "missing.png",
            interval="15m",
        )


def test_append_footprint_vector_store_hint_appends_when_enabled() -> None:
    from automation_tool.openai_footprint_vector_store import (
        GOCHARTING_FOOTPRINT_M5_VECTOR_STORE_ID,
        append_footprint_vector_store_hint,
    )

    out = append_footprint_vector_store_hint(
        "[FULL_ANALYSIS]\nCặp chính: XAUUSD.",
        [GOCHARTING_FOOTPRINT_M5_VECTOR_STORE_ID],
    )
    assert "file_search" in out
    assert "9h55m" in out
    assert GOCHARTING_FOOTPRINT_M5_VECTOR_STORE_ID in out


def test_append_footprint_vector_store_hint_skips_unrelated_stores() -> None:
    from automation_tool.openai_footprint_vector_store import append_footprint_vector_store_hint

    base = "[INTRADAY_UPDATE]\nTest."
    assert append_footprint_vector_store_hint(base, ["vs_other"]) == base


def test_append_footprint_vector_store_hint_idempotent() -> None:
    from automation_tool.openai_footprint_vector_store import (
        GOCHARTING_FOOTPRINT_VECTOR_STORE_IDS,
        append_footprint_vector_store_hint,
    )

    once = append_footprint_vector_store_hint("hello", GOCHARTING_FOOTPRINT_VECTOR_STORE_IDS)
    twice = append_footprint_vector_store_hint(once, GOCHARTING_FOOTPRINT_VECTOR_STORE_IDS)
    assert once == twice


def test_upload_footprint_image_to_vector_store_with_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from automation_tool.openai_footprint_vector_store import (
        upload_footprint_image_to_vector_store_with_retry,
    )

    image = tmp_path / "20250624_9h55m_5m.png"
    image.write_bytes(b"png")
    calls = {"n": 0}
    slept: list[float] = []

    def fake_upload(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return "file_retry_ok"

    monkeypatch.setattr(
        "automation_tool.openai_footprint_vector_store.upload_footprint_image_to_vector_store",
        fake_upload,
    )
    monkeypatch.setattr(
        "automation_tool.openai_footprint_vector_store.time.sleep",
        lambda s: slept.append(s),
    )

    file_id = upload_footprint_image_to_vector_store_with_retry(
        api_key="sk-test",
        image_path=image,
        interval="5m",
        retry_seconds=30,
    )
    assert file_id == "file_retry_ok"
    assert calls["n"] == 2
    assert slept == [30]
