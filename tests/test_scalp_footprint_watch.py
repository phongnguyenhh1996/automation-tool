"""Tests for scalp footprint watch scheduling and warm session."""

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "scalp_footprint"))

from scheduling import (  # noqa: E402
    forming_candle_open,
    intervals_due,
    latest_closed_candle_open,
    next_close_trigger,
)


def test_forming_candle_open_5m() -> None:
    now = datetime(2026, 7, 3, 10, 7, 30)
    assert forming_candle_open(now, 5) == datetime(2026, 7, 3, 10, 5, 0)


def test_latest_closed_candle_open_5m() -> None:
    now = datetime(2026, 7, 3, 10, 7, 30)
    assert latest_closed_candle_open(now, 5) == datetime(2026, 7, 3, 10, 0, 0)


def test_intervals_due_after_buffer() -> None:
    # 10:06:30 — 90s after 10:05 close; 10:00-10:05 bar should be due for 5m
    now = datetime(2026, 7, 3, 10, 6, 30)
    due = intervals_due(now, buffer_sec=90, last_processed={})
    assert "5m" in due


def test_intervals_not_due_before_buffer() -> None:
    now = datetime(2026, 7, 3, 10, 1, 0)
    due = intervals_due(now, buffer_sec=90, last_processed={})
    assert due == []


def test_intervals_skip_already_processed() -> None:
    now = datetime(2026, 7, 3, 10, 6, 30)
    last = {"5m": latest_closed_candle_open(now, 5)}
    due = intervals_due(now, buffer_sec=90, last_processed=last)
    assert "5m" not in due


def test_next_close_trigger_future() -> None:
    now = datetime(2026, 7, 3, 10, 6, 0)
    nxt = next_close_trigger(now, 5, buffer_sec=90)
    assert nxt == datetime(2026, 7, 3, 10, 6, 30)


def test_15m_due_on_quarter_hour() -> None:
    now = datetime(2026, 7, 3, 10, 16, 30)
    due = intervals_due(now, buffer_sec=90, last_processed={})
    assert "15m" in due
    assert "5m" in due


def test_map_paths_by_interval_exact_match() -> None:
    from watch import _map_paths_by_interval

    charts = Path("data/XAUUSD/charts/footprint_images")
    paths = [
        charts / "footprint_combined_5m.json",
        charts / "footprint_combined_15m.json",
    ]
    mapped = _map_paths_by_interval(paths, ("5m", "15m"))
    assert mapped["5m"].name == "footprint_combined_5m.json"
    assert mapped["15m"].name == "footprint_combined_15m.json"
    assert mapped["5m"] != mapped["15m"]


def test_capture_footprint_headless_uses_native_gc_cfg(monkeypatch) -> None:
    from watch import capture_footprint_headless

    captured_cfg: dict = {}

    captured_kwargs: dict = {}

    def _fake_ws_plan(_ctx, cfg, **kwargs):
        captured_cfg.update(cfg)
        captured_kwargs.update(kwargs)
        return []

    yaml_path = Path("config/gocharting.yaml")
    monkeypatch.setattr(
        "automation_tool.gocharting_ws_capture.capture_footprint_ws_plan",
        _fake_ws_plan,
    )
    monkeypatch.setattr(
        "automation_tool.playwright_browser.launch_chrome_context",
        lambda *a, **k: (None, None),
    )
    monkeypatch.setattr(
        "automation_tool.playwright_browser.close_browser_and_context",
        lambda *a, **k: None,
    )
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: _FakePlaywright())

    capture_footprint_headless(
        intervals=("5m",),
        charts_dir=Path("data/XAUUSD/charts"),
        gocharting_yaml=yaml_path,
        headless=True,
    )
    assert captured_cfg["footprint_ws"]["gc_to_spot"]["enabled"] is False
    assert captured_cfg["footprint_ws"]["mt5_spot"] is False
    assert captured_cfg["footprint_ws"]["extra_session_days"] == 0
    assert captured_kwargs.get("extra_session_days") == 0


class _FakePlaywright:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_default_buffer_sec_is_five() -> None:
    from watch import DEFAULT_BUFFER_SEC

    assert DEFAULT_BUFFER_SEC == 5


