"""Theo dõi sau vào lệnh: last-ref trong dải arm theo plan (mặc định ±3 giá; scalp ±1) → ``cho_tp1``; chạm TP1 → Coinmap M5 + OpenAI."""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Optional

from playwright.sync_api import BrowserContext, Page

from automation_tool.coinmap import capture_charts
from automation_tool.coinmap_merged import write_openai_coinmap_merged_from_raw_export
from automation_tool.config import Settings
from automation_tool.images import coinmap_xauusd_5m_json_path, read_main_chart_symbol
from automation_tool.mt5_accounts import (
    load_mt5_accounts_for_cli,
    primary_account,
    primary_account_id,
)
from automation_tool.mt5_manage import (
    mt5_cancel_pending_or_close_position,
    mt5_chinh_trade_line_inplace,
    mt5_close_position_partial,
    mt5_ticket_current_sltp,
    mt5_ticket_is_open_position,
    mt5_ticket_still_open,
)
from automation_tool.mt5_multi import (
    format_mt5_multi_chinh_for_telegram,
    format_mt5_multi_manage_for_telegram,
    mt5_cancel_pending_or_close_all_accounts,
    mt5_chinh_trade_line_all_accounts,
    mt5_partial_close_tp1_all_accounts,
)
from automation_tool.mt5_openai_parse import ParsedTrade, parse_openai_output_md
from automation_tool.openai_analysis_json import arm_threshold_tp1_for_label
from automation_tool.openai_prompt_flow import (
    TP1_POST_TOUCH_USER_TEMPLATE,
    run_single_followup_responses,
)
from automation_tool.state_files import (
    CHO_TP1,
    LOAI,
    VAO_LENH,
    clear_plan_mt5_fields,
    mt5_primary_ticket_for_label,
    read_last_alert_state,
    update_plan_mt5_entry,
    update_plan_tp1_followup_done,
    update_single_plan_status,
)
from automation_tool.telegram_bot import (
    send_mt5_execution_log_to_ngan_gon_chat,
    send_trade_management_reason_notice,
)

_log = logging.getLogger(__name__)
# Log có cấu trúc cho TELEGRAM_LOG_CHAT_ID (propagate → automation_tool)
_log_tp1 = logging.getLogger("automation_tool.tp1")

_tp1_lock = threading.Lock()

# Cùng epsilon touch vùng chờ
_EPS = 0.01


@dataclass
class TP1FollowupDecision:
    sau_tp1: Literal["loại", "chinh_trade_line", "giu_nguyen"]
    new_sl: Optional[float]
    new_tp: Optional[float]
    reason: str
    out_chi_tiet: str
    output_ngan_gon: str


def _entry_reference_price(parsed: ParsedTrade) -> float:
    if parsed.kind == "MARKET" or parsed.price is None:
        return (float(parsed.sl) + float(parsed.tp1)) / 2.0
    return float(parsed.price)


def _arm_threshold_met(parsed: ParsedTrade, p_last: float, *, label: str) -> bool:
    """BUY: 0 ≤ last−ref ≤ thr; SELL: −thr ≤ last−ref ≤ 0 (thr theo label, scalp hẹp hơn)."""
    thr = arm_threshold_tp1_for_label(label)
    ref = _entry_reference_price(parsed)
    diff = float(p_last) - ref
    if parsed.side == "BUY":
        return 0.0 <= diff <= thr
    return -thr <= diff <= 0.0


def _tp1_touched(parsed: ParsedTrade, p_last: float) -> bool:
    tp = float(parsed.tp1)
    if parsed.side == "BUY":
        return p_last >= tp - _EPS
    return p_last <= tp + _EPS


def _extract_tp1_json(text: str) -> Optional[dict[str, Any]]:
    t = (text or "").strip()
    if not t:
        return None

    # If OpenAI output was split into several batches, it may emit multiple JSON objects
    # separated by "\n\n---\n\n". Merge in order so later keys can refine earlier ones.
    segments = [s.strip() for s in t.split("\n\n---\n\n") if s.strip()]

    merged: dict[str, Any] = {}
    found_any = False

    def _maybe_merge_from_blob(blob: str) -> None:
        nonlocal merged, found_any
        for m in re.finditer(r"\{[\s\S]*\}", blob):
            try:
                d = json.loads(m.group(0))
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict):
                continue
            if "hanh_dong_quan_ly_lenh" in d or "sau_tp1_hanh_dong" in d:
                merged.update(d)
                found_any = True

    for seg in segments:
        _maybe_merge_from_blob(seg)

    if found_any:
        return merged
    return None


