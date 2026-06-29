#!/usr/bin/env python3
"""Download OPENAI_VECTOR_STORE_IDS files to data/vector_store_knowledge/."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from automation_tool.config import default_vector_store_knowledge_dir, load_settings
from automation_tool.vector_store_sync import sync_vector_store_knowledge


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Sync OpenAI vector store files (OPENAI_VECTOR_STORE_IDS) to a local folder "
            "for FULL_ANALYSIS legacy agent analysis."
        ),
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Destination folder (default: OPENAI_VECTOR_STORE_KNOWLEDGE_DIR or data/vector_store_knowledge)",
    )
    p.add_argument(
        "--vector-store-id",
        action="append",
        dest="vector_store_ids",
        default=None,
        help="Override vector store id (repeatable). Default: OPENAI_VECTOR_STORE_IDS from .env",
    )
    args = p.parse_args(argv)

    s = load_settings()
    vs_ids = args.vector_store_ids or list(s.openai_vector_store_ids)
    if not vs_ids:
        print(
            "No vector store ids. Set OPENAI_VECTOR_STORE_IDS in .env or pass --vector-store-id.",
            file=sys.stderr,
        )
        return 1
    if not s.openai_api_key:
        print("OPENAI_API_KEY is required.", file=sys.stderr)
        return 1

    out_dir = args.output_dir or default_vector_store_knowledge_dir()
    manifest = sync_vector_store_knowledge(
        vector_store_ids=vs_ids,
        output_dir=out_dir,
        api_key=s.openai_api_key,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if manifest.get("ready") else 1


if __name__ == "__main__":
    sys.exit(main())
