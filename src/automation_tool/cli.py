from __future__ import annotations

import argparse
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
from typing import Optional, Sequence

from automation_tool.coinmap import capture_charts, load_coinmap_yaml
from automation_tool.config import (
    default_charts_dir,
    default_coinmap_config_path,
    default_coinmap_update_config_path,
    default_data_dir,
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
)
from automation_tool.openai_errors import re_raise_unless_openai
from automation_tool.openai_prompt_flow import (
    PromptTwoStepResult,
    build_intraday_update_user_text,
    default_analysis_prompt,
    is_first_intraday_update_after_all,
    run_analysis_responses_flow,
    run_single_followup_responses,
)
from automation_tool.images import (
    CHART_SLOT_COUNT,
    ChartOpenAIPayload,
    coinmap_main_pair_interval_json_path,
    effective_chart_image_order,
    latest_chart_stamp,
    ordered_chart_openai_payloads,
    stamp_from_capture_paths,
)
from automation_tool.chart_payload_validate import list_invalid_chart_slots_for_stamp
from automation_tool.chart_recapture import recapture_failed_chart_slots
from automation_tool.first_response_trade import apply_first_response_vao_lenh
from automation_tool.state_files import (
    default_last_alert_prices_path,
    default_last_all_response_id_path,
    default_last_response_id_path,
    default_morning_baseline_prices_path,
    default_morning_full_analysis_path,
    merge_trade_lines_from_openai_analysis_text,
    read_last_alert_prices,
    read_last_all_response_id,
    read_last_response_id,
    write_last_alert_prices,
    write_last_all_response_id,
    write_last_response_id,
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
from automation_tool.zones_paths import SessionSlot, session_slot_now_hcm
from automation_tool.zones_state import (
    clear_zones_directory,
    migrate_legacy_zones_state_if_needed,
    write_zones_for_slot,
    zones_from_analysis_payload,
)
from automation_tool.telegram_bot import (
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
from automation_tool.mt5_accounts import load_mt5_accounts_for_cli
from automation_tool.mt5_execute import check_mt5_login, execute_trade, format_mt5_execution_for_telegram
from automation_tool.mt5_multi import execute_trade_all_accounts, format_mt5_multi_for_telegram

from playwright.sync_api import sync_playwright

from automation_tool.playwright_browser import close_browser_and_context, launch_chrome_context

_log = logging.getLogger("automation_tool.cli")

_HCM = ZoneInfo("Asia/Ho_Chi_Minh")


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
    "OpenAI Responses API model id (e.g. gpt-5.2). "
    "Overrides OPENAI_MODEL env and the model configured on the dashboard prompt."
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

    a = sub.add_parser(
        "analyze",
        help="OpenAI: một lần gọi multimodal với chart (no capture; uses OPENAI_PROMPT_ID)",
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
        default=CHART_SLOT_COUNT,
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
            "Chat/channel nhận OUTPUT chi tiết (markers / JSON out_chi_tiet); "
            "mặc định TELEGRAM_ANALYSIS_DETAIL_CHAT_ID trong .env"
        ),
    )
    a.add_argument("--model", default=None, metavar="ID", help=_OPENAI_MODEL_HELP)
    a.set_defaults(func=cmd_analyze)

    tcj = sub.add_parser(
        "test-cloudinary-json",
        help="Upload mọi file *.json trong charts-dir lên Cloudinary (raw) — kiểm tra CLOUDINARY_*",
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
    tcj.add_argument(
        "--purge-first",
        action="store_true",
        help="Trước khi upload: xóa raw trong CLOUDINARY_JSON_FOLDER (giống purge khi analyze)",
    )
    tcj.set_defaults(func=cmd_test_cloudinary_json)

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
        default=CHART_SLOT_COUNT,
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
        help="Chat/channel for detailed execution logs (markers / JSON out_chi_tiet)",
    )
    am.add_argument("--model", default=None, metavar="ID", help=_OPENAI_MODEL_HELP)
    am.set_defaults(func=cmd_analyze_many)

    al = sub.add_parser(
        "all",
        help=(
            "đầu phiên: xóa zones_state.json (trừ --no-clear-zones-state) → capture → OpenAI → "
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
        default=CHART_SLOT_COUNT,
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
            "Đầu phiên all: xóa file zones_state này trước capture "
            "(mặc định: data/<SYM>/zones_state.json theo cặp active sau --main-symbol / env)"
        ),
    )
    al.add_argument(
        "--no-clear-zones-state",
        action="store_true",
        help=(
            "Không xóa zones_state.json trước capture/phân tích "
            "(mặc định: xóa để phiên all không kế thừa zone/status cũ)"
        ),
    )
    al.add_argument("--model", default=None, metavar="ID", help=_OPENAI_MODEL_HELP)
    al.set_defaults(func=cmd_all)

    tl = sub.add_parser(
        "telegram-listen",
        help="Listen inbound Telegram messages in a channel/group (poll getUpdates). Supports /full.",
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
        help="Symbol used when /update triggers (default: XAUUSD). On Windows runs run_update.bat.",
    )
    tl.add_argument("--model", default=None, metavar="ID", help=_OPENAI_MODEL_HELP)
    tl.set_defaults(func=cmd_telegram_listen)

    up = sub.add_parser(
        "update",
        help="Intraday: Coinmap M5 JSON + OpenAI follow-up (same thread); rồi chạy tv-watchlist-monitor",
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
        help="Skip TradingView monitoring step (still updates last_response_id)",
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
            "Không gọi execute_trade khi follow-up có vùng đủ hop_luu (plan_chinh/plan_phu>=85, scalp>=65) + trade_line (mặc định: gọi; cần Windows + MetaTrader5)"
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

    wd = sub.add_parser(
        "tv-watchlist-daemon",
        help=(
            "Daemon giá: mặc định đọc MT5 bid → shared memory / optional last.txt; "
            "``--tv-title-price`` = đọc Last từ title tab TradingView (cần chart_url). "
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
        default=60.0,
        metavar="SEC",
        help=(
            "Daemon giá (MT5 bid): nếu bid không đổi trong SEC giây thì reconnect MT5 "
            "(shutdown + initialize; ưu tiên MT5_* env nếu có). 0 = tắt. Mặc định 60."
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
        help="Đọc Last từ title TradingView (cũ); mặc định: MT5 bid → shared memory (không cần mở browser)",
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
            "cập nhật zone tuần tự; thoát khi done/loại hoặc đến --stop-at-hour (mặc định 0 = 12h đêm): "
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
    dp.add_argument("--poll-seconds", type=float, default=1.0)
    dp.add_argument(
        "--timezone",
        default="Asia/Ho_Chi_Minh",
        help="Múi giờ IANA cho --stop-at-hour (mặc định: Asia/Ho_Chi_Minh)",
    )
    dp.add_argument(
        "--stop-at-hour",
        type=int,
        default=0,
        metavar="H",
        help=(
            "Mốc cắt giờ local (kèm --stop-at-minute): 0 = 12h đêm (00:00 ngày kế, tức 24h); "
            "1-23 = giờ trong ngày; -1 = tắt cắt giờ (chỉ thoát done/loại)."
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
        default=CHART_SLOT_COUNT,
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
        is_service_responding,
        load_browser_service_state,
        spawn_browser_service_detached,
        wait_for_service_ping,
        wait_for_state_file,
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

    proc = spawn_browser_service_detached(cwd=Path.cwd())
    st = wait_for_state_file(timeout_s=90.0)
    if not st:
        proc.kill()
        err = ""
        if proc.stderr:
            try:
                err = proc.stderr.read().decode("utf-8", errors="replace")[:2000]
            except Exception:
                pass
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
            f"Check PLAYWRIGHT_CHROME_USER_DATA_DIR / Playwright install. stderr: {err!r}"
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
            "Browser service wrote state but control plane did not respond to ping in time."
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
) -> PromptTwoStepResult:
    return run_analysis_responses_flow(
        api_key=s.openai_api_key,
        prompt_id=s.openai_prompt_id,
        prompt_version=s.openai_prompt_version,
        charts_dir=charts_dir,
        analysis_prompt=analysis_prompt,
        max_images_per_call=max_images,
        vector_store_ids=s.openai_vector_store_ids,
        store=s.openai_responses_store,
        include=s.openai_responses_include,
        chart_paths=chart_paths,
        chart_payloads=chart_payloads,
        on_first_model_text=on_first_model_text,
        purge_json_attachment_storage=purge_json_attachment_storage,
        purge_openai_user_data_files=purge_openai_user_data_files,
        model=model,
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
        )

    charts_dir = args.charts_dir or default_charts_dir()
    print(f"Saved {len(paths)} image(s) under {charts_dir}:")
    _log.info("capture: xong | %s file(s) → %s", len(paths), charts_dir)
    for pth in paths:
        print(f"  {pth}")


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

    return default_analysis_prompt(read_main_chart_symbol(charts_dir))


def _warn_if_incomplete_chart_payloads(
    charts_dir: Path, payloads: list[ChartOpenAIPayload]
) -> None:
    expected = len(effective_chart_image_order(charts_dir))
    if len(payloads) < expected:
        print(
            f"Warning: expected {expected} chart slots in fixed order, found {len(payloads)} file(s) on disk.",
            file=sys.stderr,
        )


def cmd_test_cloudinary_json(args: argparse.Namespace) -> None:
    """Upload every ``*.json`` in charts-dir to Cloudinary raw (same path as OpenAI ``file_url``)."""
    from automation_tool.cloudinary_json import purge_json_attachment_folder, upload_json_bytes_for_responses
    from automation_tool.images import set_active_main_symbol_file
    from automation_tool.openai_prompt_flow import _default_max_coinmap_json_chars, _json_file_header_and_body

    if getattr(args, "main_symbol", None):
        set_active_main_symbol_file(args.main_symbol)
    charts_dir = args.charts_dir or default_charts_dir()
    if not charts_dir.is_dir():
        raise SystemExit(f"Charts directory not found: {charts_dir}")
    paths = sorted(charts_dir.glob("*.json"))
    if not paths:
        raise SystemExit(f"No .json files under {charts_dir}")
    if args.purge_first:
        n = purge_json_attachment_folder()
        print(f"purge-first: removed {n} raw object(s) under CLOUDINARY_JSON_FOLDER prefix", flush=True)
    _log.info(
        "test-cloudinary-json: charts_dir=%s files=%d purge_first=%s",
        charts_dir,
        len(paths),
        args.purge_first,
    )
    urls: list[str] = []
    for p in paths:
        body = p.read_bytes()
        try:
            url = upload_json_bytes_for_responses(body, p.name)
        except Exception as e:
            raise SystemExit(f"{p.name}: upload failed: {e}") from e
        urls.append(url)
        print(f"{p.name}\t{url}", flush=True)
    print(f"OK: {len(paths)} file(s)", flush=True)

    mx = _default_max_coinmap_json_chars()
    preview_prompt = (
        "[test-cloudinary-json] Preview: chỉ các file *.json trong thư mục (thứ tự sort), "
        "không gồm ảnh. Lệnh analyze thật dùng prompt đầy đủ + chart theo ordered_chart_openai_payloads.\n"
    )
    content: list[dict[str, str]] = [{"type": "input_text", "text": preview_prompt}]
    for p, u in zip(paths, urls):
        header, _body_ignored = _json_file_header_and_body(p, max_chars=mx)
        content.append({"type": "input_text", "text": header})
        content.append({"type": "input_file", "file_url": u})
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
        # out_chi_tiet → TELEGRAM_CHAT_ID; output_ngan_gon → TELEGRAM_OUTPUT_NGAN_GON_CHAT_ID.
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
            # New flow requirement: only send OUTPUT_CHI_TIET to TELEGRAM_ANALYSIS_DETAIL_CHAT_ID
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


def cmd_all(args: argparse.Namespace) -> None:
    s = load_settings()
    from automation_tool.images import set_active_main_symbol_file

    if getattr(args, "main_symbol", None):
        set_active_main_symbol_file(args.main_symbol)

    zones_dir = zones_dir_from_cli_path(args.zones_json)
    if not args.no_clear_zones_state:
        stop_daemon_plans_in_zones(zones_dir)
        n_rm = clear_zones_directory(zones_dir)
        _log.info("all: cleared zones | removed=%s dir=%s", n_rm, zones_dir)
        print(f"Đã dừng daemon-plan (nếu có) và xóa zones/: {zones_dir} ({n_rm} file)", flush=True)

    cfg = args.config or default_coinmap_config_path()
    storage = args.storage_state or default_storage_state_path()
    _log.info(
        "all: bắt đầu | tv_yaml=%s charts=%s no_tradingview=%s no_tv_journal=%s",
        cfg,
        args.charts_dir if args.charts_dir is not None else "(default)",
        args.no_tradingview,
        args.no_tv_journal_monitor,
    )
    _send_python_bot_job_started(
        s,
        title="Phân tích đầu ngày bắt đầu chạy",
        no_telegram=args.no_telegram,
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
    )
    charts_dir = args.charts_dir or default_charts_dir()
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
            recapture_failed_chart_slots(
                coinmap_yaml=cfg,
                charts_dir=charts_dir,
                stamp=stamp,
                issues=bad,
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

    require_openai(s)
    payloads = ordered_chart_openai_payloads(charts_dir)
    _warn_if_incomplete_chart_payloads(charts_dir, payloads)
    if not payloads:
        raise SystemExit(
            "No TradingView/Coinmap chart files found for OpenAI step "
            f"under {charts_dir}. Check capture and chart slot order (effective_chart_image_order)."
        )

    prompt_all = _resolved_analysis_prompt(args, charts_dir)
    try:
        out = _run_openai_flow(
            s,
            charts_dir,
            prompt_all,
            args.max_images_per_call,
            chart_payloads=payloads,
            on_first_model_text=None,
            purge_json_attachment_storage=True,
            model=resolved_openai_model(s, getattr(args, "model", None)),
        )
    except Exception as e:
        re_raise_unless_openai(e)
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
            slot: SessionSlot = session_slot_now_hcm()
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
    paths = capture_charts(
        coinmap_yaml=cfg_cap,
        charts_dir=args.charts_dir,
        storage_state_path=storage,
        email=s.coinmap_email,
        password=s.coinmap_password,
        tradingview_password=s.tradingview_password,
        save_storage_state=not args.no_save_storage,
        headless=not args.headed,
        reuse_browser_context=None,
        main_chart_symbol=args.main_symbol,
        clear_charts_before_capture=True,
    )
    charts_dir = args.charts_dir or default_charts_dir()
    print(f"Captured {len(paths)} file(s) for update run.")
    stamp = stamp_from_capture_paths(paths)
    m5 = coinmap_main_pair_interval_json_path(charts_dir, "5m", stamp=stamp)
    m15 = coinmap_main_pair_interval_json_path(charts_dir, "15m", stamp=stamp)
    _log.info(
        "update: capture xong | %s file(s) | stamp=%s | M15=%s | M5=%s",
        len(paths),
        stamp,
        m15,
        m5,
    )
    if m5 is None:
        raise SystemExit(
            f"No XAUUSD 5m Coinmap JSON under {charts_dir} after capture (stamp={stamp!r}). "
            "Check coinmap_update.yaml capture_plan and api_data_export."
        )
    if m15 is None:
        raise SystemExit(
            f"No XAUUSD 15m Coinmap JSON under {charts_dir} after capture (stamp={stamp!r}). "
            "Check coinmap_update.yaml capture_plan includes 15m."
        )

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

    user_msg = build_intraday_update_user_text(first_after_all=first_after_all)

    try:
        out_text, new_id = run_single_followup_responses(
            api_key=s.openai_api_key,
            prompt_id=s.openai_prompt_id,
            prompt_version=s.openai_prompt_version,
            user_text=user_msg,
            morning_snapshot_path=morning_snapshot,
            coinmap_json_paths=[m15, m5],
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
        last_price_from_mt5=not bool(getattr(args, "tv_title_price", False)),
        mt5_stale_reconnect_seconds=float(
            getattr(args, "mt5_stale_reconnect_seconds", 60.0) or 0.0
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
    stop_h = int(getattr(args, "stop_at_hour", 0))
    stop_at_hour = None if stop_h < 0 else stop_h
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
