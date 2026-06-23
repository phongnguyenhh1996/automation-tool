from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from collections.abc import Callable
from pathlib import Path
from typing import Optional, Sequence, TypeVar

from automation_tool.coinmap import capture_charts, load_coinmap_yaml
from automation_tool.gocharting_capture import (
    GOCHARTING_UPDATE_SCALP_DETAIL_HISTORY_STEPS,
    capture_gocharting,
    load_gocharting_yaml,
)
from automation_tool.gocharting_footprint_extract import (
    DEFAULT_FOOTPRINT_EXTRACT_MODEL,
    extract_all_footprint_jsons,
    footprint_json_output_path,
    resolve_instrument_slug,
)
from automation_tool.gocharting_footprint_indexeddb import (
    GoChartingIndexedDBFootprintError,
    require_footprint_json_path,
)
from automation_tool.config import (
    resolve_update_scalp_vector_store_ids,
    default_charts_dir,
    default_coinmap_config_path,
    default_coinmap_update_config_path,
    default_data_dir,
    default_gocharting_config_path,
    default_storage_state_path,
    load_settings,
    require_openai,
    require_telegram,
    resolved_openai_model,
    symbol_data_dir,
)
from automation_tool.openai_analysis_json import (
    extract_json_object,
    format_plan_lines_for_telegram,
    parse_analysis_from_openai_text,
    triple_from_zone_prices,
    try_parse_analysis_payload,
)
from automation_tool.openai_errors import re_raise_unless_openai
from automation_tool.openai_prompt_flow import (
    ALL_FLOW_REASONING_EFFORT,
    DEFAULT_REASONING_EFFORT,
    PromptTwoStepResult,
    build_intraday_update_user_text,
    build_scalp_update_user_text,
    default_analysis_prompt,
    is_first_intraday_update_after_all,
    run_analysis_responses_flow,
    run_single_followup_responses,
)
from automation_tool.images import (
    OPENAI_PAYLOAD_MAX,
    ChartOpenAIPayload,
    coinmap_main_pair_interval_json_path,
    effective_chart_image_order,
    footprint_source_for_stamp,
    gocharting_detail_png_paths,
    gocharting_detail_zoom_png_path_for_csv,
    gocharting_footprint_export_label,
    gocharting_main_interval_csv_path,
    gocharting_png_path_for_csv,
    latest_chart_stamp,
    ordered_chart_images,
    ordered_chart_openai_payloads,
    read_main_chart_symbol,
    stamp_from_capture_paths,
)
from automation_tool.chart_payload_validate import (
    is_coinmap_stale_chart_issue,
    is_gocharting_stale_chart_issue,
    list_invalid_chart_slots_for_stamp,
    require_valid_coinmap_exports_for_stamp,
    require_valid_gocharting_exports_for_stamp,
)
from automation_tool.chart_recapture import (
    recapture_failed_chart_slots,
    recapture_failed_gocharting_slots,
)
from automation_tool.first_response_trade import apply_first_response_vao_lenh
from automation_tool.state_files import (
    default_last_alert_prices_path,
    default_last_all_response_id_path,
    default_last_response_id_path,
    default_last_scalp_response_id_path,
    default_morning_baseline_prices_path,
    default_morning_full_analysis_path,
    merge_trade_lines_from_openai_analysis_text,
    read_last_alert_prices,
    read_last_all_response_id,
    read_last_response_id,
    read_last_scalp_response_id,
    write_last_alert_prices,
    write_last_all_response_id,
    write_last_response_id,
    write_last_scalp_response_id,
    write_morning_baseline_prices,
    write_morning_full_analysis,
)
from automation_tool.zone_prices import (
    is_no_change_action_line,
    parse_update_zone_triple,
)
from automation_tool.tradingview_alerts import sync_tradingview_alerts
from automation_tool.tp1_followup import tp1_dry_run_report
from automation_tool.tradingview_journal_monitor import JournalMonitorParams, run_tv_journal_monitor
from automation_tool.tradingview_watchlist_monitor import (
    WatchlistMonitorParams,
    run_tv_watchlist_monitor,
)
from automation_tool.daemon_launcher import (
    reconcile_daemon_plans_at_boot,
    stop_daemon_plans_in_zones,
    zones_dir_from_cli_path,
)
from automation_tool.tv_watchlist_daemon import WatchlistDaemonParams, run_daemon_plan, run_tv_watchlist_daemon
from automation_tool.zones_paths import SessionSlot, session_slot_now_hcm, shard_path
from automation_tool.zones_state import (
    clear_zones_directory,
    migrate_legacy_zones_state_if_needed,
    remap_scalp_zones_avoiding_shard_collision,
    write_zones_for_slot,
    zones_from_analysis_payload,
    zones_from_scalp_payload,
)
from automation_tool.telegram_bot import (
    send_capture_screenshots_to_log_chat,
    send_message,
    send_mt5_execution_log_to_ngan_gon_chat,
    send_openai_output_to_telegram,
    send_user_friendly_notice,
    split_analysis_json_chi_tiet_ngan_gon,
    split_output_chi_tiet_ngan_gon,
)
from automation_tool.telegram_listen import TelegramListenParams, run_telegram_listener
from automation_tool.telegram_logging import setup_automation_logging
from automation_tool.config import load_all_dotenv
from automation_tool.mt5_openai_parse import parse_openai_output_md
from automation_tool.mt5_accounts import (
    load_mt5_accounts_for_cli,
    resolve_mt5_accounts_path,
    sync_accounts_all2_json,
    sync_accounts_scalp_json,
)
from automation_tool.mt5_execute import check_mt5_login, execute_trade, format_mt5_execution_for_telegram
from automation_tool.mt5_multi import execute_trade_all_accounts, format_mt5_multi_for_telegram

from playwright.sync_api import sync_playwright

from automation_tool.playwright_browser import close_browser_and_context, launch_chrome_context

_log = logging.getLogger("automation_tool.cli")

_HCM = ZoneInfo("Asia/Ho_Chi_Minh")
_ALL_SECOND_FLOW_VECTOR_STORE_ID = "vs_69fa9d55f3b48191b4aea51214b880d6"
_ALL_SECOND_FLOW_TELEGRAM_CHAT_ID = "-1003996623506"


def _now_clock_hcm() -> str:
    """Giờ:phút hiện tại theo Asia/Ho_Chi_Minh (chuỗi hiển thị ngắn)."""
    dt = datetime.now(_HCM)
    return f"{dt.hour}:{dt.minute:02d}"


def _send_python_bot_job_started(
    settings,
    *,
    title: str,
    no_telegram: bool = False,
) -> None:
    """Tin «Bước quan trọng» tới TELEGRAM_PYTHON_BOT_CHAT_ID khi job CLI bắt đầu."""
    if no_telegram:
        return
    tok = (settings.telegram_bot_token or "").strip()
    cid = (settings.telegram_python_bot_chat_id or "").strip()
    if not tok or not cid:
        return
    send_user_friendly_notice(bot_token=tok, chat_id=cid, title=title, body="")


def _telegram_log_technical(settings, text: str) -> None:
    """Best-effort technical log to ``TELEGRAM_LOG_CHAT_ID``."""
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
        pass

_OPENAI_MODEL_HELP = (
    "OpenAI Responses API model id (e.g. gpt-5.2). Overrides OPENAI_MODEL env."
)

_MT5_ACCOUNTS_JSON_HELP = (
    "File accounts.json: nhiều tài khoản MT5 (đăng nhập tuần tự). "
    "Mỗi account có thể có symbol_map (vd. XAUUSD→XAUUSD hoặc XAUUSDm). "
    "Ưu tiên hơn biến môi trường MT5_ACCOUNTS_JSON."
)


def _resolved_mt5_accounts_json(args: argparse.Namespace) -> Optional[Path]:
    p = getattr(args, "mt5_accounts_json", None)
    if p is None:
        return None
    return Path(p).expanduser()


def _maybe_export_gocharting_mt5_spot_candles(
    *,
    charts_dir: Path,
    stamp: str,
    mt5_accounts_json: Optional[Path],
    main_symbol: Optional[str] = None,
) -> Optional[Path]:
    """GoCharting flows: bổ sung JSON 50 nến spot XAUUSD MT5 mới nhất (non-fatal nếu MT5 không sẵn)."""
    from automation_tool.images import read_main_chart_symbol
    from automation_tool.mt5_candles import export_mt5_spot_candles_json

    sym = (main_symbol or read_main_chart_symbol(charts_dir)).strip().upper()
    return export_mt5_spot_candles_json(
        charts_dir=charts_dir,
        stamp=stamp,
        logic_symbol=sym,
        accounts_json=mt5_accounts_json,
    )


