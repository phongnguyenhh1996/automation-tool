"""
Re-capture only failed chart JSON files (same stamp) after validation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from automation_tool.chart_payload_validate import ChartSlotIssue
from automation_tool.coinmap import capture_charts, load_coinmap_yaml


def recapture_failed_chart_slots(
    *,
    coinmap_yaml: Path,
    charts_dir: Path,
    stamp: str,
    issues: list[ChartSlotIssue],
    storage_state_path: Optional[Path],
    email: Optional[str],
    password: Optional[str],
    tradingview_password: Optional[str],
    save_storage_state: bool,
    headless: bool,
    main_chart_symbol: Optional[str],
) -> None:
    """
    For each failed slot, re-run only the producer for that artifact (tvdatafeed partial
    export and/or Coinmap bearer API-only partial plan).
    """
    tv_paths = [i.expected_path for i in issues if i.source == "tradingview"]
    cm_paths = [i.expected_path for i in issues if i.source == "coinmap"]

    cfg = load_coinmap_yaml(coinmap_yaml)

    if tv_paths:
        tv = cfg.get("tradingview_capture") or {}
        if not isinstance(tv, dict) or not tv.get("enabled", False):
            raise SystemExit(
                "Cannot retry TradingView JSON: tradingview_capture.enabled is false in YAML."
            )
        tv_ds = str(tv.get("data_source") or "browser").strip().lower()
        if tv_ds != "tvdatafeed":
            raise SystemExit(
                "Chart JSON validation retry only supports tradingview_capture.data_source: tvdatafeed "
                f"(got {tv_ds!r})."
            )
        from automation_tool.tvdatafeed_capture import run_tvdatafeed_export

        run_tvdatafeed_export(
            tv=tv,
            charts_dir=charts_dir,
            stamp=stamp,
            tradingview_username=email,
            tradingview_password=tradingview_password,
            only_target_paths=tv_paths,
        )

    if cm_paths:
        capture_charts(
            coinmap_yaml=coinmap_yaml,
            charts_dir=charts_dir,
            storage_state_path=storage_state_path,
            email=email,
            password=password,
            tradingview_password=tradingview_password,
            save_storage_state=save_storage_state,
            headless=headless,
            main_chart_symbol=main_chart_symbol,
            stamp_override=stamp,
            clear_charts_before_capture=False,
            coinmap_only_retry_paths=cm_paths,
        )