def test_process_intervals_skips_capture_when_paths_provided(monkeypatch, tmp_path) -> None:
    from watch import process_intervals

    charts_dir = tmp_path / "charts"
    fp_dir = charts_dir / "footprint_images"
    fp_dir.mkdir(parents=True)
    json_path = fp_dir / "footprint_combined_5m.json"
    json_path.write_text(
        '{"symbol":"XAUUSD","candles":[{"ohlc":{"open":1,"high":2,"low":0.5,"close":1.5},"bar_flow":{}}]}',
        encoding="utf-8",
    )
    state_path = charts_dir / "state.json"
    trades_path = charts_dir / "trades.json"

    capture_called = False

    def _should_not_capture(**_kwargs):
        nonlocal capture_called
        capture_called = True
        return []

    monkeypatch.setattr("watch.capture_footprint_headless", _should_not_capture)
    monkeypatch.setattr("watch.detect_latest_signals", lambda *_a, **_k: [])
    monkeypatch.setattr("watch.evaluate_open_trades", lambda *_a, **_k: [])
    monkeypatch.setattr("watch.load_state", lambda _p: {"last_processed": {}, "sent_keys": []})
    monkeypatch.setattr("watch.save_state", lambda *_a, **_k: None)

    process_intervals(
        intervals=("5m",),
        charts_dir=charts_dir,
        gocharting_yaml=Path("config/gocharting.yaml"),
        confirmed=True,
        headless=True,
        bot_token="",
        chat_id="1",
        state_path=state_path,
        trades_path=trades_path,
        max_hold_bars=12,
        dry_run=True,
        captured_paths=[json_path],
    )
    assert capture_called is False


def test_warm_session_disables_idb_poll(monkeypatch, tmp_path) -> None:
    from warm_session import WarmFootprintSession

    charts_dir = tmp_path / "charts"
    session = WarmFootprintSession(
        charts_dir=charts_dir,
        gocharting_yaml=Path("config/gocharting.yaml"),
        headless=True,
    )
    monkeypatch.setattr(
        "automation_tool.gocharting_capture_lock.acquire_gocharting_capture_lock",
        lambda: None,
    )
    monkeypatch.setattr(
        "automation_tool.playwright_browser.launch_chrome_context",
        lambda *a, **k: (MagicMock(), MagicMock()),
    )
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: MagicMock(start=lambda: MagicMock()))
    monkeypatch.setattr(session, "_setup_tab", lambda _tab: None)

    session.start()
    assert session._cfg["footprint_ws"]["idb"]["enabled"] is False
    session.close()


def test_warm_session_capture_delegates_to_tabs(monkeypatch, tmp_path) -> None:
    from warm_session import WarmFootprintSession, _WarmTab

    charts_dir = tmp_path / "charts"
    dest = charts_dir / "footprint_images" / "footprint_combined_5m.json"
    dest.parent.mkdir(parents=True)

    session = WarmFootprintSession(
        charts_dir=charts_dir,
        gocharting_yaml=Path("config/gocharting.yaml"),
        headless=True,
        intervals=("5m",),
    )
    session._started = True
    fake_page = MagicMock()
    fake_page.is_closed.return_value = False
    tab = _WarmTab(interval="5m", chart_url="https://example.com", page=fake_page, dest=dest)
    session._tabs["5m"] = tab

    monkeypatch.setattr(session, "ensure_healthy", lambda: None)
    monkeypatch.setattr(session, "_capture_tab", lambda _tab: dest)

    paths = session.capture_intervals(("5m",))
    assert paths == [dest]


def test_run_loop_warm_uses_warm_session(monkeypatch) -> None:
    from watch import run_loop_warm

    class _FakeSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.started = False
            self.closed = False

        def start(self):
            self.started = True

        def close(self):
            self.closed = True

        def capture_intervals(self, intervals):
            return []

    args = MagicMock()
    args.buffer_sec = 5
    args.chat_id = "1"
    args.confirmed = True
    args.warm_wait_ms = 15000
    args.health_interval_sec = 300
    args.charts_dir = Path("data/XAUUSD/charts")
    args.gocharting_yaml = Path("config/gocharting.yaml")
    args.headed = False
    args.state_file = Path("state.json")
    args.dry_run = True
    args.bot_token = ""

    due_calls = {"n": 0}

    def _fake_intervals_due(now, *, buffer_sec, last_processed):
        due_calls["n"] += 1
        if due_calls["n"] == 1:
            return ["5m"]
        raise KeyboardInterrupt

    monkeypatch.setattr("watch.WarmFootprintSession", _FakeSession)
    monkeypatch.setattr("watch.intervals_due", _fake_intervals_due)
    monkeypatch.setattr("watch.load_state", lambda _p: {"last_processed": {}, "sent_keys": []})
    monkeypatch.setattr("watch._run_due_cycle", lambda *_a, **_k: None)
    monkeypatch.setattr("watch._startup_telegram", lambda *_a, **_k: None)

    try:
        run_loop_warm(args)
    except KeyboardInterrupt:
        pass


def test_warm_alert_body_parseable_by_executor() -> None:
    """Warm watch Telegram body must contain SCALP_EXEC lines for telegram_executor."""
    from exec_line import extract_exec_lines
    from watch import format_alert_message

    sig = {
        "pattern_id": "exhaustion_long",
        "pattern_name": "Exhaustion Long",
        "direction": "long",
        "side": "BUY",
        "entry_price": 2650.5,
        "timeframe": "5m",
        "time_gmt7": "2026-07-03 10:00",
        "bar_index": 42,
    }
    body = format_alert_message([sig], interval="5m", symbol="XAUUSD")
    lines = extract_exec_lines(body)
    assert len(lines) == 1
    assert lines[0].startswith("SCALP_EXEC|exhaustion_long|BUY|MARKET|")
