from __future__ import annotations

import json
import logging
import threading
from enum import Enum
from pathlib import Path
from typing import Any

from openai import OpenAI

from automation_tool.config import Settings

_log = logging.getLogger(__name__)
_PROMPT_CACHE_LOCK = threading.Lock()
_PROMPT_CACHE: dict[Path, tuple[float, str]] = {}
_ROOT = Path(__file__).resolve().parents[2]


class AgentRole(str, Enum):
    FULL_ANALYSIS = "full_analysis"
    INTRADAY_ALERT = "intraday_alert"
    INTRADAY_UPDATE = "intraday_update"
    TRADE_MANAGEMENT = "trade_management"
    RETROSPECTIVE = "retrospective"


def prompt_path_for_role(settings: Settings, role: AgentRole) -> Path:
    if role == AgentRole.FULL_ANALYSIS:
        return settings.openai_system_prompt_full_analysis_path
    if role == AgentRole.INTRADAY_ALERT:
        return settings.openai_system_prompt_intraday_alert_path
    if role == AgentRole.INTRADAY_UPDATE:
        return settings.openai_system_prompt_intraday_update_path
    if role == AgentRole.TRADE_MANAGEMENT:
        return settings.openai_system_prompt_trade_management_path
    if role == AgentRole.RETROSPECTIVE:
        return settings.openai_system_prompt_retrospective_path
    raise ValueError(f"Unknown role: {role}")


def _vector_store_ids_from_env(settings: Settings, role: AgentRole) -> list[str]:
    if role == AgentRole.FULL_ANALYSIS:
        ids = settings.openai_vector_store_ids_full_analysis
    elif role == AgentRole.INTRADAY_ALERT:
        ids = settings.openai_vector_store_ids_intraday_alert
    elif role == AgentRole.INTRADAY_UPDATE:
        ids = settings.openai_vector_store_ids_intraday_update
    elif role == AgentRole.TRADE_MANAGEMENT:
        ids = settings.openai_vector_store_ids_trade_management
    elif role == AgentRole.RETROSPECTIVE:
        ids = settings.openai_vector_store_ids_retrospective
    else:
        raise ValueError(f"Unknown role: {role}")
    return [s.strip() for s in ids if isinstance(s, str) and s.strip()]


def resolve_model_for_role(
    settings: Settings,
    role: AgentRole,
    *,
    override: str | None = None,
    fallback: str | None = None,
) -> str | None:
    o = (override or "").strip()
    if o:
        return o
    if role == AgentRole.FULL_ANALYSIS:
        m = settings.openai_model_full_analysis
    elif role == AgentRole.INTRADAY_ALERT:
        m = settings.openai_model_intraday_alert
    elif role == AgentRole.INTRADAY_UPDATE:
        m = settings.openai_model_intraday_update
    elif role == AgentRole.TRADE_MANAGEMENT:
        m = settings.openai_model_trade_management
    elif role == AgentRole.RETROSPECTIVE:
        m = settings.openai_model_retrospective
    else:
        raise ValueError(f"Unknown role: {role}")
    if (m or "").strip():
        return str(m).strip()
    f = (fallback or "").strip()
    if f:
        return f
    g = (settings.openai_model or "").strip()
    return g or None


def _read_prompt_file(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"System prompt file not found: {path}")
    st = path.stat()
    mtime = float(st.st_mtime)
    with _PROMPT_CACHE_LOCK:
        cached = _PROMPT_CACHE.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"System prompt file is empty: {path}")
    with _PROMPT_CACHE_LOCK:
        _PROMPT_CACHE[path] = (mtime, text)
    return text


def load_system_prompt(settings: Settings, role: AgentRole) -> str:
    return _read_prompt_file(prompt_path_for_role(settings, role))


