"""CLI argument behavior for daemon-plan."""

from __future__ import annotations

from automation_tool.cli import _parser


def test_tv_watchlist_daemon_defaults_to_mt5_price(monkeypatch) -> None:
    from automation_tool import cli

    captured = {}
    args = _parser().parse_args(["tv-watchlist-daemon"])

    monkeypatch.setattr(cli, "load_settings", lambda: object())
    monkeypatch.setattr(cli, "require_openai", lambda _settings: None)
    monkeypatch.setattr(cli, "resolved_openai_model", lambda _settings, model: model)
    monkeypatch.setattr(cli, "run_tv_watchlist_daemon", lambda *, settings, params: captured.setdefault("params", params) or "stopped")

    cli.cmd_tv_watchlist_daemon(args)

    assert captured["params"].last_price_from_mt5 is True


def test_tv_watchlist_daemon_tv_symbol_price_opts_into_tradingview(monkeypatch) -> None:
    from automation_tool import cli

    captured = {}
    args = _parser().parse_args(["tv-watchlist-daemon", "--tv-symbol-price"])

    monkeypatch.setattr(cli, "load_settings", lambda: object())
    monkeypatch.setattr(cli, "require_openai", lambda _settings: None)
    monkeypatch.setattr(cli, "resolved_openai_model", lambda _settings, model: model)
    monkeypatch.setattr(cli, "run_tv_watchlist_daemon", lambda *, settings, params: captured.setdefault("params", params) or "stopped")

    cli.cmd_tv_watchlist_daemon(args)

    assert captured["params"].last_price_from_mt5 is False


def test_daemon_plan_stop_at_hour_defaults_to_auto() -> None:
    args = _parser().parse_args(["daemon-plan", "--shard", "zones/vung_plan_chinh_sang.json"])

    assert args.stop_at_hour is None
    assert args.stop_at_minute == 0


def test_daemon_plan_poll_seconds_defaults_to_five_seconds() -> None:
    args = _parser().parse_args(["daemon-plan", "--shard", "zones/vung_plan_chinh_sang.json"])

    assert args.poll_seconds == 5.0


def test_tv_watchlist_daemon_poll_seconds_stays_fast_by_default() -> None:
    args = _parser().parse_args(["tv-watchlist-daemon"])

    assert args.poll_seconds == 1.0


def test_daemon_plan_stop_at_hour_minus_one_disables_cutoff() -> None:
    args = _parser().parse_args(
        [
            "daemon-plan",
            "--shard",
            "zones/vung_plan_chinh_sang.json",
            "--stop-at-hour",
            "-1",
        ]
    )

    assert args.stop_at_hour == -1
