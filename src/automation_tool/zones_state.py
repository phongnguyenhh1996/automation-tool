from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from automation_tool.config import symbol_data_dir
from automation_tool.openai_analysis_json import (
    AnalysisPayload,
    PriceZoneEntry,
    ZONE_LABELS_ORDER,
    parse_vung_cho_bounds,
)
from automation_tool.state_files import _atomic_write_json  # type: ignore[attr-defined]
from automation_tool.zones_paths import (
    SLOTS_ORDER,
    SessionSlot,
    default_last_price_path,
    default_zones_dir,
    iter_shard_paths,
    manifest_path,
    resolve_zones_directory,
    session_slot_from_shard_path,
    session_slot_now_hcm,
    shard_filename,
    shard_path,
    zone_id_for_shard,
)

ZoneStatus = Literal[
    "vung_cho",
    "cham",
    "dang_vao_lenh",
    "dang_thuc_thi",
    "vao_lenh",
    "cho_tp1",
    "loai",
    "done",
]

# Separator for persisted vung_cho strings (Unicode en dash, same as model / system prompt).
_VUNG_CHO_SEP = "–"

_zones_io_lock = threading.RLock()

_SCHEMA_MANIFEST = 1


def default_zones_state_path() -> Path:
    """Legacy monolithic JSON; prefer :func:`default_zones_dir` for shards."""
    return symbol_data_dir() / "zones_state.json"


def remove_zones_state_file(path: Optional[Path] = None) -> bool:
    """
    Legacy: xóa ``zones_state.json`` nếu tồn tại.

    For full clear before ``all``, use :func:`clear_zones_directory`.
    """
    p = path or default_zones_state_path()
    if p.is_file():
        p.unlink()
        return True
    return False


def clear_zones_directory(zones_dir: Optional[Path] = None) -> int:
    """
    Xóa **toàn bộ** nội dung thư mục ``zones/`` (shard, manifest, pid, …).

    Returns:
        Số file đã xóa.
    """
    root = zones_dir or default_zones_dir()
    n = 0
    if root.is_dir():
        for child in root.iterdir():
            try:
                if child.is_file():
                    child.unlink()
                    n += 1
                elif child.is_dir():
                    for sub in child.rglob("*"):
                        if sub.is_file():
                            sub.unlink()
                            n += 1
                    child.rmdir()
            except OSError:
                pass
    remove_zones_state_file()
    return n


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_float(x: Any) -> Optional[float]:
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        s = x.strip().replace(",", "")
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class LastObserved:
    tv_watchlist_last: Optional[float] = None
    observed_at: str = ""

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "tv_watchlist_last": self.tv_watchlist_last,
            "observed_at": self.observed_at or _now_iso(),
        }


@dataclass
class Zone:
    id: str
    label: str
    vung_cho: str
    side: Literal["BUY", "SELL"]
    hop_luu: Optional[int] = None
    trade_line: str = ""
    mt5_ticket: Optional[int] = None
    loai_streak: int = 0
    tp1_followup_done: bool = False
    retry_at: str = ""
    # ISO UTC: sau khi auto-entry MT5 thất bại, không dispatch lại cho đến thời điểm này (tránh lặp vô hạn).
    auto_entry_retry_after: str = ""
    status: ZoneStatus = "vung_cho"
    source: str = ""
    # Sharded storage: ``sang`` | ``chieu`` | ``toi``; ``None`` = legacy single-file state.
    session_slot: Optional[str] = None

    def to_json_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "vung_cho": self.vung_cho,
            "side": self.side,
            "hop_luu": self.hop_luu,
            "trade_line": self.trade_line,
            "mt5_ticket": self.mt5_ticket,
            "loai_streak": self.loai_streak,
            "tp1_followup_done": self.tp1_followup_done,
            "retry_at": self.retry_at,
            "auto_entry_retry_after": self.auto_entry_retry_after,
            "status": self.status,
            "source": self.source,
        }
        if self.session_slot:
            out["session_slot"] = self.session_slot
        return out


@dataclass
class ZonesState:
    symbol: str
    zones: list[Zone] = field(default_factory=list)
    updated_at: str = ""
    last_observed: Optional[LastObserved] = None

    def to_json_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "symbol": self.symbol,
            "updated_at": self.updated_at or _now_iso(),
            "zones": [z.to_json_dict() for z in self.zones],
        }
        if self.last_observed is not None:
            out["last_observed"] = self.last_observed.to_json_dict()
        return out


