"""Persistence for Responses thread id and zone price JSON files under data/{{SYMBOL}}/."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from automation_tool.config import symbol_data_dir

# Per-plan alert lifecycle for tv-journal-monitor (persisted in last_alert_prices.json).
VUNG_CHO = "vung_cho"
VAO_LENH = "vao_lenh"
CHO_TP1 = "cho_tp1"
LOAI = "loai"
AlertTerminalStatus = Literal["vao_lenh", "cho_tp1", "loai"]
PLAN_LABELS_DEFAULT: tuple[str, str, str] = ("plan_chinh", "plan_phu", "scalp")
# Float compare: treat as "same price" when syncing merge (no status reset).
_PRICE_MERGE_EPS = 1e-9


def _price_equal(a: float, b: float) -> bool:
    return abs(a - b) <= _PRICE_MERGE_EPS


def default_last_response_id_path() -> Path:
    return symbol_data_dir() / "last_response_id.txt"


def default_morning_baseline_prices_path() -> Path:
    return symbol_data_dir() / "morning_baseline_prices.json"


def default_last_alert_prices_path() -> Path:
    return symbol_data_dir() / "last_alert_prices.json"


def journal_monitor_first_run_path(last_alert_path: Optional[Path] = None) -> Path:
    """Cùng thư mục với ``last_alert_prices.json`` — ``journal_monitor_first_run.json``."""
    base = last_alert_path or default_last_alert_prices_path()
    return base.parent / "journal_monitor_first_run.json"


def write_journal_monitor_first_run(
    *,
    started_at: datetime,
    session_cutoff_end: datetime,
    timezone_name: str,
    last_alert_path: Optional[Path] = None,
) -> Path:
    """Ghi lần chạy monitor: thời điểm bắt đầu + mốc dừng theo quy tắc ca sáng/chiều."""
    path = journal_monitor_first_run_path(last_alert_path)
    data: dict[str, Any] = {
        "started_at": started_at.isoformat(),
        "session_cutoff_end": session_cutoff_end.isoformat(),
        "timezone": timezone_name,
    }
    _atomic_write_json(path, data)
    return path


def read_journal_monitor_first_run(
    last_alert_path: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    if not journal_monitor_first_run_path(last_alert_path).is_file():
        return None
    raw = journal_monitor_first_run_path(last_alert_path).read_text(encoding="utf-8")
    return json.loads(raw)


def _atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, data: Any) -> None:
    raw = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    _atomic_write_text(path, raw)


def read_last_response_id(path: Optional[Path] = None) -> Optional[str]:
    p = path or default_last_response_id_path()
    if not p.is_file():
        return None
    line = p.read_text(encoding="utf-8").strip()
    return line or None


def write_last_response_id(response_id: str, path: Optional[Path] = None) -> None:
    p = path or default_last_response_id_path()
    _atomic_write_text(p, response_id.strip() + "\n")


@dataclass(frozen=True)
class MorningBaselinePrices:
    prices: tuple[float, float, float]
    labels: tuple[str, str, str] = ("plan_chinh", "plan_phu", "scalp")
    updated_at: str = ""

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "prices": list(self.prices),
            "labels": list(self.labels),
            "updated_at": self.updated_at or datetime.now(timezone.utc).isoformat(),
        }


def read_morning_baseline_prices(path: Optional[Path] = None) -> Optional[MorningBaselinePrices]:
    p = path or default_morning_baseline_prices_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    prices = data.get("prices")
    if not isinstance(prices, list) or len(prices) != 3:
        return None
    try:
        tup = tuple(float(x) for x in prices)
    except (TypeError, ValueError):
        return None
    labels = data.get("labels")
    if isinstance(labels, list) and len(labels) == 3:
        lab = tuple(str(x) for x in labels)
    else:
        lab = ("plan_chinh", "plan_phu", "scalp")
    ts = str(data.get("updated_at") or "")
    return MorningBaselinePrices(prices=(tup[0], tup[1], tup[2]), labels=lab, updated_at=ts)


def write_morning_baseline_prices(
    prices: tuple[float, float, float],
    path: Optional[Path] = None,
) -> None:
    p = path or default_morning_baseline_prices_path()
    mb = MorningBaselinePrices(prices=prices)
    _atomic_write_json(p, mb.to_json_dict())


@dataclass
class LastAlertState:
    """Snapshot of ``last_alert_prices.json`` including per-plan journal status.

    ``entry_manual_by_label``: True = vào lệnh thủ công (ghi tay ngoài bot); False = qua tool/MT5.
    ``trade_line_by_label`` / ``mt5_ticket_by_label``: sau auto-MT5 thành công (TP1 pipeline).
    ``tp1_followup_done_by_label``: đã gửi follow-up TP1 cho lần ``cho_tp1`` hiện tại (tránh spam).
    """

    prices: tuple[float, float, float]
    labels: tuple[str, str, str] = PLAN_LABELS_DEFAULT
    status_by_label: dict[str, str] = field(default_factory=dict)
    entry_manual_by_label: dict[str, bool] = field(default_factory=dict)
    trade_line_by_label: dict[str, str] = field(default_factory=dict)
    mt5_ticket_by_label: dict[str, int] = field(default_factory=dict)
    tp1_followup_done_by_label: dict[str, bool] = field(default_factory=dict)
    updated_at: str = ""

    def to_json_dict(self) -> dict[str, Any]:
        ts = self.updated_at or datetime.now(timezone.utc).isoformat()
        out: dict[str, Any] = {
            "prices": list(self.prices),
            "labels": list(self.labels),
            "status_by_label": {k: self.status_by_label.get(k, VUNG_CHO) for k in self.labels},
            "entry_manual_by_label": {
                k: bool(self.entry_manual_by_label.get(k, False)) for k in self.labels
            },
            "trade_line_by_label": {
                k: str(self.trade_line_by_label.get(k, "") or "") for k in self.labels
            },
            "mt5_ticket_by_label": {
                k: int(self.mt5_ticket_by_label[k])
                for k in self.labels
                if k in self.mt5_ticket_by_label
            },
            "tp1_followup_done_by_label": {
                k: bool(self.tp1_followup_done_by_label.get(k, False)) for k in self.labels
            },
            "updated_at": ts,
        }
        return out


def read_last_alert_state(path: Optional[Path] = None) -> Optional[LastAlertState]:
    """Parse last alert file; missing ``status_by_label`` → all ``vung_cho``."""
    p = path or default_last_alert_prices_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    prices = data.get("prices")
    if not isinstance(prices, list) or len(prices) != 3:
        return None
    try:
        tup = (float(prices[0]), float(prices[1]), float(prices[2]))
    except (TypeError, ValueError):
        return None
    labels_raw = data.get("labels")
    if isinstance(labels_raw, list) and len(labels_raw) == 3:
        labels = tuple(str(x) for x in labels_raw)
    else:
        labels = PLAN_LABELS_DEFAULT

    status_by_label: dict[str, str] = {}
    sb = data.get("status_by_label")
    if isinstance(sb, dict):
        for lab in labels:
            v = sb.get(lab)
            if isinstance(v, str) and v.strip():
                status_by_label[lab] = v.strip()
            else:
                status_by_label[lab] = VUNG_CHO
    else:
        st_list = data.get("statuses")
        if isinstance(st_list, list) and len(st_list) == 3:
            for i, lab in enumerate(labels):
                v = st_list[i]
                status_by_label[lab] = str(v).strip() if isinstance(v, str) and v.strip() else VUNG_CHO
        else:
            for lab in labels:
                status_by_label[lab] = VUNG_CHO

    entry_manual_by_label: dict[str, bool] = {}
    emb = data.get("entry_manual_by_label")
    if isinstance(emb, dict):
        for lab in labels:
            v = emb.get(lab)
            entry_manual_by_label[lab] = bool(v) if isinstance(v, bool) else False
    else:
        for lab in labels:
            entry_manual_by_label[lab] = False

    trade_line_by_label: dict[str, str] = {}
    tl = data.get("trade_line_by_label")
    if isinstance(tl, dict):
        for lab in labels:
            v = tl.get(lab)
            trade_line_by_label[lab] = str(v).strip() if isinstance(v, str) else ""
    else:
        for lab in labels:
            trade_line_by_label[lab] = ""

    mt5_ticket_by_label: dict[str, int] = {}
    mtk = data.get("mt5_ticket_by_label")
    if isinstance(mtk, dict):
        for lab in labels:
            v = mtk.get(lab)
            if v is None:
                continue
            try:
                mt5_ticket_by_label[lab] = int(v)
            except (TypeError, ValueError):
                pass

    tp1_done: dict[str, bool] = {}
    td = data.get("tp1_followup_done_by_label")
    if isinstance(td, dict):
        for lab in labels:
            v = td.get(lab)
            tp1_done[lab] = bool(v) if isinstance(v, bool) else False
    else:
        for lab in labels:
            tp1_done[lab] = False

    ts = str(data.get("updated_at") or "")
    return LastAlertState(
        prices=tup,
        labels=labels,
        status_by_label=status_by_label,
        entry_manual_by_label=entry_manual_by_label,
        trade_line_by_label=trade_line_by_label,
        mt5_ticket_by_label=mt5_ticket_by_label,
        tp1_followup_done_by_label=tp1_done,
        updated_at=ts,
    )


def write_last_alert_state(state: LastAlertState, path: Optional[Path] = None) -> None:
    p = path or default_last_alert_prices_path()
    st = LastAlertState(
        prices=state.prices,
        labels=state.labels,
        status_by_label=dict(state.status_by_label),
        entry_manual_by_label=dict(state.entry_manual_by_label),
        trade_line_by_label=dict(state.trade_line_by_label),
        mt5_ticket_by_label=dict(state.mt5_ticket_by_label),
        tp1_followup_done_by_label=dict(state.tp1_followup_done_by_label),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    _atomic_write_json(p, st.to_json_dict())


def merge_alert_prices_with_status(
    old: Optional[LastAlertState],
    new_prices: tuple[float, float, float],
) -> LastAlertState:
    """
    When persisting a new triple: for each label, if price changed vs ``old``,
    reset that plan to ``vung_cho``; otherwise keep prior status.
    """
    labels = old.labels if old is not None else PLAN_LABELS_DEFAULT
    if old is None:
        return LastAlertState(
            prices=new_prices,
            labels=labels,
            status_by_label={lab: VUNG_CHO for lab in labels},
            entry_manual_by_label={lab: False for lab in labels},
            trade_line_by_label={lab: "" for lab in labels},
            mt5_ticket_by_label={},
            tp1_followup_done_by_label={lab: False for lab in labels},
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
    new_status: dict[str, str] = {}
    new_manual: dict[str, bool] = {}
    new_tl = {lab: old.trade_line_by_label.get(lab, "") for lab in labels}
    new_tk = {k: v for k, v in old.mt5_ticket_by_label.items() if k in labels}
    new_tp1d = {lab: old.tp1_followup_done_by_label.get(lab, False) for lab in labels}
    for i, lab in enumerate(labels):
        op = old.prices[i]
        np = new_prices[i]
        if not _price_equal(op, np):
            new_status[lab] = VUNG_CHO
            new_manual[lab] = False
            new_tl[lab] = ""
            new_tk.pop(lab, None)
            new_tp1d[lab] = False
        else:
            new_status[lab] = old.status_by_label.get(lab, VUNG_CHO)
            new_manual[lab] = old.entry_manual_by_label.get(lab, False)
    return LastAlertState(
        prices=new_prices,
        labels=labels,
        status_by_label=new_status,
        entry_manual_by_label=new_manual,
        trade_line_by_label=new_tl,
        mt5_ticket_by_label=new_tk,
        tp1_followup_done_by_label=new_tp1d,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


def update_single_plan_status(
    label: str,
    status: str,
    path: Optional[Path] = None,
    *,
    entry_manual: Optional[bool] = None,
) -> None:
    """Set one plan's status and rewrite ``last_alert_prices.json``.

    ``entry_manual``: nếu không None, ghi nhận vào ``entry_manual_by_label`` cho label đó
    (ví dụ ``False`` khi vào lệnh qua bot/MT5).
    """
    st = read_last_alert_state(path)
    if st is None:
        raise SystemExit(
            f"No last alert state at {path or default_last_alert_prices_path()} — cannot update status."
        )
    if label not in st.labels:
        raise SystemExit(f"Unknown plan label {label!r}; expected one of {st.labels}.")
    d = {**st.status_by_label, label: status}
    em = {**st.entry_manual_by_label}
    if entry_manual is not None:
        em[label] = entry_manual
    write_last_alert_state(
        LastAlertState(
            prices=st.prices,
            labels=st.labels,
            status_by_label=d,
            entry_manual_by_label=em,
            trade_line_by_label=dict(st.trade_line_by_label),
            mt5_ticket_by_label=dict(st.mt5_ticket_by_label),
            tp1_followup_done_by_label=dict(st.tp1_followup_done_by_label),
            updated_at=st.updated_at,
        ),
        path=path,
    )


def no_waiting_zones(state: LastAlertState) -> bool:
    """True when no plan is still ``vung_cho`` (all touched or beyond)."""
    for lab in state.labels:
        if state.status_by_label.get(lab, VUNG_CHO) == VUNG_CHO:
            return False
    return True


def needs_post_entry_price_watch(state: LastAlertState) -> bool:
    """True when a plan is ``vao_lenh``/``cho_tp1`` with saved trade_line + MT5 ticket (TP1 pipeline)."""
    for lab in state.labels:
        s = state.status_by_label.get(lab, VUNG_CHO)
        if s not in (VAO_LENH, CHO_TP1):
            continue
        tl = (state.trade_line_by_label.get(lab) or "").strip()
        tk = state.mt5_ticket_by_label.get(lab)
        if tl and tk is not None and int(tk) > 0:
            return True
    return False


def watchlist_journal_active_work(state: LastAlertState) -> bool:
    """Còn việc cho watchlist/journal: còn vùng chờ hoặc còn theo dõi TP1 sau vào lệnh."""
    return (not no_waiting_zones(state)) or needs_post_entry_price_watch(state)


def all_plans_terminal(state: LastAlertState) -> bool:
    """True when every plan is past ``vung_cho`` (``vao_lenh``, ``cho_tp1``, or ``loai``)."""
    for lab in state.labels:
        s = state.status_by_label.get(lab, VUNG_CHO)
        if s == VUNG_CHO:
            return False
    return True


def update_plan_mt5_entry(
    label: str,
    *,
    trade_line: str,
    mt5_ticket: int,
    path: Optional[Path] = None,
) -> None:
    """Ghi ``trade_line`` và ticket MT5 cho một plan (sau ``execute_trade`` thành công)."""
    st = read_last_alert_state(path)
    if st is None:
        raise SystemExit(
            f"No last alert state at {path or default_last_alert_prices_path()} — cannot update MT5 entry."
        )
    if label not in st.labels:
        raise SystemExit(f"Unknown plan label {label!r}; expected one of {st.labels}.")
    tl = dict(st.trade_line_by_label)
    tk = dict(st.mt5_ticket_by_label)
    tl[label] = trade_line.strip()
    tk[label] = int(mt5_ticket)
    write_last_alert_state(
        LastAlertState(
            prices=st.prices,
            labels=st.labels,
            status_by_label=dict(st.status_by_label),
            entry_manual_by_label=dict(st.entry_manual_by_label),
            trade_line_by_label=tl,
            mt5_ticket_by_label=tk,
            tp1_followup_done_by_label=dict(st.tp1_followup_done_by_label),
            updated_at=st.updated_at,
        ),
        path=path,
    )


def update_plan_tp1_followup_done(
    label: str,
    done: bool,
    path: Optional[Path] = None,
) -> None:
    st = read_last_alert_state(path)
    if st is None:
        raise SystemExit(f"No last alert state at {path or default_last_alert_prices_path()}.")
    if label not in st.labels:
        raise SystemExit(f"Unknown plan label {label!r}.")
    d = {**st.tp1_followup_done_by_label, label: done}
    write_last_alert_state(
        LastAlertState(
            prices=st.prices,
            labels=st.labels,
            status_by_label=dict(st.status_by_label),
            entry_manual_by_label=dict(st.entry_manual_by_label),
            trade_line_by_label=dict(st.trade_line_by_label),
            mt5_ticket_by_label=dict(st.mt5_ticket_by_label),
            tp1_followup_done_by_label=d,
            updated_at=st.updated_at,
        ),
        path=path,
    )


def clear_plan_mt5_fields(label: str, path: Optional[Path] = None) -> None:
    """Xoá trade_line/ticket/tp1_done cho một label (sau ``loại`` hoặc reset thủ công)."""
    st = read_last_alert_state(path)
    if st is None:
        return
    if label not in st.labels:
        return
    tl = dict(st.trade_line_by_label)
    tk = dict(st.mt5_ticket_by_label)
    td = dict(st.tp1_followup_done_by_label)
    tl[label] = ""
    tk.pop(label, None)
    td[label] = False
    write_last_alert_state(
        LastAlertState(
            prices=st.prices,
            labels=st.labels,
            status_by_label=dict(st.status_by_label),
            entry_manual_by_label=dict(st.entry_manual_by_label),
            trade_line_by_label=tl,
            mt5_ticket_by_label=tk,
            tp1_followup_done_by_label=td,
            updated_at=st.updated_at,
        ),
        path=path,
    )


def read_last_alert_prices(path: Optional[Path] = None) -> Optional[tuple[float, float, float]]:
    st = read_last_alert_state(path)
    if st is None:
        return None
    return st.prices


def write_last_alert_prices(
    prices: tuple[float, float, float],
    path: Optional[Path] = None,
) -> None:
    """Persist triple and merge statuses: only reset status for labels whose price changed."""
    p = path or default_last_alert_prices_path()
    old = read_last_alert_state(p)
    merged = merge_alert_prices_with_status(old, prices)
    write_last_alert_state(merged, path=p)


def remove_last_alert_prices_file(path: Optional[Path] = None) -> bool:
    """
    Xóa ``last_alert_prices.json`` nếu tồn tại (vd. đầu phiên ``all`` để bắt đầu sạch).

    Returns:
        ``True`` nếu đã xóa file, ``False`` nếu file không có.
    """
    p = path or default_last_alert_prices_path()
    if p.is_file():
        p.unlink()
        return True
    return False