def _configure_stdio_utf8() -> None:
    """Windows consoles often use cp1252; OpenAI/Vietnamese output triggers UnicodeEncodeError."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Coinmap chart capture → OpenAI Responses (prompt id) → Telegram bot",
    )
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("capture", help="Log in to Coinmap and save chart screenshots")
    c.add_argument("--config", type=Path, default=None, help="Path to coinmap.yaml")
    c.add_argument("--charts-dir", type=Path, default=None)
    c.add_argument("--storage-state", type=Path, default=None, help="Playwright storage state JSON")
    c.add_argument("--no-save-storage", action="store_true", help="Do not write storage state after run")
    c.add_argument("--headed", action="store_true", help="Show browser window")
    c.add_argument(
        "--main-symbol",
        default=None,
        metavar="SYM",
        help=(
            "Thay cặp mặc định XAUUSD trong config (Coinmap + TradingView chart_url/plan) bằng SYM "
            "(vd. USDJPY). Ghi data/.main_chart_symbol và dùng data/{{SYM}}/charts/."
        ),
    )
    c.add_argument(
        "--use-service",
        action="store_true",
        help=(
            "Bắt buộc có browser service (coinmap-automation browser up): capture qua RPC "
            "(spawn capture_worker attach CDP). Nếu service không chạy thì thoát lỗi."
        ),
    )
    c.set_defaults(func=cmd_capture)

    cm5 = sub.add_parser(
        "capture-m5-footprint",
        aliases=["capture-m5-merged"],
        help=(
            "Capture Coinmap footprint M5 cho cặp chính (raw API export); "
            "không tạo file merged; không gọi OpenAI."
        ),
    )
    cm5.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to coinmap_update.yaml (mặc định: config/coinmap_update.yaml)",
    )
    cm5.add_argument("--charts-dir", type=Path, default=None)
    cm5.add_argument("--storage-state", type=Path, default=None, help="Playwright storage state JSON")
    cm5.add_argument("--no-save-storage", action="store_true", help="Do not write storage state after run")
    cm5.add_argument("--headed", action="store_true", help="Show browser window")
    cm5.add_argument(
        "--main-symbol",
        default=None,
        metavar="SYM",
        help="Cặp cần capture M5 footprint (vd. XAUUSD); mặc định dùng active symbol.",
    )
    cm5.add_argument(
        "--use-service",
        action="store_true",
        help="Bắt buộc attach browser service đang chạy thay vì mở Chrome mới.",
    )
    cm5.set_defaults(func=cmd_capture_m5_footprint)

    agd = sub.add_parser(
        "analyze-gocharting-detail",
        help=(
            "OpenAI gpt-5.4: trích xuất JSON footprint từ ảnh detail GoCharting M5+M15 "
            "(một request) → m5_GC1!_footprint.json, m15_GC1!_footprint.json"
        ),
    )
    agd.add_argument("--charts-dir", type=Path, default=None)
    agd.add_argument(
        "--main-symbol",
        default=None,
        metavar="SYM",
        help="Cặp chính (vd. XAUUSD); mặc định active symbol.",
    )
    agd.add_argument(
        "--stamp",
        default=None,
        metavar="STAMP",
        help="Stamp capture (YYYYMMDD_HHMMSS); mặc định stamp mới nhất trong charts_dir.",
    )
    agd.add_argument(
        "--capture",
        action="store_true",
        help="Capture GoCharting M5+M15 detail trước khi phân tích.",
    )
    agd.add_argument(
        "--gocharting-config",
        type=Path,
        default=None,
        help="YAML GoCharting (default: config/gocharting.yaml)",
    )
    agd.add_argument("--storage-state", type=Path, default=None, help="Playwright storage state JSON")
    agd.add_argument("--no-save-storage", action="store_true", help="Do not write storage state after capture")
    agd.add_argument("--headed", action="store_true", help="Show browser window (with --capture)")
    agd.add_argument(
        "--use-service",
        action="store_true",
        help="Bắt buộc attach browser service khi --capture.",
    )
    agd.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Thư mục ghi JSON footprint (mặc định: charts_dir).",
    )
    agd.add_argument(
        "--indexeddb",
        action="store_true",
        help=(
            "Chỉ trích footprint JSON từ GoCharting IndexedDB (FOOTPRINT/V2). "
            "Bắt buộc thành công — không fallback OpenAI. Dùng với --capture."
        ),
    )
    agd.add_argument(
        "--model",
        default=None,
        metavar="ID",
        help=f"OpenAI model (default: {DEFAULT_FOOTPRINT_EXTRACT_MODEL}).",
    )
    agd.set_defaults(func=cmd_analyze_gocharting_detail)

    a = sub.add_parser(
        "analyze",
        help="OpenAI: một lần gọi multimodal với chart (no capture; system prompt từ system-prompt.md)",
    )
    a.add_argument("--charts-dir", type=Path, default=None)
    a.add_argument(
        "--main-symbol",
        default=None,
        metavar="SYM",
        help="Cặp cần phân tích: ghi data/.main_chart_symbol, đọc data/{{SYM}}/charts và last_alert theo SYM",
    )
    a.add_argument(
        "--prompt",
        type=str,
        default=None,
        help=(
            "Override user message gửi kèm chart; mặc định: tag [FULL_ANALYSIS] + cặp active "
            "(charts-dir / data/.main_chart_symbol). Schema nằm ở OpenAI Prompt / system-prompt.md."
        ),
    )
    a.add_argument(
        "--max-images-per-call",
        type=int,
        default=OPENAI_PAYLOAD_MAX,
        help="Max items per OpenAI call (TradingView images + Coinmap JSON blocks)",
    )
    a.add_argument(
        "--no-telegram",
        action="store_true",
        help="Do not send to Telegram (stdout still shows both steps)",
    )
    a.add_argument(
        "--last-alert-json",
        type=Path,
        default=None,
        metavar="FILE",
        help=(
            "Sau phản hồi đầu tiên: nếu VÀO LỆNH + trade_line + đủ 3 giá JSON, "
            f"cập nhật status (mặc định: {default_last_alert_prices_path()})"
        ),
    )
    a.add_argument(
        "--no-mt5-execute",
        action="store_true",
        help=(
            "Không gọi execute_trade khi phản hồi đầu đủ VÀO LỆNH + trade_line "
            "(mặc định: gọi lệnh thật trên MT5)"
        ),
    )
    a.add_argument("--mt5-symbol", default=None, metavar="SYM", help="Symbol MT5 (phân tích đầu)")
    a.add_argument(
        "--mt5-dry-run",
        action="store_true",
        help="Phản hồi đầu: chỉ mô phỏng MT5, không gửi lệnh thật (mặc định: lệnh thật)",
    )
    a.add_argument(
        "--mt5-accounts-json",
        type=Path,
        default=None,
        metavar="FILE",
        help=_MT5_ACCOUNTS_JSON_HELP,
    )
    a.add_argument(
        "--telegram-detail-chat-id",
        default=None,
        metavar="ID",
        help=(
            "Chat/channel nhận phân tích chấm điểm chi tiết (markers / JSON phan_tich_cham_diem); "
            "mặc định TELEGRAM_ANALYSIS_DETAIL_CHAT_ID trong .env"
        ),
    )
    a.add_argument("--model", default=None, metavar="ID", help=_OPENAI_MODEL_HELP)
    a.set_defaults(func=cmd_analyze)

    tcj = sub.add_parser(
        "test-cloudinary-json",
        help=(
            "Preview OpenAI Responses input cho các file *.json trong charts-dir "
            "(Coinmap JSON = base64 input_file.file_data; JSON khác = input_text)."
        ),
    )
    tcj.add_argument(
        "--charts-dir",
        type=Path,
        default=None,
        help="Thư mục charts (mặc định: data/{{SYM}}/charts theo cặp active)",
    )
    tcj.add_argument(
        "--main-symbol",
        default=None,
        metavar="SYM",
        help="Ghi data/.main_chart_symbol và chọn data/{{SYM}}/charts khi --charts-dir không set",
    )
    tcj.set_defaults(func=cmd_test_cloudinary_json)

    nej = sub.add_parser(
        "upload-ea-neverdie-json",
        help="Upload riêng data/<SYM>/ea_zone_neverdie.json lên Cloudinary cho EA Zone NeverDie.",
    )
    nej.add_argument(
        "--main-symbol",
        default=None,
        metavar="SYM",
        help="Symbol cần upload (mặc định: active symbol; ví dụ XAUUSD).",
    )
    nej.add_argument(
        "--file",
        type=Path,
        default=None,
        metavar="PATH",
        help="File JSON cần upload (mặc định: data/<SYM>/ea_zone_neverdie.json).",
    )
    nej.set_defaults(func=cmd_upload_ea_neverdie_json)

    cm = sub.add_parser(
        "capture-many",
        help="Capture Coinmap + TradingView per symbol (single browser session; same flow as capture)",
    )
    cm.add_argument(
        "--symbols",
        required=True,
        metavar="SYMS",
        help=(
            "Comma-separated symbols (e.g. EURUSD,USDJPY). "
            "Each symbol: Coinmap (bearer/API per config) then TradingView (browser or tvdatafeed per yaml)."
        ),
    )
    cm.add_argument("--config", type=Path, default=None, help="Path to coinmap.yaml")
    cm.add_argument(
        "--storage-state",
        type=Path,
        default=None,
        help="Playwright storage state JSON (shared for the whole run unless overridden)",
    )
    cm.add_argument("--no-save-storage", action="store_true", help="Do not write storage state after run")
    cm.add_argument("--headed", action="store_true", help="Show browser window")
    cm.add_argument(
        "--use-service",
        action="store_true",
        help=(
            "Bắt buộc browser service: capture-many qua RPC (capture_many_worker attach CDP). "
            "Nếu service không chạy thì thoát lỗi."
        ),
    )
    cm.set_defaults(func=cmd_capture_many)

    tvl = sub.add_parser(
        "tvdatafeed-login",
        help="Kiểm tra đăng nhập thư viện tvdatafeed (get_hist thử); không mở browser",
    )
    tvl.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"coinmap.yaml chứa tradingview_capture (mặc định: {default_coinmap_config_path()})",
    )
    tvl.add_argument(
        "--exchange",
        default=None,
        metavar="EX",
        help="Ghi đè sàn (mặc định: chart_url / capture_plan / tvdatafeed.exchange)",
    )
    tvl.add_argument(
        "--symbol",
        default=None,
        metavar="SYM",
        help="Ghi đè symbol (mặc định: chart_url / capture_plan)",
    )
    tvl.add_argument(
        "--interval",
        default=None,
        metavar="LABEL",
        help='Nhãn interval, ví dụ "15 phút" (mặc định: interval đầu trong capture_plan hoặc "15 phút")',
    )
    tvl.add_argument(
        "--n-bars",
        type=int,
        default=3,
        dest="n_bars",
        metavar="N",
        help="Số nến thử get_hist (mặc định: 3)",
    )
    tvl.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="DEBUG logging + traceback đầy đủ nếu get_hist lỗi (tvDatafeed.main, urllib3)",
    )
    tvl.set_defaults(func=cmd_tvdatafeed_login)

    am = sub.add_parser(
        "analyze-many",
        help="OpenAI analyze multiple symbols (parallel, best-effort)",
    )
    am.add_argument(
        "--symbols",
        required=True,
        metavar="SYMS",
        help="Comma-separated symbols (e.g. EURUSD,USDJPY). Each uses data/{SYM}/charts/.",
    )
    am.add_argument(
        "--parallel",
        type=int,
        default=2,
        help="Max concurrent OpenAI calls (default: 2)",
    )
    am.add_argument(
        "--max-images-per-call",
        type=int,
        default=OPENAI_PAYLOAD_MAX,
        help="Max items per OpenAI call (TradingView images + Coinmap JSON blocks)",
    )
    am.add_argument(
        "--prompt",
        type=str,
        default=None,
        help=(
            "Override user message (mọi symbol). Mặc định: tag [FULL_ANALYSIS] theo từng SYM; "
            "chi tiết schema trong Prompt Studio / system-prompt.md."
        ),
    )
    am.add_argument("--no-telegram", action="store_true", help="Do not send results to Telegram")
    am.add_argument(
        "--telegram-detail-chat-id",
        default=None,
        metavar="ID",
        help="Chat/channel for detailed scoring analysis (markers / JSON phan_tich_cham_diem)",
    )
    am.add_argument("--model", default=None, metavar="ID", help=_OPENAI_MODEL_HELP)
    am.set_defaults(func=cmd_analyze_many)

    al = sub.add_parser(
        "all",
        help=(
            "full analysis: chỉ reset zones khi slot sáng (trừ --no-clear-zones-state) → capture → OpenAI → "
            "ghi last_response_id + Telegram + zones_state.json "
            "(không ghi morning_baseline / last_alert_prices)"
        ),
    )
    al.add_argument("--config", type=Path, default=None)
    al.add_argument("--charts-dir", type=Path, default=None)
    al.add_argument("--storage-state", type=Path, default=None)
    al.add_argument("--no-save-storage", action="store_true")
    al.add_argument("--headed", action="store_true")
    al.add_argument(
        "--main-symbol",
        default=None,
        metavar="SYM",
        help="Giống capture: thay XAUUSD bằng SYM và ghi .main_chart_symbol",
    )
    al.add_argument(
        "--prompt",
        type=str,
        default=None,
        help="Override user message; mặc định giống analyze ([FULL_ANALYSIS] + cặp active).",
    )
    al.add_argument(
        "--max-images-per-call",
        type=int,
        default=OPENAI_PAYLOAD_MAX,
        help="Max items per OpenAI call (images + Coinmap JSON)",
    )
    al.add_argument("--no-telegram", action="store_true")
    al.add_argument(
        "--no-tradingview",
        action="store_true",
        help="Skip TradingView monitoring step (không mở/treo TradingView)",
    )
    al.add_argument(
        "--no-tv-journal-monitor",
        action="store_true",
        help=(
            "Không chạy monitor TradingView sau khi đã có 3 vùng giá "
            "(mặc định: luôn chạy khi không set --no-tradingview)"
        ),
    )
    al.add_argument(
        "--capture-config",
        type=Path,
        default=None,
        help="Yaml chụp Coinmap cho journal (mặc định: config/coinmap_update.yaml)",
    )
    al.add_argument(
        "--poll-seconds",
        type=float,
        default=45.0,
        help="(legacy tv-journal-monitor) nghỉ giữa các chu kỳ reload (mặc định: 45)",
    )
    al.add_argument(
        "--wait-minutes",
        type=int,
        default=15,
        help="Monitor: khi model trả Hành động chờ — phút trước khi chụp M5 hỏi lại (mặc định: 15)",
    )
    al.add_argument(
        "--until-hour",
        type=int,
        default=23,
        help="Monitor: dừng theo dõi sau giờ này (địa phương, mặc định: 23)",
    )
    al.add_argument(
        "--timezone",
        type=str,
        default="Asia/Ho_Chi_Minh",
        help="Monitor: IANA timezone cho --until-hour",
    )
    al.add_argument(
        "--zones-json",
        type=Path,
        default=None,
        metavar="FILE",
        help=(
            "all slot sáng: xóa file/thư mục zones này trước capture "
            "(mặc định: data/<SYM>/zones_state.json theo cặp active sau --main-symbol / env)"
        ),
    )
    al.add_argument(
        "--no-clear-zones-state",
        action="store_true",
        help=(
            "Không xóa zones_state.json trước capture/phân tích "
            "(mặc định: chỉ xóa khi `all` chạy trong slot sáng)"
        ),
    )
    al.add_argument(
        "--gocharting",
        action="store_true",
        help="Dùng GoCharting (PNG+CSV) thay Coinmap footprint",
    )
    al.add_argument(
        "--gocharting-config",
        type=Path,
        default=None,
        help="YAML GoCharting (default: config/gocharting.yaml)",
    )
    al.add_argument(
        "--mt5-accounts-json",
        type=Path,
        default=None,
        metavar="FILE",
        help="accounts.json nguồn để tạo accounts-all2.json sau luồng 2 (mặc định MT5_ACCOUNTS_JSON)",
    )
    al.add_argument("--model", default=None, metavar="ID", help=_OPENAI_MODEL_HELP)
    al.set_defaults(func=cmd_all)

    al2 = sub.add_parser(
        "all-2",
        help=(
            "Chạy riêng luồng OpenAI thứ hai của `all` (vector store + Telegram nhóm 2), "
            "dùng chart đã capture — ghi shard *-2.json source=all-2; không capture"
        ),
    )
    al2.add_argument("--charts-dir", type=Path, default=None)
    al2.add_argument(
        "--main-symbol",
        default=None,
        metavar="SYM",
        help="Cặp active: ghi data/.main_chart_symbol và đọc charts theo SYM",
    )
    al2.add_argument(
        "--stamp",
        default=None,
        metavar="STAMP",
        help="Prefix capture YYYYMMDD_HHMMSS (mặc định: stamp mới nhất trong charts-dir)",
    )
    al2.add_argument(
        "--prompt",
        type=str,
        default=None,
        help="Override user message; mặc định giống `all` ([FULL_ANALYSIS] + cặp active).",
    )
    al2.add_argument(
        "--max-images-per-call",
        type=int,
        default=OPENAI_PAYLOAD_MAX,
        help="Max items per OpenAI call (images + Coinmap JSON)",
    )
    al2.add_argument("--no-telegram", action="store_true")
    al2.add_argument(
        "--zones-json",
        type=Path,
        default=None,
        metavar="FILE",
        help="Thư mục zones shard cho all-2 (mặc định data/<SYM>/zones/)",
    )
    al2.add_argument(
        "--mt5-accounts-json",
        type=Path,
        default=None,
        metavar="FILE",
        help="accounts.json nguồn để tạo accounts-all2.json (mặc định MT5_ACCOUNTS_JSON)",
    )
    al2.add_argument("--model", default=None, metavar="ID", help=_OPENAI_MODEL_HELP)
    al2.set_defaults(func=cmd_all_2)

    tl = sub.add_parser(
        "telegram-listen",
        help=(
            "Listen inbound Telegram messages in a channel/group (poll getUpdates). "
            "Supports /full, /update, /tim-scalp, /loai, /stop, /analyze-many, /ask, /ask-high."
        ),
    )
    tl.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=0.5,
        help="Sleep between polls when idle (default: 0.5)",
    )
    tl.add_argument(
        "--long-poll-timeout-seconds",
        type=int,
        default=45,
        help="Telegram long poll timeout passed to getUpdates (default: 45)",
    )
    tl.add_argument(
        "--full-main-symbol",
        type=str,
        default="XAUUSD",
        help="Symbol used when /full triggers (default: XAUUSD). On Windows runs run_daily.bat.",
    )
    tl.add_argument(
        "--update-main-symbol",
        type=str,
        default="XAUUSD",
        help=(
            "Symbol used when /update and /loai <zone_id> trigger (default: XAUUSD). "
            "On Windows /update runs run_update.bat."
        ),
    )
    tl.add_argument("--model", default=None, metavar="ID", help=_OPENAI_MODEL_HELP)
    tl.set_defaults(func=cmd_telegram_listen)

    up = sub.add_parser(
        "update",
        help=(
            "Intraday: TradingView 15m ICT + 5m rồi Coinmap M5 JSON → OpenAI follow-up (same thread); "
            "sau đó tv-watchlist-monitor"
        ),
    )
    up.add_argument("--config", type=Path, default=None, help="Coinmap yaml for capture only (default: coinmap_update.yaml)")
    up.add_argument(
        "--tv-config",
        type=Path,
        default=None,
        help="Yaml chứa tradingview_capture cho monitor (default: config/coinmap.yaml)",
    )
    up.add_argument("--charts-dir", type=Path, default=None)
    up.add_argument("--storage-state", type=Path, default=None)
    up.add_argument("--no-save-storage", action="store_true")
    up.add_argument("--headed", action="store_true")
    up.add_argument(
        "--main-symbol",
        default=None,
        metavar="SYM",
        help="Giống capture: thay XAUUSD trong yaml chụp (mặc định coinmap_update.yaml)",
    )
    up.add_argument("--no-telegram", action="store_true")
    up.add_argument(
        "--no-tradingview",
        action="store_true",
        help="Bỏ qua chụp TradingView (15m ICT + 5m) trước Coinmap M5; chỉ export Coinmap",
    )
    up.add_argument(
        "--no-journal-monitor-after-update",
        action="store_true",
        help=(
            "Không chạy tv-watchlist-monitor sau update (kể cả no_change / giá trùng baseline; "
            "mặc định: luôn chạy)"
        ),
    )
    up.add_argument(
        "--last-alert-json",
        type=Path,
        default=None,
        metavar="FILE",
        help="File last_alert_prices.json cho ghi giá + auto-MT5 sau follow-up (mặc định: data/last_alert_prices.json)",
    )
    up.add_argument(
        "--no-mt5-execute",
        action="store_true",
        help=(
            "Không gọi execute_trade khi follow-up có vùng đủ hop_luu (plan_chinh>70, plan_phu>65, scalp>=60) + trade_line (mặc định: gọi; cần Windows + MetaTrader5)"
        ),
    )
    up.add_argument(
        "--mt5-symbol",
        default=None,
        metavar="SYM",
        help="Symbol MT5 (ghi đè parse từ trade_line) cho auto-MT5 sau follow-up",
    )
    up.add_argument(
        "--mt5-dry-run",
        action="store_true",
        help="Chỉ dry-run MT5 cho auto-MT5 sau follow-up",
    )
    up.add_argument(
        "--mt5-accounts-json",
        type=Path,
        default=None,
        metavar="FILE",
        help=_MT5_ACCOUNTS_JSON_HELP,
    )
    up.add_argument("--model", default=None, metavar="ID", help=_OPENAI_MODEL_HELP)
    up.add_argument(
        "--zones-json",
        type=Path,
        default=None,
        metavar="PATH",
        help="Thư mục zones (shard) hoặc legacy zones_state.json — mặc định data/<SYM>/zones/",
    )
    up.set_defaults(func=cmd_update)

    # --- update-scalp: same as update but asks for best scalp plan; scalp_<id> labels ---
    ups = sub.add_parser(
        "update-scalp",
        help=(
            "Scalp intraday: TradingView 15m ICT + 5m rồi Coinmap M15 + M5 (JSON + PNG) → OpenAI tìm plan scalp; "
            "vector store từ OPENAI_UPDATE_SCALP_VECTOR_STORE_ID(S) hoặc mặc định all-2; "
            "zones lưu vào data/<SYM>/zones/ với label scalp_<id>."
        ),
    )
    ups.add_argument("--config", type=Path, default=None, help="Coinmap yaml for capture only (default: coinmap_update.yaml)")
    ups.add_argument(
        "--tv-config",
        type=Path,
        default=None,
        help="Yaml chứa tradingview_capture cho bước TV (default: config/coinmap.yaml)",
    )
    ups.add_argument("--charts-dir", type=Path, default=None)
    ups.add_argument("--storage-state", type=Path, default=None)
    ups.add_argument("--no-save-storage", action="store_true")
    ups.add_argument("--headed", action="store_true")
    ups.add_argument(
        "--main-symbol",
        default=None,
        metavar="SYM",
        help="Giống capture: thay XAUUSD trong yaml chụp (mặc định coinmap_update.yaml)",
    )
    ups.add_argument("--no-telegram", action="store_true")
    ups.add_argument(
        "--no-tradingview",
        action="store_true",
        help="Bỏ qua chụp TradingView (15m ICT + 5m) trước Coinmap; chỉ export Coinmap",
    )
    ups.add_argument(
        "--zones-json",
        type=Path,
        default=None,
        metavar="PATH",
        help="Thư mục zones (mặc định: data/<SYM>/zones/) — cùng thư mục với zones thông thường",
    )
    ups.add_argument("--model", default=None, metavar="ID", help=_OPENAI_MODEL_HELP)
    ups.add_argument(
        "--mt5-accounts-json",
        type=Path,
        default=None,
        metavar="FILE",
        help=(
            "accounts.json nguồn: ghi accounts-scalp.json (cùng thư mục) chỉ các dòng "
            '"update-scalp": true; đặt MT5_ACCOUNTS_JSON cho tiến trình này và reconcile. '
            "Ưu tiên hơn biến môi trường MT5_ACCOUNTS_JSON khi chỉ định."
        ),
    )
    ups.add_argument(
        "--gocharting",
        action="store_true",
        help="Dùng GoCharting (PNG+CSV) thay Coinmap M15+M5",
    )
    ups.add_argument(
        "--gocharting-config",
        type=Path,
        default=None,
        help="YAML GoCharting (default: config/gocharting.yaml)",
    )
    ups.add_argument(
        "--no-reconcile-daemon-plans",
        action="store_true",
        help="Sau khi ghi zones: không gọi reconcile-daemon-plans (mặc định: có gọi).",
    )
    ups.set_defaults(func=cmd_update_scalp)

    rmz = sub.add_parser(
        "restore-morning-zones",
        help="Tạo lại shard zones từ data/<SYM>/morning_full_analysis.json đã lưu trước đó.",
    )
    rmz.add_argument(
        "--main-symbol",
        default=None,
        metavar="SYM",
        help="Cặp cần khôi phục zones (vd. XAUUSD); mặc định dùng active symbol hiện tại.",
    )
    rmz.add_argument(
        "--morning-json",
        type=Path,
        default=None,
        metavar="FILE",
        help="File morning_full_analysis.json nguồn (mặc định data/<SYM>/morning_full_analysis.json).",
    )
    rmz.add_argument(
        "--zones-json",
        type=Path,
        default=None,
        metavar="PATH",
        help="Thư mục zones (mặc định: data/<SYM>/zones/) hoặc legacy zones_state.json companion dir.",
    )
    rmz.add_argument(
        "--slot",
        choices=("sang", "chieu", "toi"),
        default="sang",
        help="Session slot để ghi shard zones (mặc định: sang).",
    )
    rmz.set_defaults(func=cmd_restore_morning_zones)

    wd = sub.add_parser(
        "tv-watchlist-daemon",
        help=(
            "Daemon giá: mặc định đọc MT5 bid → shared memory / optional last.txt; "
            "``--tv-symbol-price`` = đọc Last từ TradingView symbol page (legacy, cần browser). "
            "``daemon-plan`` đọc Last đó (IPC). Sau Last đầu tiên: reconcile-daemon-plans."
        ),
    )
    wd.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Yaml chứa tradingview_capture.chart_url (mặc định: config/coinmap.yaml)",
    )
    wd.add_argument(
        "--capture-config",
        type=Path,
        default=None,
        help="Yaml chụp Coinmap M5 cho job (mặc định: config/coinmap_update.yaml)",
    )
    wd.add_argument("--charts-dir", type=Path, default=None)
    wd.add_argument("--storage-state", type=Path, default=None)
    wd.add_argument("--no-save-storage", action="store_true")
    wd.add_argument("--headed", action="store_true")
    wd.add_argument("--no-telegram", action="store_true")
    wd.add_argument("--poll-seconds", type=float, default=1.0)
    wd.add_argument(
        "--mt5-stale-reconnect-seconds",
        type=float,
        default=10.0,
        metavar="SEC",
        help=(
            "Daemon giá (MT5 bid): nếu bid không đổi trong SEC giây thì gọi lại "
            "ensure_mt5_session() để re-ensure primary. 0 = tắt. Mặc định 10."
        ),
    )
    wd.add_argument(
        "--eps",
        type=float,
        default=0.0,
        metavar="X",
        help="Sau làm tròn nguyên: chạm nếu |Last−ref|≤X (mặc định 0.0 → trùng số nguyên sau làm tròn)",
    )
    wd.add_argument(
        "--last-price-file",
        type=Path,
        default=None,
        metavar="FILE",
        help="Khi dùng với --mirror-last-price-file: đường dẫn mirror (mặc định data/<SYM>/last.txt)",
    )
    wd.add_argument(
        "--mirror-last-price-file",
        action="store_true",
        help="Ngoài shared memory, ghi thêm atomic last.txt (tùy chọn debug / tương thích cũ)",
    )
    wd.add_argument(
        "--tv-title-price",
        action="store_true",
        help=(
            "DEPRECATED: trước đây đọc Last từ title TradingView. Hiện daemon giá TradingView "
            "mặc định đọc từ trang symbol (RPC). Flag này giữ lại để tương thích, không còn tác dụng."
        ),
    )
    wd.add_argument(
        "--mt5-bid-price",
        action="store_true",
        help="DEPRECATED/no-op: MT5 bid đã là mặc định.",
    )
    wd.add_argument(
        "--tv-symbol-price",
        action="store_true",
        help="Legacy opt-in: đọc Last từ TradingView symbol page qua browser RPC thay vì MT5 bid.",
    )
    wd.add_argument(
        "--stop-daemon-plans-on-exit",
        action="store_true",
        help="Khi thoát tiến trình (Ctrl+C, đóng cửa sổ CMD trên Windows, …): dừng các daemon-plan trong zones/",
    )
    wd.add_argument("--no-mt5-execute", action="store_true")
    wd.add_argument("--mt5-symbol", default=None, metavar="SYM")
    wd.add_argument("--mt5-dry-run", action="store_true")
    wd.add_argument(
        "--mt5-accounts-json",
        type=Path,
        default=None,
        metavar="FILE",
        help=_MT5_ACCOUNTS_JSON_HELP,
    )
    wd.add_argument("--model", default=None, metavar="ID", help=_OPENAI_MODEL_HELP)
    wd.set_defaults(func=cmd_tv_watchlist_daemon)

    zt = sub.add_parser(
        "zone-touch",
        help="Chạy 1 job touch cho một zone_id (Coinmap M5 + OpenAI + MT5/Telegram).",
    )
    zt.add_argument("--zone-id", required=True, metavar="ID")
    zt.add_argument("--last", type=float, required=True, metavar="PRICE", help="Watchlist Last tại thời điểm chạm")
    zt.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Yaml chứa tradingview_capture (chỉ để đồng bộ params; mặc định: config/coinmap.yaml)",
    )
    zt.add_argument(
        "--capture-config",
        type=Path,
        default=None,
        help="Yaml chụp Coinmap M5 cho job (mặc định: config/coinmap_update.yaml)",
    )
    zt.add_argument("--charts-dir", type=Path, default=None)
    zt.add_argument("--storage-state", type=Path, default=None)
    zt.add_argument("--no-save-storage", action="store_true")
    zt.add_argument("--headed", action="store_true")
    zt.add_argument("--no-telegram", action="store_true")
    zt.add_argument("--zones-json", type=Path, default=None, metavar="FILE", help="zones_state.json path override")
    zt.add_argument("--no-mt5-execute", action="store_true")
    zt.add_argument("--mt5-symbol", default=None, metavar="SYM")
    zt.add_argument("--mt5-dry-run", action="store_true")
    zt.add_argument(
        "--mt5-accounts-json",
        type=Path,
        default=None,
        metavar="FILE",
        help=_MT5_ACCOUNTS_JSON_HELP,
    )
    zt.add_argument("--model", default=None, metavar="ID", help=_OPENAI_MODEL_HELP)
    zt.set_defaults(func=cmd_zone_touch)

    dp = sub.add_parser(
        "daemon-plan",
        help=(
            "Một process / file shard: Last từ shared memory / last.txt (MT5 bid do daemon giá ghi); "
            "cập nhật zone tuần tự; thoát khi done/loại hoặc đến giờ cắt tự động theo shard "
            "(mặc định: sáng 02:00/02:01/02:02, chiều +3 phút, tối +6 phút; thứ Sáu base 01:00 hôm sau): "
            "lệnh chờ → huỷ; chỉ chờ khi còn position đã khớp. "
            "Chạy ``tv-watchlist-daemon`` (giá) cùng máy; cutoff ticket vẫn cần MT5."
        ),
    )
    dp.add_argument(
        "--shard",
        type=Path,
        required=True,
        metavar="PATH",
        help="File JSON vung_{label}_{slot}.json",
    )
    dp.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Yaml coinmap (ký hiệu; daemon-plan không mở TradingView — Last từ IPC)",
    )
    dp.add_argument("--capture-config", type=Path, default=None)
    dp.add_argument("--charts-dir", type=Path, default=None)
    dp.add_argument("--storage-state", type=Path, default=None)
    dp.add_argument("--no-save-storage", action="store_true")
    dp.add_argument("--headed", action="store_true")
    dp.add_argument("--no-telegram", action="store_true")
    dp.add_argument("--poll-seconds", type=float, default=5.0)
    dp.add_argument(
        "--timezone",
        default="Asia/Ho_Chi_Minh",
        help="Múi giờ IANA cho --stop-at-hour (mặc định: Asia/Ho_Chi_Minh)",
    )
    dp.add_argument(
        "--stop-at-hour",
        type=int,
        default=None,
        metavar="H",
        help=(
            "Override mốc cắt giờ local (kèm --stop-at-minute). "
            "Mặc định không truyền = auto theo shard; 0 = 12h đêm (00:00 ngày kế, tức 24h); "
            "1-23 = giờ trong ngày; -1 = tắt cắt giờ."
        ),
    )
    dp.add_argument(
        "--stop-at-minute",
        type=int,
        default=0,
        metavar="M",
        help="Phút đi kèm --stop-at-hour (mặc định 0).",
    )
    dp.add_argument(
        "--eps",
        type=float,
        default=0.0,
        metavar="X",
        help="Sau làm tròn nguyên: chạm nếu |Last−ref|≤X (mặc định 0.0 → trùng số nguyên sau làm tròn)",
    )
    dp.add_argument(
        "--last-price-file",
        type=Path,
        default=None,
        metavar="FILE",
        help="Fallback file last.txt nếu chưa có shared memory (mặc định data/<SYM>/last.txt)",
    )
    dp.add_argument("--no-mt5-execute", action="store_true")
    dp.add_argument("--mt5-symbol", default=None, metavar="SYM")
    dp.add_argument("--mt5-dry-run", action="store_true")
    dp.add_argument(
        "--mt5-accounts-json",
        type=Path,
        default=None,
        metavar="FILE",
        help=_MT5_ACCOUNTS_JSON_HELP,
    )
    dp.add_argument("--model", default=None, metavar="ID", help=_OPENAI_MODEL_HELP)
    dp.set_defaults(func=cmd_daemon_plan)

    rec = sub.add_parser(
        "reconcile-daemon-plans",
        help="Quét thư mục zones và spawn daemon-plan cho shard chưa terminal / chưa có PID.",
    )
    rec.add_argument(
        "--zones-json",
        type=Path,
        default=None,
        metavar="PATH",
        help="Thư mục zones (mặc định data/<SYM>/zones/)",
    )
    rec.set_defaults(func=cmd_reconcile_daemon_plans)

    sdp = sub.add_parser(
        "stop-daemon-plans",
        help="Gửi SIGTERM tới mọi daemon-plan đang track (file .daemon-plan-*.pid) trong thư mục zones.",
    )
    sdp.add_argument(
        "--zones-json",
        type=Path,
        default=None,
        metavar="PATH",
        help="Thư mục zones (mặc định data/<SYM>/zones/)",
    )
    sdp.set_defaults(func=cmd_stop_daemon_plans)

    tv = sub.add_parser(
        "tv-alerts",
        help="Đồng bộ 3 cảnh báo giá lên TradingView (cần chart_url trong yaml)",
    )
    tv.add_argument(
        "p1",
        type=float,
        nargs="?",
        default=None,
        help="Giá 1 (bỏ qua nếu dùng --prices-json / --from-last-alert)",
    )
    tv.add_argument("p2", type=float, nargs="?", default=None, help="Giá 2")
    tv.add_argument("p3", type=float, nargs="?", default=None, help="Giá 3")
    tv.add_argument(
        "--prices-json",
        type=Path,
        default=None,
        metavar="FILE",
        help="JSON có key 'prices': [a,b,c] (cùng format data/last_alert_prices.json)",
    )
    tv.add_argument(
        "--from-last-alert",
        action="store_true",
        help=f"Đọc 3 giá từ {default_last_alert_prices_path()}",
    )
    tv.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Yaml có tradingview_capture.chart_url (mặc định: config/coinmap.yaml)",
    )
    tv.add_argument("--storage-state", type=Path, default=None)
    tv.add_argument("--headed", action="store_true", help="Hiện cửa sổ trình duyệt")
    tv.set_defaults(func=cmd_tv_alerts)

    tj = sub.add_parser(
        "tv-journal-monitor",
        help=(
            "Sau khi đã có cảnh báo TV: mỗi chu kỳ reload chart, tab Nhật ký, parse giá; "
            "khớp 1 trong 3 giá → Coinmap M5 + OpenAI (chờ / loại / VÀO LỆNH) tới giờ kết thúc."
        ),
    )
    tj.add_argument(
        "--p1",
        type=float,
        default=None,
        help="Giá vùng 1 (dùng cùng --p2 --p3; mặc định: data/last_alert_prices.json)",
    )
    tj.add_argument("--p2", type=float, default=None)
    tj.add_argument("--p3", type=float, default=None)
    tj.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Yaml có tradingview_capture (mặc định: config/coinmap.yaml)",
    )
    tj.add_argument(
        "--capture-config",
        type=Path,
        default=None,
        help="Yaml chỉ chụp Coinmap (mặc định: config/coinmap_update.yaml)",
    )
    tj.add_argument("--charts-dir", type=Path, default=None)
    tj.add_argument("--storage-state", type=Path, default=None)
    tj.add_argument("--no-save-storage", action="store_true")
    tj.add_argument("--headed", action="store_true")
    tj.add_argument("--no-telegram", action="store_true")
    tj.add_argument(
        "--poll-seconds",
        type=float,
        default=45.0,
        help=(
            "Sau mỗi lần reload trang + mở tab Nhật ký + parse giá: chờ bấy nhiêu giây "
            "trước chu kỳ tiếp (mặc định: 45)"
        ),
    )
    tj.add_argument(
        "--wait-minutes",
        type=int,
        default=15,
        help="Khi model trả Hành động: chờ — chờ bao nhiêu phút trước khi chụp M5 hỏi lại (mặc định: 15)",
    )
    tj.add_argument("--until-hour", type=int, default=23, help="Dừng theo dõi sau giờ này (địa phương)")
    tj.add_argument(
        "--timezone",
        type=str,
        default="Asia/Ho_Chi_Minh",
        help="IANA timezone cho --until-hour (mặc định: Asia/Ho_Chi_Minh)",
    )
    tj.add_argument(
        "--last-alert-json",
        type=Path,
        default=None,
        metavar="FILE",
        help="File last_alert_prices.json (mặc định: data/last_alert_prices.json)",
    )
    tj.add_argument(
        "--no-mt5-execute",
        action="store_true",
        help=(
            "Không gọi execute_trade sau VÀO LỆNH + trade_line "
            "(mặc định: luôn gọi; cần Windows + MetaTrader5 pip)"
        ),
    )
    tj.add_argument("--mt5-symbol", default=None, metavar="SYM", help="Symbol MT5")
    tj.add_argument(
        "--mt5-dry-run",
        action="store_true",
        help="Chỉ dry-run MT5 (mặc định: gửi lệnh thật)",
    )
    tj.add_argument(
        "--mt5-accounts-json",
        type=Path,
        default=None,
        metavar="FILE",
        help=_MT5_ACCOUNTS_JSON_HELP,
    )
    tj.add_argument("--model", default=None, metavar="ID", help=_OPENAI_MODEL_HELP)
    tj.set_defaults(func=cmd_tv_journal_monitor)

    tp1d = sub.add_parser(
        "tp1-tick-dry-run",
        help=(
            "In so khớp ±5 / chạm TP1 từ last_alert + một giá Last (không browser, không OpenAI). "
            "Dùng để kiểm tra local."
        ),
    )
    tp1d.add_argument(
        "--last",
        type=float,
        required=True,
        metavar="PRICE",
        help="Giá Last realtime (cùng quy ước với watchlist monitor)",
    )
    tp1d.add_argument(
        "--last-alert-json",
        type=Path,
        default=None,
        metavar="FILE",
        help=f"File last_alert_prices.json (mặc định: {default_last_alert_prices_path()})",
    )
    tp1d.add_argument(
        "--mt5-symbol",
        default=None,
        metavar="SYM",
        help="Symbol override khi parse trade_line (giống các lệnh khác)",
    )
    tp1d.add_argument(
        "--mt5-accounts-json",
        type=Path,
        default=None,
        metavar="FILE",
        help=_MT5_ACCOUNTS_JSON_HELP,
    )
    tp1d.set_defaults(func=cmd_tp1_tick_dry_run)

    g = sub.add_parser(
        "chatgpt-project",
        help="Same as analyze: prompt id + multimodal Responses API",
    )
    g.add_argument(
        "--prompt",
        type=str,
        default=None,
        help="User message; default: [FULL_ANALYSIS] theo cặp active (schema trong Prompt / system-prompt.md).",
    )
    g.add_argument("--charts-dir", type=Path, default=None)
    g.add_argument(
        "--main-symbol",
        default=None,
        metavar="SYM",
        help="Giống analyze: cặp và thư mục data/{{SYM}}/",
    )
    g.add_argument(
        "--max-images-per-call",
        type=int,
        default=OPENAI_PAYLOAD_MAX,
        help="Max items per OpenAI call (images + Coinmap JSON)",
    )
    g.add_argument(
        "--no-telegram",
        action="store_true",
        help="Do not send chart-step output to Telegram",
    )
    g.add_argument(
        "--last-alert-json",
        type=Path,
        default=None,
        metavar="FILE",
        help="Giống analyze: cập nhật last_alert_prices khi VÀO LỆNH ở phản hồi đầu",
    )
    g.add_argument(
        "--no-mt5-execute",
        action="store_true",
        help="Giống analyze: không gọi MT5 từ phản hồi đầu",
    )
    g.add_argument("--mt5-symbol", default=None, metavar="SYM")
    g.add_argument(
        "--mt5-dry-run",
        action="store_true",
        help="Giống analyze: dry-run MT5 (mặc định: lệnh thật)",
    )
    g.add_argument(
        "--mt5-accounts-json",
        type=Path,
        default=None,
        metavar="FILE",
        help=_MT5_ACCOUNTS_JSON_HELP,
    )
    g.add_argument(
        "--telegram-detail-chat-id",
        default=None,
        metavar="ID",
        help="Giống analyze: kênh nhận bản chi tiết",
    )
    g.add_argument("--model", default=None, metavar="ID", help=_OPENAI_MODEL_HELP)
    g.set_defaults(func=cmd_chatgpt_project)

    t = sub.add_parser("telegram-send", help="Send a text message to Telegram (uses .env token and chat id)")
    t.add_argument("message", help="Text to send")
    t.add_argument(
        "--parse-mode",
        choices=("HTML", "Markdown", "MarkdownV2"),
        default=None,
        help="Optional Telegram parse mode",
    )
    t.set_defaults(func=cmd_telegram_send)

    mt5 = sub.add_parser(
        "mt5-trade",
        help=(
            "OpenAI .md → MetaTrader5. Mặc định gửi lệnh thật (Windows + MT5). Dùng --dry-run trên Mac/dev."
        ),
    )
    mt5.add_argument(
        "--file",
        type=Path,
        required=True,
        help="File .md (ví dụ output từ OpenAI)",
    )
    mt5.add_argument(
        "--symbol",
        default=None,
        help="Symbol MT5 (mặc định: từ 📊 trong text hoặc XAUUSD → tự đổi XAUUSDm)",
    )
    mt5.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Không gửi lệnh thật (mô phỏng). Mặc định: gửi lệnh thật qua terminal MT5 "
            "(VPS: có thể để trống MT5_LOGIN nếu đã đăng nhập sẵn)."
        ),
    )
    mt5.add_argument(
        "--lot",
        type=float,
        default=None,
        metavar="SIZE",
        help="Ghi đè khối lượng từ file .md (vd. 0.01 để test nhỏ hơn 0.02).",
    )
    mt5.add_argument(
        "--mt5-accounts-json",
        type=Path,
        default=None,
        metavar="FILE",
        help=_MT5_ACCOUNTS_JSON_HELP,
    )
    mt5.set_defaults(func=cmd_mt5_trade)

    mt5l = sub.add_parser(
        "mt5-login",
        help="Kiểm tra kết nối MetaTrader5 (initialize + account_info). Windows + pip MetaTrader5.",
    )
    mt5l.add_argument(
        "--login",
        type=int,
        default=None,
        help="Ghi đè MT5_LOGIN (kèm --password và --server)",
    )
    mt5l.add_argument("--password", default=None, help="Ghi đè MT5_PASSWORD")
    mt5l.add_argument("--server", default=None, help="Ghi đè MT5_SERVER")
    mt5l.set_defaults(func=cmd_mt5_login)

    mt5la = sub.add_parser(
        "mt5-login-all",
        help="Kết nối tất cả accounts.json song song và in terminal_info()/account_info() cho từng account.",
    )
    mt5la.add_argument(
        "--mt5-accounts-json",
        type=Path,
        default=None,
        metavar="FILE",
        help=_MT5_ACCOUNTS_JSON_HELP,
    )
    mt5la.add_argument(
        "--concurrency",
        type=int,
        default=0,
        metavar="N",
        help="Số kết nối chạy song song (default: tất cả accounts). Nếu lỗi lặt vặt, thử giảm xuống 1-2.",
    )
    mt5la.set_defaults(func=cmd_mt5_login_all_async)

    br = sub.add_parser(
        "browser",
        help="Browser worker service: long-lived Playwright + TCP control (see data/browser_service_state.json)",
    )
    br_sub = br.add_subparsers(dest="browser_cmd", required=True)
    br_up = br_sub.add_parser("up", help="Start browser service in background (detached process)")
    br_up.set_defaults(func=cmd_browser_up)
    br_down = br_sub.add_parser("down", help="Stop browser service (shutdown + SIGTERM if needed)")
    br_down.set_defaults(func=cmd_browser_down)
    br_ex = br_sub.add_parser("exec", help="Send one JSON-RPC request to the control TCP port")
    br_ex.add_argument(
        "request_json",
        nargs="?",
        default=None,
        help='JSON object, e.g. {"method":"ping","params":{}} (omit request_id/type)',
    )
    br_ex.set_defaults(func=cmd_browser_exec)
    br_tail = br_sub.add_parser(
        "tail",
        help="Keep pinging the service every few seconds until Ctrl+C (smoke / watch)",
    )
    br_tail.add_argument(
        "--interval",
        type=float,
        default=5.0,
        metavar="SEC",
        help="Seconds between pings (default: 5)",
    )
    br_tail.set_defaults(func=cmd_browser_tail)

    return p


def _parse_symbols_arg(raw: str | Sequence[str]) -> list[str]:
    """
    Normalize symbols list from a comma-separated string (or repeated values in the future).
    """
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
    else:
        parts = [str(p).strip() for p in raw]
    out: list[str] = []
    for p in parts:
        if not p:
            continue
        out.append(p.upper())
    if not out:
        raise SystemExit("No symbols provided. Use --symbols EURUSD,USDJPY")
    # De-dupe while preserving order.
    seen: set[str] = set()
    uniq: list[str] = []
    for s in out:
        if s in seen:
            continue
        seen.add(s)
        uniq.append(s)
    return uniq


def cmd_browser_up(args: argparse.Namespace) -> None:
    load_all_dotenv()
    from automation_tool.browser_client import (
        browser_service_log_path,
        is_service_responding,
        load_browser_service_state,
        spawn_browser_service_detached,
        wait_for_service_ping,
    )

    def _log_browser_up_ready(st: Optional[dict], *, note: str) -> None:
        cdp = str((st or {}).get("cdp_http") or "")
        ctrl = str((st or {}).get("control_tcp") or "")
        pid = (st or {}).get("pid")
        _log.info(
            "browser up: ready | note=%s | pid=%s | cdp_http=%s | control_tcp=%s",
            note,
            pid,
            cdp,
            ctrl,
        )

    def _browser_service_log_tail(log_path: Path, *, limit: int = 4000) -> str:
        try:
            raw = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return raw[-limit:]

    def _wait_for_state_or_spawn_exit(proc, *, timeout_s: float = 90.0, poll_s: float = 0.25) -> Optional[dict]:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            st = load_browser_service_state()
            if st and st.get("cdp_http"):
                return st
            if proc.poll() is not None:
                time.sleep(max(0.05, poll_s))
                return load_browser_service_state()
            time.sleep(poll_s)
        return None

    if is_service_responding():
        print("Browser service already running (ping ok).", flush=True)
        st = load_browser_service_state()
        if st:
            print(json.dumps(st, indent=2), flush=True)
        _log_browser_up_ready(st, note="already_running")
        return

    # Concurrent ``browser up`` (e.g. two .bat): peer may be between spawn and first ping.
    if wait_for_service_ping(timeout_s=20.0):
        print("Browser service already running (ping ok after brief wait).", flush=True)
        st = load_browser_service_state()
        if st:
            print(json.dumps(st, indent=2), flush=True)
        _log_browser_up_ready(st, note="already_running_after_wait")
        return

    log_path = browser_service_log_path(cwd=Path.cwd())
    proc = spawn_browser_service_detached(cwd=Path.cwd())
    st = _wait_for_state_or_spawn_exit(proc, timeout_s=90.0)
    if not st:
        proc.kill()
        err = _browser_service_log_tail(log_path)
        # Lost lock race: another process owns the service; state may still appear.
        if is_service_responding():
            print("Browser service already running (another process holds the lock).", flush=True)
            st2 = load_browser_service_state()
            if st2:
                print(json.dumps(st2, indent=2), flush=True)
            _log_browser_up_ready(st2, note="already_running_lock_race")
            return
        raise SystemExit(
            "Browser service did not write state file in time. "
            f"Check PLAYWRIGHT_CHROME_USER_DATA_DIR / Playwright install. "
            f"log: {log_path} tail: {err!r}"
        )

    # Our subprocess may have lost the exclusive lock and exited; peer still wrote state.
    if proc.poll() is not None and proc.returncode not in (0,):
        if is_service_responding():
            print("Browser service already running (spawn exited; peer owns service).", flush=True)
            st3 = load_browser_service_state()
            if st3:
                print(json.dumps(st3, indent=2), flush=True)
            _log_browser_up_ready(st3, note="already_running_peer_spawn_exited")
            return

    # State file can appear before the TCP control plane accepts connections; wait for ping.
    if not wait_for_service_ping(timeout_s=45.0):
        raise SystemExit(
            "Browser service wrote state but control plane did not respond to ping in time. "
            f"Check log: {log_path} tail: {_browser_service_log_tail(log_path)!r}"
        )
    st = load_browser_service_state() or st
    print("Browser service ready.", flush=True)
    print(json.dumps(st, indent=2), flush=True)
    _log_browser_up_ready(st, note="started")


def cmd_browser_down(args: argparse.Namespace) -> None:
    from automation_tool.browser_client import (
        BrowserClient,
        browser_service_state_path,
        load_browser_service_state,
    )
    from automation_tool.browser_service import release_stale_browser_service_lock

    st = load_browser_service_state()
    if not st:
        print("No browser service state file.", flush=True)
        release_stale_browser_service_lock()
        return
    pid = int(st.get("pid") or 0)
    try:
        c = BrowserClient.from_state_file()
        if c:
            c.shutdown()
    except OSError:
        pass
    if pid > 0:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            _log.warning("No permission to signal pid %s", pid)
    p = browser_service_state_path()
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        pass
    release_stale_browser_service_lock()
    print("Browser service stop requested.", flush=True)


def cmd_browser_exec(args: argparse.Namespace) -> None:
    from automation_tool.browser_client import BrowserClient

    raw = args.request_json
    if not raw or not str(raw).strip():
        raw = sys.stdin.read()
    obj = json.loads(raw)
    method = str(obj.get("method") or "")
    params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
    if not method:
        raise SystemExit("JSON must include method (and optional params)")
    c = BrowserClient.from_state_file()
    if not c:
        raise SystemExit("No browser service state; run: coinmap-automation browser up")
    resp = c.request(method, params)
    print(json.dumps(resp, ensure_ascii=False, indent=2))


def cmd_browser_tail(args: argparse.Namespace) -> None:
    from automation_tool.browser_client import BrowserClient
    import time

    c = BrowserClient.from_state_file()
    if not c:
        raise SystemExit("No browser service state; run: coinmap-automation browser up")
    interval = float(args.interval)
    print("Pinging browser service (Ctrl+C to stop)…", flush=True)
    try:
        while True:
            try:
                r = c.request("ping", {}, timeout_s=5.0)
                ok = bool(r.get("ok"))
                print(time.strftime("%H:%M:%S"), "ping", "ok" if ok else "fail", r, flush=True)
            except OSError as e:
                print(time.strftime("%H:%M:%S"), "error", e, flush=True)
            time.sleep(max(0.5, interval))
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)


_T = TypeVar("_T")


def _run_capture_telegram_log_parallel_with(
    *,
    bot_token: Optional[str],
    telegram_log_chat_id: Optional[str],
    png_paths: Sequence[Path],
    header: str,
    work_fn: Callable[[], _T],
) -> tuple[int, _T]:
    """
    Gửi capture PNG tới TELEGRAM_LOG_CHAT_ID song song với ``work_fn`` (thường là OpenAI).
    Chờ cả hai xong rồi trả (số ảnh Telegram đã gửi, kết quả work_fn).
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        tg_fut = ex.submit(
            send_capture_screenshots_to_log_chat,
            bot_token=bot_token,
            telegram_log_chat_id=telegram_log_chat_id,
            png_paths=png_paths,
            header=header,
        )
        work_fut = ex.submit(work_fn)
        concurrent.futures.wait([tg_fut, work_fut])
        return tg_fut.result(), work_fut.result()