def _parse_last_observed(v: Any) -> Optional[LastObserved]:
    if not isinstance(v, dict):
        return None
    lp = _as_float(v.get("tv_watchlist_last"))
    ts = str(v.get("observed_at") or "")
    return LastObserved(tv_watchlist_last=lp, observed_at=ts)


def _parse_zone(d: dict[str, Any]) -> Optional[Zone]:
    zid = d.get("id")
    lab = d.get("label")
    if not isinstance(zid, str) or not zid.strip():
        return None
    if not isinstance(lab, str) or not lab.strip():
        return None
    vc_raw = d.get("vung_cho")
    vung_cho: str = ""
    if isinstance(vc_raw, str) and vc_raw.strip():
        vung_cho = vc_raw.strip()
        lo, hi = parse_vung_cho_bounds(vung_cho)
        if lo is None or hi is None:
            return None
    else:
        rl = _as_float(d.get("range_low"))
        rh = _as_float(d.get("range_high"))
        ap = _as_float(d.get("alert_price"))
        if rl is not None and rh is not None:
            a, b = float(min(rl, rh)), float(max(rl, rh))
            vung_cho = f"{a}{_VUNG_CHO_SEP}{b}"
        elif ap is not None:
            vung_cho = f"{float(ap)}{_VUNG_CHO_SEP}{float(ap)}"
        else:
            return None
    side_raw = str(d.get("side") or "").strip().upper()
    if side_raw not in ("BUY", "SELL"):
        side_raw = "BUY"
    hop_raw = d.get("hop_luu")
    hop_luu: Optional[int] = None
    if hop_raw is not None:
        try:
            hop_luu = int(hop_raw)
        except Exception:
            hop_luu = None
    tl = d.get("trade_line")
    trade_line = tl.strip() if isinstance(tl, str) else ""
    tk = d.get("mt5_ticket")
    mt5_ticket: Optional[int] = None
    if tk is not None:
        try:
            mt5_ticket = int(tk)
        except Exception:
            mt5_ticket = None
    ls_raw = d.get("loai_streak")
    loai_streak = 0
    if ls_raw is not None:
        try:
            loai_streak = int(ls_raw)
        except Exception:
            loai_streak = 0
    if loai_streak < 0:
        loai_streak = 0
    td_raw = d.get("tp1_followup_done")
    tp1_done = bool(td_raw) if isinstance(td_raw, bool) else False
    ra_raw = d.get("retry_at")
    retry_at = ra_raw.strip() if isinstance(ra_raw, str) else ""
    aer_raw = d.get("auto_entry_retry_after")
    auto_entry_retry_after = aer_raw.strip() if isinstance(aer_raw, str) else ""
    st = str(d.get("status") or "").strip()
    if st not in (
        "vung_cho",
        "cham",
        "dang_vao_lenh",
        "dang_thuc_thi",
        "vao_lenh",
        "cho_tp1",
        "loai",
        "done",
    ):
        st = "vung_cho"
    src = str(d.get("source") or "").strip()
    ss_raw = d.get("session_slot")
    session_slot: Optional[str] = None
    if isinstance(ss_raw, str) and ss_raw.strip() in ("sang", "chieu", "toi"):
        session_slot = ss_raw.strip()
    return Zone(
        id=zid.strip(),
        label=lab.strip(),
        vung_cho=vung_cho,
        side=side_raw,  # type: ignore[assignment]
        hop_luu=hop_luu,
        trade_line=trade_line,
        mt5_ticket=mt5_ticket,
        loai_streak=loai_streak,
        tp1_followup_done=tp1_done,
        retry_at=retry_at,
        auto_entry_retry_after=auto_entry_retry_after,
        status=st,  # type: ignore[assignment]
        source=src,
        session_slot=session_slot,
    )


def read_zone_shard_file(shard_path: Path) -> Optional[Zone]:
    """Load a single zone from ``vung_{label}_{slot}.json``."""
    if not shard_path.is_file():
        return None
    try:
        data = json.loads(shard_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    inner = data.get("zone")
    if isinstance(inner, dict):
        return _parse_zone(inner)
    return _parse_zone(data)


def _write_shard_file(shard_path: Path, symbol: str, slot: SessionSlot, zone: Zone) -> None:
    shard_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbol": symbol,
        "slot": slot,
        "updated_at": _now_iso(),
        "zone": zone.to_json_dict(),
    }
    _atomic_write_json(shard_path, payload)


