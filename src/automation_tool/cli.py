from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

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
    DEFAULT_FIRST_PROMPT,
    DEFAULT_FOLLOW_UP_PROMPT,
    DEFAULT_UPDATE_PROMPT_TEMPLATE,
    PromptTwoStepResult,
    run_prompt_two_step_flow,
    run_single_followup_responses,
)
from automation_tool.images import (
    CHART_IMAGE_ORDER,
    coinmap_xauusd_5m_json_path,
    ordered_chart_openai_payloads,
)
from automation_tool.state_files import (
    default_last_alert_prices_path,
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
from automation_tool.telegram_bot import send_message, send_openai_output_to_telegram
from automation_tool.config import load_all_dotenv
from automation_tool.mt5_openai_parse import parse_openai_output_md
from automation_tool.mt5_execute import check_mt5_login, execute_trade


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
    c.set_defaults(func=cmd_capture)

    a = sub.add_parser(
        "analyze",
        help="OpenAI: text step then vision with charts (no capture; uses OPENAI_PROMPT_ID)",
    )
    a.add_argument("--charts-dir", type=Path, default=None)
    a.add_argument(
        "--prompt",
        type=str,
        default=DEFAULT_FIRST_PROMPT,
        help="Bước 1 — chỉ gửi text (mặc định: quy trình XAUUSD)",
    )
    a.add_argument(
        "--follow-up",
        type=str,
        default=DEFAULT_FOLLOW_UP_PROMPT,
        help="Bước 2 — kèm ảnh chart",
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
    a.set_defaults(func=cmd_analyze)

    al = sub.add_parser(
        "all",
        help="capture then OpenAI (2 steps); parse 3 zone prices from step 2 → persist + sync TradingView alerts",
    )
    al.add_argument("--config", type=Path, default=None)
    al.add_argument("--charts-dir", type=Path, default=None)
    al.add_argument("--storage-state", type=Path, default=None)
    al.add_argument("--no-save-storage", action="store_true")
    al.add_argument("--headed", action="store_true")
    al.add_argument(
        "--prompt",
        type=str,
        default=DEFAULT_FIRST_PROMPT,
        help="Bước 1 — chỉ gửi text trước; sau khi OpenAI trả lời mới gửi chart",
    )
    al.add_argument(
        "--follow-up",
        type=str,
        default=DEFAULT_FOLLOW_UP_PROMPT,
        help="Bước 2 — kèm ảnh chart sau bước 1",
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
    up.add_argument("--no-telegram", action="store_true")
    up.add_argument(
        "--no-tradingview",
        action="store_true",
        help="Skip TradingView alert sync (still updates last_response_id)",
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
    tj.set_defaults(func=cmd_tv_journal_monitor)

    g = sub.add_parser(
        "chatgpt-project",
        help="Same as analyze: prompt id + two-step Responses API (text then charts)",
    )
    g.add_argument(
        "--prompt",
        type=str,
        default=DEFAULT_FIRST_PROMPT,
        help="First user message (text-only)",
    )
    g.add_argument(
        "--follow-up",
        type=str,
        default=DEFAULT_FOLLOW_UP_PROMPT,
        help="Second user message sent with the chart images",
    )
    g.add_argument("--charts-dir", type=Path, default=None)
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
            "OpenAI .md → MetaTrader5. Dev Mac: dry-run. Prod: Windows VPS + MT5 đã đăng nhập + --execute."
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
        "--execute",
        action="store_true",
        help=(
            "Gửi lệnh thật trên máy Windows có terminal MT5 (VPS: để trống MT5_LOGIN nếu đã đăng nhập sẵn). "
            "Mặc định dry-run (Mac dev không cần MetaTrader5)."
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
    first_prompt: str,
    follow_up: str,
    max_images: int,
    chart_paths: list[Path] | None = None,
    chart_payloads: list[tuple[str, Path]] | None = None,
) -> PromptTwoStepResult:
    return run_prompt_two_step_flow(
        api_key=s.openai_api_key,
        prompt_id=s.openai_prompt_id,
        prompt_version=s.openai_prompt_version,
        charts_dir=charts_dir,
        first_prompt=first_prompt,
        follow_up_prompt=follow_up,
        max_images_per_call=max_images,
        vector_store_ids=s.openai_vector_store_ids,
        store=s.openai_responses_store,
        include=s.openai_responses_include,
        chart_paths=chart_paths,
        chart_payloads=chart_payloads,
    )


def cmd_capture(args: argparse.Namespace) -> None:
    s = load_settings()
    cfg = args.config or default_coinmap_config_path()
    charts_dir = args.charts_dir or default_charts_dir()
    storage = args.storage_state or default_storage_state_path()
    paths = capture_charts(
        coinmap_yaml=cfg,
        charts_dir=charts_dir,
        storage_state_path=storage,
        email=s.coinmap_email,
        password=s.coinmap_password,
        tradingview_password=s.tradingview_password,
        save_storage_state=not args.no_save_storage,
        headless=not args.headed,
        reuse_browser_context=None,
    )
    print(f"Saved {len(paths)} image(s) under {charts_dir}:")
    for p in paths:
        print(f"  {p}")


def _warn_if_incomplete_chart_payloads(payloads: list[tuple[str, Path]]) -> None:
    expected = len(CHART_IMAGE_ORDER)
    if len(payloads) < expected:
        print(
            f"Warning: expected {expected} chart slots in fixed order, found {len(payloads)} file(s) on disk.",
            file=sys.stderr,
        )


def cmd_analyze(args: argparse.Namespace) -> None:
    s = load_settings()
    require_openai(s)
    charts_dir = args.charts_dir or default_charts_dir()
    payloads = ordered_chart_openai_payloads(charts_dir)
    _warn_if_incomplete_chart_payloads(payloads)
    if not payloads:
        raise SystemExit(
            f"No chart files under {charts_dir} (TradingView PNG / Coinmap JSON or PNG). "
            "Run capture first or check data/charts."
        )

    try:
        out = _run_openai_flow(
            s,
            charts_dir,
            args.prompt,
            args.follow_up,
            args.max_images_per_call,
            chart_payloads=payloads,
        )
    except Exception as e:
        re_raise_unless_openai(e)
    print(out.full_text())
    if not args.no_telegram and out.after_charts:
        require_telegram(s)
        send_openai_output_to_telegram(
            bot_token=s.telegram_bot_token,
            chat_id=s.telegram_chat_id,
            raw=out.after_charts,
            default_parse_mode=s.telegram_parse_mode,
            summary_chat_id=s.telegram_output_ngan_gon_chat_id,
        )


def cmd_chatgpt_project(args: argparse.Namespace) -> None:
    cmd_analyze(args)


def cmd_mt5_trade(args: argparse.Namespace) -> None:
    path = args.file.expanduser()
    if not path.is_file():
        raise SystemExit(f"File not found: {path}")
    load_all_dotenv()
    text = path.read_text(encoding="utf-8")
    default_sym = "XAUUSD"
    trade, err = parse_openai_output_md(
        text,
        default_symbol=default_sym,
        symbol_override=args.symbol,
    )
    if err or trade is None:
        raise SystemExit(err or "Không parse được lệnh.")
    dry = not args.execute
    out = execute_trade(
        trade,
        dry_run=dry,
        symbol_override=args.symbol,
        lot_override=args.lot,
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
    charts_dir = args.charts_dir or default_charts_dir()
    storage = args.storage_state or default_storage_state_path()
    paths = capture_charts(
        coinmap_yaml=cfg,
        charts_dir=charts_dir,
        storage_state_path=storage,
        email=s.coinmap_email,
        password=s.coinmap_password,
        tradingview_password=s.tradingview_password,
        save_storage_state=not args.no_save_storage,
        headless=not args.headed,
        reuse_browser_context=None,
    )
    n_art = len(paths)
    print(f"Captured {n_art} file(s) (screenshots and/or API JSON paths returned by capture).")
    if not paths:
        raise SystemExit("No chart artifacts captured; aborting analyze step.")

    require_openai(s)
    payloads = ordered_chart_openai_payloads(charts_dir)
    _warn_if_incomplete_chart_payloads(payloads)
    if not payloads:
        raise SystemExit(
            "No TradingView/Coinmap chart files found for OpenAI step "
            f"under {charts_dir}. Check capture and CHART_IMAGE_ORDER."
        )

    try:
        out = _run_openai_flow(
            s,
            charts_dir,
            args.prompt,
            args.follow_up,
            args.max_images_per_call,
            chart_payloads=payloads,
        )
    except Exception as e:
        re_raise_unless_openai(e)
    print(out.full_text())

    write_last_response_id(out.final_response_id)
    zt, zerr = parse_three_zone_prices(out.after_charts or "")
    if zt:
        write_morning_baseline_prices(zt)
        write_last_alert_prices(zt)
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
    else:
        print(
            f"Warning: could not parse morning zone prices for persistence: {zerr}",
            file=sys.stderr,
        )

    if not args.no_telegram and out.after_charts:
        require_telegram(s)
        send_openai_output_to_telegram(
            bot_token=s.telegram_bot_token,
            chat_id=s.telegram_chat_id,
            raw=out.after_charts,
            default_parse_mode=s.telegram_parse_mode,
            summary_chat_id=s.telegram_output_ngan_gon_chat_id,
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
    require_openai(s)
    prev = read_last_response_id()
    if not prev:
        raise SystemExit(
            "Missing data/last_response_id.txt — run `coinmap-automation all` or `update` first "
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
    )

    print(
        f"tv-journal-monitor: giá {targets[0]} | {targets[1]} | {targets[2]} — "
        f"tới {args.until_hour}:00 ({args.timezone}), "
        f"chu kỳ: reload → Nhật ký → parse, nghỉ {args.poll_seconds}s.",
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


def cmd_update(args: argparse.Namespace) -> None:
    s = load_settings()
    cfg_cap = args.config or default_coinmap_update_config_path()
    charts_dir = args.charts_dir or default_charts_dir()
    storage = args.storage_state or default_storage_state_path()
    cfg_tv = args.tv_config or default_coinmap_config_path()

    baseline = read_morning_baseline_prices()
    if baseline is None:
        raise SystemExit(
            "Missing data/morning_baseline_prices.json — run `coinmap-automation all` successfully first."
        )

    prev = read_last_response_id()
    if not prev:
        raise SystemExit(
            "Missing data/last_response_id.txt — run `coinmap-automation all` successfully first."
        )

    paths = capture_charts(
        coinmap_yaml=cfg_cap,
        charts_dir=charts_dir,
        storage_state_path=storage,
        email=s.coinmap_email,
        password=s.coinmap_password,
        tradingview_password=s.tradingview_password,
        save_storage_state=not args.no_save_storage,
        headless=not args.headed,
        reuse_browser_context=None,
    )
    print(f"Captured {len(paths)} file(s) for update run.")
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

    new_triple, zerr = parse_three_zone_prices(out_text)
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
        return

    if not args.no_tradingview:
        sync_tradingview_alerts(
            coinmap_yaml=cfg_tv,
            storage_state_path=storage,
            email=s.coinmap_email,
            tradingview_password=s.tradingview_password,
            target_prices=new_triple,
            headless=not args.headed,
        )
    write_last_alert_prices(new_triple)

    if not args.no_telegram:
        require_telegram(s)
        a, b, c = new_triple
        send_message(
            bot_token=s.telegram_bot_token,
            chat_id=s.telegram_chat_id,
            text=f"Đã cập nhật vùng giá mới: {a} | {b} | {c}",
            parse_mode=s.telegram_parse_mode,
        )


def main() -> None:
    _configure_stdio_utf8()
    parser = _parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
