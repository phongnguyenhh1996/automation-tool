"""Theo dõi sau vào lệnh: last-ref trong dải arm theo plan (mặc định ±3 giá; scalp ±1) → ``cho_tp1``; chạm TP1 → Coinmap M5 + OpenAI."""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

from playwright.sync_api import BrowserContext, Page

from automation_tool.coinmap import capture_charts
from automation_tool.config import Settings, resolved_model_for_intraday_alert
from automation_tool.images import coinmap_xauusd_5m_json_path, read_main_chart_symbol
from automation_tool.mt5_execute import execute_trade, format_mt5_execution_for_telegram
from automation_tool.mt5_manage import mt5_cancel_pending_or_close_position, mt5_ticket_still_open
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
    read_last_alert_state,
    update_plan_mt5_entry,
    update_plan_tp1_followup_done,
    update_single_plan_status,
)
from automation_tool.telegram_bot import (
    send_mt5_execution_log_to_ngan_gon_chat,
    send_openai_output_to_telegram,
)

_log = logging.getLogger(__name__)
# Log có cấu trúc cho TELEGRAM_LOG_CHAT_ID (propagate → automation_tool)
_log_tp1 = logging.getLogger("automation_tool.tp1")

_tp1_lock = threading.Lock()

# Cùng epsilon touch vùng chờ
_EPS = 0.01


@dataclass
class TP1FollowupDecision:
    sau_tp1: Literal["loại", "chinh_trade_line"]
    trade_line_moi: str
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
    for m in re.finditer(r"\{[\s\S]*\}", t):
        try:
            d = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and "sau_tp1_hanh_dong" in d:
            return d
    return None


def parse_tp1_followup_decision(text: str) -> Optional[TP1FollowupDecision]:
    raw = _extract_tp1_json(text)
    if raw is None:
        return None
    sp = str(raw.get("sau_tp1_hanh_dong") or "").strip().lower()
    if sp in ("loại", "loai"):
        sau = "loại"
    elif sp in ("chinh_trade_line", "chỉnh_trade_line", "chinh_sua", "chỉnh"):
        sau = "chinh_trade_line"
    else:
        return None
    tlm = str(raw.get("trade_line_moi") or "").strip()
    oct = str(raw.get("out_chi_tiet") or "").strip()
    ogn = str(raw.get("output_ngan_gon") or "").strip()
    return TP1FollowupDecision(
        sau_tp1=sau,
        trade_line_moi=tlm,
        out_chi_tiet=oct,
        output_ngan_gon=ogn,
    )


