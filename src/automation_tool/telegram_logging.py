"""Console + optional Telegram log channel for automation_tool."""

from __future__ import annotations

import logging
import sys
import threading

from automation_tool.config import Settings
from automation_tool.telegram_bot import send_message

_exception_hooks_installed = False


def _install_exception_hooks(log: logging.Logger) -> None:
    """
    Đưa exception không bắt được (main thread + thread phụ) vào cùng logger
    → stderr + TELEGRAM_LOG_CHAT_ID (nếu đã gắn TelegramLogHandler).
    """
    global _exception_hooks_installed
    if _exception_hooks_installed:
        return
    _exception_hooks_installed = True

    prev_sys = sys.excepthook

    def _sys_excepthook(
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_tb: object | None,
    ) -> None:
        try:
            if exc_type is not None and exc_value is not None:
                log.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
        except Exception:
            pass
        prev_sys(exc_type, exc_value, exc_tb)

    sys.excepthook = _sys_excepthook

    if hasattr(threading, "excepthook"):
        prev_thread = threading.excepthook

        def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
            try:
                log.critical(
                    "Uncaught exception in thread %r",
                    args.thread.name,
                    exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
                )
            except Exception:
                pass
            prev_thread(args)

        threading.excepthook = _thread_excepthook


class TelegramLogHandler(logging.Handler):
    """Gửi từng bản ghi log tới một chat/channel Telegram (plain text, đã chunk)."""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        super().__init__()
        self._bot_token = bot_token
        self._chat_id = chat_id

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            send_message(
                bot_token=self._bot_token,
                chat_id=self._chat_id,
                text=msg,
                parse_mode=None,
            )
        except Exception:
            # Do not call handleError(): logging treats that as a logging-system failure
            # and prints "--- Logging error ---" plus a full traceback. Transient Telegram
            # / network issues (e.g. httpx.RemoteProtocolError) are expected; stderr still
            # receives the line from the StreamHandler on the same logger.
            pass


def setup_automation_logging(settings: Settings) -> logging.Logger:
    """
    Cấu hình logger ``automation_tool`` (INFO): stderr + tùy chọn Telegram.

    Khi có ``TELEGRAM_LOG_CHAT_ID`` + token: ngoài log INFO…, mọi ``logging.error``
    / ``warning`` / ``critical`` và **exception không bắt được** (``sys.excepthook``,
    ``threading.excepthook``) cũng được gửi lên cùng chat log.

    Idempotent: nếu đã có handler thì không thêm lần nữa.

    Mọi logger con ``automation_tool.*`` (vd. ``automation_tool.tp1``, ``automation_tool.tp1_followup``,
    ``automation_tool.journal``) propagate lên ``automation_tool`` → cùng stderr + Telegram log.
    Luồng Coinmap Bearer (``[coinmap bearer]``) dùng ``logging.getLogger("automation_tool").info(...)``
    trong ``coinmap.py`` — cũng tới kênh log này khi đã gọi ``setup_automation_logging`` (CLI và
    ``capture_worker`` / ``capture_many_worker``).
    """
    log = logging.getLogger("automation_tool")
    if log.handlers:
        return log

    log.setLevel(logging.INFO)
    log.propagate = False
    fmt = logging.Formatter("%(message)s")
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    log.addHandler(sh)

    cid = (settings.telegram_log_chat_id or "").strip()
    tok = (settings.telegram_bot_token or "").strip()
    if cid and tok:
        th = TelegramLogHandler(tok, cid)
        th.setFormatter(fmt)
        log.addHandler(th)
        _install_exception_hooks(log)
    elif cid and not tok:
        print(
            "Warning: TELEGRAM_LOG_CHAT_ID set but TELEGRAM_BOT_TOKEN empty — log channel disabled.",
            file=sys.stderr,
        )

    return log


def get_cli_logger() -> logging.Logger:
    return logging.getLogger("automation_tool.cli")
