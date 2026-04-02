"""Console + optional Telegram log channel for automation_tool."""

from __future__ import annotations

import logging
import sys

from automation_tool.config import Settings
from automation_tool.telegram_bot import send_message


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
            self.handleError(record)


def setup_automation_logging(settings: Settings) -> logging.Logger:
    """
    Cấu hình logger ``automation_tool`` (INFO): stderr + tùy chọn Telegram.

    Idempotent: nếu đã có handler thì không thêm lần nữa.
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
    elif cid and not tok:
        print(
            "Warning: TELEGRAM_LOG_CHAT_ID set but TELEGRAM_BOT_TOKEN empty — log channel disabled.",
            file=sys.stderr,
        )

    return log


def get_cli_logger() -> logging.Logger:
    return logging.getLogger("automation_tool.cli")
