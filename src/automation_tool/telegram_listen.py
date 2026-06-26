from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

from automation_tool.config import Settings, resolved_openai_model
from automation_tool.openai_prompt_flow import run_text_followup_responses
from automation_tool.telegram_bot import (
    download_telegram_images,
    image_file_ids_from_message,
    send_message,
    send_openai_output_to_telegram,
)
from automation_tool.zones_state import mark_zone_status_loai_by_id

_log = logging.getLogger("automation_tool.telegram_listen")

_EXPLAIN_FOLLOWUP_MODEL = "gpt-5.4-mini"
_ASK_HIGH_FOLLOWUP_MODEL = "gpt-5.4"


@dataclass(frozen=True)
class TelegramListenParams:
    poll_interval_seconds: float = 0.5
    long_poll_timeout_seconds: int = 45
    full_main_symbol: str = "XAUUSD"
    update_main_symbol: str = "XAUUSD"
    openai_model: Optional[str] = None


@dataclass
class _ManagedProc:
    name: str
    cmd: list[str]
    popen: subprocess.Popen[str]
    started_at: float


_PROC_LOCK = threading.Lock()
_PROCS: list[_ManagedProc] = []


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _normalize_chat_id(v: Optional[str]) -> str:
    return (v or "").strip()


def _command_text_from_envelope(env: dict[str, Any]) -> str:
    """Command text from ``text`` or photo/document ``caption``."""
    txt = env.get("text")
    if isinstance(txt, str) and txt.strip():
        return txt.strip()
    cap = env.get("caption")
    if isinstance(cap, str) and cap.strip():
        return cap.strip()
    return ""


def _extract_text(update: dict[str, Any]) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """
    Return (envelope, text) where envelope is the Telegram object containing chat/from/message_id.
    Supports: message, channel_post (``text`` or image ``caption``).
    """
    for k in ("message", "channel_post"):
        env = update.get(k)
        if isinstance(env, dict):
            txt = _command_text_from_envelope(env)
            if txt:
                return env, txt
    return None, None


def _collect_ask_image_file_ids(env: dict[str, Any]) -> list[str]:
    """Images from the command message, or from ``reply_to_message`` when text-only."""
    ids = image_file_ids_from_message(env)
    if ids:
        return ids
    reply = env.get("reply_to_message")
    if isinstance(reply, dict):
        return image_file_ids_from_message(reply)
    return []


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


def _message_id_from_envelope(env: dict[str, Any]) -> Optional[int]:
    mid = env.get("message_id")
    return mid if isinstance(mid, int) else None


def _message_thread_id_from_envelope(env: dict[str, Any]) -> Optional[int]:
    """
    Telegram forums/topics: messages can carry message_thread_id.
    If present, we should send replies into the same thread.
    """
    tid = env.get("message_thread_id")
    return tid if isinstance(tid, int) else None


def _parse_command(text: str) -> tuple[Optional[str], str]:
    """
    Parse "/full", "/update", "/loai", "/stop", "/analyze-many", "/ask", "/ask-high", "/full@BotName".
    Returns (cmd, args_text) where args_text is the remaining raw text (may be empty).
    """
    t = (text or "").strip()
    if not t.startswith("/"):
        return None, ""
    parts = t.split(maxsplit=1)
    head = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""
    cmd = head[1:].split("@", 1)[0].strip().lower()
    return (cmd or None), rest


def _parse_ask_args(args_text: str) -> tuple[Optional[str], str]:
    """
    Accept: "<openai_response_id> <noi_dung...>".
    Returns (openai_response_id, noi_dung). openai_response_id may be None.
    """
    raw = (args_text or "").strip()
    if not raw:
        return None, ""
    parts = raw.split(maxsplit=1)
    rid = parts[0].strip()
    msg = parts[1].strip() if len(parts) > 1 else ""
    if not rid:
        return None, msg
    return rid, msg


def _explain_followup_model(_requested_model: Optional[str] = None) -> str:
    """Force [RETROSPECTIVE_ANALYSIS]/EXPLAIN follow-ups to the mini model."""
    return _EXPLAIN_FOLLOWUP_MODEL


def _parse_symbols_from_args_text(args_text: str) -> Optional[str]:
    """
    Accept: "EURUSD,USDJPY" or "EURUSD USDJPY" or "EURUSD, USDJPY".
    Returns normalized comma-separated string (e.g. "EURUSD,USDJPY") or None.
    """
    raw = (args_text or "").strip()
    if not raw:
        return None
    # Replace whitespace with commas then split.
    raw = raw.replace("\n", " ").replace("\t", " ")
    raw = raw.replace(" ", ",")
    parts = [p.strip().upper() for p in raw.split(",") if p.strip()]
    if not parts:
        return None
    # De-dupe while preserving order.
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return ",".join(out)


