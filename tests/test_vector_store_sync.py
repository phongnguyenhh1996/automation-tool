from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from automation_tool.vector_store_sync import (
    knowledge_is_ready,
    sanitize_knowledge_filename,
    sync_vector_store_knowledge,
)


class _FakePager:
    def __init__(self, items: list[object]) -> None:
        self._items = items

    def __iter__(self):
        return iter(self._items)


class _FakeClient:
    def __init__(self, *, files_by_vs: dict[str, list[object]], payloads: dict[str, bytes]) -> None:
        self._files_by_vs = files_by_vs
        self._payloads = payloads
        self.vector_stores = SimpleNamespace(files=SimpleNamespace(list=self._list))
        self.files = SimpleNamespace(retrieve=self._retrieve, content=self._content)

    def _list(self, vector_store_id: str, **kwargs: object) -> _FakePager:
        return _FakePager(self._files_by_vs.get(vector_store_id, []))

    def _retrieve(self, file_id: str) -> SimpleNamespace:
        return SimpleNamespace(filename=f"{file_id}.md")

    def _content(self, file_id: str) -> SimpleNamespace:
        return SimpleNamespace(read=lambda: self._payloads[file_id])


def test_sanitize_knowledge_filename() -> None:
    assert sanitize_knowledge_filename("playbook/rules.md") == "rules.md"
    assert sanitize_knowledge_filename("***") == "file"


def test_sync_vector_store_knowledge_writes_manifest(tmp_path: Path) -> None:
    client = _FakeClient(
        files_by_vs={
            "vs_a": [SimpleNamespace(id="file-1", status="completed")],
        },
        payloads={"file-1": b"# legacy rules\n"},
    )
    manifest = sync_vector_store_knowledge(
        vector_store_ids=["vs_a"],
        output_dir=tmp_path,
        client=client,
    )
    assert manifest["ready"] is True
    assert len(manifest["files"]) == 1
    local = Path(manifest["files"][0]["local_path"])
    assert local.is_file()
    assert local.read_bytes() == b"# legacy rules\n"
    assert (tmp_path / "manifest.json").is_file()
    assert knowledge_is_ready(tmp_path, vector_store_ids=["vs_a"])


def test_sync_vector_store_knowledge_skips_in_progress(tmp_path: Path) -> None:
    client = _FakeClient(
        files_by_vs={
            "vs_a": [SimpleNamespace(id="file-1", status="in_progress")],
        },
        payloads={},
    )
    manifest = sync_vector_store_knowledge(
        vector_store_ids=["vs_a"],
        output_dir=tmp_path,
        client=client,
    )
    assert manifest["ready"] is False
    assert manifest["files"][0]["skipped"] is True


def test_sync_vector_store_knowledge_requires_ids() -> None:
    with pytest.raises(ValueError, match="empty"):
        sync_vector_store_knowledge(vector_store_ids=[], output_dir=Path("/tmp/x"))