def parse_tp1_followup_decision(text: str) -> Optional[TP1FollowupDecision]:
    raw = _extract_tp1_json(text)
    if raw is None:
        return None
    raw_action = raw.get("hanh_dong_quan_ly_lenh")
    if raw_action is None or (isinstance(raw_action, str) and not str(raw_action).strip()):
        raw_action = raw.get("sau_tp1_hanh_dong")
    sp = str(raw_action or "").strip().lower()
    if sp in ("loại", "loai"):
        sau = "loại"
    elif sp in ("chinh_trade_line", "chỉnh_trade_line", "chinh_sua", "chỉnh"):
        sau = "chinh_trade_line"
    elif sp in ("giu_nguyen", "giữ_nguyên", "giu nguyen"):
        sau = "giu_nguyen"
    else:
        return None
    def _num_or_none(v: Any) -> Optional[float]:
        try:
            if v is None:
                return None
            s = str(v).strip()
            if not s:
                return None
            return float(s)
        except (TypeError, ValueError):
            return None

    new_sl = _num_or_none(raw.get("new_SL"))
    if new_sl is None:
        new_sl = _num_or_none(raw.get("new_sl"))
    new_tp = _num_or_none(raw.get("new_TP"))
    if new_tp is None:
        new_tp = _num_or_none(raw.get("new_tp"))
    reason = str(raw.get("reason") or "").strip()
    oct = str(raw.get("out_chi_tiet") or "").strip()
    ogn = str(raw.get("output_ngan_gon") or "").strip()
    return TP1FollowupDecision(
        sau_tp1=sau,
        new_sl=new_sl,
        new_tp=new_tp,
        reason=reason,
        out_chi_tiet=oct,
        output_ngan_gon=ogn,
    )


def extract_trade_management_reason(text: str) -> Optional[str]:
    """Trích ``reason`` từ output Schema D — không bắt buộc ``hanh_dong_quan_ly_lenh``."""
    t = (text or "").strip()
    if not t:
        return None
    segments = [s.strip() for s in t.split("\n\n---\n\n") if s.strip()]
    if not segments:
        segments = [t]
    for seg in segments:
        for m in re.finditer(r"\{[\s\S]*\}", seg):
            try:
                d = json.loads(m.group(0))
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict):
                continue
            reason = str(d.get("reason") or "").strip()
            if reason:
                return reason
    return None


def _fmt_level_for_prompt(v: Optional[float]) -> str:
    if v is None:
        return "(không có)"
    return f"{v:g}"


def _partial_close_tp1_runner_before_openai(
    *,
    settings: Settings,
    params: Any,
    label: str,
    trade_line: str,
    parsed: ParsedTrade,
    ticket: int,
    ticket_by_account: dict[str, int],
    accounts: list[Any],
    symbol_override: Optional[str],
) -> bool:
    """Nếu trade có TP2, chốt 50% tại TP1 trước khi hỏi OpenAI quản lý runner."""
    if parsed.tp2 is None:
        return True

    dry = bool(getattr(params, "mt5_dry_run", False))
    exe = getattr(params, "mt5_execute", True)
    if not exe:
        _log_tp1.info(
            "tp1 partial-close: bỏ qua vì mt5_execute=false | label=%s ticket=%s",
            label,
            ticket,
        )
        return True

    if accounts and ticket_by_account:
        summ = mt5_partial_close_tp1_all_accounts(
            ticket_by_account,
            accounts,
            parsed,
            dry_run=dry,
            symbol_override=symbol_override,
        )
        msg = format_mt5_multi_manage_for_telegram(summ)
        ok = summ.ok_all
    else:
        prim = primary_account(accounts) if accounts else None
        if int(ticket) > 0 and prim is not None:
            is_pos, pos_msg = mt5_ticket_is_open_position(
                int(ticket),
                dry_run=dry,
                terminal_path=prim.terminal_path,
                login=prim.login,
                password=prim.password,
                server=prim.server,
            )
            if not is_pos:
                _log_tp1.info(
                    "tp1 partial-close: bỏ qua vì chưa có position | label=%s ticket=%s | %s",
                    label,
                    ticket,
                    pos_msg,
                )
                return True
        r = mt5_close_position_partial(
            int(ticket),
            fraction=0.5,
            # Không truyền expected_initial_volume — luôn lấy volume thực từ position trên MT5.
            dry_run=dry,
            terminal_path=prim.terminal_path if prim else None,
            login=prim.login if prim else None,
            password=prim.password if prim else None,
            server=prim.server if prim else None,
        )
        msg = r.message
        ok = r.ok

    _log_tp1.info(
        "tp1 partial-close trước OpenAI | label=%s ok=%s | %s",
        label,
        ok,
        msg,
    )
    if settings.telegram_bot_token and not getattr(params, "no_telegram", False):
        send_mt5_execution_log_to_ngan_gon_chat(
            bot_token=settings.telegram_bot_token,
            telegram_chat_id=settings.telegram_chat_id,
            telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
            telegram_log_chat_id=settings.telegram_log_chat_id,
            source="tp1-partial-close",
            text=f"{label}: chạm TP1 + có TP2 — chốt 50% trước khi hỏi AI\n{msg}",
            zone_label=label,
            trade_line=trade_line,
            execution_ok=ok,
            action="partial_close",
        )
    return ok


