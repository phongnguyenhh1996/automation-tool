from __future__ import annotations

import json
import logging
import math
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Literal, Optional

from playwright.sync_api import sync_playwright

from automation_tool.browser_client import BrowserClient, is_service_responding, try_attach_playwright_via_service
from automation_tool.browser_protocol import (
    METHOD_CLOSE_TAB,
    METHOD_EVAL,
    METHOD_TV_WATCHLIST_INIT,
    METHOD_TV_WATCHLIST_POLL,
)
from automation_tool.coinmap import (
    _maybe_tradingview_dark_mode,
    _maybe_tradingview_login,
    load_coinmap_yaml,
)
from automation_tool.coinmap import _tradingview_ensure_watchlist_open  # reuse internal helper
from automation_tool.coinmap_merged import write_openai_coinmap_merged_from_raw_export
from automation_tool.config import Settings, resolved_model_for_intraday_alert
from automation_tool.images import (
    DEFAULT_MAIN_CHART_SYMBOL,
    coinmap_main_pair_interval_json_path,
    coinmap_xauusd_5m_json_path,
    get_active_main_symbol,
    read_main_chart_symbol,
)
from automation_tool.mt5_accounts import MT5AccountEntry, load_mt5_accounts_for_cli, primary_account
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
)
from automation_tool.mt5_openai_parse import (
    is_last_price_hit_stop_loss,
    parse_journal_intraday_action_from_openai_text,
    parse_openai_output_md,
)
from automation_tool.mt5_manage import (
    mt5_cancel_pending_or_close_position,
    mt5_cancel_pending_order,
    mt5_chinh_trade_line_inplace,
    mt5_ticket_is_open_position,
    mt5_ticket_still_open,
    mt5_ticket_status_for_cutoff,
)
from automation_tool.openai_errors import re_raise_unless_openai
from automation_tool.openai_prompt_flow import (
    R1_POST_TOUCH_USER_TEMPLATE,
    TP1_POST_TOUCH_USER_TEMPLATE,
    run_single_followup_responses,
)
from automation_tool.zone_one_r import (
    entry_reference_price as _zone_one_r_entry_ref,
    one_r_favorable_price as _zone_one_r_target_price,
    one_r_reached,
)
from automation_tool.playwright_browser import close_browser_and_context, launch_chrome_context
from automation_tool.state_files import read_last_response_id, write_last_response_id
from automation_tool.telegram_bot import (
    mt5_zone_label_display_vn,
    send_message,
    send_mt5_execution_log_to_ngan_gon_chat,
    send_openai_output_to_telegram,
    send_phan_tich_alert_to_python_bot_if_any,
    send_user_friendly_notice,
)
from automation_tool.openai_analysis_json import (
    ARM_THRESHOLD_TP1_DEFAULT,
    arm_threshold_tp1_for_label,
    auto_mt5_hop_luu_threshold_for_label,
    parse_vung_cho_bounds,
)
from automation_tool.daemon_launcher import (
    reconcile_daemon_plans_at_boot,
    register_daemon_plan_pidfile_for_current_process,
    register_stop_daemon_plans_on_exit,
)
from automation_tool.last_price_ipc import (
    open_writer_shared_memory,
    read_last_price_for_daemon_plan,
    write_last_price_shared,
)
from automation_tool.zones_paths import (
    SessionSlot,
    default_last_price_path,
    default_zones_dir,
    label_from_shard_stem,
    read_last_price_file,
    resolve_session_slot_raw,
    session_slot_display_vn,
    write_last_price_file,
)
from automation_tool.zones_state import (
    Zone,
    ZonesState,
    read_manifest_last_write_slot,
    read_zones_state,
    read_zones_state_from_shard,
    write_zones_state,
    write_zones_state_to_shard,
)

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

# After integer rounding of Last vs zone touch ref (from vung_cho + side): touch if abs(diff) <= this (0 = exact match).
_EPS_DEFAULT = 0.0
_TP1_EPS = 0.01
# Re-export default cho test (plan_chinh / plan_phu).
_ARM_THRESHOLD = ARM_THRESHOLD_TP1_DEFAULT
_RETRY_WAIT_MINUTES = 15
_RETRY_WAIT_MINUTES_SCALP = 10


def _is_scalp_zone(zone: Zone) -> bool:
    return (zone.label or "").strip().lower() == "scalp"


def _zone_touch_retry_wait_minutes(zone: Zone) -> int:
    """Chạm vùng scalp: gửi lại Coinmap + OpenAI mỗi 10 phút; plan khác: 15 phút."""
    return _RETRY_WAIT_MINUTES_SCALP if _is_scalp_zone(zone) else _RETRY_WAIT_MINUTES


def _zone_touch_coinmap_main_json_path(charts_dir: Path, zone: Zone) -> tuple[Optional[Path], str]:
    """Scalp: JSON M1; plan_chinh / plan_phu: M5. Trả về (path, suffix log như ``1m`` / ``5m``)."""
    if _is_scalp_zone(zone):
        p = coinmap_main_pair_interval_json_path(charts_dir, "1m")
        return p, "1m"
    p = coinmap_xauusd_5m_json_path(charts_dir)
    return p, "5m"


# Daemon-plan: Last chạm SL theo trade_line → loại (vùng chờ/chạm hoặc đã vào lệnh / chờ TP1).
_DAEMON_PLAN_SL_LOAI_STATUSES = frozenset({"vung_cho", "cham", "vao_lenh", "cho_tp1"})


_TV_TITLE_PRICE_RE = re.compile(r"^\s*(?P<sym>[A-Z0-9:_-]+)\s+(?P<price>\d[\d,]*(?:\.\d+)?)\b")