def _send_status(settings: Settings, chat_id: str, text: str) -> None:
    try:
        send_message(
            bot_token=settings.telegram_bot_token,
            chat_id=chat_id,
            text=text,
            parse_mode=settings.telegram_parse_mode,
        )
    except Exception as e:
        _log.warning("Could not send Telegram status: %s", e)


def _handle_telegram_ask_followup(
    *,
    settings: Settings,
    listen_chat_id: str,
    reply_to_message_id: Optional[int],
    message_thread_id: Optional[int],
    message_env: dict[str, Any],
    args_text: str,
    model: str,
    cmd_label: str,
) -> None:
    openai_response_id, noi_dung = _parse_ask_args(args_text)
    if not openai_response_id or not noi_dung:
        _send_status(
            settings,
            listen_chat_id,
            f"Cú pháp: {cmd_label} <openai_response_id> <noi_dung>\n"
            f"Ví dụ: {cmd_label} resp_abc123 Hãy tóm tắt 3 vùng giá và gợi ý trade_line.\n"
            f"Có thể đính kèm ảnh (photo/document) hoặc reply vào tin có ảnh.",
        )
        return
    ask_body = (noi_dung or "").strip()
    if not ask_body.upper().startswith("[RETROSPECTIVE"):
        ask_body = f"[RETROSPECTIVE_ANALYSIS]\n{ask_body}"
    try:
        image_file_ids = _collect_ask_image_file_ids(message_env)
        with tempfile.TemporaryDirectory(prefix="tg-ask-") as tmp:
            image_paths: list[Path] = []
            if image_file_ids:
                image_paths = download_telegram_images(
                    bot_token=settings.telegram_bot_token,
                    file_ids=image_file_ids,
                    dest_dir=Path(tmp),
                )
            out_text, new_id = run_text_followup_responses(
                api_key=settings.openai_api_key,
                user_text=ask_body,
                previous_response_id=openai_response_id,
                vector_store_ids=settings.openai_vector_store_ids,
                store=settings.openai_responses_store,
                include=settings.openai_responses_include,
                model=model,
                image_paths=image_paths,
            )
        if new_id:
            out_text = f"(openai_response_id={new_id})\n\n{out_text}".strip()
        send_openai_output_to_telegram(
            bot_token=settings.telegram_bot_token,
            chat_id=listen_chat_id,
            raw=out_text,
            default_parse_mode=settings.telegram_parse_mode,
            summary_chat_id=None,
            detail_chat_id=None,
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
        )
    except Exception as e:
        _send_status(
            settings,
            listen_chat_id,
            f"❌ {cmd_label} failed: {e!s}",
        )


def _spawn_managed_process(
    *,
    name: str,
    cmd: list[str],
    cwd: Path,
) -> _ManagedProc:
    """
    Start a process in its own process group/session so /stop can terminate the whole tree.
    """
    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    p = subprocess.Popen(cmd, **kwargs)  # type: ignore[arg-type]
    mp = _ManagedProc(name=name, cmd=cmd, popen=p, started_at=time.time())
    with _PROC_LOCK:
        _PROCS.append(mp)
    return mp


def _terminate_managed_process(mp: _ManagedProc, *, kill: bool) -> None:
    p = mp.popen
    if p.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            # taskkill kills the process tree reliably on Windows.
            args = ["taskkill", "/PID", str(p.pid), "/T"]
            if kill:
                args.append("/F")
            subprocess.run(args, capture_output=True, text=True)
        except Exception:
            try:
                p.terminate()
            except Exception:
                pass
        return
    try:
        pgid = os.getpgid(p.pid)
    except Exception:
        pgid = None
    sig = signal.SIGKILL if kill else signal.SIGTERM
    try:
        if pgid is not None:
            os.killpg(pgid, sig)
        else:
            os.kill(p.pid, sig)
    except Exception:
        try:
            p.kill() if kill else p.terminate()
        except Exception:
            pass


