from __future__ import annotations

import json
import logging
import math
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Literal, Optional

from automation_tool.browser_client import BrowserClient, is_service_responding
from automation_tool.browser_protocol import (
    METHOD_CLOSE_TAB,
    METHOD_QUERY_TEXT,
    METHOD_TV_WATCHLIST_INIT,
)
from automation_tool.coinmap import (
    load_coinmap_yaml,
)
from automation_tool.coinmap_merged import write_openai_coinmap_merged_from_raw_export
from automation_tool.config import Settings, default_gocharting_config_path
from automation_tool.images import (
    DEFAULT_MAIN_CHART_SYMBOL,
    GOCHARTING_GOLD_EXPORT_LABEL,
    coinmap_main_pair_interval_json_path,
    coinmap_xauusd_5m_json_path,
    get_active_main_symbol,
    read_main_chart_symbol,
)
from automation_tool.mt5_accounts import (
    MT5AccountEntry,
    SOURCE_UPDATE_SCALP,
    filter_mt5_accounts_for_zone_entry,
    is_plan_chinh_family,
    is_plan_phu_family,
    load_mt5_accounts_for_cli,
    load_mt5_accounts_for_zone_entry,
    primary_account,
    subset_accounts_json_basename,
    trade_with_update_scalp_entry_lot_default,
)
from automation_tool.mt5_execute import (
    DaemonPlanMt5PriceSession,
    execute_trade,
    format_mt5_execution_for_telegram,
)
from automation_tool.mt5_multi import (
    execute_trade_all_accounts,
    format_mt5_multi_chinh_for_telegram,
    format_mt5_multi_for_telegram,
    format_mt5_multi_manage_for_telegram,
    mt5_cancel_pending_or_close_all_accounts,
    mt5_chinh_trade_line_all_accounts,
    mt5_partial_close_tp1_all_accounts,
)
from automation_tool.mt5_openai_parse import (
    is_last_price_hit_stop_loss,
    inject_filled_price_into_trade_line,
    parse_journal_intraday_action_from_openai_text,
    parse_openai_output_md,
)
from automation_tool.mt5_manage import (
    mt5_cancel_all_pending_orders,
    mt5_cancel_pending_or_close_position,
    mt5_cancel_pending_order,
    mt5_chinh_trade_line_inplace,
    mt5_close_position_partial,
    mt5_ticket_current_sltp,
    mt5_ticket_is_open_position,
    mt5_ticket_still_open,
    mt5_ticket_status_for_cutoff,
)
from automation_tool.openai_errors import re_raise_unless_openai
from automation_tool.openai_prompt_flow import (
    POST_FILL_MANAGEMENT_USER_TEMPLATE,
    TP1_POST_TOUCH_USER_TEMPLATE,
    run_single_followup_responses,
)

from automation_tool.state_files import read_last_all_response_id, read_last_response_id, write_last_response_id
from automation_tool.tp1_followup import extract_trade_management_reason
from automation_tool.telegram_bot import (
    mt5_zone_label_display_vn,
    send_message,
    send_mt5_execution_log_to_ngan_gon_chat,
    send_openai_output_to_telegram,
    send_phan_tich_alert_to_python_bot_if_any,
    send_trade_management_reason_notice,
    send_user_friendly_notice,
)
from automation_tool.openai_analysis_json import (
    ARM_THRESHOLD_TP1_DEFAULT,
    ZONE_LABELS_ORDER,
    arm_threshold_tp1_for_label,
    auto_mt5_hop_luu_passes_for_label,
    auto_mt5_hop_luu_threshold_for_label,
    is_scalp_label,
    parse_vung_cho_bounds,
)
from automation_tool.daemon_launcher import (
    reconcile_daemon_plans_at_boot,
    register_daemon_plan_pidfile_for_current_process,
    register_stop_daemon_plans_on_exit,
)
from automation_tool.last_price_ipc import (
    read_last_price_for_daemon_plan,
    open_writer_shared_memory_v2,
    read_last_prices_for_daemon_plan,
    write_last_prices_shared,
)
from automation_tool.zones_paths import (
    SLOTS_ORDER,
    SessionSlot,
    default_last_price_path,
    default_zones_dir,
    label_from_shard_stem,
    read_last_price_file,
    resolve_second_flow,
    resolve_session_slot_raw,
    session_slot_display_vn,
    session_slot_from_shard_path,
    write_last_price_file,
)
from automation_tool.zones_state import (
    Zone,
    ZonesState,
    read_manifest_updated_at,
    read_zones_state,
    read_zones_state_from_shard,
    write_zones_state,
    write_zones_state_to_shard,
)
from automation_tool.tradingview_symbol_last import parse_tv_symbol_last_value

_log = logging.getLogger("automation_tool.tv_watchlist_daemon")


def _poll_terminal_only_logger() -> logging.Logger:
    """
    Chỉ stderr — không propagate lên ``automation_tool`` → không qua TelegramLogHandler.
    Dùng cho tick mỗi vòng poll; heartbeat Telegram vẫn dùng ``_log.info`` (mỗi ~5 phút).
    """
    name = "automation_tool.tv_watchlist_daemon.poll_tick"
    lg = logging.getLogger(name)
    if lg.handlers:
        return lg
    lg.setLevel(logging.INFO)
    lg.propagate = False
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(message)s"))
    lg.addHandler(h)
    return lg


_poll_terminal = _poll_terminal_only_logger()

# Zone touch: touch if Last is within vung_cho bounds (inclusive; eps expands bounds).
_EPS_DEFAULT = 0.0
_TP1_EPS = 0.01
# Re-export default cho test (plan_chinh / plan_phu).
_ARM_THRESHOLD = ARM_THRESHOLD_TP1_DEFAULT
_RETRY_WAIT_MINUTES = 15
_RETRY_WAIT_MINUTES_SCALP = 10
_ZONE_TOUCH_LOAI_CONFIRM_ROUNDS = 3
_M5_ANALYSIS_CAPTURE_SLOT_MINUTE_REMAINDER = 1
_M5_ANALYSIS_CAPTURE_POST_SLOT_BUFFER_SECONDS = 120
DAEMON_PLAN_AUTO_CUTOFF_HOUR = 0
DAEMON_PLAN_AUTO_CUTOFF_MINUTE = 0
DAEMON_PLAN_AUTO_CUTOFF_FRIDAY_HOUR = 0
DAEMON_PLAN_AUTO_CUTOFF_FRIDAY_MINUTE = 0
POST_FILL_MANAGE_DELAY_MINUTES = 5


def _is_scalp_zone(zone: Zone) -> bool:
    return is_scalp_label(zone.label or "")


def _zone_touch_coinmap_main_json_path(charts_dir: Path, zone: Zone) -> tuple[Optional[Path], str]:
    """Scalp: JSON M1; plan_chinh / plan_phu: M5. Trả về (path, suffix log như ``1m`` / ``5m``)."""
    if _is_scalp_zone(zone):
        p = coinmap_main_pair_interval_json_path(charts_dir, "1m")
        return p, "1m"
    p = coinmap_xauusd_5m_json_path(charts_dir)
    return p, "5m"


# Daemon-plan: Last chạm SL theo trade_line → loại (vùng chờ/chạm hoặc đã vào lệnh / chờ TP1).
_DAEMON_PLAN_SL_LOAI_STATUSES = frozenset({"vung_cho", "cham", "vao_lenh", "cho_tp1"})


# TradingView symbol page Last value.
_TV_SYMBOL_LAST_SELECTOR = '[data-qa-id="symbol-last-value"]'


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _m5_analysis_capture_slot_wait_seconds(now: Optional[datetime] = None) -> int:
    """
    M5 data gửi AI chờ tới phút ``5n + 1`` (:01, :06, :11...)
    rồi buffer thêm 2 phút để Coinmap kịp cập nhật.
    """
    current = now or _now_utc()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    wait_to_slot = 0
    if current.minute % 5 == _M5_ANALYSIS_CAPTURE_SLOT_MINUTE_REMAINDER:
        return _M5_ANALYSIS_CAPTURE_POST_SLOT_BUFFER_SECONDS
    minutes_until_slot = (
        _M5_ANALYSIS_CAPTURE_SLOT_MINUTE_REMAINDER - current.minute
    ) % 5
    seconds_until_slot = (
        minutes_until_slot * 60 - current.second - current.microsecond / 1_000_000
    )
    wait_to_slot = max(0, int(math.ceil(seconds_until_slot)))
    return wait_to_slot + _M5_ANALYSIS_CAPTURE_POST_SLOT_BUFFER_SECONDS


def _third_future_m5_analysis_capture_slot(now: Optional[datetime] = None) -> datetime:
    """
    Return the capture slot for the 3rd future closed M5 candle.

    M5 data first waits for minute ``5n + 1`` then adds a 2-minute
    Coinmap buffer. If current time is 08:13, base slots are 08:16,
    08:21, 08:26, so retry at 08:28 after the buffer.
    """
    current = now or _now_utc()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)

    slot = current.replace(second=0, microsecond=0)
    minutes_until_next_slot = (
        _M5_ANALYSIS_CAPTURE_SLOT_MINUTE_REMAINDER - slot.minute
    ) % 5
    if minutes_until_next_slot == 0 and slot <= current:
        minutes_until_next_slot = 5
    slot = slot + timedelta(minutes=minutes_until_next_slot)
    return slot + timedelta(minutes=10, seconds=_M5_ANALYSIS_CAPTURE_POST_SLOT_BUFFER_SECONDS)


def _wait_for_m5_analysis_capture_slot(
    settings: Settings,
    *,
    source: str,
    params: Optional[WatchlistDaemonParams] = None,
    zone: Optional[Zone] = None,
) -> None:
    wait_s = _m5_analysis_capture_slot_wait_seconds()
    if wait_s <= 0:
        return
    target = _now_utc() + timedelta(seconds=wait_s)
    _send_log(
        settings,
        f"[m5-slot] {source}: chờ {wait_s}s để lấy Coinmap M5 tại phút 5n+1 + buffer 2p "
        f"(target={target.isoformat()})",
    )
    _send_user_notice(
        settings,
        "Đang chờ mốc lấy dữ liệu M5.",
        f"Sẽ lấy Coinmap M5 tại phút 5n+1 rồi buffer thêm 2 phút trước khi gửi AI phân tích. Dự kiến chờ {wait_s} giây.",
        zone=zone,
        params=params,
    )
    time.sleep(wait_s)


def _retry_at_iso(minutes: int = _RETRY_WAIT_MINUTES) -> str:
    return (_now_utc() + timedelta(minutes=int(minutes))).isoformat()


def _zone_touch_retry_at_iso(zone: Zone) -> str:
    if _is_scalp_zone(zone):
        return _retry_at_iso(_RETRY_WAIT_MINUTES_SCALP)
    return _third_future_m5_analysis_capture_slot().isoformat()


def _zone_touch_after_retry_title(zone: Zone) -> str:
    if _is_scalp_zone(zone):
        return f"Sau {_RETRY_WAIT_MINUTES_SCALP}p giá chạm vùng chờ."
    return "Sau 3 nến M5 giá chạm vùng chờ."


def _is_retry_due(retry_at: str) -> bool:
    s = (retry_at or "").strip()
    if not s:
        return False
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt <= _now_utc()
    except Exception:
        return False


def _zone_touch_notify_cooldown_active(zone: Zone) -> bool:
    """True khi chưa hết cooldown sau lần báo chạm vùng (chế độ tắt OpenAI)."""
    raw = (getattr(zone, "zone_touch_notify_cooldown_until", "") or "").strip()
    if not raw:
        return False
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return _now_utc() < dt
    except Exception:
        return False


def _settings_skip_intraday_alert_openai(settings: Any) -> bool:
    """Chỉ ``True`` thật (tránh object mock truthy trong test)."""
    return getattr(settings, "skip_intraday_alert_openai", False) is True


def _settings_skip_intraday_alert_cooldown_seconds(settings: Any) -> int:
    raw = getattr(settings, "skip_intraday_alert_cooldown_seconds", 120)
    if isinstance(raw, bool):
        return 120
    if type(raw) is int:
        return max(0, raw)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 120


def _settings_skip_trade_management(settings: Any) -> bool:
    """Chỉ ``True`` thật (tránh object mock truthy trong test)."""
    return getattr(settings, "skip_trade_management", False) is True


@dataclass(frozen=True)
class WatchlistDaemonParams:
    coinmap_tv_yaml: Path
    capture_coinmap_yaml: Path
    charts_dir: Path
    storage_state_path: Optional[Path]
    headless: bool
    no_save_storage: bool
    poll_seconds: float = 1.0
    timezone_name: str = "Asia/Ho_Chi_Minh"
    no_telegram: bool = False
    mt5_execute: bool = True
    mt5_symbol: Optional[str] = None
    mt5_dry_run: bool = False
    zones_state_path: Optional[Path] = None
    """One shard JSON (``daemon-plan``); when set, read/write only this file."""
    shard_path: Optional[Path] = None
    """Optional ``last.txt`` when :attr:`mirror_last_price_file` is True; else IPC only."""
    last_price_path: Optional[Path] = None
    mirror_last_price_file: bool = False
    """Also write atomic ``last.txt`` (legacy/debug); primary Last is ``multiprocessing.shared_memory``."""
    stop_daemon_plans_on_exit: bool = False
    """On process exit (Ctrl+C, atexit, Windows console close): SIGTERM tracked ``daemon-plan`` PIDs."""
    eps: float = _EPS_DEFAULT  # max |Δ| between Last and touch ref (default 0.0)
    openai_model: Optional[str] = None
    openai_model_cli: Optional[str] = None
    mt5_accounts_json: Optional[Path] = None
    stop_at_hour: Optional[int] = None
    """``None`` = auto theo shard; ``-1`` = không cắt giờ; 0 + phút 0 = 00:00 ngày kế; 1-23 = mốc cùng ngày."""
    stop_at_minute: int = 0
    """Phút đi kèm ``stop_at_hour`` (mặc định 0)."""
    last_price_from_mt5: bool = True
    """Daemon giá: ``True`` = đọc MT5 bid → shared memory; ``False`` = TradingView symbol page (legacy)."""
    mt5_stale_reconnect_seconds: float = 10.0
    """Bid không đổi trong khoảng này (giây) thì gọi lại ``ensure_mt5_session``; ``0`` = tắt."""


def _daemon_gia_same_bid(a: float, b: float) -> bool:
    """So sánh bid liên tiếp (tránh float noise)."""
    return abs(float(a) - float(b)) <= 1e-5


_DAEMON_GIA_AUTO_STOP_HOUR = 0
_DAEMON_GIA_AUTO_STOP_MINUTE = 15


def _daemon_gia_compute_stop_deadline(tz_name: str) -> datetime:
    """Tính mốc dừng tự động 00:30 (12h30 đêm) theo múi giờ ``tz_name``.

    - Nếu chưa đến 00:30 hôm nay → mốc là hôm nay 00:30.
    - Nếu đã qua 00:30 → mốc là **ngày mai** 00:30.
    """
    z = ZoneInfo(tz_name)
    now = datetime.now(z)
    candidate = datetime.combine(
        now.date(),
        dt_time(_DAEMON_GIA_AUTO_STOP_HOUR, _DAEMON_GIA_AUTO_STOP_MINUTE),
        tzinfo=z,
    )
    if now >= candidate:
        candidate += timedelta(days=1)
    return candidate


def _daemon_gia_cancel_all_pending_at_shutdown(
    accounts_json: Optional[Path],
    *,
    dry_run: bool = False,
    settings: Settings,
) -> None:
    """Huỷ toàn bộ pending orders trên tất cả tài khoản trong ``accounts.json`` trước khi dừng daemon giá."""
    accounts = load_mt5_accounts_for_cli(accounts_json)
    if not accounts:
        _send_log(settings, "[daemon-gia] shutdown | không có accounts.json — bỏ qua huỷ pending")
        return

    total_ok = 0
    total_fail = 0
    all_msgs: list[str] = []
    for acc in accounts:
        try:
            n_ok, n_fail, msgs = mt5_cancel_all_pending_orders(
                dry_run=dry_run,
                terminal_path=acc.terminal_path,
                login=acc.login,
                password=acc.password,
                server=acc.server,
            )
            total_ok += n_ok
            total_fail += n_fail
            for m in msgs:
                all_msgs.append(f"acc={acc.id}: {m}")
        except Exception as e:
            total_fail += 1
            all_msgs.append(f"acc={acc.id}: lỗi {e}")

    detail = " | ".join(all_msgs) if all_msgs else "(không có)"
    _send_log(
        settings,
        f"[daemon-gia] shutdown | huỷ pending toàn bộ account: ok={total_ok} fail={total_fail} | {detail}",
    )


def _daemon_gia_stale_reensure_due(
    state: dict[str, Any],
    p_last: Optional[float],
    *,
    stale_s: float,
    now_m: float,
) -> bool:
    """Return True once when bid is unchanged for ``stale_s`` seconds."""
    if stale_s <= 0:
        return False
    if p_last is None:
        state.clear()
        return False
    pl = float(p_last)
    prev = state.get("bid")
    if prev is None:
        state["bid"] = pl
        state["changed_at"] = float(now_m)
        return False
    if not _daemon_gia_same_bid(pl, float(prev)):
        state["bid"] = pl
        state["changed_at"] = float(now_m)
        return False
    changed_at = float(state.get("changed_at", now_m))
    if (float(now_m) - changed_at) < float(stale_s):
        return False
    state["changed_at"] = float(now_m)
    return True


def compute_daemon_plan_stop_deadline_local(
    started_at: datetime,
    timezone_name: str,
    stop_hour: int,
    stop_minute: int = 0,
) -> datetime:
    """
    - ``stop_hour == 0`` và ``stop_minute == 0``: **12h đêm** = 24h = ``00:00`` **ngày kế** (local).
    - Khác: mốc **cùng ngày dương lịch** với ``started_at`` tại ``stop_hour:stop_minute`` (đã qua → thoát sau khi hết ticket).
    Thoát khi ``now(local) >= mốc`` (sau khi kiểm tra ticket MT5).
    """
    z = ZoneInfo(timezone_name)
    s = started_at.astimezone(z)
    sh = int(stop_hour)
    sm = int(stop_minute)
    if sh == 0 and sm == 0:
        next_day = s.date() + timedelta(days=1)
        return datetime.combine(next_day, dt_time(0, 0), tzinfo=z)
    return s.replace(hour=sh, minute=sm, second=0, microsecond=0)


def _daemon_plan_session_datetime_from_updated_at(
    state_updated_at: Optional[str],
    timezone_name: str,
) -> Optional[datetime]:
    raw = (state_updated_at or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    z = ZoneInfo(timezone_name)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=z)
    return dt.astimezone(z)


def compute_daemon_plan_auto_stop_deadline_local(
    started_at: datetime,
    timezone_name: str,
    *,
    shard_path: Path | str,
    state_updated_at: Optional[str] = None,
) -> datetime:
    """
    Auto cutoff per zone shard:
    normal sessions stop from configured next-day cutoff; Friday sessions use their own cutoff.
    Shards are staggered by one minute in slot/label order.
    """
    z = ZoneInfo(timezone_name)
    started_local = started_at.astimezone(z) if started_at.tzinfo else started_at.replace(tzinfo=z)
    session_dt = _daemon_plan_session_datetime_from_updated_at(state_updated_at, timezone_name)
    if session_dt is None:
        session_dt = started_local

    sp = Path(shard_path)
    slot = session_slot_from_shard_path(sp)
    label = label_from_shard_stem(sp.stem)
    try:
        slot_index = SLOTS_ORDER.index(slot) if slot is not None else 0
    except ValueError:
        slot_index = 0
    try:
        label_index = ZONE_LABELS_ORDER.index((label or "").strip().lower())
    except ValueError:
        label_index = 0

    offset_minutes = slot_index * len(ZONE_LABELS_ORDER) + label_index
    if session_dt.weekday() == 4:
        base_hour = DAEMON_PLAN_AUTO_CUTOFF_FRIDAY_HOUR
        base_minute = DAEMON_PLAN_AUTO_CUTOFF_FRIDAY_MINUTE
    else:
        base_hour = DAEMON_PLAN_AUTO_CUTOFF_HOUR
        base_minute = DAEMON_PLAN_AUTO_CUTOFF_MINUTE
    next_day = session_dt.date() + timedelta(days=1)
    base_deadline = datetime.combine(next_day, dt_time(int(base_hour), int(base_minute)), tzinfo=z)
    return base_deadline + timedelta(minutes=offset_minutes)


