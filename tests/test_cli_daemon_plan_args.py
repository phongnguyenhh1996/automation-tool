"""CLI argument behavior for daemon-plan."""

from __future__ import annotations

from automation_tool.cli import _parser


def test_daemon_plan_stop_at_hour_defaults_to_auto() -> None:
    args = _parser().parse_args(["daemon-plan", "--shard", "zones/vung_plan_chinh_sang.json"])

    assert args.stop_at_hour is None
    assert args.stop_at_minute == 0


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
