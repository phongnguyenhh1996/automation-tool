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
import threading
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
from automation_tool.mt5_manage import mt5_latest_position_ticket
from automation_tool.mt5_openai_parse import (
    is_last_price_hit_stop_loss,
    normalize_broker_xau_symbol,
    parse_journal_intraday_action_from_openai_text,
    parse_openai_output_md,
    parse_trade_line,
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
    update_plan_mt5_entry,
    update_single_plan_status,
    write_last_response_id,
)
from automation_tool.telegram_bot import (
    send_mt5_execution_log_to_ngan_gon_chat,
    send_openai_output_to_telegram,
)
from automation_tool.tradingview_last_price import read_watchlist_last_price_wait_stable

_log = logging.getLogger("automation_tool.tv_touch")

_EPS = 0.01

# In the inner loop: only commit LOAI after repeated confirmations.
LOAI_CONFIRM_ROUNDS = 4


class _AbortLongOp(RuntimeError):
    """
    Internal control-flow exception to abort long sync operations (Coinmap capture,
    OpenAI wait) when we detect SL hit, supersede touch, or cutoff.
    """

    def __init__(
        self,
        *,
        reason: str,
        supersede: Optional[tuple[float, str, str]] = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.supersede = supersede


@dataclass
class _AsyncOpenAIResult:
    done: bool = False
    out_text: str = ""
    new_id: str = ""
    error: Optional[BaseException] = None


def _run_worker_in_background_with_poll(
    *,
    worker: Callable[[], tuple[str, str]],
    poll_abort: Callable[[], None],
    poll_interval_s: float = 0.75,
) -> tuple[str, str]:
    """
    Run a blocking worker in a daemon thread while the caller continues polling.

    - ``poll_abort`` may raise ``_AbortLongOp`` to abort early.
    - Any worker exception is raised in the caller thread.
    """
    result = _AsyncOpenAIResult()

    def _bg() -> None:
        try:
            out_text0, new_id0 = worker()
            result.out_text = out_text0
            result.new_id = new_id0
        except BaseException as e:  # noqa: BLE001 - re-raise in caller thread
            result.error = e
        finally:
            result.done = True

    th = threading.Thread(target=_bg, name="tv-touch-worker", daemon=True)
    th.start()

    while not result.done:
        poll_abort()
        time.sleep(max(0.05, float(poll_interval_s)))

    if result.error is not None:
        raise result.error
    if not result.out_text or not result.new_id:
        raise SystemExit("Background worker returned empty result unexpectedly.")
    return result.out_text, result.new_id


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
    last_alert_path: Path,
    page: Page,
    tv: dict,
) -> tuple[Literal["ok", "cutoff", "rejected"], Optional[tuple[float, str, str]]]:
    """
    Sleep up to wait_minutes, but periodically poll for another touch that should supersede
    the current one. Returns (status, supersede_touch_or_none).
    """
    end = time.time() + params.wait_minutes * 60
    poll_iv = min(30.0, max(1.0, float(poll_seconds)))
    _ts_log(
        params.timezone_name,
        "tv-touch",
        f"Chờ {params.wait_minutes} phút (poll mỗi ~{poll_iv:.0f}s) trước lần hỏi OpenAI tiếp theo.",
    )
    sym = read_main_chart_symbol(params.charts_dir)
    sym_parse = normalize_broker_xau_symbol((params.mt5_symbol or "").strip() or sym)

    while time.time() < end:
        if not before_cutoff(
            params.timezone_name, params.until_hour, session_cutoff_end=params.session_cutoff_end
        ):
            _ts_log(params.timezone_name, "tv-touch", "Hết khung giờ trong lúc chờ — dừng.")
            return ("cutoff", None)

        st = read_last_alert_state(last_alert_path)
        if st is not None:
            tl_sl = (st.trade_line_by_label.get(touched_label) or "").strip()
            if tl_sl:
                pt_sl = parse_trade_line(tl_sl, sym_parse)
                if pt_sl is not None:
                    # Poll the Watchlist Last; if SL is already hit, reject immediately.
                    p_chk = read_watchlist_last_price_wait_stable(
                        page, tv, symbol=sym, timeout_ms=3000, poll_ms=250
                    )
                    if p_chk is not None and is_last_price_hit_stop_loss(p_chk, pt_sl, eps=_EPS):
                        _ts_log(
                            params.timezone_name,
                            "tv-touch",
                            f"Last={p_chk} chạm SL trong lúc chờ — loại {touched_label} và kết thúc vòng trong.",
                        )
                        update_single_plan_status(touched_label, LOAI, path=last_alert_path)
                        return ("rejected", None)

        sup = poll_supersede_touch(touched_label, touched_price)
        if sup is not None:
            return ("ok", sup)

        remain = end - time.time()
        if remain <= 0:
            break
        time.sleep(min(poll_iv, remain, 30.0))

    ok = before_cutoff(
        params.timezone_name, params.until_hour, session_cutoff_end=params.session_cutoff_end
    )
    return ("ok" if ok else "cutoff", None)


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

        if not first:
            sym = read_main_chart_symbol(params.charts_dir)
            sym_parse = normalize_broker_xau_symbol((params.mt5_symbol or "").strip() or sym)
            wait_ms = min(15_000, max(3_000, int(float(poll_seconds) * 1000)))
            p_chk = read_watchlist_last_price_wait_stable(
                page, tv, symbol=sym, timeout_ms=wait_ms, poll_ms=250
            )
            if p_chk is not None:
                tl_sl = (st.trade_line_by_label.get(touched_label) or "").strip()
                if tl_sl:
                    pt_sl = parse_trade_line(tl_sl, sym_parse)
                    if pt_sl is not None and is_last_price_hit_stop_loss(
                        p_chk, pt_sl, eps=_EPS
                    ):
                        _ts_log(
                            tz,
                            "tv-touch",
                            f"Last={p_chk} chạm SL trước Coinmap (lặp) — loại {touched_label}.",
                        )
                        update_single_plan_status(touched_label, LOAI, path=last_alert_path)
                        return "rejected"

        _ts_log(tz, "tv-touch", f"Vòng trong #{inner_i}: chụp Coinmap M5 + hỏi OpenAI…")

        def _poll_abort_during_long_ops(*, phase: str) -> None:
            if not before_cutoff(
                params.timezone_name, params.until_hour, session_cutoff_end=params.session_cutoff_end
            ):
                raise _AbortLongOp(reason="cutoff")

            st2 = read_last_alert_state(last_alert_path)
            if st2 is None:
                return

            sym2 = read_main_chart_symbol(params.charts_dir)
            sym_parse2 = normalize_broker_xau_symbol((params.mt5_symbol or "").strip() or sym2)

            # Poll last price for SL hit.
            tl_sl2 = (st2.trade_line_by_label.get(touched_label) or "").strip()
            if tl_sl2:
                pt_sl2 = parse_trade_line(tl_sl2, sym_parse2)
                if pt_sl2 is not None:
                    p_chk2 = read_watchlist_last_price_wait_stable(
                        page, tv, symbol=sym2, timeout_ms=2500, poll_ms=250
                    )
                    if p_chk2 is not None and is_last_price_hit_stop_loss(p_chk2, pt_sl2, eps=_EPS):
                        raise _AbortLongOp(reason="sl_hit")

            # Poll for a superseding touch (another label touched).
            sup2 = poll_supersede_touch(touched_label, touched_price)
            if sup2 is not None:
                raise _AbortLongOp(reason="supersede", supersede=sup2)

        try:
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
                progress_hook=lambda: _poll_abort_during_long_ops(phase="capture"),
            )
            _ts_log(tz, "tv-touch", f"Coinmap capture xong: {len(paths)} file(s).")
        except _AbortLongOp as a:
            if a.reason == "sl_hit":
                _ts_log(tz, "tv-touch", "Chạm SL trong lúc capture Coinmap — loại ngay.")
                update_single_plan_status(touched_label, LOAI, path=last_alert_path)
                return "rejected"
            if a.reason == "supersede" and a.supersede is not None:
                _ts_log(
                    tz,
                    "tv-touch",
                    f"Touch khác đã chạm trong lúc capture Coinmap — loại {touched_label} và chuyển touch mới.",
                )
                update_single_plan_status(touched_label, LOAI, path=last_alert_path)
                touched_price, touch_line, touched_label = a.supersede[0], a.supersede[1], a.supersede[2]
                first = True
                loai_streak = 0
                continue
            return "cutoff"
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

        # Run OpenAI request in background so we can continue polling `last` and supersede touches.
        try:
            out_text, new_id = _run_worker_in_background_with_poll(
                worker=lambda: run_single_followup_responses(
                    api_key=settings.openai_api_key,
                    prompt_id=settings.openai_prompt_id,
                    prompt_version=settings.openai_prompt_version,
                    user_text=user_msg,
                    coinmap_json_path=json_path,
                    previous_response_id=prev_id,
                    vector_store_ids=settings.openai_vector_store_ids,
                    store=settings.openai_responses_store,
                    include=settings.openai_responses_include,
                ),
                poll_abort=lambda: _poll_abort_during_long_ops(phase="waiting_openai"),
                poll_interval_s=0.75,
            )
        except _AbortLongOp as a:
            if a.reason == "sl_hit":
                _ts_log(tz, "tv-touch", "Chạm SL trong lúc chờ OpenAI — loại ngay (ignore output).")
                update_single_plan_status(touched_label, LOAI, path=last_alert_path)
                return "rejected"
            if a.reason == "supersede" and a.supersede is not None:
                _ts_log(
                    tz,
                    "tv-touch",
                    f"Touch khác đã chạm trong lúc chờ OpenAI — loại {touched_label} và chuyển touch mới (ignore output).",
                )
                update_single_plan_status(touched_label, LOAI, path=last_alert_path)
                touched_price, touch_line, touched_label = a.supersede[0], a.supersede[1], a.supersede[2]
                first = True
                loai_streak = 0
                continue
            return "cutoff"
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
            telegram_log_chat_id=settings.telegram_log_chat_id,
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
                    last_alert_path=last_alert_path,
                    page=page,
                    tv=tv,
                )
                if ok_sleep != "ok":
                    if ok_sleep == "rejected":
                        return "rejected"
                    return "cutoff"
                if sup is not None:
                    update_single_plan_status(touched_label, LOAI, path=last_alert_path)
                    touched_price, touch_line, touched_label = sup[0], sup[1], sup[2]
                    first = True
                    continue
                first = False
                continue

            # Guard: avoid duplicate MT5 execution if already entered for this label.
            st = read_last_alert_state(last_alert_path)
            if st is not None and st.status_by_label.get(touched_label) == VAO_LENH:
                _ts_log(
                    tz,
                    "tv-touch",
                    f"Bỏ qua MT5: `{touched_label}` đã ở trạng thái `{VAO_LENH}` trong last_alert_prices.",
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
                tid = int(ex.order) if ex.order else 0
                if (not tid or tid <= 0) and not params.mt5_dry_run and (ex.resolved_symbol or "").strip():
                    alt = mt5_latest_position_ticket(str(ex.resolved_symbol).strip())
                    if alt:
                        tid = int(alt)
                if ex.ok and tid > 0 and (parsed.raw_line or "").strip():
                    try:
                        update_plan_mt5_entry(
                            touched_label,
                            trade_line=parsed.raw_line.strip(),
                            mt5_ticket=tid,
                            path=last_alert_path,
                        )
                    except SystemExit:
                        pass
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
                    last_alert_path=last_alert_path,
                    page=page,
                    tv=tv,
                )
                if ok_sleep != "ok":
                    if ok_sleep == "rejected":
                        return "rejected"
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
            last_alert_path=last_alert_path,
            page=page,
            tv=tv,
        )
        if ok_sleep != "ok":
            if ok_sleep == "rejected":
                return "rejected"
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

