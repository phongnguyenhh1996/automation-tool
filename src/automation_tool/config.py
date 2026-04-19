from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

# Thư mục chứa package (editable: trùng repo; wheel: site-packages/...).
_ROOT = Path(__file__).resolve().parents[2]


def load_all_dotenv() -> None:
    """
    1) .env cạnh mã nguồn (pip install -e).
    2) .env ở thư mục làm việc hiện tại — **ghi đè** (1); cần khi chạy từ thư mục project
    nhưng package cài non-editable (_ROOT không có .env).
    """
    load_dotenv(_ROOT / ".env")
    load_dotenv(Path.cwd() / ".env", override=True)


load_all_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _parse_include() -> List[str]:
    raw = os.getenv("OPENAI_RESPONSES_INCLUDE")
    if raw is None or not raw.strip():
        return [
            "reasoning.encrypted_content",
            "web_search_call.action.sources",
        ]
    return [p.strip() for p in raw.split(",") if p.strip()]


@dataclass
class Settings:
    coinmap_email: Optional[str]
    coinmap_password: Optional[str]
    tradingview_password: Optional[str]
    openai_api_key: str
    openai_prompt_id: str
    openai_prompt_version: Optional[str]
    # Optional model id for Responses API (overrides model saved on the dashboard prompt).
    openai_model: Optional[str]
    # Optional override for [INTRADAY_ALERT] flows only; empty → built-in default (gpt-5.4-mini).
    openai_model_intraday_alert: Optional[str]
    openai_vector_store_ids: list[str]
    openai_responses_store: bool
    openai_responses_include: List[str]
    telegram_bot_token: str
    telegram_chat_id: str
    # Optional: chat/channel/supergroup to listen for inbound commands (e.g. /full).
    # Defaults to telegram_chat_id when unset.
    telegram_listen_chat_id: Optional[str]
    # Optional second chat: [OUTPUT_NGAN_GON] (dual markers). MT5: thành công → telegram_chat_id;
    # thất bại / từ chối → telegram_python_bot_chat_id (tin user-friendly).
    telegram_output_ngan_gon_chat_id: Optional[str]
    # Optional: [OUTPUT_CHI_TIET] / JSON out_chi_tiet → this channel (analyze / dual-send).
    telegram_analysis_detail_chat_id: Optional[str]
    # Optional: nhận bản sao log bước chạy (INFO) — cùng bot, chat/channel khác (vd. supergroup -100…).
    telegram_log_chat_id: Optional[str]
    # Optional: tin ngắn tiếng Việt cho người không đọc log kỹ thuật (milestone quan trọng).
    telegram_python_bot_chat_id: Optional[str]
    # Telegram sendMessage parse_mode: None = plain text. Use Markdown, MarkdownV2, or HTML for formatting.
    telegram_parse_mode: Optional[str]
    coinmap_base_url: str


def _root() -> Path:
    return _ROOT


def default_data_dir() -> Path:
    return _root() / "data"


def symbol_data_dir(symbol: Optional[str] = None) -> Path:
    """
    Per-instrument data root: ``data/{{SYMBOL}}/`` (e.g. ``data/XAUUSD``, ``data/USDJPY``).
    Symbol comes from ``symbol`` or :func:`automation_tool.images.get_active_main_symbol`.
    """
    from automation_tool.images import get_active_main_symbol

    sym = (symbol or get_active_main_symbol()).strip().upper()
    return default_data_dir() / sym


def default_charts_dir() -> Path:
    """``data/{{active_symbol}}/charts/`` — see ``get_active_main_symbol`` in ``images.py``."""
    return symbol_data_dir() / "charts"


def default_logs_dir() -> Path:
    """Project ``logs/`` — capture failures, batch scripts, etc."""
    return _root() / "logs"


def default_coinmap_data_json_path() -> Path:
    """Single combined file is no longer written; JSON exports live next to PNGs in charts_dir."""
    return default_data_dir() / "coinmap_data.json"


def default_coinmap_config_path() -> Path:
    return _root() / "config" / "coinmap.yaml"


def default_coinmap_update_config_path() -> Path:
    return _root() / "config" / "coinmap_update.yaml"


def default_storage_state_path() -> Path:
    """
    Một file Playwright session dùng chung cho mọi symbol (Coinmap / TradingView),
    không theo ``data/{{SYM}}/``. Ghi đè bằng ``--storage-state`` nếu cần.
    """
    return default_data_dir() / "storage_state.json"