def _stop_all_processes(settings: Settings, reply_chat_id: str) -> None:
    with _PROC_LOCK:
        procs = list(_PROCS)

    if not procs:
        _send_status(settings, reply_chat_id, "🛑 /stop: no running jobs to stop.")
        return

    stopped: list[str] = []
    for mp in procs:
        if mp.popen.poll() is None:
            _terminate_managed_process(mp, kill=False)
            stopped.append(f"{mp.name} (pid={mp.popen.pid})")

    # Give processes a moment to exit gracefully, then force kill any that remain.
    time.sleep(1.5)
    killed: list[str] = []
    for mp in procs:
        if mp.popen.poll() is None:
            _terminate_managed_process(mp, kill=True)
            killed.append(f"{mp.name} (pid={mp.popen.pid})")

    # Prune finished processes from registry.
    with _PROC_LOCK:
        _PROCS[:] = [mp for mp in _PROCS if mp.popen.poll() is None]

    lines: list[str] = ["🛑 /stop requested."]
    if stopped:
        lines.append("Sent TERM to:")
        lines.extend(f"- {x}" for x in stopped)
    if killed:
        lines.append("Forced kill on:")
        lines.extend(f"- {x}" for x in killed)
    _send_status(settings, reply_chat_id, "\n".join(lines))


def _run_full_pipeline_in_thread(
    *,
    settings: Settings,
    reply_chat_id: str,
    full_main_symbol: str,
    trigger_message_id: Optional[int],
) -> None:
    """
    Runs the full daily pipeline asynchronously and posts start/finish messages.
    """
    root = _project_root()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    ref = f"(msg_id={trigger_message_id})" if trigger_message_id else ""
    _send_status(
        settings,
        reply_chat_id,
        f"▶️ /full received {ref}\nStarting full pipeline ({full_main_symbol}) at {stamp}.",
    )

    if sys.platform == "win32":
        # Prefer the batch file exactly as requested.
        cmd = ["cmd", "/c", "run_daily.bat"]
    else:
        # Cross-platform fallback (macOS/Linux): run the equivalent CLI directly.
        cmd = [
            sys.executable,
            "-m",
            "automation_tool.cli",
            "all",
            "--main-symbol",
            full_main_symbol,
        ]

    try:
        mp = _spawn_managed_process(name="full", cmd=cmd, cwd=root)
        out, _ = mp.popen.communicate()
        code = int(mp.popen.returncode or 0)
        if code == 0:
            _send_status(settings, reply_chat_id, "✅ /full finished successfully (exit code 0).")
        else:
            tail = (out or "").strip()
            if len(tail) > 1500:
                tail = tail[-1500:]
            msg = f"❌ /full failed (exit code {code})."
            if tail:
                msg += "\n\nLast output:\n" + tail
            _send_status(settings, reply_chat_id, msg)
    except Exception as e:
        _send_status(settings, reply_chat_id, f"❌ /full crashed: {e!r}")
    finally:
        with _PROC_LOCK:
            _PROCS[:] = [p for p in _PROCS if p.popen.poll() is None]


def _run_update_pipeline_in_thread(
    *,
    settings: Settings,
    reply_chat_id: str,
    update_main_symbol: str,
    trigger_message_id: Optional[int],
) -> None:
    root = _project_root()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    ref = f"(msg_id={trigger_message_id})" if trigger_message_id else ""
    _send_status(
        settings,
        reply_chat_id,
        f"▶️ /update received {ref}\nStarting update pipeline ({update_main_symbol}) at {stamp}.",
    )

    if sys.platform == "win32":
        cmd = ["cmd", "/c", "run_update.bat"]
    else:
        cmd = [
            sys.executable,
            "-m",
            "automation_tool.cli",
            "update",
            "--main-symbol",
            update_main_symbol,
        ]

    try:
        mp = _spawn_managed_process(name="update", cmd=cmd, cwd=root)
        out, _ = mp.popen.communicate()
        code = int(mp.popen.returncode or 0)
        if code == 0:
            _send_status(settings, reply_chat_id, "✅ /update finished successfully (exit code 0).")
        else:
            tail = (out or "").strip()
            if len(tail) > 1500:
                tail = tail[-1500:]
            msg = f"❌ /update failed (exit code {code})."
            if tail:
                msg += "\n\nLast output:\n" + tail
            _send_status(settings, reply_chat_id, msg)
    except Exception as e:
        _send_status(settings, reply_chat_id, f"❌ /update crashed: {e!r}")
    finally:
        with _PROC_LOCK:
            _PROCS[:] = [p for p in _PROCS if p.popen.poll() is None]


