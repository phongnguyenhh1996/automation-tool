from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from automation_tool.config import symbol_data_dir
from automation_tool.openai_analysis_json import AnalysisPayload, PriceZoneEntry, ZONE_LABELS_ORDER
from automation_tool.state_files import _atomic_write_json  # type: ignore[attr-defined]

ZoneStatus = Literal["vung_cho", "cham", "dang_thuc_thi", "vao_lenh", "cho_tp1", "loai", "done"]


def default_zones_state_path() -> Path:
    return symbol_data_dir() / "zones_state.json"


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
    range_low: float
    range_high: float
    alert_price: float
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
            "range_low": self.range_low,
            "range_high": self.range_high,
            "alert_price": self.alert_price,
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
    rl = _as_float(d.get("range_low"))
    rh = _as_float(d.get("range_high"))
    ap = _as_float(d.get("alert_price"))
    if rl is None or rh is None or ap is None:
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
    if st not in ("vung_cho", "cham", "dang_thuc_thi", "vao_lenh", "cho_tp1", "loai", "done"):
        st = "vung_cho"
    src = str(d.get("source") or "").strip()
    return Zone(
        id=zid.strip(),
        label=lab.strip(),
        range_low=float(rl),
        range_high=float(rh),
        alert_price=float(ap),
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


def zones_from_analysis_payload(
    *,
    symbol: str,
    payload: AnalysisPayload,
    source: str,
) -> list[Zone]:
    """
    Convert analysis payload (prices[] entries) into zones.

    Notes:
    - Current analysis JSON only provides a single `value` per label; we default range_low=range_high=value,
      and alert_price=value. User can widen ranges later.
    - Side is inferred from `trade_line` prefix (BUY/SELL) when present; defaults to BUY.
    """
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
        v = float(pe.value)
        tl = (pe.trade_line or "").strip()
        side = "BUY"
        if tl:
            head = tl.lstrip().upper()
            if head.startswith("SELL"):
                side = "SELL"
        rl = pe.range_low
        rh = pe.range_high
        if rl is None or rh is None:
            range_low = v
            range_high = v
        else:
            range_low = float(min(rl, rh))
            range_high = float(max(rl, rh))
        zones.append(
            Zone(
                id=_default_zone_id(lab),
                label=lab,
                range_low=range_low,
                range_high=range_high,
                alert_price=v,
                side=side,  # type: ignore[arg-type]
                hop_luu=pe.hop_luu,
                trade_line=tl,
                mt5_ticket=None,
                status="vung_cho",
                source=source,
            )
        )
    return zones

