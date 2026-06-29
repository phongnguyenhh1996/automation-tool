#!/usr/bin/env python3
"""Print FULL_ANALYSIS capture manifest as JSON (for AI agents)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from automation_tool.full_analysis_manifest import (
    build_full_analysis_manifest,
    default_charts_dir_for_manifest,
    manifest_to_json,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="JSON manifest of FULL_ANALYSIS chart slots and footprint readiness.",
    )
    p.add_argument(
        "--charts-dir",
        type=Path,
        default=None,
        help="Charts directory (default: data/<active_symbol>/charts/)",
    )
    p.add_argument(
        "--stamp",
        default=None,
        metavar="STAMP",
        help="Capture stamp YYYYMMDD_HHMMSS (default: latest on disk)",
    )
    p.add_argument(
        "--gocharting-config",
        type=Path,
        default=None,
        help="GoCharting YAML (default: config/gocharting.yaml)",
    )
    p.add_argument(
        "--legacy",
        action="store_true",
        help=(
            "FULL_ANALYSIS legacy: require synced vector store knowledge "
            "(data/vector_store_knowledge/) instead of master_trading_playbook.md"
        ),
    )
    p.add_argument(
        "--knowledge-dir",
        type=Path,
        default=None,
        help="Local vector store mirror (default: OPENAI_VECTOR_STORE_KNOWLEDGE_DIR)",
    )
    args = p.parse_args(argv)
    charts_dir = default_charts_dir_for_manifest(args.charts_dir)
    from automation_tool.config import load_settings

    s = load_settings()
    manifest = build_full_analysis_manifest(
        charts_dir,
        stamp=args.stamp,
        gocharting_yaml=args.gocharting_config,
        legacy=args.legacy,
        knowledge_dir=args.knowledge_dir,
        vector_store_ids=s.openai_vector_store_ids if args.legacy else None,
    )
    print(manifest_to_json(manifest))
    return 0 if manifest.get("ready_for_analysis") else 1


if __name__ == "__main__":
    sys.exit(main())
