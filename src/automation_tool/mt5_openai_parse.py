from __future__ import annotations

import os
import re

import automation_tool.config  # noqa: F401 — nạp .env từ root project (MT5_SYMBOL, …)
from dataclasses import dataclass
from typing import Literal, Optional

_RE_OUTPUT_NGAN_GON = re.compile(r"\[OUTPUT_NGAN_GON\]", re.IGNORECASE)

# Single-line: SELL LIMIT 3360.0 | SL 3363.5 | TP1 3354.5 | TP2 3350.5 | Lot 0.02
_RE_TRADE_PIPE = re.compile(
    r"(?P<side>BUY|SELL)\s+"
    r"(?P<kind>LIMIT|STOP|MARKET)\s+"
    r"(?P<price>[\d.]+)\s*\|\s*"
    r"SL\s+(?P<sl>[\d.]+)\s*\|\s*"
    r"TP1\s+(?P<tp1>[\d.]+)"
    r"(?:\s*\|\s*TP2\s+(?P<tp2>[\d.]+))?"
    r"\s*\|\s*Lot\s+(?P<lot>[\d.]+)",
    re.IGNORECASE,
)

# MARKET: có thể không có giá limit — ví dụ sau này: BUY MARKET | SL ... | TP1 ... | Lot ...
_RE_TRADE_PIPE_MARKET = re.compile(
    r"(?P<side>BUY|SELL)\s+"
    r"MARKET\s*\|\s*"
    r"SL\s+(?P<sl>[\d.]+)\s*\|\s*"
    r"TP1\s+(?P<tp1>[\d.]+)"
    r"(?:\s*\|\s*TP2\s+(?P<tp2>[\d.]+))?"
    r"\s*\|\s*Lot\s+(?P<lot>[\d.]+)",
    re.IGNORECASE,
)

_RE_SYMBOL_HEADING = re.compile(
    r"📊\s*([A-Z0-9]+)\s*[–\-—]",
    re.UNICODE,
)


@dataclass(frozen=True)
class ParsedTrade:
    symbol: str
    side: Literal["BUY", "SELL"]
    kind: Literal["LIMIT", "STOP", "MARKET"]
    price: Optional[float]
    sl: float
    tp1: float
    tp2: Optional[float]
    lot: float
    raw_line: str