def write_zones_for_slot(
    *,
    symbol: str,
    zones: list[Zone],
    slot: SessionSlot,
    zones_dir: Optional[Path] = None,
    last_observed: Optional[LastObserved] = None,
) -> None:
    """Write three shard files + manifest for one session slot."""
    root = zones_dir or default_zones_dir()
    with _zones_io_lock:
        root.mkdir(parents=True, exist_ok=True)
        for z in zones:
            lab = (z.label or "").strip().lower()
            if not lab:
                continue
            z2 = Zone(
                id=zone_id_for_shard(lab, slot),
                label=z.label,
                vung_cho=z.vung_cho,
                side=z.side,
                hop_luu=z.hop_luu,
                trade_line=z.trade_line,
                mt5_ticket=z.mt5_ticket,
                loai_streak=z.loai_streak,
                tp1_followup_done=z.tp1_followup_done,
                retry_at=z.retry_at,
                auto_entry_retry_after=z.auto_entry_retry_after,
                status=z.status,
                source=z.source,
                session_slot=slot,
            )
            _write_shard_file(shard_path(root, lab, slot), symbol, slot, z2)
        man: dict[str, Any] = {
            "symbol": symbol,
            "last_write_slot": slot,
            "updated_at": _now_iso(),
            "schema_version": _SCHEMA_MANIFEST,
        }
        if last_observed is not None:
            man["last_observed"] = last_observed.to_json_dict()
        _atomic_write_json(manifest_path(root), man)