def _run_openai_flow(
    s,
    charts_dir: Path,
    analysis_prompt: str,
    max_images: int,
    chart_paths: list[Path] | None = None,
    chart_payloads: list[ChartOpenAIPayload] | None = None,
    on_first_model_text: Optional[Callable[[str], None]] = None,
    *,
    purge_json_attachment_storage: bool = False,
    purge_openai_user_data_files: bool | None = None,
    model: str | None = None,
    vector_store_ids: list[str] | None = None,
    reasoning_effort: str | None = DEFAULT_REASONING_EFFORT,
) -> PromptTwoStepResult:
    return run_analysis_responses_flow(
        api_key=s.openai_api_key,
        charts_dir=charts_dir,
        analysis_prompt=analysis_prompt,
        max_images_per_call=max_images,
        vector_store_ids=vector_store_ids if vector_store_ids is not None else s.openai_vector_store_ids,
        store=s.openai_responses_store,
        include=s.openai_responses_include,
        chart_paths=chart_paths,
        chart_payloads=chart_payloads,
        on_first_model_text=on_first_model_text,
        purge_json_attachment_storage=purge_json_attachment_storage,
        purge_openai_user_data_files=purge_openai_user_data_files,
        model=model,
        reasoning_effort=reasoning_effort,
    )