def _send_tp1_telegram(
    *,
    settings: Settings,
    params: Any,
    raw_text: str,
) -> None:
    if getattr(params, "no_telegram", False):
        return
    send_openai_output_to_telegram(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        raw=raw_text,
        default_parse_mode=settings.telegram_parse_mode,
        summary_chat_id=settings.telegram_output_ngan_gon_chat_id,
    )


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
    capture_yaml = params.capture_coinmap_yaml
    charts_dir = params.charts_dir
    storage = params.storage_state_path
    headless = params.headless
    no_save = params.no_save_storage

    st0 = read_last_alert_state(last_alert_path)
    tk0 = int(st0.mt5_ticket_by_label.get(label) or 0) if st0 else 0
    dry = bool(getattr(params, "mt5_dry_run", False))
    exe = getattr(params, "mt5_execute", True)
    if exe and tk0 > 0:
        still_open, ticket_msg = mt5_ticket_still_open(tk0, dry_run=dry)
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
    )
    _log.info("tp1-followup: capture_charts → %d file(s)", len(paths))
    json_path = coinmap_xauusd_5m_json_path(charts_dir)
    if json_path is None or not json_path.is_file():
        raise SystemExit(f"tp1-followup: no main 5m Coinmap JSON under {charts_dir}")
    _log_tp1.info("tp1-followup Coinmap M5 JSON: %s", json_path)

    user_msg = TP1_POST_TOUCH_USER_TEMPLATE.format(
        plan_label=label,
        trade_line=trade_line,
        last_price=p_last,
        tp1_price=parsed.tp1,
    )
    out_text, new_id = run_single_followup_responses(
        api_key=settings.openai_api_key,
        prompt_id=settings.openai_prompt_id,
        prompt_version=settings.openai_prompt_version,
        user_text=user_msg,
        coinmap_json_paths=[json_path],
        previous_response_id=prev_response_id,
        vector_store_ids=settings.openai_vector_store_ids,
        store=settings.openai_responses_store,
        include=settings.openai_responses_include,
        model=resolved_model_for_intraday_alert(
            settings, getattr(params, "openai_model_cli", None)
        ),
    )
    update_plan_tp1_followup_done(label, True, path=last_alert_path)
    _log_tp1.info(
        "tp1-followup OpenAI xong | response_id=%s | độ dài output=%d | gửi Telegram phân tích=%s",
        new_id,
        len(out_text or ""),
        "có" if not getattr(params, "no_telegram", False) else "không (--no-telegram)",
    )
    _send_tp1_telegram(settings=settings, params=params, raw_text=out_text)

    dec = parse_tp1_followup_decision(out_text)
    if dec is None:
        _log.warning("tp1-followup: không parse được sau_tp1_hanh_dong — bỏ qua hành động MT5.")
        _log_tp1.warning("tp1-followup: không parse JSON sau_tp1_hanh_dong từ output model")
        update_plan_tp1_followup_done(label, False, path=last_alert_path)
        return new_id

    st = read_last_alert_state(last_alert_path)
    tk = (st.mt5_ticket_by_label.get(label) if st else None) or 0
    _log_tp1.info(
        "tp1-followup parse OK | sau_tp1=%s | mt5_execute=%s mt5_dry_run=%s | trade_line_moi_len=%d",
        dec.sau_tp1,
        exe,
        dry,
        len(dec.trade_line_moi or ""),
    )

    if dec.sau_tp1 == "loại":
        if exe and tk > 0:
            r = mt5_cancel_pending_or_close_position(int(tk), dry_run=dry)
            _log.info("tp1-followup loại: %s", r.message)
            if settings.telegram_bot_token and settings.telegram_chat_id and not getattr(
                params, "no_telegram", False
            ):
                send_mt5_execution_log_to_ngan_gon_chat(
                    bot_token=settings.telegram_bot_token,
                    telegram_chat_id=settings.telegram_chat_id,
                    source="tp1-followup",
                    text=f"{label}: loại sau TP1\n{r.message}",
                    trade_line=(st.trade_line_by_label.get(label) or "") if st else None,
                    execution_ok=r.ok,
                )
        update_single_plan_status(label, LOAI, path=last_alert_path)
        clear_plan_mt5_fields(label, path=last_alert_path)
        update_plan_tp1_followup_done(label, False, path=last_alert_path)
        _log_tp1.info("tp1-followup kết thúc nhánh loại | label=%s → status=loai", label)
        return new_id

    # chinh_trade_line
    if not dec.trade_line_moi.strip():
        _log.warning("tp1-followup: chinh_trade_line nhưng trade_line_moi rỗng.")
        update_plan_tp1_followup_done(label, False, path=last_alert_path)
        return new_id
    minimal = json.dumps(
        {"intraday_hanh_dong": "VÀO LỆNH", "trade_line": dec.trade_line_moi.strip()},
        ensure_ascii=False,
    )
    new_parsed, err = parse_openai_output_md(
        minimal,
        symbol_override=getattr(params, "mt5_symbol", None),
    )
    if err or new_parsed is None:
        _log.warning("tp1-followup: parse trade_line_moi lỗi: %s", err)
        update_plan_tp1_followup_done(label, False, path=last_alert_path)
        return new_id
    if exe and tk > 0:
        r0 = mt5_cancel_pending_or_close_position(int(tk), dry_run=dry)
        _log.info("tp1-followup: đóng/huỷ lệnh cũ: %s", r0.message)
    if exe:
        ex = execute_trade(
            new_parsed,
            dry_run=dry,
            symbol_override=getattr(params, "mt5_symbol", None),
        )
        _log.info("tp1-followup: execute_trade → %s", ex.message)
        if settings.telegram_bot_token and settings.telegram_chat_id and not getattr(
            params, "no_telegram", False
        ):
            send_mt5_execution_log_to_ngan_gon_chat(
                bot_token=settings.telegram_bot_token,
                telegram_chat_id=settings.telegram_chat_id,
                source="tp1-followup-chinh",
                text=format_mt5_execution_for_telegram(ex),
                zone_label=label,
                trade_line=dec.trade_line_moi.strip(),
                execution_ok=ex.ok,
            )
        tid = int(ex.order) if ex.order else 0
        if tid > 0:
            update_plan_mt5_entry(
                label,
                trade_line=dec.trade_line_moi.strip(),
                mt5_ticket=tid,
                path=last_alert_path,
            )
    update_single_plan_status(label, VAO_LENH, path=last_alert_path, entry_manual=False)
    update_plan_tp1_followup_done(label, False, path=last_alert_path)
    _log_tp1.info("tp1-followup kết thúc nhánh chỉnh trade_line | label=%s → vao_lenh + ticket mới (nếu có)", label)
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
        for lab in st.labels:
            if st.status_by_label.get(lab, "") != VAO_LENH:
                continue
            tl = (st.trade_line_by_label.get(lab) or "").strip()
            tk = st.mt5_ticket_by_label.get(lab)
            if not tl or not tk or int(tk) <= 0:
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
            tk = st.mt5_ticket_by_label.get(lab)
            if not tl or not tk or int(tk) <= 0:
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
            # Scalp: chạm TP1 → huỷ ticket ngay, không gọi OpenAI follow-up.
            if lab == "scalp":
                dry = bool(getattr(params, "mt5_dry_run", False))
                exe = getattr(params, "mt5_execute", True)
                tk_sc = int(tk or 0)
                if exe and tk_sc > 0:
                    r = mt5_cancel_pending_or_close_position(tk_sc, dry_run=dry)
                    _log.info("tp1: scalp cho_tp1 chạm TP1 — huỷ ticket | %s", r.message)
                    if settings.telegram_bot_token and settings.telegram_chat_id and not getattr(
                        params, "no_telegram", False
                    ):
                        send_mt5_execution_log_to_ngan_gon_chat(
                            bot_token=settings.telegram_bot_token,
                            telegram_chat_id=settings.telegram_chat_id,
                            source="tp1-scalp-tp1",
                            text=f"{lab}: scalp chạm TP1 — huỷ ticket\n{r.message}",
                            zone_label=lab,
                            trade_line=tl,
                            execution_ok=r.ok,
                        )
                else:
                    _log.info(
                        "tp1: scalp cho_tp1 chạm TP1 — bỏ qua MT5 (exe=%s tk=%s)",
                        exe,
                        tk_sc,
                    )
                update_single_plan_status(lab, LOAI, path=last_alert_path)
                clear_plan_mt5_fields(lab, path=last_alert_path)
                update_plan_tp1_followup_done(lab, False, path=last_alert_path)
                _log_tp1.info(
                    "tp1: %s cho_tp1 chạm TP1 (scalp) — huỷ ticket / loại, không gọi OpenAI",
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
) -> str:
    """
    Báo cáo text cho CLI: so ``p_last`` với entry/TP1 parse từ ``trade_line`` (không browser/OpenAI/MT5).
    """
    st = read_last_alert_state(last_alert_path)
    if st is None:
        return f"Không đọc được last_alert_prices: {last_alert_path}\n"
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
        tk = st.mt5_ticket_by_label.get(lab)
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
