"""Sau phản hồi đầu tiên của phân tích chart: hop_luu + trade_line theo vùng → MT5 + last_alert_prices."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from automation_tool.mt5_execute import execute_trade, format_mt5_execution_for_telegram
from automation_tool.mt5_openai_parse import ParsedTrade, parse_openai_output_md
from automation_tool.openai_analysis_json import (
    parse_analysis_from_openai_text,
    select_zone_for_auto_mt5,
)
from automation_tool.state_files import VAO_LENH, update_single_plan_status, write_last_alert_prices
from automation_tool.telegram_bot import send_mt5_execution_log_to_ngan_gon_chat
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
    telegram_output_ngan_gon_chat_id: Optional[str] = None,
    telegram_source_label: str = "phân tích chart (phản hồi đầu)",
) -> None:
    """
    Ghi ``last_alert_prices`` khi đủ triple giá.

    Auto-MT5 (không dry-run mặc định): chỉ khi có vùng với ``hop_luu`` > 80 và ``trade_line``
    không rỗng trong JSON; dùng đúng ``trade_line`` của vùng đó. Ghi ``vao_lenh`` + ``entry_manual`` false.
    """
    text = (first_model_text or "").strip()
    if not text:
        return

    zt, _zerr, nc = parse_three_zone_prices(text)
    if nc is True or zt is None:
        return

    try:
        write_last_alert_prices(zt, path=last_alert_path)
    except SystemExit as e:
        _log.warning("first_response: không ghi last_alert — %s", e)
        return

    payload = parse_analysis_from_openai_text(text)
    if payload is None or not payload.prices:
        _log.info(
            "first_response: đã ghi 3 giá; không có JSON prices để hop_luu/trade_line — bỏ qua auto-MT5."
        )
        return

    picked = select_zone_for_auto_mt5(payload.prices)
    if picked is None:
        _log.info(
            "first_response: không có vùng nào hop_luu>80 kèm trade_line — chỉ cập nhật giá, không vao_lenh/MT5."
        )
        return

    label, hop, zone_trade_line = picked
    minimal = _minimal_json_for_trade_parse(zone_trade_line)
    parsed, err = parse_openai_output_md(minimal, symbol_override=mt5_symbol)
    if err or parsed is None:
        _log.warning(
            "first_response: hop_luu=%s label=%s nhưng không parse được trade_line: %s",
            hop,
            label,
            err,
        )
        return

    try:
        update_single_plan_status(
            label,
            VAO_LENH,
            path=last_alert_path,
            entry_manual=False,
        )
    except SystemExit as e:
        _log.warning("first_response: không cập nhật status — %s", e)
        return

    _log.info(
        "first_response: vùng %s hop_luu=%s → %s tại %s",
        label,
        hop,
        VAO_LENH,
        last_alert_path,
    )

    if not mt5_execute:
        _log.info("first_response: bỏ qua MT5 (--no-mt5-execute).")
        return

    try:
        ex = execute_trade(
            parsed,
            dry_run=mt5_dry_run,
            symbol_override=mt5_symbol,
        )
        print(ex.message, flush=True)
        _log.info("first_response MT5: %s", ex.message)
        if telegram_bot_token and telegram_output_ngan_gon_chat_id:
            send_mt5_execution_log_to_ngan_gon_chat(
                bot_token=telegram_bot_token,
                output_ngan_gon_chat_id=telegram_output_ngan_gon_chat_id,
                source=telegram_source_label,
                text=format_mt5_execution_for_telegram(ex),
            )
    except SystemExit as e:
        _log.warning("first_response: MT5 không chạy — %s", e)
    except Exception as e:
        _log.exception("first_response: lỗi execute_trade: %s", e)