def compute_daemon_plan_effective_stop_deadline_local(
    started_at: datetime,
    timezone_name: str,
    *,
    stop_at_hour: Optional[int],
    stop_at_minute: int = 0,
    shard_path: Path | str,
    state_updated_at: Optional[str] = None,
) -> Optional[datetime]:
    """Resolve daemon-plan cutoff: ``None`` = auto per shard, ``-1`` = disabled, otherwise manual."""
    if stop_at_hour is None:
        return compute_daemon_plan_auto_stop_deadline_local(
            started_at,
            timezone_name,
            shard_path=shard_path,
            state_updated_at=state_updated_at,
        )
    if int(stop_at_hour) < 0:
        return None
    return compute_daemon_plan_stop_deadline_local(
        started_at,
        timezone_name,
        int(stop_at_hour),
        int(stop_at_minute or 0),
    )


def _daemon_plan_collect_ticket_account_pairs(zone: Zone) -> list[tuple[Optional[str], int]]:
    """``(account_id | None = primary/env, ticket)`` — ưu tiên map đa tài khoản."""
    out: list[tuple[Optional[str], int]] = []
    tmap = zone.mt5_tickets_by_account or {}
    if tmap:
        for aid, tk in tmap.items():
            try:
                tki = int(tk)
            except (TypeError, ValueError):
                continue
            if tki > 0:
                out.append((str(aid).strip() or None, tki))
        return out
    try:
        tk = int(zone.mt5_ticket or 0)
    except (TypeError, ValueError):
        tk = 0
    if tk > 0:
        out.append((None, tk))
    return out


def _daemon_plan_unique_ticket_account_pairs(zones: list[Zone]) -> list[tuple[Optional[str], int]]:
    """Các cặp (account_id, ticket) duy nhất trong state (``mt5_tickets_by_account`` hoặc legacy ``mt5_ticket``)."""
    seen: set[tuple[Optional[str], int]] = set()
    out: list[tuple[Optional[str], int]] = []
    for z in zones:
        for acc_id, tk in _daemon_plan_collect_ticket_account_pairs(z):
            key = (acc_id, tk)
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
    return out


def _daemon_plan_cutoff_resolve_mt5_account(
    acc_id: Optional[str],
    accounts: Optional[list[MT5AccountEntry]],
) -> tuple[Literal["terminal", "api", "missing", "no_accounts_file"], Optional[MT5AccountEntry]]:
    """
    ``terminal``: chỉ có ``mt5_ticket`` đơn — dùng phiên MT5 đang mở.

    ``api``: có ``acc_id`` trong ``accounts.json`` — đăng nhập API theo từng acc.

    ``no_accounts_file`` / ``missing``: map đa acc trong state nhưng thiếu file hoặc thiếu id.
    """
    if acc_id is None:
        return "terminal", None
    if not accounts:
        return "no_accounts_file", None
    by_id = {a.id: a for a in accounts}
    acc = by_id.get(acc_id)
    if acc is None:
        return "missing", None
    return "api", acc


def daemon_plan_resolve_cutoff_mt5(
    zones: list[Zone],
    *,
    dry_run: bool,
    accounts_json: Optional[Path],
    settings: Settings,
    shard_tag: str,
) -> tuple[bool, str]:
    """
    Quá giờ cắt: **lệnh chờ** → huỷ rồi tiếp tục; **position đã khớp** → chặn thoát (chờ đóng);
    ticket đã đóng / không còn trên MT5 → không chặn.

    - State có ``mt5_tickets_by_account``: load ``accounts.json`` (CLI / env), huỷ/kiểm tra **từng** acc.
    - Chỉ ``mt5_ticket`` (legacy): như cũ — **phiên terminal** đang login (``initialize()`` không đối số).

    Trả ``(True, ...)`` nếu cần chờ thêm (còn position) hoặc lỗi MT5 / thiếu cấu hình; ``(False, ...)`` khi có thể kết thúc.
    """
    if dry_run:
        return False, "[daemon-plan] mt5_dry_run — bỏ qua cutoff MT5"
    pairs = _daemon_plan_unique_ticket_account_pairs(zones)
    if not pairs:
        return False, "no mt5_ticket in state"

    accounts = load_mt5_accounts_for_cli(accounts_json)

    def _status(
        acc_id: Optional[str], ticket: int
    ) -> tuple[Literal["pending", "position", "none", "error"], str]:
        mode, acc = _daemon_plan_cutoff_resolve_mt5_account(acc_id, accounts)
        if mode == "api":
            assert acc is not None
            return mt5_ticket_status_for_cutoff(
                ticket,
                dry_run=False,
                terminal_path=acc.terminal_path,
                login=acc.login,
                password=acc.password,
                server=acc.server,
            )
        return mt5_ticket_status_for_cutoff(ticket, dry_run=False)

    for acc_id, ticket in pairs:
        mode, acc = _daemon_plan_cutoff_resolve_mt5_account(acc_id, accounts)
        if mode == "no_accounts_file":
            return True, (
                "state có mt5_tickets_by_account nhưng không tìm thấy accounts.json "
                "(CLI --mt5-accounts-json hoặc MT5_ACCOUNTS_JSON) — không huỷ được theo acc"
            )
        if mode == "missing":
            return True, f"account id={acc_id!r} không có trong accounts.json — không huỷ được"

        st, msg = _status(acc_id, ticket)
        if st == "error":
            return True, msg
        if st != "pending":
            continue

        if mode == "api":
            assert acc is not None
            r = mt5_cancel_pending_order(
                ticket,
                dry_run=False,
                terminal_path=acc.terminal_path,
                login=acc.login,
                password=acc.password,
                server=acc.server,
                terminal_session_only=False,
                shutdown_after=True,
            )
            log_extra = f"acc={acc_id} | {r.message}"
        else:
            r = mt5_cancel_pending_order(
                ticket,
                dry_run=False,
                terminal_session_only=True,
                shutdown_after=False,
            )
            log_extra = r.message
        if not r.ok:
            return True, f"huỷ pending ticket={ticket}: {r.message}"
        _send_log(
            settings,
            f"[daemon-plan] quá giờ cắt — đã huỷ lệnh chờ | shard={shard_tag} | {log_extra}",
        )

    for acc_id, ticket in pairs:
        mode, acc = _daemon_plan_cutoff_resolve_mt5_account(acc_id, accounts)
        if mode == "no_accounts_file":
            return True, (
                "state có mt5_tickets_by_account nhưng không tìm thấy accounts.json "
                "(CLI --mt5-accounts-json hoặc MT5_ACCOUNTS_JSON) — không kiểm tra được position"
            )
        if mode == "missing":
            return True, f"account id={acc_id!r} không có trong accounts.json"

        st, msg = _status(acc_id, ticket)
        if st == "error":
            return True, msg
        if st == "position":
            who = acc_id or "terminal"
            return True, f"ticket={ticket} acc={who} còn position ({msg}) — chờ đóng lệnh"
    return False, "cutoff: không còn pending/position theo ticket trong state"


def daemon_plan_should_exit_if_mt5_tickets_closed(
    zones: list[Zone],
    *,
    dry_run: bool,
    accounts_json: Optional[Path],
    settings: Settings,
    shard_tag: str,
) -> tuple[bool, str]:
    """
    Nếu state có ``mt5_ticket`` / ``mt5_tickets_by_account`` và **mọi** ticket đó đều không còn
    trên MT5 (đã huỷ / chốt / đóng) → trả ``(True, ...)`` để ``daemon-plan`` thoát.

    Còn pending hoặc position trên bất kỳ ticket nào → tiếp tục. Lỗi kết nối MT5 → không thoát (thử lại sau).

    Map đa acc: cần ``accounts.json`` để kiểm tra từng acc; legacy ``mt5_ticket``: phiên terminal đang mở.
    """
    if dry_run:
        return False, "[daemon-plan] mt5_dry_run — bỏ qua kiểm tra ticket đã đóng"
    pairs = _daemon_plan_unique_ticket_account_pairs(zones)
    if not pairs:
        return False, "no mt5_ticket in state"

    accounts = load_mt5_accounts_for_cli(accounts_json)

    def _status(
        acc_id: Optional[str], ticket: int
    ) -> tuple[Literal["pending", "position", "none", "error"], str]:
        mode, acc = _daemon_plan_cutoff_resolve_mt5_account(acc_id, accounts)
        if mode == "api":
            assert acc is not None
            return mt5_ticket_status_for_cutoff(
                ticket,
                dry_run=False,
                terminal_path=acc.terminal_path,
                login=acc.login,
                password=acc.password,
                server=acc.server,
            )
        return mt5_ticket_status_for_cutoff(ticket, dry_run=False)

    for acc_id, ticket in pairs:
        mode, _acc = _daemon_plan_cutoff_resolve_mt5_account(acc_id, accounts)
        if mode == "no_accounts_file":
            return False, (
                "state có mt5_tickets_by_account nhưng không có accounts.json — không xác nhận ticket đã đóng"
            )
        if mode == "missing":
            return False, f"account id={acc_id!r} không có trong accounts.json"

        st, msg = _status(acc_id, ticket)
        if st == "error":
            return False, msg
        if st in ("pending", "position"):
            return False, f"ticket={ticket} còn ({st})"

    tickets_desc = ", ".join(f"{aid or 'terminal'}:{t}" for aid, t in pairs)
    _send_log(
        settings,
        f"[daemon-plan] ticket MT5 đã đóng — dừng | shard={shard_tag} | tickets=[{tickets_desc}]",
    )
    return True, f"mọi ticket đã đóng trên MT5: {tickets_desc}"


def _mark_daemon_plan_cutoff_loai(state: Optional[ZonesState]) -> Optional[Zone]:
    """Mark active daemon-plan zones as ``loai`` when cutoff finishes the shard."""
    if state is None or not state.zones:
        return None
    notice_zone: Optional[Zone] = None
    for zone in state.zones:
        if zone.status in ("done", "loai"):
            if notice_zone is None:
                notice_zone = zone
            continue
        zone.status = "loai"
        zone.retry_at = ""
        zone.loai_streak = 0
        if notice_zone is None:
            notice_zone = zone
    return notice_zone


def _state_read(params: WatchlistDaemonParams) -> Optional[ZonesState]:
    if params.shard_path is not None:
        return read_zones_state_from_shard(params.shard_path)
    return read_zones_state(params.zones_state_path)


def _state_write(params: WatchlistDaemonParams, st: ZonesState) -> None:
    if params.shard_path is not None:
        write_zones_state_to_shard(params.shard_path, st)
    else:
        write_zones_state(st, path=params.zones_state_path)


def _daemon_plan_response_id_path(shard_path: Path) -> Path:
    """
    File cạnh shard: luồng OpenAI riêng cho ``daemon-plan`` (ghi id mới ở đây, không ghi ``last_response_id.txt`` chính).

    Ví dụ: ``zones/vung_plan_chinh_sang.json`` → ``zones/vung_plan_chinh_sang.last_response_id.txt``
    (cùng thư mục; stem khớp tên file shard).
    """
    return shard_path.parent / f"{shard_path.stem}.last_response_id.txt"


def _openai_followup_prev_response_id(params: WatchlistDaemonParams) -> str:
    """
    ``daemon-plan``: ưu tiên id đã lưu trong sidecar shard; nếu chưa có thì **seed** từ ``last_response_id.txt`` chính (lần đầu nối bản phân tích sáng).
    Tv-watchlist không shard: chỉ đọc file chính.
    """
    if params.shard_path is not None:
        p = _daemon_plan_response_id_path(params.shard_path)
        s = (read_last_response_id(p) or "").strip()
        if s:
            return s
        return (read_last_response_id() or "").strip()
    return (read_last_response_id() or "").strip()


def _openai_followup_persist_new_id(params: WatchlistDaemonParams, new_id: str) -> None:
    """
    ``daemon-plan``: ghi id mới vào sidecar shard để lần sau chain trong cùng thread.
    Tiến trình tv-watchlist chính (không ``--shard``): không ghi ``last_response_id.txt`` (giữ hành vi cũ).

    Lưu ý: [INTRADAY_ALERT] / :func:`_zone_touch_job` chỉ gọi hàm này khi
    :func:`_should_write_intraday_alert_anchor` — lần đầu có anchor; các lần retry không ghi đè.
    """
    s = (new_id or "").strip()
    if not s:
        return
    if params.shard_path is None:
        return
    write_last_response_id(s, path=_daemon_plan_response_id_path(params.shard_path))


def _post_fill_prev_response_id(params: WatchlistDaemonParams) -> str:
    """
    ``previous_response_id`` cho post-fill [TRADE_MANAGEMENT]: tiếp nối FULL_ANALYSIS.

    1. Shard sidecar nếu có (chuỗi intraday cùng shard).
    2. ``last_all_response_id.txt`` từ lệnh ``all`` ([FULL_ANALYSIS]).
    Không fallback ``last_response_id.txt`` chung.
    """
    if params.shard_path is not None:
        p = _daemon_plan_response_id_path(params.shard_path)
        s = (read_last_response_id(p) or "").strip()
        if s:
            return s
    return (read_last_all_response_id() or "").strip()


def _is_plan_chinh_or_phu_zone(zone: Zone) -> bool:
    return is_plan_chinh_family(zone.label, zone.id) or is_plan_phu_family(zone.label, zone.id)


def _parse_iso_utc_optional(raw: str) -> Optional[datetime]:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _post_fill_manage_retry_due(zone: Zone, *, now: Optional[datetime] = None) -> bool:
    due_at = _parse_iso_utc_optional(getattr(zone, "openai_manage_retry_at", "") or "")
    if due_at is None:
        return False
    current = now or _now_utc()
    return current >= due_at


def _should_write_intraday_alert_anchor(params: WatchlistDaemonParams) -> bool:
    """
    ``True`` khi cần ghi ``response_id`` mới từ [INTRADAY_ALERT] (zone-touch) vào sidecar shard.

    Lần chạm đầu (file sidecar trống): sau OpenAI, lưu id để các lần sau dùng làm
    ``previous_response_id``. Các lần chạm sau (retry ``cham``): sidecar đã có id — không ghi đè
    bằng id mới; vẫn chain từ id đã lưu.

    Không ``--shard``: luôn ``False`` (không ghi sidecar; giữ hành vi cũ).
    """
    if params.shard_path is None:
        return False
    p = _daemon_plan_response_id_path(params.shard_path)
    return not (read_last_response_id(p) or "").strip()


def _report_telegram_send_failure(context: str, text: str, exc: BaseException) -> None:
    preview = (text or "").strip().replace("\r", " ").replace("\n", " ")
    if len(preview) > 280:
        preview = preview[:277] + "..."
    _poll_terminal.warning(
        "[telegram-log] send failed | context=%s | err=%s | text=%s",
        context,
        exc,
        preview or "(empty)",
    )


def _send_log(settings: Settings, text: str) -> None:
    """
    Best-effort: send plain text to TELEGRAM_LOG_CHAT_ID.
    """
    cid = (settings.telegram_log_chat_id or "").strip()
    if not cid:
        return
    body = (text or "").strip()
    if not body:
        return
    try:
        send_message(
            bot_token=settings.telegram_bot_token,
            chat_id=cid,
            text=body,
            parse_mode=None,
        )
    except Exception as exc:
        # Never let logging break the daemon, but keep a local trace for debugging.
        _report_telegram_send_failure("tv_watchlist_daemon._send_log", body, exc)
        return


def _user_notice_plan_slot_tag(
    *,
    zone: Optional[Zone] = None,
    params: Optional[WatchlistDaemonParams] = None,
    zone_label: Optional[str] = None,
) -> str:
    """
    Tiền tố hiển thị plan + khung giờ, ví dụ ``(Plan chính - Sáng)`` / ``(Plan chính - Tối luồng 2)``.
    Slot: ``zone.session_slot`` hoặc parse từ ``params.shard_path`` (``vung_*_{sang|chieu|toi}.json``).
    """
    slot_raw = resolve_session_slot_raw(
        zone_session_slot=getattr(zone, "session_slot", None) if zone is not None else None,
        shard_path=params.shard_path if params is not None else None,
    )

    lab_disp: Optional[str] = None
    if zone is not None and (zone.label or "").strip():
        lab_disp = mt5_zone_label_display_vn(zone.label) or (zone.label or "").strip()
    elif zone_label is not None and str(zone_label).strip():
        zl = str(zone_label).strip()
        lab_disp = mt5_zone_label_display_vn(zl) or zl
    elif params is not None and params.shard_path is not None:
        raw = label_from_shard_stem(params.shard_path.stem)
        if raw:
            lab_disp = mt5_zone_label_display_vn(raw) or raw

    second_flow = resolve_second_flow(
        zone_id=getattr(zone, "id", None) if zone is not None else None,
        source=getattr(zone, "source", None) if zone is not None else None,
        shard_path=params.shard_path if params is not None else None,
    )
    slot_vn = (
        session_slot_display_vn(slot_raw, second_flow=second_flow) if slot_raw else None
    )
    if lab_disp and slot_vn:
        return f"({lab_disp} - {slot_vn})"
    if lab_disp:
        return f"({lab_disp})"
    return ""


def _send_user_notice(
    settings: Settings,
    title: str,
    body: str = "",
    *,
    zone: Optional[Zone] = None,
    params: Optional[WatchlistDaemonParams] = None,
    zone_label: Optional[str] = None,
) -> None:
    """Tin ngắn tới TELEGRAM_PYTHON_BOT_CHAT_ID (non-tech)."""
    tag = _user_notice_plan_slot_tag(zone=zone, params=params, zone_label=zone_label)
    out_title = f"{tag} {title}".strip() if tag else (title or "").strip()
    send_user_friendly_notice(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_python_bot_chat_id,
        title=out_title,
        body=body,
    )


def _entry_slot_for_zone(zone: Zone, params: WatchlistDaemonParams) -> Optional[str]:
    return resolve_session_slot_raw(
        zone_session_slot=getattr(zone, "session_slot", None),
        shard_path=params.shard_path,
    )


def _mt5_telegram_zone_context(zone: Zone, params: WatchlistDaemonParams) -> dict[str, Any]:
    """``zone_id`` / ``source`` / ``shard_path`` để nhãn Telegram phân biệt luồng 2."""
    return {
        "zone_id": zone.id,
        "zone_source": zone.source,
        "shard_path": params.shard_path,
    }


def _filter_entry_accounts_for_zone(
    accounts: list[MT5AccountEntry],
    zone: Zone,
    params: WatchlistDaemonParams,
) -> tuple[list[MT5AccountEntry], Optional[str], list[str]]:
    slot = _entry_slot_for_zone(zone, params)
    allowed = filter_mt5_accounts_for_zone_entry(
        accounts,
        slot,
        zone.label,
        zone_id=zone.id,
        zone_source=zone.source,
    )
    allowed_ids = {a.id for a in allowed}
    blocked_ids = [a.id for a in accounts if a.id not in allowed_ids]
    return allowed, slot, blocked_ids


def _resolve_zone_entry_accounts(
    zone: Zone,
    params: WatchlistDaemonParams,
) -> tuple[Optional[list[MT5AccountEntry]], Optional[str], list[str], Optional[str]]:
    """
    Load và lọc account vào lệnh cho zone.

    Returns:
        ``(None, None, [], None)`` — single-terminal (không dùng multi-account file).
        ``([], slot, blocked, missing_subset)`` — thiếu subset (``missing_subset`` set) hoặc bị lọc hết.
        ``(exec_accs, slot, blocked, None)`` — có ít nhất một account.
    """
    accs = load_mt5_accounts_for_zone_entry(
        zone_source=(zone.source or ""),
        cli_path=params.mt5_accounts_json,
    )
    if accs is None:
        return None, None, [], None
    slot = _entry_slot_for_zone(zone, params)
    if not accs:
        subset_fn = subset_accounts_json_basename(zone.source or "") or "accounts.json"
        return [], slot, [], subset_fn
    exec_accs, slot, blocked = _filter_entry_accounts_for_zone(accs, zone, params)
    if blocked:
        _log.debug(
            "entry accounts filtered (entry_slots / only_plan_chinh) | zone_id=%s label=%r slot=%s blocked=%s",
            zone.id,
            zone.label,
            slot or "unknown",
            blocked,
        )
    return exec_accs, slot, blocked, None


