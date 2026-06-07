from __future__ import annotations

import base64
import mimetypes
import re
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Optional, Tuple, Union

# OpenAI multimodal slot: ``json`` → Path; ``image`` → PNG on disk; ``image_url`` → https string.
ChartOpenAIPayload = Tuple[str, Union[Path, str]]

# Per-charts marker; global active pair is ``data/.main_chart_symbol`` (see ``get_active_main_symbol``).
MAIN_CHART_SYMBOL_FILENAME = ".main_chart_symbol"
GLOBAL_MAIN_CHART_SYMBOL_FILENAME = ".main_chart_symbol"
DEFAULT_MAIN_CHART_SYMBOL = "XAUUSD"

GC_MANUAL_URL_M15 = "gc_m15.url"
GC_MANUAL_URL_M5 = "gc_m5.url"
GC_MODE_MARKER = ".gc_mode"


def normalize_main_chart_symbol(s: str) -> str:
    """Uppercase forex/crypto pair id for filenames (watchlist id on Coinmap / TV label)."""
    t = (s or "").strip().upper()
    if not re.match(r"^[A-Z0-9]{4,16}$", t):
        raise ValueError(
            f"main symbol must be 4-16 letters/digits (e.g. XAUUSD, USDJPY), got {s!r}"
        )
    return t


def get_active_main_symbol() -> str:
    """
    Active instrument for ``data/{{SYM}}/`` layout.

    1. ``AUTOMATION_MAIN_SYMBOL`` env
    2. ``data/.main_chart_symbol`` (written by capture / set_active_main_symbol_file)
    3. Legacy ``data/charts/.main_chart_symbol`` (pre per-symbol dirs)
    4. ``DEFAULT_MAIN_CHART_SYMBOL``
    """
    import os

    from automation_tool.config import default_data_dir

    env = (os.getenv("AUTOMATION_MAIN_SYMBOL") or "").strip()
    if env:
        try:
            return normalize_main_chart_symbol(env)
        except ValueError:
            pass

    root = default_data_dir()
    for rel in (GLOBAL_MAIN_CHART_SYMBOL_FILENAME,):
        marker = root / rel
        if marker.is_file():
            try:
                line = marker.read_text(encoding="utf-8").strip().splitlines()
                raw = line[0] if line else ""
                if raw:
                    return normalize_main_chart_symbol(raw)
            except (OSError, UnicodeError, ValueError):
                pass

    legacy = root / "charts" / MAIN_CHART_SYMBOL_FILENAME
    if legacy.is_file():
        try:
            line = legacy.read_text(encoding="utf-8").strip().splitlines()
            raw = line[0] if line else ""
            if raw:
                return normalize_main_chart_symbol(raw)
        except (OSError, UnicodeError, ValueError):
            pass

    return DEFAULT_MAIN_CHART_SYMBOL


def set_active_main_symbol_file(main_chart_symbol: Optional[str]) -> None:
    """
    Global pointer ``data/.main_chart_symbol`` so ``default_charts_dir()`` resolves to
    ``data/{{SYM}}/charts/``. Pass ``None`` to remove (active symbol defaults to XAUUSD).
    """
    from automation_tool.config import default_data_dir

    root = default_data_dir()
    marker = root / GLOBAL_MAIN_CHART_SYMBOL_FILENAME
    if main_chart_symbol is not None and str(main_chart_symbol).strip():
        sym = normalize_main_chart_symbol(main_chart_symbol)
        root.mkdir(parents=True, exist_ok=True)
        marker.write_text(sym + "\n", encoding="utf-8")
        try:
            from automation_tool.browser_client import try_tv_prewarm_reset

            try_tv_prewarm_reset(sym)
        except Exception:
            pass
    else:
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def chart_image_order_for_main_symbol(main_sym: str) -> tuple[tuple[str, str, str], ...]:
    """
    Filenames: ``{{stamp}}_tradingview_{{SYMBOL}}_{{interval}}.url`` (https, one line) or ``.png`` / ``coinmap_…``.
    ``main_sym`` replaces the default XAUUSD block (DXY TV block unchanged).

    **11 slots (default full-analysis set):** DXY TV H4/H1/M15 → main TV H4/H1/M15
    → main TV M15 Session Liquidity Check / ICT Killzones → main TV M5
    → Coinmap DXY footprint M15 → Coinmap main M15/M5.
    """
    m = normalize_main_chart_symbol(main_sym)
    return (
        ("tradingview", "DXY", "4h"),
        ("tradingview", "DXY", "1h"),
        ("tradingview", "DXY", "15m"),
        ("tradingview", m, "4h"),
        ("tradingview", m, "1h"),
        ("tradingview", m, "15m"),
        ("tradingview", m, "15m_ict"),
        ("tradingview", m, "5m"),
        ("coinmap", "DXY", "15m"),
        ("coinmap", m, "15m"),
        ("coinmap", m, "5m"),
    )


