"""
Build EA Zone NeverDie JSON from shard zones (plan_chinh / plan_phu), persist locally,
and optionally upload to Cloudinary with a stable public_id.

Used after ``coinmap-automation all`` / ``update``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Literal, Optional

import cloudinary.uploader
from cloudinary.exceptions import Error as CloudinaryError

from automation_tool.config import symbol_data_dir
from automation_tool.mt5_openai_parse import ParsedTrade, parse_trade_line
from automation_tool.state_files import _atomic_write_json
from automation_tool.zones_paths import SessionSlot, session_slot_now_hcm
from automation_tool.zones_state import Zone, ZonesState, read_manifest_last_write_slot, read_zones_state

_log = logging.getLogger(__name__)

_DEFAULT_EA_FOLDER = "automation_tool/ea_neverdie"
_PLAN_LABELS = ("plan_phu", "plan_chinh")  # plan_phu first, plan_chinh overwrites same side

NeverdieMode = Literal["trade", "off", "watch"]


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def ea_neverdie_skip_cloudinary() -> bool:
    return _env_bool("EA_NEVERDIE_SKIP_CLOUDINARY", False)


def neverdie_cloud_folder() -> str:
    raw = (os.getenv("CLOUDINARY_EA_NEVERDIE_FOLDER") or "").strip().strip("/")
    return raw if raw else _DEFAULT_EA_FOLDER


def neverdie_public_stem(symbol: str) -> str:
    """File stem under folder (with ``.json`` suffix)."""
    custom = (os.getenv("CLOUDINARY_EA_NEVERDIE_PUBLIC_ID") or "").strip()
    if custom:
        base = custom
    else:
        base = f"neverdie_{symbol.strip().upper()}"
    base = re.sub(r"[^\w.\-]+", "_", base, flags=re.ASCII)
    if not base.lower().endswith(".json"):
        base = f"{base}.json"
    return base


def neverdie_full_public_id(symbol: str) -> str:
    """Full Cloudinary public_id (folder/stem) for destroy/upload."""
    folder = neverdie_cloud_folder()
    stem = neverdie_public_stem(symbol)
    return f"{folder}/{stem}"


def default_local_path(symbol: str) -> Path:
    return symbol_data_dir(symbol) / "ea_zone_neverdie.json"


def entry_low_from_parsed(parsed: ParsedTrade) -> float:
    """EA ``low`` = entry; MARKET / no price → midpoint of SL and TP1."""
    if parsed.kind == "MARKET" or parsed.price is None:
        return (float(parsed.sl) + float(parsed.tp1)) / 2.0
    return float(parsed.price)


def _side_bucket(side: str) -> Optional[Literal["buy", "sell"]]:
    s = (side or "").strip().upper()
    if s == "BUY":
        return "buy"
    if s == "SELL":
        return "sell"
    return None


def _off_side() -> dict[str, Any]:
    return {"mode": "off", "low": 0.0, "high": 0.0, "sl": 0.0}


def _trade_side(low: float, high: float, sl: float) -> dict[str, Any]:
    return {"mode": "trade", "low": float(low), "high": float(high), "sl": float(sl)}


def _resolve_slot(zones_dir: Path) -> SessionSlot:
    slot = read_manifest_last_write_slot(zones_dir)
    if slot is not None:
        return slot
    return session_slot_now_hcm()


def _zone_by_label(zones: list[Zone], label: str) -> Optional[Zone]:
    key = label.strip().lower()
    for z in zones:
        if (z.label or "").strip().lower() == key:
            return z
    return None


def build_neverdie_payload(
    *,
    zones_dir: Path,
    symbol: str,
    state: Optional[ZonesState] = None,
    manifest_slot: Optional[SessionSlot] = None,
) -> dict[str, Any]:
    """
    Merge ``plan_phu`` then ``plan_chinh`` into ``buy`` / ``sell`` blocks.
    Successful parse updates that side; ``plan_chinh`` overwrites ``plan_phu`` for the same side.

    ``manifest_slot``: when set, skip reading ``zones_manifest.json`` (tests / callers).
    """
    buy: dict[str, Any] = dict(_off_side())
    sell: dict[str, Any] = dict(_off_side())

    st = state if state is not None else read_zones_state(zones_dir)
    if st is None or not st.zones:
        return {"buy": buy, "sell": sell}

    slot = manifest_slot if manifest_slot is not None else _resolve_slot(zones_dir)
    by_lab: dict[str, Zone] = {}
    for z in st.zones:
        if getattr(z, "session_slot", None) != slot:
            continue
        lab = (z.label or "").strip().lower()
        if lab in ("plan_chinh", "plan_phu"):
            by_lab[lab] = z

    sym = symbol.strip().upper()
    for lab in _PLAN_LABELS:
        z = by_lab.get(lab)
        if z is None:
            continue
        tl = (z.trade_line or "").strip()
        if not tl:
            continue
        parsed = parse_trade_line(tl, sym)
        if parsed is None:
            continue
        bucket = _side_bucket(parsed.side)
        if bucket is None:
            continue
        low = entry_low_from_parsed(parsed)
        high = float(parsed.tp1)
        sl = float(parsed.sl)
        block = _trade_side(low, high, sl)
        if bucket == "buy":
            buy = block
        else:
            sell = block

    return {"buy": buy, "sell": sell}


def delete_local_neverdie_json(path: Path) -> None:
    try:
        if path.is_file():
            path.unlink()
            _log.info("[ea-neverdie] removed local %s", path)
    except OSError as e:
        _log.warning("[ea-neverdie] unlink %s failed: %s", path, e)


def destroy_neverdie_cloudinary_asset(symbol: str) -> None:
    if ea_neverdie_skip_cloudinary():
        return
    try:
        from automation_tool.cloudinary_json import ensure_cloudinary_config

        ensure_cloudinary_config()
    except SystemExit:
        _log.info("[ea-neverdie] Cloudinary not configured — skip destroy")
        return
    public_id = neverdie_full_public_id(symbol)
    try:
        res = cloudinary.uploader.destroy(public_id, resource_type="raw", invalidate=True)
        _log.info("[ea-neverdie] Cloudinary destroy %s → %s", public_id, res)
    except CloudinaryError as e:
        _log.warning("[ea-neverdie] Cloudinary destroy %s failed: %s", public_id, e)


def clear_neverdie_before_all(symbol: str, *, local_path: Optional[Path] = None) -> None:
    """``all``: remove local JSON and Cloudinary asset before new zones."""
    p = local_path or default_local_path(symbol)
    delete_local_neverdie_json(p)
    destroy_neverdie_cloudinary_asset(symbol)


def upload_neverdie_json(body: bytes, symbol: str) -> Optional[str]:
    """Upload raw JSON; return ``secure_url`` or ``None`` if skipped / no Cloudinary."""
    if ea_neverdie_skip_cloudinary():
        _log.info("[ea-neverdie] EA_NEVERDIE_SKIP_CLOUDINARY — skip upload")
        return None
    try:
        from automation_tool.cloudinary_json import ensure_cloudinary_config

        ensure_cloudinary_config()
    except SystemExit:
        _log.warning("[ea-neverdie] Cloudinary not configured — skip upload")
        return None

    full_id = neverdie_full_public_id(symbol)
    folder = str(Path(full_id).parent).replace("\\", "/")
    public_id = Path(full_id).name

    for attempt in range(2):
        try:
            import io

            bio = io.BytesIO(body)
            result = cloudinary.uploader.upload(
                bio,
                resource_type="raw",
                folder=folder,
                public_id=public_id,
                use_filename=False,
                unique_filename=False,
                overwrite=True,
                invalidate=True,
            )
        except CloudinaryError as e:
            if attempt == 0:
                _log.warning(
                    "[ea-neverdie] upload failed (%s), retry once: %s",
                    type(e).__name__,
                    e,
                )
                continue
            raise
        url = (result.get("secure_url") or "").strip()
        if not url:
            raise RuntimeError("Cloudinary upload returned no secure_url")
        _log.info("[ea-neverdie] uploaded → %s (%d B)", url, len(body))
        return url
    return None


def write_local_neverdie_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, payload)


def publish_neverdie_ea_json(
    *,
    symbol: str,
    zones_dir: Path,
    state: Optional[ZonesState] = None,
    manifest_slot: Optional[SessionSlot] = None,
) -> tuple[dict[str, Any], Optional[str]]:
    """
    Build payload from shards, write ``data/<SYM>/ea_zone_neverdie.json``, optional Cloudinary upload.

    Returns ``(payload, secure_url_or_none)``.
    """
    payload = build_neverdie_payload(
        zones_dir=zones_dir,
        symbol=symbol,
        state=state,
        manifest_slot=manifest_slot,
    )
    sym = symbol.strip().upper()
    local_path = default_local_path(sym)
    write_local_neverdie_json(local_path, payload)
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    url: Optional[str] = None
    try:
        url = upload_neverdie_json(body, sym)
    except Exception as e:
        _log.warning("[ea-neverdie] upload failed: %s", e)
    cloud = (os.getenv("CLOUDINARY_CLOUD_NAME") or "").strip()
    if cloud:
        stem = neverdie_public_stem(sym)
        folder = neverdie_cloud_folder()
        versionless = f"https://res.cloudinary.com/{cloud}/raw/upload/{folder}/{stem}"
        _log.info("[ea-neverdie] versionless URL hint: %s", versionless)
        print(f"EA NeverDie JSON: local={local_path}", flush=True)
        print(f"EA NeverDie JSON (Cloudinary hint): {versionless}", flush=True)
        if url:
            print(f"EA NeverDie JSON (secure_url): {url}", flush=True)
    else:
        print(f"EA NeverDie JSON: local={local_path}", flush=True)
    return payload, url


def maybe_publish_neverdie_after_cli(*, symbol: str, zones_dir: Path) -> None:
    """Best-effort publish from current disk state (logs warnings, never raises)."""
    try:
        publish_neverdie_ea_json(symbol=symbol.strip().upper(), zones_dir=zones_dir)
    except Exception as e:
        _log.warning("[ea-neverdie] publish failed: %s", e)