def _mark_zone_loai_no_entry_accounts(
    zone: Zone,
    *,
    zone_id: str,
    settings: Settings,
    params: WatchlistDaemonParams,
    slot: Optional[str],
    log_prefix: str,
    missing_subset: Optional[str] = None,
) -> bool:
    """Loại zone khi không còn account nào được phép vào lệnh. Returns True nếu đã loại."""
    if zone.status == "loai":
        return True
    zone.status = "loai"
    zone.loai_streak = 0
    zone.retry_at = ""
    zone.auto_entry_retry_after = ""
    zone.auto_entry_mt5_failed = False
    zone.mt5_ticket = None
    zone.mt5_tickets_by_account = None
    if missing_subset:
        _send_log(
            settings,
            f"[{log_prefix}] missing subset {missing_subset!r} -> loai | zone_id={zone_id} label={zone.label!r}",
        )
        _send_user_notice(
            settings,
            "Loại vùng: thiếu file accounts.",
            f"Vùng {zone.source!r} cần {missing_subset} cạnh accounts.json "
            f'(flag trên account hoặc chạy all / all-2 để tạo subset).',
            zone=zone,
            params=params,
        )
    else:
        _send_log(
            settings,
            f"[{log_prefix}] no entry accounts -> loai | zone_id={zone_id} label={zone.label!r} "
            f"slot={slot or 'unknown'}",
        )
        _send_user_notice(
            settings,
            "Loại vùng: không có account vào lệnh.",
            f"Zone {zone.label!r} (khung {slot or 'unknown'}) — kiểm tra entry_slots / only_plan_chinh.",
            zone=zone,
            params=params,
        )
    return True


def _maybe_loai_zone_if_no_entry_accounts(
    zone: Zone,
    *,
    zone_id: str,
    settings: Settings,
    params: WatchlistDaemonParams,
    exec_accs: Optional[list[MT5AccountEntry]],
    slot: Optional[str],
    log_prefix: str,
    missing_subset: Optional[str] = None,
) -> bool:
    """``True`` khi zone đã / vừa chuyển ``loai`` vì không có account vào lệnh."""
    if exec_accs is None:
        return False
    if exec_accs:
        return zone.status == "loai"
    return _mark_zone_loai_no_entry_accounts(
        zone,
        zone_id=zone_id,
        settings=settings,
        params=params,
        slot=slot,
        log_prefix=log_prefix,
        missing_subset=missing_subset,
    )


def _mt5_entry_order_comment(
    zone_id: str,
    *,
    zone: Optional[Zone] = None,
    params: Optional[WatchlistDaemonParams] = None,
) -> str:
    """MT5 order comment: ``update-scalp-<slot>-<hop_luu>`` cho zone scalp; còn lại từ ``zone_id``."""
    if zone is not None and (zone.source or "").strip().lower() == SOURCE_UPDATE_SCALP:
        if params is not None:
            slot = _entry_slot_for_zone(zone, params)
        else:
            slot = resolve_session_slot_raw(
                zone_session_slot=getattr(zone, "session_slot", None),
                shard_path=None,
            )
        slot_s = (slot or "unknown").strip()
        hop = zone.hop_luu
        hop_s = str(int(hop)) if hop is not None else "0"
        return f"update-scalp-{slot_s}-{hop_s}"
    comment = str(zone_id or "").strip()
    if comment.startswith("plan_"):
        return comment[len("plan_") :]
    return comment


def _multi_summary_tracking_ticket(summary, accounts: list[MT5AccountEntry]) -> int:
    """Prefer the configured primary ticket; if that account is filtered out, track the first ticket."""
    try:
        return int(summary.primary_ticket(accounts) or 0)
    except Exception:
        pass
    for tid in (summary.tickets_by_account_id or {}).values():
        try:
            n = int(tid)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            return n
    return 0


def _skip_scalp_r1_followup_if_needed(
    zone: Zone,
    *,
    settings: Settings,
    params: WatchlistDaemonParams,
) -> bool:
    """Compatibility hook: scalp now uses the same 1R follow-up flow as other plans."""
    _ = (zone, settings, params)
    return False


def _zone_label_slot_display_vn(zone: Zone, params: Optional[WatchlistDaemonParams] = None) -> str:
    """Ví dụ: ``Plan chính - Sáng`` hoặc ``Plan chính - Tối luồng 2`` (không ngoặc)."""
    lab = mt5_zone_label_display_vn(zone.label) or (zone.label or "").strip()
    second_flow = resolve_second_flow(
        zone_id=zone.id,
        source=zone.source,
        shard_path=params.shard_path if params is not None else None,
    )
    slot = session_slot_display_vn(
        getattr(zone, "session_slot", None),
        second_flow=second_flow,
    )
    if lab and slot:
        return f"{lab} - {slot}"
    return lab or "(unknown)"


def _send_entry_management_notice(settings: Settings, zone: Zone, text: str) -> None:
    """Notice ngắn cho luồng quản lý lệnh sau khi đã has_position."""
    title = (text or "").strip()
    if not title:
        return
    send_user_friendly_notice(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_python_bot_chat_id,
        title=title,
        body="",
    )


def _mark_initial_zone_touch_dispatch(
    st: ZonesState,
    *,
    touched_zone: Zone,
    last_price: float,
    settings: Settings,
    params: WatchlistDaemonParams,
) -> list[tuple[Zone, str, float]]:
    """Lần đầu chạm vùng: chuyển sang worker ngay; không hẹn chờ 10 phút."""
    touched_zone.status = "dang_thuc_thi"
    touched_zone.auto_entry_mt5_failed = False
    touched_zone.retry_at = ""

    invalidated = _invalidate_same_side_zones_after_touch(st, touched_zone=touched_zone)
    if invalidated:
        touched_ref = _zone_compare_ref(touched_zone)
        side = (touched_zone.side or "").strip().upper()
        huong = "thấp hơn" if side == "SELL" else "cao hơn"
        _send_log(
            settings,
            f"[zone-touch] invalidate_same_side | touched_id={touched_zone.id} "
            f"label={(touched_zone.label or '').strip()} side={side} "
            f"vung_cho={(touched_zone.vung_cho or '').strip()} "
            f"ref={touched_ref} | loai={','.join(zz.id for zz, _prev, _ref in invalidated)}",
        )

        names = ", ".join(
            _zone_label_slot_display_vn(zz, params) for zz, _prev, _ref in invalidated
        )
        _send_user_notice(
            settings,
            f"Đã loại những vùng {side} {huong}: {names}",
            "",
            zone=touched_zone,
            params=params,
        )

    _send_log(
        settings,
        f"[zone-touch] initial_touch_dispatch | zone_id={touched_zone.id} "
        f"last={last_price} -> status=dang_thuc_thi retry_at={touched_zone.retry_at}",
    )
    if _settings_skip_intraday_alert_openai(settings):
        _send_user_notice(
            settings,
            "Giá đã chạm vùng chờ.",
            "Chế độ tạm: không gọi OpenAI [INTRADAY_ALERT] — chỉ báo Telegram rồi đưa vùng về trạng thái chờ.",
            zone=touched_zone,
            params=params,
        )
    else:
        _send_user_notice(
            settings,
            "Giá đã chạm vùng chờ.",
            "Hệ thống sẽ lấy dữ liệu tại mốc M5 kế tiếp rồi gửi AI phân tích.",
            zone=touched_zone,
            params=params,
        )
    return invalidated


def _apply_zone_touch_loai_decision(
    zone: Zone,
    *,
    confirm_rounds: int = _ZONE_TOUCH_LOAI_CONFIRM_ROUNDS,
) -> bool:
    """Apply one AI ``loại`` confirmation; return True when the zone is terminal."""
    zone.loai_streak = int(getattr(zone, "loai_streak", 0) or 0) + 1
    if zone.loai_streak >= int(confirm_rounds):
        zone.status = "loai"
        zone.retry_at = ""
        return True

    zone.status = "cham"
    zone.retry_at = _zone_touch_retry_at_iso(zone)
    return False


def _zone_bounds(zone: Zone) -> Optional[tuple[float, float]]:
    lo, hi = parse_vung_cho_bounds(zone.vung_cho)
    if lo is None or hi is None:
        return None
    return float(lo), float(hi)


def _zone_compare_ref(zone: Zone) -> Optional[float]:
    """
    Rule compare:
    - SELL: dùng hi (upper bound) của vung_cho
    - BUY: dùng lo (lower bound) của vung_cho
    """
    b = _zone_bounds(zone)
    if b is None:
        return None
    lo, hi = b
    side = (zone.side or "").strip().upper()
    if side == "SELL":
        return float(hi)
    if side == "BUY":
        return float(lo)
    return None


def _invalidate_same_side_zones_after_touch(
    st: ZonesState, *, touched_zone: Zone
) -> list[tuple[Zone, str, float]]:
    """
    Khi một zone non-scalp vừa chạm:
    - cùng side (BUY/SELL), trừ scalp
    - áp dụng cho status in {"vung_cho", "cham"}
    - SELL: loại zone có ref(hi) < ref(touched)
    - BUY:  loại zone có ref(lo) > ref(touched)

    Trả về list (zone_bi_loai, prev_status, ref_value).
    """
    if _is_scalp_zone(touched_zone):
        return []
    touched_ref = _zone_compare_ref(touched_zone)
    if touched_ref is None:
        return []
    side = (touched_zone.side or "").strip().upper()
    if side not in ("BUY", "SELL"):
        return []

    invalidated: list[tuple[Zone, str, float]] = []
    for z in st.zones:
        if z.id == touched_zone.id:
            continue
        if (z.side or "").strip().upper() != side:
            continue
        if _is_scalp_zone(z):
            continue
        if z.status not in ("vung_cho", "cham"):
            continue

        zref = _zone_compare_ref(z)
        if zref is None:
            continue

        should_loai = (zref < touched_ref) if side == "SELL" else (zref > touched_ref)
        if not should_loai:
            continue

        prev_status = str(z.status)
        z.status = "loai"
        z.retry_at = ""
        invalidated.append((z, prev_status, float(zref)))

    return invalidated


def _touch_prompt(
    *,
    zone: Zone,
    last_price: float,
    after_retry_wait: bool = False,
) -> str:
    """
    User turn for zone-touch OpenAI follow-up: ``[INTRADAY_ALERT]`` / **Schema E** (system prompt).

    Bắt buộc JSON: ``phan_tich_alert`` + ``intraday_hanh_dong``. Khi ``VÀO LỆNH``, nên trả thêm
    ``trade_line`` (pipe) để cập nhật lệnh theo chạm vùng; nếu không, tool dùng ``trade_line`` lưu trên zone cho MT5.

    ``after_retry_wait``: lần gửi sau khi đã chạm vùng trước đó (dispatch từ ``cham`` sau ``retry_at``) —
    thêm dòng ngữ cảnh cho model.
    """
    cm_tf = "M1" if _is_scalp_zone(zone) else "M5"
    iv_key = "1m" if _is_scalp_zone(zone) else "5m"
    side_s = ""
    tl = (zone.trade_line or "").strip()
    tl_snip = ""
    if tl:
        tl_snip = (tl[:200] + "…") if len(tl) > 200 else tl
        parsed, err = _parse_trade_from_zone_trade_line(tl, symbol_override=None)
        if not err and parsed is not None:
            side = (getattr(parsed, "side", "") or "").strip().upper()
            if side in ("BUY", "SELL"):
                side_s = f"{side} "
    lead = (
        "Đánh giá sau khi đã chạm vùng trước đó.\n"
        if after_retry_wait
        else ""
    )
    return (
        "[INTRADAY_ALERT]\n"
        f"{lead}"
        f"Vùng chờ {side_s}{zone.vung_cho}.\n"
        f"Giá trigger realtime khi chạm vùng: {last_price:g}.\n"
        f"Một file JSON **coinmap_merged** từ Coinmap {cm_tf} đính kèm (``frames['{iv_key}']``, ``session_profile`` chung).\n"
    )


def _mark_zone_status(
    *,
    st: ZonesState,
    zone_id: str,
    new_status: str,
) -> ZonesState:
    for z in st.zones:
        if z.id == zone_id:
            z.status = new_status  # type: ignore[assignment]
            break
    return st


def _parse_trade_from_zone_trade_line(trade_line: str, *, symbol_override: Optional[str]) -> tuple[object, Optional[str]]:
    """
    Reuse existing `parse_openai_output_md` by wrapping trade_line into minimal JSON.
    Returns (parsed, err). Parsed is the ParsedTrade-like object used by execute_trade.
    """
    tl = (trade_line or "").strip()
    if not tl:
        return None, "trade_line is empty"
    minimal = json.dumps({"intraday_hanh_dong": "VÀO LỆNH", "trade_line": tl}, ensure_ascii=False)
    return parse_openai_output_md(minimal, symbol_override=symbol_override)


def _maybe_loai_zone_if_last_hit_sl(
    zone: Zone,
    p_last: float,
    *,
    settings: Settings,
    params: WatchlistDaemonParams,
) -> bool:
    """
    Nếu Last đã chạm/vượt mức SL trên ``trade_line``, loại vùng ngay.

    Caller chỉ gọi khi ``zone.status`` thuộc
    :data:`_DAEMON_PLAN_SL_LOAI_STATUSES` (chờ/chạm hoặc ``vao_lenh`` / ``cho_tp1``).
    """
    tl = (zone.trade_line or "").strip()
    if not tl:
        return False
    parsed, err = _parse_trade_from_zone_trade_line(tl, symbol_override=params.mt5_symbol)
    if err or parsed is None:
        return False
    managed_sl = getattr(zone, "managed_sl", None)
    if managed_sl is not None:
        sl_level = float(managed_sl)
        side = str(getattr(parsed, "side", "") or "").strip().upper()
        if side == "BUY":
            hit_sl = float(p_last) <= (sl_level + _TP1_EPS)
        else:
            hit_sl = float(p_last) >= (sl_level - _TP1_EPS)
    else:
        sl_level = float(parsed.sl)
        hit_sl = is_last_price_hit_stop_loss(float(p_last), parsed, eps=_TP1_EPS)
    if not hit_sl:
        return False
    prev_status = zone.status
    zone.status = "loai"
    zone.loai_streak = 0
    _send_log(
        settings,
        f"[sl-hit] last={p_last} touched SL -> loai | zone_id={zone.id} label={zone.label} "
        f"sl={sl_level} side={parsed.side}",
    )
    if prev_status in ("vao_lenh", "cho_tp1"):
        detail = (
            f"Last {p_last} — plan đang theo dõi (đã vào lệnh / chờ TP1) chạm SL trên trade_line, "
            "đánh dấu loại."
        )
    else:
        detail = f"Last {p_last} — vùng chờ không còn hiệu lực."
    _send_user_notice(
        settings,
        f"Loại vùng: giá chạm SL ({sl_level}).",
        detail,
        zone=zone,
        params=params,
    )
    return True


def _favorable_distance_from_entry(parsed, p_last: float) -> float:
    ref = _entry_reference_price(parsed)
    side = str(getattr(parsed, "side", "") or "").strip().upper()
    if side == "BUY":
        return float(p_last) - ref
    return ref - float(p_last)


def _risk_distance_with_zone_override(zone: Zone, parsed) -> float:
    _ = zone
    ref = _entry_reference_price(parsed)
    # R multiple luôn neo theo SL gốc trong trade_line (parsed.sl),
    # không đổi theo managed_sl để tránh "nhảy R" sau khi dời SL.
    sl = float(parsed.sl)
    return abs(ref - sl)


def _max_r_multiple_reached(zone: Zone, parsed, p_last: float, *, eps: float = _TP1_EPS) -> int:
    """Largest integer R-level reached from entry using original trade SL."""
    risk = _risk_distance_with_zone_override(zone, parsed)
    if risk <= 0:
        return 0
    favorable = _favorable_distance_from_entry(parsed, float(p_last))
    if favorable < -eps:
        return 0
    return max(0, int(math.floor((favorable + eps) / risk)))


def _r_level_text(r_level: float) -> str:
    return f"{float(r_level):g}"


def _managed_tp_touched(zone: Zone, parsed, p_last: float) -> bool:
    tp = getattr(zone, "managed_tp", None)
    if tp is None:
        return False
    side = str(getattr(parsed, "side", "") or "").strip().upper()
    tpf = float(tp)
    if side == "BUY":
        return float(p_last) >= tpf - _TP1_EPS
    return float(p_last) <= tpf + _TP1_EPS


def _should_check_managed_tp_done(zone: Zone, parsed, p_last: float) -> bool:
    """Managed TP -> done check chỉ chạy khi đã xác nhận ``has_position=true``."""
    return bool(getattr(zone, "has_position", False)) and _managed_tp_touched(zone, parsed, p_last)


def _daemon_plan_positive_prices(prices: list[float]) -> list[float]:
    """Bỏ Last <= 0 (tick MT5 lỗi, vd. 0.0) trước khi daemon-plan quét buffer."""
    return [float(p) for p in prices if float(p) > 0.0]


def _daemon_plan_watch_telegram_text(
    z: Zone,
    *,
    sym: str,
    p_last: Optional[float],
) -> str:
    """Một dòng cho kênh log kỹ thuật: Last từ daemon giá; kèm vùng chờ và trade_line."""
    last_s = f"{p_last}" if p_last is not None else "(none)"
    vc = (z.vung_cho or "").strip() or "(none)"
    tl = (z.trade_line or "").strip() or "(none)"
    extra: list[str] = [f"vung_cho={vc}", f"trade_line={tl}"]
    if z.mt5_ticket is not None and int(z.mt5_ticket) > 0:
        extra.append(f"ticket={z.mt5_ticket}")
    if z.hop_luu is not None:
        extra.append(f"hop_luu={z.hop_luu}")
    tail = " | ".join(extra)
    base = (
        f"[daemon-plan] watch | sym={sym} | zone_id={z.id} | "
        f"status={z.status} | exec_price={last_s}"
    )
    return f"{base} | {tail}"


def _entry_reference_price(parsed) -> float:
    if getattr(parsed, "kind", "") == "MARKET" or getattr(parsed, "price", None) is None:
        return (float(parsed.sl) + float(parsed.tp1)) / 2.0
    return float(parsed.price)


def _arm_threshold_met_for_zone(
    zone: Zone,
    p_last: float,
    *,
    symbol_override: Optional[str] = None,
) -> bool:
    """
    Arm sau vào lệnh: ``ref`` = :func:`_entry_reference_price` từ parse ``zone.trade_line``
    (đồng bộ ``tp1_followup`` / ``last_alert``). BUY: ``0 ≤ last−ref ≤ thr``; SELL: ``−thr ≤ last−ref ≤ 0``.
    ``thr`` theo ``zone.label`` (scalp hẹp hơn plan_chinh / plan_phu).
    """
    tl = (zone.trade_line or "").strip()
    if not tl:
        return False
    parsed, err = _parse_trade_from_zone_trade_line(tl, symbol_override=symbol_override)
    if err or parsed is None:
        return False
    thr = arm_threshold_tp1_for_label(zone.label)
    ref = _entry_reference_price(parsed)
    diff = float(p_last) - ref
    if getattr(parsed, "side", "") == "BUY":
        return 0.0 <= diff <= thr
    return -thr <= diff <= 0.0


def _tp1_touched(parsed, p_last: float) -> bool:
    tp = float(parsed.tp1)
    if getattr(parsed, "side", "") == "BUY":
        return p_last >= tp - _TP1_EPS
    return p_last <= tp + _TP1_EPS


def _entry_touched_for_position_check(parsed, p_last: float) -> bool:
    entry = getattr(parsed, "price", None)
    if entry is None:
        return False
    e = float(entry)
    if getattr(parsed, "side", "") == "BUY":
        return p_last <= e + _TP1_EPS
    return p_last >= e - _TP1_EPS


def _fmt_level_for_prompt(v: Optional[float]) -> str:
    if v is None:
        return "(không có)"
    return f"{v:g}"


def _cancel_tp1_pending_orders_for_zone(
    *,
    zone: Zone,
    params: WatchlistDaemonParams,
    settings: Settings,
) -> tuple[bool, str]:
    """Huỷ pending ticket(s) của zone trước khi loại do chạm TP1 mà chưa khớp position."""
    if not params.mt5_execute:
        return True, "mt5_execute=false — bỏ qua huỷ pending trên MT5"

    dry = bool(params.mt5_dry_run)
    accounts = load_mt5_accounts_for_cli(params.mt5_accounts_json)
    tmap = zone.mt5_tickets_by_account or {}
    messages: list[str] = []
    ok_all = True

    if accounts and tmap:
        by_id = {acc.id: acc for acc in accounts}
        for acc_id, ticket in tmap.items():
            acc = by_id.get(acc_id)
            if acc is None:
                ok_all = False
                messages.append(f"acc={acc_id}: không có trong accounts.json")
                continue
            r = mt5_cancel_pending_order(
                int(ticket),
                dry_run=dry,
                terminal_path=acc.terminal_path,
                login=acc.login,
                password=acc.password,
                server=acc.server,
            )
            ok_all = ok_all and bool(r.ok)
            messages.append(f"acc={acc_id} ticket={ticket}: {r.message}")
        msg = " | ".join(messages)
        _send_log(settings, f"[tp1] huỷ pending trước khi loại multi | ok={ok_all} | {msg}".strip())
        return ok_all, msg

    ticket = int(zone.mt5_ticket or 0)
    if ticket <= 0:
        return True, "zone không có mt5_ticket"

    primary = primary_account(accounts) if accounts else None
    if primary is not None:
        r = mt5_cancel_pending_order(
            ticket,
            dry_run=dry,
            terminal_path=primary.terminal_path,
            login=primary.login,
            password=primary.password,
            server=primary.server,
        )
    else:
        r = mt5_cancel_pending_order(
            ticket,
            dry_run=dry,
            terminal_session_only=True,
        )
    _send_log(settings, f"[tp1] huỷ pending trước khi loại | ok={r.ok} | {r.message}".strip())
    return bool(r.ok), r.message