# Backward compat: default order equals XAUUSD main pair.
CHART_IMAGE_ORDER: tuple[tuple[str, str, str], ...] = chart_image_order_for_main_symbol(
    DEFAULT_MAIN_CHART_SYMBOL
)

# Number of multimodal slots (must match ``chart_image_order_for_main_symbol`` length).
CHART_SLOT_COUNT = len(CHART_IMAGE_ORDER)


def openai_payload_max_for_order(
    order: tuple[tuple[str, str, str], ...],
) -> int:
    """Upper bound when each Coinmap slot sends JSON + PNG alongside other slot payloads."""
    return len(order) + sum(1 for src, _, _ in order if src == "coinmap")


# Default ``--max-images-per-call`` for full analysis (11 slots + 3 Coinmap PNG extras).
OPENAI_PAYLOAD_MAX = openai_payload_max_for_order(CHART_IMAGE_ORDER)


def chart_image_order_for_gc(main_sym: str) -> tuple[tuple[str, str, str], ...]:
    """
    ``all --gc``: TradingView slots unchanged; Coinmap replaced by manual GC Futures URLs.

    **10 slots:** DXY TV H4/H1/M15 → main TV H4/H1/M15/M15 ICT/M5 → GC M15 + M5 (``gc_*.url``).
    """
    m = normalize_main_chart_symbol(main_sym)
    return (
        ("tradingview", "DXY", "4h"),
        ("tradingview", "DXY", "1h"),
        ("tradingview", "DXY", "15m"),
        ("tradingview", m, "4h"),
        ("tradingview", m, "1h"),
        ("tradingview", m, "15m"),
        ("tradingview", m, "15m_ict"),
        ("tradingview", m, "5m"),
        ("gc_url", "GC", "15m"),
        ("gc_url", "GC", "5m"),
    )


GC_CHART_SLOT_COUNT = len(chart_image_order_for_gc(DEFAULT_MAIN_CHART_SYMBOL))
GC_OPENAI_PAYLOAD_MAX = openai_payload_max_for_order(
    chart_image_order_for_gc(DEFAULT_MAIN_CHART_SYMBOL)
)


def read_main_chart_symbol(charts_dir: Optional[Path] = None) -> str:
    """
    Main pair for filename slots.

    If ``charts_dir`` is set: read that directory's ``.main_chart_symbol`` if present,
    else ``DEFAULT_MAIN_CHART_SYMBOL`` (no mixing with global ``data/.main_chart_symbol``).

    If ``charts_dir`` is ``None``: :func:`get_active_main_symbol`.
    """
    if charts_dir is not None:
        marker = charts_dir / MAIN_CHART_SYMBOL_FILENAME
        if marker.is_file():
            try:
                line = marker.read_text(encoding="utf-8").strip().splitlines()
                raw = line[0] if line else ""
                if raw:
                    return normalize_main_chart_symbol(raw)
            except (OSError, UnicodeError, ValueError):
                pass
        return DEFAULT_MAIN_CHART_SYMBOL
    return get_active_main_symbol()


def write_main_chart_symbol_marker(charts_dir: Path, symbol: str) -> None:
    """Persist main pair so OpenAI ordering matches captured filenames."""
    sym = normalize_main_chart_symbol(symbol)
    charts_dir.mkdir(parents=True, exist_ok=True)
    (charts_dir / MAIN_CHART_SYMBOL_FILENAME).write_text(sym + "\n", encoding="utf-8")


def clear_main_chart_symbol_marker(charts_dir: Path) -> None:
    """Remove marker so consumers use default XAUUSD (yaml default capture)."""
    p = charts_dir / MAIN_CHART_SYMBOL_FILENAME
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def is_gc_mode(charts_dir: Path) -> bool:
    """True when ``charts_dir`` has a ``.gc_mode`` marker (``all --gc`` batch)."""
    return (charts_dir / GC_MODE_MARKER).is_file()


def write_gc_mode_marker(charts_dir: Path) -> None:
    """Mark charts_dir as GC manual-URL mode for ordering, validation, and prompts."""
    charts_dir.mkdir(parents=True, exist_ok=True)
    (charts_dir / GC_MODE_MARKER).write_text("1\n", encoding="utf-8")


def _gc_manual_url_path(charts_dir: Path, which: Literal["m15", "m5"]) -> Path:
    name = GC_MANUAL_URL_M15 if which == "m15" else GC_MANUAL_URL_M5
    return charts_dir / name