def _run_tp1_openai_and_act(
    *,
    settings: Settings,
    params: Any,
    last_alert_path: Path,
    label: str,
    trade_line: str,
    p_last: float,
    parsed: ParsedTrade,
    page: Page,
    tv: dict[str, Any],
    symbol: str,
    settle_ms: int,
    browser_context: BrowserContext,
    prev_response_id: str,
) -> Optional[str]:
    if getattr(settings, "skip_trade_management", False) is True:
        _log_tp1.info(
            "tp1-followup: bỏ qua TRADE_MANAGEMENT (SKIP_TRADE_MANAGEMENT) | label=%s",
            label,
        )
        return None
    capture_yaml = params.capture_coinmap_yaml
    charts_dir = params.charts_dir
    storage = params.storage_state_path
    headless = params.headless
    no_save = params.no_save_storage

    st0 = read_last_alert_state(last_alert_path)
    accounts0 = load_mt5_accounts_for_cli(getattr(params, "mt5_accounts_json", None))
    pid0 = primary_account_id(accounts0) if accounts0 else None
    tk0 = (
        int(mt5_primary_ticket_for_label(st0, label, pid0) or 0)
        if st0
        else 0
    )
    dry = bool(getattr(params, "mt5_dry_run", False))
    exe = getattr(params, "mt5_execute", True)
    if exe and tk0 > 0:
        prim0 = primary_account(accounts0) if accounts0 else None
        still_open, ticket_msg = mt5_ticket_still_open(
            tk0,
            dry_run=dry,
            terminal_path=prim0.terminal_path if prim0 else None,
            login=prim0.login if prim0 else None,
            password=prim0.password if prim0 else None,
            server=prim0.server if prim0 else None,
        )
        _log_tp1.info("tp1-followup kiểm tra ticket | %s", ticket_msg)
        if not still_open:
            _log.info(
                "tp1-followup bỏ qua (ticket không còn trên MT5) | label=%s | %s",
                label,
                ticket_msg,
            )
            update_single_plan_status(label, LOAI, path=last_alert_path)
            clear_plan_mt5_fields(label, path=last_alert_path)
            update_plan_tp1_followup_done(label, False, path=last_alert_path)
            return None
    _log_tp1.info(
        "tp1-followup bắt đầu | label=%s symbol=%s last=%.5f tp1=%.5f side=%s ticket=%s | chart_dir=%s",
        label,
        symbol,
        p_last,
        float(parsed.tp1),
        parsed.side,
        tk0,
        charts_dir,
    )
    _log_tp1.info(
        "tp1-followup trade_line (rút): %s",
        (trade_line[:200] + "…") if len(trade_line) > 200 else trade_line,
    )

    paths = capture_charts(
        coinmap_yaml=capture_yaml,
        charts_dir=charts_dir,
        storage_state_path=storage,
        email=settings.coinmap_email,
        password=settings.coinmap_password,
        tradingview_password=settings.tradingview_password,
        save_storage_state=not no_save,
        headless=headless,
        reuse_browser_context=browser_context,
        main_chart_symbol=read_main_chart_symbol(charts_dir),
        coinmap_capture_intervals=("5m",),
    )
    _log.info("tp1-followup: capture_charts → %d file(s)", len(paths))
    json_path = coinmap_xauusd_5m_json_path(charts_dir)
    if json_path is None or not json_path.is_file():
        raise SystemExit(f"tp1-followup: no main 5m Coinmap JSON under {charts_dir}")
    _log_tp1.info("tp1-followup Coinmap M5 JSON: %s", json_path)
    openai_merged = write_openai_coinmap_merged_from_raw_export(json_path)

    current_sl: Optional[float] = None
    current_tp: Optional[float] = None
    if tk0 > 0 and accounts0:
        prim_for_sltp = primary_account(accounts0)
        current_sl, current_tp, sltp_msg = mt5_ticket_current_sltp(
            tk0,
            dry_run=dry,
            terminal_path=prim_for_sltp.terminal_path,
            login=prim_for_sltp.login,
            password=prim_for_sltp.password,
            server=prim_for_sltp.server,
        )
        _log_tp1.info("tp1-followup đọc SL/TP hiện tại | %s", sltp_msg)

    user_msg = TP1_POST_TOUCH_USER_TEMPLATE.format(
        plan_label=label,
        entry_side=str(getattr(parsed, "side", "") or "").upper(),
        entry_price=_fmt_level_for_prompt(parsed.price),
        current_sl=_fmt_level_for_prompt(current_sl),
        current_tp=_fmt_level_for_prompt(current_tp),
    )
    out_text, new_id = run_single_followup_responses(
        api_key=settings.openai_api_key,
        user_text=user_msg,
        coinmap_json_paths=[openai_merged],
        previous_response_id=prev_response_id,
        vector_store_ids=settings.openai_vector_store_ids,
        store=settings.openai_responses_store,
        include=settings.openai_responses_include,
        # Force config for [TRADE_MANAGEMENT]
        model="gpt-5.4-mini",
        reasoning_summary="auto",
        reasoning_effort="medium",
    )
    update_plan_tp1_followup_done(label, True, path=last_alert_path)
    _log_tp1.info(
        "tp1-followup OpenAI xong | response_id=%s | độ dài output=%d | gửi Telegram raw=%s",
        new_id,
        len(out_text or ""),
        "không (ẩn raw JSON TRADE_MANAGEMENT)",
    )

    dec = parse_tp1_followup_decision(out_text)
    if dec is None:
        _log.warning(
            "tp1-followup: không parse được hanh_dong_quan_ly_lenh — bỏ qua hành động MT5."
        )
        _log_tp1.warning(
            "tp1-followup: không parse JSON hanh_dong_quan_ly_lenh từ output model"
        )
        update_plan_tp1_followup_done(label, False, path=last_alert_path)
        return new_id

    st = read_last_alert_state(last_alert_path)
    accounts = load_mt5_accounts_for_cli(getattr(params, "mt5_accounts_json", None))
    pid = primary_account_id(accounts) if accounts else None
    tk = int(mt5_primary_ticket_for_label(st, label, pid) or 0) if st else 0
    _log_tp1.info(
        "tp1-followup parse OK | sau_tp1=%s | mt5_execute=%s mt5_dry_run=%s | new_SL=%s new_TP=%s",
        dec.sau_tp1,
        exe,
        dry,
        dec.new_sl,
        dec.new_tp,
    )
    if settings.telegram_bot_token and not getattr(params, "no_telegram", False):
        send_trade_management_reason_notice(
            bot_token=settings.telegram_bot_token,
            telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
            zone_label=label,
            session_slot=None,
            action=dec.sau_tp1,
            reason=dec.reason,
            trade_line=None,
        )

    if dec.sau_tp1 == "loại":
        if exe and tk > 0:
            tmap = (st.mt5_tickets_by_label.get(label) or {}) if st else {}
            if accounts and tmap:
                summ = mt5_cancel_pending_or_close_all_accounts(tmap, accounts, dry_run=dry)
                log_txt = format_mt5_multi_manage_for_telegram(summ)
                _log.info("tp1-followup loại multi: %s", log_txt)
                r_ok = summ.ok_all
            else:
                prim_cancel = primary_account(accounts) if accounts else None
                r = mt5_cancel_pending_or_close_position(
                    int(tk),
                    dry_run=dry,
                    terminal_path=prim_cancel.terminal_path if prim_cancel else None,
                    login=prim_cancel.login if prim_cancel else None,
                    password=prim_cancel.password if prim_cancel else None,
                    server=prim_cancel.server if prim_cancel else None,
                )
                log_txt = r.message
                _log.info("tp1-followup loại: %s", r.message)
                r_ok = r.ok
            if settings.telegram_bot_token and not getattr(params, "no_telegram", False):
                send_mt5_execution_log_to_ngan_gon_chat(
                    bot_token=settings.telegram_bot_token,
                    telegram_chat_id=settings.telegram_chat_id,
                    telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
                    telegram_log_chat_id=settings.telegram_log_chat_id,
                    source="tp1-followup",
                    text=f"{label}: loại sau TP1\n{log_txt}",
                    trade_line=(st.trade_line_by_label.get(label) or "") if st else None,
                    execution_ok=r_ok,
                )
        update_single_plan_status(label, LOAI, path=last_alert_path)
        clear_plan_mt5_fields(label, path=last_alert_path)
        update_plan_tp1_followup_done(label, False, path=last_alert_path)
        _log_tp1.info("tp1-followup kết thúc nhánh loại | label=%s → status=loai", label)
        return new_id

    if dec.sau_tp1 == "giu_nguyen":
        update_plan_tp1_followup_done(label, False, path=last_alert_path)
        _log_tp1.info("tp1-followup kết thúc nhánh giữ nguyên | label=%s", label)
        return new_id

    # chinh_trade_line
    if dec.new_sl is None and dec.new_tp is None:
        _log.warning("tp1-followup: chinh_trade_line nhưng thiếu cả new_SL/new_TP.")
        update_plan_tp1_followup_done(label, False, path=last_alert_path)
        return new_id

    next_sl = float(dec.new_sl) if dec.new_sl is not None else float(parsed.sl)
    if dec.new_tp is not None:
        new_parsed = replace(parsed, sl=next_sl, tp1=float(dec.new_tp), tp2=None)
    else:
        new_parsed = replace(parsed, sl=next_sl)

    _log_tp1.info(
        "tp1-followup chinh_trade_line → áp dụng inplace new_SL=%s new_TP=%s",
        dec.new_sl,
        dec.new_tp,
    )
    sym_ov = getattr(params, "mt5_symbol", None)
    used_inplace = False
    if exe and tk > 0:
        tmap_old = (st.mt5_tickets_by_label.get(label) or {}) if st else {}
        if accounts and tmap_old:
            ch_s = mt5_chinh_trade_line_all_accounts(
                tmap_old,
                accounts,
                new_parsed,
                dry_run=dry,
                symbol_override=sym_ov,
            )
            ch_txt = format_mt5_multi_chinh_for_telegram(ch_s)
            _log.info("tp1-followup: chinh_trade_line (thử SLTP/modify): %s", ch_txt)
            if ch_s.ok_all_inplace:
                used_inplace = True
                if settings.telegram_bot_token and not getattr(params, "no_telegram", False):
                    send_mt5_execution_log_to_ngan_gon_chat(
                        bot_token=settings.telegram_bot_token,
                        telegram_chat_id=settings.telegram_chat_id,
                        telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
                        telegram_log_chat_id=settings.telegram_log_chat_id,
                        source="tp1-followup-chinh",
                        text=ch_txt,
                        zone_label=label,
                        trade_line=st.trade_line_by_label.get(label) if st else trade_line,
                        previous_trade_line=(
                            (st.trade_line_by_label.get(label) or trade_line) if st else trade_line
                        ),
                        execution_ok=True,
                        action="chinh_trade_line",
                    )
            else:
                _log.info(
                    "tp1-followup: chinh_trade_line thất bại (multi) — giữ nguyên lệnh cũ, không đặt mới: %s",
                    ch_txt,
                )
        else:
            prim = primary_account(accounts) if accounts else None
            cr = mt5_chinh_trade_line_inplace(
                int(tk),
                new_parsed,
                dry_run=dry,
                symbol_override=sym_ov,
                terminal_path=prim.terminal_path if prim else None,
                login=prim.login if prim else None,
                password=prim.password if prim else None,
                server=prim.server if prim else None,
            )
            _log.info("tp1-followup: chinh_trade_line (thử SLTP/modify): %s", cr.message)
            if cr.ok and cr.outcome in ("modified_sltp", "modified_pending", "dry_run"):
                used_inplace = True
                if settings.telegram_bot_token and not getattr(params, "no_telegram", False):
                    send_mt5_execution_log_to_ngan_gon_chat(
                        bot_token=settings.telegram_bot_token,
                        telegram_chat_id=settings.telegram_chat_id,
                        telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
                        telegram_log_chat_id=settings.telegram_log_chat_id,
                        source="tp1-followup-chinh",
                        text=cr.message,
                        zone_label=label,
                        trade_line=st.trade_line_by_label.get(label) if st else trade_line,
                        previous_trade_line=(
                            (st.trade_line_by_label.get(label) or trade_line) if st else trade_line
                        ),
                        execution_ok=True,
                        action="chinh_trade_line",
                    )
            else:
                _log.info(
                    "tp1-followup: chinh_trade_line thất bại — giữ nguyên lệnh cũ, không đặt mới: %s",
                    cr.message,
                )

    if exe and not used_inplace:
        update_plan_tp1_followup_done(label, False, path=last_alert_path)
        _log_tp1.info(
            "tp1-followup kết thúc nhánh chỉnh trade_line | label=%s → chỉnh thất bại, giữ nguyên ticket/trade_line cũ",
            label,
        )
        return new_id
    if exe and used_inplace and st is not None:
        tmap_keep = st.mt5_tickets_by_label.get(label)
        update_plan_mt5_entry(
            label,
            trade_line=st.trade_line_by_label.get(label) or trade_line,
            mt5_ticket=tk,
            mt5_tickets_by_account=dict(tmap_keep) if tmap_keep else None,
            path=last_alert_path,
        )
    update_plan_tp1_followup_done(label, False, path=last_alert_path)
    _log_tp1.info(
        "tp1-followup kết thúc nhánh chỉnh trade_line | label=%s → giữ nguyên status, chỉ cập nhật trade_line/ticket",
        label,
    )
    return new_id