def cmd_capture(args: argparse.Namespace) -> None:
    s = load_settings()
    use_service = bool(getattr(args, "use_service", False))
    _log.info(
        "capture: bắt đầu | config=%s charts_dir=%s headed=%s use_service=%s",
        args.config or default_coinmap_config_path(),
        args.charts_dir if args.charts_dir is not None else "(default theo data/.main_chart_symbol)",
        args.headed,
        use_service,
    )
    cfg = args.config or default_coinmap_config_path()
    storage = args.storage_state or default_storage_state_path()

    from automation_tool.browser_client import BrowserClient, is_service_responding
    from automation_tool.browser_protocol import METHOD_CAPTURE_CHARTS

    if use_service and not is_service_responding():
        raise SystemExit(
            "capture --use-service: browser service không chạy hoặc không phản hồi. "
            "Chạy trước: coinmap-automation browser up "
            f"(và cần {default_data_dir() / 'browser_service_state.json'} với cdp_http)."
        )

    if is_service_responding():
        c = BrowserClient.from_state_file()
        if not c:
            raise SystemExit("browser service state missing. Run: coinmap-automation browser up")
        _log.info("capture: mode=rpc | METHOD_CAPTURE_CHARTS (capture_worker)")
        resp = c.request(
            METHOD_CAPTURE_CHARTS,
            {
                "coinmap_yaml": str(cfg),
                "charts_dir": str(args.charts_dir) if args.charts_dir is not None else None,
                "storage_state_path": str(storage) if storage is not None else None,
                "email": s.coinmap_email,
                "password": s.coinmap_password,
                "tradingview_password": s.tradingview_password,
                "save_storage_state": not args.no_save_storage,
                "headless": not args.headed,
                "main_chart_symbol": args.main_symbol,
                "tradingview_force_screenshot": True,
            },
            timeout_s=600.0,
        )
        if not bool(resp.get("ok")):
            raise SystemExit(f"capture RPC failed: {resp.get('error')}")
        result = resp.get("result") or {}
        paths = [Path(p) for p in (result.get("paths") or []) if isinstance(p, str)]
    else:
        paths = capture_charts(
            coinmap_yaml=cfg,
            charts_dir=args.charts_dir,
            storage_state_path=storage,
            email=s.coinmap_email,
            password=s.coinmap_password,
            tradingview_password=s.tradingview_password,
            save_storage_state=not args.no_save_storage,
            headless=not args.headed,
            reuse_browser_context=None,
            main_chart_symbol=args.main_symbol,
            require_browser_service=False,
            tradingview_force_screenshot=True,
        )

    charts_dir = args.charts_dir or default_charts_dir()
    print(f"Saved {len(paths)} image(s) under {charts_dir}:")
    _log.info("capture: xong | %s file(s) → %s", len(paths), charts_dir)
    for pth in paths:
        print(f"  {pth}")


def _first_coinmap_m5_json_path(paths: Sequence[Path]) -> Optional[Path]:
    for pth in paths:
        if pth.name.endswith("_5m.json") and "_coinmap_" in pth.name:
            return pth
    return None


def cmd_capture_m5_footprint(args: argparse.Namespace) -> None:
    """
    Manual data prep: capture Coinmap M5 raw API export only (no merged file, no OpenAI).
    """
    s = load_settings()
    cfg = args.config or default_coinmap_update_config_path()
    storage = args.storage_state or default_storage_state_path()
    use_service = bool(getattr(args, "use_service", False))
    _log.info(
        "capture-m5-footprint: bắt đầu | config=%s charts_dir=%s headed=%s use_service=%s",
        cfg,
        args.charts_dir if args.charts_dir is not None else "(default theo data/.main_chart_symbol)",
        args.headed,
        use_service,
    )
    paths = capture_charts(
        coinmap_yaml=cfg,
        charts_dir=args.charts_dir,
        storage_state_path=storage,
        email=s.coinmap_email,
        password=s.coinmap_password,
        tradingview_password=s.tradingview_password,
        save_storage_state=not args.no_save_storage,
        headless=not args.headed,
        reuse_browser_context=None,
        main_chart_symbol=args.main_symbol,
        enable_coinmap=True,
        enable_tradingview=False,
        clear_charts_before_capture=True,
        require_browser_service=use_service,
        coinmap_capture_intervals=("5m",),
        write_coinmap_merged_after_capture=False,
    )
    charts_dir = args.charts_dir or default_charts_dir()
    stamp = stamp_from_capture_paths(paths)
    raw_m5 = (
        coinmap_main_pair_interval_json_path(charts_dir, "5m", stamp=stamp)
        if stamp
        else None
    )
    if raw_m5 is None:
        raw_m5 = _first_coinmap_m5_json_path(paths)
    if raw_m5 is None or not raw_m5.is_file():
        raise SystemExit(
            f"capture-m5-footprint: no Coinmap M5 JSON under {charts_dir} after capture "
            f"(stamp={stamp!r})."
        )
    _log.info("capture-m5-footprint: xong | raw_m5=%s", raw_m5)
    print(f"Captured {len(paths)} file(s).")
    print(f"Raw Coinmap M5 JSON: {raw_m5}")


def cmd_analyze_gocharting_detail(args: argparse.Namespace) -> None:
    """Extract Bid/Ask footprint JSON from GoCharting detail PNGs or IndexedDB."""
    from automation_tool.images import normalize_main_chart_symbol, set_active_main_symbol_file

    use_indexeddb = bool(getattr(args, "indexeddb", False))
    s = load_settings()
    if not use_indexeddb:
        require_openai(s)
    if getattr(args, "main_symbol", None):
        set_active_main_symbol_file(args.main_symbol)

    charts_dir = args.charts_dir or default_charts_dir()
    main_sym = normalize_main_chart_symbol(
        args.main_symbol or read_main_chart_symbol(charts_dir)
    )
    gc_yaml = args.gocharting_config or default_gocharting_config_path()
    storage = args.storage_state or default_storage_state_path()
    model = (getattr(args, "model", None) or DEFAULT_FOOTPRINT_EXTRACT_MODEL).strip()
    output_dir = args.output_dir or charts_dir

    _log.info(
        "analyze-gocharting-detail: bắt đầu | charts_dir=%s main=%s capture=%s indexeddb=%s model=%s",
        charts_dir,
        main_sym,
        bool(getattr(args, "capture", False)),
        use_indexeddb,
        model,
    )

    stamp: Optional[str] = (args.stamp or "").strip() or None
    if use_indexeddb and not getattr(args, "capture", False):
        cfg = load_gocharting_yaml(gc_yaml)
        instrument_slug = resolve_instrument_slug(cfg, main_sym)
        try:
            for interval in ("5m", "15m"):
                out_path = footprint_json_output_path(output_dir, interval, instrument_slug)
                require_footprint_json_path(out_path, interval=interval)
                print(f"{interval} footprint JSON (indexeddb): {out_path}")
        except GoChartingIndexedDBFootprintError as exc:
            raise SystemExit(
                f"analyze-gocharting-detail: {exc}. "
                "Run with --capture --indexeddb --headed --use-service."
            ) from exc
        _log.info("analyze-gocharting-detail: indexeddb xong (existing JSON)")
        return

    if getattr(args, "capture", False):
        use_service = bool(getattr(args, "use_service", False))
        try:
            paths = capture_gocharting(
                gocharting_yaml=gc_yaml,
                charts_dir=charts_dir,
                email=s.gocharting_email or "",
                password=s.gocharting_password or "",
                storage_state_path=storage,
                save_storage_state=not args.no_save_storage,
                headless=not args.headed,
                main_chart_symbol=main_sym,
                capture_symbols=(main_sym,),
                capture_intervals=("5m", "15m"),
                require_browser_service=use_service,
                require_footprint_indexeddb=use_indexeddb,
            )
        except GoChartingIndexedDBFootprintError as exc:
            raise SystemExit(f"analyze-gocharting-detail: {exc}") from exc
        stamp = stamp_from_capture_paths(paths) or stamp
        _log.info("analyze-gocharting-detail: capture xong | files=%d stamp=%s", len(paths), stamp)

    if use_indexeddb:
        cfg = load_gocharting_yaml(gc_yaml)
        instrument_slug = resolve_instrument_slug(cfg, main_sym)
        results: dict[str, Path] = {}
        try:
            for interval in ("5m", "15m"):
                out_path = footprint_json_output_path(output_dir, interval, instrument_slug)
                require_footprint_json_path(out_path, interval=interval)
                results[interval] = out_path
        except GoChartingIndexedDBFootprintError as exc:
            raise SystemExit(f"analyze-gocharting-detail: {exc}") from exc
        for interval in ("5m", "15m"):
            print(f"{interval} footprint JSON (indexeddb): {results[interval]}")
        _log.info("analyze-gocharting-detail: indexeddb xong | outputs=%s", results)
        return

    if not stamp:
        stamp = latest_chart_stamp(charts_dir)
    if not stamp:
        raise SystemExit(
            f"analyze-gocharting-detail: no chart stamp under {charts_dir}. "
            "Run capture first or pass --stamp / --capture."
        )

    export_label = gocharting_footprint_export_label(main_sym)
    for interval in ("5m", "15m"):
        detail_paths = gocharting_detail_png_paths(charts_dir, stamp, export_label, interval)
        if not detail_paths:
            raise SystemExit(
                f"analyze-gocharting-detail: no {interval} detail PNGs for stamp {stamp!r} "
                f"under {charts_dir} (expected {stamp}_gocharting_{export_label}_{interval}_detail_*.png). "
                "Run with --capture or ensure GoCharting detail capture completed."
            )

    results = extract_all_footprint_jsons(
        api_key=s.openai_api_key,
        charts_dir=charts_dir,
        output_dir=output_dir,
        stamp=stamp,
        main_symbol=main_sym,
        gocharting_yaml=gc_yaml,
        model=model,
        store=s.openai_responses_store,
        include=s.openai_responses_include,
    )
    for interval in ("5m", "15m"):
        path = results.get(interval)
        if path is not None:
            print(f"{interval} footprint JSON: {path}")
    _log.info("analyze-gocharting-detail: xong | outputs=%s", results)


def cmd_tvdatafeed_login(args: argparse.Namespace) -> None:
    """Probe TvDatafeed credentials with one ``get_hist`` (same env/yaml as capture)."""
    if getattr(args, "verbose", False):
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("automation_tool.tvdatafeed_capture").setLevel(logging.DEBUG)
        logging.getLogger("urllib3").setLevel(logging.DEBUG)
        logging.getLogger("tvDatafeed.main").setLevel(logging.DEBUG)
    load_all_dotenv()
    s = load_settings()
    from automation_tool.tvdatafeed_capture import run_tvdatafeed_login_probe

    cfg_path = args.config or default_coinmap_config_path()
    raw = load_coinmap_yaml(cfg_path)
    tv = raw.get("tradingview_capture")
    tv = tv if isinstance(tv, dict) else {}
    ok, msg, _n = run_tvdatafeed_login_probe(
        tv=tv,
        tradingview_username=s.coinmap_email,
        tradingview_password=s.tradingview_password,
        exchange=getattr(args, "exchange", None),
        symbol=getattr(args, "symbol", None),
        interval_label=getattr(args, "interval", None),
        n_bars=int(getattr(args, "n_bars", 3)),
        verbose=bool(getattr(args, "verbose", False)),
    )
    print(msg, flush=True)
    if not ok:
        raise SystemExit(1)


def cmd_capture_many(args: argparse.Namespace) -> None:
    """
    Multi-symbol capture in one browser session.

    Per symbol: one ``capture_charts`` (Coinmap bearer/API + TradingView, same as
    single ``capture`` — e.g. ``tvdatafeed`` when ``tradingview_capture.data_source`` is set).
    """
    s = load_settings()
    use_service = bool(getattr(args, "use_service", False))
    symbols = _parse_symbols_arg(args.symbols)
    cfg_path = args.config or default_coinmap_config_path()
    storage = args.storage_state or default_storage_state_path()

    _log.info(
        "capture-many: start | symbols=%s config=%s headed=%s storage=%s use_service=%s",
        ",".join(symbols),
        cfg_path,
        args.headed,
        storage,
        use_service,
    )

    from automation_tool.browser_client import BrowserClient, is_service_responding
    from automation_tool.browser_protocol import METHOD_CAPTURE_MANY

    if use_service and not is_service_responding():
        raise SystemExit(
            "capture-many --use-service: browser service không chạy hoặc không phản hồi. "
            "Chạy trước: coinmap-automation browser up "
            f"(và cần {default_data_dir() / 'browser_service_state.json'} với cdp_http)."
        )

    if is_service_responding():
        c = BrowserClient.from_state_file()
        if not c:
            raise SystemExit("browser service state missing. Run: coinmap-automation browser up")
        per_sym = max(1, len(symbols))
        timeout_s = min(7200.0, max(1200.0, 600.0 * float(per_sym)))
        _log.info("capture-many: mode=rpc | METHOD_CAPTURE_MANY (capture_many_worker) timeout_s=%s", timeout_s)
        resp = c.request(
            METHOD_CAPTURE_MANY,
            {
                "symbols": symbols,
                "coinmap_yaml": str(cfg_path),
                "storage_state_path": str(storage) if storage is not None else None,
                "email": s.coinmap_email,
                "password": s.coinmap_password,
                "tradingview_password": s.tradingview_password,
                "save_storage_state": not args.no_save_storage,
                "headless": not args.headed,
                "tradingview_force_screenshot": True,
            },
            timeout_s=timeout_s,
        )
        if not bool(resp.get("ok")):
            raise SystemExit(f"capture-many RPC failed: {resp.get('error')}")
        result = resp.get("result") or {}
        npaths = len([p for p in (result.get("paths") or []) if isinstance(p, str)])
        _log.info("capture-many: xong | rpc | %s file(s)", npaths)
        print("capture-many finished. Charts dirs:")
        for sym in symbols:
            print(f"  {sym}: {symbol_data_dir(sym) / 'charts'}")
        return

    cfg = load_coinmap_yaml(cfg_path)
    vw = int(cfg.get("viewport_width", 1920))
    vh = int(cfg.get("viewport_height", 1080))

    # One stamp per symbol so Coinmap + TradingView artifacts line up for OpenAI ordering.
    stamps: dict[str, str] = {sym: time.strftime("%Y%m%d_%H%M%S") for sym in symbols}

    with sync_playwright() as p:
        browser, context = launch_chrome_context(
            p,
            headless=not args.headed,
            storage_state_path=storage,
            viewport_width=vw,
            viewport_height=vh,
        )
        try:
            for sym in symbols:
                charts_dir = symbol_data_dir(sym) / "charts"
                _log.info("capture-many: coinmap + tradingview | %s → %s", sym, charts_dir)
                capture_charts(
                    coinmap_yaml=cfg_path,
                    charts_dir=charts_dir,
                    storage_state_path=storage,
                    email=s.coinmap_email,
                    password=s.coinmap_password,
                    tradingview_password=s.tradingview_password,
                    save_storage_state=False,
                    headless=not args.headed,
                    reuse_browser_context=context,
                    main_chart_symbol=sym,
                    set_global_active_symbol=False,
                    enable_coinmap=True,
                    enable_tradingview=True,
                    clear_charts_before_capture=True,
                    stamp_override=stamps[sym],
                    tradingview_force_screenshot=True,
                )

            if not args.no_save_storage and storage:
                storage.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(storage))
                _log.info("capture-many: wrote storage state | %s", storage)
        finally:
            close_browser_and_context(browser, context)

    print("capture-many finished. Charts dirs:")
    for sym in symbols:
        print(f"  {sym}: {symbol_data_dir(sym) / 'charts'}")