def _post_fill_manage_job(
    *,
    settings: Settings,
    params: WatchlistDaemonParams,
    zone_id: str,
) -> None:
    """
    Post-fill [TRADE_MANAGEMENT] cho plan chính / plan phụ: GoCharting M5 detail + OpenAI;
    gửi ``reason`` lên Telegram — không thao tác MT5.
    """
    from automation_tool.gocharting_capture import capture_gocharting, gocharting_detail_png_path

    try:
        st0 = _state_read(params)
        if st0 is None:
            return
        z0 = next((z for z in st0.zones if z.id == zone_id), None)
        if z0 is None:
            return
        if z0.status in ("done", "loai"):
            return

        prev = _post_fill_prev_response_id(params)
        if not prev:
            _send_log(
                settings,
                f"[post-fill] thiếu FULL_ANALYSIS anchor | zone_id={zone_id}",
            )
            _send_user_notice(
                settings,
                "Chưa có phân tích FULL_ANALYSIS.",
                "Chạy coinmap-automation all trước khi quản lý lệnh sau khớp.",
                zone=z0,
                params=params,
            )
            return

        if not z0.trade_line:
            return
        parsed, err = _parse_trade_from_zone_trade_line(
            z0.trade_line, symbol_override=params.mt5_symbol
        )
        if err or parsed is None:
            _send_log(settings, f"[post-fill] parse trade_line failed | zone_id={zone_id} | {err}")
            return

        _send_user_notice(
            settings,
            "Lệnh đã khớp — đang lấy chart GoCharting M5 và hỏi AI.",
            f"Sẽ gửi khuyến nghị quản lý lệnh (không tự động sửa MT5) cho {_zone_label_slot_display_vn(z0, params)}.",
            zone=z0,
            params=params,
        )

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        gc_label = GOCHARTING_GOLD_EXPORT_LABEL
        capture_gocharting(
            gocharting_yaml=default_gocharting_config_path(),
            charts_dir=params.charts_dir,
            email=settings.gocharting_email or "",
            password=settings.gocharting_password or "",
            storage_state_path=params.storage_state_path,
            save_storage_state=not params.no_save_storage,
            headless=params.headless,
            main_chart_symbol=read_main_chart_symbol(params.charts_dir),
            stamp_override=stamp,
            capture_symbols=(DEFAULT_MAIN_CHART_SYMBOL,),
            capture_intervals=("5m",),
            only_slots=[(gc_label, "5m")],
            overview_capture=False,
            detail_history_steps=0,
        )
        detail_png = gocharting_detail_png_path(
            params.charts_dir, stamp, gc_label, "5m", "zoom"
        )
        if not detail_png.is_file():
            raise FileNotFoundError(
                f"post-fill: GoCharting M5 detail PNG not found: {detail_png}"
            )

        tk_check = int(z0.mt5_ticket or 0)
        dry = bool(params.mt5_dry_run)
        current_sl: Optional[float] = None
        current_tp: Optional[float] = None
        if tk_check > 0:
            accs_for_prompt = load_mt5_accounts_for_cli(params.mt5_accounts_json)
            prim_for_prompt = primary_account(accs_for_prompt) if accs_for_prompt else None
            if prim_for_prompt is not None:
                current_sl, current_tp, sltp_msg = mt5_ticket_current_sltp(
                    tk_check,
                    dry_run=dry,
                    terminal_path=prim_for_prompt.terminal_path,
                    login=prim_for_prompt.login,
                    password=prim_for_prompt.password,
                    server=prim_for_prompt.server,
                )
                _send_log(settings, f"[post-fill] đọc SL/TP hiện tại | {sltp_msg}")

        user_text = POST_FILL_MANAGEMENT_USER_TEMPLATE.format(
            minutes_after_fill=POST_FILL_MANAGE_DELAY_MINUTES,
            plan_label=z0.label,
            entry_side=str(getattr(parsed, "side", "") or "").upper(),
            entry_price=_fmt_level_for_prompt(getattr(parsed, "price", None)),
            current_sl=_fmt_level_for_prompt(current_sl),
            current_tp=_fmt_level_for_prompt(current_tp),
        )
        out_text, new_id = run_single_followup_responses(
            api_key=settings.openai_api_key,
            user_text=user_text,
            coinmap_json_paths=[],
            extra_chart_payloads=[("image", detail_png)],
            previous_response_id=prev,
            vector_store_ids=settings.openai_vector_store_ids,
            store=settings.openai_responses_store,
            include=settings.openai_responses_include,
            model="gpt-5.4-mini",
            reasoning_summary="auto",
            reasoning_effort="medium",
        )
        _openai_followup_persist_new_id(params, new_id)
        _send_log(settings, "[post-fill] OpenAI TRADE_MANAGEMENT xong (ẩn raw JSON).")

        reason = extract_trade_management_reason(out_text)
        st1 = _state_read(params)
        z1 = z0
        if st1 is not None:
            z1 = next((z for z in st1.zones if z.id == zone_id), z0)

        if not reason:
            _send_user_notice(
                settings,
                "Sau khớp lệnh: không đọc được khuyến nghị từ AI.",
                "Xem log kỹ thuật nếu cần chi tiết.",
                zone=z1,
                params=params,
            )
            return

        slot_raw = resolve_session_slot_raw(
            zone_session_slot=getattr(z1, "session_slot", None),
            shard_path=params.shard_path,
        )
        if not params.no_telegram:
            send_trade_management_reason_notice(
                bot_token=settings.telegram_bot_token,
                telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
                zone_label=z1.label,
                session_slot=slot_raw,
                action="khuyen_nghi",
                reason=reason,
                trade_line=None,
                zone_id=z1.id,
                zone_source=z1.source,
                shard_path=params.shard_path,
            )
    except Exception as e:
        _send_log(settings, f"[post-fill] ERROR | zone_id={zone_id} | {e!s}")
        _send_user_notice(
            settings,
            "Lỗi khi quản lý lệnh sau khớp.",
            str(e),
            params=params,
        )
        re_raise_unless_openai(e, exit_on_openai=False, settings=settings)


def _tp1_followup_job(
    *,
    settings: Settings,
    params: WatchlistDaemonParams,
    zone_id: str,
    p_last: float,
) -> None:
    """
    Follow-up after TP1 touch:
    - capture Coinmap M5
    - call OpenAI TP1 template
    - parse decision: loai | chinh_trade_line
    - act on MT5 and update zones_state
    """
    try:
        if _settings_skip_trade_management(settings):
            st_skip = _state_read(params)
            if st_skip is not None:
                z_skip = next((z for z in st_skip.zones if z.id == zone_id), None)
                if z_skip is not None and z_skip.status not in ("done", "loai"):
                    z_skip.status = "cho_tp1"
                    z_skip.tp1_followup_done = True
                    _state_write(params, st_skip)
            _send_log(
                settings,
                f"[tp1] skip TRADE_MANAGEMENT (SKIP_TRADE_MANAGEMENT) | zone_id={zone_id}",
            )
            return
        st0 = _state_read(params)
        if st0 is None:
            return
        z0 = next((z for z in st0.zones if z.id == zone_id), None)
        if z0 is None:
            return
        if z0.status in ("done", "loai"):
            return
        if not z0.trade_line or not z0.mt5_ticket:
            z0.status = "cho_tp1"
            z0.tp1_followup_done = False
            z0.r1_followup_done = False
            _state_write(params, st0)
            return

        parsed, err = _parse_trade_from_zone_trade_line(z0.trade_line, symbol_override=params.mt5_symbol)
        if err or parsed is None:
            z0.tp1_followup_done = False
            z0.r1_followup_done = False
            _state_write(params, st0)
            return

        tk_check = int(z0.mt5_ticket or 0)
        dry = bool(params.mt5_dry_run)
        exe = bool(params.mt5_execute)
        if exe and tk_check > 0:
            accs_chk = load_mt5_accounts_for_cli(params.mt5_accounts_json)
            prim_chk = primary_account(accs_chk) if accs_chk else None
            still_open, ticket_msg = mt5_ticket_still_open(
                tk_check,
                dry_run=dry,
                terminal_path=prim_chk.terminal_path if prim_chk else None,
                login=prim_chk.login if prim_chk else None,
                password=prim_chk.password if prim_chk else None,
                server=prim_chk.server if prim_chk else None,
            )
            _send_log(settings, f"[tp1] kiểm tra ticket | {ticket_msg}")
            if not still_open:
                st_done = _state_read(params)
                if st_done is not None:
                    z_done = next((z for z in st_done.zones if z.id == zone_id), None)
                    if z_done is not None:
                        z_done.status = "done"
                        z_done.mt5_ticket = None
                        z_done.mt5_tickets_by_account = None
                        z_done.tp1_followup_done = True
                        z_done.r1_followup_done = True
                        _state_write(params, st_done)
                _send_log(
                    settings,
                    f"[tp1] bỏ qua follow-up TP1 (ticket đã đóng trên MT5) | zone_id={zone_id} | {ticket_msg}",
                )
                return

        if not bool(getattr(z0, "has_position", False)):
            ok_cancel, cancel_msg = _cancel_tp1_pending_orders_for_zone(
                zone=z0,
                params=params,
                settings=settings,
            )
            if not ok_cancel:
                z0.status = "cho_tp1"
                z0.tp1_followup_done = False
                z0.r1_followup_done = False
                _state_write(params, st0)
                _send_user_notice(
                    settings,
                    "Chạm TP1 nhưng chưa có position và huỷ pending chưa thành công.",
                    "Chưa gọi AI; hệ thống sẽ thử lại ở tick sau để tránh bỏ sót pending order.",
                    zone=z0,
                    params=params,
                )
                return
            z0.status = "loai"
            z0.mt5_ticket = None
            z0.mt5_tickets_by_account = None
            z0.tp1_followup_done = True
            z0.r1_followup_done = True
            z0.has_position = False
            _state_write(params, st0)
            _send_user_notice(
                settings,
                "Chạm TP1 nhưng lệnh chưa khớp — loại vùng.",
                f"Đã huỷ pending ticket(s) của zone trước khi loại. {cancel_msg}",
                zone=z0,
                params=params,
            )
            return

        if getattr(parsed, "tp2", None) is not None and bool(getattr(z0, "has_position", False)):
            ok_partial = True
            partial_msg = ""
            if exe:
                accs_pc = load_mt5_accounts_for_cli(params.mt5_accounts_json)
                tmap_pc = z0.mt5_tickets_by_account or {}
                skipped_partial_no_position = False
                if accs_pc and tmap_pc:
                    summ_pc = mt5_partial_close_tp1_all_accounts(
                        tmap_pc,
                        accs_pc,
                        parsed,
                        dry_run=dry,
                        symbol_override=params.mt5_symbol,
                    )
                    partial_msg = format_mt5_multi_manage_for_telegram(summ_pc)
                    ok_partial = summ_pc.ok_all
                else:
                    prim_pc = primary_account(accs_pc) if accs_pc else None
                    if tk_check > 0 and prim_pc is not None:
                        is_pos, pos_msg = mt5_ticket_is_open_position(
                            tk_check,
                            dry_run=dry,
                            terminal_path=prim_pc.terminal_path,
                            login=prim_pc.login,
                            password=prim_pc.password,
                            server=prim_pc.server,
                        )
                        if not is_pos:
                            skipped_partial_no_position = True
                            partial_msg = pos_msg
                            ok_partial = True
                            _send_log(
                                settings,
                                f"[tp1] bỏ qua partial close 50% vì chưa có position | {pos_msg}",
                            )
                    if not skipped_partial_no_position:
                        r_pc = mt5_close_position_partial(
                            tk_check,
                            fraction=0.5,
                            # Không truyền expected_initial_volume — luôn lấy volume thực từ position trên MT5.
                            dry_run=dry,
                            terminal_path=prim_pc.terminal_path if prim_pc else None,
                            login=prim_pc.login if prim_pc else None,
                            password=prim_pc.password if prim_pc else None,
                            server=prim_pc.server if prim_pc else None,
                        )
                        partial_msg = r_pc.message
                        ok_partial = r_pc.ok
                _send_log(
                    settings,
                    f"[tp1] partial close 50% trước OpenAI | ok={ok_partial} | {partial_msg}".strip(),
                )
                if (
                    not params.no_telegram
                    and settings.telegram_bot_token
                    and not skipped_partial_no_position
                ):
                    send_mt5_execution_log_to_ngan_gon_chat(
                        bot_token=settings.telegram_bot_token,
                        telegram_chat_id=settings.telegram_chat_id,
                        telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
                        telegram_log_chat_id=settings.telegram_log_chat_id,
                        source="tp1-partial-close",
                        text=f"{z0.label}: chạm TP1 + có TP2 — chốt 50% trước khi hỏi AI\n{partial_msg}",
                        zone_label=z0.label,
                        trade_line=z0.trade_line,
                        execution_ok=ok_partial,
                        session_slot=resolve_session_slot_raw(
                            zone_session_slot=getattr(z0, "session_slot", None),
                            shard_path=params.shard_path,
                        ),
                        **_mt5_telegram_zone_context(z0, params),
                    )
            else:
                _send_log(settings, "[tp1] bỏ qua partial close 50% vì mt5_execute=false")

            if not ok_partial:
                z0.status = "cho_tp1"
                z0.tp1_followup_done = False
                z0.r1_followup_done = False
                _state_write(params, st0)
                _send_user_notice(
                    settings,
                    "Chạm TP1 nhưng chốt 50% chưa thành công.",
                    "Chưa gọi AI quản lý lệnh; hệ thống sẽ thử lại ở tick sau.",
                    zone=z0,
                    params=params,
                )
                return

        from automation_tool.coinmap import capture_charts
        from automation_tool.images import coinmap_xauusd_5m_json_path, read_main_chart_symbol
        from automation_tool.tp1_followup import parse_tp1_followup_decision

        _send_user_notice(
            settings,
            "Giá đã tới vùng theo dõi sau TP1.",
            "Đang lấy biểu đồ M5 và hỏi AI bước tiếp theo.",
            zone=z0,
            params=params,
        )

        _wait_for_m5_analysis_capture_slot(
            settings,
            source="tp1 TRADE_MANAGEMENT",
            params=params,
            zone=z0,
        )
        capture_charts(
            coinmap_yaml=params.capture_coinmap_yaml,
            charts_dir=params.charts_dir,
            storage_state_path=params.storage_state_path,
            email=settings.coinmap_email,
            password=settings.coinmap_password,
            tradingview_password=settings.tradingview_password,
            save_storage_state=not params.no_save_storage,
            headless=params.headless,
            reuse_browser_context=None,
            main_chart_symbol=read_main_chart_symbol(params.charts_dir),
            coinmap_capture_intervals=("5m",),
        )
        json_path = coinmap_xauusd_5m_json_path(params.charts_dir)
        if json_path is None or not json_path.is_file():
            raise SystemExit(f"tp1-followup: no main 5m Coinmap JSON under {params.charts_dir}")
        openai_merged = write_openai_coinmap_merged_from_raw_export(json_path)

        prev = _openai_followup_prev_response_id(params)
        current_sl: Optional[float] = None
        current_tp: Optional[float] = None
        if tk_check > 0:
            accs_for_prompt = load_mt5_accounts_for_cli(params.mt5_accounts_json)
            prim_for_prompt = primary_account(accs_for_prompt) if accs_for_prompt else None
            if prim_for_prompt is not None:
                current_sl, current_tp, sltp_msg = mt5_ticket_current_sltp(
                    tk_check,
                    dry_run=dry,
                    terminal_path=prim_for_prompt.terminal_path,
                    login=prim_for_prompt.login,
                    password=prim_for_prompt.password,
                    server=prim_for_prompt.server,
                )
                _send_log(settings, f"[tp1] đọc SL/TP hiện tại | {sltp_msg}")
        user_text = TP1_POST_TOUCH_USER_TEMPLATE.format(
            plan_label=z0.label,
            entry_side=str(getattr(parsed, "side", "") or "").upper(),
            entry_price=_fmt_level_for_prompt(getattr(parsed, "price", None)),
            current_sl=_fmt_level_for_prompt(current_sl),
            current_tp=_fmt_level_for_prompt(current_tp),
        )
        out_text, new_id = run_single_followup_responses(
            api_key=settings.openai_api_key,
            user_text=user_text,
            coinmap_json_paths=[openai_merged],
            previous_response_id=prev or "",
            vector_store_ids=settings.openai_vector_store_ids,
            store=settings.openai_responses_store,
            include=settings.openai_responses_include,
            # Force config for [TRADE_MANAGEMENT]
            model="gpt-5.4-mini",
            reasoning_summary="auto",
            reasoning_effort="medium",
        )
        _openai_followup_persist_new_id(params, new_id)
        _send_log(settings, "[tp1] OpenAI TRADE_MANAGEMENT xong (ẩn raw JSON).")

        dec = parse_tp1_followup_decision(out_text)
        st1 = _state_read(params)
        if st1 is None:
            return
        z1 = next((z for z in st1.zones if z.id == zone_id), None)
        if z1 is None:
            return

        # Mark handled so we don't spam.
        z1.tp1_followup_done = True

        if dec is None:
            # cannot parse -> allow retry later
            z1.tp1_followup_done = False
            z1.status = "cho_tp1"
            _state_write(params, st1)
            _send_user_notice(
                settings,
                "Sau TP1: không đọc được quyết định từ AI.",
                "Hệ thống sẽ thử lại — xem log kỹ thuật nếu cần chi tiết.",
                zone=z1,
                params=params,
            )
            return

        tk = int(z1.mt5_ticket or 0)
        dry = bool(params.mt5_dry_run)
        exe = bool(params.mt5_execute)
        slot_raw = resolve_session_slot_raw(
            zone_session_slot=getattr(z1, "session_slot", None),
            shard_path=params.shard_path,
        )
        if not params.no_telegram:
            send_trade_management_reason_notice(
                bot_token=settings.telegram_bot_token,
                telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
                zone_label=z1.label,
                session_slot=slot_raw,
                action=dec.sau_tp1,
                reason=dec.reason,
                trade_line=None,
                **_mt5_telegram_zone_context(z1, params),
            )

        if dec.sau_tp1 == "giu_nguyen":
            z1.status = "vao_lenh"
            z1.last_r_followup_level = 0
            _state_write(params, st1)
            _send_user_notice(
                settings,
                "Sau TP1: AI chọn «giữ nguyên» — không đổi lệnh.",
                "Tiếp tục theo dõi theo plan.",
                zone=z1,
                params=params,
            )
            return

        if dec.sau_tp1 == "loại":
            if exe and tk > 0:
                accs_lo = load_mt5_accounts_for_cli(params.mt5_accounts_json)
                tmap_lo = z1.mt5_tickets_by_account or {}
                if accs_lo and tmap_lo:
                    summ_lo = mt5_cancel_pending_or_close_all_accounts(
                        tmap_lo, accs_lo, dry_run=dry
                    )
                    _send_log(
                        settings,
                        f"[tp1] mt5_cancel_close multi: {format_mt5_multi_manage_for_telegram(summ_lo)}".strip(),
                    )
                else:
                    prim_lo = primary_account(accs_lo) if accs_lo else None
                    r = mt5_cancel_pending_or_close_position(
                        tk,
                        dry_run=dry,
                        terminal_path=prim_lo.terminal_path if prim_lo else None,
                        login=prim_lo.login if prim_lo else None,
                        password=prim_lo.password if prim_lo else None,
                        server=prim_lo.server if prim_lo else None,
                    )
                    _send_log(settings, f"[tp1] mt5_cancel_close: {r.message}".strip())
            z1.status = "loai"
            z1.mt5_tickets_by_account = None
            _state_write(params, st1)
            _send_user_notice(
                settings,
                "Sau TP1: AI chọn «loại» — đóng / bỏ theo dõi vùng.",
                "Đã gửi lệnh đóng trên MT5 nếu bật thực thi.",
                zone=z1,
                params=params,
            )
            return

        # chinh_trade_line
        if dec.new_sl is None and dec.new_tp is None:
            z1.tp1_followup_done = False
            z1.status = "cho_tp1"
            _state_write(params, st1)
            return

        next_sl = float(dec.new_sl) if dec.new_sl is not None else float(parsed.sl)
        if dec.new_tp is not None:
            new_parsed = replace(parsed, sl=next_sl, tp1=float(dec.new_tp), tp2=None)
        else:
            new_parsed = replace(parsed, sl=next_sl)

        used_inplace = False
        accs_mt5 = load_mt5_accounts_for_cli(params.mt5_accounts_json)
        if exe and tk > 0:
            tmap_old = z1.mt5_tickets_by_account or {}
            if accs_mt5 and tmap_old:
                ch_s = mt5_chinh_trade_line_all_accounts(
                    tmap_old,
                    accs_mt5,
                    new_parsed,
                    dry_run=dry,
                    symbol_override=params.mt5_symbol,
                )
                ch_txt = format_mt5_multi_chinh_for_telegram(ch_s)
                _send_log(settings, f"[tp1] mt5_chinh_inplace multi: {ch_txt}".strip())
                if ch_s.ok_all_inplace:
                    used_inplace = True
                    if not params.no_telegram:
                        send_mt5_execution_log_to_ngan_gon_chat(
                            bot_token=settings.telegram_bot_token,
                            telegram_chat_id=settings.telegram_chat_id,
                            telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
                            telegram_log_chat_id=settings.telegram_log_chat_id,
                            source="tp1-followup",
                            text=ch_txt,
                            zone_label=z1.label,
                            trade_line=z0.trade_line,
                            previous_trade_line=z0.trade_line,
                            execution_ok=True,
                            action="chinh_trade_line",
                            session_slot=resolve_session_slot_raw(
                                zone_session_slot=getattr(z1, "session_slot", None),
                                shard_path=params.shard_path,
                            ),
                            **_mt5_telegram_zone_context(z1, params),
                        )
                else:
                    _send_log(
                        settings,
                        f"[tp1] chinh_trade_line failed multi; giữ nguyên lệnh cũ, không đặt mới: {ch_txt}".strip(),
                    )
            else:
                prim = primary_account(accs_mt5) if accs_mt5 else None
                cr = mt5_chinh_trade_line_inplace(
                    tk,
                    new_parsed,
                    dry_run=dry,
                    symbol_override=params.mt5_symbol,
                    terminal_path=prim.terminal_path if prim else None,
                    login=prim.login if prim else None,
                    password=prim.password if prim else None,
                    server=prim.server if prim else None,
                )
                _send_log(settings, f"[tp1] mt5_chinh_inplace: {cr.message}".strip())
                if cr.ok and cr.outcome in ("modified_sltp", "modified_pending", "dry_run"):
                    used_inplace = True
                    if not params.no_telegram:
                        send_mt5_execution_log_to_ngan_gon_chat(
                            bot_token=settings.telegram_bot_token,
                            telegram_chat_id=settings.telegram_chat_id,
                            telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
                            telegram_log_chat_id=settings.telegram_log_chat_id,
                            source="tp1-followup",
                            text=cr.message,
                            zone_label=z1.label,
                            trade_line=z0.trade_line,
                            previous_trade_line=z0.trade_line,
                            execution_ok=True,
                            action="chinh_trade_line",
                            session_slot=resolve_session_slot_raw(
                                zone_session_slot=getattr(z1, "session_slot", None),
                                shard_path=params.shard_path,
                            ),
                            **_mt5_telegram_zone_context(z1, params),
                        )
                else:
                    _send_log(
                        settings,
                        f"[tp1] chinh_trade_line failed; giữ nguyên lệnh cũ, không đặt mới: {cr.message}".strip(),
                    )

        if exe and not used_inplace:
            z1.tp1_followup_done = False
            z1.r1_followup_done = False
            _state_write(params, st1)
            _send_user_notice(
                settings,
                "Sau TP1: chỉnh lệnh không thành công.",
                "Giữ nguyên lệnh/ticket cũ; hệ thống không đóng, không huỷ và không đặt lệnh mới.",
                zone=z1,
                params=params,
            )
            return
        elif exe and used_inplace:
            _send_user_notice(
                settings,
                "Sau TP1: đã cập nhật lệnh tại chỗ (SL/TP hoặc sửa lệnh chờ).",
                "Không đóng + mở mới; ticket giữ nguyên.",
                zone=z1,
                params=params,
            )
        # TRADE_MANAGEMENT: chỉ thực thi trade_line mới trên MT5, không ghi đè trade_line gốc trong state.
        z1.tp1_followup_done = False
        z1.r1_followup_done = False
        if dec.new_sl is not None:
            z1.managed_sl = float(dec.new_sl)
        if dec.new_tp is not None:
            z1.managed_tp = float(dec.new_tp)
        _state_write(params, st1)
        return
    except Exception as e:
        _send_log(settings, f"[tp1] ERROR | zone_id={zone_id} | {e!s}")
        _send_user_notice(settings, "Lỗi khi xử lý bước sau TP1.", str(e), params=params)
        re_raise_unless_openai(e, exit_on_openai=False, settings=settings)


