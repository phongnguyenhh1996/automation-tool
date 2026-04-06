from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Optional

from automation_tool.coinmap import capture_charts
from automation_tool.config import (
    default_charts_dir,
    default_coinmap_config_path,
    default_coinmap_update_config_path,
    default_storage_state_path,
    load_settings,
    require_openai,
    require_telegram,
)
from automation_tool.openai_errors import re_raise_unless_openai
from automation_tool.openai_prompt_flow import (
    DEFAULT_UPDATE_PROMPT_TEMPLATE,
    PromptTwoStepResult,
    default_analysis_prompt,
    run_analysis_responses_flow,
    run_single_followup_responses,
)
from automation_tool.images import (
    coinmap_xauusd_5m_json_path,
    effective_chart_image_order,
    ordered_chart_openai_payloads,
)
from automation_tool.first_response_trade import apply_first_response_vao_lenh
from automation_tool.state_files import (
    default_last_alert_prices_path,
    default_last_response_id_path,
    default_morning_baseline_prices_path,
    read_last_alert_prices,
    read_last_response_id,
    read_morning_baseline_prices,
    write_last_alert_prices,
    write_last_response_id,
    write_morning_baseline_prices,
)
from automation_tool.zone_prices import (
    is_no_change_action_line,
    parse_three_zone_prices,
    prices_equal_triple,
)
from automation_tool.tradingview_alerts import sync_tradingview_alerts
from automation_tool.tradingview_journal_monitor import JournalMonitorParams, run_tv_journal_monitor
from automation_tool.telegram_bot import (
    send_message,
    send_mt5_execution_log_to_ngan_gon_chat,
    send_openai_output_to_telegram,
    split_analysis_json_chi_tiet_ngan_gon,
    split_output_chi_tiet_ngan_gon,
)
from automation_tool.telegram_logging import setup_automation_logging
from automation_tool.config import load_all_dotenv
from automation_tool.mt5_openai_parse import parse_openai_output_md
from automation_tool.mt5_execute import check_mt5_login, execute_trade, format_mt5_execution_for_telegram

