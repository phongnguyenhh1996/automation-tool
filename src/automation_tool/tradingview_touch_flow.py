"""
Shared TradingView "touch" flow:

- Outer monitors detect a price touching a waiting zone (via Journal tab, Watchlist, etc.)
- Inner loop captures Coinmap M5, calls OpenAI follow-up, updates per-plan status,
  and optionally executes MT5.

This module keeps the inner loop generic and pluggable via a `poll_supersede_touch`
callback used while sleeping between OpenAI retries.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Callable, Optional

from playwright.sync_api import BrowserContext, Page
from zoneinfo import ZoneInfo

from automation_tool.coinmap import capture_charts
from automation_tool.config import Settings
from automation_tool.first_response_trade import apply_first_response_vao_lenh
from automation_tool.images import coinmap_xauusd_5m_json_path, read_main_chart_symbol
from automation_tool.mt5_execute import execute_trade, format_mt5_execution_for_telegram
from automation_tool.mt5_openai_parse import (
    parse_journal_intraday_action_from_openai_text,
    parse_openai_output_md,
)
from automation_tool.openai_errors import re_raise_unless_openai
from automation_tool.openai_prompt_flow import (
    JOURNAL_INTRADAY_FIRST_USER_TEMPLATE,
    JOURNAL_INTRADAY_RETRY_USER_TEMPLATE,
    run_single_followup_responses,
)
from automation_tool.state_files import (
    LOAI,
    VAO_LENH,
    read_last_alert_state,
    update_single_plan_status,
    write_last_response_id,
)
from automation_tool.telegram_bot import (
    send_mt5_execution_log_to_ngan_gon_chat,
    send_openai_output_to_telegram,
)

_log = logging.getLogger("automation_tool.tv_touch")

_EPS = 0.01

# In the inner loop: only commit LOAI after repeated confirmations.
LOAI_CONFIRM_ROUNDS = 3


def _ts_log(timezone_name: str, prefix: str, msg: str) -> None:
    try:
        z = ZoneInfo(timezone_name)
        ts = datetime.now(z).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.getLogger("automation_tool.journal").info(f"[{ts}] {prefix} | {msg}")


def _truncate(s: str, max_len: int = 140) -> str:
    s = (s or "").replace("\n", " ").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _next_02am_after(fr: datetime, z: ZoneInfo) -> datetime:
    fr = fr.astimezone(z)
    d = fr.date()
    today_2am = datetime.combine(d, dt_time(2, 0), tzinfo=z)
    if fr < today_2am:
        return today_2am
    next_d = d + timedelta(days=1)
    return datetime.combine(next_d, dt_time(2, 0), tzinfo=z)


def compute_session_cutoff(first_run: datetime, timezone_name: str) -> datetime:
    """
    - Before 13:00 local -> stop at 13:00 same day
    - From 13:00 onwards -> stop at next 02:00 AM
    """
    z = ZoneInfo(timezone_name)
    fr = first_run.astimezone(z) if first_run.tzinfo else first_run.replace(tzinfo=z)
    noon = dt_time(13, 0)
    if fr.time() < noon:
        return fr.replace(hour=13, minute=0, second=0, microsecond=0)
    return _next_02am_after(fr, z)


def before_cutoff(
    timezone_name: str,
    until_hour: int,
    *,
    session_cutoff_end: Optional[datetime] = None,
) -> bool:
    z = ZoneInfo(timezone_name)
    now = datetime.now(z)
    if session_cutoff_end is not None:
        co = session_cutoff_end
        if co.tzinfo is None:
            co = co.replace(tzinfo=z)
        else:
            co = co.astimezone(z)
        return now < co
    cutoff = now.replace(hour=until_hour, minute=0, second=0, microsecond=0)
    return now < cutoff


PollSupersedeTouch = Callable[
    [str, float],
    Optional[tuple[float, str, str]],
]


@dataclass(frozen=True)
class TouchFlowParams:
    capture_coinmap_yaml: Path
    charts_dir: Path
    storage_state_path: Optional[Path]
    headless: bool
    no_save_storage: bool
    wait_minutes: int
    until_hour: int
    timezone_name: str
    no_telegram: bool
    mt5_execute: bool = True
    mt5_symbol: Optional[str] = None
    mt5_dry_run: bool = False
    session_cutoff_end: Optional[datetime] = None


def _sleep_wait_minutes_with_poll(
    *,
    params: TouchFlowParams,
    touched_label: str,
    touched_price: float,
    poll_seconds: float,
    poll_supersede_touch: PollSupersedeTouch,
) -> tuple[bool, Optional[tuple[float, str, str]]]:
    """
    Sleep up to wait_minutes, but periodically poll for another touch that should supersede
    the current one. Returns (ok_before_cutoff, supersede_touch_or_none).
    """
    end = time.time() + params.wait_minutes * 60
    poll_iv = min(30.0, max(1.0, float(poll_seconds)))
    _ts_log(
        params.timezone_name,
        "tv-touch",
        f"Chờ {params.wait_minutes} phút (poll mỗi ~{poll_iv:.0f}s) trước lần hỏi OpenAI tiếp theo.",
    )
    while time.time() < end:
        if not before_cutoff(
            params.timezone_name, params.until_hour, session_cutoff_end=params.session_cutoff_end
        ):
            _ts_log(params.timezone_name, "tv-touch", "Hết khung giờ trong lúc chờ — dừng.")
            return (False, None)

        sup = poll_supersede_touch(touched_label, touched_price)
        if sup is not None:
            return (True, sup)

        remain = end - time.time()
        if remain <= 0:
            break
        time.sleep(min(poll_iv, remain, 30.0))

    ok = before_cutoff(
        params.timezone_name, params.until_hour, session_cutoff_end=params.session_cutoff_end
    )
    return (ok, None)


def run_intraday_touch_flow(
    *,
    settings: Settings,
    params: TouchFlowParams,
    touched_price: float,
    touched_label: str,
    touch_line: str,
    initial_response_id: str,
    browser_context: BrowserContext,
    last_alert_path: Path,
    page: Page,
    tv: dict,
    settle_ms: int,
    poll_seconds: float,
    poll_supersede_touch: PollSupersedeTouch,
) -> str:
    """
    Inner loop once a touch is detected.

    Returns: "entered" | "rejected" | "cutoff"
    """
    tz = params.timezone_name
    prev_id = initial_response_id
    first = True
    loai_streak = 0
    inner_i = 0

    _ts_log(
        tz,
        "tv-touch",
        f"=== Touch bắt đầu — label={touched_label} | giá={touched_price} | ctx={_truncate(touch_line, 220)}",
    )

    while before_cutoff(
        params.timezone_name, params.until_hour, session_cutoff_end=params.session_cutoff_end
    ):
        inner_i += 1
        st = read_last_alert_state(last_alert_path)
        if st is None:
            raise SystemExit(f"Missing last alert state at {last_alert_path}")
        p1, p2, p3 = st.prices
        _ts_log(tz, "tv-touch", f"Vòng trong #{inner_i}: chụp Coinmap M5 + hỏi OpenAI…")

        paths = capture_charts(
            coinmap_yaml=params.capture_coinmap_yaml,
            charts_dir=params.charts_dir,
            storage_state_path=params.storage_state_path,
            email=settings.coinmap_email,
            password=settings.coinmap_password,
            tradingview_password=settings.tradingview_password,
            save_storage_state=not params.no_save_storage,
            headless=params.headless,
            reuse_browser_context=browser_context,
            main_chart_symbol=read_main_chart_symbol(params.charts_dir),
        )
        _ts_log(tz, "tv-touch", f"Coinmap capture xong: {len(paths)} file(s).")
        json_path = coinmap_xauusd_5m_json_path(params.charts_dir)
        if json_path is None or not json_path.is_file():
            raise SystemExit(
                f"No main-pair 5m Coinmap JSON under {params.charts_dir} "
                f"(expected stamp coinmap_{read_main_chart_symbol(params.charts_dir)}_5m)."
            )

        # OpenAI follow-up message (kept compatible with existing prompt templates)
        if first:
            user_msg = JOURNAL_INTRADAY_FIRST_USER_TEMPLATE.format(
                touched_price=touched_price,
                p1=p1,
                p2=p2,
                p3=p3,
                journal_line=touch_line,
            )
        else:
            user_msg = JOURNAL_INTRADAY_RETRY_USER_TEMPLATE.format(
                wait_minutes=params.wait_minutes,
                touched_price=touched_price,
                journal_line=touch_line,
            )

        try:
            out_text, new_id = run_single_followup_responses(
                api_key=settings.openai_api_key,
                prompt_id=settings.openai_prompt_id,
                prompt_version=settings.openai_prompt_version,
                user_text=user_msg,
                coinmap_json_path=json_path,
                previous_response_id=prev_id,
                vector_store_ids=settings.openai_vector_store_ids,
                store=settings.openai_responses_store,
                include=settings.openai_responses_include,
            )
        except Exception as e:
            re_raise_unless_openai(e)
            raise

        write_last_response_id(new_id)
        prev_id = new_id

        # Apply first response JSON side effects (may auto-MT5, but we also handle intraday below)
        _ = apply_first_response_vao_lenh(
            out_text,
            last_alert_path=last_alert_path,
            mt5_execute=params.mt5_execute,
            mt5_dry_run=params.mt5_dry_run,
            mt5_symbol=params.mt5_symbol,
            telegram_bot_token=settings.telegram_bot_token,
            telegram_chat_id=settings.telegram_chat_id,
            telegram_analysis_detail_chat_id=settings.telegram_analysis_detail_chat_id,
            telegram_output_ngan_gon_chat_id=settings.telegram_output_ngan_gon_chat_id,
            telegram_source_label="tv-touch (watch)",
            auto_mt5_zone_label=touched_label,
        )

        act = parse_journal_intraday_action_from_openai_text(out_text)
        _ts_log(tz, "tv-touch", f"Parse intraday action: {act!r}")

        if act == "VÀO LỆNH":
            loai_streak = 0
            parsed, tl_err = parse_openai_output_md(out_text, symbol_override=params.mt5_symbol)
            if tl_err or parsed is None:
                _ts_log(
                    tz,
                    "tv-touch",
                    f"VÀO LỆNH nhưng trade_line không hợp lệ ({tl_err}) — coi như chờ.",
                )
                ok_sleep, sup = _sleep_wait_minutes_with_poll(
                    params=params,
                    touched_label=touched_label,
                    touched_price=touched_price,
                    poll_seconds=poll_seconds,
                    poll_supersede_touch=poll_supersede_touch,
                )
                if not ok_sleep:
                    return "cutoff"
                if sup is not None:
                    update_single_plan_status(touched_label, LOAI, path=last_alert_path)
                    touched_price, touch_line, touched_label = sup[0], sup[1], sup[2]
                    first = True
                    continue
                first = False
                continue

            update_single_plan_status(
                touched_label,
                VAO_LENH,
                path=last_alert_path,
                entry_manual=False,
            )
            if params.mt5_execute:
                ex = execute_trade(
                    parsed,
                    dry_run=params.mt5_dry_run,
                    symbol_override=params.mt5_symbol,
                )
                _ts_log(tz, "tv-touch", ex.message)
                if not params.no_telegram:
                    send_mt5_execution_log_to_ngan_gon_chat(
                        bot_token=settings.telegram_bot_token,
                        telegram_chat_id=settings.telegram_chat_id,
                        source="tv-touch",
                        text=format_mt5_execution_for_telegram(ex),
                    )
            if not params.no_telegram:
                send_openai_output_to_telegram(
                    bot_token=settings.telegram_bot_token,
                    chat_id=settings.telegram_chat_id,
                    raw=out_text,
                    default_parse_mode=settings.telegram_parse_mode,
                    summary_chat_id=settings.telegram_output_ngan_gon_chat_id,
                )
            return "entered"

        if act == "loại":
            loai_streak += 1
            if loai_streak < LOAI_CONFIRM_ROUNDS:
                ok_sleep, sup = _sleep_wait_minutes_with_poll(
                    params=params,
                    touched_label=touched_label,
                    touched_price=touched_price,
                    poll_seconds=poll_seconds,
                    poll_supersede_touch=poll_supersede_touch,
                )
                if not ok_sleep:
                    return "cutoff"
                if sup is not None:
                    update_single_plan_status(touched_label, LOAI, path=last_alert_path)
                    touched_price, touch_line, touched_label = sup[0], sup[1], sup[2]
                    first = True
                    loai_streak = 0
                    continue
                first = False
                continue
            update_single_plan_status(touched_label, LOAI, path=last_alert_path)
            return "rejected"

        # default: "chờ" or parse failure
        loai_streak = 0
        ok_sleep, sup = _sleep_wait_minutes_with_poll(
            params=params,
            touched_label=touched_label,
            touched_price=touched_price,
            poll_seconds=poll_seconds,
            poll_supersede_touch=poll_supersede_touch,
        )
        if not ok_sleep:
            return "cutoff"
        if sup is not None:
            update_single_plan_status(touched_label, LOAI, path=last_alert_path)
            touched_price, touch_line, touched_label = sup[0], sup[1], sup[2]
            first = True
            continue
        first = False

    return "cutoff"


_FLOAT_RE = re.compile(r"-?\d+(?:\.\d+)?")


def parse_first_float(text: str) -> Optional[float]:
    """
    Extract the first float-like number from text.
    Intended for TradingView UI strings; caller should gate out unstable/highlight states.
    """
    raw = (text or "").strip().replace(",", "")
    m = _FLOAT_RE.search(raw)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def parse_first_float_trunc0(text: str) -> Optional[float]:
    """
    Like :func:`parse_first_float` but truncates to integer precision.

    Example: ``4656.355`` -> ``4656.0``.
    """
    v = parse_first_float(text)
    if v is None:
        return None
    try:
        return float(int(v))
    except Exception:
        return None