def _run_analyze_many_pipeline_in_thread(
    *,
    settings: Settings,
    reply_chat_id: str,
    symbols_csv: str,
    trigger_message_id: Optional[int],
) -> None:
    """
    Runs analyze-many asynchronously and posts start/finish messages.
    """
    root = _project_root()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    ref = f"(msg_id={trigger_message_id})" if trigger_message_id else ""
    _send_status(
        settings,
        reply_chat_id,
        f"▶️ /analyze-many received {ref}\nStarting analyze-many ({symbols_csv}) at {stamp}.",
    )

    # Cross-platform: run the CLI module. (On Windows, this assumes the bot runs inside the venv.)
    cmd = [
        sys.executable,
        "-m",
        "automation_tool.cli",
        "analyze-many",
        "--symbols",
        symbols_csv,
        "--parallel",
        "2",
    ]

    try:
        mp = _spawn_managed_process(name="analyze-many", cmd=cmd, cwd=root)
        out, _ = mp.popen.communicate()
        code = int(mp.popen.returncode or 0)
        if code == 0:
            _send_status(settings, reply_chat_id, "✅ /analyze-many finished successfully (exit code 0).")
        else:
            tail = (out or "").strip()
            if len(tail) > 1500:
                tail = tail[-1500:]
            msg = f"❌ /analyze-many failed (exit code {code})."
            if tail:
                msg += "\n\nLast output:\n" + tail
            _send_status(settings, reply_chat_id, msg)
    except Exception as e:
        _send_status(settings, reply_chat_id, f"❌ /analyze-many crashed: {e!r}")
    finally:
        with _PROC_LOCK:
            _PROCS[:] = [p for p in _PROCS if p.popen.poll() is None]

def _browser_command_for_action(action: str) -> list[str]:
    root = _project_root()
    if sys.platform == "win32":
        script = "browser_up.bat" if action == "up" else "browser_down.bat"
        return ["cmd", "/c", str(root / script)]
    return [
        sys.executable,
        "-m",
        "automation_tool.cli",
        "browser",
        action,
    ]


def _run_browser_command_in_thread(
    *,
    settings: Settings,
    reply_chat_id: str,
    action: str,
    trigger_message_id: Optional[int],
) -> None:
    root = _project_root()
    cmd_label = f"/browser-{action}"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    ref = f"(msg_id={trigger_message_id})" if trigger_message_id else ""
    _send_status(
        settings,
        reply_chat_id,
        f"▶️ {cmd_label} received {ref}\nRunning browser {action} at {stamp}.",
    )

    try:
        mp = _spawn_managed_process(
            name=f"browser-{action}",
            cmd=_browser_command_for_action(action),
            cwd=root,
        )
        out, _ = mp.popen.communicate()
        code = int(mp.popen.returncode or 0)
        tail = (out or "").strip()
        if len(tail) > 1500:
            tail = tail[-1500:]

        if code == 0:
            msg = f"✅ {cmd_label} finished successfully (exit code 0)."
        else:
            msg = f"❌ {cmd_label} failed (exit code {code})."
        if tail:
            msg += "\n\nOutput:\n" + tail
        _send_status(settings, reply_chat_id, msg)
    except Exception as e:
        _send_status(settings, reply_chat_id, f"❌ {cmd_label} crashed: {e!r}")
    finally:
        with _PROC_LOCK:
            _PROCS[:] = [p for p in _PROCS if p.popen.poll() is None]


