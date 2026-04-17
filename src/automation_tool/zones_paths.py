"""Paths and session slots for sharded zone state (``data/<SYM>/zones/``)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from zoneinfo import ZoneInfo

from automation_tool.config import symbol_data_dir
from automation_tool.openai_analysis_json import ZONE_LABELS_ORDER
from automation_tool.state_files import _atomic_write_text

SessionSlot = Literal["sang", "chieu", "toi"]

SLOTS_ORDER: tuple[SessionSlot, ...] = ("sang", "chieu", "toi")

_ZONES_SUBDIR = "zones"
_MANIFEST_NAME = "zones_manifest.json"


def default_zones_dir(symbol: Optional[str] = None) -> Path:
    """``data/<SYM>/zones/`` — one JSON file per zone shard."""
    return symbol_data_dir(symbol) / _ZONES_SUBDIR


def default_last_price_path(symbol: Optional[str] = None) -> Path:
    """Shared Last price file for daemon giá + ``daemon-plan`` readers."""
    return symbol_data_dir(symbol) / "last.txt"


def read_last_price_file(path: Optional[Path] = None, *, symbol: Optional[str] = None) -> Optional[float]:
    p = path or default_last_price_path(symbol)
    if not p.is_file():
        return None
    try:
        raw = p.read_text(encoding="utf-8").strip().replace(",", "")
        if not raw:
            return None
        return float(raw)
    except (OSError, ValueError):
        return None


def write_last_price_file(price: float, path: Optional[Path] = None, *, symbol: Optional[str] = None) -> None:
    p = path or default_last_price_path(symbol)
    _atomic_write_text(p, f"{price}\n")




def resolve_zones_directory(zs_path: Optional[Path]) -> Path:
    """
    Resolve CLI ``--zones-json`` to the shard directory.

    - ``None`` → :func:`default_zones_dir`
    - existing **directory** → use as-is
    - path ending in ``zones_state.json`` → ``parent / "zones"`` (companion dir)
    - existing **file** (other) → parent as dir (fallback)
    """
    if zs_path is None:
        return default_zones_dir()
    p = zs_path.expanduser().resolve()
    if p.is_dir():
        return p
    name = p.name.lower()
    if name == "zones_state.json":
        return p.parent / _ZONES_SUBDIR
    if p.is_file():
        return p.parent
    if p.suffix.lower() == ".json" and "zones_state" in name:
        return p.parent / _ZONES_SUBDIR
    return p


def manifest_path(zones_dir: Path) -> Path:
    return zones_dir / _MANIFEST_NAME


def shard_filename(label: str, slot: SessionSlot) -> str:
    """``vung_{label}_{slot}.json`` — label must match ``ZONE_LABELS_ORDER`` keys."""
    lab = label.strip().lower()
    return f"vung_{lab}_{slot}.json"


def shard_path(zones_dir: Path, label: str, slot: SessionSlot) -> Path:
    return zones_dir / shard_filename(label, slot)


def session_slot_now_hcm(
    when: Optional[datetime] = None,
    *,
    tz_name: str = "Asia/Ho_Chi_Minh",
) -> SessionSlot:
    """
    Map local VN time to ``sang`` / ``chieu`` / ``toi``.

    - sang: [00:00, 13:00)
    - chieu: [13:00, 19:00)
    - toi: [19:00, 24:00)
    """
    try:
        tz = ZoneInfo((tz_name or "UTC").strip() or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    dt = when or datetime.now(tz)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone(tz)
    h = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
    if h < 13.0:
        return "sang"
    if h < 19.0:
        return "chieu"
    return "toi"


def iter_shard_paths(zones_dir: Path) -> list[Path]:
    """All expected shard paths (27); file may be missing."""
    out: list[Path] = []
    for slot in SLOTS_ORDER:
        for lab in ZONE_LABELS_ORDER:
            out.append(shard_path(zones_dir, lab, slot))
    return out


def zone_id_for_shard(label: str, slot: SessionSlot) -> str:
    return f"{label.strip().lower()}__{slot}"


def session_slot_from_shard_path(path: Path) -> Optional[SessionSlot]:
    """Infer ``sang``/``chieu``/``toi`` from ``vung_*_{slot}.json`` filename."""
    stem = path.stem
    for s in SLOTS_ORDER:
        if stem.endswith("_" + s):
            return s
    return None


def label_from_shard_stem(stem: str) -> Optional[str]:
    """
    From ``vung_plan_chinh_sang`` → ``plan_chinh``; ``vung_scalp_toi`` → ``scalp``.
    """
    s = (stem or "").strip()
    if not s:
        return None
    for slot in SLOTS_ORDER:
        if s.endswith("_" + slot):
            base = s[: -(len(slot) + 1)]
            if base.startswith("vung_"):
                return base[5:]
    return None


def session_slot_display_vn(slot: Optional[str]) -> Optional[str]:
    """``sang`` / ``chieu`` / ``toi`` → tiếng Việt cho tin user."""
    if not slot or not str(slot).strip():
        return None
    key = str(slot).strip().lower()
    if key == "sang":
        return "Sáng"
    if key == "chieu":
        return "Chiều"
    if key == "toi":
        return "Tối"
    return None
