"""
Download OpenAI vector store files to a local knowledge folder for offline agent analysis.

Used by FULL_ANALYSIS legacy mode (replaces ``master_trading_playbook.md`` as knowledge source).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from automation_tool.config import default_vector_store_knowledge_dir

MANIFEST_FILENAME = "manifest.json"


class _VectorStoreFilesClient(Protocol):
    def list(self, vector_store_id: str, **kwargs: Any) -> Any: ...


class _FilesClient(Protocol):
    def retrieve(self, file_id: str) -> Any: ...

    def content(self, file_id: str) -> Any: ...


class _OpenAIClient(Protocol):
    vector_stores: Any
    files: _FilesClient


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sanitize_knowledge_filename(name: str, *, fallback: str = "file") -> str:
    base = Path(name).name.strip() or fallback
    base = re.sub(r"[^\w.\-()+ ]+", "_", base)
    base = base.strip("._") or fallback
    return base[:180]


def knowledge_manifest_path(output_dir: Path) -> Path:
    return Path(output_dir) / MANIFEST_FILENAME


def load_knowledge_manifest(output_dir: Path) -> dict[str, Any] | None:
    path = knowledge_manifest_path(output_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def knowledge_is_ready(
    output_dir: Path,
    *,
    vector_store_ids: Iterable[str] | None = None,
) -> bool:
    manifest = load_knowledge_manifest(output_dir)
    if not manifest or not manifest.get("ready"):
        return False
    if vector_store_ids is None:
        return bool(manifest.get("files"))
    expected = {str(v).strip() for v in vector_store_ids if str(v).strip()}
    got = {str(v).strip() for v in (manifest.get("vector_store_ids") or []) if str(v).strip()}
    if expected and got != expected:
        return False
    return bool(manifest.get("files"))


def _iter_vector_store_files(
    client: _OpenAIClient,
    vector_store_id: str,
) -> list[Any]:
    pager = client.vector_stores.files.list(vector_store_id=vector_store_id, limit=100)
    items: list[Any] = []
    for item in pager:
        items.append(item)
    return items


def _download_vector_store_parsed_text(
    client: _OpenAIClient,
    vector_store_id: str,
    file_id: str,
) -> str:
    pager = client.vector_stores.files.content(
        vector_store_id=vector_store_id,
        file_id=file_id,
    )
    chunks: list[str] = []
    # SyncPage yields FileContentResponse items; first page also exposes .data
    if getattr(pager, "data", None):
        for item in pager.data:
            text = getattr(item, "text", None)
            if text is None and isinstance(item, dict):
                text = item.get("text")
            if text:
                chunks.append(str(text))
    for item in pager:
        text = getattr(item, "text", None)
        if text is None and isinstance(item, dict):
            text = item.get("text")
        if text:
            chunks.append(str(text))
    # dedupe while preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for chunk in chunks:
        if chunk not in seen:
            seen.add(chunk)
            ordered.append(chunk)
    return "\n".join(ordered).strip()


def _download_file_content(
    client: _OpenAIClient,
    file_id: str,
    *,
    vector_store_id: str | None = None,
) -> tuple[bytes, str]:
    """
    Return ``(content_bytes, content_source)``.

    ``files.content`` works for user_data; assistants-purpose files must use
    ``vector_stores.files.content`` (parsed text chunks).
    """
    try:
        response = client.files.content(file_id)
        if hasattr(response, "read"):
            return response.read(), "files_api"
        if hasattr(response, "content"):
            return response.content, "files_api"
        if isinstance(response, (bytes, bytearray)):
            return bytes(response), "files_api"
        raise TypeError(f"Unsupported files.content response type: {type(response)!r}")
    except Exception as exc:
        message = str(exc).lower()
        if "purpose" not in message and "not allowed to download" not in message:
            raise
        if not vector_store_id:
            raise
        text = _download_vector_store_parsed_text(client, vector_store_id, file_id)
        if not text:
            raise RuntimeError(f"No parsed content returned for {vector_store_id}/{file_id}") from exc
        return text.encode("utf-8"), "vector_store_parsed"



def sync_vector_store_knowledge(
    *,
    vector_store_ids: list[str],
    output_dir: Path | None = None,
    client: _OpenAIClient | None = None,
    api_key: str | None = None,
    list_files: Callable[[_OpenAIClient, str], list[Any]] | None = None,
    download_content: Callable[[_OpenAIClient, str], bytes] | None = None,
) -> dict[str, Any]:
    """
    Download all completed files from each vector store into ``output_dir/files/``.

    Writes ``manifest.json`` at ``output_dir`` root. Returns the manifest dict.
    """
    if not vector_store_ids:
        raise ValueError("vector_store_ids is empty")

    out_root = Path(output_dir or default_vector_store_knowledge_dir())
    files_dir = out_root / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    if client is None:
        from openai import OpenAI

        key = (api_key or "").strip()
        if not key:
            raise ValueError("OPENAI_API_KEY is required to sync vector store knowledge")
        client = OpenAI(api_key=key)

    list_fn = list_files or _iter_vector_store_files

    entries: list[dict[str, Any]] = []
    errors: list[str] = []

    for vs_id in vector_store_ids:
        vs_id = vs_id.strip()
        if not vs_id:
            continue
        try:
            vs_files = list_fn(client, vs_id)
        except Exception as exc:  # noqa: BLE001 — surface per-store failure in manifest
            errors.append(f"{vs_id}: list failed: {exc}")
            continue

        for vs_file in vs_files:
            file_id = str(getattr(vs_file, "id", "") or "").strip()
            status = str(getattr(vs_file, "status", "") or "").strip()
            if not file_id:
                continue
            if status and status != "completed":
                entries.append(
                    {
                        "vector_store_id": vs_id,
                        "file_id": file_id,
                        "filename": file_id,
                        "local_path": "",
                        "bytes": 0,
                        "status": status,
                        "skipped": True,
                    }
                )
                continue
            try:
                meta = client.files.retrieve(file_id)
                filename = sanitize_knowledge_filename(
                    str(getattr(meta, "filename", "") or file_id),
                    fallback=file_id,
                )
                if download_content is not None:
                    content = download_content(client, file_id)
                    content_source = "test_stub"
                else:
                    content, content_source = _download_file_content(
                        client,
                        file_id,
                        vector_store_id=vs_id,
                    )
                if content_source == "vector_store_parsed" and not filename.lower().endswith(".txt"):
                    stem = Path(filename).stem or file_id
                    filename = f"{stem}.txt"
                local_name = f"{vs_id}__{file_id}__{filename}"
                local_path = files_dir / local_name
                local_path.write_bytes(content)
                entries.append(
                    {
                        "vector_store_id": vs_id,
                        "file_id": file_id,
                        "filename": filename,
                        "local_path": str(local_path.resolve()),
                        "bytes": len(content),
                        "status": status or "completed",
                        "skipped": False,
                        "content_source": content_source,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{vs_id}/{file_id}: download failed: {exc}")

    ready = bool(entries) and all(not e.get("skipped") for e in entries if e.get("local_path"))
    if entries and not any(e.get("local_path") for e in entries):
        ready = False

    manifest: dict[str, Any] = {
        "synced_at": _utc_now_iso(),
        "vector_store_ids": [v.strip() for v in vector_store_ids if v.strip()],
        "output_dir": str(out_root.resolve()),
        "files": entries,
        "ready": ready and not errors,
        "errors": errors,
    }
    knowledge_manifest_path(out_root).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def manifest_to_json(manifest: dict[str, Any], *, indent: int = 2) -> str:
    return json.dumps(manifest, ensure_ascii=False, indent=indent)


def list_knowledge_text_paths(output_dir: Path) -> list[Path]:
    """Return local knowledge files (sorted) for agent reading in legacy mode."""
    manifest = load_knowledge_manifest(output_dir)
    if not manifest:
        return []
    paths: list[Path] = []
    for item in manifest.get("files") or []:
        if not isinstance(item, dict) or item.get("skipped"):
            continue
        raw = str(item.get("local_path") or "").strip()
        if not raw:
            continue
        p = Path(raw)
        if p.is_file():
            paths.append(p)
    return sorted(paths, key=lambda p: p.name.lower())
