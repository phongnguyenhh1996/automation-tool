"""One-time move from flat ``data/*.json`` + ``data/charts/`` to ``data/{{SYMBOL}}/``."""

from __future__ import annotations

import re
import shutil

from automation_tool.config import default_data_dir
from automation_tool.images import (
    DEFAULT_MAIN_CHART_SYMBOL,
    GLOBAL_MAIN_CHART_SYMBOL_FILENAME,
    MAIN_CHART_SYMBOL_FILENAME,
)


def migrate_legacy_flat_data_layout() -> None:
    """
    If ``data/XAUUSD/`` (or chosen symbol) does not exist but legacy flat files do,
    move them under ``data/{{SYM}}/`` and write ``data/.main_chart_symbol``.
    """
    root = default_data_dir()
    nested_default = root / DEFAULT_MAIN_CHART_SYMBOL
    if nested_default.is_dir():
        return

    legacy_charts = root / "charts"
    names = (
        "last_alert_prices.json",
        "morning_baseline_prices.json",
        "morning_full_analysis.json",
        "last_response_id.txt",
        "last_all_response_id.txt",
        "storage_state.json",
    )
    has_flat = any((root / n).is_file() for n in names)
    has_legacy_charts = legacy_charts.is_dir() and any(legacy_charts.iterdir())
    if not has_flat and not has_legacy_charts:
        return

    target_sym = DEFAULT_MAIN_CHART_SYMBOL
    lm = legacy_charts / MAIN_CHART_SYMBOL_FILENAME
    if lm.is_file():
        try:
            raw = lm.read_text(encoding="utf-8").strip().splitlines()
            line = raw[0] if raw else ""
            if line and re.match(r"^[A-Za-z0-9]{4,16}$", line.strip()):
                target_sym = line.strip().upper()
        except OSError:
            pass

    dest = root / target_sym
    dest.mkdir(parents=True, exist_ok=True)
    if has_legacy_charts:
        shutil.move(str(legacy_charts), str(dest / "charts"))
    for n in names:
        p = root / n
        if p.is_file():
            shutil.move(str(p), str(dest / n))

    gm = root / GLOBAL_MAIN_CHART_SYMBOL_FILENAME
    if not gm.is_file():
        gm.write_text(target_sym + "\n", encoding="utf-8")