def maybe_post_entry_tp1_tick(
    *,
    settings: Settings,
    params: Any,
    last_alert_path: Path,
    page: Page,
    tv: dict[str, Any],
    symbol: str,
    settle_ms: int,
    p_last: float,
    browser_context: BrowserContext,
    initial_response_id: str,
    tick_source: str = "monitor",
) -> Optional[str]:
    """
    Một tick: ``vao_lenh`` → ``cho_tp1`` nếu đạt dải arm theo plan; ``cho_tp1`` + chạm TP1 → follow-up OpenAI.

    Trả về ``response_id`` mới nếu đã gọi OpenAI (để caller cập nhật thread); ngược lại ``None``.
    """
    with _tp1_lock:
        st = read_last_alert_state(last_alert_path)
        if st is None:
            _log_tp1.warning("tp1 tick: không đọc được state | path=%s", last_alert_path)
            return None

        mt5_sym = getattr(params, "mt5_symbol", None)
        rid_preview = initial_response_id
        if len(rid_preview) > 28:
            rid_preview = rid_preview[:24] + "…"
        _log_tp1.info(
            "tp1 tick [%s] | symbol=%s last=%.5f mt5_symbol=%s | last_alert=%s | prev_response=%s",
            tick_source,
            symbol,
            p_last,
            mt5_sym or "(từ lệnh)",
            last_alert_path,
            rid_preview,
        )

        arm_action = False
        accounts_m = load_mt5_accounts_for_cli(getattr(params, "mt5_accounts_json", None))
        pid_m = primary_account_id(accounts_m) if accounts_m else None
        for lab in st.labels:
            if st.status_by_label.get(lab, "") != VAO_LENH:
                continue
            tl = (st.trade_line_by_label.get(lab) or "").strip()
            tk = mt5_primary_ticket_for_label(st, lab, pid_m)
            if not tl or tk is None or int(tk) <= 0:
                _log_tp1.info(
                    "tp1 arm: bỏ qua %s — thiếu trade_line hoặc ticket (tl=%s tk=%s)",
                    lab,
                    "có" if tl else "không",
                    tk,
                )
                continue
            minimal = json.dumps(
                {"intraday_hanh_dong": "VÀO LỆNH", "trade_line": tl},
                ensure_ascii=False,
            )
            parsed, err = parse_openai_output_md(
                minimal,
                symbol_override=mt5_sym,
            )
            if err or parsed is None:
                _log_tp1.info(
                    "tp1 arm: không parse trade_line | label=%s err=%s | dòng (200 ký tự đầu): %s",
                    lab,
                    err,
                    (tl[:200] + "…") if len(tl) > 200 else tl,
                )
                continue
            ref = _entry_reference_price(parsed)
            diff = float(p_last) - ref
            thr = arm_threshold_tp1_for_label(lab)
            band = (
                f"0≤last-ref≤{thr:g}"
                if parsed.side == "BUY"
                else f"-{thr:g}≤last-ref≤0"
            )
            met = _arm_threshold_met(parsed, p_last, label=lab)
            _log_tp1.info(
                "tp1 arm: %s | side=%s entry_ref=%.5f | last-ref=%.5f (%s) | last=%.5f → %s",
                lab,
                parsed.side,
                ref,
                diff,
                band,
                p_last,
                "đạt → cho_tp1" if met else "chưa đạt",
            )
            if met:
                arm_action = True
                _log.info("tp1: %s vao_lenh → cho_tp1 (last=%s)", lab, p_last)
                update_single_plan_status(lab, CHO_TP1, path=last_alert_path)
                update_plan_tp1_followup_done(lab, False, path=last_alert_path)

        st = read_last_alert_state(last_alert_path)
        if st is None:
            return None

        rid = initial_response_id
        rid_show = rid if len(rid) <= 32 else rid[:28] + "…"
        _log_tp1.info("tp1 tick: thread OpenAI (cho_tp1) | last_response_id=%s", rid_show)

        for lab in st.labels:
            if st.status_by_label.get(lab, "") != CHO_TP1:
                continue
            if st.tp1_followup_done_by_label.get(lab, False):
                _log_tp1.info(
                    "tp1 TP1: bỏ qua %s — tp1_followup_done=true (đã gửi follow-up, chờ reset)",
                    lab,
                )
                continue
            tl = (st.trade_line_by_label.get(lab) or "").strip()
            tk = mt5_primary_ticket_for_label(st, lab, pid_m)
            if not tl or tk is None or int(tk) <= 0:
                _log_tp1.info("tp1 TP1: bỏ qua %s — thiếu trade_line hoặc ticket", lab)
                continue
            minimal = json.dumps(
                {"intraday_hanh_dong": "VÀO LỆNH", "trade_line": tl},
                ensure_ascii=False,
            )
            parsed, err = parse_openai_output_md(
                minimal,
                symbol_override=mt5_sym,
            )
            if err or parsed is None:
                _log_tp1.info("tp1 TP1: không parse trade_line | label=%s err=%s", lab, err)
                continue
            tp = float(parsed.tp1)
            touched = _tp1_touched(parsed, p_last)
            _log_tp1.info(
                "tp1 TP1: %s | side=%s tp1=%.5f last=%.5f epsilon=%.2f | chạm TP1=%s",
                lab,
                parsed.side,
                tp,
                p_last,
                _EPS,
                touched,
            )
            if not touched:
                continue
            if getattr(settings, "skip_trade_management", False) is True:
                update_plan_tp1_followup_done(lab, True, path=last_alert_path)
                _log_tp1.info(
                    "tp1 TP1: bỏ qua TRADE_MANAGEMENT (SKIP_TRADE_MANAGEMENT) | label=%s",
                    lab,
                )
                continue
            # Scalp: chạm TP1 → kiểm tra has_position.
            # Nếu chưa có position (lệnh pending) → huỷ lệnh ngay, không gọi OpenAI.
            # Nếu đã có position → fall through xuống partial-close + OpenAI như các label khác.
            if lab == "scalp":
                dry = bool(getattr(params, "mt5_dry_run", False))
                exe = getattr(params, "mt5_execute", True)
                tk_sc = int(tk or 0)
                accs_sp = load_mt5_accounts_for_cli(getattr(params, "mt5_accounts_json", None))

                has_pos = False
                if exe and tk_sc > 0:
                    prim_hp = primary_account(accs_sp) if accs_sp else None
                    if prim_hp:
                        has_pos, hp_msg = mt5_ticket_is_open_position(
                            tk_sc,
                            dry_run=dry,
                            terminal_path=prim_hp.terminal_path,
                            login=prim_hp.login,
                            password=prim_hp.password,
                            server=prim_hp.server,
                        )
                        _log_tp1.info(
                            "tp1 scalp has_position=%s | label=%s ticket=%s | %s",
                            has_pos,
                            lab,
                            tk_sc,
                            hp_msg,
                        )

                if not has_pos:
                    # Lệnh pending (chưa khớp) → huỷ ngay, không gọi OpenAI
                    tmap_sp = (st.mt5_tickets_by_label.get(lab) or {}) if st else {}
                    if exe and (tk_sc > 0 or tmap_sp):
                        if accs_sp and tmap_sp:
                            summ_sp = mt5_cancel_pending_or_close_all_accounts(
                                tmap_sp, accs_sp, dry_run=dry
                            )
                            msg_sp = format_mt5_multi_manage_for_telegram(summ_sp)
                            r_ok_sp = summ_sp.ok_all
                        else:
                            prim_sp = primary_account(accs_sp) if accs_sp else None
                            r = mt5_cancel_pending_or_close_position(
                                tk_sc,
                                dry_run=dry,
                                terminal_path=prim_sp.terminal_path if prim_sp else None,
                                login=prim_sp.login if prim_sp else None,
                                password=prim_sp.password if prim_sp else None,
                                server=prim_sp.server if prim_sp else None,
                            )
                            msg_sp = r.message
                            r_ok_sp = r.ok
                        _log.info(
                            "tp1: scalp chạm TP1 (không có position) — huỷ pending | %s", msg_sp
                        )
                        if settings.telegram_bot_token and not getattr(params, "no_telegram", False):
                            send_mt5_execution_log_to_ngan_gon_chat(
                                bot_token=settings.telegram_bot_token,
                                telegram_chat_id=settings.telegram_chat_id,
                                telegram_python_bot_chat_id=settings.telegram_python_bot_chat_id,
                                telegram_log_chat_id=settings.telegram_log_chat_id,
                                source="tp1-scalp-tp1",
                                text=f"{lab}: scalp chạm TP1 (pending) — huỷ lệnh\n{msg_sp}",
                                zone_label=lab,
                                trade_line=tl,
                                execution_ok=r_ok_sp,
                            )
                    else:
                        _log.info(
                            "tp1: scalp chạm TP1 (pending) — bỏ qua MT5 (exe=%s tk=%s)",
                            exe,
                            tk_sc,
                        )
                    update_single_plan_status(lab, LOAI, path=last_alert_path)
                    clear_plan_mt5_fields(lab, path=last_alert_path)
                    update_plan_tp1_followup_done(lab, False, path=last_alert_path)
                    _log_tp1.info(
                        "tp1: %s chạm TP1 (scalp, pending) — huỷ lệnh, không gọi OpenAI",
                        lab,
                    )
                    return None
                # has_pos=True: đã có position, chạm TP1 → đánh dấu done luôn, không gọi OpenAI
                update_single_plan_status(lab, LOAI, path=last_alert_path)
                clear_plan_mt5_fields(lab, path=last_alert_path)
                update_plan_tp1_followup_done(lab, False, path=last_alert_path)
                _log_tp1.info(
                    "tp1: %s chạm TP1 (scalp, has_position) — đánh dấu done, không gọi OpenAI",
                    lab,
                )
                return None
            tmap_tp1 = (st.mt5_tickets_by_label.get(lab) or {}) if st else {}
            ok_partial = _partial_close_tp1_runner_before_openai(
                settings=settings,
                params=params,
                label=lab,
                trade_line=tl,
                parsed=parsed,
                ticket=int(tk),
                ticket_by_account=tmap_tp1,
                accounts=accounts_m,
                symbol_override=mt5_sym,
            )
            if not ok_partial:
                update_plan_tp1_followup_done(lab, False, path=last_alert_path)
                _log_tp1.info(
                    "tp1: %s chạm TP1 nhưng partial close 50%% lỗi — chưa gọi OpenAI",
                    lab,
                )
                return None
            _log.info("tp1: %s cho_tp1 chạm TP1 last=%s — follow-up OpenAI", lab, p_last)
            try:
                new_r = _run_tp1_openai_and_act(
                    settings=settings,
                    params=params,
                    last_alert_path=last_alert_path,
                    label=lab,
                    trade_line=tl,
                    p_last=p_last,
                    parsed=parsed,
                    page=page,
                    tv=tv,
                    symbol=symbol,
                    settle_ms=settle_ms,
                    browser_context=browser_context,
                    prev_response_id=rid,
                )
            except Exception as e:
                _log.exception("tp1 follow-up lỗi: %s", e)
                update_plan_tp1_followup_done(lab, False, path=last_alert_path)
                raise
            return new_r

        _log_tp1.info(
            "tp1 tick: không gọi OpenAI (chưa chạm TP1 hoặc đã xử lý) | đã_đổi_cho_tp1=%s",
            arm_action,
        )
        return None