def default_coinmap_bearer_cache_path() -> Path:
    """Cached ``Authorization: Bearer …`` for Coinmap ``bearer_request`` API-only capture."""
    return default_data_dir() / "coinmap_bearer_authorization.txt"


def _parse_vector_store_ids() -> list[str]:
    raw = os.getenv("OPENAI_VECTOR_STORE_IDS") or os.getenv("OPENAI_VECTOR_STORE_ID") or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


def _parse_telegram_parse_mode() -> Optional[str]:
    raw = (os.getenv("TELEGRAM_PARSE_MODE") or "").strip()
    if not raw:
        return None
    allowed = {"HTML", "Markdown", "MarkdownV2"}
    if raw not in allowed:
        raise SystemExit(
            f"TELEGRAM_PARSE_MODE must be one of {sorted(allowed)} or empty; got {raw!r}."
        )
    return raw


def load_settings() -> Settings:
    ver = (os.getenv("OPENAI_PROMPT_VERSION") or "").strip()
    return Settings(
        coinmap_email=os.getenv("COINMAP_EMAIL") or None,
        coinmap_password=os.getenv("COINMAP_PASSWORD") or None,
        tradingview_password=os.getenv("TRADINGVIEW_PASSWORD") or None,
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_prompt_id=(os.getenv("OPENAI_PROMPT_ID") or "").strip(),
        openai_prompt_version=ver if ver else None,
        openai_model=((os.getenv("OPENAI_MODEL") or "").strip() or None),
        openai_model_intraday_alert=((os.getenv("OPENAI_MODEL_INTRADAY_ALERT") or "").strip() or None),
        openai_vector_store_ids=_parse_vector_store_ids(),
        openai_responses_store=_env_bool("OPENAI_RESPONSES_STORE", True),
        openai_responses_include=_parse_include(),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        telegram_listen_chat_id=((os.getenv("TELEGRAM_LISTEN_CHAT_ID") or "").strip() or None),
        telegram_output_ngan_gon_chat_id=(
            (os.getenv("TELEGRAM_OUTPUT_NGAN_GON_CHAT_ID") or "").strip() or None
        ),
        telegram_analysis_detail_chat_id=(
            (os.getenv("TELEGRAM_ANALYSIS_DETAIL_CHAT_ID") or "").strip() or None
        ),
        telegram_log_chat_id=((os.getenv("TELEGRAM_LOG_CHAT_ID") or "").strip() or None),
        telegram_python_bot_chat_id=(
            (os.getenv("TELEGRAM_PYTHON_BOT_CHAT_ID") or "").strip() or None
        ),
        telegram_parse_mode=_parse_telegram_parse_mode(),
        coinmap_base_url=os.getenv("COINMAP_BASE_URL", "https://coinmap.tech"),
    )


DEFAULT_INTRADAY_ALERT_MODEL = "gpt-5.4-mini"


def resolved_openai_model(settings: Settings, override: Optional[str] = None) -> Optional[str]:
    """
    Model id for ``responses.create``: ``override`` (e.g. CLI ``--model``) wins, else ``OPENAI_MODEL``.
    Returns None to let the API use the model configured on the stored prompt.
    """
    o = (override or "").strip()
    if o:
        return o
    m = (settings.openai_model or "").strip()
    return m or None


def resolved_model_for_intraday_alert(
    settings: Settings,
    cli_model_only: Optional[str] = None,
) -> str:
    """
    Model for ``[INTRADAY_ALERT]`` (journal touch, watchlist touch, zone-touch) và
    ``[TRADE_MANAGEMENT]`` / TP1 follow-up (``tp1_followup``, daemon ``_tp1_followup_job``).

    Priority: CLI ``--model`` (passed as ``cli_model_only``) → ``OPENAI_MODEL_INTRADAY_ALERT``
    → :data:`DEFAULT_INTRADAY_ALERT_MODEL`.

    Does not use ``OPENAI_MODEL`` so đầu ngày / các luồng khác có thể khác model.
    """
    o = (cli_model_only or "").strip()
    if o:
        return o
    ia = (settings.openai_model_intraday_alert or "").strip()
    if ia:
        return ia
    return DEFAULT_INTRADAY_ALERT_MODEL


def require_openai(s: Settings) -> None:
    if not s.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is required.")
    if not s.openai_prompt_id:
        raise SystemExit("OPENAI_PROMPT_ID is required (dashboard prompt id, e.g. pmpt_...).")


def require_telegram(s: Settings) -> None:
    if not s.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required.")
    if not s.telegram_chat_id:
        raise SystemExit("TELEGRAM_CHAT_ID is required.")