def _r1_followup_job(
    *,
    settings: Settings,
    params: WatchlistDaemonParams,
    zone_id: str,
    prev_status: str,
    reached_r_level: float,
    trigger_tag: str = "r_multiple",
) -> None:
    """
    Follow-up theo mốc R (zones daemon): Coinmap M5 + [TRADE_MANAGEMENT] / Schema D.
    """
    from automation_tool.coinmap import capture_charts
    from automation_tool.images import coinmap_xauusd_5m_json_path, read_main_chart_symbol
    from automation_tool.tp1_followup import parse_tp1_followup_decision

    try:
        if _settings_skip_trade_management(settings):
            st_skip = _state_read(params)
            if st_skip is not None:
                z_skip = next((z for z in st_skip.zones if z.id == zone_id), None)
                if z_skip is not None and z_skip.status not in ("done", "loai"):
                    z_skip.status = prev_status  # type: ignore[assignment]
                    z_skip.r1_followup_done = False
                    z_skip.last_r_followup_level = max(
                        int(getattr(z_skip, "last_r_followup_level", 0) or 0),
                        int(reached_r_level),
                    )
                    _state_write(params, st_skip)
            _send_log(
                settings,
                f"[r1] skip TRADE_MANAGEMENT (SKIP_TRADE_MANAGEMENT) at {_r_level_text(reached_r_level)}R "
                f"| zone_id={zone_id}",
            )
            return
        st0 = _state_read(params)
        if st0 is None:
            return
        z0 = next((z for z in st0.zones if z.id == zone_id), None)
        if z0 is None:
            return
        if z0.status in ("done", "loai"):
            return
        followup_flag = "r1_followup_done"
        should_toggle_followup_flag = True

        def _reset_followup_flag(zone: Zone) -> None:
            if should_toggle_followup_flag and followup_flag:
                setattr(zone, followup_flag, False)

        r_level_text = _r_level_text(reached_r_level)
        if int(reached_r_level) <= 1:
            # User rule: mốc 1R không chạy TRADE_MANAGEMENT (không capture, không gọi OpenAI).
            z0.status = prev_status  # type: ignore[assignment]
            _reset_followup_flag(z0)
            z0.last_r_followup_level = max(int(getattr(z0, "last_r_followup_level", 0) or 0), 1)
            _state_write(params, st0)
            _send_log(settings, f"[r1] skip TRADE_MANAGEMENT at {r_level_text}R | zone_id={zone_id}")
            return
        if _skip_scalp_r1_followup_if_needed(z0, settings=settings, params=params):
            z0.status = prev_status  # type: ignore[assignment]
            _state_write(params, st0)
            return
        if not z0.trade_line or not z0.mt5_ticket:
            z0.status = prev_status
            _reset_followup_flag(z0)
            _state_write(params, st0)
            return

        parsed, err = _parse_trade_from_zone_trade_line(z0.trade_line, symbol_override=params.mt5_symbol)
        if err or parsed is None:
            z0.status = prev_status
            _reset_followup_flag(z0)
            _state_write(params, st0)
            return

        tk_check = int(z0.mt5_ticket or 0)
        dry = bool(params.mt5_dry_run)

        _send_user_notice(
            settings,
            f"Giá đã đạt mức {r_level_text}R.",
            "Đang lấy biểu đồ M5 và hỏi AI quản lý lệnh.",
            zone=z0,
            params=params,
        )

        _wait_for_m5_analysis_capture_slot(
            settings,
            source="r1 TRADE_MANAGEMENT",
            params=params,
            zone=z0,
        )
        capture_charts(
            coinmap_yaml=params.capture_coinmap_yaml,
            charts_dir=params.charts_dir,
            storage_state_path=params.storage_state_path,
            email=settings.coinmap_email,
            password=settings.coinmap_password,
            tradingview_password=settings.tradingview_password,
            save_storage_state=not params.no_save_storage,
            headless=params.headless,
            reuse_browser_context=None,
            main_chart_symbol=read_main_chart_symbol(params.charts_dir),
            coinmap_capture_intervals=("5m",),
        )
        json_path = coinmap_xauusd_5m_json_path(params.charts_dir)
        if json_path is None or not json_path.is_file():
            raise SystemExit(f"r1-followup: no main 5m Coinmap JSON under {params.charts_dir}")
        openai_merged = write_openai_coinmap_merged_from_raw_export(json_path)

        prev = _openai_followup_prev_response_id(params)
        current_sl: Optional[float] = None
        current_tp: Optional[float] = None
        if tk_check > 0:
            accs_for_prompt = load_mt5_accounts_for_cli(params.mt5_accounts_json)
            prim_for_prompt = primary_account(accs_for_prompt) if accs_for_prompt else None
            if prim_for_prompt is not None:
                current_sl, current_tp, sltp_msg = mt5_ticket_current_sltp(
                    tk_check,
                    dry_run=dry,
                    terminal_path=prim_for_prompt.terminal_path,
                    login=prim_for_prompt.login,
                    password=prim_for_prompt.password,
                    server=prim_for_prompt.server,
                )
                _send_log(settings, f"[r1] đọc SL/TP hiện tại | {sltp_msg}")
        user_text = TP1_POST_TOUCH_USER_TEMPLATE.format(
            plan_label=z0.label,
            entry_side=str(getattr(parsed, "side", "") or "").upper(),
            entry_price=_fmt_level_for_prompt(getattr(parsed, "price", None)),
            current_sl=_fmt_level_for_prompt(current_sl),
            current_tp=_fmt_level_for_prompt(current_tp),
        )
        out_text, new_id = run_single_followup_responses(
            api_key=settings.openai_api_key,
            user_text=user_text,
            coinmap_json_paths=[openai_merged],
            previous_response_id=prev or "",
            vector_store_ids=settings.openai_vector_store_ids,
            store=settings.openai_responses_store,
            include=settings.openai_responses_include,
            # Force config for [TRADE_MANAGEMENT]
            model="gpt-5.4-mini",
            reasoning_summary="auto",
            reasoning_effort="medium",
        )
        _openai_followup_persist_new_id(params, new_id)
        _send_log(settings, "[r1] OpenAI TRADE_MANAGEMENT xong (ẩn raw JSON).")

        dec = parse_tp1_followup_decision(out_text)
        st1 = _state_read(params)
        if st1 is None:
            return
        z1 = next((z for z in st1.zones if z.id == zone_id), None)
        if z1 is None:
            return

        if dec is None:
            _reset_followup_flag(z1)
            z1.status = prev_status
            _state_write(params, st1)
            _send_user_notice(
                settings,
                f"Tại {r_level_text}R: không đọc được quyết định từ AI.",
                "Hệ thống sẽ thử lại — xem log kỹ thuật nếu cần chi tiết.",
                zone=z1,
                params=params,
            )
            return

        tk = int(z1.mt5_ticket or 0)
        dry = bool(params.mt5_dry_run)
        exe = bool(params.mt5_execute)
        slot_raw = resolve_session_slot_raw(
            zone_session_slot=getattr(z1, "session_slot", None),
            shard_path=params.shard_path,
        )
        if not params.no_telegram:
            send_trade_management_reason_notice(
                bot_token=settings.telegram_bot_token,
                telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
                zone_label=z1.label,
                session_slot=slot_raw,
                action=dec.sau_tp1,
                reason=dec.reason,
                trade_line=None,
                **_mt5_telegram_zone_context(z1, params),
            )

        if dec.sau_tp1 == "giu_nguyen":
            z1.status = prev_status  # type: ignore[assignment]
            _reset_followup_flag(z1)
            _state_write(params, st1)
            _send_user_notice(
                settings,
                f"Tại {r_level_text}R: AI chọn «giữ nguyên» — không đổi lệnh.",
                "Tiếp tục theo dõi theo plan.",
                zone=z1,
                params=params,
            )
            return

        if dec.sau_tp1 == "loại":
            if exe and tk > 0:
                accs_lo = load_mt5_accounts_for_cli(params.mt5_accounts_json)
                tmap_lo = z1.mt5_tickets_by_account or {}
                if accs_lo and tmap_lo:
                    summ_lo = mt5_cancel_pending_or_close_all_accounts(
                        tmap_lo, accs_lo, dry_run=dry
                    )
                    _send_log(
                        settings,
                        f"[r1] mt5_cancel_close multi: {format_mt5_multi_manage_for_telegram(summ_lo)}".strip(),
                    )
                else:
                    prim_lo = primary_account(accs_lo) if accs_lo else None
                    r = mt5_cancel_pending_or_close_position(
                        tk,
                        dry_run=dry,
                        terminal_path=prim_lo.terminal_path if prim_lo else None,
                        login=prim_lo.login if prim_lo else None,
                        password=prim_lo.password if prim_lo else None,
                        server=prim_lo.server if prim_lo else None,
                    )
                    _send_log(settings, f"[r1] mt5_cancel_close: {r.message}".strip())
            z1.status = "loai"
            z1.mt5_tickets_by_account = None
            _reset_followup_flag(z1)
            _state_write(params, st1)
            _send_user_notice(
                settings,
                f"Tại {r_level_text}R: AI chọn «loại» — đóng / bỏ theo dõi vùng.",
                "Đã gửi lệnh đóng trên MT5 nếu bật thực thi.",
                zone=z1,
                params=params,
            )
            return

        if dec.new_sl is None and dec.new_tp is None:
            _reset_followup_flag(z1)
            z1.status = prev_status  # type: ignore[assignment]
            _state_write(params, st1)
            return

        next_sl = float(dec.new_sl) if dec.new_sl is not None else float(parsed.sl)
        if dec.new_tp is not None:
            new_parsed = replace(parsed, sl=next_sl, tp1=float(dec.new_tp), tp2=None)
        else:
            new_parsed = replace(parsed, sl=next_sl)

        used_inplace_r1 = False
        accs_r1 = load_mt5_accounts_for_cli(params.mt5_accounts_json)
        if exe and tk > 0:
            tmap_r1 = z1.mt5_tickets_by_account or {}
            if accs_r1 and tmap_r1:
                ch_s = mt5_chinh_trade_line_all_accounts(
                    tmap_r1,
                    accs_r1,
                    new_parsed,
                    dry_run=dry,
                    symbol_override=params.mt5_symbol,
                )
                ch_txt = format_mt5_multi_chinh_for_telegram(ch_s)
                _send_log(settings, f"[r1] mt5_chinh_inplace multi: {ch_txt}".strip())
                if ch_s.ok_all_inplace:
                    used_inplace_r1 = True
                    if not params.no_telegram:
                        send_mt5_execution_log_to_ngan_gon_chat(
                            bot_token=settings.telegram_bot_token,
                            telegram_chat_id=settings.telegram_chat_id,
                            telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
                            telegram_log_chat_id=settings.telegram_log_chat_id,
                            source="r1-followup",
                            text=ch_txt,
                            zone_label=z1.label,
                            trade_line=z0.trade_line,
                            previous_trade_line=z0.trade_line,
                            execution_ok=True,
                            action="chinh_trade_line",
                            session_slot=resolve_session_slot_raw(
                                zone_session_slot=getattr(z1, "session_slot", None),
                                shard_path=params.shard_path,
                            ),
                            **_mt5_telegram_zone_context(z1, params),
                        )
                else:
                    _send_log(
                        settings,
                        f"[r1] chinh_trade_line failed multi; giữ nguyên lệnh cũ, không đặt mới: {ch_txt}".strip(),
                    )
            else:
                prim_r = primary_account(accs_r1) if accs_r1 else None
                cr = mt5_chinh_trade_line_inplace(
                    tk,
                    new_parsed,
                    dry_run=dry,
                    symbol_override=params.mt5_symbol,
                    terminal_path=prim_r.terminal_path if prim_r else None,
                    login=prim_r.login if prim_r else None,
                    password=prim_r.password if prim_r else None,
                    server=prim_r.server if prim_r else None,
                )
                _send_log(settings, f"[r1] mt5_chinh_inplace: {cr.message}".strip())
                if cr.ok and cr.outcome in ("modified_sltp", "modified_pending", "dry_run"):
                    used_inplace_r1 = True
                    if not params.no_telegram:
                        send_mt5_execution_log_to_ngan_gon_chat(
                            bot_token=settings.telegram_bot_token,
                            telegram_chat_id=settings.telegram_chat_id,
                            telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
                            telegram_log_chat_id=settings.telegram_log_chat_id,
                            source="r1-followup",
                            text=cr.message,
                            zone_label=z1.label,
                            trade_line=z0.trade_line,
                            previous_trade_line=z0.trade_line,
                            execution_ok=True,
                            action="chinh_trade_line",
                            session_slot=resolve_session_slot_raw(
                                zone_session_slot=getattr(z1, "session_slot", None),
                                shard_path=params.shard_path,
                            ),
                            **_mt5_telegram_zone_context(z1, params),
                        )
                else:
                    _send_log(
                        settings,
                        f"[r1] chinh_trade_line failed; giữ nguyên lệnh cũ, không đặt mới: {cr.message}".strip(),
                    )

        if exe and not used_inplace_r1:
            _reset_followup_flag(z1)
            z1.status = prev_status  # type: ignore[assignment]
            _state_write(params, st1)
            _send_user_notice(
                settings,
                f"Tại {r_level_text}R: chỉnh lệnh không thành công.",
                "Giữ nguyên lệnh/ticket cũ; hệ thống không đóng, không huỷ và không đặt lệnh mới.",
                zone=z1,
                params=params,
            )
            return
        elif exe and used_inplace_r1:
            _send_user_notice(
                settings,
                f"Tại {r_level_text}R: đã cập nhật lệnh tại chỗ (SL/TP hoặc sửa lệnh chờ).",
                "Không đóng + mở mới; ticket giữ nguyên.",
                zone=z1,
                params=params,
            )
        # TRADE_MANAGEMENT: chỉ thực thi trade_line mới trên MT5, không ghi đè trade_line gốc trong state.
        z1.status = prev_status  # type: ignore[assignment]
        z1.tp1_followup_done = False
        z1.r1_followup_done = False
        z1.last_r_followup_level = max(
            int(getattr(z1, "last_r_followup_level", 0) or 0), int(reached_r_level)
        )
        if dec.new_sl is not None:
            z1.managed_sl = float(dec.new_sl)
        if dec.new_tp is not None:
            z1.managed_tp = float(dec.new_tp)
        _state_write(params, st1)
        return
    except Exception as e:
        _send_log(settings, f"[r1] ERROR | zone_id={zone_id} | {e!s}")
        _send_user_notice(
            settings,
            f"Lỗi khi xử lý bước tại {_r_level_text(reached_r_level)}R.",
            str(e),
            params=params,
        )
        re_raise_unless_openai(e, exit_on_openai=False, settings=settings)