def _price_round_nearest_int(v: object) -> float:
    """
    Normalize price by rounding to the nearest whole number (integer), returned as float.
    Used for zone touch: compare Last vs side ref (BUY=max, SELL=min from ``vung_cho``) after this
    rounding; touch if ``abs(last_int - ref_int) <= eps`` (default eps=0: exact integer match only).
    """
    d = Decimal(str(v)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(d)


def _zone_side_ref_from_vung_cho(zone: Zone) -> Optional[float]:
    """
    Parse ``zone.vung_cho`` into (lo, hi); BUY uses max(hi), SELL uses min(lo) — same ref for touch and arm.
    """
    lo, hi = parse_vung_cho_bounds(zone.vung_cho)
    if lo is None or hi is None:
        return None
    side = (zone.side or "").strip().upper()
    if side == "SELL":
        return float(lo)
    return float(hi)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _retry_at_iso(minutes: int = _RETRY_WAIT_MINUTES) -> str:
    return (_now_utc() + timedelta(minutes=int(minutes))).isoformat()


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
    eps: float = _EPS_DEFAULT  # max |Δ| between integer-rounded Last and touch ref (default 0.0)
    openai_model: Optional[str] = None
    openai_model_cli: Optional[str] = None
    mt5_accounts_json: Optional[Path] = None
    stop_at_hour: Optional[int] = 0
    """0 + phút 0 = 12h đêm (00:00 ngày kế, local); 1-23 = mốc cùng ngày; ``None`` = không cắt giờ."""
    stop_at_minute: int = 0
    """Phút đi kèm ``stop_at_hour`` (mặc định 0)."""
    last_price_from_mt5: bool = True
    """Daemon giá: ``True`` = đọc MT5 bid → shared memory; ``False`` = title TradingView (legacy)."""
    mt5_stale_reconnect_seconds: float = 60.0
    """Bid không đổi trong khoảng này (giây) thì ``shutdown``/``initialize`` lại MT5; ``0`` = tắt."""


def _daemon_gia_same_bid(a: float, b: float) -> bool:
    """So sánh bid liên tiếp (tránh float noise)."""
    return abs(float(a) - float(b)) <= 1e-5


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
    except Exception:
        # Never let logging break the daemon.
        return


def _user_notice_plan_slot_tag(
    *,
    zone: Optional[Zone] = None,
    params: Optional[WatchlistDaemonParams] = None,
    zone_label: Optional[str] = None,
) -> str:
    """
    Tiền tố hiển thị plan + khung giờ, ví dụ ``(Plan chính - Sáng)`` / ``(Scalp - Chiều)``.
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

    slot_vn = session_slot_display_vn(slot_raw) if slot_raw else None
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
    lead = (
        "Đánh giá sau khi đã chạm vùng trước đó.\n"
        if after_retry_wait
        else ""
    )
    return (
        "[INTRADAY_ALERT]\n"
        f"{lead}"
        f"Vùng chờ {zone.vung_cho}.\n"
        f"Giá hiện tại (MT5): {last_price}.\n"
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
    if not is_last_price_hit_stop_loss(float(p_last), parsed, eps=_TP1_EPS):
        return False
    prev_status = zone.status
    zone.status = "loai"
    zone.loai_streak = 0
    _send_log(
        settings,
        f"[sl-hit] last={p_last} touched SL -> loai | zone_id={zone.id} label={zone.label} "
        f"sl={parsed.sl} side={parsed.side}",
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
        f"Loại vùng: giá chạm SL ({parsed.sl}).",
        detail,
        zone=zone,
        params=params,
    )
    return True


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

        # Scalp: chạm TP1 → huỷ ticket ngay, không gọi OpenAI / Coinmap.
        if (z0.label or "").strip().lower() == "scalp":
            tk = tk_check
            if exe and tk > 0:
                r = mt5_cancel_pending_or_close_position(tk, dry_run=dry)
                _send_log(settings, f"[tp1] scalp chạm TP1 — mt5_cancel_close: {r.message}".strip())
                if not params.no_telegram and settings.telegram_bot_token:
                    send_mt5_execution_log_to_ngan_gon_chat(
                        bot_token=settings.telegram_bot_token,
                        telegram_chat_id=settings.telegram_chat_id,
                        telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
                        telegram_log_chat_id=settings.telegram_log_chat_id,
                        source="tp1-scalp-tp1",
                        text=f"scalp: chạm TP1 — huỷ ticket\n{r.message}",
                        zone_label="scalp",
                        trade_line=z0.trade_line,
                        execution_ok=r.ok,
                        session_slot=resolve_session_slot_raw(
                            zone_session_slot=getattr(z0, "session_slot", None),
                            shard_path=params.shard_path,
                        ),
                    )
            z0.status = "loai"
            z0.mt5_ticket = None
            z0.tp1_followup_done = True
            z0.r1_followup_done = True
            _state_write(params, st0)
            _send_user_notice(
                settings,
                "Scalp chạm TP1 — đã huỷ lệnh (không gọi AI).",
                "Vùng scalp chuyển trạng thái loại.",
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

        prev = _openai_followup_prev_response_id(params)
        user_text = TP1_POST_TOUCH_USER_TEMPLATE.format(
            plan_label=z0.label,
            trade_line=z0.trade_line,
            last_price=p_last,
            tp1_price=getattr(parsed, "tp1", ""),
        )
        out_text, new_id = run_single_followup_responses(
            api_key=settings.openai_api_key,
            prompt_id=settings.openai_prompt_id,
            prompt_version=settings.openai_prompt_version,
            user_text=user_text,
            coinmap_json_paths=[json_path],
            previous_response_id=prev or "",
            vector_store_ids=settings.openai_vector_store_ids,
            store=settings.openai_responses_store,
            include=settings.openai_responses_include,
            model=resolved_model_for_intraday_alert(settings, params.openai_model_cli),
            reasoning_effort="high",
        )
        _openai_followup_persist_new_id(params, new_id)
        if not params.no_telegram:
            send_openai_output_to_telegram(
                bot_token=settings.telegram_bot_token,
                chat_id=settings.telegram_chat_id,
                raw=out_text,
                default_parse_mode=settings.telegram_parse_mode,
                summary_chat_id=settings.telegram_output_ngan_gon_chat_id,
            )
        _send_log(settings, f"[tp1] openai_output_raw:\n{out_text}".strip())

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

        if dec.sau_tp1 == "giu_nguyen":
            z1.status = "vao_lenh"
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
                    r = mt5_cancel_pending_or_close_position(tk, dry_run=dry)
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
        if not dec.trade_line_moi.strip():
            z1.tp1_followup_done = False
            z1.status = "cho_tp1"
            _state_write(params, st1)
            return

        minimal = json.dumps(
            {"intraday_hanh_dong": "VÀO LỆNH", "trade_line": dec.trade_line_moi.strip()},
            ensure_ascii=False,
        )
        new_parsed, err2 = parse_openai_output_md(minimal, symbol_override=params.mt5_symbol)
        if err2 or new_parsed is None:
            z1.tp1_followup_done = False
            z1.status = "cho_tp1"
            _state_write(params, st1)
            return

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
                            trade_line=dec.trade_line_moi.strip(),
                            execution_ok=True,
                            session_slot=resolve_session_slot_raw(
                                zone_session_slot=getattr(z1, "session_slot", None),
                                shard_path=params.shard_path,
                            ),
                        )
                elif ch_s.all_ticket_missing():
                    pass
                else:
                    r0m = mt5_cancel_pending_or_close_all_accounts(
                        tmap_old, accs_mt5, dry_run=dry
                    )
                    _send_log(
                        settings,
                        f"[tp1] mt5_close_old multi: {format_mt5_multi_manage_for_telegram(r0m)}".strip(),
                    )
            else:
                prim = primary_account(accs_mt5) if accs_mt5 else None
                cr = mt5_chinh_trade_line_inplace(
                    tk,
                    new_parsed,
                    dry_run=dry,
                    symbol_override=params.mt5_symbol,
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
                            trade_line=dec.trade_line_moi.strip(),
                            execution_ok=True,
                            session_slot=resolve_session_slot_raw(
                                zone_session_slot=getattr(z1, "session_slot", None),
                                shard_path=params.shard_path,
                            ),
                        )
                elif cr.outcome == "ticket_missing":
                    pass
                else:
                    r0 = mt5_cancel_pending_or_close_position(tk, dry_run=dry)
                    _send_log(settings, f"[tp1] mt5_close_old: {r0.message}".strip())

        if exe and not used_inplace:
            if accs_mt5:
                summary = execute_trade_all_accounts(
                    new_parsed,
                    accs_mt5,
                    dry_run=dry,
                    symbol_override=params.mt5_symbol,
                )
                multi_txt = format_mt5_multi_for_telegram(summary)
                if not params.no_telegram:
                    send_mt5_execution_log_to_ngan_gon_chat(
                        bot_token=settings.telegram_bot_token,
                        telegram_chat_id=settings.telegram_chat_id,
                        telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
                        telegram_log_chat_id=settings.telegram_log_chat_id,
                        source="tp1-followup",
                        text=multi_txt,
                        zone_label=z1.label,
                        trade_line=dec.trade_line_moi.strip(),
                        execution_ok=summary.ok_all,
                        session_slot=resolve_session_slot_raw(
                            zone_session_slot=getattr(z1, "session_slot", None),
                            shard_path=params.shard_path,
                        ),
                    )
                _send_log(settings, f"[tp1] mt5_execute_trade multi: {multi_txt[:500]}".strip())
                tid = summary.primary_ticket(accs_mt5)
                if tid > 0:
                    z1.mt5_ticket = tid
                    z1.mt5_tickets_by_account = summary.tickets_by_account_id or None
                _tp1_lines = [multi_txt]
                if dry:
                    _tp1_lines.append("(Chế độ thử.)")
            else:
                ex = execute_trade(
                    new_parsed,
                    dry_run=dry,
                    symbol_override=params.mt5_symbol,
                )
                if not params.no_telegram:
                    send_mt5_execution_log_to_ngan_gon_chat(
                        bot_token=settings.telegram_bot_token,
                        telegram_chat_id=settings.telegram_chat_id,
                        telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
                        telegram_log_chat_id=settings.telegram_log_chat_id,
                        source="tp1-followup",
                        text=format_mt5_execution_for_telegram(ex),
                        zone_label=z1.label,
                        trade_line=dec.trade_line_moi.strip(),
                        execution_ok=ex.ok,
                        session_slot=resolve_session_slot_raw(
                            zone_session_slot=getattr(z1, "session_slot", None),
                            shard_path=params.shard_path,
                        ),
                    )
                _send_log(settings, f"[tp1] mt5_execute_trade: {ex.message}".strip())
                tid = int(ex.order) if ex.order else 0
                if ex.ok and tid > 0:
                    z1.mt5_ticket = tid
                z1.mt5_tickets_by_account = None
                _tp1_lines = [ex.message]
                if ex.order:
                    _tp1_lines.append(f"Mã lệnh: {ex.order}")
                if dry:
                    _tp1_lines.append("(Chế độ thử.)")
            _send_user_notice(
                settings,
                "Sau TP1: đã đặt lệnh mới theo trade line cập nhật.",
                "\n".join(_tp1_lines),
                zone=z1,
                params=params,
            )
        elif exe and used_inplace:
            _send_user_notice(
                settings,
                "Sau TP1: đã cập nhật lệnh tại chỗ (SL/TP hoặc sửa lệnh chờ).",
                "Không đóng + mở mới; ticket giữ nguyên.",
                zone=z1,
                params=params,
            )
        z1.trade_line = dec.trade_line_moi.strip()
        z1.status = "vao_lenh"
        z1.tp1_followup_done = False
        z1.r1_followup_done = False
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
) -> None:
    """
    Follow-up khi giá đạt +1R (zones daemon): Coinmap M5 + [TRADE_MANAGEMENT] / Schema D — cùng parser
    ``parse_tp1_followup_decision`` như sau TP1.
    """
    from automation_tool.coinmap import capture_charts
    from automation_tool.images import coinmap_xauusd_5m_json_path, read_main_chart_symbol
    from automation_tool.tp1_followup import parse_tp1_followup_decision

    try:
        st0 = _state_read(params)
        if st0 is None:
            return
        z0 = next((z for z in st0.zones if z.id == zone_id), None)
        if z0 is None:
            return
        if z0.status in ("done", "loai"):
            return
        if not z0.trade_line or not z0.mt5_ticket:
            z0.status = prev_status
            z0.r1_followup_done = False
            _state_write(params, st0)
            return

        parsed, err = _parse_trade_from_zone_trade_line(z0.trade_line, symbol_override=params.mt5_symbol)
        if err or parsed is None:
            z0.status = prev_status
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
                login=prim_chk.login if prim_chk else None,
                password=prim_chk.password if prim_chk else None,
                server=prim_chk.server if prim_chk else None,
            )
            _send_log(settings, f"[r1] kiểm tra ticket | {ticket_msg}")
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
                    f"[r1] bỏ qua follow-up 1R (ticket đã đóng trên MT5) | zone_id={zone_id} | {ticket_msg}",
                )
                return

        _send_user_notice(
            settings,
            "Giá đã đạt mức 1R.",
            "Đang lấy biểu đồ M5 và hỏi AI quản lý lệnh.",
            zone=z0,
            params=params,
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

        prev = _openai_followup_prev_response_id(params)
        tl0 = (z0.trade_line or "").strip()
        snip = (tl0[:200] + "…") if len(tl0) > 200 else tl0
        eref = _zone_one_r_entry_ref(parsed)
        r1p = _zone_one_r_target_price(parsed)
        user_text = R1_POST_TOUCH_USER_TEMPLATE.format(
            plan_label=z0.label,
            entry_ref=eref,
            r1_price=r1p,
            trade_line_snip=snip,
        )
        out_text, new_id = run_single_followup_responses(
            api_key=settings.openai_api_key,
            prompt_id=settings.openai_prompt_id,
            prompt_version=settings.openai_prompt_version,
            user_text=user_text,
            coinmap_json_paths=[json_path],
            previous_response_id=prev or "",
            vector_store_ids=settings.openai_vector_store_ids,
            store=settings.openai_responses_store,
            include=settings.openai_responses_include,
            model=resolved_model_for_intraday_alert(settings, params.openai_model_cli),
            reasoning_effort="high",
        )
        _openai_followup_persist_new_id(params, new_id)
        if not params.no_telegram:
            send_openai_output_to_telegram(
                bot_token=settings.telegram_bot_token,
                chat_id=settings.telegram_chat_id,
                raw=out_text,
                default_parse_mode=settings.telegram_parse_mode,
                summary_chat_id=settings.telegram_output_ngan_gon_chat_id,
            )
        _send_log(settings, f"[r1] openai_output_raw:\n{out_text}".strip())

        dec = parse_tp1_followup_decision(out_text)
        st1 = _state_read(params)
        if st1 is None:
            return
        z1 = next((z for z in st1.zones if z.id == zone_id), None)
        if z1 is None:
            return

        if dec is None:
            z1.r1_followup_done = False
            z1.status = prev_status
            _state_write(params, st1)
            _send_user_notice(
                settings,
                "Tại 1R: không đọc được quyết định từ AI.",
                "Hệ thống sẽ thử lại — xem log kỹ thuật nếu cần chi tiết.",
                zone=z1,
                params=params,
            )
            return

        tk = int(z1.mt5_ticket or 0)
        dry = bool(params.mt5_dry_run)
        exe = bool(params.mt5_execute)

        if dec.sau_tp1 == "giu_nguyen":
            z1.status = prev_status  # type: ignore[assignment]
            _state_write(params, st1)
            _send_user_notice(
                settings,
                "Tại 1R: AI chọn «giữ nguyên» — không đổi lệnh.",
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
                    r = mt5_cancel_pending_or_close_position(tk, dry_run=dry)
                    _send_log(settings, f"[r1] mt5_cancel_close: {r.message}".strip())
            z1.status = "loai"
            z1.mt5_tickets_by_account = None
            _state_write(params, st1)
            _send_user_notice(
                settings,
                "Tại 1R: AI chọn «loại» — đóng / bỏ theo dõi vùng.",
                "Đã gửi lệnh đóng trên MT5 nếu bật thực thi.",
                zone=z1,
                params=params,
            )
            return

        if not dec.trade_line_moi.strip():
            z1.r1_followup_done = False
            z1.status = prev_status  # type: ignore[assignment]
            _state_write(params, st1)
            return

        minimal = json.dumps(
            {"intraday_hanh_dong": "VÀO LỆNH", "trade_line": dec.trade_line_moi.strip()},
            ensure_ascii=False,
        )
        new_parsed, err2 = parse_openai_output_md(minimal, symbol_override=params.mt5_symbol)
        if err2 or new_parsed is None:
            z1.r1_followup_done = False
            z1.status = prev_status  # type: ignore[assignment]
            _state_write(params, st1)
            return

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
                            trade_line=dec.trade_line_moi.strip(),
                            execution_ok=True,
                            session_slot=resolve_session_slot_raw(
                                zone_session_slot=getattr(z1, "session_slot", None),
                                shard_path=params.shard_path,
                            ),
                        )
                elif ch_s.all_ticket_missing():
                    pass
                else:
                    r0m = mt5_cancel_pending_or_close_all_accounts(
                        tmap_r1, accs_r1, dry_run=dry
                    )
                    _send_log(
                        settings,
                        f"[r1] mt5_close_old multi: {format_mt5_multi_manage_for_telegram(r0m)}".strip(),
                    )
            else:
                prim_r = primary_account(accs_r1) if accs_r1 else None
                cr = mt5_chinh_trade_line_inplace(
                    tk,
                    new_parsed,
                    dry_run=dry,
                    symbol_override=params.mt5_symbol,
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
                            trade_line=dec.trade_line_moi.strip(),
                            execution_ok=True,
                            session_slot=resolve_session_slot_raw(
                                zone_session_slot=getattr(z1, "session_slot", None),
                                shard_path=params.shard_path,
                            ),
                        )
                elif cr.outcome == "ticket_missing":
                    pass
                else:
                    r0 = mt5_cancel_pending_or_close_position(tk, dry_run=dry)
                    _send_log(settings, f"[r1] mt5_close_old: {r0.message}".strip())

        if exe and not used_inplace_r1:
            if accs_r1:
                summary = execute_trade_all_accounts(
                    new_parsed,
                    accs_r1,
                    dry_run=dry,
                    symbol_override=params.mt5_symbol,
                )
                multi_txt = format_mt5_multi_for_telegram(summary)
                if not params.no_telegram:
                    send_mt5_execution_log_to_ngan_gon_chat(
                        bot_token=settings.telegram_bot_token,
                        telegram_chat_id=settings.telegram_chat_id,
                        telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
                        telegram_log_chat_id=settings.telegram_log_chat_id,
                        source="r1-followup",
                        text=multi_txt,
                        zone_label=z1.label,
                        trade_line=dec.trade_line_moi.strip(),
                        execution_ok=summary.ok_all,
                        session_slot=resolve_session_slot_raw(
                            zone_session_slot=getattr(z1, "session_slot", None),
                            shard_path=params.shard_path,
                        ),
                    )
                _send_log(settings, f"[r1] mt5_execute_trade multi: {multi_txt[:500]}".strip())
                tid = summary.primary_ticket(accs_r1)
                if tid > 0:
                    z1.mt5_ticket = tid
                    z1.mt5_tickets_by_account = summary.tickets_by_account_id or None
                _r1_lines = [multi_txt]
                if dry:
                    _r1_lines.append("(Chế độ thử.)")
            else:
                ex = execute_trade(
                    new_parsed,
                    dry_run=dry,
                    symbol_override=params.mt5_symbol,
                )
                if not params.no_telegram:
                    send_mt5_execution_log_to_ngan_gon_chat(
                        bot_token=settings.telegram_bot_token,
                        telegram_chat_id=settings.telegram_chat_id,
                        telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
                        telegram_log_chat_id=settings.telegram_log_chat_id,
                        source="r1-followup",
                        text=format_mt5_execution_for_telegram(ex),
                        zone_label=z1.label,
                        trade_line=dec.trade_line_moi.strip(),
                        execution_ok=ex.ok,
                        session_slot=resolve_session_slot_raw(
                            zone_session_slot=getattr(z1, "session_slot", None),
                            shard_path=params.shard_path,
                        ),
                    )
                _send_log(settings, f"[r1] mt5_execute_trade: {ex.message}".strip())
                tid = int(ex.order) if ex.order else 0
                if ex.ok and tid > 0:
                    z1.mt5_ticket = tid
                z1.mt5_tickets_by_account = None
                _r1_lines = [ex.message]
                if ex.order:
                    _r1_lines.append(f"Mã lệnh: {ex.order}")
                if dry:
                    _r1_lines.append("(Chế độ thử.)")
            _send_user_notice(
                settings,
                "Tại 1R: đã đặt lệnh mới theo trade line cập nhật.",
                "\n".join(_r1_lines),
                zone=z1,
                params=params,
            )
        elif exe and used_inplace_r1:
            _send_user_notice(
                settings,
                "Tại 1R: đã cập nhật lệnh tại chỗ (SL/TP hoặc sửa lệnh chờ).",
                "Không đóng + mở mới; ticket giữ nguyên.",
                zone=z1,
                params=params,
            )
        z1.trade_line = dec.trade_line_moi.strip()
        z1.status = "vao_lenh"
        z1.tp1_followup_done = False
        z1.r1_followup_done = False
        _state_write(params, st1)
        return
    except Exception as e:
        _send_log(settings, f"[r1] ERROR | zone_id={zone_id} | {e!s}")
        _send_user_notice(settings, "Lỗi khi xử lý bước tại 1R.", str(e), params=params)
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
        if int(z0.hop_luu) < thr:
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

        accs_ae = load_mt5_accounts_for_cli(params.mt5_accounts_json)
        if accs_ae:
            summary_ae = execute_trade_all_accounts(
                parsed,
                accs_ae,
                dry_run=params.mt5_dry_run,
                symbol_override=params.mt5_symbol,
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
                )
            _send_log(settings, f"[auto-entry] mt5_execute_trade multi: {multi_ae[:400]}".strip())
            tid = summary_ae.primary_ticket(accs_ae)
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
    (scalp ~10 phút + Coinmap M1; plan khác ~15 phút + M5), khác với lần chạm đầu từ ``vung_cho``.
    """
    st0 = _state_read(params)
    if st0 is None:
        return
    zone = next((z for z in st0.zones if z.id == zone_id), None)
    if zone is None:
        return

    try:
        ref = _zone_side_ref_from_vung_cho(zone)
        _send_log(
            settings,
            f"[zone-touch] start | zone_id={zone_id} label={zone.label} "
            f"vung_cho={zone.vung_cho} ref={ref} last={last_price}",
        )
        side_vn = "mua" if (zone.side or "").strip().upper() == "BUY" else "bán"
        _rw_m = _zone_touch_retry_wait_minutes(zone)
        _chart_tf = "M1" if _is_scalp_zone(zone) else "M5"
        _touch_title = (
            f"Sau {_rw_m}p giá chạm vùng chờ."
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

        loai_confirm_rounds = 6

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

        # Mark running (anti-spam + visibility). Daemon will handle retries using retry_at.
        zone.status = "dang_thuc_thi"
        zone.retry_at = ""
        _state_write(params, st_check)

        # Capture Coinmap (reuse capture pipeline; scalp → đọc M1, còn lại M5)
        from automation_tool.coinmap import capture_charts

        _touch_iv = ("1m",) if _is_scalp_zone(zone) else ("5m",)
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
            prompt_id=settings.openai_prompt_id,
            prompt_version=settings.openai_prompt_version,
            user_text=user_text,
            coinmap_json_paths=[openai_merged],
            previous_response_id=prev or "",
            vector_store_ids=settings.openai_vector_store_ids,
            store=settings.openai_responses_store,
            include=settings.openai_responses_include,
            model=resolved_model_for_intraday_alert(settings, params.openai_model_cli),
            reasoning_effort="high",
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
            z1.loai_streak = int(getattr(z1, "loai_streak", 0) or 0) + 1
            if z1.loai_streak >= loai_confirm_rounds:
                z1.status = "loai"
                z1.retry_at = ""
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
            z1.status = "cham"
            z1.retry_at = _retry_at_iso(_zone_touch_retry_wait_minutes(z1))
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
            z1.retry_at = _retry_at_iso(_zone_touch_retry_wait_minutes(z1))
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
            z1.retry_at = _retry_at_iso(_zone_touch_retry_wait_minutes(z1))
            _state_write(params, st1)
            _send_log(
                settings,
                f"[zone-touch] parse_trade_line_failed | err={err} | zone_id={zone_id} -> status=cham",
            )
            return

        z1.trade_line = (parsed.raw_line or "").strip()
        z1.status = "vao_lenh"
        z1.tp1_followup_done = False
        z1.r1_followup_done = False
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

        accs_zt = load_mt5_accounts_for_cli(params.mt5_accounts_json)
        if accs_zt:
            summary_zt = execute_trade_all_accounts(
                parsed,
                accs_zt,
                dry_run=params.mt5_dry_run,
                symbol_override=params.mt5_symbol,
            )
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
                )
            _send_log(settings, f"[zone-touch] mt5_execute_trade multi: {zt_txt[:400]}".strip())
            tid = summary_zt.primary_ticket(accs_zt)
            if tid > 0:
                st2 = _state_read(params)
                if st2 is None:
                    return
                for z in st2.zones:
                    if z.id == zone_id:
                        z.mt5_ticket = tid
                        z.mt5_tickets_by_account = summary_zt.tickets_by_account_id or None
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
        )
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
        # On any error: keep touched state (no revert to vung_cho); daemon will retry using retry_at
        try:
            stx = _state_read(params)
            if stx is not None:
                for z in stx.zones:
                    if z.id == zone_id:
                        z.status = "cham"
                        z.retry_at = _retry_at_iso(_zone_touch_retry_wait_minutes(z))
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
        re_raise_unless_openai(e, exit_on_openai=False, settings=settings)


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
    shm = open_writer_shared_memory(sym)
    zones_dir = default_zones_dir(sym)
    prev_manifest_slot: Optional[SessionSlot] = None
    reconciled_after_first_last = False
    heartbeat_s = 300.0
    last_heartbeat_at = 0.0
    telegram_log_interval_s = 60.0
    last_telegram_log_at = 0.0
    stale_s = float(mt5_stale_reconnect_s or 0.0)
    last_stale_bid: Optional[float] = None
    last_stale_change_mono = time.monotonic()
    try:
        while True:
            try:
                cur_manifest_slot = read_manifest_last_write_slot(zones_dir)
                if cur_manifest_slot is not None:
                    if prev_manifest_slot is None:
                        prev_manifest_slot = cur_manifest_slot
                    elif cur_manifest_slot != prev_manifest_slot:
                        _log.info(
                            "tv-watchlist-daemon (gia) | zones_manifest last_write_slot %s -> %s | "
                            "reconcile-daemon-plans (no stop)",
                            prev_manifest_slot,
                            cur_manifest_slot,
                        )
                        n_rec = reconcile_daemon_plans_at_boot(zones_dir)
                        _log.info(
                            "tv-watchlist-daemon (gia) | reconcile-daemon-plans spawned %s process(es)",
                            n_rec,
                        )
                        prev_manifest_slot = cur_manifest_slot
            except Exception as e:
                _log.warning(
                    "tv-watchlist-daemon (gia) | last_write_slot watch / reconcile-daemon-plans: %s",
                    e,
                )

            wms = min(15_000, max(2_000, int(poll_s * 1000)))
            p_last = get_price(wms)
            if stale_s > 0 and mt5_sess is not None:
                if p_last is None:
                    last_stale_bid = None
                else:
                    now_m = time.monotonic()
                    pl = float(p_last)
                    if last_stale_bid is None:
                        last_stale_bid = pl
                        last_stale_change_mono = now_m
                    elif not _daemon_gia_same_bid(pl, last_stale_bid):
                        last_stale_bid = pl
                        last_stale_change_mono = now_m
                    elif (now_m - last_stale_change_mono) >= stale_s:
                        _log.warning(
                            "tv-watchlist-daemon (gia) | bid %.5f unchanged for >= %.0fs — MT5 reconnect",
                            last_stale_bid,
                            stale_s,
                        )
                        if mt5_sess.reconnect():
                            last_stale_change_mono = time.monotonic()
                            last_stale_bid = None
                            p_last = get_price(wms)
                        else:
                            _log.warning(
                                "tv-watchlist-daemon (gia) | MT5 reconnect failed: %s",
                                mt5_sess.last_error,
                            )
                            last_stale_change_mono = time.monotonic()
            if p_last is not None:
                write_last_price_shared(shm, float(p_last))
                if params.mirror_last_price_file:
                    write_last_price_file(float(p_last), last_path)
                if not reconciled_after_first_last:
                    try:
                        n = reconcile_daemon_plans_at_boot(zones_dir)
                        _log.info(
                            "tv-watchlist-daemon (gia) | reconcile-daemon-plans after first last "
                            "spawned %s process(es)",
                            n,
                        )
                        reconciled_after_first_last = True
                        if prev_manifest_slot is None:
                            prev_manifest_slot = read_manifest_last_write_slot(zones_dir)
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
                        f"[daemon-gia] last (shared memory) <- {p_last} | symbol={sym} | source={price_log_source}{mirror}",
                    )
                _poll_terminal.info(
                    "tv-watchlist-daemon (gia) | symbol=%s | last=%s | source=%s",
                    sym,
                    p_last,
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
    if params.stop_at_hour is not None:
        tz_name = (params.timezone_name or "Asia/Ho_Chi_Minh").strip() or "Asia/Ho_Chi_Minh"
        sh = int(params.stop_at_hour)
        sm = int(params.stop_at_minute or 0)
        started = datetime.now(ZoneInfo(tz_name))
        stop_deadline = compute_daemon_plan_stop_deadline_local(started, tz_name, sh, sm)
        if sh == 0 and sm == 0:
            cut_desc = f"12h đêm (00:00 ngày kế, {tz_name}) | mốc={stop_deadline.strftime('%Y-%m-%d %H:%M')}"
        else:
            cut_desc = (
                f"dừng khi ≥ {sh:02d}:{sm:02d} ({tz_name}) | mốc cùng ngày={stop_deadline.strftime('%Y-%m-%d %H:%M')}"
            )
        _send_log(
            settings,
            f"[daemon-plan] cắt giờ | {cut_desc} (pending → huỷ; chỉ chờ khi còn position đã khớp)",
        )
    try:
        while True:
            st = _state_read(params)
            if stop_deadline is not None:
                tz_nm = (params.timezone_name or "Asia/Ho_Chi_Minh").strip() or "Asia/Ho_Chi_Minh"
                now_local = datetime.now(ZoneInfo(tz_nm))
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
                    _send_user_notice(
                        settings,
                        "Đã ngưng theo dõi.",
                        f"Lý do: quá giờ cắt — {detail}",
                        zone=st.zones[0] if st is not None and st.zones else None,
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

            p_last = read_last_price_for_daemon_plan(sym, last_price_file)
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
            if p_last is None:
                _poll_terminal.info(
                    "daemon-plan | tick | sym=%s | zone_id=%s | last=(none) | file=%s",
                    sym,
                    z0.id,
                    last_price_file,
                )
                time.sleep(poll_s)
                continue

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
                    if int(z.hop_luu) < thr:
                        continue
                    if getattr(z, "auto_entry_mt5_failed", False):
                        continue
                    aer = (getattr(z, "auto_entry_retry_after", "") or "").strip()
                    if aer and not _is_retry_due(aer):
                        continue
                    z.status = "dang_vao_lenh"
                    z.auto_entry_retry_after = ""
                    _state_write(params, st_auto)
                    _send_log(
                        settings,
                        f"[auto-entry] dispatch | zone_id={z.id} label={z.label} hop_luu={z.hop_luu} thr(>=)={thr}",
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
                time.sleep(poll_s)
                continue

            matched: list[Zone] = []
            for z in st.zones:
                if z.status != "vung_cho":
                    continue
                ref = _zone_side_ref_from_vung_cho(z)
                if ref is None:
                    continue
                try:
                    p_last_n = _price_round_nearest_int(p_last)
                    ref_n = _price_round_nearest_int(ref)
                except Exception:
                    p_last_n = float(p_last)
                    ref_n = float(ref)
                if abs(p_last_n - ref_n) <= float(params.eps):
                    matched.append(z)

            for z in matched:
                z.status = "cham"
                z.auto_entry_mt5_failed = False
                _state_write(params, st)
                _zone_touch_job(
                    settings=settings,
                    params=params,
                    zone_id=z.id,
                    last_price=float(p_last),
                )

            st_r1 = _state_read(params)
            if st_r1 is not None:
                for z in st_r1.zones:
                    if z.status != "cho_tp1":
                        continue
                    if z.r1_followup_done:
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
                    if not one_r_reached(parsed_r1, float(p_last), eps=_TP1_EPS):
                        continue
                    tk_r1 = int(z.mt5_ticket or 0)
                    accs_r1_chk = load_mt5_accounts_for_cli(params.mt5_accounts_json)
                    prim_r1_chk = primary_account(accs_r1_chk) if accs_r1_chk else None
                    is_pos_r1, pos_msg_r1 = mt5_ticket_is_open_position(
                        tk_r1,
                        dry_run=bool(params.mt5_dry_run),
                        login=prim_r1_chk.login if prim_r1_chk else None,
                        password=prim_r1_chk.password if prim_r1_chk else None,
                        server=prim_r1_chk.server if prim_r1_chk else None,
                    )
                    if not is_pos_r1:
                        _poll_terminal.info(
                            "daemon-plan | shard=%s | r1 skip (need open position) | zone_id=%s | %s",
                            shard_tag,
                            z.id,
                            pos_msg_r1,
                        )
                        continue
                    prev_status = z.status
                    z.status = "dang_thuc_thi"
                    z.r1_followup_done = True
                    _state_write(params, st_r1)
                    _send_log(
                        settings,
                        f"[r1] dispatch | zone_id={z.id} {prev_status}->dang_thuc_thi last={p_last}",
                    )
                    _r1_followup_job(
                        settings=settings,
                        params=params,
                        zone_id=z.id,
                        prev_status=prev_status,
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
                        if not _tp1_touched(parsed, float(p_last)):
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

            time.sleep(poll_s)
    finally:
        pass


def _tv_watchlist_rpc_poll_price(
    client: BrowserClient,
    *,
    tab_id: str,
    tv: dict[str, Any],
    sym: str,
    wms: int,
) -> Optional[float]:
    # Backward-compat: old watchlist polling (kept for reference).
    # The daemon now uses chart tab `document.title` as the source of truth.
    return None


def _parse_price_from_tv_title(title: str, *, sym: str) -> Optional[float]:
    """
    Title example: "XAUUSD 4,755.145 ▼ −0.22% Vô danh"
    We parse the first number after the symbol.
    """
    t = (title or "").strip()
    if not t:
        return None
    m = _TV_TITLE_PRICE_RE.match(t)
    if not m:
        return None
    sym_in_title = str(m.group("sym") or "").strip().upper()
    if sym_in_title != str(sym or "").strip().upper():
        return None
    raw = str(m.group("price") or "").replace(",", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


# If ``document.title`` parses to the same price for this long, treat as stale and reload the chart tab.
# A pure "N polls in a row" rule is too aggressive: TV often updates the tab title slower than the
# poll interval, so identical parses for a few seconds are normal, not a broken feed.
_TITLE_PRICE_STALE_MIN_SECONDS = 30.0


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
        "tv": tv,
        "email": settings.coinmap_email,
        "password": settings.tradingview_password,
        "initial_settle_ms": int(tv.get("initial_settle_ms", 3000)),
    }


def _prepare_tv_chart_page(page: Any, tv: dict[str, Any], settings: Settings) -> None:
    """Open chart URL, login, settle, dark mode, watchlist — shared by initial load and stale-title reload."""
    chart_url = str(tv.get("chart_url") or "").strip()
    if not chart_url:
        raise SystemExit("tradingview_capture.chart_url missing in coinmap yaml.")
    page.goto(chart_url, wait_until="domcontentloaded", timeout=120_000)
    _maybe_tradingview_login(page, tv, settings.coinmap_email, settings.tradingview_password)
    page.wait_for_timeout(int(tv.get("initial_settle_ms", 3000)))
    _maybe_tradingview_dark_mode(page, tv)
    _tradingview_ensure_watchlist_open(page, tv)


def _make_playwright_title_price_getter(
    page: Any,
    *,
    sym: str,
    tv: dict[str, Any],
    settings: Settings,
) -> Callable[[int], Optional[float]]:
    """
    Poll ``page.title()`` → parse price. If the same value persists for
    ``_TITLE_PRICE_STALE_MIN_SECONDS``, run ``_prepare_tv_chart_page`` and re-parse.
    """

    stale_st: dict[str, Any] = {}

    def get_price(_wms: int) -> Optional[float]:
        p = _parse_price_from_tv_title(page.title(), sym=sym)
        do_reload, elapsed = _title_price_should_reload_stale(stale_st, p)
        if not do_reload:
            return p
        _log.info(
            "tv-watchlist-daemon | title price unchanged %s for %.0fs — reloading chart",
            p,
            elapsed,
        )
        try:
            _prepare_tv_chart_page(page, tv, settings)
        except Exception as e:
            _log.warning("tv-watchlist-daemon | reload chart failed: %s", e)
        stale_st.clear()
        p = _parse_price_from_tv_title(page.title(), sym=sym)
        _title_price_should_reload_stale(stale_st, p)
        return p

    return get_price


def _tv_rpc_poll_title_price(
    client: BrowserClient,
    *,
    tab_id: str,
    sym: str,
    wms: int,
) -> Optional[float]:
    timeout_rpc = max(30.0, float(wms) / 1000.0 + 10.0)
    resp = client.request(
        METHOD_EVAL,
        {"tab_id": tab_id, "script": "() => document.title", "arg": None},
        timeout_s=timeout_rpc,
    )
    if not resp.get("ok"):
        _log.warning("eval(document.title) RPC failed: %s", resp.get("error"))
        return None
    title = (resp.get("result") or {}).get("value")
    if not isinstance(title, str):
        return None
    return _parse_price_from_tv_title(title, sym=sym)


def _rpc_replace_chart_tab(
    client: BrowserClient,
    *,
    old_tab_id: str,
    tv: dict[str, Any],
    settings: Settings,
) -> str:
    """
    Open a fresh chart via ``tv_watchlist_init``, then close ``old_tab_id``.
    Creates the new tab first so a failed init leaves the previous tab usable.
    """
    init = client.request(
        METHOD_TV_WATCHLIST_INIT,
        _tv_watchlist_init_request_params(tv, settings),
        timeout_s=600.0,
    )
    if not init.get("ok"):
        raise RuntimeError(str(init.get("error") or "tv_watchlist_init failed"))
    new_id = str((init.get("result") or {}).get("tab_id") or "").strip()
    if not new_id:
        raise RuntimeError("tv_watchlist_init: missing tab_id")
    try:
        client.request(METHOD_CLOSE_TAB, {"tab_id": old_tab_id}, timeout_s=60.0)
    except OSError:
        pass
    return new_id


def _make_rpc_title_price_getter(
    client: BrowserClient,
    tab_id_holder: list[str],
    *,
    sym: str,
    tv: dict[str, Any],
    settings: Settings,
) -> Callable[[int], Optional[float]]:
    """
    Same stale-title logic as ``_make_playwright_title_price_getter`` but reloads by RPC:
    new ``tv_watchlist_init`` tab, then close the old tab.
    ``tab_id_holder`` is a single-element list updated when the tab is replaced.
    """

    stale_st: dict[str, Any] = {}

    def get_price(wms: int) -> Optional[float]:
        tid = tab_id_holder[0]
        p = _tv_rpc_poll_title_price(client, tab_id=tid, sym=sym, wms=wms)
        do_reload, elapsed = _title_price_should_reload_stale(stale_st, p)
        if not do_reload:
            return p
        _log.info(
            "tv-watchlist-daemon | title price unchanged %s for %.0fs — RPC reload chart (new tab)",
            p,
            elapsed,
        )
        try:
            tab_id_holder[0] = _rpc_replace_chart_tab(
                client,
                old_tab_id=tid,
                tv=tv,
                settings=settings,
            )
        except Exception as e:
            _log.warning("tv-watchlist-daemon | RPC reload chart failed: %s", e)
        stale_st.clear()
        p = _tv_rpc_poll_title_price(
            client, tab_id=tab_id_holder[0], sym=sym, wms=wms
        )
        _title_price_should_reload_stale(stale_st, p)
        return p

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
        )
        try:

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
        finally:
            mt5_sess.shutdown()
        return "stopped"

    if not tv.get("chart_url"):
        raise SystemExit(
            "tradingview_capture.chart_url missing in coinmap yaml "
            "(bật daemon giá chỉ MT5: mặc định last_price_from_mt5=True; "
            "hoặc thêm chart_url nếu dùng --tv-title-price)."
        )

    # Browser service up: drive TradingView entirely via RPC (no second CDP client in this process).
    if is_service_responding():
        c = BrowserClient.from_state_file()
        if not c:
            raise SystemExit("browser service state missing; run: coinmap-automation browser up")
        _log.info("tv-watchlist-daemon mode=rpc | no local Playwright CDP attach")
        init = c.request(
            METHOD_TV_WATCHLIST_INIT,
            _tv_watchlist_init_request_params(tv, settings),
            timeout_s=600.0,
        )
        if not init.get("ok"):
            raise SystemExit(f"tv_watchlist_init failed: {init.get('error')}")
        tab_id = str((init.get("result") or {}).get("tab_id") or "").strip()
        if not tab_id:
            raise SystemExit("tv_watchlist_init: missing tab_id")
        tab_holder = [tab_id]
        get_price = _make_rpc_title_price_getter(
            c, tab_holder, sym=sym, tv=tv, settings=settings
        )
        try:
            _tv_watchlist_price_only_loop(
                settings=settings,
                params=params,
                sym=sym,
                poll_s=poll_s,
                get_price=get_price,
                price_log_source="TradingView title (RPC)",
            )
        finally:
            try:
                c.request(METHOD_CLOSE_TAB, {"tab_id": tab_holder[0]}, timeout_s=60.0)
            except OSError:
                pass
        return "stopped"

    with sync_playwright() as p:
        _log.info("tv-watchlist-daemon playwright ready | attempting_attach_via_service=true")
        attached = try_attach_playwright_via_service(p)
        if attached is not None:
            browser, context = attached
            use_browser_service = True
            _log.info("tv-watchlist-daemon using_browser_service=true (local CDP; rare if service down)")
        else:
            _log.info("tv-watchlist-daemon using_browser_service=false | fallback=launch_chrome_context")
            browser, context = launch_chrome_context(
                p,
                headless=params.headless,
                storage_state_path=params.storage_state_path,
                viewport_width=int(cfg.get("viewport_width", 1920)),
                viewport_height=int(cfg.get("viewport_height", 1080)),
            )
            use_browser_service = False
        page = context.new_page()
        try:
            _prepare_tv_chart_page(page, tv, settings)
            get_price = _make_playwright_title_price_getter(page, sym=sym, tv=tv, settings=settings)

            _tv_watchlist_price_only_loop(
                settings=settings,
                params=params,
                sym=sym,
                poll_s=poll_s,
                get_price=get_price,
                price_log_source="TradingView title (Playwright)",
            )
        finally:
            try:
                page.close()
            except Exception:
                pass
            if use_browser_service:
                try:
                    browser.close()
                except Exception:
                    pass
            else:
                close_browser_and_context(browser, context)
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

