"""Extract and validate structured JSON from OpenAI analysis output."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

IntradayHanhDong = Literal["chờ", "loại", "VÀO LỆNH"]

ZONE_LABELS_ORDER = ("plan_chinh", "plan_phu", "scalp")

# Morning auto-MT5: hop_luu strictly above this threshold (plan_chinh / plan_phu).
AUTO_MT5_HOP_LUU_THRESHOLD = 80
# Lower bar for scalp-only auto-MT5.
AUTO_MT5_HOP_LUU_THRESHOLD_SCALP = 70


def auto_mt5_hop_luu_threshold_for_label(label: str) -> int:
    """Ngưỡng hop_luu cho auto-MT5 theo vùng: scalp thấp hơn plan_chinh / plan_phu."""
    key = label.strip().lower()
    if key == "scalp":
        return AUTO_MT5_HOP_LUU_THRESHOLD_SCALP
    return AUTO_MT5_HOP_LUU_THRESHOLD


def _strip_json_code_fence(text: str) -> str:
    t = text.strip()
    if not t.startswith("```"):
        return t
    first_nl = t.find("\n")
    if first_nl == -1:
        return t
    body = t[first_nl + 1 :].rstrip()
    if body.endswith("```"):
        body = body[:-3].rstrip()
    return body


def extract_json_object(raw: str) -> Optional[dict[str, Any]]:
    """
    Parse the first JSON object from model output: full string, fenced `` ```json ``,
    or first balanced ``{...}`` substring.
    """
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    for candidate in (raw, _strip_json_code_fence(raw)):
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(raw):
        if ch != "{":
            continue
        try:
            obj, _ = dec.raw_decode(raw, i)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def _as_float(x: Any) -> Optional[float]:
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        s = x.strip().replace(",", "")
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _normalize_intraday(s: str) -> Optional[IntradayHanhDong]:
    t = (s or "").strip()
    if not t:
        return None
    low = t.lower()
    if t == "VÀO LỆNH" or ("vào" in low and "lệnh" in low):
        return "VÀO LỆNH"
    if low in ("loại", "loai"):
        return "loại"
    if low == "chờ" or low == "cho":
        return "chờ"
    return None


def _as_int(x: Any) -> Optional[int]:
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, float):
        if x != x:  # NaN
            return None
        return int(round(x))
    if isinstance(x, str):
        s = x.strip().replace(",", "")
        if not s:
            return None
        try:
            return int(round(float(s)))
        except ValueError:
            return None
    return None


@dataclass
class PriceZoneEntry:
    label: str
    value: float
    range_low: Optional[float] = None
    range_high: Optional[float] = None
    hop_luu: Optional[int] = None
    trade_line: str = ""


@dataclass
class AnalysisPayload:
    """Structured analysis JSON (snake_case keys)."""

    out_chi_tiet: str = ""
    output_ngan_gon: str = ""
    prices: list[PriceZoneEntry] = field(default_factory=list)
    intraday_hanh_dong: Optional[IntradayHanhDong] = None
    trade_line: str = ""
    no_change: Optional[bool] = None


def _parse_price_entry(d: dict[str, Any]) -> Optional[PriceZoneEntry]:
    lab = d.get("label")
    if not isinstance(lab, str) or not lab.strip():
        return None
    v = _as_float(d.get("value"))
    if v is None:
        return None
    rl = _as_float(d.get("range_low"))
    rh = _as_float(d.get("range_high"))
    hop = _as_int(d.get("hop_luu"))
    tl_raw = d.get("trade_line")
    trade_line = tl_raw.strip() if isinstance(tl_raw, str) else ""
    return PriceZoneEntry(
        label=lab.strip(),
        value=v,
        range_low=rl,
        range_high=rh,
        hop_luu=hop,
        trade_line=trade_line,
    )


def try_parse_analysis_payload(data: dict[str, Any]) -> Optional[AnalysisPayload]:
    """Best-effort parse; returns None if ``data`` is empty or not a dict with usable keys."""
    if not data:
        return None
    oct = data.get("out_chi_tiet")
    ogn = data.get("output_ngan_gon")
    out_chi = oct.strip() if isinstance(oct, str) else ""
    out_ngan = ogn.strip() if isinstance(ogn, str) else ""

    prices_raw = data.get("prices")
    prices: list[PriceZoneEntry] = []
    if isinstance(prices_raw, list):
        for item in prices_raw:
            if isinstance(item, dict):
                pe = _parse_price_entry(item)
                if pe is not None:
                    prices.append(pe)

    intra_raw = data.get("intraday_hanh_dong")
    intra: Optional[IntradayHanhDong] = None
    if isinstance(intra_raw, str):
        intra = _normalize_intraday(intra_raw)

    tl = data.get("trade_line")
    trade_line = tl.strip() if isinstance(tl, str) else ""

    nc = data.get("no_change")
    no_change: Optional[bool] = None
    if isinstance(nc, bool):
        no_change = nc

    # Accept payload if it has at least one semantic field (not empty shell).
    if (
        not out_chi
        and not out_ngan
        and not prices
        and intra is None
        and not trade_line
        and no_change is None
    ):
        return None

    return AnalysisPayload(
        out_chi_tiet=out_chi,
        output_ngan_gon=out_ngan,
        prices=prices,
        intraday_hanh_dong=intra,
        trade_line=trade_line,
        no_change=no_change,
    )


def triple_from_zone_prices(prices: list[PriceZoneEntry]) -> Optional[tuple[float, float, float]]:
    """Return (plan_chinh, plan_phu, scalp) if all three labels present."""
    by_label: dict[str, float] = {}
    for p in prices:
        key = p.label.strip().lower()
        by_label[key] = p.value
    out: list[float] = []
    for lab in ZONE_LABELS_ORDER:
        if lab not in by_label:
            return None
        out.append(by_label[lab])
    return (out[0], out[1], out[2])


def select_zone_for_auto_mt5(
    prices: list[PriceZoneEntry],
) -> Optional[tuple[str, int, str]]:
    """
    Chọn một vùng để auto-MT5 sáng: ``hop_luu`` vượt ngưỡng theo vùng
    (:data:`AUTO_MT5_HOP_LUU_THRESHOLD` cho plan_chinh/plan_phu,
    :data:`AUTO_MT5_HOP_LUU_THRESHOLD_SCALP` cho scalp), có ``trade_line`` không rỗng.

    Nhiều vùng hợp lệ: **điểm cao nhất**; hòa điểm: thứ tự ``plan_chinh`` → ``plan_phu`` → ``scalp``.

    Returns:
        ``(label, hop_luu, trade_line)`` với ``label`` chữ thường khớp :data:`ZONE_LABELS_ORDER`,
        hoặc ``None``.
    """
    scored: list[tuple[str, int, str, int]] = []
    for p in prices:
        key = p.label.strip().lower()
        if key not in ZONE_LABELS_ORDER:
            continue
        thr = auto_mt5_hop_luu_threshold_for_label(key)
        if p.hop_luu is None or p.hop_luu <= thr:
            continue
        tl = (p.trade_line or "").strip()
        if not tl:
            continue
        tie = ZONE_LABELS_ORDER.index(key)
        scored.append((key, int(p.hop_luu), tl, tie))
    if not scored:
        return None
    scored.sort(key=lambda x: (-x[1], x[3]))
    best = scored[0]
    return (best[0], best[1], best[2])


def select_zone_for_auto_mt5_for_label(
    prices: list[PriceZoneEntry],
    zone_label: str,
) -> Optional[tuple[str, int, str]]:
    """
    Giống :func:`select_zone_for_auto_mt5` nhưng chỉ xét **một** vùng ``zone_label``
    (``plan_chinh`` | ``plan_phu`` | ``scalp``), ví dụ plan vừa chạm giá trong Nhật ký TV.
    """
    want = zone_label.strip().lower()
    if want not in ZONE_LABELS_ORDER:
        return None
    for p in prices:
        key = p.label.strip().lower()
        if key != want:
            continue
        thr = auto_mt5_hop_luu_threshold_for_label(key)
        if p.hop_luu is None or p.hop_luu <= thr:
            return None
        tl = (p.trade_line or "").strip()
        if not tl:
            return None
        return (key, int(p.hop_luu), tl)
    return None


def parse_analysis_from_openai_text(text: str) -> Optional[AnalysisPayload]:
    """Extract JSON from ``text`` and parse into :class:`AnalysisPayload`."""
    obj = extract_json_object(text)
    if not obj:
        return None
    return try_parse_analysis_payload(obj)

