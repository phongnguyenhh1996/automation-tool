from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from automation_tool.coinmap import capture_charts
from automation_tool.config import (
    default_charts_dir,
    default_coinmap_config_path,
    default_storage_state_path,
    load_settings,
    require_openai,
    require_telegram,
)
from automation_tool.openai_errors import re_raise_unless_openai
from automation_tool.openai_prompt_flow import (
    DEFAULT_FIRST_PROMPT,
    DEFAULT_FOLLOW_UP_PROMPT,
    PromptTwoStepResult,
    run_prompt_two_step_flow,
)
from automation_tool.images import CHART_IMAGE_ORDER, ordered_chart_openai_payloads
from automation_tool.telegram_bot import send_message, send_openai_output_to_telegram
from automation_tool.mt5_openai_parse import parse_openai_output_md
from automation_tool.mt5_execute import execute_trade


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
        help="capture then OpenAI: text step first, then vision with charts",
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
    al.set_defaults(func=cmd_all)

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
        help="Symbol MT5 (mặc định: từ 📊 trong text hoặc biến MT5_SYMBOL hoặc XAUUSD)",
    )
    mt5.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Gửi lệnh thật trên máy Windows có terminal MT5 (VPS: để trống MT5_LOGIN nếu đã đăng nhập sẵn). "
            "Mặc định dry-run (Mac dev không cần MetaTrader5)."
        ),
    )
    mt5.set_defaults(func=cmd_mt5_trade)

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
    text = path.read_text(encoding="utf-8")
    default_sym = (os.getenv("MT5_SYMBOL") or "XAUUSD").strip()
    trade, err = parse_openai_output_md(
        text,
        default_symbol=default_sym,
        symbol_override=args.symbol,
    )
    if err or trade is None:
        raise SystemExit(err or "Không parse được lệnh.")
    dry = not args.execute
    out = execute_trade(trade, dry_run=dry)
    print(out.message)
    if out.request:
        print("request/preview:", out.request)
    if not out.ok:
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
    if not args.no_telegram and out.after_charts:
        require_telegram(s)
        send_openai_output_to_telegram(
            bot_token=s.telegram_bot_token,
            chat_id=s.telegram_chat_id,
            raw=out.after_charts,
            default_parse_mode=s.telegram_parse_mode,
            summary_chat_id=s.telegram_output_ngan_gon_chat_id,
        )


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