def _load_vector_store_map(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_vector_store_map(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _read_vector_store_id_from_map(path: Path, role: AgentRole) -> str:
    data = _load_vector_store_map(path)
    rid = data.get(role.value)
    if isinstance(rid, str) and rid.strip():
        return rid.strip()
    return ""


def _write_vector_store_id_to_map(path: Path, role: AgentRole, vector_store_id: str) -> None:
    data = _load_vector_store_map(path)
    data[role.value] = vector_store_id
    _save_vector_store_map(path, data)


def _create_vector_store_for_role(api_key: str, role: AgentRole) -> str:
    client = OpenAI(api_key=api_key)
    name = f"automation-tool-{role.value}"
    created = client.vector_stores.create(name=name)
    vid = str(getattr(created, "id", "") or "").strip()
    if not vid:
        raise RuntimeError(f"Created vector store for {role.value} but id was empty.")
    _log.info("Created OpenAI vector store | role=%s id=%s", role.value, vid)
    return vid


def resolve_vector_store_ids_for_role(
    *,
    settings: Settings,
    role: AgentRole,
    api_key: str,
) -> list[str]:
    env_ids = _vector_store_ids_from_env(settings, role)
    if env_ids:
        return env_ids

    map_path = settings.openai_agent_vector_stores_map_path
    mapped = _read_vector_store_id_from_map(map_path, role)
    if mapped:
        return [mapped]

    if not settings.openai_auto_create_agent_vector_store:
        return []

    try:
        created_id = _create_vector_store_for_role(api_key, role)
    except Exception as e:
        _log.warning(
            "Auto-create vector store failed; continue without file_search | role=%s err=%s",
            role.value,
            e,
        )
        return []
    _write_vector_store_id_to_map(map_path, role, created_id)
    return [created_id]


def knowledge_paths_for_role(role: AgentRole) -> list[Path]:
    kdir = _ROOT / "knowledge" / "agents"
    core = kdir / "core_rules.md"
    pair = kdir / "pair_specific_rules.md"
    full = kdir / "full_analysis_knowledge.md"
    intraday_alert = kdir / "intraday_alert_knowledge.md"
    intraday_update = kdir / "intraday_update_knowledge.md"
    trade_management = kdir / "trade_management_knowledge.md"
    template_full = kdir / "template_output_full_analysis.md"

    if role == AgentRole.FULL_ANALYSIS:
        return [core, full, pair, template_full]
    if role == AgentRole.INTRADAY_ALERT:
        return [core, intraday_alert, pair]
    if role == AgentRole.INTRADAY_UPDATE:
        return [core, intraday_update, pair]
    if role == AgentRole.TRADE_MANAGEMENT:
        return [core, trade_management, pair]
    if role == AgentRole.RETROSPECTIVE:
        return [core, full, intraday_alert, intraday_update, trade_management, pair]
    raise ValueError(f"Unknown role: {role}")


def _attached_filenames(client: OpenAI, vector_store_id: str) -> set[str]:
    out: set[str] = set()
    try:
        lst = client.vector_stores.files.list(vector_store_id=vector_store_id, limit=100)
    except Exception as e:
        _log.warning("Cannot list vector store files | vs=%s err=%s", vector_store_id, e)
        return out
    for item in list(getattr(lst, "data", []) or []):
        fid = str(getattr(item, "file_id", "") or "").strip()
        if not fid:
            continue
        try:
            fobj = client.files.retrieve(fid)
        except Exception:
            continue
        name = str(getattr(fobj, "filename", "") or "").strip()
        if name:
            out.add(name)
    return out


def sync_knowledge_files_for_role(
    *,
    api_key: str,
    vector_store_id: str,
    role: AgentRole,
) -> dict[str, list[str]]:
    client = OpenAI(api_key=api_key)
    want_paths = [p for p in knowledge_paths_for_role(role) if p.is_file()]
    existing = _attached_filenames(client, vector_store_id)

    uploaded: list[str] = []
    skipped: list[str] = []
    missing: list[str] = []
    for p in knowledge_paths_for_role(role):
        if not p.is_file():
            missing.append(str(p))

    for p in want_paths:
        if p.name in existing:
            skipped.append(p.name)
            continue
        with p.open("rb") as fh:
            fobj = client.files.create(file=fh, purpose="assistants")
        fid = str(getattr(fobj, "id", "") or "").strip()
        if not fid:
            raise RuntimeError(f"Uploaded file missing id: {p}")
        client.vector_stores.files.create(vector_store_id=vector_store_id, file_id=fid)
        uploaded.append(p.name)
    return {"uploaded": uploaded, "skipped": skipped, "missing": missing}