def _auto_entry_job(
    *,
    settings: Settings,
    params: WatchlistDaemonParams,
    zone_id: str,
) -> None:
    """
    Fire-and-forget worker:
    - Zone must already be ``dang_vao_lenh`` (main loop sets this before spawn; next poll skips duplicate dispatch).
    - Re-check hop_luu / trade_line / ticket; execute MT5; persist ``vao_lenh`` or revert to ``cham`` on failure.

    Khi MT5 thất bại: ``cham`` + ``auto_entry_mt5_failed=True`` — không tự dispatch auto-entry nữa
    cho đến khi giá chạm vùng chờ lại (``vung_cho`` → ``cham``) hoặc sửa state thủ công.

    Does not use ``dang_thuc_thi``; that status remains for zone-touch / TP1 / other flows.
    """
    try:
        st0 = _state_read(params)
        if st0 is None:
            return
        z0 = next((z for z in st0.zones if z.id == zone_id), None)
        if z0 is None:
            return
        if z0.status != "dang_vao_lenh":
            return
        if z0.mt5_ticket is not None and int(z0.mt5_ticket or 0) > 0:
            return
        if not z0.trade_line:
            z0.status = "cham"
            z0.auto_entry_retry_after = ""
            z0.auto_entry_mt5_failed = False
            _state_write(params, st0)
            return
        if z0.hop_luu is None:
            z0.status = "cham"
            z0.auto_entry_retry_after = ""
            z0.auto_entry_mt5_failed = False
            _state_write(params, st0)
            return
        thr = int(auto_mt5_hop_luu_threshold_for_label(z0.label))
        if not auto_mt5_hop_luu_passes_for_label(z0.label, int(z0.hop_luu)):
            z0.status = "cham"
            z0.auto_entry_retry_after = ""
            z0.auto_entry_mt5_failed = False
            _state_write(params, st0)
            return
        if not params.mt5_execute:
            _send_log(settings, f"[auto-entry] mt5_execute=off | zone_id={zone_id} skip")
            _send_user_notice(
                settings,
                "Tự động vào lệnh đang tắt.",
                "Vùng được giữ ở trạng thái chờ — bật thực thi MT5 nếu cần.",
                zone=z0,
                params=params,
            )
            z0.status = "cham"
            z0.auto_entry_retry_after = ""
            z0.auto_entry_mt5_failed = False
            _state_write(params, st0)
            return

        parsed, err = _parse_trade_from_zone_trade_line(z0.trade_line, symbol_override=params.mt5_symbol)
        if err or parsed is None:
            st1 = _state_read(params)
            if st1 is not None:
                for z in st1.zones:
                    if z.id == zone_id:
                        z.status = "cham"
                        z.auto_entry_retry_after = ""
                        z.auto_entry_mt5_failed = False
                        break
                _state_write(params, st1)
            _send_log(settings, f"[auto-entry] parse_trade_line_failed | zone_id={zone_id} err={err}")
            _send_user_notice(
                settings,
                "Tự động vào lệnh: không hiểu được dòng lệnh.",
                "Kiểm tra trade_line trong trạng thái vùng.",
                zone=z0,
                params=params,
            )
            return
        parsed = trade_with_update_scalp_entry_lot_default(
            parsed,
            zone_source=(z0.source or ""),
        )
        if parsed.raw_line:
            z0.trade_line = parsed.raw_line.strip()

        accs_ae = load_mt5_accounts_for_zone_entry(
            zone_source=(z0.source or ""),
            cli_path=params.mt5_accounts_json,
        )
        exec_accs_ae, slot_ae, _blocked_ae, subset_missing_ae = _resolve_zone_entry_accounts(z0, params)
        if accs_ae is not None and len(accs_ae) == 0:
            _maybe_loai_zone_if_no_entry_accounts(
                z0,
                zone_id=zone_id,
                settings=settings,
                params=params,
                exec_accs=[],
                slot=slot_ae,
                log_prefix="auto-entry",
                missing_subset=subset_missing_ae or subset_accounts_json_basename(z0.source or "") or "accounts.json",
            )
            _state_write(params, st0)
            return
        if accs_ae:
            if _maybe_loai_zone_if_no_entry_accounts(
                z0,
                zone_id=zone_id,
                settings=settings,
                params=params,
                exec_accs=exec_accs_ae,
                slot=slot_ae,
                log_prefix="auto-entry",
            ):
                _state_write(params, st0)
                return
            summary_ae = execute_trade_all_accounts(
                parsed,
                exec_accs_ae,
                dry_run=params.mt5_dry_run,
                symbol_override=params.mt5_symbol,
                zone_label=z0.label,
                zone_id=zone_id,
                zone_source=z0.source,
                order_comment=_mt5_entry_order_comment(zone_id, zone=z0, params=params),
            )
            multi_ae = format_mt5_multi_for_telegram(summary_ae)
            if not params.no_telegram:
                send_mt5_execution_log_to_ngan_gon_chat(
                    bot_token=settings.telegram_bot_token,
                    telegram_chat_id=settings.telegram_chat_id,
                    telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
                    telegram_log_chat_id=settings.telegram_log_chat_id,
                    source="auto-entry",
                    text=multi_ae,
                    zone_label=z0.label,
                    trade_line=z0.trade_line,
                    execution_ok=summary_ae.ok_all,
                    session_slot=resolve_session_slot_raw(
                        zone_session_slot=getattr(z0, "session_slot", None),
                        shard_path=params.shard_path,
                    ),
                    **_mt5_telegram_zone_context(z0, params),
                )
            _send_log(settings, f"[auto-entry] mt5_execute_trade multi: {multi_ae[:400]}".strip())
            tid = _multi_summary_tracking_ticket(summary_ae, exec_accs_ae)
            ok_ae = tid > 0
            st2 = _state_read(params)
            if st2 is None:
                return
            for z in st2.zones:
                if z.id != zone_id:
                    continue
                if ok_ae and tid > 0:
                    z.mt5_ticket = tid
                    z.mt5_tickets_by_account = summary_ae.tickets_by_account_id or None
                    z.status = "vao_lenh"
                    z.tp1_followup_done = False
                    z.r1_followup_done = False
                    z.has_position = False
                    z.managed_sl = None
                    z.managed_tp = None
                    z.last_r_followup_level = 0
                    z.auto_entry_retry_after = ""
                    z.auto_entry_mt5_failed = False
                    _send_user_notice(
                        settings,
                        "Đã tự động vào lệnh MT5",
                        "",
                        zone=z0,
                        params=params,
                    )
                else:
                    z.status = "cham"
                    z.auto_entry_retry_after = ""
                    z.auto_entry_mt5_failed = True
                    _send_log(
                        settings,
                        f"[auto-entry] mt5_failed -> cham, không tự thử lại auto-entry "
                        f"(chạm vùng chờ lại hoặc sửa state) | zone_id={zone_id}",
                    )
                break
            _state_write(params, st2)
            return

        ex = execute_trade(
            parsed,
            dry_run=params.mt5_dry_run,
            symbol_override=params.mt5_symbol,
            order_comment=_mt5_entry_order_comment(zone_id, zone=z0, params=params),
        )
        if not params.no_telegram:
            send_mt5_execution_log_to_ngan_gon_chat(
                bot_token=settings.telegram_bot_token,
                telegram_chat_id=settings.telegram_chat_id,
                telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
                telegram_log_chat_id=settings.telegram_log_chat_id,
                source="auto-entry",
                text=format_mt5_execution_for_telegram(ex),
                zone_label=z0.label,
                trade_line=z0.trade_line,
                execution_ok=ex.ok,
                session_slot=resolve_session_slot_raw(
                    zone_session_slot=getattr(z0, "session_slot", None),
                    shard_path=params.shard_path,
                ),
                **_mt5_telegram_zone_context(z0, params),
            )
        _send_log(settings, f"[auto-entry] mt5_execute_trade: {ex.message}".strip())

        tid = int(ex.order) if ex.order else 0
        st2 = _state_read(params)
        if st2 is None:
            return
        for z in st2.zones:
            if z.id != zone_id:
                continue
            if ex.ok and tid > 0:
                z.mt5_ticket = tid
                z.mt5_tickets_by_account = None
                z.status = "vao_lenh"
                z.tp1_followup_done = False
                z.r1_followup_done = False
                z.has_position = False
                z.managed_sl = None
                z.managed_tp = None
                z.last_r_followup_level = 0
                z.auto_entry_retry_after = ""
                z.auto_entry_mt5_failed = False
                _send_user_notice(
                    settings,
                    "Đã tự động vào lệnh MT5",
                    "",
                    zone=z0,
                    params=params,
                )
            else:
                z.status = "cham"
                z.auto_entry_retry_after = ""
                z.auto_entry_mt5_failed = True
                _send_log(
                    settings,
                    f"[auto-entry] mt5_failed -> cham, không tự thử lại auto-entry "
                    f"(chạm vùng chờ lại hoặc sửa state) | zone_id={zone_id}",
                )
            break
        _state_write(params, st2)
        return
    except Exception as e:
        _send_log(settings, f"[auto-entry] ERROR | zone_id={zone_id} | {e!s}")
        _send_user_notice(settings, "Lỗi khi tự động vào lệnh.", str(e), params=params)
        re_raise_unless_openai(e, exit_on_openai=False, settings=settings)


def _zone_touch_job(
    *,
    settings: Settings,
    params: WatchlistDaemonParams,
    zone_id: str,
    last_price: float,
    after_retry_wait: bool = False,
) -> None:
    """
    Fire-and-forget worker:
    - capture Coinmap (scalp: đính kèm JSON M1; plan khác: M5)
    - call OpenAI follow-up
    - update zone status + trade_line + mt5 ticket (optional)

    ``after_retry_wait``: True khi dispatch từ vòng ``cham`` sau khi hết ``retry_at``
    (scalp ~10 phút + Coinmap M1; plan khác sau 3 slot M5 ``5n+1`` + buffer 2p), khác với lần chạm đầu từ ``vung_cho``.
    """
    st0 = _state_read(params)
    if st0 is None:
        return
    zone = next((z for z in st0.zones if z.id == zone_id), None)
    if zone is None:
        return

    try:
        lo, hi = parse_vung_cho_bounds(zone.vung_cho)
        _send_log(
            settings,
            f"[zone-touch] start | zone_id={zone_id} label={zone.label} "
            f"vung_cho={zone.vung_cho} bounds={lo}–{hi} last={last_price}",
        )

        loai_confirm_rounds = _ZONE_TOUCH_LOAI_CONFIRM_ROUNDS

        st_check = _state_read(params)
        if st_check is None:
            return
        zc = next((z for z in st_check.zones if z.id == zone_id), None)
        if zc is None:
            return
        # If user manually marked terminal states while job is running, stop.
        if zc.status in ("done", "loai"):
            _send_log(settings, f"[zone-touch] stop: zone already terminal ({zc.status}) | zone_id={zone_id}")
            return
        zone = zc

        if _settings_skip_intraday_alert_openai(settings):
            _send_log(
                settings,
                f"[zone-touch] skip_openai | zone_id={zone_id} "
                f"cooldown_s={_settings_skip_intraday_alert_cooldown_seconds(settings)}",
            )
            side_vn = "mua" if (zone.side or "").strip().upper() == "BUY" else "bán"
            _touch_title = (
                _zone_touch_after_retry_title(zone)
                if after_retry_wait
                else "Giá đã chạm vùng chờ."
            )
            _send_user_notice(
                settings,
                _touch_title,
                (
                    f"Last≈{last_price} ({side_vn}). Vùng chờ: {zone.vung_cho}.\n"
                    "OpenAI [INTRADAY_ALERT] đang tắt (SKIP_INTRADAY_ALERT_OPENAI) — không gọi Coinmap/AI."
                ).strip(),
                zone=zone,
                params=params,
            )
            cd_sec = _settings_skip_intraday_alert_cooldown_seconds(settings)
            until = ""
            if cd_sec > 0:
                until = (_now_utc() + timedelta(seconds=cd_sec)).isoformat()
            zone.status = "vung_cho"
            zone.retry_at = ""
            zone.loai_streak = 0
            zone.zone_touch_notify_cooldown_until = until
            _state_write(params, st_check)
            _send_log(
                settings,
                f"[zone-touch] skip_openai done | zone_id={zone_id} -> status=vung_cho "
                f"cooldown_until={until!r}",
            )
            return

        _chart_tf = "M1" if _is_scalp_zone(zone) else "M5"
        _touch_title = (
            _zone_touch_after_retry_title(zone)
            if after_retry_wait
            else "Giá đã chạm vùng chờ."
        )
        _send_user_notice(
            settings,
            _touch_title,
            f"Đang lấy dữ liệu biểu đồ {_chart_tf} và phân tích lại với AI.",
            zone=zone,
            params=params,
        )

        # Mark running (anti-spam + visibility). Daemon will handle retries using retry_at.
        zone.status = "dang_thuc_thi"
        zone.retry_at = ""
        _state_write(params, st_check)

        # Capture Coinmap (reuse capture pipeline; scalp → đọc M1, còn lại M5)
        from automation_tool.coinmap import capture_charts

        _touch_iv = ("1m",) if _is_scalp_zone(zone) else ("5m",)
        if _touch_iv == ("5m",):
            _wait_for_m5_analysis_capture_slot(
                settings,
                source="zone-touch INTRADAY_ALERT",
                params=params,
                zone=zone,
            )
        capture_charts(
            coinmap_yaml=params.capture_coinmap_yaml,
            charts_dir=params.charts_dir,
            storage_state_path=params.storage_state_path,
            email=settings.coinmap_email,
            password=settings.coinmap_password,
            tradingview_password=settings.tradingview_password,
            save_storage_state=not params.no_save_storage,
            headless=params.headless,
            reuse_browser_context=None,
            main_chart_symbol=read_main_chart_symbol(params.charts_dir),
            coinmap_capture_intervals=_touch_iv,
        )
        json_path, _cm_iv = _zone_touch_coinmap_main_json_path(params.charts_dir, zone)
        if json_path is None or not json_path.is_file():
            raise SystemExit(
                f"zone-touch: no main {_cm_iv} Coinmap JSON under {params.charts_dir}"
            )

        openai_merged = write_openai_coinmap_merged_from_raw_export(json_path)
        _send_log(
            settings,
            f"[zone-touch] coinmap_{_cm_iv}_raw={json_path} | openai_merged={openai_merged}",
        )

        prev = _openai_followup_prev_response_id(params)
        user_text = _touch_prompt(
            zone=zone,
            last_price=last_price,
            after_retry_wait=after_retry_wait,
        )
        out_text, new_id = run_single_followup_responses(
            api_key=settings.openai_api_key,
            user_text=user_text,
            coinmap_json_paths=[openai_merged],
            previous_response_id=prev or "",
            vector_store_ids=settings.openai_vector_store_ids,
            store=settings.openai_responses_store,
            include=settings.openai_responses_include,
            # Force config for [INTRADAY_ALERT]
            model="gpt-5.4-mini",
            reasoning_summary="auto",
            reasoning_effort="medium",
        )
        if _should_write_intraday_alert_anchor(params):
            _openai_followup_persist_new_id(params, new_id)
        if new_id:
            _send_log(settings, f"[zone-touch] openai_response_id={new_id}")

        send_phan_tich_alert_to_python_bot_if_any(
            bot_token=settings.telegram_bot_token,
            telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
            raw_openai_text=out_text,
            no_telegram=params.no_telegram,
            alert_label=zone.label,
            alert_vung_cho=(zone.vung_cho or "").strip(),
        )

        act = parse_journal_intraday_action_from_openai_text(out_text)
        # Default to "chờ" on parse failure
        if act is None:
            act = "chờ"

            # Always forward OpenAI output to Telegram if enabled:
            # - short/main channels as configured
            # - full raw log to TELEGRAM_LOG_CHAT_ID
            if not params.no_telegram:
                send_openai_output_to_telegram(
                    bot_token=settings.telegram_bot_token,
                    chat_id=settings.telegram_chat_id,
                    raw=out_text,
                    default_parse_mode=settings.telegram_parse_mode,
                    summary_chat_id=settings.telegram_output_ngan_gon_chat_id,
                )
            _send_log(settings, f"[zone-touch] openai_output_raw:\n{out_text}".strip())

        st1 = _state_read(params)
        if st1 is None:
            return
        z1 = next((z for z in st1.zones if z.id == zone_id), None)
        if z1 is None:
            return

        # [INTRADAY_ALERT] Schema E: không còn áp dụng `vung_cho` từ JSON để sửa vùng chờ trên disk
        # (chỉ giữ vùng baseline từ plan sáng / [INTRADAY_UPDATE] / seed thủ công).

        if act == "loại":
            terminal_loai = _apply_zone_touch_loai_decision(
                z1,
                confirm_rounds=loai_confirm_rounds,
            )
            if terminal_loai:
                _state_write(params, st1)
                _send_log(
                    settings,
                    f"[zone-touch] act=loai confirm {z1.loai_streak}/{loai_confirm_rounds} "
                    f"| zone_id={zone_id} -> status=loai",
                )
                _send_user_notice(
                    settings,
                    "Vùng được đánh dấu «loại» sau nhiều lần xác nhận.",
                    "Hệ thống không còn theo dõi vùng này theo kịch bản chạm giá.",
                    zone=z1,
                    params=params,
                )
                return
            # keep touched state; daemon will re-dispatch after retry_at
            _state_write(params, st1)
            _send_log(
                settings,
                f"[zone-touch] act=loai confirm {z1.loai_streak}/{loai_confirm_rounds} "
                f"| zone_id={zone_id} -> status=cham retry_at={z1.retry_at}",
            )
            _send_user_notice(
                settings,
                "AI gợi ý «loại» — chưa đủ lần xác nhận.",
                "Vùng vẫn được theo dõi; sẽ thử lại sau.",
                zone=z1,
                params=params,
            )
            return

        # Any non-loai action resets loai_streak.
        z1.loai_streak = 0
        z1.tp1_followup_done = False
        z1.r1_followup_done = False

        if act != "VÀO LỆNH":
            # keep touched state (no revert to vung_cho); daemon can retry later
            z1.status = "cham"
            z1.retry_at = _zone_touch_retry_at_iso(z1)
            _state_write(params, st1)
            _send_log(
                settings,
                f"[zone-touch] act={act} | zone_id={zone_id} -> status=cham retry_at={z1.retry_at}",
            )
            _send_user_notice(
                settings,
                "Sau khi chạm vùng: chưa vào lệnh lần này.",
                f"AI trả về hành động «{act}». Hệ thống sẽ thử lại sau.",
                zone=z1,
                params=params,
            )
            return

        # Schema E: ``VÀO LỆNH`` → vào lệnh ngay (không gate hop_luu); trade_line ưu tiên JSON,
        # không có thì markdown/OUTPUT_NGAN_GON, cuối cùng baseline vùng.
        zone_tl = (z1.trade_line or "").strip()
        parsed, err = parse_openai_output_md(
            out_text,
            symbol_override=params.mt5_symbol,
            fallback_trade_line=zone_tl or None,
        )
        if err or parsed is None:
            z1.status = "cham"
            z1.retry_at = _zone_touch_retry_at_iso(z1)
            _state_write(params, st1)
            _send_log(
                settings,
                f"[zone-touch] parse_trade_line_failed | err={err} | zone_id={zone_id} -> status=cham",
            )
            return

        parsed = trade_with_update_scalp_entry_lot_default(
            parsed,
            zone_source=(z1.source or ""),
        )
        z1.trade_line = (parsed.raw_line or "").strip()
        z1.status = "vao_lenh"
        z1.tp1_followup_done = False
        z1.r1_followup_done = False
        z1.has_position = False
        z1.managed_sl = None
        z1.managed_tp = None
        z1.last_r_followup_level = 0
        _state_write(params, st1)
        _send_log(
            settings,
            f"[zone-touch] act=VAO_LENH | zone_id={zone_id} -> status=vao_lenh | trade_line={z1.trade_line!r}",
        )
        _send_user_notice(
            settings,
            "Sau khi chạm vùng: AI xác nhận «VÀO LỆNH».",
            zone=z1,
            params=params,
        )

        if not params.mt5_execute:
            _send_log(settings, f"[zone-touch] mt5_execute=off | done | zone_id={zone_id}")
            return

        if z1.mt5_ticket is not None and int(z1.mt5_ticket or 0) > 0:
            _send_log(
                settings,
                f"[zone-touch] skip_mt5_execute | already_has_ticket | zone_id={zone_id} ticket={z1.mt5_ticket}",
            )
            return

        accs_zt = load_mt5_accounts_for_zone_entry(
            zone_source=(z1.source or ""),
            cli_path=params.mt5_accounts_json,
        )
        exec_accs_zt, slot_zt, _blocked_zt, subset_missing_zt = _resolve_zone_entry_accounts(z1, params)
        if accs_zt is not None and len(accs_zt) == 0:
            _maybe_loai_zone_if_no_entry_accounts(
                z1,
                zone_id=zone_id,
                settings=settings,
                params=params,
                exec_accs=[],
                slot=slot_zt,
                log_prefix="zone-touch",
                missing_subset=subset_missing_zt or subset_accounts_json_basename(z1.source or "") or "accounts.json",
            )
            _state_write(params, st1)
            return
        if accs_zt:
            if _maybe_loai_zone_if_no_entry_accounts(
                z1,
                zone_id=zone_id,
                settings=settings,
                params=params,
                exec_accs=exec_accs_zt,
                slot=slot_zt,
                log_prefix="zone-touch",
            ):
                _state_write(params, st1)
                return
            summary_zt = execute_trade_all_accounts(
                parsed,
                exec_accs_zt,
                dry_run=params.mt5_dry_run,
                symbol_override=params.mt5_symbol,
                zone_label=z1.label,
                zone_id=zone_id,
                zone_source=z1.source,
                order_comment=_mt5_entry_order_comment(zone_id, zone=z1, params=params),
            )
            # MARKET: MT5 trả fill price; chỉ dùng giá từ account primary để update trade_line.
            try:
                prim = primary_account(exec_accs_zt)
                filled_primary: Optional[float] = None
                for rex in summary_zt.results:
                    if rex.account_id != prim.id:
                        continue
                    tr = rex.trade_result or {}
                    fp = tr.get("price") if isinstance(tr, dict) else None
                    try:
                        fpf = float(fp)  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        fpf = 0.0
                    if fpf > 0.0:
                        filled_primary = fpf
                    break
                if filled_primary is not None:
                    z1.trade_line = inject_filled_price_into_trade_line(z1.trade_line, filled_primary)
            except Exception:
                pass
            zt_txt = format_mt5_multi_for_telegram(summary_zt)
            if not params.no_telegram:
                send_mt5_execution_log_to_ngan_gon_chat(
                    bot_token=settings.telegram_bot_token,
                    telegram_chat_id=settings.telegram_chat_id,
                    telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
                    telegram_log_chat_id=settings.telegram_log_chat_id,
                    source="zone-touch",
                    text=zt_txt,
                    zone_label=z1.label,
                    trade_line=z1.trade_line,
                    execution_ok=summary_zt.ok_all,
                    session_slot=resolve_session_slot_raw(
                        zone_session_slot=getattr(z1, "session_slot", None),
                        shard_path=params.shard_path,
                    ),
                    **_mt5_telegram_zone_context(z1, params),
                )
            _send_log(settings, f"[zone-touch] mt5_execute_trade multi: {zt_txt[:400]}".strip())
            tid = _multi_summary_tracking_ticket(summary_zt, exec_accs_zt)
            if tid > 0:
                st2 = _state_read(params)
                if st2 is None:
                    return
                for z in st2.zones:
                    if z.id == zone_id:
                        z.mt5_ticket = tid
                        z.mt5_tickets_by_account = summary_zt.tickets_by_account_id or None
                        z.trade_line = (z1.trade_line or "").strip()
                        break
                _state_write(params, st2)
                _send_log(settings, f"[zone-touch] mt5_ticket_saved | zone_id={zone_id} ticket={tid}")
                _send_user_notice(
                    settings,
                    "Đã tự động vào lệnh MT5",
                    "",
                    zone=z1,
                    params=params,
                )
            return

        ex = execute_trade(
            parsed,
            dry_run=params.mt5_dry_run,
            symbol_override=params.mt5_symbol,
            order_comment=_mt5_entry_order_comment(zone_id, zone=z1, params=params),
        )
        # MARKET: MT5 trả fill price; update trade_line để theo dõi 1R/TP1.
        try:
            tr = ex.trade_result or {}
            fp = tr.get("price") if isinstance(tr, dict) else None
            z1.trade_line = inject_filled_price_into_trade_line(z1.trade_line, fp)  # type: ignore[arg-type]
        except Exception:
            pass
        if not params.no_telegram:
            send_mt5_execution_log_to_ngan_gon_chat(
                bot_token=settings.telegram_bot_token,
                telegram_chat_id=settings.telegram_chat_id,
                telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
                telegram_log_chat_id=settings.telegram_log_chat_id,
                source="zone-touch",
                text=format_mt5_execution_for_telegram(ex),
                zone_label=z1.label,
                trade_line=z1.trade_line,
                execution_ok=ex.ok,
                session_slot=resolve_session_slot_raw(
                    zone_session_slot=getattr(z1, "session_slot", None),
                    shard_path=params.shard_path,
                ),
                **_mt5_telegram_zone_context(z1, params),
            )
        _send_log(settings, f"[zone-touch] mt5_execute_trade: {ex.message}".strip())

        tid = int(ex.order) if ex.order else 0
        if ex.ok and tid > 0:
            st2 = _state_read(params)
            if st2 is None:
                return
            for z in st2.zones:
                if z.id == zone_id:
                    z.mt5_ticket = tid
                    z.mt5_tickets_by_account = None
                    z.trade_line = (z1.trade_line or "").strip()
                    break
            _state_write(params, st2)
            _send_log(settings, f"[zone-touch] mt5_ticket_saved | zone_id={zone_id} ticket={tid}")
            _send_user_notice(
                settings,
                "Đã tự động vào lệnh MT5",
                "",
                zone=z1,
                params=params,
            )
        return
    except Exception as e:
        # On any error: keep touched state (no revert to vung_cho); daemon will retry using retry_at.
        # IMPORTANT: Do not re-raise here; this job runs inside a long-lived daemon-plan.
        try:
            stx = _state_read(params)
            if stx is not None:
                for z in stx.zones:
                    if z.id == zone_id:
                        z.status = "cham"
                        # If zone-touch failed mid-flight, retry soon (1 minute) instead of the normal cadence.
                        z.retry_at = _retry_at_iso(1)
                        break
                _state_write(params, stx)
        except Exception:
            pass
        _send_log(settings, f"[zone-touch] ERROR | zone_id={zone_id} | {e!s}")
        _send_user_notice(
            settings,
            "Lỗi khi xử lý chạm vùng chờ.",
            "Xem kênh log kỹ thuật để biết chi tiết.",
            zone=zone,
            params=params,
        )
        # Only OpenAI errors are "handled" by re_raise_unless_openai (log-and-continue here).
        # For any other error, swallow to keep daemon-plan alive (state has been reverted to cham above).
        try:
            re_raise_unless_openai(e, exit_on_openai=False, settings=settings)
        except Exception:
            return


