from __future__ import annotations

import base64
import mimetypes
import re
from pathlib import Path
from typing import Optional

# Filenames: ``{stamp}_tradingview_{SYMBOL}_{interval}.png`` / ``{stamp}_coinmap_{SYMBOL}_{interval}.png``
# Order used for OpenAI vision (must match capture naming in coinmap.py).
CHART_IMAGE_ORDER: tuple[tuple[str, str, str], ...] = (
    ("tradingview", "DXY", "4h"),
    ("tradingview", "DXY", "1h"),
    ("tradingview", "DXY", "15m"),
    ("coinmap", "DXY", "15m"),
    ("tradingview", "XAUUSD", "4h"),
    ("tradingview", "XAUUSD", "1h"),
    ("tradingview", "XAUUSD", "15m"),
    ("tradingview", "XAUUSD", "5m"),
    ("coinmap", "XAUUSD", "15m"),
    ("coinmap", "XAUUSD", "5m"),
)

_STAMP_RE = re.compile(r"^(\d{8}_\d{6})_(?:tradingview|coinmap)_")


def latest_chart_stamp(charts_dir: Path) -> Optional[str]:
    """Latest ``YYYYMMDD_HHMMSS`` prefix shared by tradingview/coinmap shots in ``charts_dir``."""
    if not charts_dir.is_dir():
        return None
    stamps: set[str] = set()
    for p in charts_dir.glob("*.png"):
        m = _STAMP_RE.match(p.name)
        if m:
            stamps.add(m.group(1))
    for p in charts_dir.glob("*.json"):
        m = _STAMP_RE.match(p.name)
        if m:
            stamps.add(m.group(1))
    return max(stamps) if stamps else None


def ordered_chart_openai_payloads(
    charts_dir: Path, *, stamp: Optional[str] = None
) -> list[tuple[str, Path]]:
    """
    Same slot order as ``CHART_IMAGE_ORDER`` (for OpenAI step 2).

    * **TradingView** — ``.png`` only.
    * **Coinmap** — prefer ``.json`` (API export) over ``.png`` so analysis can run
      without screenshots while keeping the same ordering as when images were used.
    """
    if not charts_dir.is_dir():
        return []
    st = stamp or latest_chart_stamp(charts_dir)
    if not st:
        return []
    out: list[tuple[str, Path]] = []
    for src, sym, iv in CHART_IMAGE_ORDER:
        if src == "coinmap":
            jp = charts_dir / f"{st}_coinmap_{sym}_{iv}.json"
            pp = charts_dir / f"{st}_coinmap_{sym}_{iv}.png"
            if jp.is_file():
                out.append(("json", jp))
            elif pp.is_file():
                out.append(("image", pp))
        else:
            pp = charts_dir / f"{st}_tradingview_{sym}_{iv}.png"
            if pp.is_file():
                out.append(("image", pp))
    return out


def ordered_chart_images(charts_dir: Path, *, stamp: Optional[str] = None) -> list[Path]:
    """
    Return chart paths in analysis order (DXY TV → DXY coinmap → XAUUSD TV → XAUUSD coinmap).
    Only includes files that exist. Uses latest stamp in directory when ``stamp`` is omitted.
    """
    if not charts_dir.is_dir():
        return []
    st = stamp or latest_chart_stamp(charts_dir)
    if not st:
        return []
    out: list[Path] = []
    for src, sym, iv in CHART_IMAGE_ORDER:
        p = charts_dir / f"{st}_{src}_{sym}_{iv}.png"
        if p.is_file():
            out.append(p)
    return out


def image_to_data_url(path: Path) -> str:
    raw = path.read_bytes()
    mime, _ = mimetypes.guess_type(str(path))
    if not mime:
        mime = "image/png"
    b64 = base64.standard_b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def list_chart_images(charts_dir: Path, patterns: tuple[str, ...] = ("*.png", "*.jpg", "*.jpeg", "*.webp")) -> list[Path]:
    if not charts_dir.is_dir():
        return []
    out: list[Path] = []
    for pat in patterns:
        out.extend(sorted(charts_dir.glob(pat)))
    return sorted(set(out), key=lambda p: p.name)


def chunk_image_paths(paths: list[Path], max_per_chunk: int) -> list[list[Path]]:
    if max_per_chunk <= 0:
        return [paths]
    return [paths[i : i + max_per_chunk] for i in range(0, len(paths), max_per_chunk)]


def chunk_payloads(
    payloads: list[tuple[str, Path]], max_per_chunk: int
) -> list[list[tuple[str, Path]]]:
    if max_per_chunk <= 0:
        return [payloads]
    return [payloads[i : i + max_per_chunk] for i in range(0, len(payloads), max_per_chunk)]
