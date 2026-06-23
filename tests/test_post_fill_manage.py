"""Tests for post-fill [TRADE_MANAGEMENT] (plan chính / plan phụ)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from automation_tool.tp1_followup import extract_trade_management_reason
from automation_tool.tv_watchlist_daemon import (
    WatchlistDaemonParams,
    _post_fill_manage_job,
)
from automation_tool.zones_state import Zone, ZonesState, write_zones_state_to_shard


def test_extract_trade_management_reason_reads_reason_only() -> None:
    text = '```json\n{"reason": "Giữ lệnh — footprint ủng hộ.", "out_chi_tiet": "x"}\n```'
    assert extract_trade_management_reason(text) == "Giữ lệnh — footprint ủng hộ."


def test_extract_trade_management_reason_without_action() -> None:
    assert extract_trade_management_reason("not json") is None


def test_post_fill_manage_job_sends_telegram_no_mt5(monkeypatch, tmp_path: Path) -> None:
    shard = tmp_path / "vung_plan_chinh_sang.json"
    zone = Zone(
        id="plan_chinh__sang",
        label="plan_chinh",
        vung_cho="100–101",
        side="BUY",
        status="cho_tp1",
        trade_line="BUY LIMIT 100 | SL 99 | TP1 101 | Lot 0.01",
        mt5_ticket=12345,
        has_position=True,
    )
    write_zones_state_to_shard(shard, ZonesState(symbol="XAUUSD", zones=[zone]))

    detail_png = tmp_path / "charts" / "20260101_120000_gocharting_GC_5m_detail_zoom.png"
    detail_png.parent.mkdir(parents=True, exist_ok=True)
    detail_png.write_bytes(b"png")

    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon._post_fill_prev_response_id",
        lambda _params: "resp-full-analysis",
    )
    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon._parse_trade_from_zone_trade_line",
        lambda tl, symbol_override=None: (MagicMock(side="BUY", price=100.0), None),
    )
    monkeypatch.setattr(
        "automation_tool.gocharting_capture.capture_gocharting",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "automation_tool.gocharting_capture.gocharting_detail_png_path",
        lambda *a, **k: detail_png,
    )
    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon.mt5_ticket_current_sltp",
        lambda *a, **k: (99.0, 101.0, "ok"),
    )
    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon._openai_followup_persist_new_id",
        lambda *a, **k: None,
    )
    monkeypatch.setattr("automation_tool.tv_watchlist_daemon._send_log", lambda *a, **k: None)
    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon._send_user_notice", lambda *a, **k: None
    )

    mt5_calls: list[str] = []

    def _block_mt5(name):
        def _inner(*a, **k):
            mt5_calls.append(name)
            raise AssertionError(f"MT5 must not be called: {name}")

        return _inner

    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon.mt5_cancel_pending_or_close_position",
        _block_mt5("cancel"),
    )
    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon.mt5_chinh_trade_line_inplace",
        _block_mt5("chinh"),
    )

    openai_kwargs: dict = {}

    def _capture_openai(**kwargs):
        openai_kwargs.update(kwargs)
        return (
            '{"reason": "Khuyến nghị giữ lệnh.", "hanh_dong_quan_ly_lenh": "giu_nguyen"}',
            "resp-new",
        )

    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon.run_single_followup_responses",
        _capture_openai,
    )

    tg_calls: list[dict] = []

    def _fake_tg(**kwargs):
        tg_calls.append(kwargs)

    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon.send_trade_management_reason_notice",
        _fake_tg,
    )

    settings = MagicMock(
        openai_api_key="sk-test",
        openai_vector_store_ids=[],
        openai_responses_store=False,
        openai_responses_include=[],
        gocharting_email="gc@test.com",
        gocharting_password="pw",
        telegram_bot_token="tok",
        telegram_python_bot_chat_id="-1001",
    )
    params = WatchlistDaemonParams(
        coinmap_tv_yaml=tmp_path / "coinmap_tv.yaml",
        capture_coinmap_yaml=tmp_path / "cap.yaml",
        charts_dir=tmp_path / "charts",
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        shard_path=shard,
        no_telegram=False,
    )

    _post_fill_manage_job(settings=settings, params=params, zone_id=zone.id)

    assert openai_kwargs.get("previous_response_id") == "resp-full-analysis"
    assert openai_kwargs.get("coinmap_json_paths") == []
    user_text = openai_kwargs.get("user_text", "")
    assert "GC1!" in user_text
    assert "không phải spot XAUUSD" in user_text
    assert "#8FAF8E" in user_text
    assert mt5_calls == []
    assert len(tg_calls) == 1
    assert tg_calls[0]["reason"] == "Khuyến nghị giữ lệnh."
    assert tg_calls[0]["action"] == "khuyen_nghi"
    assert tg_calls[0].get("trade_line") is None


def test_post_fill_manage_job_aborts_without_full_analysis_anchor(
    monkeypatch, tmp_path: Path
) -> None:
    shard = tmp_path / "vung_plan_phu_sang.json"
    zone = Zone(
        id="plan_phu__sang",
        label="plan_phu",
        vung_cho="100–101",
        side="BUY",
        status="cho_tp1",
        trade_line="BUY LIMIT 100 | SL 99 | TP1 101 | Lot 0.01",
        mt5_ticket=99,
    )
    write_zones_state_to_shard(shard, ZonesState(symbol="XAUUSD", zones=[zone]))

    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon._post_fill_prev_response_id",
        lambda _params: "",
    )
    openai_called = False

    def _openai(**kwargs):
        nonlocal openai_called
        openai_called = True
        return ("", "")

    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon.run_single_followup_responses",
        _openai,
    )
    monkeypatch.setattr("automation_tool.tv_watchlist_daemon._send_log", lambda *a, **k: None)
    notices: list[tuple] = []

    monkeypatch.setattr(
        "automation_tool.tv_watchlist_daemon._send_user_notice",
        lambda *a, **k: notices.append(a),
    )

    settings = MagicMock()
    params = WatchlistDaemonParams(
        coinmap_tv_yaml=tmp_path / "coinmap_tv.yaml",
        capture_coinmap_yaml=tmp_path / "cap.yaml",
        charts_dir=tmp_path / "charts",
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        shard_path=shard,
    )

    _post_fill_manage_job(settings=settings, params=params, zone_id=zone.id)

    assert openai_called is False
    assert notices
    assert "FULL_ANALYSIS" in notices[0][1]
