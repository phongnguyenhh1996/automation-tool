from __future__ import annotations

from types import SimpleNamespace

import pytest

import automation_tool.tv_watchlist_daemon as daemon
from automation_tool.tv_watchlist_daemon import _touch_prompt
from automation_tool.zones_state import Zone, ZonesState


def test_touch_prompt_includes_trigger_price_and_anti_chase_rule() -> None:
    zone = Zone(
        id="z-sell",
        label="plan_phu",
        vung_cho="4549.5–4552.5",
        side="SELL",
        trade_line="SELL LIMIT 4551.0 | SL 4560.0 | TP1 4538.0 | Lot 0.01",
    )

    prompt = _touch_prompt(zone=zone, last_price=4551.76)

    assert "Giá trigger realtime khi chạm vùng: 4551.76." in prompt
    assert 'intraday_hanh_dong="chờ"' in prompt


def test_tp1_followup_loai_and_cancels_pending_when_tp1_touched_without_position(
    monkeypatch, tmp_path
) -> None:
    state_path = tmp_path / "zones_state.json"
    zone = Zone(
        id="z-buy",
        label="plan_chinh",
        vung_cho="4500–4502",
        side="BUY",
        trade_line="BUY LIMIT 4501.0 | SL 4490.0 | TP1 4512.0 | Lot 0.01",
        mt5_ticket=12345,
        status="dang_thuc_thi",
        tp1_followup_done=True,
        has_position=False,
    )
    params = daemon.WatchlistDaemonParams(
        coinmap_tv_yaml=tmp_path / "coinmap_tv.yaml",
        capture_coinmap_yaml=tmp_path / "cap.yaml",
        charts_dir=tmp_path / "charts",
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        zones_state_path=state_path,
    )
    daemon._state_write(params, ZonesState(symbol="XAUUSD", zones=[zone]))

    canceled: list[int] = []
    monkeypatch.setattr(daemon, "_send_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(daemon, "_send_user_notice", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(daemon, "load_mt5_accounts_for_cli", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        daemon,
        "mt5_ticket_still_open",
        lambda *_args, **_kwargs: (True, "still pending"),
    )
    monkeypatch.setattr(
        daemon,
        "mt5_cancel_pending_order",
        lambda ticket, **_kwargs: canceled.append(ticket)
        or SimpleNamespace(ok=True, message="cancelled pending"),
    )
    monkeypatch.setattr(
        daemon,
        "run_single_followup_responses",
        lambda **_kwargs: pytest.fail("OpenAI follow-up should not be called"),
    )

    daemon._tp1_followup_job(
        settings=SimpleNamespace(),
        params=params,
        zone_id="z-buy",
        p_last=4512.0,
    )

    updated = daemon._state_read(params)
    assert updated is not None
    assert canceled == [12345]
    assert updated.zones[0].status == "loai"
    assert updated.zones[0].mt5_ticket is None
    assert updated.zones[0].mt5_tickets_by_account is None
    assert updated.zones[0].tp1_followup_done is True