def tp1_dry_run_report(
    *,
    last_alert_path: Path,
    p_last: float,
    symbol_override: Optional[str] = None,
    mt5_accounts_json: Optional[Path] = None,
) -> str:
    """
    Báo cáo text cho CLI: so ``p_last`` với entry/TP1 parse từ ``trade_line`` (không browser/OpenAI/MT5).
    """
    st = read_last_alert_state(last_alert_path)
    if st is None:
        return f"Không đọc được last_alert_prices: {last_alert_path}\n"
    accs = load_mt5_accounts_for_cli(mt5_accounts_json)
    pid = primary_account_id(accs) if accs else None
    lines: list[str] = [
        f"last_alert: {last_alert_path}",
        f"p_last (cùng quy ước Last watchlist): {p_last}",
        "",
    ]
    seen_detail = False
    for lab in st.labels:
        s = st.status_by_label.get(lab, "")
        if s not in (VAO_LENH, CHO_TP1):
            continue
        tl = (st.trade_line_by_label.get(lab) or "").strip()
        tk = mt5_primary_ticket_for_label(st, lab, pid)
        if not tl or tk is None or int(tk) <= 0:
            lines.append(f"[{lab}] status={s} — thiếu trade_line hoặc ticket hợp lệ")
            lines.append("")
            continue
        minimal = json.dumps(
            {"intraday_hanh_dong": "VÀO LỆNH", "trade_line": tl},
            ensure_ascii=False,
        )
        parsed, err = parse_openai_output_md(minimal, symbol_override=symbol_override)
        if err or parsed is None:
            lines.append(f"[{lab}] status={s} ticket={tk} — không parse trade_line: {err}")
            lines.append("")
            continue
        seen_detail = True
        ref = _entry_reference_price(parsed)
        thr = arm_threshold_tp1_for_label(lab)
        arm = _arm_threshold_met(parsed, p_last, label=lab)
        tp_hit = _tp1_touched(parsed, p_last)
        diff = float(p_last) - ref
        band_txt = (
            f"0≤last-ref≤{thr:g}"
            if parsed.side == "BUY"
            else f"-{thr:g}≤last-ref≤0"
        )
        tp1_done = bool(st.tp1_followup_done_by_label.get(lab, False))
        lines.append(f"[{lab}] status={s} | ticket={tk}")
        lines.append(
            f"  side={parsed.side} entry_ref={ref:.5f} tp1={float(parsed.tp1):.5f}"
        )
        lines.append(
            f"  vao_lenh→cho_tp1 ({band_txt}): "
            f"{'đạt' if arm else 'chưa'} — last-ref={diff:.5f}"
        )
        lines.append(
            f"  cho_tp1→chạm TP1: {'đạt' if tp_hit else 'chưa'} | tp1_followup_done={tp1_done}"
        )
        lines.append("")
    if not seen_detail:
        lines.append(
            "Không có plan vao_lenh/cho_tp1 với trade_line + ticket để so khớp (hoặc parse lỗi)."
        )
    return "\n".join(lines).rstrip() + "\n"