_DAEMON_GIA_MANIFEST_RECONCILE_DEBOUNCE_S = 10.0


def _daemon_gia_tick_manifest_updated_at_reconcile(
    zones_dir: Path,
    *,
    state: dict[str, Any],
    debounce_s: float = _DAEMON_GIA_MANIFEST_RECONCILE_DEBOUNCE_S,
) -> Optional[int]:
    """
    When ``zones_manifest.json`` ``updated_at`` changes, wait ``debounce_s`` after the latest
    change (trailing debounce), then ``reconcile_daemon_plans_at_boot``.
    Returns spawn count when reconcile runs, else ``None``.
    """
    cur = read_manifest_updated_at(zones_dir)
    if cur is None:
        return None

    last_rec = state.get("last_reconciled_updated_at")
    if last_rec is None:
        state["last_reconciled_updated_at"] = cur
        return None

    if cur == last_rec:
        state.pop("pending_updated_at", None)
        state.pop("pending_since_mono", None)
        return None

    pending = state.get("pending_updated_at")
    if cur != pending:
        state["pending_updated_at"] = cur
        state["pending_since_mono"] = time.monotonic()
        return None

    since = state.get("pending_since_mono")
    if since is None:
        state["pending_since_mono"] = time.monotonic()
        return None

    if (time.monotonic() - float(since)) < debounce_s:
        return None

    prev_rec = last_rec
    n = reconcile_daemon_plans_at_boot(zones_dir)
    state["last_reconciled_updated_at"] = cur
    state.pop("pending_updated_at", None)
    state.pop("pending_since_mono", None)
    _log.info(
        "tv-watchlist-daemon (gia) | zones_manifest updated_at %s -> %s | "
        "reconcile-daemon-plans after %.0fs debounce | spawned %s",
        prev_rec,
        cur,
        debounce_s,
        n,
    )
    return n


def _tv_watchlist_price_only_loop(
    *,
    settings: Settings,
    params: WatchlistDaemonParams,
    sym: str,
    poll_s: float,
    get_price: Callable[[int], Optional[float]],
    price_log_source: str = "TradingView title",
    mt5_stale_reconnect_s: float = 0.0,
    mt5_sess: Optional[DaemonPlanMt5PriceSession] = None,
) -> None:
    """Daemon giá: poll giá (MT5 bid hoặc title TV) → shared memory (optional mirror ``last.txt``)."""
    last_path = params.last_price_path or default_last_price_path(sym)
    shm = open_writer_shared_memory_v2(sym)
    zones_dir = default_zones_dir(sym)
    manifest_reconcile_state: dict[str, Any] = {}
    reconciled_after_first_last = False
    heartbeat_s = 300.0
    last_heartbeat_at = 0.0
    telegram_log_interval_s = 60.0
    last_telegram_log_at = 0.0
    stale_s = float(mt5_stale_reconnect_s or 0.0)
    stale_state: dict[str, Any] = {}
    last_prices: list[float] = []
    _tz_hcm = ZoneInfo(params.timezone_name or "Asia/Ho_Chi_Minh")
    # Auto-stop 00:30 (12h30 đêm)
    _gia_stop_deadline = _daemon_gia_compute_stop_deadline(params.timezone_name or "Asia/Ho_Chi_Minh")
    _send_log(
        settings,
        f"[daemon-gia] auto-stop 00:30 | mốc={_gia_stop_deadline.strftime('%Y-%m-%d %H:%M %Z')}",
    )
    _gia_stop_logged = False
    try:
        while True:
            # ── Kiểm tra auto-stop 00:30 ──────────────────────────────────────
            _now_tz = datetime.now(_tz_hcm)
            if _now_tz >= _gia_stop_deadline:
                if not _gia_stop_logged:
                    _gia_stop_logged = True
                    _send_log(
                        settings,
                        f"[daemon-gia] đã đến 00:30 | đang huỷ toàn bộ pending orders rồi dừng…",
                    )
                _daemon_gia_cancel_all_pending_at_shutdown(
                    params.mt5_accounts_json,
                    dry_run=bool(params.mt5_dry_run),
                    settings=settings,
                )
                _send_log(settings, "[daemon-gia] auto-stop 00:30 | đã dừng daemon giá.")
                break
            try:
                _daemon_gia_tick_manifest_updated_at_reconcile(
                    zones_dir, state=manifest_reconcile_state
                )
            except Exception as e:
                _log.warning(
                    "tv-watchlist-daemon (gia) | updated_at watch / reconcile-daemon-plans: %s",
                    e,
                )

            wms = min(15_000, max(2_000, int(poll_s * 1000)))
            p_last = get_price(wms)
            if stale_s > 0 and mt5_sess is not None and _daemon_gia_stale_reensure_due(
                stale_state,
                p_last,
                stale_s=stale_s,
                now_m=time.monotonic(),
            ):
                _log.warning(
                    "tv-watchlist-daemon (gia) | bid unchanged for >= %.0fs — MT5 re-ensure primary",
                    stale_s,
                )
                if mt5_sess.reensure_primary_session():
                    p_last = get_price(wms)
                else:
                    _log.warning(
                        "tv-watchlist-daemon (gia) | MT5 re-ensure primary failed: %s",
                        mt5_sess.last_error,
                    )
            if p_last is not None:
                plf = float(p_last)
                if not last_prices or plf != float(last_prices[-1]):
                    last_prices.append(plf)
                    if len(last_prices) > 15:
                        last_prices.pop(0)
                    write_last_prices_shared(shm, list(last_prices))
                if params.mirror_last_price_file:
                    write_last_price_file(plf, last_path)
                if not reconciled_after_first_last:
                    try:
                        n = reconcile_daemon_plans_at_boot(zones_dir)
                        _log.info(
                            "tv-watchlist-daemon (gia) | reconcile-daemon-plans after first last "
                            "spawned %s process(es)",
                            n,
                        )
                        reconciled_after_first_last = True
                        ts0 = read_manifest_updated_at(zones_dir)
                        if ts0:
                            manifest_reconcile_state["last_reconciled_updated_at"] = ts0
                    except Exception as e:
                        _log.warning(
                            "tv-watchlist-daemon (gia) | reconcile-daemon-plans after first last failed: %s",
                            e,
                        )
                now_mono_tg = time.monotonic()
                if (now_mono_tg - last_telegram_log_at) >= telegram_log_interval_s:
                    last_telegram_log_at = now_mono_tg
                    mirror = f" mirror={last_path}" if params.mirror_last_price_file else ""
                    _send_log(
                        settings,
                        f"[daemon-gia] last_prices(<=15) <- {plf} | n={len(last_prices)} | symbol={sym} | source={price_log_source}{mirror}",
                    )
                _poll_terminal.info(
                    "tv-watchlist-daemon (gia) | symbol=%s | last=%s | n=%s | source=%s",
                    sym,
                    plf,
                    len(last_prices),
                    price_log_source,
                )
            else:
                _poll_terminal.info(
                    "tv-watchlist-daemon (gia) | symbol=%s | last=(none) | source=%s",
                    sym,
                    price_log_source,
                )
            try:
                now_mono = time.monotonic()
                if p_last is not None and (now_mono - last_heartbeat_at) >= heartbeat_s:
                    last_heartbeat_at = now_mono
                    _log.info(
                        "tv-watchlist-daemon (gia) alive | symbol=%s last=%s source=%s",
                        sym,
                        p_last,
                        price_log_source,
                    )
            except Exception:
                pass
            time.sleep(poll_s)
    finally:
        try:
            shm.close()
        except Exception:
            pass


