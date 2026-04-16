"""Sau phản hồi đầu tiên của phân tích chart: hop_luu + trade_line theo vùng → MT5 + last_alert_prices."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from automation_tool.mt5_execute import execute_trade, format_mt5_execution_for_telegram
from automation_tool.mt5_manage import mt5_latest_position_ticket
from automation_tool.mt5_openai_parse import ParsedTrade, parse_openai_output_md
from automation_tool.openai_analysis_json import (
    AUTO_MT5_HOP_LUU_THRESHOLD,
    AUTO_MT5_HOP_LUU_THRESHOLD_SCALP,
    PriceZoneEntry,
    auto_mt5_hop_luu_threshold_for_label,
    parse_analysis_from_openai_text,
    select_zone_for_auto_mt5,
    select_zone_for_auto_mt5_for_label,
    select_zone_for_vao_lenh_ignore_hop,
    select_zone_for_vao_lenh_ignore_hop_for_label,
)
from automation_tool.state_files import (
    VAO_LENH,
    merge_trade_lines_from_openai_analysis_text,
    read_last_alert_state,
    update_plan_mt5_entry,
    update_single_plan_status,
    write_last_alert_prices,
)
from automation_tool.telegram_bot import (
    send_first_response_log_to_log_chat,
    send_mt5_execution_log_to_ngan_gon_chat,
    send_user_friendly_notice,
)
from automation_tool.zone_prices import parse_three_zone_prices

_log = logging.getLogger(__name__)

_PLAN_ORDER = ("plan_chinh", "plan_phu", "scalp")


def plan_label_nearest_trade_entry(
    parsed: ParsedTrade,
    triple: tuple[float, float, float],
) -> str:
    """Chọn plan_chinh / plan_phu / scalp gần giá vào lệnh (hoặc heuristic khi MARKET)."""
    if parsed.kind == "MARKET" or parsed.price is None:
        ref = (parsed.sl + parsed.tp1) / 2.0
    else:
        ref = float(parsed.price)
    best_i = min(range(3), key=lambda i: abs(triple[i] - ref))
    return _PLAN_ORDER[best_i]


def _zone_diagnostics_lines(prices: list[PriceZoneEntry]) -> str:
    lines: list[str] = []
    for p in prices:
        hop_s = str(p.hop_luu) if p.hop_luu is not None else "—"
        has_tl = "có" if (p.trade_line or "").strip() else "không"
        lines.append(f"  • {p.label}: value={p.value} hop_luu={hop_s} trade_line={has_tl}")
    return "\n".join(lines)


def _tg(
    *,
    telegram_bot_token: Optional[str],
    telegram_log_chat_id: Optional[str],
    telegram_source_label: str,
    body: str,
) -> None:
    if not telegram_bot_token or not telegram_log_chat_id:
        return
    send_first_response_log_to_log_chat(
        bot_token=telegram_bot_token,
        telegram_log_chat_id=telegram_log_chat_id,
        source=telegram_source_label,
        text=body,
    )


def _nf(
    *,
    telegram_bot_token: Optional[str],
    telegram_python_bot_chat_id: Optional[str],
    title: str,
    body: str = "",
) -> None:
    if not telegram_bot_token:
        return
    send_user_friendly_notice(
        bot_token=telegram_bot_token,
        chat_id=telegram_python_bot_chat_id,
        title=title,
        body=body,
    )


_ZONE_LABEL_VN = {
    "plan_chinh": "Plan chính",
    "plan_phu": "Plan phụ",
    "scalp": "Scalp",
}


def _zone_display(label: str) -> str:
    k = (label or "").strip().lower()
    return _ZONE_LABEL_VN.get(k, label)


def _minimal_json_for_trade_parse(trade_line: str) -> str:
    """JSON tối thiểu để ``parse_openai_output_md`` lấy đúng một dòng lệnh."""
    return json.dumps(
        {"intraday_hanh_dong": "VÀO LỆNH", "trade_line": trade_line.strip()},
        ensure_ascii=False,
    )


def apply_first_response_vao_lenh(
    first_model_text: str,
    *,
    last_alert_path: Path,
    mt5_execute: bool = True,
    mt5_dry_run: bool = False,
    mt5_symbol: Optional[str] = None,
    telegram_bot_token: Optional[str] = None,
    telegram_chat_id: Optional[str] = None,
    telegram_log_chat_id: Optional[str] = None,
    telegram_python_bot_chat_id: Optional[str] = None,
    telegram_output_ngan_gon_chat_id: Optional[str] = None,
    telegram_source_label: str = "phân tích chart (phản hồi đầu)",
    auto_mt5_zone_label: Optional[str] = None,
) -> bool:
    """
    Ghi ``last_alert_prices`` khi đủ triple giá.

    Auto-MT5 (không dry-run mặc định): ưu tiên vùng với ``hop_luu`` vượt ngưỡng
    (plan_chinh/plan_phu > 75; scalp > 60) và ``trade_line`` không rỗng.
    Nếu không đủ ngưỡng nhưng JSON có ``intraday_hanh_dong: VÀO LỆNH`` và có ``trade_line``
    khả dụng cho vùng — vẫn vào lệnh (bỏ gate hợp lưu). Ghi ``vao_lenh`` + ``entry_manual`` false.
    Nếu ``auto_mt5_zone_label`` được set (vd. ``plan_chinh``), chỉ xét đúng vùng đó (Nhật ký TV).

    Telegram: log phản hồi đầu (hop_luu, vùng, lỗi…) → ``telegram_log_chat_id``
    (``TELEGRAM_LOG_CHAT_ID``). Tin ngắn non-tech → ``telegram_python_bot_chat_id``
    (``TELEGRAM_PYTHON_BOT_CHAT_ID``). Kết quả ``execute_trade`` → ``telegram_chat_id`` (``TELEGRAM_CHAT_ID``).

    Returns:
        ``True`` nếu đã hoàn tất nhánh auto-MT5 (đã ghi ``vao_lenh`` cho vùng chọn; MT5 chạy thành công
        khi bật ``mt5_execute``). ``False`` trong mọi trường hợp khác (gồm chỉ ghi 3 giá, chưa đủ hop_luu).
    """
    text = (first_model_text or "").strip()
    if not text:
        _log.debug("first_response: bỏ qua — text rỗng.")
        return False

    try:
        merge_trade_lines_from_openai_analysis_text(text, path=last_alert_path)
    except Exception as e:
        _log.warning("first_response: merge trade_line từ JSON — %s", e)

    zt, zerr, nc = parse_three_zone_prices(text)
    if nc is True:
        _log.info("first_response: no_change — không ghi last_alert.")
        _nf(
            telegram_bot_token=telegram_bot_token,
            telegram_python_bot_chat_id=telegram_python_bot_chat_id,
            title="Không có cập nhật giá mới từ phân tích lần này.",
            body="Hệ thống giữ nguyên bản ghi giá trước đó.",
        )
        return False
    if zt is None:
        _log.warning("first_response: không parse được triple giá: %s", zerr)
        _nf(
            telegram_bot_token=telegram_bot_token,
            telegram_python_bot_chat_id=telegram_python_bot_chat_id,
            title="Không đọc được 3 mức giá từ phân tích.",
            body="Vui lòng xem log kỹ thuật hoặc chạy lại phân tích.",
        )
        return False

    p1, p2, p3 = zt
    _log.info(
        "first_response: triple giá plan_chinh=%s plan_phu=%s scalp=%s | last_alert_path=%s",
        p1,
        p2,
        p3,
        last_alert_path,
    )

    try:
        write_last_alert_prices(zt, path=last_alert_path)
    except SystemExit as e:
        _log.warning("first_response: không ghi last_alert — %s", e)
        _nf(
            telegram_bot_token=telegram_bot_token,
            telegram_python_bot_chat_id=telegram_python_bot_chat_id,
            title="Không lưu được bản ghi giá.",
            body=str(e),
        )
        return False

    _log.info(
        "first_response: đã merge last_alert_prices (3 giá) → %s",
        last_alert_path,
    )
    _nf(
        telegram_bot_token=telegram_bot_token,
        telegram_python_bot_chat_id=telegram_python_bot_chat_id,
        title="Đã lưu xong 3 mức giá (plan chính, phụ, scalp).",
        body="",
    )

    payload = parse_analysis_from_openai_text(text)
    if payload is None or not payload.prices:
        msg = (
            "Đã ghi 3 giá vào last_alert.\n"
            "Không có JSON `prices` (hoặc rỗng) — không thể đọc hop_luu/trade_line theo vùng; bỏ qua auto-MT5."
        )
        _log.info("first_response: %s", msg.replace("\n", " "))
        _tg(
            telegram_bot_token=telegram_bot_token,
            telegram_log_chat_id=telegram_log_chat_id,
            telegram_source_label=telegram_source_label,
            body=msg,
        )
        _nf(
            telegram_bot_token=telegram_bot_token,
            telegram_python_bot_chat_id=telegram_python_bot_chat_id,
            title="Đã lưu giá nhưng chưa đọc được thông tin vùng.",
            body="Không tự đặt lệnh. Cần phản hồi đủ JSON vùng (hop_luu, trade_line).",
        )
        return False

    zones_txt = _zone_diagnostics_lines(payload.prices)
    _log.info(
        "first_response: JSON prices (%d vùng):\n%s",
        len(payload.prices),
        zones_txt,
    )

    zone_filter = (auto_mt5_zone_label or "").strip()
    if zone_filter:
        picked = select_zone_for_auto_mt5_for_label(payload.prices, zone_filter)
    else:
        picked = select_zone_for_auto_mt5(payload.prices)
    if picked is None and payload.intraday_hanh_dong == "VÀO LỆNH":
        root_tl = (payload.trade_line or "").strip()
        if zone_filter:
            picked = select_zone_for_vao_lenh_ignore_hop_for_label(
                payload.prices,
                zone_filter,
                root_trade_line=root_tl,
            )
        else:
            picked = select_zone_for_vao_lenh_ignore_hop(payload.prices)
        if picked is not None:
            _log.info(
                "first_response: VÀO LỆNH — bỏ gate hop_luu, chọn vùng %s (hop_luu=%s)",
                picked[0],
                picked[1],
            )
    if picked is None:
        zhint = f" (chỉ vùng `{zone_filter}`)" if zone_filter else ""
        msg = (
            f"Đã ghi 3 giá. Ngưỡng auto-MT5: plan_chinh/plan_phu hop_luu > {AUTO_MT5_HOP_LUU_THRESHOLD}, "
            f"scalp hop_luu > {AUTO_MT5_HOP_LUU_THRESHOLD_SCALP} + trade_line không rỗng{zhint}.\n"
            f"Không có vùng đủ điều kiện — không ghi vao_lenh / không MT5.\n"
            f"Vùng trong JSON:\n{zones_txt}"
        )
        _log.info("first_response: không có vùng auto-MT5 — chỉ giá đã lưu.")
        _tg(
            telegram_bot_token=telegram_bot_token,
            telegram_log_chat_id=telegram_log_chat_id,
            telegram_source_label=telegram_source_label,
            body=msg,
        )
        _nf(
            telegram_bot_token=telegram_bot_token,
            telegram_python_bot_chat_id=telegram_python_bot_chat_id,
            title="Chưa đủ độ tin cậy để tự đặt lệnh.",
            body="Chờ cơ hội tiếp theo — xem log kỹ thuật nếu cần chi tiết các vùng.",
        )
        return False

    label, hop, zone_trade_line = picked
    minimal = _minimal_json_for_trade_parse(zone_trade_line)
    parsed, err = parse_openai_output_md(minimal, symbol_override=mt5_symbol)
    if err or parsed is None:
        msg = (
            f"Chọn vùng {label} (hop_luu={hop}) nhưng không parse được trade_line.\n"
            f"Lỗi: {err}\n"
            f"Dòng (rút gọn): {(zone_trade_line[:200] + '…') if len(zone_trade_line) > 200 else zone_trade_line}"
        )
        _log.warning("first_response: %s", msg.replace("\n", " "))
        _tg(
            telegram_bot_token=telegram_bot_token,
            telegram_log_chat_id=telegram_log_chat_id,
            telegram_source_label=telegram_source_label,
            body=msg,
        )
        _nf(
            telegram_bot_token=telegram_bot_token,
            telegram_python_bot_chat_id=telegram_python_bot_chat_id,
            title="Đã chọn vùng nhưng không hiểu được dòng lệnh.",
            body="Không tự đặt lệnh. Kiểm tra trade_line trong phản hồi AI.",
        )
        return False

    _thr = auto_mt5_hop_luu_threshold_for_label(label)
    _log.info(
        "first_response: chọn vùng %s hop_luu=%s (>%s) — parse trade_line OK → ghi %s",
        label,
        hop,
        _thr,
        VAO_LENH,
    )

    # Guard: avoid duplicate MT5 execution if this zone already entered previously.
    st = read_last_alert_state(last_alert_path)
    if st is not None and st.status_by_label.get(label) == VAO_LENH:
        msg = (
            f"Bỏ qua auto-MT5: vùng `{label}` đã ở trạng thái `{VAO_LENH}` trong last_alert_prices.\n"
            f"last_alert: {last_alert_path}"
        )
        _log.info("first_response: %s", msg.replace("\n", " "))
        _tg(
            telegram_bot_token=telegram_bot_token,
            telegram_log_chat_id=telegram_log_chat_id,
            telegram_source_label=telegram_source_label,
            body=msg,
        )
        _nf(
            telegram_bot_token=telegram_bot_token,
            telegram_python_bot_chat_id=telegram_python_bot_chat_id,
            title=f"Vùng «{_zone_display(label)}» đã có lệnh trước đó.",
            body="Hệ thống không gửi trùng lệnh tự động.",
        )
        return False

    try:
        update_single_plan_status(
            label,
            VAO_LENH,
            path=last_alert_path,
            entry_manual=False,
        )
    except SystemExit as e:
        _log.warning("first_response: không cập nhật status — %s", e)
        _tg(
            telegram_bot_token=telegram_bot_token,
            telegram_log_chat_id=telegram_log_chat_id,
            telegram_source_label=telegram_source_label,
            body=f"Lỗi ghi status: {e}",
        )
        _nf(
            telegram_bot_token=telegram_bot_token,
            telegram_python_bot_chat_id=telegram_python_bot_chat_id,
            title="Lỗi khi cập nhật trạng thái vùng.",
            body=str(e),
        )
        return False

    _log.info(
        "first_response: đã ghi status %s=%s tại %s (entry_manual=false)",
        label,
        VAO_LENH,
        last_alert_path,
    )

    summary = (
        f"Vùng chọn: {label} | hop_luu={hop} (ngưỡng >{auto_mt5_hop_luu_threshold_for_label(label)})\n"
        f"last_alert: {last_alert_path}\n"
        f"MT5 symbol override: {mt5_symbol or '(từ lệnh)'}\n"
        "Đã ghi vao_lenh (entry_manual=false)."
    )
    if mt5_execute:
        summary += "\nKết quả MT5 (hoặc dry-run) gửi tới TELEGRAM_CHAT_ID."
    else:
        summary += "\n--no-mt5-execute: không gọi MT5."
    _tg(
        telegram_bot_token=telegram_bot_token,
        telegram_log_chat_id=telegram_log_chat_id,
        telegram_source_label=telegram_source_label,
        body=summary,
    )
    zd = _zone_display(label)
    if mt5_execute:
        _nf(
            telegram_bot_token=telegram_bot_token,
            telegram_python_bot_chat_id=telegram_python_bot_chat_id,
            title=f"Đã chọn «{zd}» (độ tin cậy đủ). Chuẩn bị gửi lệnh lên MT5.",
            body="Đang xử lý…" if not mt5_dry_run else "Chế độ thử (dry-run) — không gửi lệnh thật.",
        )
    else:
        _nf(
            telegram_bot_token=telegram_bot_token,
            telegram_python_bot_chat_id=telegram_python_bot_chat_id,
            title=f"Đã ghi trạng thái «{zd}» — vào lệnh (chưa gửi MT5).",
            body="Thực thi MT5 đang tắt (--no-mt5-execute).",
        )

    if not mt5_execute:
        _log.info("first_response: bỏ qua MT5 (--no-mt5-execute).")
        return True

    try:
        ex = execute_trade(
            parsed,
            dry_run=mt5_dry_run,
            symbol_override=mt5_symbol,
        )
        print(ex.message, flush=True)
        _log.info(
            "first_response MT5 dry_run=%s: %s",
            mt5_dry_run,
            ex.message,
        )
        if telegram_bot_token and (telegram_chat_id or "").strip():
            send_mt5_execution_log_to_ngan_gon_chat(
                bot_token=telegram_bot_token,
                telegram_chat_id=telegram_chat_id,
                source=telegram_source_label,
                text=format_mt5_execution_for_telegram(ex),
                zone_label=label,
            )
        _mt5_lines = [ex.message]
        if ex.order:
            _mt5_lines.append(f"Mã lệnh: {ex.order}")
        if mt5_dry_run:
            _mt5_lines.append("(Chế độ thử — kiểm tra trước khi gửi lệnh thật.)")
        _nf(
            telegram_bot_token=telegram_bot_token,
            telegram_python_bot_chat_id=telegram_python_bot_chat_id,
            title=f"Kết quả lệnh MT5 — «{_zone_display(label)}»",
            body="\n".join(_mt5_lines),
        )
        tid = int(ex.order) if ex.order else 0
        if (not tid or tid <= 0) and not mt5_dry_run and (ex.resolved_symbol or "").strip():
            alt = mt5_latest_position_ticket(str(ex.resolved_symbol).strip())
            if alt:
                tid = int(alt)
        if ex.ok and tid > 0:
            try:
                update_plan_mt5_entry(
                    label,
                    trade_line=zone_trade_line.strip(),
                    mt5_ticket=tid,
                    path=last_alert_path,
                )
            except SystemExit as pe:
                _log.warning("first_response: không ghi trade_line/ticket — %s", pe)
        return True
    except SystemExit as e:
        _log.warning("first_response: MT5 không chạy — %s", e)
        _tg(
            telegram_bot_token=telegram_bot_token,
            telegram_log_chat_id=telegram_log_chat_id,
            telegram_source_label=telegram_source_label,
            body=f"MT5 SystemExit: {e}",
        )
        _nf(
            telegram_bot_token=telegram_bot_token,
            telegram_python_bot_chat_id=telegram_python_bot_chat_id,
            title="Không gửi được lệnh lên MT5.",
            body=str(e),
        )
        return False
    except Exception as e:
        _log.exception("first_response: lỗi execute_trade: %s", e)
        _tg(
            telegram_bot_token=telegram_bot_token,
            telegram_log_chat_id=telegram_log_chat_id,
            telegram_source_label=telegram_source_label,
            body=f"Lỗi execute_trade: {e!s}",
        )
        _nf(
            telegram_bot_token=telegram_bot_token,
            telegram_python_bot_chat_id=telegram_python_bot_chat_id,
            title="Lỗi khi gửi lệnh MT5.",
            body=str(e),
        )
        return False