def run_telegram_listener(
    *,
    settings: Settings,
    params: TelegramListenParams,
) -> None:
    token = _normalize_chat_id(settings.telegram_bot_token)
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required for telegram-listen.")

    listen_chat_id = _normalize_chat_id(settings.telegram_listen_chat_id) or _normalize_chat_id(
        settings.telegram_chat_id
    )
    if not listen_chat_id:
        raise SystemExit("TELEGRAM_CHAT_ID (or TELEGRAM_LISTEN_CHAT_ID) is required for telegram-listen.")

    base = f"https://api.telegram.org/bot{token}/getUpdates"
    offset: Optional[int] = None

    allowed_updates = json.dumps(["message", "channel_post"])

    _log.info("telegram-listen: listening on chat_id=%s", listen_chat_id)
    with httpx.Client(timeout=float(params.long_poll_timeout_seconds) + 10.0) as client:
        while True:
            try:
                q: dict[str, Any] = {
                    "timeout": int(params.long_poll_timeout_seconds),
                    "allowed_updates": allowed_updates,
                }
                if offset is not None:
                    q["offset"] = offset
                r = client.get(base, params=q)
                r.raise_for_status()
                payload = r.json()
                if not payload.get("ok"):
                    _log.warning("Telegram getUpdates returned ok=false: %s", payload)
                    time.sleep(2.0)
                    continue

                updates = payload.get("result")
                if not isinstance(updates, list):
                    time.sleep(params.poll_interval_seconds)
                    continue

                for upd in updates:
                    if not isinstance(upd, dict):
                        continue
                    uid = upd.get("update_id")
                    if isinstance(uid, int):
                        offset = uid + 1

                    env, text = _extract_text(upd)
                    if env is None or not text:
                        continue
                    chat_id = _chat_id_from_envelope(env)
                    if not chat_id or chat_id != listen_chat_id:
                        continue

                    cmd, args_text = _parse_command(text)
                    if cmd == "stop":
                        _stop_all_processes(settings, listen_chat_id)
                    elif cmd == "ask":
                        _handle_telegram_ask_followup(
                            settings=settings,
                            listen_chat_id=listen_chat_id,
                            reply_to_message_id=_message_id_from_envelope(env),
                            message_thread_id=_message_thread_id_from_envelope(env),
                            message_env=env,
                            args_text=args_text,
                            model=_explain_followup_model(
                                resolved_openai_model(settings, params.openai_model)
                            ),
                            cmd_label="/ask",
                        )
                    elif cmd == "ask-high":
                        _handle_telegram_ask_followup(
                            settings=settings,
                            listen_chat_id=listen_chat_id,
                            reply_to_message_id=_message_id_from_envelope(env),
                            message_thread_id=_message_thread_id_from_envelope(env),
                            message_env=env,
                            args_text=args_text,
                            model=_ASK_HIGH_FOLLOWUP_MODEL,
                            cmd_label="/ask-high",
                        )
                    elif cmd == "update":
                        mid = _message_id_from_envelope(env)
                        thread_id = _message_thread_id_from_envelope(env)
                        t = threading.Thread(
                            target=_run_update_pipeline_in_thread,
                            kwargs={
                                "settings": settings,
                                "reply_chat_id": listen_chat_id,
                                "update_main_symbol": (params.update_main_symbol or "XAUUSD")
                                .strip()
                                .upper(),
                                "trigger_message_id": mid,
                            },
                            daemon=True,
                            name="telegram-update-runner",
                        )
                        t.start()
                    elif cmd == "full":
                        mid = _message_id_from_envelope(env)
                        thread_id = _message_thread_id_from_envelope(env)
                        t = threading.Thread(
                            target=_run_full_pipeline_in_thread,
                            kwargs={
                                "settings": settings,
                                "reply_chat_id": listen_chat_id,
                                "full_main_symbol": (params.full_main_symbol or "XAUUSD").strip().upper(),
                                "trigger_message_id": mid,
                            },
                            daemon=True,
                            name="telegram-full-runner",
                        )
                        t.start()
                    elif cmd == "analyze-many":
                        mid = _message_id_from_envelope(env)
                        thread_id = _message_thread_id_from_envelope(env)
                        symbols_csv = _parse_symbols_from_args_text(args_text) or "EURUSD,USDJPY"
                        t = threading.Thread(
                            target=_run_analyze_many_pipeline_in_thread,
                            kwargs={
                                "settings": settings,
                                "reply_chat_id": listen_chat_id,
                                "symbols_csv": symbols_csv,
                                "trigger_message_id": mid,
                            },
                            daemon=True,
                            name="telegram-analyze-many-runner",
                        )
                        t.start()
                    elif cmd in ("browser-up", "browser-down"):
                        mid = _message_id_from_envelope(env)
                        action = "up" if cmd == "browser-up" else "down"
                        t = threading.Thread(
                            target=_run_browser_command_in_thread,
                            kwargs={
                                "settings": settings,
                                "reply_chat_id": listen_chat_id,
                                "action": action,
                                "trigger_message_id": mid,
                            },
                            daemon=True,
                            name=f"telegram-browser-{action}-runner",
                        )
                        t.start()
                    elif cmd == "loai":
                        zid = (args_text or "").strip()
                        main_sym = (params.update_main_symbol or "XAUUSD").strip().upper()
                        ok, msg = mark_zone_status_loai_by_id(zid, symbol=main_sym)
                        prefix = "✅" if ok else "❌"
                        _send_status(settings, listen_chat_id, f"{prefix} /loai\n{msg}")

            except httpx.HTTPError as e:
                _log.warning("telegram-listen: HTTP error: %s", e)
                time.sleep(2.0)
            except Exception as e:
                _log.exception("telegram-listen: unexpected error: %s", e)
                time.sleep(2.0)