def _daemon_plan_main_loop(
    *,
    settings: Settings,
    params: WatchlistDaemonParams,
    sym: str,
    poll_s: float,
) -> None:
    """
    One shard, one process: đọc Last (MT5 bid) từ shared memory / ``last.txt`` do daemon giá ghi.
    Run zone pipeline **sequentially**. Exit when the zone reaches ``done`` or ``loai``.
    """
    if params.shard_path is None:
        raise ValueError("daemon-plan requires params.shard_path")
    last_price_file = params.last_price_path or default_last_price_path(sym)
    heartbeat_s = 300.0
    last_heartbeat_at = 0.0
    shard_tag = str(params.shard_path)
    telegram_plan_interval_s = 60.0
    last_plan_tg_at = 0.0
    _send_log(
        settings,
        f"[daemon-plan] start | shard={shard_tag} symbol={sym} last=MT5_bid via shared memory (daemon giá)",
    )
    stop_deadline: Optional[datetime] = None
    last_stop_wait_log_at = 0.0
    tz_name = (params.timezone_name or "Asia/Ho_Chi_Minh").strip() or "Asia/Ho_Chi_Minh"
    started = datetime.now(ZoneInfo(tz_name))
    initial_state = _state_read(params)
    stop_deadline = compute_daemon_plan_effective_stop_deadline_local(
        started,
        tz_name,
        stop_at_hour=params.stop_at_hour,
        stop_at_minute=int(params.stop_at_minute or 0),
        shard_path=params.shard_path,
        state_updated_at=initial_state.updated_at if initial_state is not None else None,
    )
    if stop_deadline is not None:
        if params.stop_at_hour is None:
            cut_desc = (
                f"auto theo shard (base 02:00, thứ Sáu 01:00; lệch 1 phút/zone, {tz_name}) "
                f"| mốc={stop_deadline.strftime('%Y-%m-%d %H:%M')}"
            )
        elif int(params.stop_at_hour) == 0 and int(params.stop_at_minute or 0) == 0:
            cut_desc = f"12h đêm (00:00 ngày kế, {tz_name}) | mốc={stop_deadline.strftime('%Y-%m-%d %H:%M')}"
        else:
            sh = int(params.stop_at_hour)
            sm = int(params.stop_at_minute or 0)
            cut_desc = (
                f"dừng khi ≥ {sh:02d}:{sm:02d} ({tz_name}) | mốc cùng ngày={stop_deadline.strftime('%Y-%m-%d %H:%M')}"
            )
        _send_log(
            settings,
            f"[daemon-plan] cắt giờ | {cut_desc} (pending → huỷ; chỉ chờ khi còn position đã khớp)",
        )
    else:
        _send_log(settings, "[daemon-plan] cắt giờ | đã tắt cutoff tự động/thủ công")
    try:
        while True:
            st = _state_read(params)
            if stop_deadline is not None:
                now_local = datetime.now(ZoneInfo(tz_name))
                if now_local >= stop_deadline:
                    zones_list = list(st.zones) if st is not None and st.zones else []
                    blocking, detail = daemon_plan_resolve_cutoff_mt5(
                        zones_list,
                        dry_run=bool(params.mt5_dry_run),
                        accounts_json=params.mt5_accounts_json,
                        settings=settings,
                        shard_tag=shard_tag,
                    )
                    if blocking:
                        now_mono = time.monotonic()
                        if (now_mono - last_stop_wait_log_at) >= 60.0:
                            last_stop_wait_log_at = now_mono
                            _send_log(
                                settings,
                                f"[daemon-plan] quá giờ cắt — chưa kết thúc | shard={shard_tag} | {detail}",
                            )
                            _poll_terminal.info(
                                "daemon-plan | shard=%s | past_cutoff waiting mt5 | %s",
                                shard_tag,
                                detail,
                            )
                        time.sleep(poll_s)
                        continue
                    _send_log(
                        settings,
                        f"[daemon-plan] exit | past_cutoff shard={shard_tag} | {detail}",
                    )
                    notice_zone = _mark_daemon_plan_cutoff_loai(st)
                    if st is not None and st.zones:
                        _state_write(params, st)
                    _send_user_notice(
                        settings,
                        "Đã ngưng theo dõi.",
                        f"Lý do: quá giờ cắt — {detail}",
                        zone=notice_zone,
                        params=params,
                    )
                    return

            if st is None or not st.zones:
                _poll_terminal.info(
                    "daemon-plan | tick | sym=%s | zones=0 (no state)",
                    sym,
                )
                time.sleep(poll_s)
                continue

            z0 = st.zones[0]
            if z0.status in ("done", "loai"):
                _send_log(
                    settings,
                    f"[daemon-plan] exit | status={z0.status} shard={shard_tag} zone_id={z0.id}",
                )
                _loai_done_reason = (
                    "vùng đã hoàn thành (done)."
                    if z0.status == "done"
                    else "vùng đã loại."
                )
                _send_user_notice(
                    settings,
                    "Đã ngưng theo dõi.",
                    f"Lý do: {_loai_done_reason}",
                    zone=z0,
                    params=params,
                )
                return

            # TEMP: tắt thoát khi ticket MT5 đã đóng — bỏ comment block dưới để bật lại.
            # exit_closed, closed_detail = daemon_plan_should_exit_if_mt5_tickets_closed(
            #     list(st.zones),
            #     dry_run=bool(params.mt5_dry_run),
            #     accounts_json=params.mt5_accounts_json,
            #     settings=settings,
            #     shard_tag=shard_tag,
            # )
            # if exit_closed:
            #     _poll_terminal.info(
            #         "daemon-plan | shard=%s | exit | mt5_ticket_closed | %s",
            #         shard_tag,
            #         closed_detail,
            #     )
            #     _send_user_notice(
            #         settings,
            #         "Đã ngưng theo dõi.",
            #         f"Lý do: {closed_detail}",
            #         zone=z0,
            #         params=params,
            #     )
            #     return

            _seq, prices = read_last_prices_for_daemon_plan(sym, last_price_file)
            prices = _daemon_plan_positive_prices(prices)
            p_last = float(prices[-1]) if prices else None
            now_plan_tg = time.monotonic()
            if (now_plan_tg - last_plan_tg_at) >= telegram_plan_interval_s:
                last_plan_tg_at = now_plan_tg
                _send_log(
                    settings,
                    _daemon_plan_watch_telegram_text(
                        z0,
                        sym=sym,
                        p_last=p_last,
                    ),
                )
            if p_last is None or not prices:
                _poll_terminal.info(
                    "daemon-plan | tick | sym=%s | zone_id=%s | last=(none) | file=%s",
                    sym,
                    z0.id,
                    last_price_file,
                )
                time.sleep(poll_s)
                continue

            # Always scan the whole shared window (<=15) to avoid missing fast moves between polls.
            # IMPORTANT: newest -> oldest (reverse order).
            for p_last in reversed(prices):

                _poll_terminal.info(
                    "daemon-plan | tick | sym=%s | zone_id=%s | last=%s | vung_cho=%s | trade_line=%s",
                    sym,
                    z0.id,
                    p_last,
                    (z0.vung_cho or "").strip(),
                    (z0.trade_line or "").strip(),
                )

                try:
                    now_mono = time.monotonic()
                    if (now_mono - last_heartbeat_at) >= heartbeat_s:
                        last_heartbeat_at = now_mono
                except Exception:
                    pass

                # vung_cho / cham / vao_lenh / cho_tp1: SL hit (theo trade_line) → loại
                sl_invalidated = False
                for z in st.zones:
                    if z.status not in _DAEMON_PLAN_SL_LOAI_STATUSES:
                        continue
                    if _maybe_loai_zone_if_last_hit_sl(z, float(p_last), settings=settings, params=params):
                        sl_invalidated = True
                if sl_invalidated:
                    _state_write(params, st)

                st_auto = _state_read(params)
                if st_auto is not None:
                    for z in st_auto.zones:
                        if z.status not in ("vung_cho", "cham"):
                            continue
                        if z.mt5_ticket is not None and int(z.mt5_ticket or 0) > 0:
                            continue
                        if not z.trade_line:
                            continue
                        if z.hop_luu is None:
                            continue
                        thr = int(auto_mt5_hop_luu_threshold_for_label(z.label))
                        if not auto_mt5_hop_luu_passes_for_label(z.label, int(z.hop_luu)):
                            continue
                        if getattr(z, "auto_entry_mt5_failed", False):
                            continue
                        aer = (getattr(z, "auto_entry_retry_after", "") or "").strip()
                        if aer and not _is_retry_due(aer):
                            continue
                        exec_accs_pre, slot_pre, _, subset_missing_pre = _resolve_zone_entry_accounts(
                            z, params
                        )
                        if exec_accs_pre is not None and not exec_accs_pre:
                            if _maybe_loai_zone_if_no_entry_accounts(
                                z,
                                zone_id=z.id,
                                settings=settings,
                                params=params,
                                exec_accs=[],
                                slot=slot_pre,
                                log_prefix="auto-entry",
                                missing_subset=subset_missing_pre,
                            ):
                                _state_write(params, st_auto)
                            continue
                        z.status = "dang_vao_lenh"
                        z.auto_entry_retry_after = ""
                        _state_write(params, st_auto)
                        _send_log(
                            settings,
                            f"[auto-entry] dispatch | zone_id={z.id} label={z.label} hop_luu={z.hop_luu} thr({'>' if not is_scalp_label(z.label) else '>='})={thr}",
                        )
                        _auto_entry_job(settings=settings, params=params, zone_id=z.id)

                st_retry = _state_read(params)
                if st_retry is not None:
                    for z in st_retry.zones:
                        if z.status != "cham":
                            continue
                        if not _is_retry_due(getattr(z, "retry_at", "")):
                            continue
                        z.status = "dang_thuc_thi"
                        z.retry_at = ""
                        _state_write(params, st_retry)
                        _send_log(settings, f"[zone-touch] retry_dispatch | zone_id={z.id} last={p_last}")
                        _zone_touch_job(
                            settings=settings,
                            params=params,
                            zone_id=z.id,
                            last_price=float(p_last),
                            after_retry_wait=True,
                        )

                st = _state_read(params)
                if st is None or not st.zones:
                    break

                matched: list[Zone] = []
                for z in st.zones:
                    if z.status != "vung_cho":
                        continue
                    if _zone_touch_notify_cooldown_active(z):
                        continue
                    lo, hi = parse_vung_cho_bounds(z.vung_cho)
                    if lo is None or hi is None:
                        continue
                    p = float(p_last)
                    eps = float(params.eps)
                    if (float(lo) - eps) <= p <= (float(hi) + eps):
                        matched.append(z)

                for mz in matched:
                    st_current = _state_read(params)
                    if st_current is None:
                        break
                    z = next((zz for zz in st_current.zones if zz.id == mz.id), None)
                    if z is None or z.status != "vung_cho":
                        continue
                    _mark_initial_zone_touch_dispatch(
                        st_current,
                        touched_zone=z,
                        last_price=float(p_last),
                        settings=settings,
                        params=params,
                    )
                    _state_write(params, st_current)
                    _zone_touch_job(
                        settings=settings,
                        params=params,
                        zone_id=z.id,
                        last_price=float(p_last),
                        after_retry_wait=False,
                    )

                st_r1 = _state_read(params)
                if st_r1 is not None:
                    for z in st_r1.zones:
                        if z.status != "cho_tp1":
                            continue
                        if z.r1_followup_done:
                            continue
                        if not bool(getattr(z, "has_position", False)):
                            continue
                        if _skip_scalp_r1_followup_if_needed(z, settings=settings, params=params):
                            _state_write(params, st_r1)
                            continue
                        if not z.trade_line or not z.mt5_ticket or int(z.mt5_ticket) <= 0:
                            continue
                        parsed_r1, err_r1 = _parse_trade_from_zone_trade_line(
                            z.trade_line, symbol_override=params.mt5_symbol
                        )
                        if err_r1 or parsed_r1 is None:
                            continue
                        if _tp1_touched(parsed_r1, float(p_last)):
                            continue
                        reached_r_level = _max_r_multiple_reached(z, parsed_r1, float(p_last), eps=_TP1_EPS)
                        if reached_r_level <= int(getattr(z, "last_r_followup_level", 0) or 0):
                            continue
                        if _settings_skip_trade_management(settings):
                            z.last_r_followup_level = max(
                                int(getattr(z, "last_r_followup_level", 0) or 0),
                                int(reached_r_level),
                            )
                            _state_write(params, st_r1)
                            _send_log(
                                settings,
                                f"[r1] skip TRADE_MANAGEMENT (SKIP_TRADE_MANAGEMENT) at {reached_r_level}R "
                                f"| zone_id={z.id} last={p_last}",
                            )
                            continue
                        prev_status = z.status
                        z.status = "dang_thuc_thi"
                        z.r1_followup_done = True
                        _state_write(params, st_r1)
                        _send_log(
                            settings,
                            f"[r1] dispatch {reached_r_level}R | zone_id={z.id} {prev_status}->dang_thuc_thi last={p_last}",
                        )
                        _r1_followup_job(
                            settings=settings,
                            params=params,
                            zone_id=z.id,
                            prev_status=prev_status,
                            reached_r_level=reached_r_level,
                        )

                st_tp1 = _state_read(params)
                if st_tp1 is not None:
                    changed = False
                    for z in st_tp1.zones:
                        if z.status != "vao_lenh":
                            continue
                        if not z.trade_line or not z.mt5_ticket or int(z.mt5_ticket) <= 0:
                            continue
                        if _arm_threshold_met_for_zone(z, float(p_last), symbol_override=params.mt5_symbol):
                            z.status = "cho_tp1"
                            z.tp1_followup_done = False
                            z.has_position = False
                            z.openai_manage_done = False
                            z.openai_manage_retry_at = ""
                            changed = True
                            _send_log(settings, f"[tp1] arm | zone_id={z.id} vao_lenh->cho_tp1 last={p_last}")
                            _thr_tp1 = arm_threshold_tp1_for_label(z.label or "")
                            _send_user_notice(
                                settings,
                                f"Giá đã cách entry {_thr_tp1:g} giá — sẽ xử lý khi chạm TP1",
                                zone=z,
                                params=params,
                            )
                    if changed:
                        _state_write(params, st_tp1)

                    st_tp1b = _state_read(params)
                    if st_tp1b is not None:
                        changed_has_position = False
                        accs_pos = load_mt5_accounts_for_cli(params.mt5_accounts_json)
                        prim_pos = primary_account(accs_pos) if accs_pos else None
                        for z in st_tp1b.zones:
                            if z.status != "cho_tp1":
                                continue
                            if z.tp1_followup_done:
                                continue
                            if not z.trade_line or not z.mt5_ticket or int(z.mt5_ticket) <= 0:
                                continue
                            parsed, err = _parse_trade_from_zone_trade_line(
                                z.trade_line, symbol_override=params.mt5_symbol
                            )
                            if err or parsed is None:
                                continue
                            if (
                                not bool(getattr(z, "has_position", False))
                                and _entry_touched_for_position_check(parsed, float(p_last))
                            ):
                                is_pos = False
                                msg_pos = "không có account primary để kiểm tra position"
                                if prim_pos is not None:
                                    is_pos, msg_pos = mt5_ticket_is_open_position(
                                        int(z.mt5_ticket),
                                        dry_run=bool(params.mt5_dry_run),
                                        terminal_path=prim_pos.terminal_path,
                                        login=prim_pos.login,
                                        password=prim_pos.password,
                                        server=prim_pos.server,
                                    )
                                if is_pos:
                                    z.has_position = True
                                    changed_has_position = True
                                    if _is_plan_chinh_or_phu_zone(z):
                                        z.openai_manage_retry_at = (
                                            _now_utc()
                                            + timedelta(minutes=POST_FILL_MANAGE_DELAY_MINUTES)
                                        ).isoformat()
                                        z.openai_manage_done = False
                                    _send_log(
                                        settings,
                                        f"[tp1] has_position=true | zone_id={z.id} | {msg_pos}",
                                    )
                                    _send_entry_management_notice(
                                        settings,
                                        z,
                                        f"Đã khớp lệnh {_zone_label_slot_display_vn(z, params)}.",
                                    )
                            if _should_check_managed_tp_done(z, parsed, float(p_last)) and prim_pos is not None:
                                is_pos_now, msg_pos_now = mt5_ticket_is_open_position(
                                    int(z.mt5_ticket),
                                    dry_run=bool(params.mt5_dry_run),
                                    terminal_path=prim_pos.terminal_path,
                                    login=prim_pos.login,
                                    password=prim_pos.password,
                                    server=prim_pos.server,
                                )
                                if not is_pos_now:
                                    z.status = "done"
                                    z.mt5_ticket = None
                                    z.mt5_tickets_by_account = None
                                    z.tp1_followup_done = True
                                    z.r1_followup_done = True
                                    z.has_position = False
                                    _state_write(params, st_tp1b)
                                    _send_log(
                                        settings,
                                        f"[tp1] managed TP touched + no position -> done | zone_id={z.id} | {msg_pos_now}",
                                    )
                                    continue
                            if not _tp1_touched(parsed, float(p_last)):
                                continue
                            if _settings_skip_trade_management(settings):
                                z.tp1_followup_done = True
                                _state_write(params, st_tp1b)
                                _send_log(
                                    settings,
                                    f"[tp1] skip TRADE_MANAGEMENT (SKIP_TRADE_MANAGEMENT) | zone_id={z.id} "
                                    f"chạm TP1 last={p_last}",
                                )
                                continue
                            z.status = "dang_thuc_thi"
                            z.tp1_followup_done = True
                            _state_write(params, st_tp1b)
                            _send_log(settings, f"[tp1] touched | zone_id={z.id} -> followup last={p_last}")
                            _tp1_followup_job(
                                settings=settings,
                                params=params,
                                zone_id=z.id,
                                p_last=float(p_last),
                            )
                        for z_pf in st_tp1b.zones:
                            if z_pf.status != "cho_tp1":
                                continue
                            if not bool(getattr(z_pf, "has_position", False)):
                                continue
                            if bool(getattr(z_pf, "openai_manage_done", False)):
                                continue
                            if not _is_plan_chinh_or_phu_zone(z_pf):
                                continue
                            if not z_pf.trade_line or not z_pf.mt5_ticket or int(z_pf.mt5_ticket) <= 0:
                                continue
                            if not _post_fill_manage_retry_due(z_pf):
                                continue
                            z_pf.openai_manage_done = True
                            _state_write(params, st_tp1b)
                            tk_pf = int(z_pf.mt5_ticket or 0)
                            skip_openai = False
                            if params.mt5_execute and tk_pf > 0 and prim_pos is not None:
                                is_pos_pf, msg_pf = mt5_ticket_is_open_position(
                                    tk_pf,
                                    dry_run=bool(params.mt5_dry_run),
                                    terminal_path=prim_pos.terminal_path,
                                    login=prim_pos.login,
                                    password=prim_pos.password,
                                    server=prim_pos.server,
                                )
                                if not is_pos_pf:
                                    _send_log(
                                        settings,
                                        f"[post-fill] bỏ qua OpenAI (không còn position) | "
                                        f"zone_id={z_pf.id} | {msg_pf}",
                                    )
                                    skip_openai = True
                            if not skip_openai:
                                _send_log(
                                    settings,
                                    f"[post-fill] dispatch | zone_id={z_pf.id}",
                                )
                                _post_fill_manage_job(
                                    settings=settings,
                                    params=params,
                                    zone_id=z_pf.id,
                                )
                        if changed_has_position:
                            _state_write(params, st_tp1b)

            time.sleep(poll_s)
    finally:
        pass


 


# If the parsed TV price stays unchanged for this long, treat as stale and reload the tab.
# A pure "N polls in a row" rule is too aggressive: TV often updates the tab title slower than the
# poll interval, so identical parses for a few seconds are normal, not a broken feed.
_TITLE_PRICE_STALE_MIN_SECONDS = 15.0


def _title_price_should_reload_stale(
    st: dict[str, Any],
    p: Optional[float],
) -> tuple[bool, float]:
    """
    Track whether the parsed title price has been unchanged long enough to reload.

    ``st`` holds ``last_p`` and ``since`` (monotonic time when ``last_p`` was first seen).

    Returns:
        ``(should_reload, elapsed_seconds)`` — elapsed is meaningful only when ``should_reload``.
    """
    if p is None:
        st.clear()
        return False, 0.0
    now = time.monotonic()
    lp = st.get("last_p")
    if lp is None or p != lp:
        st["last_p"] = p
        st["since"] = now
        return False, 0.0
    since = float(st["since"])
    elapsed = now - since
    if elapsed >= _TITLE_PRICE_STALE_MIN_SECONDS:
        return True, elapsed
    return False, 0.0


def _tv_watchlist_init_request_params(tv: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Payload for ``METHOD_TV_WATCHLIST_INIT`` (browser service RPC)."""
    return {
        "chart_url": str(tv.get("chart_url")),
        "symbol_page_url": str(tv.get("symbol_page_url") or ""),
        "tv": tv,
        "email": settings.coinmap_email,
        "password": settings.tradingview_password,
        "initial_settle_ms": int(tv.get("initial_settle_ms", 3000)),
    }


def _resolved_tv_symbol_page_url(tv: dict[str, Any], *, symbol: str) -> str:
    """
    Resolve TradingView symbol page URL.

    If `tradingview_capture.symbol_page_url` is set, it is used as-is.
    Otherwise, default to vn TradingView: https://vn.tradingview.com/symbols/{SYMBOL}/
    """
    s = str(tv.get("symbol_page_url") or "").strip()
    if s:
        return s
    sym = (symbol or "").strip().upper()
    if not sym:
        raise SystemExit("Cannot resolve symbol page URL: empty symbol.")
    return f"https://vn.tradingview.com/symbols/{sym}/"


def _tv_rpc_poll_symbol_last_price(
    client: BrowserClient,
    *,
    tab_id: str,
    wms: int,
) -> Optional[float]:
    timeout_rpc = max(30.0, float(wms) / 1000.0 + 10.0)
    resp = client.request(
        METHOD_QUERY_TEXT,
        {"tab_id": tab_id, "selector": _TV_SYMBOL_LAST_SELECTOR},
        timeout_s=timeout_rpc,
    )
    if not resp.get("ok"):
        _log.warning("query_text(%s) RPC failed: %s", _TV_SYMBOL_LAST_SELECTOR, resp.get("error"))
        return None
    text = (resp.get("result") or {}).get("text")
    if not isinstance(text, str):
        return None
    return parse_tv_symbol_last_value(text)


def _make_rpc_symbol_last_price_getter(
    client: BrowserClient,
    tab_id_holder: list[str],
    *,
    symbol_page_url: str,
    tv: dict[str, Any],
    settings: Settings,
) -> Callable[[int], Optional[float]]:
    """
    Poll TV symbol page Last value via RPC query_text.
    If the same parsed value persists for `_TITLE_PRICE_STALE_MIN_SECONDS`, reload by opening a new init tab.
    """
    stale_st: dict[str, Any] = {}

    def get_price(wms: int) -> Optional[float]:
        tid = tab_id_holder[0]
        p = _tv_rpc_poll_symbol_last_price(client, tab_id=tid, wms=wms)
        do_reload, elapsed = _title_price_should_reload_stale(stale_st, p)
        if not do_reload:
            return p
        _log.info(
            "tv-watchlist-daemon | symbol page last unchanged %s for %.0fs — RPC reload (new tab)",
            p,
            elapsed,
        )
        try:
            init = client.request(
                METHOD_TV_WATCHLIST_INIT,
                {
                    **_tv_watchlist_init_request_params(tv, settings),
                    "symbol_page_url": symbol_page_url,
                },
                timeout_s=600.0,
            )
            if init.get("ok"):
                new_id = str((init.get("result") or {}).get("tab_id") or "").strip()
                if new_id:
                    tab_id_holder[0] = new_id
                    try:
                        client.request(METHOD_CLOSE_TAB, {"tab_id": tid}, timeout_s=60.0)
                    except OSError:
                        pass
        except Exception as e:
            _log.warning("tv-watchlist-daemon | RPC reload symbol page failed: %s", e)
        stale_st.clear()
        p2 = _tv_rpc_poll_symbol_last_price(client, tab_id=tab_id_holder[0], wms=wms)
        _title_price_should_reload_stale(stale_st, p2)
        return p2

    return get_price


def run_tv_watchlist_daemon(
    *,
    settings: Settings,
    params: WatchlistDaemonParams,
) -> str:
    cfg = load_coinmap_yaml(params.coinmap_tv_yaml)
    tv = cfg.get("tradingview_capture") or {}
    if not isinstance(tv, dict):
        tv = {}

    poll_s = float(params.poll_seconds or 1.0)
    if poll_s <= 0:
        poll_s = 1.0

    sym = (tv.get("watchlist_symbol_short") or "").strip().upper()
    if not sym or sym == DEFAULT_MAIN_CHART_SYMBOL:
        sym = get_active_main_symbol().strip().upper()

    last_p = params.last_price_path or default_last_price_path(sym)
    _log.info(
        "tv-watchlist-daemon (gia) start | symbol=%s poll=%.1fs mirror_last_file=%s path=%s "
        "stop_plans_on_exit=%s last_price_from_mt5=%s mt5_stale_reconnect_s=%s",
        sym,
        poll_s,
        params.mirror_last_price_file,
        last_p,
        params.stop_daemon_plans_on_exit,
        params.last_price_from_mt5,
        float(params.mt5_stale_reconnect_seconds or 0.0) if params.last_price_from_mt5 else 0.0,
    )
    if params.stop_daemon_plans_on_exit:
        register_stop_daemon_plans_on_exit(default_zones_dir(sym))

    if params.last_price_from_mt5:
        mt5_sess = DaemonPlanMt5PriceSession(
            symbol_hint=sym,
            symbol_override=params.mt5_symbol,
            dry_run=bool(params.mt5_dry_run),
            accounts_json=params.mt5_accounts_json,
        )

        def get_price(_wms: int) -> Optional[float]:
            p, _err = mt5_sess.read_bid_price()
            return p

        _tv_watchlist_price_only_loop(
            settings=settings,
            params=params,
            sym=sym,
            poll_s=poll_s,
            get_price=get_price,
            price_log_source="MT5 bid",
            mt5_stale_reconnect_s=float(params.mt5_stale_reconnect_seconds or 0.0),
            mt5_sess=mt5_sess,
        )
        return "stopped"

    # RPC only for TradingView source.
    if not is_service_responding():
        raise SystemExit("browser service not responding; run: coinmap-automation browser up")

    c = BrowserClient.from_state_file()
    if not c:
        raise SystemExit("browser service state missing; run: coinmap-automation browser up")

    symbol_page_url = _resolved_tv_symbol_page_url(tv, symbol=sym)
    _log.info("tv-watchlist-daemon mode=rpc | symbol_page_url=%s", symbol_page_url)

    init = c.request(
        METHOD_TV_WATCHLIST_INIT,
        {
            **_tv_watchlist_init_request_params(tv, settings),
            "symbol_page_url": symbol_page_url,
        },
        timeout_s=600.0,
    )
    if not init.get("ok"):
        raise SystemExit(f"tv_watchlist_init failed: {init.get('error')}")
    tab_id = str((init.get("result") or {}).get("tab_id") or "").strip()
    if not tab_id:
        raise SystemExit("tv_watchlist_init: missing tab_id")
    tab_holder = [tab_id]
    get_price = _make_rpc_symbol_last_price_getter(
        c,
        tab_holder,
        symbol_page_url=symbol_page_url,
        tv=tv,
        settings=settings,
    )
    try:
        _tv_watchlist_price_only_loop(
            settings=settings,
            params=params,
            sym=sym,
            poll_s=poll_s,
            get_price=get_price,
            price_log_source='TradingView symbol page [data-qa-id="symbol-last-value"] (RPC)',
        )
    finally:
        try:
            c.request(METHOD_CLOSE_TAB, {"tab_id": tab_holder[0]}, timeout_s=60.0)
        except OSError:
            pass
    return "stopped"


def run_daemon_plan(
    *,
    settings: Settings,
    params: WatchlistDaemonParams,
) -> str:
    """
    One process per shard JSON: đọc Last (MT5 bid) từ shared memory / ``last.txt`` do daemon giá ghi.
    """
    if params.shard_path is None:
        raise SystemExit("daemon-plan requires --shard PATH (vung_*.json)")
    register_daemon_plan_pidfile_for_current_process(params.shard_path)
    poll_s = float(params.poll_seconds or 1.0)
    if poll_s <= 0:
        poll_s = 1.0
    sym = get_active_main_symbol().strip().upper()
    _log.info(
        "daemon-plan start | shard=%s poll=%.1fs symbol=%s mt5_accounts=%s",
        params.shard_path,
        poll_s,
        sym,
        params.mt5_accounts_json,
    )
    _daemon_plan_main_loop(settings=settings, params=params, sym=sym, poll_s=poll_s)
    return "stopped"

