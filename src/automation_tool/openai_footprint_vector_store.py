from __future__ import annotations

import logging
import time
from pathlib import Path

from openai import OpenAI

_log = logging.getLogger(__name__)

GOCHARTING_FOOTPRINT_M5_VECTOR_STORE_ID = "vs_6a3b2f4ab6d88191b01fc0f08ffc9697"
GOCHARTING_FOOTPRINT_M15_VECTOR_STORE_ID = "vs_6a3b2f5cbfc0819184f890bf3a2329d2"
GOCHARTING_FOOTPRINT_VECTOR_STORE_IDS = [
    GOCHARTING_FOOTPRINT_M5_VECTOR_STORE_ID,
    GOCHARTING_FOOTPRINT_M15_VECTOR_STORE_ID,
]
FOOTPRINT_VECTOR_STORE_FILE_EXPIRES_SECONDS = 86_400  # 1 day
FOOTPRINT_VECTOR_STORE_UPLOAD_RETRY_SECONDS = 30

_FOOTPRINT_VECTOR_STORE_HINT_MARKER = "[GoCharting footprint clip — file_search]"

_INTERVAL_VECTOR_STORE: dict[str, str] = {
    "5m": GOCHARTING_FOOTPRINT_M5_VECTOR_STORE_ID,
    "15m": GOCHARTING_FOOTPRINT_M15_VECTOR_STORE_ID,
}


def footprint_vector_store_id_for_interval(interval: str) -> str:
    key = (interval or "").strip().lower()
    vs_id = _INTERVAL_VECTOR_STORE.get(key)
    if not vs_id:
        raise ValueError(f"no footprint vector store configured for interval {interval!r}")
    return vs_id


def merge_vector_store_ids(
    base: list[str] | tuple[str, ...] | None,
    *,
    extra: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    """Append ``extra`` vector store ids to ``base``, preserving order and deduping."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in list(base or []) + list(extra or []):
        vid = (raw or "").strip()
        if not vid or vid in seen:
            continue
        seen.add(vid)
        out.append(vid)
    return out


def with_gocharting_footprint_vector_store_ids(
    base: list[str] | tuple[str, ...] | None,
) -> list[str]:
    return merge_vector_store_ids(base, extra=GOCHARTING_FOOTPRINT_VECTOR_STORE_IDS)


def gocharting_footprint_vector_store_user_hint() -> str:
    """Prompt block: where to find GoCharting clip footprints via file_search."""
    return (
        f"\n\n{_FOOTPRINT_VECTOR_STORE_HINT_MARKER}\n"
        "Footprint GC1! theo từng nến đã đóng được upload vào vector store (file_search):\n"
        f"- M5 → vector store `{GOCHARTING_FOOTPRINT_M5_VECTOR_STORE_ID}`\n"
        f"- M15 → vector store `{GOCHARTING_FOOTPRINT_M15_VECTOR_STORE_ID}`\n"
        "Định dạng tên file: `{YYYYMMDD}_{H}h{M}m_{interval}.png`\n"
        "- `{H}h{M}m` = thời điểm **mở** của nến **vừa đóng** (không zero-pad: `9h55m`, `10h0m`)\n"
        "- `{interval}` = `5m` hoặc `15m`\n"
        "Ảnh là vùng clip footprint bid/ask theo price level từ GoCharting.\n"
    )


def append_footprint_vector_store_hint(
    user_text: str,
    vector_store_ids: list[str] | tuple[str, ...] | None,
) -> str:
    """Append footprint file_search hint when footprint vector stores are enabled."""
    ids = {str(v).strip() for v in (vector_store_ids or []) if str(v).strip()}
    if not ids.intersection(GOCHARTING_FOOTPRINT_VECTOR_STORE_IDS):
        return user_text
    if _FOOTPRINT_VECTOR_STORE_HINT_MARKER in (user_text or ""):
        return user_text
    return (user_text or "").rstrip() + gocharting_footprint_vector_store_user_hint()


def upload_footprint_image_to_vector_store(
    *,
    api_key: str,
    image_path: Path,
    interval: str,
) -> str:
    """
    Upload a footprint PNG to the interval's vector store.

    File expires after :data:`FOOTPRINT_VECTOR_STORE_FILE_EXPIRES_SECONDS` (1 day).
    Returns the OpenAI file id.
    """
    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"footprint image not found: {path}")

    vector_store_id = footprint_vector_store_id_for_interval(interval)
    client = OpenAI(api_key=api_key)
    with path.open("rb") as fh:
        uploaded = client.files.create(
            file=fh,
            purpose="assistants",
            expires_after={
                "anchor": "created_at",
                "seconds": FOOTPRINT_VECTOR_STORE_FILE_EXPIRES_SECONDS,
            },
        )
    client.vector_stores.files.create_and_poll(
        vector_store_id=vector_store_id,
        file_id=uploaded.id,
    )
    _log.info(
        "footprint vector store: uploaded %s → %s (file_id=%s, expires_in=1d)",
        path.name,
        vector_store_id,
        uploaded.id,
    )
    return uploaded.id


def upload_footprint_image_to_vector_store_with_retry(
    *,
    api_key: str,
    image_path: Path,
    interval: str,
    retry_seconds: float = FOOTPRINT_VECTOR_STORE_UPLOAD_RETRY_SECONDS,
) -> str:
    """Upload footprint PNG; on failure wait ``retry_seconds`` then retry once."""
    try:
        return upload_footprint_image_to_vector_store(
            api_key=api_key,
            image_path=image_path,
            interval=interval,
        )
    except Exception as first_err:
        _log.warning(
            "footprint vector store: upload failed for %s, retry in %ss: %s",
            Path(image_path).name,
            retry_seconds,
            first_err,
        )
        time.sleep(retry_seconds)
        return upload_footprint_image_to_vector_store(
            api_key=api_key,
            image_path=image_path,
            interval=interval,
        )