def _resolved_analysis_prompt(args: argparse.Namespace, charts_dir: Path) -> str:
    """Khi không truyền --prompt: dùng default_analysis_prompt(read_main_chart_symbol(charts_dir))."""
    p = getattr(args, "prompt", None)
    if p is not None and str(p).strip():
        return str(p)
    from automation_tool.images import read_main_chart_symbol

    sym = read_main_chart_symbol(charts_dir)
    fp = footprint_source_for_stamp(charts_dir)
    return default_analysis_prompt(sym, footprint_source=fp)


def _warn_if_incomplete_chart_payloads(
    charts_dir: Path, payloads: list[ChartOpenAIPayload]
) -> None:
    st = latest_chart_stamp(charts_dir)
    if not st:
        return
    from automation_tool.images import (
        coinmap_merged_openai_files,
        read_main_chart_symbol,
    )

    main_sym = read_main_chart_symbol(charts_dir)
    expected = len(effective_chart_image_order(charts_dir, stamp=st))
    _dxy, main_m = coinmap_merged_openai_files(charts_dir, st, main_sym)
    if main_m is not None:
        expected -= 1
    if len(payloads) < expected:
        print(
            f"Warning: expected {expected} chart slot(s) in fixed order, found {len(payloads)} file(s) on disk.",
            file=sys.stderr,
        )


def _tradingview_slot_openai_payload(
    charts_dir: Path, *, stamp: str, symbol: str, interval_slug: str
) -> list[ChartOpenAIPayload]:
    """Return one TradingView slot as the same payload shape used by full analysis."""
    stem = f"{stamp}_tradingview_{symbol}_{interval_slug}"
    jp = charts_dir / f"{stem}.json"
    up = charts_dir / f"{stem}.url"
    pp = charts_dir / f"{stem}.png"
    if jp.is_file():
        return [("json", jp)]
    if up.is_file():
        raw = up.read_text(encoding="utf-8").strip().splitlines()
        line = (raw[0] if raw else "").strip()
        if line.startswith("http://") or line.startswith("https://"):
            return [("image_url", line)]
        if pp.is_file():
            return [("image", pp)]
    if pp.is_file():
        return [("image", pp)]
    return []


def _intraday_tradingview_interval_specs(*, include_m15_regular: bool) -> list[dict[str, str]]:
    """TV shots for intraday flows: optional plain M15, then M15 ICT, then M5."""
    specs: list[dict[str, str]] = []
    if include_m15_regular:
        specs.append({"label": "15 phút", "slug": "15m"})
    specs.extend(
        [
            {"label": "15 phút", "slug": "15m_ict", "indicator_profile": "ict_killzones"},
            {"label": "5 phút", "slug": "5m"},
        ]
    )
    return specs


def _intraday_tradingview_openai_slugs(*, include_m15_regular: bool) -> tuple[str, ...]:
    if include_m15_regular:
        return ("15m", "15m_ict", "5m")
    return ("15m_ict", "5m")


def _intraday_tv_then_coinmap_m5_capture(
    *,
    cfg_tv: Path,
    cfg_cap: Path,
    charts_dir: Path,
    storage: Path,
    email: str,
    password: str,
    tradingview_password: str,
    save_storage: bool,
    headless: bool,
    main_chart_symbol: Optional[str],
    no_tradingview: bool,
    flow_label: str,
    coinmap_capture_intervals: tuple[str, ...] = ("15m", "5m"),
    include_tv_m15_regular: bool = False,
    use_gocharting: bool = False,
    gocharting_yaml: Optional[Path] = None,
    gocharting_email: str = "",
    gocharting_password: str = "",
    gocharting_detail_history_steps: Optional[int] = None,
) -> tuple[list[Path], str | None, str, list[ChartOpenAIPayload]]:
    """
    Shared capture for ``update`` / ``update-scalp``: TradingView 15m ICT + 5m, then
    footprint export (Coinmap JSON or GoCharting CSV) reusing the same ``stamp``.
    """
    from automation_tool.images import get_active_main_symbol, read_main_chart_symbol

    paths: list[Path] = []
    tv_chart_payloads: list[ChartOpenAIPayload] = []
    gc_yaml = gocharting_yaml or default_gocharting_config_path()

    def _footprint_capture(*, stamp_override: str | None, clear_before: bool) -> list[Path]:
        if use_gocharting:
            main_for_cap = get_active_main_symbol()
            return capture_gocharting(
                gocharting_yaml=gc_yaml,
                charts_dir=charts_dir,
                email=gocharting_email,
                password=gocharting_password,
                storage_state_path=storage,
                save_storage_state=save_storage,
                headless=headless,
                main_chart_symbol=main_chart_symbol,
                stamp_override=stamp_override,
                clear_charts_before_capture=clear_before,
                capture_symbols=(main_for_cap,),
                capture_intervals=coinmap_capture_intervals,
                detail_history_steps=gocharting_detail_history_steps,
            )
        return capture_charts(
            coinmap_yaml=cfg_cap,
            charts_dir=charts_dir,
            storage_state_path=storage,
            email=email,
            password=password,
            tradingview_password=tradingview_password,
            save_storage_state=save_storage,
            headless=headless,
            reuse_browser_context=None,
            main_chart_symbol=main_chart_symbol,
            enable_coinmap=True,
            enable_tradingview=False,
            clear_charts_before_capture=clear_before,
            stamp_override=stamp_override,
            coinmap_capture_intervals=coinmap_capture_intervals,
            write_coinmap_merged_after_capture=False,
        )

    if not no_tradingview:
        main_for_tv = get_active_main_symbol()
        tv_plan = [
            {
                "symbol": main_for_tv,
                "intervals": _intraday_tradingview_interval_specs(
                    include_m15_regular=include_tv_m15_regular
                ),
            }
        ]
        tv_paths = capture_charts(
            coinmap_yaml=cfg_tv,
            charts_dir=charts_dir,
            storage_state_path=storage,
            email=email,
            password=password,
            tradingview_password=tradingview_password,
            save_storage_state=save_storage,
            headless=headless,
            reuse_browser_context=None,
            main_chart_symbol=main_chart_symbol,
            enable_coinmap=False,
            enable_tradingview=True,
            clear_charts_before_capture=True,
            tradingview_capture_plan=tv_plan,
            tradingview_force_screenshot=True,
        )
        paths.extend(tv_paths)
        stamp = stamp_from_capture_paths(paths)
        if not stamp:
            raise SystemExit(
                f"{flow_label}: không có stamp sau capture TradingView; kiểm tra tradingview_capture và charts_dir."
            )
        main_s = read_main_chart_symbol(charts_dir)
        fp_paths = _footprint_capture(stamp_override=stamp, clear_before=use_gocharting)
        paths.extend(fp_paths)
        tv_slugs = _intraday_tradingview_openai_slugs(include_m15_regular=include_tv_m15_regular)
        for slug in tv_slugs:
            tv_chart_payloads.extend(
                _tradingview_slot_openai_payload(
                    charts_dir, stamp=stamp, symbol=main_s, interval_slug=slug
                )
            )
        fp_label = "GoCharting" if use_gocharting else "Coinmap"
        _log.info(
            "%s: TradingView %s trước %s | tv_files=%s | stamp=%s | tv_payloads=%s",
            flow_label,
            "+".join(tv_slugs),
            fp_label,
            len(tv_paths),
            stamp,
            len(tv_chart_payloads),
        )
        expected_tv = len(tv_slugs)
        if len(tv_chart_payloads) < expected_tv:
            print(
                f"Warning: expected {expected_tv} TradingView slot(s) ({', '.join(tv_slugs)}) for OpenAI; "
                f"got {len(tv_chart_payloads)} payload(s) (stamp={stamp!r}, symbol={main_s}).",
                file=sys.stderr,
            )
    else:
        paths = _footprint_capture(stamp_override=None, clear_before=True)
        stamp = stamp_from_capture_paths(paths)
        main_s = read_main_chart_symbol(charts_dir)

    return paths, stamp, main_s, tv_chart_payloads


def cmd_test_cloudinary_json(args: argparse.Namespace) -> None:
    """Preview OpenAI Responses `input` for JSON files under charts-dir."""
    from automation_tool.images import set_active_main_symbol_file
    from automation_tool.openai_prompt_flow import (
        _build_mixed_chart_user_content,
        _default_max_coinmap_json_chars,
    )

    if getattr(args, "main_symbol", None):
        set_active_main_symbol_file(args.main_symbol)
    charts_dir = args.charts_dir or default_charts_dir()
    if not charts_dir.is_dir():
        raise SystemExit(f"Charts directory not found: {charts_dir}")
    paths = sorted(charts_dir.glob("*.json"))
    if not paths:
        raise SystemExit(f"No .json files under {charts_dir}")
    _log.info(
        "test-cloudinary-json: charts_dir=%s files=%d",
        charts_dir,
        len(paths),
    )

    mx = _default_max_coinmap_json_chars()
    preview_prompt = (
        "[test-cloudinary-json] Preview: chỉ các file *.json trong thư mục (thứ tự sort), "
        "không gồm ảnh. Coinmap JSON sẽ đính kèm dạng `input_file.file_data` base64; "
        "JSON khác sẽ nằm trong `input_text`.\n"
    )
    payloads = [("json", p) for p in paths]
    content = _build_mixed_chart_user_content(
        preview_prompt,
        payloads,
        max_json_chars=mx,
    )
    openai_preview = {
        "note": (
            "Mẫu `input` cho Responses API: một user message với `content` như dưới "
            "(tương đương phần JSON trong _build_mixed_chart_user_content khi chỉ có json payloads)."
        ),
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": content,
            }
        ],
    }
    print("--- openai input preview (JSON) ---", flush=True)
    print(json.dumps(openai_preview, ensure_ascii=False, indent=2), flush=True)


def cmd_upload_ea_neverdie_json(args: argparse.Namespace) -> None:
    """Upload the existing local EA NeverDie JSON without rebuilding zones."""
    from automation_tool.ea_neverdie_zone_publish import (
        default_local_path,
        neverdie_cloud_folder,
        neverdie_public_stem,
        upload_neverdie_json,
    )
    from automation_tool.images import (
        get_active_main_symbol,
        normalize_main_chart_symbol,
        set_active_main_symbol_file,
    )

    if getattr(args, "main_symbol", None):
        set_active_main_symbol_file(args.main_symbol)
        sym = get_active_main_symbol().strip().upper()
    elif getattr(args, "file", None) is not None:
        try:
            sym = normalize_main_chart_symbol(Path(args.file).expanduser().parent.name)
        except ValueError:
            sym = get_active_main_symbol().strip().upper()
    else:
        sym = get_active_main_symbol().strip().upper()
    local_path = (args.file or default_local_path(sym)).expanduser()
    if not local_path.is_file():
        raise SystemExit(f"EA NeverDie JSON not found: {local_path}")

    body = local_path.read_bytes()
    try:
        json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise SystemExit(f"EA NeverDie JSON invalid: {local_path} ({e})") from e

    url = upload_neverdie_json(body, sym)
    cloud = (os.getenv("CLOUDINARY_CLOUD_NAME") or "").strip()
    print(f"EA NeverDie JSON source: {local_path}", flush=True)
    if cloud:
        versionless = (
            f"https://res.cloudinary.com/{cloud}/raw/upload/"
            f"{neverdie_cloud_folder()}/{neverdie_public_stem(sym)}"
        )
        print(f"EA NeverDie JSON (Cloudinary hint): {versionless}", flush=True)
    if url:
        print(f"EA NeverDie JSON (secure_url): {url}", flush=True)
    else:
        print("EA NeverDie JSON upload skipped (Cloudinary not configured or disabled).", flush=True)


def cmd_analyze(args: argparse.Namespace) -> None:
    from automation_tool.images import set_active_main_symbol_file

    s = load_settings()
    if getattr(args, "main_symbol", None):
        set_active_main_symbol_file(args.main_symbol)
    _log.info("analyze: bắt đầu | charts_dir=%s no_telegram=%s", args.charts_dir or default_charts_dir(), args.no_telegram)
    require_openai(s)
    charts_dir = args.charts_dir or default_charts_dir()
    payloads = ordered_chart_openai_payloads(charts_dir)
    _warn_if_incomplete_chart_payloads(charts_dir, payloads)
    if not payloads:
        raise SystemExit(
            f"No chart files under {charts_dir} (TradingView .url / PNG, Coinmap JSON or PNG). "
            "Run capture first or check charts under data/{SYMBOL}/charts/."
        )

    lap = args.last_alert_json or default_last_alert_prices_path()

    def _on_first(text: str) -> None:
        apply_first_response_vao_lenh(
            text,
            last_alert_path=lap,
            mt5_execute=not args.no_mt5_execute,
            mt5_dry_run=args.mt5_dry_run,
            mt5_symbol=args.mt5_symbol,
            mt5_accounts_json=_resolved_mt5_accounts_json(args),
            telegram_bot_token=s.telegram_bot_token,
            telegram_chat_id=s.telegram_chat_id,
            telegram_log_chat_id=s.telegram_log_chat_id,
            telegram_python_bot_chat_id=s.telegram_python_bot_chat_id,
            telegram_output_ngan_gon_chat_id=s.telegram_output_ngan_gon_chat_id,
            telegram_source_label="analyze (phản hồi đầu)",
        )

    prompt = _resolved_analysis_prompt(args, charts_dir)
    try:
        out = _run_openai_flow(
            s,
            charts_dir,
            prompt,
            args.max_images_per_call,
            chart_payloads=payloads,
            on_first_model_text=_on_first,
            model=resolved_openai_model(s, getattr(args, "model", None)),
        )
    except Exception as e:
        re_raise_unless_openai(e)
    print(out.full_text())
    _log.info("analyze: OpenAI xong | response_id=%s", out.final_response_id)
    if not args.no_telegram and out.after_charts:
        require_telegram(s)
        # phan_tich_cham_diem → TELEGRAM_CHAT_ID; output_ngan_gon → TELEGRAM_OUTPUT_NGAN_GON_CHAT_ID.
        # TELEGRAM_ANALYSIS_DETAIL_CHAT_ID is for per-step logs (first_response / journal), not here.
        send_openai_output_to_telegram(
            bot_token=s.telegram_bot_token,
            chat_id=s.telegram_chat_id,
            raw=out.after_charts,
            default_parse_mode=s.telegram_parse_mode,
            summary_chat_id=s.telegram_output_ngan_gon_chat_id,
            detail_chat_id=None,
        )