_log = logging.getLogger("automation_tool.cli")


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
            "User message kèm hướng dẫn JSON; mặc định: theo cặp đang active "
            "(marker charts-dir / data/.main_chart_symbol, xem default_analysis_prompt)"
        ),
    )
    a.add_argument(
        "--max-images-per-call",
        type=int,
        default=10,
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
        "--telegram-detail-chat-id",
        default=None,
        metavar="ID",
        help=(
            "Chat/channel nhận OUTPUT chi tiết (markers / JSON out_chi_tiet); "
            "mặc định TELEGRAM_ANALYSIS_DETAIL_CHAT_ID trong .env"
        ),
    )
    a.set_defaults(func=cmd_analyze)

    al = sub.add_parser(
        "all",
        help=(
            "capture → OpenAI → parse 3 vùng giá → persist + đồng bộ TradingView alerts → "
            "tv-journal-monitor (mặc định; dùng --no-tv-journal-monitor để bỏ)"
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
        help="Prompt user kèm JSON; mặc định: theo cặp active (giống analyze)",
    )
    al.add_argument(
        "--max-images-per-call",
        type=int,
        default=10,
        help="Max items per OpenAI call (images + Coinmap JSON)",
    )
    al.add_argument("--no-telegram", action="store_true")
    al.add_argument(
        "--no-tradingview",
        action="store_true",
        help="Skip TradingView alert sync after parsing 3 zone prices from step-2 output",
    )
    al.add_argument(
        "--no-tv-journal-monitor",
        action="store_true",
        help=(
            "Không chạy tv-journal-monitor sau khi đồng bộ cảnh báo TV "
            "(mặc định: luôn chạy khi đã đồng bộ TV)"
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
        help="tv-journal-monitor: nghỉ giữa các chu kỳ reload (mặc định: 45)",
    )
    al.add_argument(
        "--wait-minutes",
        type=int,
        default=15,
        help="tv-journal-monitor: khi model trả Hành động chờ — phút trước khi chụp M5 hỏi lại (mặc định: 15)",
    )
    al.add_argument(
        "--until-hour",
        type=int,
        default=23,
        help="tv-journal-monitor: dừng theo dõi sau giờ này (địa phương, mặc định: 23)",
    )
    al.add_argument(
        "--timezone",
        type=str,
        default="Asia/Ho_Chi_Minh",
        help="tv-journal-monitor: IANA timezone cho --until-hour",
    )
    al.add_argument(
        "--last-alert-json",
        type=Path,
        default=None,
        metavar="FILE",
        help=(
            "Phân tích chart + tv-journal-monitor: file last_alert_prices.json "
            "(mặc định: data/last_alert_prices.json)"
        ),
    )
    al.add_argument(
        "--no-mt5-execute",
        action="store_true",
        help=(
            "Không gọi execute_trade (phản hồi đầu phân tích + journal sau VÀO LỆNH + trade_line; "
            "mặc định: gọi; cần Windows + MetaTrader5 pip)"
        ),
    )
    al.add_argument(
        "--mt5-symbol",
        default=None,
        metavar="SYM",
        help="Symbol MT5 (ghi đè parse từ trade_line)",
    )
    al.add_argument(
        "--mt5-dry-run",
        action="store_true",
        help="Chỉ dry-run MT5 (mặc định: gửi lệnh thật)",
    )
    al.set_defaults(func=cmd_all)

    up = sub.add_parser(
        "update",
        help="Intraday: Coinmap XAUUSD M5 JSON + OpenAI follow-up (same thread); sync TradingView if zones changed vs morning",
    )
    up.add_argument("--config", type=Path, default=None, help="Coinmap yaml for capture only (default: coinmap_update.yaml)")
    up.add_argument(
        "--tv-config",
        type=Path,
        default=None,
        help="Yaml containing tradingview_capture for alert sync (default: config/coinmap.yaml)",
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
        help="Skip TradingView alert sync (still updates last_response_id)",
    )
    up.add_argument(
        "--no-journal-monitor-after-update",
        action="store_true",
        help=(
            "Không chạy tv-journal-monitor sau update (kể cả no_change / giá trùng baseline; "
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
            "Không gọi execute_trade khi follow-up có vùng hop_luu>80 + trade_line (mặc định: gọi; cần Windows + MetaTrader5)"
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
    up.set_defaults(func=cmd_update)

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
    tj.set_defaults(func=cmd_tv_journal_monitor)

    g = sub.add_parser(
        "chatgpt-project",
        help="Same as analyze: prompt id + multimodal Responses API",
    )
    g.add_argument(
        "--prompt",
        type=str,
        default=None,
        help="User message; default: analysis prompt theo cặp active + JSON schema",
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
        default=10,
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
        "--telegram-detail-chat-id",
        default=None,
        metavar="ID",
        help="Giống analyze: kênh nhận bản chi tiết",
    )
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

    return p


def _run_openai_flow(
    s,
    charts_dir: Path,
    analysis_prompt: str,
    max_images: int,
    chart_paths: list[Path] | None = None,
    chart_payloads: list[tuple[str, Path]] | None = None,
    on_first_model_text: Optional[Callable[[str], None]] = None,
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
    )


def cmd_capture(args: argparse.Namespace) -> None:
    s = load_settings()
    _log.info(
        "capture: bắt đầu | config=%s charts_dir=%s headed=%s",
        args.config or default_coinmap_config_path(),
        args.charts_dir if args.charts_dir is not None else "(default theo data/.main_chart_symbol)",
        args.headed,
    )
    cfg = args.config or default_coinmap_config_path()
    storage = args.storage_state or default_storage_state_path()
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
    print(f"Saved {len(paths)} image(s) under {charts_dir}:")
    _log.info("capture: xong | %s file(s) → %s", len(paths), charts_dir)
    for p in paths:
        print(f"  {p}")


def _resolved_analysis_prompt(args: argparse.Namespace, charts_dir: Path) -> str:
    """Khi không truyền --prompt: dùng default_analysis_prompt theo read_main_chart_symbol(charts_dir)."""
    p = getattr(args, "prompt", None)
    if p is not None and str(p).strip():
        return str(p)
    from automation_tool.images import read_main_chart_symbol

    return default_analysis_prompt(read_main_chart_symbol(charts_dir))


def _warn_if_incomplete_chart_payloads(
    charts_dir: Path, payloads: list[tuple[str, Path]]
) -> None:
    expected = len(effective_chart_image_order(charts_dir))
    if len(payloads) < expected:
        print(
            f"Warning: expected {expected} chart slots in fixed order, found {len(payloads)} file(s) on disk.",
            file=sys.stderr,
        )


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
            f"No chart files under {charts_dir} (TradingView PNG / Coinmap JSON or PNG). "
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
            telegram_bot_token=s.telegram_bot_token,
            telegram_analysis_detail_chat_id=s.telegram_analysis_detail_chat_id,
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
    out = execute_trade(
        trade,
        dry_run=args.dry_run,
        symbol_override=args.symbol,
        lot_override=args.lot,
    )
    if s_mt5.telegram_output_ngan_gon_chat_id:
        send_mt5_execution_log_to_ngan_gon_chat(
            bot_token=s_mt5.telegram_bot_token,
            output_ngan_gon_chat_id=s_mt5.telegram_output_ngan_gon_chat_id,
            source="mt5-trade",
            text=format_mt5_execution_for_telegram(out),
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


def cmd_all(args: argparse.Namespace) -> None:
    s = load_settings()
    cfg = args.config or default_coinmap_config_path()
    storage = args.storage_state or default_storage_state_path()
    _log.info(
        "all: bắt đầu | tv_yaml=%s charts=%s no_tradingview=%s no_tv_journal=%s",
        cfg,
        args.charts_dir if args.charts_dir is not None else "(default)",
        args.no_tradingview,
        args.no_tv_journal_monitor,
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

    require_openai(s)
    payloads = ordered_chart_openai_payloads(charts_dir)
    _warn_if_incomplete_chart_payloads(charts_dir, payloads)
    if not payloads:
        raise SystemExit(
            "No TradingView/Coinmap chart files found for OpenAI step "
            f"under {charts_dir}. Check capture and chart slot order (effective_chart_image_order)."
        )

    lap_all = args.last_alert_json or default_last_alert_prices_path()

    def _on_first_all(text: str) -> None:
        apply_first_response_vao_lenh(
            text,
            last_alert_path=lap_all,
            mt5_execute=not args.no_mt5_execute,
            mt5_dry_run=args.mt5_dry_run,
            mt5_symbol=args.mt5_symbol,
            telegram_bot_token=s.telegram_bot_token,
            telegram_analysis_detail_chat_id=s.telegram_analysis_detail_chat_id,
            telegram_output_ngan_gon_chat_id=s.telegram_output_ngan_gon_chat_id,
            telegram_source_label="all (phản hồi đầu)",
        )

    prompt_all = _resolved_analysis_prompt(args, charts_dir)
    try:
        out = _run_openai_flow(
            s,
            charts_dir,
            prompt_all,
            args.max_images_per_call,
            chart_payloads=payloads,
            on_first_model_text=_on_first_all,
        )
    except Exception as e:
        re_raise_unless_openai(e)
    print(out.full_text())
    _log.info("all: OpenAI xong | response_id=%s", out.final_response_id)

    write_last_response_id(out.final_response_id)
    if not args.no_telegram and out.after_charts:
        require_telegram(s)
        send_openai_output_to_telegram(
            bot_token=s.telegram_bot_token,
            chat_id=s.telegram_chat_id,
            raw=out.after_charts,
            default_parse_mode=s.telegram_parse_mode,
            summary_chat_id=s.telegram_output_ngan_gon_chat_id,
        )

    zt, zerr, _nc = parse_three_zone_prices(out.after_charts or "")
    if zt:
        write_morning_baseline_prices(zt)
        write_last_alert_prices(zt)
        _log.info(
            "all: đã ghi morning_baseline + last_alert | giá=%s | %s | %s",
            zt[0],
            zt[1],
            zt[2],
        )
        if not args.no_tradingview:
            p1, p2, p3 = zt
            print(
                f"Đồng bộ TradingView alerts → {p1} | {p2} | {p3} (config: {cfg})",
                flush=True,
            )
            sync_tradingview_alerts(
                coinmap_yaml=cfg,
                storage_state_path=storage,
                email=s.coinmap_email,
                tradingview_password=s.tradingview_password,
                target_prices=zt,
                headless=not args.headed,
            )
            _log.info("all: TradingView sync xong")
            if not args.no_tv_journal_monitor:
                require_openai(s)
                prev_j = read_last_response_id()
                if not prev_j:
                    raise SystemExit(
                        f"Thiếu {default_last_response_id_path()} — không chạy tv-journal-monitor."
                    )
                cfg_cap = args.capture_config or default_coinmap_update_config_path()
                params = JournalMonitorParams(
                    coinmap_tv_yaml=cfg,
                    capture_coinmap_yaml=cfg_cap,
                    charts_dir=charts_dir,
                    storage_state_path=storage,
                    target_prices=zt,
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
                )
                print(
                    f"tv-journal-monitor: giá {zt[0]} | {zt[1]} | {zt[2]} — "
                    f"tới {args.until_hour}:00 ({args.timezone}), "
                    f"chu kỳ: reload → Nhật ký → parse, nghỉ {args.poll_seconds}s.",
                    flush=True,
                )
                print(
                    f"  TV yaml: {cfg} | Capture yaml: {cfg_cap} | charts: {charts_dir} | "
                    f"storage: {storage} | headed={args.headed} | no_telegram={args.no_telegram}",
                    flush=True,
                )
                _log.info("all: chạy tv-journal-monitor…")
                try:
                    outcome = run_tv_journal_monitor(
                        settings=s,
                        params=params,
                        initial_response_id=prev_j,
                    )
                except Exception as e:
                    re_raise_unless_openai(e)
                    raise
                print(f"Kết thúc tv-journal-monitor: {outcome}", flush=True)
                _log.info("all: tv-journal-monitor kết thúc | outcome=%s", outcome)
    else:
        print(
            f"Warning: could not parse morning zone prices for persistence: {zerr}",
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

    baseline = read_morning_baseline_prices()
    if baseline is None:
        raise SystemExit(
            f"Missing {default_morning_baseline_prices_path()} — run `coinmap-automation all` successfully first."
        )

    prev = read_last_response_id()
    if not prev:
        raise SystemExit(
            f"Missing {default_last_response_id_path()} — run `coinmap-automation all` successfully first."
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
    )
    charts_dir = args.charts_dir or default_charts_dir()
    print(f"Captured {len(paths)} file(s) for update run.")
    _log.info("update: capture xong | %s file(s) | json M5=%s", len(paths), coinmap_xauusd_5m_json_path(charts_dir))
    json_path = coinmap_xauusd_5m_json_path(charts_dir)
    if json_path is None:
        raise SystemExit(
            f"No XAUUSD 5m Coinmap JSON under {charts_dir} after capture. "
            "Check coinmap_update.yaml capture_plan and api_data_export."
        )

    require_openai(s)
    p1, p2, p3 = baseline.prices
    user_msg = DEFAULT_UPDATE_PROMPT_TEMPLATE.format(p1=p1, p2=p2, p3=p3)

    try:
        out_text, new_id = run_single_followup_responses(
            api_key=s.openai_api_key,
            prompt_id=s.openai_prompt_id,
            prompt_version=s.openai_prompt_version,
            user_text=user_msg,
            coinmap_json_path=json_path,
            previous_response_id=prev,
            vector_store_ids=s.openai_vector_store_ids,
            store=s.openai_responses_store,
            include=s.openai_responses_include,
        )
    except Exception as e:
        re_raise_unless_openai(e)

    print(out_text)
    write_last_response_id(new_id)
    _log.info("update: OpenAI follow-up xong | new_response_id=%s", new_id)

    lap = args.last_alert_json or default_last_alert_prices_path()
    apply_first_response_vao_lenh(
        out_text,
        last_alert_path=lap,
        mt5_execute=not args.no_mt5_execute,
        mt5_dry_run=args.mt5_dry_run,
        mt5_symbol=args.mt5_symbol,
        telegram_bot_token=s.telegram_bot_token,
        telegram_analysis_detail_chat_id=s.telegram_analysis_detail_chat_id,
        telegram_output_ngan_gon_chat_id=s.telegram_output_ngan_gon_chat_id,
        telegram_source_label="update (follow-up M5)",
    )

    def _run_tv_journal_after_update(
        target_prices: tuple[float, float, float], *, log_label: str
    ) -> None:
        if args.no_journal_monitor_after_update:
            _log.info(
                "update: bỏ qua tv-journal-monitor (%s) — --no-journal-monitor-after-update",
                log_label,
            )
            return
        print(f"Chạy tv-journal-monitor ({log_label}).", flush=True)
        _log.info("update: chạy tv-journal-monitor | %s", log_label)
        jparams = JournalMonitorParams(
            coinmap_tv_yaml=cfg_tv,
            capture_coinmap_yaml=cfg_cap,
            charts_dir=charts_dir,
            storage_state_path=storage,
            target_prices=target_prices,
            headless=not args.headed,
            no_save_storage=args.no_save_storage,
            poll_seconds=45.0,
            wait_minutes=15,
            until_hour=23,
            timezone_name="Asia/Ho_Chi_Minh",
            no_telegram=args.no_telegram,
            last_alert_path=lap,
            mt5_execute=not args.no_mt5_execute,
            mt5_symbol=args.mt5_symbol,
            mt5_dry_run=args.mt5_dry_run,
        )
        try:
            outcome = run_tv_journal_monitor(
                settings=s,
                params=jparams,
                initial_response_id=new_id,
            )
        except Exception as e:
            re_raise_unless_openai(e)
            raise
        print(f"Kết thúc tv-journal-monitor (sau update): {outcome}", flush=True)
        _log.info("update: tv-journal-monitor sau update kết thúc | outcome=%s", outcome)

    new_triple, zerr, no_change_json = parse_three_zone_prices(out_text)
    if no_change_json is True:
        if not args.no_telegram:
            require_telegram(s)
            send_message(
                bot_token=s.telegram_bot_token,
                chat_id=s.telegram_chat_id,
                text="Vùng giá không đổi so với sáng (no_change), giữ nguyên cảnh báo.",
                parse_mode=s.telegram_parse_mode,
            )
        _log.info("update: no_change (JSON) — không ghi giá mới")
        p1, p2, p3 = baseline.prices
        _run_tv_journal_after_update((p1, p2, p3), log_label="no_change JSON")
        return
    if new_triple is None:
        if is_no_change_action_line(out_text):
            if not args.no_telegram:
                require_telegram(s)
                send_message(
                    bot_token=s.telegram_bot_token,
                    chat_id=s.telegram_chat_id,
                    text="Vùng giá không đổi so với sáng, giữ nguyên cảnh báo.",
                    parse_mode=s.telegram_parse_mode,
                )
            _log.info("update: no_change (action line) — không ghi giá mới")
            p1, p2, p3 = baseline.prices
            _run_tv_journal_after_update((p1, p2, p3), log_label="no_change action line")
            return
        raise SystemExit(zerr or "Could not parse three zone prices from model output.")

    if prices_equal_triple(new_triple, baseline.prices):
        if not args.no_telegram:
            require_telegram(s)
            send_message(
                bot_token=s.telegram_bot_token,
                chat_id=s.telegram_chat_id,
                text="Vùng giá không đổi so với sáng, giữ nguyên cảnh báo.",
                parse_mode=s.telegram_parse_mode,
            )
        _log.info("update: giá trùng baseline — không ghi giá mới")
        _run_tv_journal_after_update(new_triple, log_label="giá trùng baseline")
        return

    write_last_alert_prices(new_triple)
    _log.info(
        "update: đã ghi last_alert_prices | %s | %s | %s",
        new_triple[0],
        new_triple[1],
        new_triple[2],
    )

    if not args.no_tradingview:
        sync_tradingview_alerts(
            coinmap_yaml=cfg_tv,
            storage_state_path=storage,
            email=s.coinmap_email,
            tradingview_password=s.tradingview_password,
            target_prices=new_triple,
            headless=not args.headed,
        )
        _log.info("update: TradingView sync xong")

    if not args.no_telegram:
        require_telegram(s)
        a, b, c = new_triple
        send_message(
            bot_token=s.telegram_bot_token,
            chat_id=s.telegram_chat_id,
            text=f"Đã cập nhật vùng giá mới: {a} | {b} | {c}",
            parse_mode=s.telegram_parse_mode,
        )
        dual = split_analysis_json_chi_tiet_ngan_gon(out_text)
        if dual is None:
            dual = split_output_chi_tiet_ngan_gon(out_text)
        if dual is not None:
            # out_chi_tiet → TELEGRAM_CHAT_ID; output_ngan_gon → TELEGRAM_OUTPUT_NGAN_GON_CHAT_ID (hoặc main nếu không cấu hình).
            send_openai_output_to_telegram(
                bot_token=s.telegram_bot_token,
                chat_id=s.telegram_chat_id,
                raw=out_text,
                default_parse_mode=s.telegram_parse_mode,
                summary_chat_id=(s.telegram_output_ngan_gon_chat_id or "").strip() or None,
                detail_chat_id=None,
            )
            _log.info(
                "update: đã gửi out_chi_tiet lên TELEGRAM_CHAT_ID, output_ngan_gon lên kênh ngắn gọn (nếu có)"
            )
        else:
            _log.info(
                "update: không tách được out_chi_tiet/output_ngan_gon — chỉ gửi dòng giá lên main"
            )

    _run_tv_journal_after_update(new_triple, log_label="ghi giá mới")


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
