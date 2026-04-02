"""Sau phản hồi đầu tiên của phân tích chart: VÀO LỆNH + trade_line → MT5 + cập nhật last_alert_prices."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from automation_tool.mt5_execute import execute_trade, format_mt5_execution_for_telegram
from automation_tool.mt5_openai_parse import ParsedTrade, parse_openai_output_md
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
    Nếu text đủ JSON: đủ 3 vùng giá + ``intraday_hanh_dong`` VÀO LỆNH + ``trade_line`` parse được:
    ghi ``last_alert_prices.json`` (merge), đặt ``vao_lenh`` cho vùng gần entry, optional MT5.

    Bỏ qua khi thiếu triple hoặc không parse được lệnh (ví dụ batch đầu trong luồng nhiều batch).
    """
    text = (first_model_text or "").strip()
    if not text:
        return

    zt, _zerr, nc = parse_three_zone_prices(text)
    if nc is True or zt is None:
        return

    parsed, err = parse_openai_output_md(text, symbol_override=mt5_symbol)
    if err or parsed is None:
        return

    label = plan_label_nearest_trade_entry(parsed, zt)
    try:
        write_last_alert_prices(zt, path=last_alert_path)
        update_single_plan_status(label, VAO_LENH, path=last_alert_path)
    except SystemExit as e:
        _log.warning("first_response: không cập nhật last_alert — %s", e)
        return

    _log.info(
        "first_response: VÀO LỆNH → đã ghi status %s=%s tại %s",
        label,
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