def cmd_analyze_many(args: argparse.Namespace) -> None:
    s = load_settings()
    require_openai(s)
    symbols = _parse_symbols_arg(args.symbols)
    parallel = max(1, int(args.parallel or 1))

    detail_chat_id = (
        str(args.telegram_detail_chat_id).strip()
        if getattr(args, "telegram_detail_chat_id", None) is not None
        and str(args.telegram_detail_chat_id).strip()
        else s.telegram_analysis_detail_chat_id
    )

    _log.info(
        "analyze-many: start | symbols=%s parallel=%s no_telegram=%s",
        ",".join(symbols),
        parallel,
        args.no_telegram,
    )

    def _analyze_one(sym: str) -> tuple[str, Optional[PromptTwoStepResult], Optional[BaseException]]:
        charts_dir = symbol_data_dir(sym) / "charts"
        payloads = ordered_chart_openai_payloads(charts_dir)
        _warn_if_incomplete_chart_payloads(charts_dir, payloads)
        if not payloads:
            return sym, None, SystemExit(f"No chart files under {charts_dir} (run capture-many first).")

        prompt = (
            str(args.prompt).strip()
            if getattr(args, "prompt", None) is not None and str(args.prompt).strip()
            else default_analysis_prompt(sym)
        )

        try:
            out = _run_openai_flow(
                s,
                charts_dir,
                prompt,
                args.max_images_per_call,
                chart_payloads=payloads,
                on_first_model_text=None,
                model=resolved_openai_model(s, getattr(args, "model", None)),
            )
            return sym, out, None
        except BaseException as e:
            return sym, None, e

    results: list[tuple[str, Optional[PromptTwoStepResult], Optional[BaseException]]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as ex:
        futs = {ex.submit(_analyze_one, sym): sym for sym in symbols}
        for fut in concurrent.futures.as_completed(futs):
            sym = futs[fut]
            try:
                results.append(fut.result())
            except BaseException as e:
                results.append((sym, None, e))

    # Stable output order = input order.
    by_sym = {sym: (out, err) for sym, out, err in results}
    ok = 0
    fail = 0
    for sym in symbols:
        out, err = by_sym.get(sym, (None, RuntimeError("missing result")))
        if err is not None:
            fail += 1
            try:
                re_raise_unless_openai(err)
            except BaseException:
                pass
            print(f"\n==== {sym} ERROR ====\n{err}\n")
            continue
        ok += 1
        assert out is not None
        print(f"\n==== {sym} OUTPUT ====\n{out.full_text()}\n")
        if not args.no_telegram and out.after_charts:
            require_telegram(s)
            # New flow requirement: only send phan_tich_cham_diem to TELEGRAM_ANALYSIS_DETAIL_CHAT_ID
            # (or --telegram-detail-chat-id override).
            dual = split_analysis_json_chi_tiet_ngan_gon(out.after_charts)
            if dual is None:
                dual = split_output_chi_tiet_ngan_gon(out.after_charts)
            if dual is not None:
                chi_tiet, _ngan_gon = dual
                plan_lines = format_plan_lines_for_telegram(
                    parse_analysis_from_openai_text(out.after_charts)
                )
                if chi_tiet and plan_lines:
                    chi_tiet = chi_tiet.rstrip() + "\n\n" + plan_lines
                if chi_tiet:
                    send_message(
                        bot_token=s.telegram_bot_token,
                        chat_id=detail_chat_id or s.telegram_chat_id,
                        text=chi_tiet,
                        parse_mode=s.telegram_parse_mode,
                    )
            else:
                # Fallback: send whatever the model produced (still to detail chat).
                send_openai_output_to_telegram(
                    bot_token=s.telegram_bot_token,
                    chat_id=detail_chat_id or s.telegram_chat_id,
                    raw=out.after_charts,
                    default_parse_mode=s.telegram_parse_mode,
                    summary_chat_id=None,
                    detail_chat_id=None,
                )

    print(f"analyze-many finished: ok={ok} fail={fail}")


def cmd_chatgpt_project(args: argparse.Namespace) -> None:
    cmd_analyze(args)


def cmd_mt5_trade(args: argparse.Namespace) -> None:
    path = args.file.expanduser()
    if not path.is_file():
        raise SystemExit(f"File not found: {path}")
    load_all_dotenv()
    s_mt5 = load_settings()
    text = path.read_text(encoding="utf-8")
    default_sym = "XAUUSD"
    trade, err = parse_openai_output_md(
        text,
        default_symbol=default_sym,
        symbol_override=args.symbol,
    )
    if err or trade is None:
        raise SystemExit(err or "Không parse được lệnh.")
    accounts = load_mt5_accounts_for_cli(_resolved_mt5_accounts_json(args))
    if accounts:
        summ = execute_trade_all_accounts(
            trade,
            accounts,
            dry_run=args.dry_run,
            symbol_override=args.symbol,
        )
        tg_text = format_mt5_multi_for_telegram(summ)
        send_mt5_execution_log_to_ngan_gon_chat(
            bot_token=s_mt5.telegram_bot_token,
            telegram_chat_id=s_mt5.telegram_chat_id,
            telegram_python_bot_chat_id=s_mt5.telegram_python_bot_chat_id,
            telegram_log_chat_id=s_mt5.telegram_log_chat_id,
            source="mt5-trade",
            text=tg_text,
            trade_line=(trade.raw_line or "").strip() or None,
            execution_ok=summ.ok_all,
        )
        for ex in summ.results:
            if ex.resolved_symbol:
                print("Symbol MT5 (đã resolve):", ex.resolved_symbol)
                break
        print(tg_text)
        if not summ.ok_all:
            raise SystemExit(1)
        return

    out = execute_trade(
        trade,
        dry_run=args.dry_run,
        symbol_override=args.symbol,
        lot_override=args.lot,
    )
    send_mt5_execution_log_to_ngan_gon_chat(
        bot_token=s_mt5.telegram_bot_token,
        telegram_chat_id=s_mt5.telegram_chat_id,
        telegram_python_bot_chat_id=s_mt5.telegram_python_bot_chat_id,
        telegram_log_chat_id=s_mt5.telegram_log_chat_id,
        source="mt5-trade",
        text=format_mt5_execution_for_telegram(out),
        trade_line=(trade.raw_line or "").strip() or None,
        execution_ok=out.ok,
    )
    if out.resolved_symbol:
        print("Symbol MT5 (đã resolve):", out.resolved_symbol)
    print(out.message)
    if out.request:
        print("request/preview:", out.request)
    if out.last_error is not None:
        print("mt5.last_error:", out.last_error)
    if out.trade_check is not None:
        print("order_check (trade_check):", out.trade_check)
    if out.trade_result is not None:
        print("order_send (trade_result):", out.trade_result)
    if not out.ok:
        raise SystemExit(1)


def cmd_mt5_login(args: argparse.Namespace) -> None:
    r = check_mt5_login(
        login=args.login,
        password=args.password,
        server=args.server,
    )
    print("\n".join(r.lines))
    if not r.ok:
        raise SystemExit(1)


def cmd_mt5_login_all_async(args: argparse.Namespace) -> None:
    """
    Kết nối tất cả accounts (accounts.json) song song, log terminal_info() + account_info().

    Lưu ý: MetaTrader5 Python API có thể không thread-safe. Nếu gặp lỗi ngẫu nhiên, giảm --concurrency.
    """

    accounts = load_mt5_accounts_for_cli(_resolved_mt5_accounts_json(args))
    if not accounts:
        raise SystemExit("Không có accounts.json. Dùng --mt5-accounts-json hoặc set MT5_ACCOUNTS_JSON.")

    try:
        max_conc = int(getattr(args, "concurrency", 0) or 0)
    except Exception:
        max_conc = 0
    if max_conc <= 0:
        max_conc = len(accounts)
    max_conc = max(1, min(max_conc, len(accounts)))

    def _probe_one(acc) -> tuple[str, bool, list[str]]:
        from automation_tool.mt5_execute import ensure_mt5_session

        lines: list[str] = []
        term_path = str(getattr(acc, "terminal_path", "") or "").strip()
        if not term_path:
            return acc.id, False, ["terminal_path rỗng"]
        session = ensure_mt5_session(
            terminal_path=term_path,
            login=int(acc.login),
            password=str(acc.password),
            server=str(acc.server),
        )
        lines.append(f"terminal_path: {term_path}")
        lines.append(f"session: {session.message}")
        lines.append(f"reused={session.reused} initialized={session.initialized}")
        lines.append(f"terminal_info: {session.terminal_info}")
        lines.append(f"account_info: {session.account_info}")
        ai = session.account_info
        if ai is not None:
            try:
                lines.append(f"account.login={ai.login} server={ai.server!r} currency={ai.currency}")
            except Exception:
                pass
        return acc.id, session.ok, lines

    async def _run() -> list[tuple[str, bool, list[str]]]:
        sem = asyncio.Semaphore(max_conc)

        async def _one(acc):
            async with sem:
                return await asyncio.to_thread(_probe_one, acc)

        tasks = [_one(a) for a in accounts]
        return await asyncio.gather(*tasks)

    results = asyncio.run(_run())
    any_fail = False
    for acc_id, ok, lines in results:
        print(f"\n[{acc_id}] {'OK' if ok else 'FAIL'}")
        for ln in lines:
            print(ln)
        if not ok:
            any_fail = True

    if any_fail:
        raise SystemExit(1)


def cmd_telegram_send(args: argparse.Namespace) -> None:
    s = load_settings()
    require_telegram(s)
    send_message(
        bot_token=s.telegram_bot_token,
        chat_id=s.telegram_chat_id,
        text=args.message,
        parse_mode=args.parse_mode,
    )
    print("Sent.")


def cmd_telegram_listen(args: argparse.Namespace) -> None:
    s = load_settings()
    require_telegram(s)
    params = TelegramListenParams(
        poll_interval_seconds=float(args.poll_interval_seconds),
        long_poll_timeout_seconds=int(args.long_poll_timeout_seconds),
        full_main_symbol=str(args.full_main_symbol or "XAUUSD"),
        update_main_symbol=str(args.update_main_symbol or "XAUUSD"),
        openai_model=resolved_openai_model(s, getattr(args, "model", None)),
    )
    run_telegram_listener(settings=s, params=params)


def _reconcile_daemon_plans_after_cli(zones_dir: Path, log_step: str) -> None:
    """Spawn daemon-plan for each non-terminal ``vung_*.json`` missing a live PID (same as ``reconcile-daemon-plans``)."""
    n = reconcile_daemon_plans_at_boot(zones_dir)
    _log.info("%s: reconcile-daemon-plans | spawned=%s | dir=%s", log_step, n, zones_dir)
    print(f"reconcile-daemon-plans: spawned {n} process(es) | dir={zones_dir}", flush=True)


def _sync_all2_accounts_subset(cli_path: Optional[Path]) -> None:
    """Tạo ``accounts-all2.json`` từ các dòng ``\"all-2\": true`` trong accounts.json."""
    base_acc = resolve_mt5_accounts_path(cli_path)
    if base_acc is None or not base_acc.is_file():
        return
    try:
        out = sync_accounts_all2_json(base_acc)
    except Exception as e:
        raise SystemExit(f"Không tạo accounts-all2.json từ {base_acc}: {e}") from e
    if out is not None:
        _log.info("all-2: đã ghi %s", out)
    else:
        _log.warning(
            'all-2: không có account nào "all-2": true trong %s — daemon-plan sẽ không vào lệnh '
            "zone source=all-2 cho đến khi có subset",
            base_acc,
        )


def _persist_all_second_flow_zones(
    *,
    out2: PromptTwoStepResult,
    zones_dir: Path,
    session_slot: SessionSlot,
) -> None:
    """Ghi shard ``*_<slot>-2.json`` với ``source=all-2`` từ output OpenAI luồng 2."""
    if not out2.after_charts.strip():
        return
    payload = parse_analysis_from_openai_text(out2.after_charts)
    if payload is None or not payload.prices:
        if out2.after_charts.strip():
            print(
                "Warning: all-2 could not parse analysis JSON for zones (no `prices` or empty).",
                file=sys.stderr,
            )
        return
    from automation_tool.images import get_active_main_symbol

    sym = get_active_main_symbol().strip().upper()
    zones = zones_from_analysis_payload(
        symbol=sym, payload=payload, source="all-2", session_slot=session_slot
    )
    if not zones:
        _log.warning("all-2: parse JSON có prices nhưng không tạo được zones — không ghi shard")
        return
    write_zones_for_slot(
        symbol=sym,
        zones=zones,
        slot=session_slot,
        zones_dir=zones_dir,
        shard_suffix="-2",
        update_manifest_slot=False,
    )
    _log.info(
        "all-2: đã ghi shard zones | slot=%s zones=%d | symbol=%s | suffix=-2",
        session_slot,
        len(zones),
        sym,
    )


def _run_all_second_flow(
    s,
    *,
    charts_dir: Path,
    analysis_prompt: str,
    max_images_per_call: int,
    chart_payloads: list[ChartOpenAIPayload],
    no_telegram: bool,
    model: str | None,
    zones_dir: Optional[Path] = None,
    session_slot: Optional[SessionSlot] = None,
    mt5_accounts_json: Optional[Path] = None,
) -> PromptTwoStepResult:
    """Luồng OpenAI thứ hai của ``all``: vector store riêng, Telegram nhóm 2, ghi shard ``-2``."""
    try:
        out2 = _run_openai_flow(
            s,
            charts_dir,
            analysis_prompt,
            max_images_per_call,
            chart_payloads=chart_payloads,
            on_first_model_text=None,
            model=resolved_openai_model(s, model),
            vector_store_ids=[_ALL_SECOND_FLOW_VECTOR_STORE_ID],
            reasoning_effort=ALL_FLOW_REASONING_EFFORT,
        )
    except Exception as e:
        re_raise_unless_openai(e)
    print(out2.full_text())
    _log.info("all-2: OpenAI xong | response_id=%s", out2.final_response_id)
    if not no_telegram and out2.after_charts:
        require_telegram(s)
        send_openai_output_to_telegram(
            bot_token=s.telegram_bot_token,
            chat_id=_ALL_SECOND_FLOW_TELEGRAM_CHAT_ID,
            raw=out2.after_charts,
            default_parse_mode=s.telegram_parse_mode,
            summary_chat_id=None,
        )
    if zones_dir is not None and session_slot is not None:
        _sync_all2_accounts_subset(mt5_accounts_json)
        _persist_all_second_flow_zones(
            out2=out2, zones_dir=zones_dir, session_slot=session_slot
        )
    return out2


def cmd_all_2(args: argparse.Namespace) -> None:
    """Chạy lại luồng 2 của ``all`` từ chart đã có (khi luồng 1 xong nhưng luồng 2 lỗi)."""
    from automation_tool.images import set_active_main_symbol_file

    s = load_settings()
    if getattr(args, "main_symbol", None):
        set_active_main_symbol_file(args.main_symbol)

    zones_dir = zones_dir_from_cli_path(getattr(args, "zones_json", None))
    run_slot: SessionSlot = session_slot_now_hcm()
    charts_dir = args.charts_dir or default_charts_dir()
    stamp_raw = getattr(args, "stamp", None)
    stamp = (str(stamp_raw).strip() if stamp_raw else None) or latest_chart_stamp(charts_dir)
    if not stamp:
        raise SystemExit(
            f"No chart stamp under {charts_dir}. Run `all` or `capture` first, "
            "or pass --stamp YYYYMMDD_HHMMSS matching existing filenames."
        )

    _log.info("all-2: bắt đầu | charts=%s stamp=%s no_telegram=%s", charts_dir, stamp, args.no_telegram)
    require_valid_coinmap_exports_for_stamp(charts_dir, stamp)
    require_openai(s)
    payloads = ordered_chart_openai_payloads(charts_dir, stamp=stamp)
    _warn_if_incomplete_chart_payloads(charts_dir, payloads)
    if not payloads:
        raise SystemExit(
            f"No chart files for stamp {stamp!r} under {charts_dir}. "
            "Check capture artifacts or pass a different --stamp."
        )

    prompt = _resolved_analysis_prompt(args, charts_dir)
    print(f"all-2: using stamp {stamp} | {len(payloads)} payload(s) from {charts_dir}", flush=True)
    _run_all_second_flow(
        s,
        charts_dir=charts_dir,
        analysis_prompt=prompt,
        max_images_per_call=args.max_images_per_call,
        chart_payloads=payloads,
        no_telegram=args.no_telegram,
        model=getattr(args, "model", None),
        zones_dir=zones_dir,
        session_slot=run_slot,
        mt5_accounts_json=getattr(args, "mt5_accounts_json", None),
    )


def cmd_all(args: argparse.Namespace) -> None:
    s = load_settings()
    from automation_tool.images import set_active_main_symbol_file

    if getattr(args, "main_symbol", None):
        set_active_main_symbol_file(args.main_symbol)

    zones_dir = zones_dir_from_cli_path(args.zones_json)
    run_slot: SessionSlot = session_slot_now_hcm()
    if not args.no_clear_zones_state and run_slot == "sang":
        stop_daemon_plans_in_zones(zones_dir)
        n_rm = clear_zones_directory(zones_dir)
        _log.info("all: cleared zones | slot=%s removed=%s dir=%s", run_slot, n_rm, zones_dir)
        print(f"Đã dừng daemon-plan (nếu có) và xóa zones/: {zones_dir} ({n_rm} file)", flush=True)
    elif args.no_clear_zones_state:
        _log.info("all: skip clearing zones | --no-clear-zones-state | slot=%s dir=%s", run_slot, zones_dir)
    else:
        _log.info("all: skip clearing zones | slot=%s dir=%s", run_slot, zones_dir)

    cfg = args.config or default_coinmap_config_path()
    storage = args.storage_state or default_storage_state_path()
    use_gc = bool(getattr(args, "gocharting", False))
    gc_yaml = getattr(args, "gocharting_config", None) or default_gocharting_config_path()
    _log.info(
        "all: bắt đầu | tv_yaml=%s charts=%s no_tradingview=%s gocharting=%s",
        cfg,
        args.charts_dir if args.charts_dir is not None else "(default)",
        args.no_tradingview,
        use_gc,
    )
    _send_python_bot_job_started(
        s,
        title=f"Phân tích vào lúc {_now_clock_hcm()} bắt đầu chạy",
        no_telegram=args.no_telegram,
    )
    charts_dir = args.charts_dir or default_charts_dir()
    paths: list[Path] = []
    if use_gc:
        if not args.no_tradingview:
            paths = capture_charts(
                coinmap_yaml=cfg,
                charts_dir=args.charts_dir,
                storage_state_path=storage,
                email=s.coinmap_email,
                password=s.coinmap_password,
                tradingview_password=s.tradingview_password,
                save_storage_state=not args.no_save_storage,
                headless=not args.headed,
                reuse_browser_context=None,
                main_chart_symbol=args.main_symbol,
                enable_coinmap=False,
                enable_tradingview=True,
                clear_charts_before_capture=True,
                tradingview_force_screenshot=True,
                write_coinmap_merged_after_capture=False,
            )
        stamp_pre = stamp_from_capture_paths(paths)
        from automation_tool.images import get_active_main_symbol

        main_sym = get_active_main_symbol()
        gc_paths = capture_gocharting(
            gocharting_yaml=gc_yaml,
            charts_dir=charts_dir,
            email=s.gocharting_email or "",
            password=s.gocharting_password or "",
            storage_state_path=storage,
            save_storage_state=not args.no_save_storage,
            headless=not args.headed,
            main_chart_symbol=args.main_symbol,
            stamp_override=stamp_pre,
            clear_charts_before_capture=True,
            capture_symbols=("DXY", main_sym),
        )
        paths.extend(gc_paths)
    else:
        capture_kw: dict[str, object] = {
            "coinmap_yaml": cfg,
            "charts_dir": args.charts_dir,
            "storage_state_path": storage,
            "email": s.coinmap_email,
            "password": s.coinmap_password,
            "tradingview_password": s.tradingview_password,
            "save_storage_state": not args.no_save_storage,
            "headless": not args.headed,
            "reuse_browser_context": None,
            "main_chart_symbol": args.main_symbol,
            "tradingview_force_screenshot": True,
            "write_coinmap_merged_after_capture": False,
        }
        if args.no_tradingview:
            capture_kw["enable_tradingview"] = False
        paths = capture_charts(**capture_kw)
    n_art = len(paths)
    print(f"Captured {n_art} file(s) (screenshots and/or API JSON paths returned by capture).")
    _log.info("all: capture xong | %s artifact(s)", n_art)
    if not paths:
        raise SystemExit("No chart artifacts captured; aborting analyze step.")

    stamp = stamp_from_capture_paths(paths) or latest_chart_stamp(charts_dir)
    if not stamp:
        raise SystemExit("Could not determine capture stamp from chart artifacts; aborting.")
    _CHART_JSON_VALIDATE_MAX_ROUNDS = 3
    for attempt in range(_CHART_JSON_VALIDATE_MAX_ROUNDS + 1):
        bad = list_invalid_chart_slots_for_stamp(charts_dir, stamp)
        stale_fn = is_gocharting_stale_chart_issue if use_gc else is_coinmap_stale_chart_issue
        stale = [x for x in bad if stale_fn(x)]
        if stale:
            detail = "; ".join(f"{x.expected_path.name}: {x.reason}" for x in stale)
            label = "GoCharting" if use_gc else "Coinmap"
            raise SystemExit(f"{label} data stale (aborting, no recapture): {detail}")
        if not bad:
            break
        if attempt >= _CHART_JSON_VALIDATE_MAX_ROUNDS:
            detail = "; ".join(f"{x.expected_path.name}: {x.reason}" for x in bad)
            raise SystemExit(
                f"Chart JSON validation failed after {_CHART_JSON_VALIDATE_MAX_ROUNDS} recapture attempt(s): {detail}"
            )
        print(
            f"Chart JSON validation: {len(bad)} slot(s) invalid — recapturing (attempt {attempt + 1}/{_CHART_JSON_VALIDATE_MAX_ROUNDS})...",
            flush=True,
        )
        _log.warning(
            "all: chart JSON validation failed | attempt=%s | issues=%s",
            attempt + 1,
            [(x.expected_path.name, x.reason) for x in bad],
        )
        try:
            if use_gc:
                recapture_failed_gocharting_slots(
                    gocharting_yaml=gc_yaml,
                    charts_dir=charts_dir,
                    stamp=stamp,
                    issues=bad,
                        storage_state_path=storage,
                        email=s.gocharting_email,
                    password=s.gocharting_password,
                    save_storage_state=not args.no_save_storage,
                    headless=not args.headed,
                    main_chart_symbol=getattr(args, "main_symbol", None),
                )
            tv_cm_issues = [i for i in bad if i.source in ("coinmap", "tradingview")]
            if tv_cm_issues:
                recapture_failed_chart_slots(
                    coinmap_yaml=cfg,
                    charts_dir=charts_dir,
                    stamp=stamp,
                    issues=tv_cm_issues,
                    storage_state_path=storage,
                    email=s.coinmap_email,
                    password=s.coinmap_password,
                    tradingview_password=s.tradingview_password,
                    save_storage_state=not args.no_save_storage,
                    headless=not args.headed,
                    main_chart_symbol=getattr(args, "main_symbol", None),
                )
        except SystemExit:
            raise
        except Exception as e:
            raise SystemExit(f"Recapture after validation failed: {e}") from e

    if use_gc:
        require_valid_gocharting_exports_for_stamp(charts_dir, stamp)
        _maybe_export_gocharting_mt5_spot_candles(
            charts_dir=charts_dir,
            stamp=stamp,
            mt5_accounts_json=_resolved_mt5_accounts_json(args),
            main_symbol=getattr(args, "main_symbol", None),
        )
    else:
        require_valid_coinmap_exports_for_stamp(charts_dir, stamp)

    capture_pngs = ordered_chart_images(charts_dir, stamp=stamp)
    require_openai(s)
    payloads = ordered_chart_openai_payloads(charts_dir)
    _warn_if_incomplete_chart_payloads(charts_dir, payloads)
    if not payloads:
        raise SystemExit(
            "No TradingView/Coinmap chart files found for OpenAI step "
            f"under {charts_dir}. Check capture and chart slot order (effective_chart_image_order)."
        )

    prompt_all = _resolved_analysis_prompt(args, charts_dir)
    max_images = args.max_images_per_call
    openai_model = resolved_openai_model(s, getattr(args, "model", None))

    def _openai_all_work() -> PromptTwoStepResult:
        return _run_openai_flow(
            s,
            charts_dir,
            prompt_all,
            max_images,
            chart_payloads=payloads,
            on_first_model_text=None,
            model=openai_model,
            reasoning_effort=ALL_FLOW_REASONING_EFFORT,
        )

    try:
        n_sent, out = _run_capture_telegram_log_parallel_with(
            bot_token=s.telegram_bot_token,
            telegram_log_chat_id=s.telegram_log_chat_id,
            png_paths=capture_pngs,
            header=f"all: capture screenshots | stamp={stamp} | {len(capture_pngs)} PNG",
            work_fn=_openai_all_work,
        )
    except Exception as e:
        re_raise_unless_openai(e)
    _log.info(
        "all: capture PNG (Telegram log) + OpenAI song song | stamp=%s | png_files=%s sent=%s",
        stamp,
        len(capture_pngs),
        n_sent,
    )
    print(out.full_text())
    _log.info("all: OpenAI xong | response_id=%s", out.final_response_id)

    write_last_response_id(out.final_response_id)
    write_last_all_response_id(out.final_response_id)
    if not args.no_telegram and out.after_charts:
        require_telegram(s)
        send_openai_output_to_telegram(
            bot_token=s.telegram_bot_token,
            chat_id=s.telegram_chat_id,
            raw=out.after_charts,
            default_parse_mode=s.telegram_parse_mode,
            summary_chat_id=s.telegram_output_ngan_gon_chat_id,
        )

    if out.after_charts:
        morning_obj = extract_json_object(out.after_charts)
        if morning_obj is not None:
            write_morning_full_analysis(morning_obj)
            _log.info(
                "all: đã ghi %s",
                default_morning_full_analysis_path().name,
            )
        else:
            _log.warning(
                "all: không extract được JSON object từ after_charts — không ghi %s",
                default_morning_full_analysis_path().name,
            )

        payload = parse_analysis_from_openai_text(out.after_charts)
        if payload is not None and payload.prices:
            trip = triple_from_zone_prices(payload.prices)
            if trip is not None:
                write_morning_baseline_prices(trip)
                _log.info(
                    "all: đã ghi %s",
                    default_morning_baseline_prices_path().name,
                )
            from automation_tool.images import get_active_main_symbol

            sym = get_active_main_symbol().strip().upper()
            slot: SessionSlot = run_slot
            zones = zones_from_analysis_payload(
                symbol=sym, payload=payload, source="all", session_slot=slot
            )
            if zones:
                write_zones_for_slot(symbol=sym, zones=zones, slot=slot, zones_dir=zones_dir)
                _log.info(
                    "all: đã ghi shard zones | slot=%s zones=%d | symbol=%s",
                    slot,
                    len(zones),
                    sym,
                )
            else:
                _log.warning("all: parse JSON có prices nhưng không tạo được zones — không ghi shard")
        elif out.after_charts.strip():
            print(
                "Warning: could not parse analysis JSON for zones (no `prices` or empty).",
                file=sys.stderr,
            )

        from automation_tool.ea_neverdie_zone_publish import maybe_publish_neverdie_after_cli
        from automation_tool.images import get_active_main_symbol as _get_sym_publish

        try:
            maybe_publish_neverdie_after_cli(
                symbol=_get_sym_publish().strip().upper(),
                zones_dir=zones_dir,
            )
        except Exception as e:
            _log.warning("all: ea-neverdie publish failed: %s", e)

    _run_all_second_flow(
        s,
        charts_dir=charts_dir,
        analysis_prompt=prompt_all,
        max_images_per_call=max_images,
        chart_payloads=payloads,
        no_telegram=args.no_telegram,
        model=getattr(args, "model", None),
        zones_dir=zones_dir,
        session_slot=run_slot,
        mt5_accounts_json=getattr(args, "mt5_accounts_json", None),
    )


def cmd_tv_alerts(args: argparse.Namespace) -> None:
    s = load_settings()
    cfg_tv = args.config or default_coinmap_config_path()
    storage = args.storage_state or default_storage_state_path()

    if args.from_last_alert:
        t = read_last_alert_prices()
        if t is None:
            raise SystemExit(
                f"Không đọc được {default_last_alert_prices_path()} "
                "(cần key 'prices': [a,b,c]). Chạy all/update trước hoặc dùng --prices-json FILE."
            )
        p1, p2, p3 = t
    elif args.prices_json is not None:
        t = read_last_alert_prices(args.prices_json)
        if t is None:
            raise SystemExit(
                f"Không đọc được 3 giá từ {args.prices_json} "
                "(JSON cần key 'prices': [a, b, c])."
            )
        p1, p2, p3 = t
    elif args.p1 is not None and args.p2 is not None and args.p3 is not None:
        p1, p2, p3 = args.p1, args.p2, args.p3
    else:
        raise SystemExit(
            "Cần một trong: --from-last-alert | --prices-json FILE | ba số: p1 p2 p3\n"
            "Ví dụ: coinmap-automation tv-alerts --prices-json data/last_alert_prices.json"
        )

    print(f"Đồng bộ TradingView alerts → {p1} | {p2} | {p3} (config: {cfg_tv})")
    _log.info("tv-alerts: sync | %s | %s | %s | yaml=%s", p1, p2, p3, cfg_tv)
    sync_tradingview_alerts(
        coinmap_yaml=cfg_tv,
        storage_state_path=storage,
        email=s.coinmap_email,
        tradingview_password=s.tradingview_password,
        target_prices=(p1, p2, p3),
        headless=not args.headed,
    )
    print("Xong.")


def cmd_tv_journal_monitor(args: argparse.Namespace) -> None:
    s = load_settings()
    _log.info(
        "tv-journal-monitor: bắt đầu (CLI) | poll=%s until_hour=%s",
        args.poll_seconds,
        args.until_hour,
    )
    require_openai(s)
    prev = read_last_response_id()
    if not prev:
        raise SystemExit(
            f"Missing {default_last_response_id_path()} — run `coinmap-automation all` or `update` first "
            "so OpenAI has a thread id."
        )

    if args.p1 is not None:
        if args.p2 is None or args.p3 is None:
            raise SystemExit("Khi dùng --p1 cần truyền đủ --p1 --p2 --p3.")
        targets = (args.p1, args.p2, args.p3)
    else:
        if args.p2 is not None or args.p3 is not None:
            raise SystemExit("Dùng cả ba --p1 --p2 --p3 hoặc để trống để đọc last_alert_prices.")
        t = read_last_alert_prices()
        if t is None:
            raise SystemExit(
                "Không có data/last_alert_prices.json — chạy update hoặc truyền --p1 --p2 --p3."
            )
        targets = t

    cfg_tv = args.config or default_coinmap_config_path()
    cfg_cap = args.capture_config or default_coinmap_update_config_path()
    charts_dir = args.charts_dir or default_charts_dir()
    storage = args.storage_state or default_storage_state_path()

    params = JournalMonitorParams(
        coinmap_tv_yaml=cfg_tv,
        capture_coinmap_yaml=cfg_cap,
        charts_dir=charts_dir,
        storage_state_path=storage,
        target_prices=targets,
        headless=not args.headed,
        no_save_storage=args.no_save_storage,
        poll_seconds=args.poll_seconds,
        wait_minutes=args.wait_minutes,
        until_hour=args.until_hour,
        timezone_name=args.timezone,
        no_telegram=args.no_telegram,
        last_alert_path=args.last_alert_json or default_last_alert_prices_path(),
        mt5_execute=not args.no_mt5_execute,
        mt5_symbol=args.mt5_symbol,
        mt5_dry_run=args.mt5_dry_run,
        mt5_accounts_json=_resolved_mt5_accounts_json(args),
        openai_model=resolved_openai_model(s, getattr(args, "model", None)),
        openai_model_cli=getattr(args, "model", None),
    )

    print(
        f"tv-journal-monitor: giá {targets[0]} | {targets[1]} | {targets[2]} — "
        f"mốc dừng phiên: trước 13:00→13:00 cùng ngày; từ 13:00→02:00 sáng ({args.timezone}); "
        f"fallback nếu không set session: --until-hour={args.until_hour}. "
        f"Chu kỳ: reload → Nhật ký → parse, nghỉ {args.poll_seconds}s.",
        flush=True,
    )
    print(
        f"  TV yaml: {cfg_tv} | Capture yaml: {cfg_cap} | charts: {charts_dir} | "
        f"storage: {storage} | headed={args.headed} | no_telegram={args.no_telegram}",
        flush=True,
    )
    try:
        outcome = run_tv_journal_monitor(
            settings=s,
            params=params,
            initial_response_id=prev,
        )
    except Exception as e:
        re_raise_unless_openai(e)
        raise
    print(f"Kết thúc: {outcome}")
    _log.info("tv-journal-monitor: kết thúc | outcome=%s", outcome)


def cmd_tp1_tick_dry_run(args: argparse.Namespace) -> None:
    lap = args.last_alert_json or default_last_alert_prices_path()
    text = tp1_dry_run_report(
        last_alert_path=lap,
        p_last=float(args.last),
        symbol_override=args.mt5_symbol,
        mt5_accounts_json=_resolved_mt5_accounts_json(args),
    )
    print(text, end="")


def cmd_update(args: argparse.Namespace) -> None:
    from automation_tool.images import set_active_main_symbol_file

    s = load_settings()
    if getattr(args, "main_symbol", None):
        set_active_main_symbol_file(args.main_symbol)
    _log.info(
        "update: bắt đầu | capture_yaml=%s tv_yaml=%s no_tradingview=%s no_journal_after=%s",
        args.config or default_coinmap_update_config_path(),
        args.tv_config or default_coinmap_config_path(),
        args.no_tradingview,
        getattr(args, "no_journal_monitor_after_update", False),
    )
    cfg_cap = args.config or default_coinmap_update_config_path()
    storage = args.storage_state or default_storage_state_path()
    cfg_tv = args.tv_config or default_coinmap_config_path()

    all_rid = read_last_all_response_id()
    if not (all_rid or "").strip():
        raise SystemExit(
            f"Missing {default_last_all_response_id_path()} — run `coinmap-automation all` once "
            "to seed last_all_response_id.txt for [INTRADAY_UPDATE]."
        )

    cur = (read_last_response_id() or "").strip()
    first_after_all = is_first_intraday_update_after_all(
        last_response_id=cur or None,
        last_all_response_id=all_rid,
    )
    _log.info("update: first intraday update after all=%s", first_after_all)
    if not first_after_all and not cur:
        raise SystemExit(
            f"Missing {default_last_response_id_path()} — run `coinmap-automation all` first."
        )

    _send_python_bot_job_started(
        s,
        title=f"Cập nhật vào lúc {_now_clock_hcm()} bắt đầu chạy",
        no_telegram=args.no_telegram,
    )
    charts_dir = args.charts_dir or default_charts_dir()
    paths, stamp, _main_s, tv_chart_payloads = _intraday_tv_then_coinmap_m5_capture(
        cfg_tv=cfg_tv,
        cfg_cap=cfg_cap,
        charts_dir=charts_dir,
        storage=storage,
        email=s.coinmap_email,
        password=s.coinmap_password,
        tradingview_password=s.tradingview_password,
        save_storage=not args.no_save_storage,
        headless=not args.headed,
        main_chart_symbol=args.main_symbol,
        no_tradingview=args.no_tradingview,
        flow_label="update",
    )

    print(f"Captured {len(paths)} file(s) for update run.")
    m15 = coinmap_main_pair_interval_json_path(charts_dir, "15m", stamp=stamp)
    m5 = coinmap_main_pair_interval_json_path(charts_dir, "5m", stamp=stamp)
    _log.info(
        "update: capture xong | %s file(s) | stamp=%s | M15 raw OpenAI=%s | M5 raw OpenAI=%s",
        len(paths),
        stamp,
        m15,
        m5,
    )
    if m15 is None:
        raise SystemExit(
            f"No 15m Coinmap JSON under {charts_dir} after capture (stamp={stamp!r}). "
            "Check coinmap_update.yaml capture_plan and api_data_export."
        )
    if m5 is None:
        raise SystemExit(
            f"No 5m Coinmap JSON under {charts_dir} after capture (stamp={stamp!r}). "
            "Check coinmap_update.yaml capture_plan and api_data_export."
        )
    require_valid_coinmap_exports_for_stamp(charts_dir, stamp)

    require_openai(s)

    morning_snapshot: Path | None = None
    prev_for_openai: str | None = None
    if first_after_all:
        mp = default_morning_full_analysis_path()
        if not mp.is_file():
            raise SystemExit(
                f"Missing {mp} — run `coinmap-automation all` so morning analysis is saved "
                "before the first [INTRADAY_UPDATE] after each `all`."
            )
        morning_snapshot = mp
        prev_for_openai = None
    else:
        morning_snapshot = None
        prev_for_openai = cur

    user_msg = build_intraday_update_user_text(
        first_after_all=first_after_all,
        coinmap_attachment_mode="legacy",
    )
    if tv_chart_payloads:
        user_msg += (
            "\nĐính kèm thêm ảnh TradingView cặp chính sau JSON Coinmap: **15m** Session Liquidity Check "
            "/ ICT Killzones và **5m** khung thường (không ICT).\n"
        )

    try:
        out_text, new_id = run_single_followup_responses(
            api_key=s.openai_api_key,
            user_text=user_msg,
            morning_snapshot_path=morning_snapshot,
            coinmap_json_paths=coinmap_paths,
            extra_chart_payloads=tv_chart_payloads,
            previous_response_id=prev_for_openai,
            vector_store_ids=s.openai_vector_store_ids,
            store=s.openai_responses_store,
            include=s.openai_responses_include,
            model=resolved_openai_model(s, getattr(args, "model", None)),
        )
    except Exception as e:
        re_raise_unless_openai(e)

    print(out_text)
    write_last_response_id(new_id)
    _log.info("update: OpenAI follow-up xong | new_response_id=%s", new_id)

    update_payload = parse_analysis_from_openai_text(out_text)
    zones_dir = zones_dir_from_cli_path(getattr(args, "zones_json", None))

    def _send_phan_tich_update_if_any() -> None:
        if args.no_telegram:
            return
        if update_payload is None:
            return
        text = (update_payload.phan_tich_update or "").strip()
        plan_lines = format_plan_lines_for_telegram(update_payload)
        if not text and not plan_lines:
            return
        require_telegram(s)
        parts: list[str] = []
        if text:
            parts.append("Phản hồi sau khi cập nhật: " + text)
        if plan_lines:
            parts.append(plan_lines)
        message = "\n\n".join(parts)
        send_message(
            bot_token=s.telegram_bot_token,
            chat_id=s.telegram_chat_id,
            text=message,
            parse_mode=s.telegram_parse_mode,
        )

    # Apply old plan decisions (Schema B: old_prices) to existing zones first.
    if update_payload is not None and getattr(update_payload, "old_prices", None):
        try:
            from automation_tool.zones_state import (
                ZonesState,
                can_apply_old_price_loai,
                read_zones_state,
                write_zones_state_to_shard,
            )
            from automation_tool.zones_paths import shard_path as _zone_shard_path

            st0 = read_zones_state(zones_dir)
            if st0 is not None and st0.zones:
                for dec in update_payload.old_prices:
                    if dec.hanh_dong != "loại":
                        continue
                    want_label = (dec.label or "").strip().lower()
                    want_vc = (dec.vung_cho or "").strip()
                    if not want_label or not want_vc:
                        continue
                    for z in st0.zones:
                        if (z.label or "").strip().lower() != want_label:
                            continue
                        if (z.vung_cho or "").strip() != want_vc:
                            continue
                        if not can_apply_old_price_loai(z.status):
                            continue
                        # Mark exact waiting/touched zone as loai (avoid wrong label-only match).
                        z.status = "loai"  # type: ignore[assignment]
                        z.retry_at = ""
                        slot = getattr(z, "session_slot", None)
                        if isinstance(slot, str) and slot.strip() in ("sang", "chieu", "toi"):
                            sp = _zone_shard_path(
                                zones_dir, want_label, slot.strip()  # type: ignore[arg-type]
                            )
                            write_zones_state_to_shard(
                                sp,
                                ZonesState(
                                    symbol=st0.symbol,
                                    zones=[z],
                                    updated_at=st0.updated_at,
                                    last_observed=st0.last_observed,
                                ),
                            )
                        break
        except Exception as e:
            _log.warning("update: apply old_prices -> loai failed: %s", e)

    if update_payload is not None and update_payload.prices:
        from automation_tool.images import get_active_main_symbol

        sym = get_active_main_symbol().strip().upper()
        slot: SessionSlot = session_slot_now_hcm()
        zones = zones_from_analysis_payload(
            symbol=sym,
            payload=update_payload,
            source="update",
            session_slot=slot,
        )
        if zones:
            write_zones_for_slot(symbol=sym, zones=zones, slot=slot, zones_dir=zones_dir)
            _log.info(
                "update: đã ghi shard zones | slot=%s zones=%d | symbol=%s",
                slot,
                len(zones),
                sym,
            )

    if update_payload is not None:
        from automation_tool.ea_neverdie_zone_publish import maybe_publish_neverdie_after_cli
        from automation_tool.images import get_active_main_symbol as _sym_up_neverdie

        try:
            maybe_publish_neverdie_after_cli(
                symbol=_sym_up_neverdie().strip().upper(),
                zones_dir=zones_dir,
            )
        except Exception as e:
            _log.warning("update: ea-neverdie publish failed: %s", e)

    lap = args.last_alert_json or default_last_alert_prices_path()

    new_triple, zerr, no_change_json = parse_update_zone_triple(out_text)
    if no_change_json is True:
        _send_phan_tich_update_if_any()
        _log.info("update: no_change (JSON) — không ghi giá mới")
        return
    if new_triple is None:
        if is_no_change_action_line(out_text):
            _send_phan_tich_update_if_any()
            _log.info("update: no_change (action line) — không ghi giá mới")
            return
        if zerr is None and update_payload is not None and update_payload.prices:
            try:
                merge_trade_lines_from_openai_analysis_text(out_text, path=lap)
            except Exception as e:
                _log.warning("update: merge trade_line từ JSON — %s", e)
            _send_phan_tich_update_if_any()
            _log.info(
                "update: JSON prices có %d plan, chưa đủ triple — bỏ qua ghi last_alert_prices",
                len(update_payload.prices),
            )
            return
        _send_phan_tich_update_if_any()
        raise SystemExit(zerr or "Could not parse three zone prices from model output.")

    try:
        merge_trade_lines_from_openai_analysis_text(out_text, path=lap)
    except Exception as e:
        _log.warning("update: merge trade_line từ JSON — %s", e)

    write_last_alert_prices(new_triple)
    _log.info(
        "update: đã ghi last_alert_prices | %s | %s | %s",
        new_triple[0],
        new_triple[1],
        new_triple[2],
    )

    # Không cần tạo TradingView alerts nữa; monitor đọc trực tiếp giá realtime từ Watchlist.

    _send_phan_tich_update_if_any()


def cmd_update_scalp(args: argparse.Namespace) -> None:
    """
    Luồng ``update-scalp``: TradingView **15m ICT + 5m**, rồi Coinmap **M15 + M5** (JSON + PNG,
    ``bearer_request``) → OpenAI, tìm plan scalp đẹp nhất. Dùng ``--no-tradingview`` để chỉ Coinmap.
    - Vector store: ``OPENAI_UPDATE_SCALP_VECTOR_STORE_ID(S)``; nếu không set thì giống ``all-2``
      (``_ALL_SECOND_FLOW_VECTOR_STORE_ID``), không dùng ``OPENAI_VECTOR_STORE_IDS``.
    - Thread OpenAI riêng (``last_scalp_response_id.txt``).
    - Zone labels dạng ``scalp_<id>`` (ví dụ: ``scalp_1``, ``scalp_2``, ``scalp_3``).
    - Zones lưu vào ``data/<SYM>/zones/`` cùng với zones thông thường.
    - Nếu có ``accounts.json`` (CLI ``--mt5-accounts-json`` hoặc ``MT5_ACCOUNTS_JSON``):
      tạo ``accounts-scalp.json`` cạnh file nguồn với các account có ``\"update-scalp\": true``,
      rồi đặt ``MT5_ACCOUNTS_JSON`` cho tiến trình (reconcile spawn ``daemon-plan`` kế thừa env).
    """
    from automation_tool.images import (
        coinmap_png_path_for_json,
        set_active_main_symbol_file,
    )

    s = load_settings()
    if getattr(args, "main_symbol", None):
        set_active_main_symbol_file(args.main_symbol)
    zones_dir = zones_dir_from_cli_path(getattr(args, "zones_json", None))
    base_acc = resolve_mt5_accounts_path(getattr(args, "mt5_accounts_json", None))
    if base_acc is not None and base_acc.is_file():
        try:
            scalp_acc_path = sync_accounts_scalp_json(base_acc)
        except Exception as e:
            raise SystemExit(f"Không tạo accounts-scalp.json từ {base_acc}: {e}") from e
        if scalp_acc_path is not None:
            os.environ["MT5_ACCOUNTS_JSON"] = str(scalp_acc_path)
            _log.info("update-scalp: MT5_ACCOUNTS_JSON → %s (subset update-scalp)", scalp_acc_path)
        else:
            _log.info(
                'update-scalp: không có account nào "update-scalp": true trong %s — '
                "giữ MT5_ACCOUNTS_JSON như môi trường hiện tại",
                base_acc,
            )
    cfg_cap = args.config or default_coinmap_update_config_path()
    cfg_tv = args.tv_config or default_coinmap_config_path()
    storage = args.storage_state or default_storage_state_path()
    use_gc = bool(getattr(args, "gocharting", False))
    gc_yaml = getattr(args, "gocharting_config", None) or default_gocharting_config_path()
    _log.info(
        "update-scalp: bắt đầu | capture_yaml=%s tv_yaml=%s no_tradingview=%s gocharting=%s",
        cfg_cap,
        cfg_tv,
        args.no_tradingview,
        use_gc,
    )

    mp = default_morning_full_analysis_path()
    if not mp.is_file():
        raise SystemExit(
            f"Missing {mp} — run `coinmap-automation all` so morning analysis is saved "
            "before running update-scalp."
        )

    _send_python_bot_job_started(
        s,
        title=f"Scalp update vào lúc {_now_clock_hcm()} bắt đầu chạy",
        no_telegram=args.no_telegram,
    )
    charts_dir = args.charts_dir or default_charts_dir()
    paths, stamp, _main_s, tv_chart_payloads = _intraday_tv_then_coinmap_m5_capture(
        cfg_tv=cfg_tv,
        cfg_cap=cfg_cap,
        charts_dir=charts_dir,
        storage=storage,
        email=s.coinmap_email or "",
        password=s.coinmap_password or "",
        tradingview_password=s.tradingview_password or "",
        save_storage=not args.no_save_storage,
        headless=not args.headed,
        main_chart_symbol=args.main_symbol,
        no_tradingview=args.no_tradingview,
        flow_label="update-scalp",
        use_gocharting=use_gc,
        gocharting_yaml=gc_yaml,
        gocharting_email=s.gocharting_email or "",
        gocharting_password=s.gocharting_password or "",
        gocharting_detail_history_steps=GOCHARTING_UPDATE_SCALP_DETAIL_HISTORY_STEPS,
    )

    print(f"Captured {len(paths)} file(s) for update-scalp run.")
    footprint_paths: list[Path]
    if use_gc:
        m15 = gocharting_main_interval_csv_path(charts_dir, "15m", stamp=stamp)
        m5 = gocharting_main_interval_csv_path(charts_dir, "5m", stamp=stamp)
        _log.info(
            "update-scalp: capture xong | %s file(s) | stamp=%s | M15 CSV=%s | M5 CSV=%s",
            len(paths),
            stamp,
            m15,
            m5,
        )
        if m15 is None:
            raise SystemExit(
                f"No 15m GoCharting CSV under {charts_dir} after capture (stamp={stamp!r}). "
                "Check config/gocharting.yaml capture_plan."
            )
        if m5 is None:
            raise SystemExit(
                f"No 5m GoCharting CSV under {charts_dir} after capture (stamp={stamp!r}). "
                "Check config/gocharting.yaml capture_plan."
            )
        require_valid_gocharting_exports_for_stamp(charts_dir, stamp or "")
        mt5_spot = _maybe_export_gocharting_mt5_spot_candles(
            charts_dir=charts_dir,
            stamp=stamp or "",
            mt5_accounts_json=resolve_mt5_accounts_path(getattr(args, "mt5_accounts_json", None)),
            main_symbol=getattr(args, "main_symbol", None),
        )
        footprint_paths = [m15, m5]
        if mt5_spot is not None:
            footprint_paths.append(mt5_spot)
        m15_png = gocharting_png_path_for_csv(m15)
        m5_png = gocharting_png_path_for_csv(m5)
        m15_detail = gocharting_detail_zoom_png_path_for_csv(m15)
        m5_detail = gocharting_detail_zoom_png_path_for_csv(m5)
        _log.info(
            "update-scalp: GoCharting PNG | M15=%s | M5=%s | detail M15=%s | detail M5=%s",
            m15_png,
            m5_png,
            m15_detail,
            m5_detail,
        )
        if m15_png is None or m5_png is None:
            print(
                "Warning: thiếu PNG GoCharting overview sau capture "
                f"(M15={m15_png is not None}, M5={m5_png is not None}).",
                file=sys.stderr,
            )
        if m15_detail is None or m5_detail is None:
            print(
                "Warning: thiếu PNG GoCharting detail zoom sau capture "
                f"(M15={m15_detail is not None}, M5={m5_detail is not None}).",
                file=sys.stderr,
            )
    else:
        m15 = coinmap_main_pair_interval_json_path(charts_dir, "15m", stamp=stamp)
        m5 = coinmap_main_pair_interval_json_path(charts_dir, "5m", stamp=stamp)
        _log.info(
            "update-scalp: capture xong | %s file(s) | stamp=%s | M15 raw OpenAI=%s | M5 raw OpenAI=%s",
            len(paths),
            stamp,
            m15,
            m5,
        )
        if m15 is None:
            raise SystemExit(
                f"No 15m Coinmap JSON under {charts_dir} after capture (stamp={stamp!r}). "
                "Check coinmap_update.yaml capture_plan and api_data_export."
            )
        if m5 is None:
            raise SystemExit(
                f"No 5m Coinmap JSON under {charts_dir} after capture (stamp={stamp!r}). "
                "Check coinmap_update.yaml capture_plan and api_data_export."
            )
        require_valid_coinmap_exports_for_stamp(charts_dir, stamp or "")
        footprint_paths = [m15, m5]
        from automation_tool.images import coinmap_png_path_for_json

        m15_png = coinmap_png_path_for_json(m15)
        m5_png = coinmap_png_path_for_json(m5)
        _log.info(
            "update-scalp: Coinmap PNG | M15=%s | M5=%s",
            m15_png,
            m5_png,
        )
        if m15_png is None or m5_png is None:
            print(
                "Warning: thiếu PNG Coinmap fullscreen sau capture "
                f"(M15={m15_png is not None}, M5={m5_png is not None}). "
                "Kiểm tra chart_download.coinmap_screenshot_enabled trong coinmap_update.yaml.",
                file=sys.stderr,
            )

    capture_pngs = ordered_chart_images(charts_dir, stamp=stamp)
    require_openai(s)

    morning_snapshot: Path = mp
    prev_for_openai: str | None = None

    user_msg = build_scalp_update_user_text(
        first_after_all=True,
        coinmap_attachment_mode="legacy",
        footprint_source="gocharting" if use_gc else "coinmap",
    )
    if tv_chart_payloads:
        fp_label = "GoCharting CSV" if use_gc else "JSON Coinmap"
        user_msg += (
            f"\nĐính kèm thêm ảnh TradingView cặp chính sau {fp_label}: **15m** Session Liquidity Check "
            "/ ICT Killzones và **5m** khung thường (không ICT).\n"
        )

    scalp_vector_store_ids = resolve_update_scalp_vector_store_ids(
        s,
        fallback=[_ALL_SECOND_FLOW_VECTOR_STORE_ID],
    )
    scalp_openai_model = resolved_openai_model(s, getattr(args, "model", None))

    def _openai_scalp_work() -> tuple[str, str]:
        return run_single_followup_responses(
            api_key=s.openai_api_key,
            user_text=user_msg,
            morning_snapshot_path=morning_snapshot,
            coinmap_json_paths=footprint_paths,
            extra_chart_payloads=tv_chart_payloads,
            previous_response_id=prev_for_openai,
            vector_store_ids=scalp_vector_store_ids,
            store=s.openai_responses_store,
            include=s.openai_responses_include,
            model=scalp_openai_model,
        )

    try:
        n_sent, (out_text, new_id) = _run_capture_telegram_log_parallel_with(
            bot_token=s.telegram_bot_token,
            telegram_log_chat_id=s.telegram_log_chat_id,
            png_paths=capture_pngs,
            header=f"update-scalp: capture screenshots | stamp={stamp} | {len(capture_pngs)} PNG",
            work_fn=_openai_scalp_work,
        )
    except Exception as e:
        re_raise_unless_openai(e)
    _log.info(
        "update-scalp: capture PNG (Telegram log) + OpenAI song song | stamp=%s | png_files=%s sent=%s",
        stamp,
        len(capture_pngs),
        n_sent,
    )

    print(out_text)
    write_last_scalp_response_id(new_id)
    _log.info(
        "update-scalp: OpenAI follow-up xong | new_response_id=%s | vector_store=%s | tv_payloads=%s",
        new_id,
        scalp_vector_store_ids,
        len(tv_chart_payloads),
    )

    update_payload = parse_analysis_from_openai_text(out_text)

    def _send_phan_tich_scalp_if_any() -> None:
        if args.no_telegram:
            return
        if update_payload is None:
            return
        text = (update_payload.phan_tich_update or "").strip()
        plan_lines = format_plan_lines_for_telegram(update_payload)
        if not text and not plan_lines:
            return
        require_telegram(s)
        parts: list[str] = []
        if text:
            parts.append("Scalp update: " + text)
        if plan_lines:
            parts.append(plan_lines)
        message = "\n\n".join(parts)
        send_message(
            bot_token=s.telegram_bot_token,
            chat_id=s.telegram_chat_id,
            text=message,
            parse_mode=s.telegram_parse_mode,
        )

    if update_payload is not None and update_payload.prices:
        from automation_tool.images import get_active_main_symbol
        sym = get_active_main_symbol().strip().upper()
        slot: SessionSlot = session_slot_now_hcm()
        scalp_zones = zones_from_scalp_payload(
            symbol=sym,
            payload=update_payload,
            source="update-scalp",
            session_slot=slot,
        )
        if scalp_zones:
            scalp_zones = remap_scalp_zones_avoiding_shard_collision(
                scalp_zones,
                zones_dir=zones_dir,
                slot=slot,
            )
            write_zones_for_slot(
                symbol=sym,
                zones=scalp_zones,
                slot=slot,
                zones_dir=zones_dir,
                update_manifest_slot=False,
            )
            _log.info(
                "update-scalp: đã ghi shard zones | slot=%s zones=%d labels=%s | dir=%s",
                slot,
                len(scalp_zones),
                [z.label for z in scalp_zones],
                zones_dir,
            )

    _send_phan_tich_scalp_if_any()
    if not args.no_reconcile_daemon_plans:
        _reconcile_daemon_plans_after_cli(zones_dir, "update-scalp")


def cmd_restore_morning_zones(args: argparse.Namespace) -> None:
    from automation_tool.images import get_active_main_symbol, set_active_main_symbol_file

    if getattr(args, "main_symbol", None):
        set_active_main_symbol_file(args.main_symbol)

    morning_path = Path(getattr(args, "morning_json", None) or default_morning_full_analysis_path()).expanduser()
    if not morning_path.is_file():
        raise SystemExit(
            f"Missing {morning_path} — cần file morning_full_analysis.json đã lưu từ lệnh `all`."
        )

    try:
        raw = json.loads(morning_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"Không đọc được JSON từ {morning_path}: {e}") from e
    if not isinstance(raw, dict):
        raise SystemExit(f"File {morning_path} phải là JSON object của morning_full_analysis.")

    payload = try_parse_analysis_payload(raw)
    if payload is None or not payload.prices:
        raise SystemExit(f"File {morning_path} không có `prices` hợp lệ để tạo shard zones.")

    slot: SessionSlot = getattr(args, "slot", "sang")
    symbol = str(getattr(args, "main_symbol", None) or get_active_main_symbol()).strip().upper()
    zones_dir = zones_dir_from_cli_path(getattr(args, "zones_json", None))
    zones = zones_from_analysis_payload(
        symbol=symbol,
        payload=payload,
        source="all",
        session_slot=slot,
    )
    if not zones:
        raise SystemExit(f"File {morning_path} có `prices` nhưng không tạo được zone nào.")

    write_zones_for_slot(symbol=symbol, zones=zones, slot=slot, zones_dir=zones_dir)
    _log.info(
        "restore-morning-zones: đã ghi shard zones | slot=%s zones=%d | symbol=%s | morning=%s",
        slot,
        len(zones),
        symbol,
        morning_path,
    )
    print(
        f"Đã tạo lại {len(zones)} shard zone từ {morning_path.name} vào {zones_dir} (slot={slot}).",
        flush=True,
    )


def cmd_tv_watchlist_daemon(args: argparse.Namespace) -> None:
    s = load_settings()
    require_openai(s)
    cfg_tv = args.config or default_coinmap_config_path()
    cfg_cap = args.capture_config or default_coinmap_update_config_path()
    charts_dir = args.charts_dir or default_charts_dir()
    storage = args.storage_state or default_storage_state_path()

    params = WatchlistDaemonParams(
        coinmap_tv_yaml=cfg_tv,
        capture_coinmap_yaml=cfg_cap,
        charts_dir=charts_dir,
        storage_state_path=storage,
        headless=not args.headed,
        no_save_storage=args.no_save_storage,
        poll_seconds=float(args.poll_seconds),
        no_telegram=args.no_telegram,
        zones_state_path=None,
        last_price_path=getattr(args, "last_price_file", None),
        mirror_last_price_file=bool(getattr(args, "mirror_last_price_file", False)),
        stop_daemon_plans_on_exit=bool(getattr(args, "stop_daemon_plans_on_exit", False)),
        mt5_execute=not args.no_mt5_execute,
        mt5_symbol=args.mt5_symbol,
        mt5_dry_run=args.mt5_dry_run,
        mt5_accounts_json=_resolved_mt5_accounts_json(args),
        eps=float(args.eps),
        openai_model=resolved_openai_model(s, getattr(args, "model", None)),
        openai_model_cli=getattr(args, "model", None),
        last_price_from_mt5=not bool(getattr(args, "tv_symbol_price", False)),
        mt5_stale_reconnect_seconds=float(
            getattr(args, "mt5_stale_reconnect_seconds", 10.0) or 0.0
        ),
    )
    outcome = run_tv_watchlist_daemon(settings=s, params=params)
    print(outcome, flush=True)


def cmd_daemon_plan(args: argparse.Namespace) -> None:
    s = load_settings()
    require_openai(s)
    cfg_tv = args.config or default_coinmap_config_path()
    cfg_cap = args.capture_config or default_coinmap_update_config_path()
    charts_dir = args.charts_dir or default_charts_dir()
    storage = args.storage_state or default_storage_state_path()
    shard = args.shard.expanduser().resolve()
    raw_stop_h = getattr(args, "stop_at_hour", None)
    if raw_stop_h is None:
        stop_at_hour = None
    else:
        stop_h = int(raw_stop_h)
        stop_at_hour = -1 if stop_h < 0 else stop_h
    params = WatchlistDaemonParams(
        coinmap_tv_yaml=cfg_tv,
        capture_coinmap_yaml=cfg_cap,
        charts_dir=charts_dir,
        storage_state_path=storage,
        headless=not args.headed,
        no_save_storage=args.no_save_storage,
        poll_seconds=float(args.poll_seconds),
        timezone_name=str(getattr(args, "timezone", None) or "Asia/Ho_Chi_Minh"),
        no_telegram=args.no_telegram,
        zones_state_path=None,
        shard_path=shard,
        last_price_path=getattr(args, "last_price_file", None),
        mt5_execute=not args.no_mt5_execute,
        mt5_symbol=args.mt5_symbol,
        mt5_dry_run=args.mt5_dry_run,
        mt5_accounts_json=_resolved_mt5_accounts_json(args),
        eps=float(args.eps),
        openai_model=resolved_openai_model(s, getattr(args, "model", None)),
        openai_model_cli=getattr(args, "model", None),
        stop_at_hour=stop_at_hour,
        stop_at_minute=int(getattr(args, "stop_at_minute", 0) or 0),
    )
    outcome = run_daemon_plan(settings=s, params=params)
    print(outcome, flush=True)


def cmd_reconcile_daemon_plans(args: argparse.Namespace) -> None:
    load_settings()
    zd = zones_dir_from_cli_path(getattr(args, "zones_json", None))
    _reconcile_daemon_plans_after_cli(zd, "reconcile-daemon-plans")


def cmd_stop_daemon_plans(args: argparse.Namespace) -> None:
    load_settings()
    zd = zones_dir_from_cli_path(getattr(args, "zones_json", None))
    n = stop_daemon_plans_in_zones(zd)
    print(f"stop-daemon-plans: signalled {n} process(es) | dir={zd}", flush=True)


def cmd_zone_touch(args: argparse.Namespace) -> None:
    """
    Manual run: execute one zone-touch job synchronously.
    (Daemon uses the same underlying worker logic, but fire-and-forget.)
    """
    s = load_settings()
    require_openai(s)
    cfg_tv = args.config or default_coinmap_config_path()
    cfg_cap = args.capture_config or default_coinmap_update_config_path()
    charts_dir = args.charts_dir or default_charts_dir()
    storage = args.storage_state or default_storage_state_path()

    params = WatchlistDaemonParams(
        coinmap_tv_yaml=cfg_tv,
        capture_coinmap_yaml=cfg_cap,
        charts_dir=charts_dir,
        storage_state_path=storage,
        headless=not args.headed,
        no_save_storage=args.no_save_storage,
        poll_seconds=10.0,
        no_telegram=args.no_telegram,
        zones_state_path=args.zones_json,
        mt5_execute=not args.no_mt5_execute,
        mt5_symbol=args.mt5_symbol,
        mt5_dry_run=args.mt5_dry_run,
        mt5_accounts_json=_resolved_mt5_accounts_json(args),
        openai_model=resolved_openai_model(s, getattr(args, "model", None)),
        openai_model_cli=getattr(args, "model", None),
    )

    # Reuse daemon worker by importing and calling it directly.
    from automation_tool.tv_watchlist_daemon import _zone_touch_job  # type: ignore

    _zone_touch_job(
        settings=s,
        params=params,
        zone_id=str(args.zone_id),
        last_price=float(args.last),
    )


def main() -> None:
    _configure_stdio_utf8()
    from automation_tool.data_migration import migrate_legacy_flat_data_layout

    migrate_legacy_flat_data_layout()
    setup_automation_logging(load_settings())
    parser = _parser()
    args = parser.parse_args()
    _log.info("CLI argv: %s", " ".join(sys.argv[1:]))
    args.func(args)


if __name__ == "__main__":
    main()
