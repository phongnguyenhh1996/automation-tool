"""Tests for last_alert_prices.json merge and terminal checks."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from automation_tool.state_files import (
    CHO_TP1,
    LOAI,
    VAO_LENH,
    VUNG_CHO,
    LastAlertState,
    all_plans_terminal,
    merge_alert_prices_with_status,
    merge_trade_lines_from_openai_analysis_text,
    needs_post_entry_price_watch,
    read_last_alert_state,
    remove_last_alert_prices_file,
    update_single_plan_status,
    watchlist_journal_active_work,
    write_last_alert_state,
)


def test_read_legacy_no_status_all_vung_cho() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "last_alert_prices.json"
        p.write_text(
            json.dumps(
                {"prices": [2600.0, 2610.0, 2620.0], "labels": ["plan_chinh", "plan_phu", "scalp"]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        st = read_last_alert_state(p)
        assert st is not None
        for lab in st.labels:
            assert st.status_by_label[lab] == VUNG_CHO
            assert st.entry_manual_by_label.get(lab) is False
        assert not all_plans_terminal(st)


def test_merge_only_resets_changed_price_labels() -> None:
    old = LastAlertState(
        prices=(2600.0, 2610.0, 2620.0),
        labels=("plan_chinh", "plan_phu", "scalp"),
        status_by_label={
            "plan_chinh": VAO_LENH,
            "plan_phu": LOAI,
            "scalp": VUNG_CHO,
        },
        entry_manual_by_label={
            "plan_chinh": True,
            "plan_phu": False,
            "scalp": False,
        },
    )
    new = merge_alert_prices_with_status(old, (2600.5, 2610.0, 2620.0))
    assert new.status_by_label["plan_chinh"] == VUNG_CHO
    assert new.status_by_label["plan_phu"] == LOAI
    assert new.status_by_label["scalp"] == VUNG_CHO
    assert new.entry_manual_by_label["plan_chinh"] is False
    assert new.entry_manual_by_label["plan_phu"] is False


def test_all_plans_terminal_true() -> None:
    st = LastAlertState(
        prices=(1.0, 2.0, 3.0),
        labels=("plan_chinh", "plan_phu", "scalp"),
        status_by_label={
            "plan_chinh": VAO_LENH,
            "plan_phu": LOAI,
            "scalp": LOAI,
        },
    )
    assert all_plans_terminal(st)


def test_watchlist_journal_active_work_post_entry() -> None:
    """Còn vao_lenh + trade_line + ticket → monitor không dừng dù hết vung_cho."""
    st = LastAlertState(
        prices=(1.0, 2.0, 3.0),
        labels=("plan_chinh", "plan_phu", "scalp"),
        status_by_label={
            "plan_chinh": VAO_LENH,
            "plan_phu": LOAI,
            "scalp": LOAI,
        },
        trade_line_by_label={"plan_chinh": "BUY 0.1 @ 2600 SL … TP1 2610"},
        mt5_ticket_by_label={"plan_chinh": 123456},
    )
    assert needs_post_entry_price_watch(st)
    assert watchlist_journal_active_work(st)


def test_cho_tp1_is_terminal_for_all_plans_terminal() -> None:
    st = LastAlertState(
        prices=(1.0, 2.0, 3.0),
        labels=("plan_chinh", "plan_phu", "scalp"),
        status_by_label={
            "plan_chinh": CHO_TP1,
            "plan_phu": LOAI,
            "scalp": LOAI,
        },
    )
    assert all_plans_terminal(st)


def test_update_single_plan_status_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "x.json"
        write_last_alert_state(
            LastAlertState(
                prices=(100.0, 200.0, 300.0),
                labels=("plan_chinh", "plan_phu", "scalp"),
                status_by_label={lab: VUNG_CHO for lab in ("plan_chinh", "plan_phu", "scalp")},
            ),
            path=p,
        )
        update_single_plan_status("plan_chinh", VAO_LENH, path=p, entry_manual=False)
        st = read_last_alert_state(p)
        assert st is not None
        assert st.status_by_label["plan_chinh"] == VAO_LENH
        assert st.status_by_label["plan_phu"] == VUNG_CHO
        assert st.entry_manual_by_label["plan_chinh"] is False


def test_remove_last_alert_prices_file() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "last_alert_prices.json"
        assert remove_last_alert_prices_file(p) is False
        write_last_alert_state(
            LastAlertState(
                prices=(1.0, 2.0, 3.0),
                labels=("plan_chinh", "plan_phu", "scalp"),
                status_by_label={lab: VUNG_CHO for lab in ("plan_chinh", "plan_phu", "scalp")},
            ),
            path=p,
        )
        assert p.is_file()
        assert remove_last_alert_prices_file(p) is True
        assert not p.exists()
        assert remove_last_alert_prices_file(p) is False


def test_update_single_plan_unknown_label_exits() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "x.json"
        write_last_alert_state(
            LastAlertState(
                prices=(1.0, 2.0, 3.0),
                labels=("plan_chinh", "plan_phu", "scalp"),
                status_by_label={lab: VUNG_CHO for lab in ("plan_chinh", "plan_phu", "scalp")},
            ),
            path=p,
        )
        with pytest.raises(SystemExit):
            update_single_plan_status("nope", VAO_LENH, path=p)


def test_merge_trade_lines_from_openai_analysis_text() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "last_alert_prices.json"
        write_last_alert_state(
            LastAlertState(
                prices=(100.0, 200.0, 300.0),
                labels=("plan_chinh", "plan_phu", "scalp"),
                status_by_label={lab: VUNG_CHO for lab in ("plan_chinh", "plan_phu", "scalp")},
            ),
            path=p,
        )
        text = r"""
{
  "prices": [
    {"label": "plan_chinh", "value": 100, "hop_luu": 80, "trade_line": "BUY LIMIT 100 | SL 99 | TP1 101 | Lot 0.01"},
    {"label": "plan_phu", "value": 200, "hop_luu": 70, "trade_line": ""}
  ]
}
"""
        merge_trade_lines_from_openai_analysis_text(text, path=p)
        st = read_last_alert_state(p)
        assert st is not None
        assert "BUY LIMIT 100" in (st.trade_line_by_label.get("plan_chinh") or "")
        assert (st.trade_line_by_label.get("plan_phu") or "") == ""
