from __future__ import annotations

import json
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


def default_zones_state_path() -> Path:
    return symbol_data_dir() / "zones_state.json"


def remove_zones_state_file(path: Optional[Path] = None) -> bool:
    """
    Xóa ``zones_state.json`` nếu tồn tại (vd. đầu phiên ``all`` để bắt đầu sạch).

    Returns:
        ``True`` nếu đã xóa file, ``False`` nếu file không có.
    """
    p = path or default_zones_state_path()
    if p.is_file():
        p.unlink()
        return True
    return False


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
    status: ZoneStatus = "vung_cho"
    source: str = ""

    def to_json_dict(self) -> dict[str, Any]:
        return {
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
            "status": self.status,
            "source": self.source,
        }


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
        status=st,  # type: ignore[assignment]
        source=src,
    )


def read_zones_state(path: Optional[Path] = None) -> Optional[ZonesState]:
    p = path or default_zones_state_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    sym = data.get("symbol")
    if not isinstance(sym, str) or not sym.strip():
        return None
    zones_raw = data.get("zones")
    zones: list[Zone] = []
    if isinstance(zones_raw, list):
        for item in zones_raw:
            if isinstance(item, dict):
                z = _parse_zone(item)
                if z is not None:
                    zones.append(z)
    updated_at = str(data.get("updated_at") or "")
    last_obs = _parse_last_observed(data.get("last_observed"))
    return ZonesState(symbol=sym.strip(), zones=zones, updated_at=updated_at, last_observed=last_obs)


def baseline_triple_from_zones_state(st: Optional[ZonesState]) -> Optional[tuple[float, float, float]]:
    """
    Mid-price triple (plan_chinh, plan_phu, scalp) from persisted zones.

    Used when ``morning_baseline_prices.json`` is absent (e.g. ``all`` only writes ``zones_state.json``).
    """
    if st is None or not st.zones:
        return None
    by_label: dict[str, Zone] = {}
    for z in st.zones:
        key = (z.label or "").strip().lower()
        if key:
            by_label[key] = z
    mids: list[float] = []
    for lab in ZONE_LABELS_ORDER:
        z = by_label.get(lab)
        if z is None:
            return None
        lo, hi = parse_vung_cho_bounds(z.vung_cho)
        if lo is None or hi is None:
            return None
        mids.append((float(lo) + float(hi)) / 2.0)
    return (mids[0], mids[1], mids[2])


def write_zones_state(state: ZonesState, path: Optional[Path] = None) -> None:
    p = path or default_zones_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # normalize updated_at on write
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
    p = path or default_zones_state_path()
    st = read_zones_state(p)
    if st is None:
        return
    st.last_observed = LastObserved(tv_watchlist_last=tv_watchlist_last, observed_at=_now_iso())
    write_zones_state(st, path=p)


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


def zone_from_price_entry(*, lab: str, pe: PriceZoneEntry, source: str) -> Zone:
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
    return Zone(
        id=_default_zone_id(lab),
        label=lab,
        vung_cho=vc,
        side=side,
        hop_luu=pe.hop_luu,
        trade_line=tl,
        mt5_ticket=None,
        status="vung_cho",
        source=source,
    )


def zones_from_analysis_payload(
    *,
    symbol: str,
    payload: AnalysisPayload,
    source: str,
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
        zones.append(zone_from_price_entry(lab=lab, pe=pe, source=source))
    return zones


def zones_from_analysis_payload_merged(
    *,
    existing: ZonesState | None,
    payload: AnalysisPayload,
    source: str,
) -> list[Zone]:
    """
    Like :func:`zones_from_analysis_payload`, but for Schema B update: keep an existing zone when
    the model sets ``no_change`` to non-``False`` for that label; replace when ``no_change is False``.
    If there is no existing zone and the model keeps baseline (non-False ``no_change``), synthesize
    from the entry so the list still has three zones when all labels are present.
    """
    by_old: dict[str, Zone] = {}
    if existing is not None:
        for z in existing.zones:
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
                zones.append(zone_from_price_entry(lab=lab, pe=pe, source=source))
            continue
        zones.append(zone_from_price_entry(lab=lab, pe=pe, source=source))
    return zones

