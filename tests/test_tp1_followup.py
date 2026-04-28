"""Tests for [TRADE_MANAGEMENT] follow-up handling."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from automation_tool.mt5_accounts import LotRuleFromTrade, MT5AccountEntry
from automation_tool.mt5_execute import MT5ExecutionResult
from automation_tool.mt5_manage import MT5ChinhTradeLineResult
from automation_tool.state_files import (
    CHO_TP1,
    LastAlertState,
    read_last_alert_state,
    write_last_alert_state,
)
from automation_tool.tp1_followup import _run_tp1_openai_and_act, parse_tp1_followup_decision


def test_parse_tp1_followup_decision_includes_reason() -> None:
    dec = parse_tp1_followup_decision(
        """```json
{
  "hanh_dong_quan_ly_lenh": "giu_nguyen",
  "trade_line_moi": "",
  "reason": "Giá giữ được VWAP và CVD chưa đảo chiều, nên tiếp tục giữ lệnh."
}
```"""
    )

    assert dec is not None
    assert dec.sau_tp1 == "giu_nguyen"
    assert dec.reason == "Giá giữ được VWAP và CVD chưa đảo chiều, nên tiếp tục giữ lệnh."


def test_chinh_trade_line_failure_does_not_create_new_order(monkeypatch, tmp_path) -> None:
    last_alert_path = tmp_path / "last_alert_prices.json"
    old_trade_line = "BUY LIMIT 100 | SL 99 | TP1 101 | Lot 0.01"
    write_last_alert_state(
        LastAlertState(
            prices=(100.0, 200.0, 300.0),
            status_by_label={"plan_chinh": CHO_TP1, "plan_phu": "loai", "scalp": "loai"},
            trade_line_by_label={"plan_chinh": old_trade_line},
            mt5_ticket_by_label={"plan_chinh": 111},
            mt5_tickets_by_label={"plan_chinh": {"main": 111}},
            tp1_followup_done_by_label={"plan_chinh": True},
        ),
        path=last_alert_path,
    )

    account = MT5AccountEntry(
        id="main",
        terminal_path="/tmp/metatrader64.exe",
        login=1,
        password="x",
        server="demo",
        primary=True,
        lot=LotRuleFromTrade(),
    )
    monkeypatch.setattr("automation_tool.tp1_followup.load_mt5_accounts_for_cli", lambda _path: [account])
    monkeypatch.setattr("automation_tool.tp1_followup.mt5_ticket_still_open", lambda *a, **k: (True, "open"))
    monkeypatch.setattr("automation_tool.tp1_followup.capture_charts", lambda **k: [Path(k["charts_dir"]) / "x.json"])
    monkeypatch.setattr("automation_tool.tp1_followup.coinmap_xauusd_5m_json_path", lambda _charts_dir: tmp_path / "x.json")
    monkeypatch.setattr("automation_tool.tp1_followup.read_main_chart_symbol", lambda _charts_dir: "XAUUSD")
    monkeypatch.setattr("automation_tool.tp1_followup.write_openai_coinmap_merged_from_raw_export", lambda p: p)
    (tmp_path / "x.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "automation_tool.tp1_followup.run_single_followup_responses",
        lambda **k: (
            '{"hanh_dong_quan_ly_lenh":"chinh_trade_line","trade_line_moi":"BUY LIMIT 100 | SL 100 | TP1 102 | Lot 0.01","reason":"Dời SL."}',
            "new-response-id",
        ),
    )
    close_calls: list[object] = []
    execute_calls: list[object] = []
    monkeypatch.setattr(
        "automation_tool.tp1_followup.mt5_chinh_trade_line_all_accounts",
        lambda *a, **k: SimpleNamespace(
            ok_all_inplace=False,
            results=[("main", MT5ChinhTradeLineResult(False, "broker reject", "modify_failed"))],
            all_ticket_missing=lambda: False,
        ),
    )
    monkeypatch.setattr(
        "automation_tool.tp1_followup.mt5_cancel_pending_or_close_all_accounts",
        lambda *a, **k: close_calls.append((a, k)),
    )

    def fake_execute_trade_all_accounts(*a, **k):
        execute_calls.append((a, k))
        return SimpleNamespace(
            ok_all=True,
            tickets_by_account_id={"main": 222},
            primary_ticket=lambda _accounts: 222,
        )

    monkeypatch.setattr(
        "automation_tool.tp1_followup.execute_trade_all_accounts",
        fake_execute_trade_all_accounts,
        raising=False,
    )
    monkeypatch.setattr(
        "automation_tool.tp1_followup.execute_trade",
        lambda *a, **k: MT5ExecutionResult(True, "created", order=333),
        raising=False,
    )

    settings = MagicMock()
    settings.coinmap_email = ""
    settings.coinmap_password = ""
    settings.tradingview_password = ""
    settings.telegram_bot_token = ""
    settings.telegram_parse_mode = None
    settings.openai_api_key = "key"
    settings.openai_prompt_id = "prompt"
    settings.openai_prompt_version = None
    settings.openai_vector_store_ids = []
    settings.openai_responses_store = True
    settings.openai_responses_include = None
    params = SimpleNamespace(
        capture_coinmap_yaml=tmp_path / "cap.yaml",
        charts_dir=tmp_path,
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        mt5_accounts_json=None,
        mt5_dry_run=False,
        mt5_execute=True,
        mt5_symbol="XAUUSD",
        no_telegram=True,
    )

    result_id = _run_tp1_openai_and_act(
        settings=settings,
        params=params,
        last_alert_path=last_alert_path,
        label="plan_chinh",
        trade_line=old_trade_line,
        p_last=101.0,
        parsed=MagicMock(side="BUY", tp1=101.0),
        page=MagicMock(),
        tv={},
        symbol="XAUUSD",
        settle_ms=0,
        browser_context=MagicMock(),
        prev_response_id="old-response-id",
    )

    st = read_last_alert_state(last_alert_path)
    assert result_id == "new-response-id"
    assert close_calls == []
    assert execute_calls == []
    assert st is not None
    assert st.mt5_ticket_by_label["plan_chinh"] == 111
    assert st.trade_line_by_label["plan_chinh"] == old_trade_line


def test_chinh_trade_line_success_keeps_status_and_sends_manage_action(monkeypatch, tmp_path) -> None:
    last_alert_path = tmp_path / "last_alert_prices.json"
    old_trade_line = "BUY LIMIT 100 | SL 99 | TP1 101 | Lot 0.01"
    new_trade_line = "BUY LIMIT 100 | SL 100 | TP1 102 | Lot 0.01"
    write_last_alert_state(
        LastAlertState(
            prices=(100.0, 200.0, 300.0),
            status_by_label={"plan_chinh": CHO_TP1, "plan_phu": "loai", "scalp": "loai"},
            trade_line_by_label={"plan_chinh": old_trade_line},
            mt5_ticket_by_label={"plan_chinh": 111},
            mt5_tickets_by_label={"plan_chinh": {"main": 111}},
            tp1_followup_done_by_label={"plan_chinh": True},
        ),
        path=last_alert_path,
    )
    account = MT5AccountEntry(
        id="main",
        terminal_path="/tmp/metatrader64.exe",
        login=1,
        password="x",
        server="demo",
        primary=True,
        lot=LotRuleFromTrade(),
    )
    monkeypatch.setattr("automation_tool.tp1_followup.load_mt5_accounts_for_cli", lambda _path: [account])
    monkeypatch.setattr("automation_tool.tp1_followup.mt5_ticket_still_open", lambda *a, **k: (True, "open"))
    monkeypatch.setattr("automation_tool.tp1_followup.capture_charts", lambda **k: [Path(k["charts_dir"]) / "x.json"])
    monkeypatch.setattr("automation_tool.tp1_followup.coinmap_xauusd_5m_json_path", lambda _charts_dir: tmp_path / "x.json")
    monkeypatch.setattr("automation_tool.tp1_followup.read_main_chart_symbol", lambda _charts_dir: "XAUUSD")
    monkeypatch.setattr("automation_tool.tp1_followup.write_openai_coinmap_merged_from_raw_export", lambda p: p)
    (tmp_path / "x.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "automation_tool.tp1_followup.run_single_followup_responses",
        lambda **k: (
            f'{{"hanh_dong_quan_ly_lenh":"chinh_trade_line","trade_line_moi":"{new_trade_line}","reason":"Dời SL."}}',
            "new-response-id",
        ),
    )
    monkeypatch.setattr(
        "automation_tool.tp1_followup.mt5_chinh_trade_line_all_accounts",
        lambda *a, **k: SimpleNamespace(
            ok_all_inplace=True,
            results=[("main", MT5ChinhTradeLineResult(True, "modified", "modified_sltp"))],
            all_ticket_missing=lambda: False,
        ),
    )
    sent_actions: list[str | None] = []
    monkeypatch.setattr(
        "automation_tool.tp1_followup.send_mt5_execution_log_to_ngan_gon_chat",
        lambda **kwargs: sent_actions.append(kwargs.get("action")),
    )
    settings = MagicMock()
    settings.coinmap_email = ""
    settings.coinmap_password = ""
    settings.tradingview_password = ""
    settings.telegram_bot_token = "token"
    settings.telegram_chat_id = "main"
    settings.telegram_python_bot_chat_id = "python"
    settings.telegram_log_chat_id = None
    settings.telegram_parse_mode = None
    settings.openai_api_key = "key"
    settings.openai_prompt_id = "prompt"
    settings.openai_prompt_version = None
    settings.openai_vector_store_ids = []
    settings.openai_responses_store = True
    settings.openai_responses_include = None
    params = SimpleNamespace(
        capture_coinmap_yaml=tmp_path / "cap.yaml",
        charts_dir=tmp_path,
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        mt5_accounts_json=None,
        mt5_dry_run=False,
        mt5_execute=True,
        mt5_symbol="XAUUSD",
        no_telegram=False,
    )

    _run_tp1_openai_and_act(
        settings=settings,
        params=params,
        last_alert_path=last_alert_path,
        label="plan_chinh",
        trade_line=old_trade_line,
        p_last=101.0,
        parsed=MagicMock(side="BUY", tp1=101.0),
        page=MagicMock(),
        tv={},
        symbol="XAUUSD",
        settle_ms=0,
        browser_context=MagicMock(),
        prev_response_id="old-response-id",
    )

    st = read_last_alert_state(last_alert_path)
    assert sent_actions == ["chinh_trade_line"]
    assert st is not None
    assert st.status_by_label["plan_chinh"] == CHO_TP1
    assert st.mt5_ticket_by_label["plan_chinh"] == 111
    assert st.trade_line_by_label["plan_chinh"] == new_trade_line


def test_trade_management_does_not_send_raw_json_to_telegram(monkeypatch, tmp_path) -> None:
    last_alert_path = tmp_path / "last_alert_prices.json"
    old_trade_line = "BUY LIMIT 100 | SL 99 | TP1 101 | Lot 0.01"
    write_last_alert_state(
        LastAlertState(
            prices=(100.0, 200.0, 300.0),
            status_by_label={"plan_chinh": CHO_TP1, "plan_phu": "loai", "scalp": "loai"},
            trade_line_by_label={"plan_chinh": old_trade_line},
            mt5_ticket_by_label={"plan_chinh": 111},
            mt5_tickets_by_label={"plan_chinh": {"main": 111}},
        ),
        path=last_alert_path,
    )
    account = MT5AccountEntry(
        id="main",
        terminal_path="/tmp/metatrader64.exe",
        login=1,
        password="x",
        server="demo",
        primary=True,
        lot=LotRuleFromTrade(),
    )
    monkeypatch.setattr("automation_tool.tp1_followup.load_mt5_accounts_for_cli", lambda _path: [account])
    monkeypatch.setattr("automation_tool.tp1_followup.mt5_ticket_still_open", lambda *a, **k: (True, "open"))
    monkeypatch.setattr("automation_tool.tp1_followup.capture_charts", lambda **k: [Path(k["charts_dir"]) / "x.json"])
    monkeypatch.setattr("automation_tool.tp1_followup.coinmap_xauusd_5m_json_path", lambda _charts_dir: tmp_path / "x.json")
    monkeypatch.setattr("automation_tool.tp1_followup.read_main_chart_symbol", lambda _charts_dir: "XAUUSD")
    monkeypatch.setattr("automation_tool.tp1_followup.write_openai_coinmap_merged_from_raw_export", lambda p: p)
    (tmp_path / "x.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "automation_tool.tp1_followup.run_single_followup_responses",
        lambda **k: (
            '{"hanh_dong_quan_ly_lenh":"giu_nguyen","trade_line_moi":"","reason":"Giữ nguyên."}',
            "new-response-id",
        ),
    )
    raw_sends: list[str] = []
    monkeypatch.setattr(
        "automation_tool.tp1_followup.send_openai_output_to_telegram",
        lambda **kwargs: raw_sends.append(kwargs["raw"]),
        raising=False,
    )
    reason_sends: list[str] = []
    monkeypatch.setattr(
        "automation_tool.tp1_followup.send_trade_management_reason_notice",
        lambda **kwargs: reason_sends.append(kwargs["reason"]),
    )
    settings = MagicMock()
    settings.coinmap_email = ""
    settings.coinmap_password = ""
    settings.tradingview_password = ""
    settings.telegram_bot_token = "token"
    settings.telegram_chat_id = "main"
    settings.telegram_python_bot_chat_id = "python"
    settings.telegram_log_chat_id = None
    settings.telegram_parse_mode = None
    settings.openai_api_key = "key"
    settings.openai_prompt_id = "prompt"
    settings.openai_prompt_version = None
    settings.openai_vector_store_ids = []
    settings.openai_responses_store = True
    settings.openai_responses_include = None
    params = SimpleNamespace(
        capture_coinmap_yaml=tmp_path / "cap.yaml",
        charts_dir=tmp_path,
        storage_state_path=None,
        headless=True,
        no_save_storage=True,
        mt5_accounts_json=None,
        mt5_dry_run=False,
        mt5_execute=True,
        mt5_symbol="XAUUSD",
        no_telegram=False,
    )

    _run_tp1_openai_and_act(
        settings=settings,
        params=params,
        last_alert_path=last_alert_path,
        label="plan_chinh",
        trade_line=old_trade_line,
        p_last=101.0,
        parsed=MagicMock(side="BUY", tp1=101.0),
        page=MagicMock(),
        tv={},
        symbol="XAUUSD",
        settle_ms=0,
        browser_context=MagicMock(),
        prev_response_id="old-response-id",
    )

    assert raw_sends == []
    assert reason_sends == ["Giữ nguyên."]