def _read_url_first_line(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        return None
    line = (raw[0] if raw else "").strip()
    if line.startswith("http://") or line.startswith("https://"):
        return line
    return None


def read_gc_manual_url(charts_dir: Path, which: Literal["m15", "m5"]) -> Optional[str]:
    """First https line from ``gc_m15.url`` or ``gc_m5.url``, or ``None``."""
    return _read_url_first_line(_gc_manual_url_path(charts_dir, which))


def ensure_gc_manual_url_placeholders(charts_dir: Path) -> tuple[Path, Path]:
    """Create empty ``gc_m15.url`` / ``gc_m5.url`` if missing (user fills with screenshot URLs)."""
    charts_dir.mkdir(parents=True, exist_ok=True)
    p15 = _gc_manual_url_path(charts_dir, "m15")
    p5 = _gc_manual_url_path(charts_dir, "m5")
    for p in (p15, p5):
        if not p.is_file():
            p.write_text("", encoding="utf-8")
    return p15, p5


def wait_for_gc_manual_urls(charts_dir: Path, *, poll_seconds: float = 30.0) -> None:
    """
    Block until both GC manual URL files have a valid https first line.
    Polls indefinitely (``poll_seconds`` between checks).
    """
    poll = max(1.0, float(poll_seconds))
    p15, p5 = ensure_gc_manual_url_placeholders(charts_dir)
    while True:
        u15 = read_gc_manual_url(charts_dir, "m15")
        u5 = read_gc_manual_url(charts_dir, "m5")
        if u15 and u5:
            return
        missing: list[str] = []
        if not u15:
            missing.append(p15.name)
        if not u5:
            missing.append(p5.name)
        print(
            f"Chờ URL GC Vàng Futures: dán https vào {', '.join(missing)} "
            f"trong {charts_dir} (poll {poll:.0f}s)...",
            flush=True,
            file=sys.stderr,
        )
        time.sleep(poll)


def effective_chart_image_order(charts_dir: Path) -> tuple[tuple[str, str, str], ...]:
    if is_gc_mode(charts_dir):
        return chart_image_order_for_gc(read_main_chart_symbol(charts_dir))
    return chart_image_order_for_main_symbol(read_main_chart_symbol(charts_dir))

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
    for p in charts_dir.glob("*.url"):
        m = _STAMP_RE.match(p.name)
        if m:
            stamps.add(m.group(1))
    return max(stamps) if stamps else None


def stamp_from_capture_paths(paths: Sequence[Path]) -> Optional[str]:
    """Largest ``YYYYMMDD_HHMMSS`` prefix found on capture artifact filenames (e.g. returned by ``capture_charts``)."""
    stamps: set[str] = set()
    for p in paths:
        m = _STAMP_RE.match(p.name)
        if m:
            stamps.add(m.group(1))
    return max(stamps) if stamps else None


def coinmap_main_pair_interval_json_path(
    charts_dir: Path, interval: str, *, stamp: Optional[str] = None
) -> Optional[Path]:
    """``{{stamp}}_coinmap_{{main_pair}}_{interval}.json`` (main pair from marker; ``interval`` e.g. ``5m``, ``15m``)."""
    sym = read_main_chart_symbol(charts_dir)
    iv = (interval or "").strip()
    if not iv:
        return None
    st = stamp or latest_chart_stamp(charts_dir)
    if not st:
        return None
    p = charts_dir / f"{st}_coinmap_{sym}_{iv}.json"
    return p if p.is_file() else None


def coinmap_main_pair_5m_json_path(
    charts_dir: Path, *, stamp: Optional[str] = None
) -> Optional[Path]:
    """Latest ``{{stamp}}_coinmap_{{main_pair}}_5m.json`` (main pair from marker or XAUUSD)."""
    return coinmap_main_pair_interval_json_path(charts_dir, "5m", stamp=stamp)


def coinmap_xauusd_5m_json_path(
    charts_dir: Path, *, stamp: Optional[str] = None
) -> Optional[Path]:
    """Backward compat: same as ``coinmap_main_pair_5m_json_path``."""
    return coinmap_main_pair_5m_json_path(charts_dir, stamp=stamp)


def coinmap_merged_openai_files(
    charts_dir: Path, stamp: str, main_sym: str
) -> tuple[Optional[Path], Optional[Path]]:
    """
    If present, paths to DXY and main-pair ``*_coinmap_*_merged.json`` (``coinmap_merged``).
    """
    m = (main_sym or "").strip() or DEFAULT_MAIN_CHART_SYMBOL
    dxy = charts_dir / f"{stamp}_coinmap_DXY_merged.json"
    mainp = charts_dir / f"{stamp}_coinmap_{m}_merged.json"
    d_ok = dxy if dxy.is_file() else None
    m_ok = mainp if mainp.is_file() else None
    return d_ok, m_ok


def _append_coinmap_openai_payloads(
    out: list[ChartOpenAIPayload],
    *,
    charts_dir: Path,
    stamp: str,
    sym: str,
    iv: str,
    json_path: Optional[Path] = None,
) -> None:
    """Append Coinmap JSON and/or PNG for one slot (both when present)."""
    jp = (
        json_path
        if json_path is not None
        else charts_dir / f"{stamp}_coinmap_{sym}_{iv}.json"
    )
    pp = charts_dir / f"{stamp}_coinmap_{sym}_{iv}.png"
    if jp.is_file():
        out.append(("json", jp))
    if pp.is_file():
        out.append(("image", pp))


def ordered_chart_openai_payloads(
    charts_dir: Path, *, stamp: Optional[str] = None
) -> list[ChartOpenAIPayload]:
    """
    Same slot order as ``effective_chart_image_order(charts_dir)`` (for OpenAI step 2).

    * **TradingView** — prefer ``.json`` (tvdatafeed OHLC) else ``.url`` (snapshot) else ``.png``.
    * **Coinmap** — attach ``.json`` (API export) when present, and **also** ``.png`` when present
      (JSON-only still works when screenshots are disabled).
    * When **merged** files exist (see :func:`coinmap_merged_openai_files`), DXY 15m uses
      ``DXY_merged.json``; main M15 + M5 collapse to a single ``{MAIN}_merged.json`` attachment
      (merged main M5 JSON slot skipped; PNG for M5 still attached when on disk).
    """
    if not charts_dir.is_dir():
        return []
    st = stamp or latest_chart_stamp(charts_dir)
    if not st:
        return []
    main_sym = read_main_chart_symbol(charts_dir)
    dxy_merged, main_merged = coinmap_merged_openai_files(charts_dir, st, main_sym)
    order = effective_chart_image_order(charts_dir)
    out: list[ChartOpenAIPayload] = []
    for src, sym, iv in order:
        if src == "gc_url":
            which: Literal["m15", "m5"] = "m15" if iv == "15m" else "m5"
            line = read_gc_manual_url(charts_dir, which)
            if line:
                out.append(("image_url", line))
        elif src == "coinmap":
            if dxy_merged is not None and sym == "DXY" and iv == "15m":
                _append_coinmap_openai_payloads(
                    out,
                    charts_dir=charts_dir,
                    stamp=st,
                    sym=sym,
                    iv=iv,
                    json_path=dxy_merged,
                )
                continue
            if main_merged is not None and sym == main_sym and iv == "15m":
                _append_coinmap_openai_payloads(
                    out,
                    charts_dir=charts_dir,
                    stamp=st,
                    sym=sym,
                    iv=iv,
                    json_path=main_merged,
                )
                continue
            if main_merged is not None and sym == main_sym and iv == "5m":
                _append_coinmap_openai_payloads(
                    out, charts_dir=charts_dir, stamp=st, sym=sym, iv=iv, json_path=None
                )
                continue
            _append_coinmap_openai_payloads(
                out, charts_dir=charts_dir, stamp=st, sym=sym, iv=iv
            )
        else:
            jp = charts_dir / f"{st}_tradingview_{sym}_{iv}.json"
            up = charts_dir / f"{st}_tradingview_{sym}_{iv}.url"
            pp = charts_dir / f"{st}_tradingview_{sym}_{iv}.png"
            if jp.is_file():
                out.append(("json", jp))
            elif up.is_file():
                raw = up.read_text(encoding="utf-8").strip().splitlines()
                line = (raw[0] if raw else "").strip()
                if line.startswith("http://") or line.startswith("https://"):
                    out.append(("image_url", line))
                elif pp.is_file():
                    out.append(("image", pp))
            elif pp.is_file():
                out.append(("image", pp))
    return out


def ordered_chart_images(charts_dir: Path, *, stamp: Optional[str] = None) -> list[Path]:
    """
    Return chart paths in analysis order (DXY TV H4/H1/M15 → main TV H4/H1/M15/M5 → DXY Coinmap M15
    → main Coinmap M15/M5).
    Only includes files that exist. Uses latest stamp in directory when ``stamp`` is omitted.
    """
    if not charts_dir.is_dir():
        return []
    st = stamp or latest_chart_stamp(charts_dir)
    if not st:
        return []
    order = effective_chart_image_order(charts_dir)
    out: list[Path] = []
    for src, sym, iv in order:
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
    payloads: list[ChartOpenAIPayload], max_per_chunk: int
) -> list[list[ChartOpenAIPayload]]:
    if max_per_chunk <= 0:
        return [payloads]
    return [payloads[i : i + max_per_chunk] for i in range(0, len(payloads), max_per_chunk)]
