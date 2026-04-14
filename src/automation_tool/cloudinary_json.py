"""
Upload Coinmap/TradingView JSON to Cloudinary (raw) for OpenAI Responses ``input_file`` ``file_url``.

Requires ``CLOUDINARY_CLOUD_NAME``, ``CLOUDINARY_API_KEY``, ``CLOUDINARY_API_SECRET``.
Optional ``CLOUDINARY_JSON_FOLDER`` (default ``automation_tool/coinmap_json``).
"""

from __future__ import annotations

import io
import logging
import os
import uuid
from pathlib import Path

import cloudinary
import cloudinary.api
import cloudinary.uploader
from cloudinary.exceptions import Error as CloudinaryError

_log = logging.getLogger(__name__)

_DEFAULT_JSON_FOLDER = "automation_tool/coinmap_json"
_configured = False


def _json_folder() -> str:
    raw = (os.getenv("CLOUDINARY_JSON_FOLDER") or "").strip()
    return raw.strip("/") if raw else _DEFAULT_JSON_FOLDER


def ensure_cloudinary_config() -> None:
    """Set ``cloudinary.config`` from env; idempotent."""
    global _configured
    if _configured:
        return
    cloud_name = (os.getenv("CLOUDINARY_CLOUD_NAME") or "").strip()
    api_key = (os.getenv("CLOUDINARY_API_KEY") or "").strip()
    api_secret = (os.getenv("CLOUDINARY_API_SECRET") or "").strip()
    if not cloud_name or not api_key or not api_secret:
        raise SystemExit(
            "Cloudinary JSON upload requires CLOUDINARY_CLOUD_NAME, "
            "CLOUDINARY_API_KEY, and CLOUDINARY_API_SECRET."
        )
    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True,
    )
    _configured = True


def upload_json_bytes_for_responses(body: bytes, filename_hint: str) -> str:
    """
    Upload raw JSON bytes; return ``secure_url`` for OpenAI ``input_file`` ``file_url``.

    Retries once on transient Cloudinary errors (same pattern as former OpenAI Files upload).
    """
    ensure_cloudinary_config()
    folder = _json_folder()
    stem = Path(filename_hint).stem
    safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in stem)[:80]
    # Keep ``.json`` in public_id so delivery URLs and Cloudinary UI show the type; OpenAI ``file_url`` matches docs.
    public_id = f"{uuid.uuid4().hex}_{safe or 'export'}.json"
    for attempt in range(2):
        bio = io.BytesIO(body)
        try:
            result = cloudinary.uploader.upload(
                bio,
                resource_type="raw",
                folder=folder,
                public_id=public_id,
                use_filename=False,
                unique_filename=False,
            )
        except CloudinaryError as e:
            if attempt == 0:
                _log.warning(
                    "[cloudinary] upload failed for %s (%s), retrying once: %s",
                    filename_hint,
                    type(e).__name__,
                    e,
                )
                continue
            raise
        url = (result.get("secure_url") or "").strip()
        if not url:
            raise RuntimeError("Cloudinary upload returned no secure_url")
        _log.info(
            "[cloudinary] %s → %s (%d B, raw, folder=%s)",
            filename_hint,
            url,
            len(body),
            folder,
        )
        return url
    raise RuntimeError("unreachable")


def purge_json_attachment_folder() -> int:
    """
    Delete all raw assets whose public_id starts with the configured JSON folder prefix.

    Used when ``purge_json_attachment_storage`` is True before uploading new JSON
    (replaces former OpenAI ``purpose=user_data`` purge).
    """
    ensure_cloudinary_config()
    folder = _json_folder()
    prefix = folder.rstrip("/")
    try:
        result = cloudinary.api.delete_resources_by_prefix(
            prefix,
            resource_type="raw",
        )
    except CloudinaryError as e:
        _log.warning("[cloudinary] purge prefix=%s failed: %s", prefix, e)
        return 0
    deleted = result.get("deleted") or {}
    n = len(deleted) if isinstance(deleted, dict) else 0
    partial = result.get("partial") or {}
    if partial:
        _log.warning("[cloudinary] partial delete failures: %s", partial)
    if n:
        _log.info("[cloudinary] deleted %d raw object(s) under prefix %s/", n, prefix)
    return n
