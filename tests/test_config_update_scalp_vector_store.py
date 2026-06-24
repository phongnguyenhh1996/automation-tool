from __future__ import annotations

import os

import pytest

from automation_tool.config import (
    Settings,
    _parse_update_scalp_vector_store_ids,
    load_settings,
    resolve_update_scalp_vector_store_ids,
)


def test_parse_update_scalp_vector_store_ids_from_plural_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_UPDATE_SCALP_VECTOR_STORE_IDS", "vs_a, vs_b")
    monkeypatch.delenv("OPENAI_UPDATE_SCALP_VECTOR_STORE_ID", raising=False)
    assert _parse_update_scalp_vector_store_ids() == ["vs_a", "vs_b"]


def test_parse_update_scalp_vector_store_ids_singular_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_UPDATE_SCALP_VECTOR_STORE_IDS", raising=False)
    monkeypatch.setenv("OPENAI_UPDATE_SCALP_VECTOR_STORE_ID", "vs_only")
    assert _parse_update_scalp_vector_store_ids() == ["vs_only"]


def test_resolve_update_scalp_vector_store_ids_prefers_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_UPDATE_SCALP_VECTOR_STORE_IDS", raising=False)
    monkeypatch.delenv("OPENAI_UPDATE_SCALP_VECTOR_STORE_ID", raising=False)
    s = Settings(
        coinmap_email=None,
        coinmap_password=None,
        gocharting_email=None,
        gocharting_password=None,
        tradingview_password=None,
        openai_api_key="k",
        openai_model="gpt-5.2",
        openai_vector_store_ids=[],
        openai_update_scalp_vector_store_ids=["vs_from_settings"],
        ocr_space_api_key=None,
        openai_responses_store=True,
        openai_responses_include=[],
        telegram_bot_token="",
        telegram_chat_id="",
        telegram_listen_chat_id=None,
        telegram_output_ngan_gon_chat_id=None,
        telegram_analysis_detail_chat_id=None,
        telegram_log_chat_id=None,
        telegram_python_bot_chat_id=None,
        telegram_parse_mode=None,
        coinmap_base_url="https://coinmap.tech",
    )
    assert resolve_update_scalp_vector_store_ids(s, fallback=["vs_fallback"]) == ["vs_from_settings"]


def test_resolve_update_scalp_vector_store_ids_uses_fallback_when_empty() -> None:
    s = Settings(
        coinmap_email=None,
        coinmap_password=None,
        gocharting_email=None,
        gocharting_password=None,
        tradingview_password=None,
        openai_api_key="k",
        openai_model="gpt-5.2",
        openai_vector_store_ids=[],
        openai_update_scalp_vector_store_ids=[],
        ocr_space_api_key=None,
        openai_responses_store=True,
        openai_responses_include=[],
        telegram_bot_token="",
        telegram_chat_id="",
        telegram_listen_chat_id=None,
        telegram_output_ngan_gon_chat_id=None,
        telegram_analysis_detail_chat_id=None,
        telegram_log_chat_id=None,
        telegram_python_bot_chat_id=None,
        telegram_parse_mode=None,
        coinmap_base_url="https://coinmap.tech",
    )
    assert resolve_update_scalp_vector_store_ids(s, fallback=["vs_all2"]) == ["vs_all2"]


def test_load_settings_reads_update_scalp_vector_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_UPDATE_SCALP_VECTOR_STORE_IDS", "vs_env")
    s = load_settings()
    assert s.openai_update_scalp_vector_store_ids == ["vs_env"]
