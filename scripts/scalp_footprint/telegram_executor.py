#!/usr/bin/env python3
"""
VPS daemon: listen Telegram for SCALP_EXEC lines from watch.py and place MT5 orders.

Requires Windows VPS with MetaTrader5 terminal logged in.

Env:
  TELEGRAM_BOT_TOKEN — gửi reply vào channel scalp (watch cũng dùng token này để post)
  SCALP_EXEC_LISTEN_BOT_TOKEN — bot KHÁC, admin channel scalp, dùng getUpdates (bắt buộc
    nếu watch post bằng TELEGRAM_BOT_TOKEN: Telegram không echo channel_post của chính bot đó)
  SCALP_EXEC_LOT=0.01
  SCALP_EXEC_PATTERNS=   (empty = all patterns; comma list to filter)
  SCALP_EXEC_SL_POINTS=4
  SCALP_EXEC_TP_POINTS=4
  SCALP_EXEC_ACCOUNT_IDS=main,acc2   (id trong accounts.json; empty = lọc zone scalp)
  SCALP_EXEC_DRY_RUN=1  (default dry-run on VPS until explicitly disabled)
  MT5_*                 (optional; omit to use terminal session)

SL/TP: lấy bid/ask MT5 live (XAUUSD/XAUUSDm/XAUUSDc), ±SCALP_EXEC_SL/TP_POINTS — không dùng giá futures trong EXEC line.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx

ROOT = Path(__file__).resolve().parents[2]
_SRC = ROOT / "src"
_SCALP = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
if str(_SCALP) not in sys.path:
    sys.path.insert(0, str(_SCALP))

import automation_tool.config  # noqa: F401 — load .env

from automation_tool.config import Settings, load_settings
from automation_tool.mt5_accounts import (
    MT5AccountEntry,
    filter_mt5_accounts_for_zone_label,
    load_mt5_accounts_for_cli,
    select_mt5_accounts_by_ids,
)
from automation_tool.mt5_execute import (
    MT5ExecutionResult,
    ensure_mt5_session,
    execute_trade,
    format_mt5_execution_for_telegram,
)
from automation_tool.mt5_multi import MT5MultiExecutionSummary, format_mt5_multi_for_telegram
from automation_tool.mt5_openai_parse import ParsedTrade
from automation_tool.mt5_manage import _mt5_init_current_terminal
from automation_tool.telegram_bot import send_message

from exec_line import (
    DEFAULT_SL_POINTS,
    DEFAULT_TP_POINTS,
    extract_exec_lines,
    exec_to_parsed_trade,
    parse_exec_line,
)
from scalp_mt5_live import build_scalp_trade_live

_log = logging.getLogger("scalp_footprint.telegram_executor")

DEFAULT_STATE_NAME = "scalp_footprint_executor_state.json"
ZONE_LABEL = "scalp"
# Cố định channel scalp footprint (cùng watch.py); không dùng TELEGRAM_CHAT_ID / *_CHAT_ID khác.
DEFAULT_SCALP_TELEGRAM_CHAT_ID = "-1004297700919"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        return default


def _pattern_whitelist() -> set[str] | None:
    raw = (os.getenv("SCALP_EXEC_PATTERNS") or "").strip()
    if not raw:
        return None
    items = {p.strip() for p in raw.split(",") if p.strip()}
    return items or None


def _account_ids_from_env() -> tuple[str, ...]:
    raw = (os.getenv("SCALP_EXEC_ACCOUNT_IDS") or "").strip()
    if not raw:
        return ()
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _send_bot_token(settings: Settings) -> str:
    return (settings.telegram_bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()


def _listen_bot_token(settings: Settings) -> str:
    """Bot token for getUpdates — must differ from watch poster when using a channel."""
    raw = (os.getenv("SCALP_EXEC_LISTEN_BOT_TOKEN") or "").strip()
    if raw:
        return raw
    return _send_bot_token(settings)


def _resolve_exec_accounts(
    accounts_path: Optional[Path],
    account_ids: tuple[str, ...],
) -> tuple[list[MT5AccountEntry], list[str]]:
    """Load accounts.json và chọn theo id (nếu có)."""
    all_accounts = load_mt5_accounts_for_cli(accounts_path)
    if not all_accounts:
        return [], list(account_ids)
    if not account_ids:
        return all_accounts, []
    selected, missing = select_mt5_accounts_by_ids(all_accounts, account_ids)
    return selected, missing


def _accounts_for_execution(
    all_accounts: list[MT5AccountEntry],
    account_ids: tuple[str, ...],
) -> list[MT5AccountEntry]:
    if account_ids:
        selected, _ = select_mt5_accounts_by_ids(all_accounts, account_ids)
        return selected
    return filter_mt5_accounts_for_zone_label(
        all_accounts,
        ZONE_LABEL,
        zone_id=ZONE_LABEL,
    )


def _build_live_trade_for_account(
    parsed: dict[str, Any],
    acc: MT5AccountEntry,
    *,
    lot: float,
    sl_points: float,
    tp_points: float,
    dry_run: bool,
) -> tuple[ParsedTrade, float]:
    session = ensure_mt5_session(
        terminal_path=acc.terminal_path,
        login=acc.login,
        password=acc.password,
        server=acc.server,
    )
    if not session.ok:
        if dry_run and float(parsed.get("entry_price") or 0) > 0:
            _log.warning(
                "MT5 session failed for %s (%s) — dry-run fallback futures entry ref",
                acc.id,
                session.message,
            )
            trade = exec_to_parsed_trade(
                parsed,
                lot=lot,
                entry_price=float(parsed["entry_price"]),
                sl_points=sl_points,
                tp_points=tp_points,
            )
            return trade, float(parsed["entry_price"])
        raise RuntimeError(f"[{acc.id}] {session.message}")

    trade, live_entry = build_scalp_trade_live(
        parsed,
        lot=lot,
        mt5=session.mt5,
        account_symbol_map=acc.symbol_map or None,
        sl_points=sl_points,
        tp_points=tp_points,
    )
    return trade, live_entry


def _build_live_trade_default_session(
    parsed: dict[str, Any],
    *,
    lot: float,
    sl_points: float,
    tp_points: float,
    dry_run: bool,
) -> tuple[ParsedTrade, float]:
    mt5 = _mt5_init_current_terminal()
    if mt5 is None:
        if dry_run and float(parsed.get("entry_price") or 0) > 0:
            _log.warning("MT5 terminal không kết nối — dry-run fallback futures entry ref")
            trade = exec_to_parsed_trade(
                parsed,
                lot=lot,
                entry_price=float(parsed["entry_price"]),
                sl_points=sl_points,
                tp_points=tp_points,
            )
            return trade, float(parsed["entry_price"])
        raise RuntimeError("Không kết nối được MT5 terminal (initialize failed)")

    return build_scalp_trade_live(
        parsed,
        lot=lot,
        mt5=mt5,
        sl_points=sl_points,
        tp_points=tp_points,
    )


def _execute_trade_on_account(
    trade: ParsedTrade,
    acc: MT5AccountEntry,
    *,
    lot: float,
    dry_run: bool,
    comment: str,
) -> MT5ExecutionResult:
    return execute_trade(
        trade,
        terminal_path=acc.terminal_path,
        login=acc.login,
        password=acc.password,
        server=acc.server,
        dry_run=dry_run,
        lot_override=lot,
        take_profit_target="tp1",
        take_profit_override=float(trade.tp1),
        log_tp2=False,
        order_comment=comment,
        account_id=acc.id,
        account_symbol_map=acc.symbol_map or None,
    )


def _default_state_path() -> Path:
    try:
        sym = load_settings().main_chart_symbol
        return Path("data") / sym / "charts" / DEFAULT_STATE_NAME
    except Exception:
        return Path("data/XAUUSD/charts") / DEFAULT_STATE_NAME


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"executed_trade_ids": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"executed_trade_ids": []}
    if not isinstance(data, dict):
        return {"executed_trade_ids": []}
    data.setdefault("executed_trade_ids", [])
    return data


def save_state(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ids = data.get("executed_trade_ids") or []
    if isinstance(ids, list):
        data["executed_trade_ids"] = sorted(set(str(x) for x in ids))[-1000:]
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _chat_id_from_envelope(env: dict[str, Any]) -> Optional[str]:
    chat = env.get("chat")
    if not isinstance(chat, dict):
        return None
    cid = chat.get("id")
    if isinstance(cid, int):
        return str(cid)
    if isinstance(cid, str) and cid.strip():
        return cid.strip()
    return None


def _text_from_update(update: dict[str, Any]) -> tuple[Optional[dict[str, Any]], str]:
    for key in ("message", "channel_post"):
        env = update.get(key)
        if not isinstance(env, dict):
            continue
        for field in ("text", "caption"):
            txt = env.get(field)
            if isinstance(txt, str) and txt.strip():
                return env, txt
    return None, ""


def _execute_parsed(
    parsed: dict[str, Any],
    *,
    lot: float,
    dry_run: bool,
    sl_points: float,
    tp_points: float,
    accounts_path: Optional[Path],
    account_ids: tuple[str, ...],
) -> tuple[str, float, float, float]:
    comment = f"scalp-{parsed.get('pattern_id', '')}"[:31]
    all_accounts, missing = _resolve_exec_accounts(accounts_path, account_ids)
    if missing:
        raise ValueError(f"account id không có trong accounts.json: {', '.join(missing)}")

    live_entry = 0.0
    last_sl = 0.0
    last_tp = 0.0

    if all_accounts:
        targets = _accounts_for_execution(all_accounts, account_ids)
        if not targets:
            raise ValueError("Không có account nào khớp (zone scalp hoặc account-ids)")

        summary = MT5MultiExecutionSummary()
        for acc in targets:
            try:
                trade, live_entry = _build_live_trade_for_account(
                    parsed,
                    acc,
                    lot=lot,
                    sl_points=sl_points,
                    tp_points=tp_points,
                    dry_run=dry_run,
                )
                last_sl, last_tp = trade.sl, trade.tp1
                ex = _execute_trade_on_account(
                    trade,
                    acc,
                    lot=lot,
                    dry_run=dry_run,
                    comment=comment,
                )
            except Exception as e:
                _log.exception("Execution failed for account %s", acc.id)
                ex = MT5ExecutionResult(ok=False, message=str(e), account_id=acc.id)
            summary.results.append(ex)
            if not ex.ok:
                summary.ok_all = False

        return format_mt5_multi_for_telegram(summary), live_entry, last_sl, last_tp

    if account_ids:
        raise ValueError(
            "SCALP_EXEC_ACCOUNT_IDS được set nhưng không load được accounts.json "
            f"(path={accounts_path or 'MT5_ACCOUNTS_JSON'})"
        )

    trade, live_entry = _build_live_trade_default_session(
        parsed,
        lot=lot,
        sl_points=sl_points,
        tp_points=tp_points,
        dry_run=dry_run,
    )
    ex = execute_trade(
        trade,
        dry_run=dry_run,
        lot_override=lot,
        take_profit_target="tp1",
        take_profit_override=float(trade.tp1),
        log_tp2=False,
        order_comment=comment,
    )
    return format_mt5_execution_for_telegram(ex), live_entry, trade.sl, trade.tp1


def handle_exec_line(
    line: str,
    *,
    state: dict[str, Any],
    lot: float,
    dry_run: bool,
    sl_points: float,
    tp_points: float,
    pattern_whitelist: set[str] | None,
    accounts_path: Optional[Path],
    account_ids: tuple[str, ...],
) -> tuple[Optional[str], bool]:
    """
    Process one SCALP_EXEC line.
    Returns (telegram_reply_or_none, executed).
    """
    parsed = parse_exec_line(line)
    if parsed is None:
        return None, False

    pattern_id = str(parsed.get("pattern_id") or "")
    if pattern_whitelist and pattern_id not in pattern_whitelist:
        _log.info("Skip pattern not in whitelist: %s", pattern_id)
        return None, False

    trade_id = str(parsed.get("trade_id") or "")
    executed_ids: set[str] = set(state.get("executed_trade_ids") or [])
    if trade_id in executed_ids:
        _log.info("Skip duplicate trade_id=%s", trade_id)
        return None, False

    side = str(parsed.get("side") or "")

    _log.info(
        "Executing %s MARKET %s (SL/TP from MT5 live ±%s/%s) dry_run=%s",
        pattern_id,
        side,
        sl_points,
        tp_points,
        dry_run,
    )

    try:
        result_text, live_entry, sl, tp = _execute_parsed(
            parsed,
            lot=lot,
            dry_run=dry_run,
            sl_points=sl_points,
            tp_points=tp_points,
            accounts_path=accounts_path,
            account_ids=account_ids,
        )
    except Exception as e:
        _log.exception("MT5 execution failed for %s", trade_id)
        return f"❌ Scalp EXEC failed ({pattern_id}): {e!r}", False

    executed_ids.add(trade_id)
    state["executed_trade_ids"] = sorted(executed_ids)[-1000:]

    prefix = "🧪 DRY-RUN" if dry_run else "✅ MT5"
    reply = (
        f"{prefix} scalp {pattern_id}\n"
        f"{side} MARKET @ {live_entry} (MT5 live)\n"
        f"SL {sl} TP {tp}\n"
        f"{result_text}"
    )
    return reply, True


def run_exec_line_once(
    line: str,
    *,
    settings: Settings,
    state_path: Path,
    lot: float,
    dry_run: bool,
    sl_points: float,
    tp_points: float,
    pattern_whitelist: set[str] | None,
    accounts_path: Optional[Path],
    account_ids: tuple[str, ...],
    notify: bool = True,
) -> int:
    """Execute one SCALP_EXEC line (manual catch-up). Returns process exit code."""
    send_token = _send_bot_token(settings)
    if not send_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required.")

    state = load_state(state_path)
    reply, executed = handle_exec_line(
        line.strip(),
        state=state,
        lot=lot,
        dry_run=dry_run,
        sl_points=sl_points,
        tp_points=tp_points,
        pattern_whitelist=pattern_whitelist,
        accounts_path=accounts_path,
        account_ids=account_ids,
    )
    save_state(state_path, state)

    if reply and notify:
        try:
            send_message(
                bot_token=send_token,
                chat_id=DEFAULT_SCALP_TELEGRAM_CHAT_ID,
                text=reply,
            )
        except Exception as e:
            _log.warning("Failed to send execution reply: %s", e)

    if reply:
        print(reply)
    return 0 if executed else 1


def run_executor(
    *,
    settings: Settings,
    state_path: Path,
    lot: float,
    dry_run: bool,
    sl_points: float,
    tp_points: float,
    pattern_whitelist: set[str] | None,
    accounts_path: Optional[Path],
    account_ids: tuple[str, ...],
    long_poll_timeout: int = 45,
) -> None:
    send_token = _send_bot_token(settings)
    listen_token = _listen_bot_token(settings)
    if not send_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required.")
    if not listen_token:
        raise SystemExit("SCALP_EXEC_LISTEN_BOT_TOKEN or TELEGRAM_BOT_TOKEN is required.")

    chat_id = DEFAULT_SCALP_TELEGRAM_CHAT_ID
    notify = chat_id
    state = load_state(state_path)
    base = f"https://api.telegram.org/bot{listen_token}/getUpdates"
    offset: Optional[int] = None
    allowed_updates = json.dumps(["message", "channel_post"])

    accounts_label = ",".join(account_ids) if account_ids else "zone:scalp"
    listen_src = "SCALP_EXEC_LISTEN_BOT_TOKEN" if os.getenv("SCALP_EXEC_LISTEN_BOT_TOKEN", "").strip() else "TELEGRAM_BOT_TOKEN"
    _log.info(
        "Scalp executor listening chat_id=%s listen_token=%s lot=%s sl/tp=%s/%s accounts=%s dry_run=%s patterns=%s state=%s",
        chat_id,
        listen_src,
        lot,
        sl_points,
        tp_points,
        accounts_label,
        dry_run,
        sorted(pattern_whitelist) if pattern_whitelist else "ALL",
        state_path,
    )
    if listen_token == send_token:
        _log.warning(
            "SCALP_EXEC_LISTEN_BOT_TOKEN not set — polling same bot that watch uses to POST. "
            "Telegram does not deliver channel_post updates for a bot's own messages; "
            "create a second bot, add it as channel admin, set SCALP_EXEC_LISTEN_BOT_TOKEN on VPS."
        )

    if not dry_run:
        try:
            send_message(
                bot_token=send_token,
                chat_id=notify,
                text=(
                    f"✅ Scalp footprint executor started (LIVE lot={lot}, "
                    f"MARKET SL={sl_points} TP={tp_points}, accounts={accounts_label})"
                ),
            )
        except Exception as e:
            _log.warning("Startup Telegram ping failed: %s", e)

    with httpx.Client(timeout=float(long_poll_timeout) + 10.0) as client:
        while True:
            try:
                params: dict[str, Any] = {
                    "timeout": int(long_poll_timeout),
                    "allowed_updates": allowed_updates,
                }
                if offset is not None:
                    params["offset"] = offset
                r = client.get(base, params=params)
                r.raise_for_status()
                payload = r.json()
                if not payload.get("ok"):
                    _log.warning("getUpdates ok=false: %s", payload)
                    time.sleep(2.0)
                    continue

                updates = payload.get("result")
                if not isinstance(updates, list):
                    time.sleep(0.5)
                    continue

                for upd in updates:
                    if not isinstance(upd, dict):
                        continue
                    uid = upd.get("update_id")
                    if isinstance(uid, int):
                        offset = uid + 1

                    env, text = _text_from_update(upd)
                    if env is None or not text:
                        continue
                    msg_chat = _chat_id_from_envelope(env)
                    if not msg_chat or msg_chat != chat_id:
                        continue

                    exec_lines = extract_exec_lines(text)
                    if not exec_lines:
                        continue

                    for line in exec_lines:
                        reply, _ = handle_exec_line(
                            line,
                            state=state,
                            lot=lot,
                            dry_run=dry_run,
                            sl_points=sl_points,
                            tp_points=tp_points,
                            pattern_whitelist=pattern_whitelist,
                            accounts_path=accounts_path,
                            account_ids=account_ids,
                        )
                        save_state(state_path, state)
                        if reply:
                            try:
                                send_message(bot_token=send_token, chat_id=notify, text=reply)
                            except Exception as e:
                                _log.warning("Failed to send execution reply: %s", e)

            except httpx.HTTPError as e:
                _log.warning("Telegram poll error: %s", e)
                time.sleep(3.0)
            except KeyboardInterrupt:
                _log.info("Stopped by user")
                return


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Listen Telegram SCALP_EXEC lines and execute on MT5.")
    p.add_argument("--state-file", type=Path, default=None)
    p.add_argument("--lot", type=float, default=None, help="Override SCALP_EXEC_LOT")
    p.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Simulate MT5 (default: SCALP_EXEC_DRY_RUN env or True)",
    )
    p.add_argument(
        "--patterns",
        default=None,
        help="Optional comma whitelist (default: all patterns)",
    )
    p.add_argument(
        "--sl-points",
        type=float,
        default=None,
        help=f"SL distance in giá (default {DEFAULT_SL_POINTS})",
    )
    p.add_argument(
        "--tp-points",
        type=float,
        default=None,
        help=f"TP distance in giá (default {DEFAULT_TP_POINTS})",
    )
    p.add_argument("--accounts", type=Path, default=None, help="mt5 accounts.json path")
    p.add_argument(
        "--account-ids",
        default=None,
        help="Comma-separated account id from accounts.json (override SCALP_EXEC_ACCOUNT_IDS)",
    )
    p.add_argument("--long-poll-timeout", type=int, default=45)
    p.add_argument(
        "--exec-line",
        default=None,
        help="Execute one SCALP_EXEC line then exit (manual catch-up)",
    )
    p.add_argument(
        "--no-notify",
        action="store_true",
        help="With --exec-line: do not post result to Telegram",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = load_settings()
    state_path = args.state_file or _default_state_path()
    lot = args.lot if args.lot is not None else _env_float("SCALP_EXEC_LOT", 0.01)

    if args.dry_run is None:
        dry_run = _env_bool("SCALP_EXEC_DRY_RUN", True)
    else:
        dry_run = bool(args.dry_run)

    if args.patterns is not None:
        wl = {p.strip() for p in args.patterns.split(",") if p.strip()} or None
    else:
        wl = _pattern_whitelist()

    sl_points = (
        args.sl_points
        if args.sl_points is not None
        else _env_float("SCALP_EXEC_SL_POINTS", DEFAULT_SL_POINTS)
    )
    tp_points = (
        args.tp_points
        if args.tp_points is not None
        else _env_float("SCALP_EXEC_TP_POINTS", DEFAULT_TP_POINTS)
    )

    if args.account_ids is not None:
        account_ids = tuple(p.strip() for p in args.account_ids.split(",") if p.strip())
    else:
        account_ids = _account_ids_from_env()

    if args.exec_line:
        raise SystemExit(
            run_exec_line_once(
                args.exec_line,
                settings=settings,
                state_path=state_path,
                lot=lot,
                dry_run=dry_run,
                sl_points=sl_points,
                tp_points=tp_points,
                pattern_whitelist=wl,
                accounts_path=args.accounts,
                account_ids=account_ids,
                notify=not args.no_notify,
            )
        )

    run_executor(
        settings=settings,
        state_path=state_path,
        lot=lot,
        dry_run=dry_run,
        sl_points=sl_points,
        tp_points=tp_points,
        pattern_whitelist=wl,
        accounts_path=args.accounts,
        account_ids=account_ids,
        long_poll_timeout=args.long_poll_timeout,
    )


if __name__ == "__main__":
    main()