def read_zones_state_from_shard(shard_path: Path) -> Optional[ZonesState]:
    """Load a single-zone :class:`ZonesState` from one shard file (``daemon-plan``)."""
    if not shard_path.is_file():
        return None
    try:
        data = json.loads(shard_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    sym = data.get("symbol")
    if not isinstance(sym, str) or not sym.strip():
        sym = "XAUUSD"
    slot_raw = data.get("slot")
    inner = data.get("zone")
    if not isinstance(inner, dict):
        return None
    z = _parse_zone(inner)
    if z is None:
        return None
    if isinstance(slot_raw, str) and slot_raw.strip() in ("sang", "chieu", "toi"):
        z.session_slot = slot_raw.strip()
    elif z.session_slot is None:
        inferred = session_slot_from_shard_path(shard_path)
        if inferred is not None:
            z.session_slot = inferred
    updated_at = str(data.get("updated_at") or "")
    return ZonesState(symbol=sym.strip(), zones=[z], updated_at=updated_at)


def migrate_legacy_zones_state_if_needed(path_hint: Optional[Path] = None) -> bool:
    """
    If ``zones_state.json`` exists and no shard files exist yet, split into one slot (now HCM) + remove legacy.
    """
    zones_dir = resolve_zones_directory(path_hint)
    legacy = default_zones_state_path()
    if not legacy.is_file():
        return False
    for p in iter_shard_paths(zones_dir):
        if p.is_file():
            return False
    try:
        raw = json.loads(legacy.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(raw, dict):
        return False
    s2 = raw.get("symbol")
    if not isinstance(s2, str) or not s2.strip():
        sym = "XAUUSD"
    else:
        sym = s2.strip()
    zones_raw = raw.get("zones")
    zones_l: list[Zone] = []
    if isinstance(zones_raw, list):
        for item in zones_raw:
            if isinstance(item, dict):
                zz = _parse_zone(item)
                if zz is not None:
                    zones_l.append(zz)
    if not zones_l:
        return False
    slot = session_slot_now_hcm()
    write_zones_for_slot(symbol=sym, zones=zones_l, slot=slot, zones_dir=zones_dir)
    legacy.unlink()
    return True


def write_zones_state_to_shard(shard_path: Path, state: ZonesState) -> None:
    """Persist exactly one zone to ``shard_path`` (atomic)."""
    if len(state.zones) != 1:
        raise ValueError("write_zones_state_to_shard requires exactly one zone")
    z = state.zones[0]
    slot: SessionSlot
    if z.session_slot in ("sang", "chieu", "toi"):
        slot = z.session_slot  # type: ignore[assignment]
    else:
        inferred = session_slot_from_shard_path(shard_path)
        if inferred is None:
            raise ValueError(f"cannot infer session slot for shard {shard_path!s}")
        slot = inferred
    with _zones_io_lock:
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        _write_shard_file(shard_path, state.symbol, slot, z)


def _read_zones_state_unlocked(path: Optional[Path] = None) -> Optional[ZonesState]:
    if path is not None:
        p = path.expanduser().resolve()
        if p.is_file() and p.name.lower() == "zones_state.json":
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return None
            if not isinstance(data, dict):
                return None
            s2 = data.get("symbol")
            if not isinstance(s2, str) or not s2.strip():
                return None
            zones_raw = data.get("zones")
            zones_l: list[Zone] = []
            if isinstance(zones_raw, list):
                for item in zones_raw:
                    if isinstance(item, dict):
                        zz = _parse_zone(item)
                        if zz is not None:
                            zones_l.append(zz)
            updated_at = str(data.get("updated_at") or "")
            last_obs2 = _parse_last_observed(data.get("last_observed"))
            return ZonesState(
                symbol=s2.strip(),
                zones=zones_l,
                updated_at=updated_at,
                last_observed=last_obs2,
            )
    zones_dir = resolve_zones_directory(path) if path is not None else default_zones_dir()
    zones: list[Zone] = []
    sym: Optional[str] = None
    updated_at = ""
    last_obs: Optional[LastObserved] = None
    any_shard = False
    for sp in iter_shard_paths(zones_dir):
        if sp.is_file():
            any_shard = True
            z = read_zone_shard_file(sp)
            if z is not None:
                zones.append(z)
    mp = manifest_path(zones_dir)
    if mp.is_file():
        try:
            md = json.loads(mp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            md = {}
        if isinstance(md, dict):
            s = md.get("symbol")
            if isinstance(s, str) and s.strip():
                sym = s.strip()
            updated_at = str(md.get("updated_at") or "")
            last_obs = _parse_last_observed(md.get("last_observed"))
    if any_shard:
        return ZonesState(
            symbol=(sym or "XAUUSD"),
            zones=zones,
            updated_at=updated_at,
            last_observed=last_obs,
        )
    legacy = default_zones_state_path()
    if legacy.is_file():
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        s2 = data.get("symbol")
        if not isinstance(s2, str) or not s2.strip():
            return None
        zones_raw = data.get("zones")
        zones_l: list[Zone] = []
        if isinstance(zones_raw, list):
            for item in zones_raw:
                if isinstance(item, dict):
                    zz = _parse_zone(item)
                    if zz is not None:
                        zones_l.append(zz)
        updated_at = str(data.get("updated_at") or "")
        last_obs2 = _parse_last_observed(data.get("last_observed"))
        return ZonesState(
            symbol=s2.strip(),
            zones=zones_l,
            updated_at=updated_at,
            last_observed=last_obs2,
        )
    return None


def read_zones_state(path: Optional[Path] = None) -> Optional[ZonesState]:
    """Read merged zones from all shards under ``zones/``, or legacy ``zones_state.json``."""
    with _zones_io_lock:
        return _read_zones_state_unlocked(path)


def write_zones_state(state: ZonesState, path: Optional[Path] = None) -> None:
    """
    Legacy write of monolithic ``zones_state.json`` (tests / migration).

    Production ``all`` / ``update`` should use :func:`write_zones_for_slot`.
    """
    p = path or default_zones_state_path()
    with _zones_io_lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        st = ZonesState(
            symbol=state.symbol,
            zones=list(state.zones),
            updated_at=_now_iso(),
            last_observed=state.last_observed,
        )
        _atomic_write_json(p, st.to_json_dict())


def upsert_last_observed(
    *,
    tv_watchlist_last: Optional[float],
    path: Optional[Path] = None,
) -> None:
    zones_dir = resolve_zones_directory(path) if path is not None else default_zones_dir()
    with _zones_io_lock:
        st = _read_zones_state_unlocked(path)
        if st is None:
            return
        st.last_observed = LastObserved(tv_watchlist_last=tv_watchlist_last, observed_at=_now_iso())
        mp = manifest_path(zones_dir)
        if mp.is_file():
            try:
                md = json.loads(mp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                md = {}
            if not isinstance(md, dict):
                md = {}
            md["last_observed"] = st.last_observed.to_json_dict()
            md["updated_at"] = _now_iso()
            _atomic_write_json(mp, md)
        else:
            write_zones_state(st, path=default_zones_state_path())


def _default_zone_id(label: str) -> str:
    return label.strip().lower()


def _zone_status_phrase_vn(z: Zone) -> str:
    """Một câu tiếng Việt mô tả trạng thái (dùng sau chữ 'vùng {label} …')."""
    s = z.status
    if s == "vung_cho":
        return "vẫn đang là vùng chờ"
    if s == "cham":
        return "đã chạm và vẫn đang chờ"
    if s == "dang_vao_lenh":
        return "đang xử lý vào lệnh (auto-entry)"
    if s == "dang_thuc_thi":
        return "đang thực thi / chờ xác nhận"
    if s == "loai":
        return "đã loại"
    if s == "vao_lenh":
        return "đã vào lệnh"
    if s == "cho_tp1":
        return "đã vào lệnh và đang theo dõi TP1"
    if s == "done":
        return "đã hoàn tất (done)"
    return f"trạng thái={s}"


def format_intraday_update_time_line(
    *,
    timezone_name: str = "Asia/Ho_Chi_Minh",
) -> str:
    """One line: current local time for [INTRADAY_UPDATE] user text (no zone snapshot)."""
    try:
        tz = ZoneInfo((timezone_name or "UTC").strip() or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    return (
        f"Thời gian hiện tại ({timezone_name}): "
        f"{now.strftime('%Y-%m-%d %H:%M:%S')}\n"
    )


def format_intraday_update_baseline_vung_cho(st: Optional[ZonesState]) -> str:
    """
    Baseline lines for [INTRADAY_UPDATE]: ``vùng chờ {label}: {vung_cho}`` for each of
    plan_chinh, plan_phu, scalp from ``zones_state``. If a label is missing or ``vung_cho``
    is empty, ``(chưa có)`` — no fallback to morning price triple.
    """
    by_label: dict[str, Zone] = {}
    if st is not None:
        for z in st.zones:
            key = (z.label or "").strip().lower()
            if key:
                by_label[key] = z
    lines: list[str] = []
    for lab in ZONE_LABELS_ORDER:
        z = by_label.get(lab)
        vc = (z.vung_cho or "").strip() if z is not None else ""
        if vc:
            lines.append(f"vùng chờ {lab}: {vc}")
        else:
            lines.append(f"vùng chờ {lab}: (chưa có)")
    return "\n".join(lines) + "\n"


def format_zones_snapshot_for_intraday_update(
    st: Optional[ZonesState],
    *,
    timezone_name: str = "Asia/Ho_Chi_Minh",
) -> str:
    """
    Human-readable block for [INTRADAY_UPDATE] user text: current local time + one line per
    canonical label (plan_chinh, plan_phu, scalp) in that order, then compact details.
    """
    try:
        tz = ZoneInfo((timezone_name or "UTC").strip() or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    header = f"Thời gian hiện tại ({timezone_name}): {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
    if st is None or not st.zones:
        return (
            header
            + "Chưa có snapshot zone trên disk (zones_state.json) — chỉ dùng baseline + footprint đính kèm.\n"
        )
    by_label: dict[str, Zone] = {}
    for z in st.zones:
        key = (z.label or "").strip().lower()
        if key:
            by_label[key] = z

    lines: list[str] = [
        header,
        f"Symbol: {st.symbol}",
        "",
        "Tóm tắt theo label (thứ tự plan_chinh → plan_phu → scalp):",
        "",
    ]

    ordered: list[Zone] = []
    for lab in ZONE_LABELS_ORDER:
        z = by_label.get(lab)
        if z is None:
            lines.append(f"vùng {lab}: không có trong zones_state.")
            lines.append("")
            continue
        phrase = _zone_status_phrase_vn(z)
        lines.append(f"vùng {lab} {phrase}")
        ordered.append(z)

    lines.append("Chi tiết (status kỹ thuật + giá + trade_line):")
    lines.append("")

    def _one(z: Zone) -> str:
        tl = (z.trade_line or "").strip()
        if len(tl) > 160:
            tl = tl[:157] + "..."
        hop = "" if z.hop_luu is None else f" hop_luu={z.hop_luu}"
        return (
            f"  - {z.label} | status={z.status} | "
            f"vung_cho={z.vung_cho} | {hop}\n"
            f"    trade_line: {tl or '(trống)'}"
        )

    for z in ordered:
        lines.append(_one(z))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def zone_from_price_entry(
    *,
    lab: str,
    pe: PriceZoneEntry,
    source: str,
    session_slot: Optional[SessionSlot] = None,
) -> Zone:
    """
    Build a single :class:`Zone` from one :class:`PriceZoneEntry`.

    Persists ``vung_cho`` (from model or synthesized from ``range_low``/``range_high`` or single ``value``).
    Side is inferred from ``trade_line`` prefix (BUY/SELL) when present; defaults to BUY.
    """
    v = float(pe.value)
    tl = (pe.trade_line or "").strip()
    side: Literal["BUY", "SELL"] = "BUY"
    if tl:
        head = tl.lstrip().upper()
        if head.startswith("SELL"):
            side = "SELL"
    vc = (pe.vung_cho or "").strip()
    if not vc:
        rl = pe.range_low
        rh = pe.range_high
        if rl is not None and rh is not None:
            lo = float(min(rl, rh))
            hi = float(max(rl, rh))
            vc = f"{lo}{_VUNG_CHO_SEP}{hi}"
        else:
            vc = f"{v}{_VUNG_CHO_SEP}{v}"
    sid = zone_id_for_shard(lab, session_slot) if session_slot else _default_zone_id(lab)
    return Zone(
        id=sid,
        label=lab,
        vung_cho=vc,
        side=side,
        hop_luu=pe.hop_luu,
        trade_line=tl,
        mt5_ticket=None,
        status="vung_cho",
        source=source,
        session_slot=session_slot,
    )


def zones_from_analysis_payload(
    *,
    symbol: str,
    payload: AnalysisPayload,
    source: str,
    session_slot: Optional[SessionSlot] = None,
) -> list[Zone]:
    """Convert analysis payload (prices[] entries) into zones (full replace per label present)."""
    by_label: dict[str, PriceZoneEntry] = {}
    for pe in payload.prices:
        key = pe.label.strip().lower()
        if key:
            by_label[key] = pe

    zones: list[Zone] = []
    for lab in ZONE_LABELS_ORDER:
        pe = by_label.get(lab)
        if pe is None:
            continue
        zones.append(
            zone_from_price_entry(lab=lab, pe=pe, source=source, session_slot=session_slot)
        )
    return zones


def zones_from_analysis_payload_merged(
    *,
    existing: ZonesState | None,
    payload: AnalysisPayload,
    source: str,
    merge_slot: Optional[SessionSlot] = None,
) -> list[Zone]:
    """
    Like :func:`zones_from_analysis_payload`, but for Schema B update: keep an existing zone when
    the model sets ``no_change`` to non-``False`` for that label; replace when ``no_change is False``.
    If there is no existing zone and the model keeps baseline (non-False ``no_change``), synthesize
    from the entry so the list still has three zones when all labels are present.

    When ``merge_slot`` is set, only existing zones with that ``session_slot`` participate in merge
    (per-shard update).
    """
    by_old: dict[str, Zone] = {}
    if existing is not None:
        for z in existing.zones:
            if merge_slot is not None:
                zs = getattr(z, "session_slot", None)
                if zs != merge_slot:
                    continue
            by_old[z.label.strip().lower()] = z

    by_price: dict[str, PriceZoneEntry] = {}
    for pe in payload.prices:
        k = pe.label.strip().lower()
        if k:
            by_price[k] = pe

    zones: list[Zone] = []
    for lab in ZONE_LABELS_ORDER:
        pe = by_price.get(lab)
        old = by_old.get(lab)
        if pe is None:
            if old is not None:
                zones.append(old)
            continue
        if pe.no_change is not False:
            if old is not None:
                zones.append(old)
            else:
                zones.append(
                    zone_from_price_entry(
                        lab=lab, pe=pe, source=source, session_slot=merge_slot
                    )
                )
            continue
        zones.append(
            zone_from_price_entry(lab=lab, pe=pe, source=source, session_slot=merge_slot)
        )
    return zones