def extract_output_ngan_gon_block(text: str) -> Optional[str]:
    """Trả về nội dung sau ``[OUTPUT_NGAN_GON]`` tới hết text hoặc tới section khác ``[...]``.

    Nếu prompt/system message cũng chứa ``[OUTPUT_NGAN_GON]`` (mô tả format), lấy
    **lần xuất hiện cuối** — trùng với bản phân tích thật của model.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    matches = list(_RE_OUTPUT_NGAN_GON.finditer(text))
    if not matches:
        return None
    m = matches[-1]
    rest = text[m.end() :].lstrip()
    # Cắt nếu có section markdown tiếp theo [SOMETHING]
    cut = re.search(r"^\s*\[[^\]]+\]\s*$", rest, re.MULTILINE)
    if cut:
        rest = rest[: cut.start()]
    return rest.strip()


def extract_symbol_hint(text: str) -> Optional[str]:
    """Tìm mã từ dòng kiểu ``📊 XAUUSD – ...`` hoặc chữ XAUUSD trong text."""
    m = _RE_SYMBOL_HEADING.search(text)
    if m:
        return m.group(1).strip().upper()
    if re.search(r"\bXAUUSD\b", text, re.IGNORECASE):
        return "XAUUSD"
    return None


def parse_hanh_dong_ngan_gon(ngan_gon_block: str) -> Optional[Literal["VÀO LỆNH", "ĐỨNG NGOÀI"]]:
    """
    Lấy hành động cuối trong block (dòng ``Hành động: ...``).
    """
    if not ngan_gon_block:
        return None
    last: Optional[Literal["VÀO LỆNH", "ĐỨNG NGOÀI"]] = None
    for line in ngan_gon_block.split("\n"):
        line = line.strip()
        low = line.lower()
        if "hành động" not in low and "hanh dong" not in low.replace("à", "a"):
            continue
        if ":" not in line:
            continue
        tail = line.split(":", 1)[-1].strip().lower()
        if "vào" in tail and "lệnh" in tail:
            last = "VÀO LỆNH"
        elif "đứng" in tail and "ngoài" in tail:
            last = "ĐỨNG NGOÀI"
    return last


def find_trade_line(ngan_gon_block: str) -> Optional[str]:
    """Dòng chứa lệnh dạng pipe (ưu tiên dòng có '|' và BUY/SELL)."""
    for line in ngan_gon_block.split("\n"):
        s = line.strip()
        if "|" in s and re.search(r"\b(BUY|SELL)\s+(LIMIT|STOP|MARKET)\b", s, re.I):
            return s
        if "|" in s and re.search(r"\b(BUY|SELL)\s+MARKET\b", s, re.I):
            return s
    return None


def parse_trade_line(
    line: str,
    symbol: str,
) -> Optional[ParsedTrade]:
    line = line.strip()
    m = _RE_TRADE_PIPE.match(line)
    if m:
        tp2_raw = m.group("tp2")
        return ParsedTrade(
            symbol=symbol,
            side=m.group("side").upper(),  # type: ignore[arg-type]
            kind=m.group("kind").upper(),  # type: ignore[arg-type]
            price=float(m.group("price")),
            sl=float(m.group("sl")),
            tp1=float(m.group("tp1")),
            tp2=float(tp2_raw) if tp2_raw else None,
            lot=float(m.group("lot")),
            raw_line=line,
        )
    m2 = _RE_TRADE_PIPE_MARKET.match(line)
    if m2:
        tp2_raw = m2.group("tp2")
        return ParsedTrade(
            symbol=symbol,
            side=m2.group("side").upper(),  # type: ignore[arg-type]
            kind="MARKET",
            price=None,
            sl=float(m2.group("sl")),
            tp1=float(m2.group("tp1")),
            tp2=float(tp2_raw) if tp2_raw else None,
            lot=float(m2.group("lot")),
            raw_line=line,
        )
    return None


def parse_openai_output_md(
    text: str,
    default_symbol: str = "XAUUSD",
    symbol_override: Optional[str] = None,
) -> tuple[Optional[ParsedTrade], Optional[str]]:
    """
    Đọc full markdown OpenAI: chỉ trả lệnh khi ``[OUTPUT_NGAN_GON]`` kết thúc bằng
    ``Hành động: VÀO LỆNH`` và có dòng lệnh pipe.

    Returns:
        (ParsedTrade or None, error message or None)
    """
    # --symbol > MT5_SYMBOL (.env) > hint từ text (📊 XAUUSD) > default_symbol.
    # Nhiều broker chỉ có XAUUSDm: đặt MT5_SYMBOL=XAUUSDm để không bị hint "XAUUSD" ghi đè.
    sym = (
        (symbol_override or "").strip()
        or (os.getenv("MT5_SYMBOL") or "").strip()
        or extract_symbol_hint(text)
        or default_symbol
    )
    block = extract_output_ngan_gon_block(text)
    if not block:
        return None, "Không tìm thấy [OUTPUT_NGAN_GON]."

    action = parse_hanh_dong_ngan_gon(block)
    if action != "VÀO LỆNH":
        return None, (
            f"Hành động trong OUTPUT_NGAN_GON là {action!r}, cần 'VÀO LỆNH' để gửi lệnh."
        )

    trade_line = find_trade_line(block)
    if not trade_line:
        return None, "Không có dòng lệnh dạng BUY/SELL LIMIT|...|Lot trong OUTPUT_NGAN_GON."

    parsed = parse_trade_line(trade_line, symbol=sym)
    if not parsed:
        return None, f"Không parse được dòng lệnh: {trade_line!r}"

    return parsed, None
