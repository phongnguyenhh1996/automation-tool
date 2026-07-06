#!/usr/bin/env python3
"""Headless GoCharting footprint watcher: fetch on M5/M15 close, detect scalp signals, Telegram alert."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
_SRC = ROOT / "src"
_SCALP = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
if str(_SCALP) not in sys.path:
    sys.path.insert(0, str(_SCALP))

from candle_confirm import filter_confirmed  # noqa: E402
from detect import _format_signal_text  # noqa: E402
from footprint_loader import load_footprint_json  # noqa: E402
from patterns import detect_patterns  # noqa: E402
from scheduling import (  # noqa: E402
    intervals_due,
    next_close_trigger,
    now_gmt7_naive,
    seconds_until,
)

_log = logging.getLogger("scalp_footprint.watch")

DEFAULT_TELEGRAM_CHAT_ID = "-1004297700919"
DEFAULT_BUFFER_SEC = 20
DEFAULT_STATE_NAME = "scalp_footprint_watch_state.json"


def _default_charts_dir() -> Path:
    try:
        from automation_tool.config import load_settings

        sym = load_settings().main_chart_symbol
        return Path("data") / sym / "charts"
    except Exception:
        return Path("data/XAUUSD/charts")


def _default_gocharting_yaml() -> Path:
    try:
        from automation_tool.config import default_gocharting_config_path

        return default_gocharting_config_path()
    except Exception:
        return ROOT / "config" / "gocharting.yaml"


def _combined_json_path(charts_dir: Path, interval: str) -> Path:
    return charts_dir / "footprint_images" / f"footprint_combined_{interval}.json"


def _signal_key(sig: dict[str, Any]) -> str:
    return "|".join(
        [
            str(sig.get("timeframe") or ""),
            str(sig.get("time_gmt7") or ""),
            str(sig.get("pattern_id") or ""),
            str(sig.get("bar_index") or ""),
        ]
    )


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"last_processed": {}, "sent_keys": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"last_processed": {}, "sent_keys": []}
    if not isinstance(data, dict):
        return {"last_processed": {}, "sent_keys": []}
    data.setdefault("last_processed", {})
    data.setdefault("sent_keys", [])
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _parse_processed_times(raw: dict[str, Any]) -> dict[str, datetime]:
    out: dict[str, datetime] = {}
    for iv, val in (raw or {}).items():
        if isinstance(val, str) and val.strip():
            try:
                out[str(iv)] = datetime.fromisoformat(val)
            except ValueError:
                continue
    return out


def _serialize_processed_times(values: dict[str, datetime]) -> dict[str, str]:
    return {iv: dt.isoformat() for iv, dt in values.items()}


def _map_paths_by_interval(
    paths: list[Path],
    intervals: tuple[str, ...],
) -> dict[str, Path]:
    """Map capture output paths to interval keys (``5m`` must not match ``15m``)."""
    expected = {
        iv.strip().lower(): f"footprint_combined_{iv.strip().lower()}.json" for iv in intervals
    }
    by_iv: dict[str, Path] = {}
    for path in paths:
        name = path.name.lower()
        for iv, stem in expected.items():
            if name == stem:
                by_iv[iv] = path
                break
    return by_iv


def capture_footprint_headless(
    *,
    intervals: tuple[str, ...],
    charts_dir: Path,
    gocharting_yaml: Path,
    headless: bool = True,
    parallel: bool = False,
    extra_session_days: int | None = None,
    stale_fallback: bool = True,
) -> list[Path]:
    """Capture WS footprint sequentially in one browser (default)."""
    from automation_tool.config import default_storage_state_path, load_all_dotenv
    from automation_tool.gocharting_capture import load_gocharting_yaml
    from automation_tool.gocharting_ws_capture import capture_footprint_ws_plan
    from automation_tool.playwright_browser import close_browser_and_context, launch_chrome_context
    from playwright.sync_api import sync_playwright

    load_all_dotenv()
    from automation_tool.gocharting_gc_spot_convert import native_gc_footprint_cfg

    cfg = native_gc_footprint_cfg(load_gocharting_yaml(gocharting_yaml))
    email = os.getenv("GOCHARTING_EMAIL", "")
    password = os.getenv("GOCHARTING_PASSWORD", "")

    use_parallel = parallel and len(intervals) > 1
    if use_parallel:
        return capture_footprint_ws_plan(
            None,
            cfg,
            charts_dir=charts_dir,
            email=email,
            password=password,
            gocharting_yaml=gocharting_yaml,
            capture_intervals=intervals,
            parallel=True,
            extra_session_days=extra_session_days,
            headless=headless,
        )

    storage = default_storage_state_path()
    vw = int(cfg.get("viewport_width", 1920))
    vh = int(cfg.get("viewport_height", 1080))

    with sync_playwright() as p:
        browser, context = launch_chrome_context(
            p,
            headless=headless,
            storage_state_path=storage if storage.is_file() else None,
            viewport_width=vw,
            viewport_height=vh,
        )
        try:
            return capture_footprint_ws_plan(
                context,
                cfg,
                charts_dir=charts_dir,
                email=email,
                password=password,
                gocharting_yaml=gocharting_yaml,
                capture_intervals=intervals,
                parallel=False,
                extra_session_days=extra_session_days,
                headless=headless,
                stale_fallback=stale_fallback,
            )
        finally:
            close_browser_and_context(browser, context)


def detect_latest_signals(
    json_path: Path,
    *,
    interval: str,
    confirmed: bool,
) -> list[dict[str, Any]]:
    from automation_tool.gocharting_ws_decode import drop_forming_footprint_candle

    doc = load_footprint_json(json_path)
    iv = interval.strip().lower()
    trimmed = drop_forming_footprint_candle(
        {"candles": doc["candles"], **{k: v for k, v in doc.items() if k != "candles"}},
        interval=iv,
        now=now_gmt7_naive(),
    )
    candles = trimmed.get("candles") or []
    if len(candles) < 2:
        return []

    raw_signals = detect_patterns(candles, interval=iv, latest_only=True)
    if confirmed:
        confirmed_signals = filter_confirmed(raw_signals, candles, interval=iv)
    else:
        confirmed_signals = list(raw_signals)
    return [s.to_dict() for s in confirmed_signals]


def send_telegram_alert(
    *,
    bot_token: str,
    chat_id: str,
    text: str,
    parse_mode: str | None = None,
) -> None:
    from automation_tool.telegram_bot import send_message

    send_message(
        bot_token=bot_token,
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode,
    )


def format_alert_message(signals: list[dict[str, Any]], *, interval: str) -> str:
    lines = [f"📊 Scalp footprint {interval.upper()} — {len(signals)} signal(s)"]
    for sig in signals:
        lines.append("")
        lines.append(_format_signal_text(sig))
    return "\n".join(lines)


def process_intervals(
    *,
    intervals: tuple[str, ...],
    charts_dir: Path,
    gocharting_yaml: Path,
    confirmed: bool,
    headless: bool,
    bot_token: str,
    chat_id: str,
    state_path: Path,
    dry_run: bool,
) -> None:
    from scheduling import latest_closed_candle_open

    from automation_tool.gocharting_ws_decode import (
        footprint_last_candle_fresh,
        last_closed_candle_open,
    )

    def _last_closed_in_file(json_path: Path, *, interval: str) -> datetime | None:
        doc = load_footprint_json(json_path)
        return last_closed_candle_open(
            {"candles": doc["candles"]},
            interval=interval,
            now=now_gmt7_naive(),
        )

    state = load_state(state_path)
    last_processed = _parse_processed_times(state.get("last_processed"))
    sent_keys: set[str] = set(state.get("sent_keys") or [])

    _log.info("Capturing footprint headless: %s", ", ".join(intervals))
    paths = capture_footprint_headless(
        intervals=intervals,
        charts_dir=charts_dir,
        gocharting_yaml=gocharting_yaml,
        headless=headless,
    )
    path_by_iv = _map_paths_by_interval(paths, intervals)

    now = now_gmt7_naive()
    alerts: list[str] = []

    for iv in intervals:
        json_path = path_by_iv.get(iv) or _combined_json_path(charts_dir, iv)
        if not json_path.is_file():
            _log.warning("No footprint JSON for %s at %s", iv, json_path)
            continue

        signals = detect_latest_signals(json_path, interval=iv, confirmed=confirmed)
        new_signals = [s for s in signals if _signal_key(s) not in sent_keys]

        minutes = 5 if iv == "5m" else 15
        closed_open = latest_closed_candle_open(now, minutes)
        last_closed = _last_closed_in_file(json_path, interval=iv)
        file_fresh = footprint_last_candle_fresh(last_closed, closed_open)
        if file_fresh:
            last_processed[iv] = closed_open
        else:
            _log.warning(
                "%s: stale footprint file last_closed=%s expected=%s — skip state update",
                iv,
                last_closed.isoformat() if last_closed else "?",
                closed_open.isoformat(),
            )
        _log.info(
            "%s: closed_bar=%s last_closed=%s fresh=%s signals=%d new=%d path=%s",
            iv,
            closed_open.isoformat(),
            last_closed.isoformat() if last_closed else "?",
            file_fresh,
            len(signals),
            len(new_signals),
            json_path.name,
        )

        if new_signals:
            alerts.append(format_alert_message(new_signals, interval=iv))
            for s in new_signals:
                sent_keys.add(_signal_key(s))

    if alerts:
        body = "\n\n---\n\n".join(alerts)
        if dry_run:
            print(body)
        else:
            send_telegram_alert(
                bot_token=bot_token,
                chat_id=chat_id,
                text=body,
            )
            _log.info("Telegram alert sent to %s", chat_id)
    else:
        _log.info("No new signals for intervals: %s", ", ".join(intervals))

    state["last_processed"] = _serialize_processed_times(last_processed)
    state["sent_keys"] = sorted(sent_keys)[-500:]
    save_state(state_path, state)


def run_once(args: argparse.Namespace) -> None:
    now = now_gmt7_naive()
    last_processed = _parse_processed_times(load_state(args.state_file).get("last_processed"))
    due = intervals_due(now, buffer_sec=args.buffer_sec, last_processed=last_processed)
    if not due:
        due = ["5m", "15m"]
        _log.info("Nothing due by schedule — forcing capture: %s", due)
    process_intervals(
        intervals=tuple(due),
        charts_dir=args.charts_dir,
        gocharting_yaml=args.gocharting_yaml,
        confirmed=args.confirmed,
        headless=not args.headed,
        bot_token=args.bot_token,
        chat_id=args.chat_id,
        state_path=args.state_file,
        dry_run=args.dry_run,
    )


def run_loop(args: argparse.Namespace) -> None:
    _log.info(
        "Scalp footprint watch started | buffer=%ds chat=%s confirmed=%s",
        args.buffer_sec,
        args.chat_id,
        args.confirmed,
    )
    if not args.dry_run and args.bot_token:
        try:
            send_telegram_alert(
                bot_token=args.bot_token,
                chat_id=args.chat_id,
                text="✅ Scalp footprint watch started (M5/M15 headless)",
            )
        except Exception as e:
            _log.warning("Startup Telegram ping failed: %s", e)

    while True:
        now = now_gmt7_naive()
        state = load_state(args.state_file)
        last_processed = _parse_processed_times(state.get("last_processed"))
        due = intervals_due(now, buffer_sec=args.buffer_sec, last_processed=last_processed)

        if due:
            try:
                process_intervals(
                    intervals=tuple(due),
                    charts_dir=args.charts_dir,
                    gocharting_yaml=args.gocharting_yaml,
                    confirmed=args.confirmed,
                    headless=not args.headed,
                    bot_token=args.bot_token,
                    chat_id=args.chat_id,
                    state_path=args.state_file,
                    dry_run=args.dry_run,
                )
            except Exception:
                _log.exception("Capture/detect cycle failed")
                if not args.dry_run and args.bot_token:
                    try:
                        send_telegram_alert(
                            bot_token=args.bot_token,
                            chat_id=args.chat_id,
                            text="⚠️ Scalp footprint watch error — xem log server",
                        )
                    except Exception:
                        pass
            continue

        t5 = next_close_trigger(now, 5, buffer_sec=args.buffer_sec)
        t15 = next_close_trigger(now, 15, buffer_sec=args.buffer_sec)
        wake_at = min(t5, t15)
        sleep_s = seconds_until(wake_at, now)
        _log.debug("Sleep %.0fs until %s", sleep_s, wake_at.isoformat())
        time.sleep(min(sleep_s, 3600))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Watch M5/M15 candle closes, fetch GoCharting footprint headless, alert Telegram.",
    )
    p.add_argument("--charts-dir", type=Path, default=None)
    p.add_argument("--gocharting-yaml", type=Path, default=None)
    p.add_argument("--state-file", type=Path, default=None)
    p.add_argument("--telegram-chat-id", default=DEFAULT_TELEGRAM_CHAT_ID)
    p.add_argument("--buffer-sec", type=int, default=DEFAULT_BUFFER_SEC)
    p.add_argument("--confirmed", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--headed", action="store_true", help="Show browser (default: headless)")
    p.add_argument("--dry-run", action="store_true", help="Print signals, do not send Telegram")
    p.add_argument("--once", action="store_true", help="Single capture cycle then exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    from automation_tool.config import load_all_dotenv, load_settings

    load_all_dotenv()
    settings = load_settings()
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    args.charts_dir = args.charts_dir or _default_charts_dir()
    args.gocharting_yaml = args.gocharting_yaml or _default_gocharting_yaml()
    args.state_file = args.state_file or (args.charts_dir / DEFAULT_STATE_NAME)
    args.chat_id = (args.telegram_chat_id or DEFAULT_TELEGRAM_CHAT_ID).strip()
    args.bot_token = (settings.telegram_bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()

    if not args.dry_run and not args.bot_token:
        _log.error("TELEGRAM_BOT_TOKEN required (or use --dry-run)")
        return 1

    if args.once:
        run_once(args)
    else:
        run_loop(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
