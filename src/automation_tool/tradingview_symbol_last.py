"""Parse TradingView symbol page 'Last' value text."""

from __future__ import annotations

import re
from typing import Optional


_NUM_TOKEN_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def parse_tv_symbol_last_value(text: str) -> Optional[float]:
    """
    Parse TradingView symbol page Last value.

    TradingView sometimes renders the last digits in a nested <span>, e.g.:
        4,709.0<span>60</span>

    The resulting inner_text can be either:
      - "4,709.060"
      - "4,709.0\\n60"
      - "4,709.0 60"

    This function normalizes those into a float (4709.06).
    """
    raw = (text or "").strip()
    if not raw:
        return None

    tokens = [t for t in _NUM_TOKEN_RE.findall(raw) if t and any(ch.isdigit() for ch in t)]
    if not tokens:
        return None

    # Fast path: single token.
    if len(tokens) == 1:
        try:
            return float(tokens[0].replace(",", ""))
        except ValueError:
            return None

    # Two tokens: if the second is digits-only, and the first has a decimal part,
    # append digits to the decimal part (TV UI splits small last digits into a nested span).
    first = tokens[0].replace(",", "")
    second = tokens[1].replace(",", "")

    if "." in first and second.isdigit():
        head, frac = first.split(".", 1)
        if head and head.lstrip("-").isdigit() and frac.isdigit():
            merged = f"{head}.{frac}{second}"
            try:
                return float(merged)
            except ValueError:
                return None

    # Fallback: use the first numeric token only.
    try:
        return float(first)
    except ValueError:
        return None

