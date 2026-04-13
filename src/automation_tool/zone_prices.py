"""Parse three zone prices from OpenAI analysis text (PLAN CHÍNH / PHỤ / SCALP + BUY/SELL + pair)."""

from __future__ import annotations

import re
from typing import Optional

from automation_tool.openai_analysis_json import (
    merge_triple_with_baseline,
    parse_analysis_from_openai_text,
    triple_from_zone_prices,
)

# Section headers (Vietnamese diacritics + optional emoji)
_RE_PLAN_CHINH = re.compile(
    r"(?:📍\s*)?PLAN\s*CH[ÍI]NH\s+V[ÙU]NG\s+CH[ỜO]",
    re.IGNORECASE,
)
_RE_PLAN_PHU = re.compile(
    r"(?:📍\s*)?PLAN\s*PH[ỤU]\s+V[ÙU]NG\s+CH[ỜO]",
    re.IGNORECASE,
)
_RE_SCALP = re.compile(r"(?:⚡️?\s*)?SCALP\s+V[ÙU]NG", re.IGNORECASE)

# Two prices separated by dash/en-dash
_RE_PAIR = re.compile(
    r"(\d+(?:[.,]\d+)*)\s*[–\-]\s*(\d+(?:[.,]\d+)*)",
)
_RE_BUY = re.compile(r"\bBUY\b", re.IGNORECASE)
_RE_SELL = re.compile(r"\bSELL\b", re.IGNORECASE)


def normalize_price_token(s: str) -> float:
    s = s.strip().replace(",", "")
    return float(s)


def _pick_from_pair(a: float, b: float, side: str) -> float:
    if side.upper() == "BUY":
        return max(a, b)
    return min(a, b)


def _detect_side(block: str) -> Optional[str]:
    mb = _RE_BUY.search(block)
    ms = _RE_SELL.search(block)
    if mb and not ms:
        return "BUY"
    if ms and not mb:
        return "SELL"
    if mb and ms:
        return "BUY" if mb.start() < ms.start() else "SELL"
    return None


def _parse_one_block(block: str) -> Optional[float]:
    side = _detect_side(block)
    if not side:
        return None
    m = _RE_PAIR.search(block)
    if not m:
        return None
    p1 = normalize_price_token(m.group(1))
    p2 = normalize_price_token(m.group(2))
    return _pick_from_pair(p1, p2, side)


def _split_three_sections(text: str) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Returns (plan_chinh_block, plan_phu_block, scalp_block, error_message).
    """
    m0 = _RE_PLAN_CHINH.search(text)
    m1 = _RE_PLAN_PHU.search(text)
    m2 = _RE_SCALP.search(text)
    if not m0:
        return None, None, None, "Không tìm thấy section PLAN CHÍNH VÙNG CHỜ."
    if not m1:
        return None, None, None, "Không tìm thấy section PLAN PHỤ VÙNG CHỜ."
    if not m2:
        return None, None, None, "Không tìm thấy section SCALP VÙNG."

    headers: list[tuple[int, int, str]] = [
        (m0.start(), m0.end(), "c"),
        (m1.start(), m1.end(), "p"),
        (m2.start(), m2.end(), "s"),
    ]
    headers.sort(key=lambda x: x[0])
    blocks: dict[str, str] = {}
    for i, (_s, end_pos, key) in enumerate(headers):
        next_start = len(text)
        if i + 1 < len(headers):
            next_start = headers[i + 1][0]
        blocks[key] = text[end_pos:next_start]

    return blocks.get("c"), blocks.get("p"), blocks.get("s"), None


def parse_update_zone_triple(
    text: str,
    baseline: tuple[float, float, float],
) -> tuple[Optional[tuple[float, float, float]], Optional[str], Optional[bool]]:
    """
    Intraday update (Schema B): merge model ``prices`` with morning ``baseline`` using per-label
    ``no_change``. Root ``no_change: true`` (legacy Schema A) still means entire payload unchanged.
    """
    payload = parse_analysis_from_openai_text(text)
    if payload is None:
        return None, "Không parse được JSON từ phản hồi.", None
    if payload.no_change is True:
        return None, None, True
    if not payload.prices:
        return None, "JSON không có prices.", None
    merged = merge_triple_with_baseline(baseline, payload.prices)
    return merged, None, False


def parse_three_zone_prices(
    text: str,
) -> tuple[Optional[tuple[float, float, float]], Optional[str], Optional[bool]]:
    """
    Parse (plan_chinh, plan_phu, scalp) single price each.

    Returns ``(triple, err, no_change)``. When the model returns JSON with
    ``"no_change": true``, returns ``(None, None, True)``. The third value is
    ``None`` for legacy markdown-only output.
    """
    if not text or not text.strip():
        return None, "Empty text.", None

    payload = parse_analysis_from_openai_text(text)
    if payload is not None:
        if payload.no_change is True:
            return None, None, True
        if payload.prices:
            trip = triple_from_zone_prices(payload.prices)
            if trip is not None:
                return trip, None, False if payload.no_change is False else None
            return (
                None,
                "JSON có 'prices' nhưng thiếu đủ plan_chinh, plan_phu, scalp hoặc value không hợp lệ.",
                None,
            )

    bc, bp, bs, err = _split_three_sections(text)
    if err:
        return None, err, None
    assert bc is not None and bp is not None and bs is not None
    pc = _parse_one_block(bc)
    pp = _parse_one_block(bp)
    ps = _parse_one_block(bs)
    if pc is None:
        return (
            None,
            "Không parse được giá trong PLAN CHÍNH (cần BUY/SELL và cặp giá).",
            None,
        )
    if pp is None:
        return (
            None,
            "Không parse được giá trong PLAN PHỤ (cần BUY/SELL và cặp giá).",
            None,
        )
    if ps is None:
        return (
            None,
            "Không parse được giá trong SCALP (cần BUY/SELL và cặp giá).",
            None,
        )
    return (pc, pp, ps), None, None


def prices_equal_triple(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    *,
    eps: float = 0.01,
) -> bool:
    return all(abs(x - y) <= eps for x, y in zip(a, b))


def is_no_change_action_line(text: str) -> bool:
    t = text.lower()
    if "không đổi" in t and "hành động" in t:
        return True
    if "hanh dong" in t and "khong doi" in t:
        return True
    return "hành động: không đổi" in t or "hanh dong: khong doi" in t
