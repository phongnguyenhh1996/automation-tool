from __future__ import annotations

import base64
import mimetypes
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Optional, Tuple, Union

# OpenAI multimodal slot: ``json`` → Path; ``image`` → PNG on disk; ``image_url`` → https string.
ChartOpenAIPayload = Tuple[str, Union[Path, str]]

# Per-charts marker; global active pair is ``data/.main_chart_symbol`` (see ``get_active_main_symbol``).
MAIN_CHART_SYMBOL_FILENAME = ".main_chart_symbol"
GLOBAL_MAIN_CHART_SYMBOL_FILENAME = ".main_chart_symbol"
DEFAULT_MAIN_CHART_SYMBOL = "XAUUSD"


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

    **10 slots (default full-analysis set):** DXY TV H4/H1/M15 → main TV H4/H1/M15/M5 → Coinmap DXY
    footprint M15 → Coinmap main M15/M5.
    """
    m = normalize_main_chart_symbol(main_sym)
    return (
        ("tradingview", "DXY", "4h"),
        ("tradingview", "DXY", "1h"),
        ("tradingview", "DXY", "15m"),
        ("tradingview", m, "4h"),
        ("tradingview", m, "1h"),
        ("tradingview", m, "15m"),
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


def effective_chart_image_order(charts_dir: Path) -> tuple[tuple[str, str, str], ...]:
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


def ordered_chart_openai_payloads(
    charts_dir: Path, *, stamp: Optional[str] = None
) -> list[ChartOpenAIPayload]:
    """
    Same slot order as ``effective_chart_image_order(charts_dir)`` (for OpenAI step 2).

    * **TradingView** — prefer ``.json`` (tvdatafeed OHLC) else ``.url`` (snapshot) else ``.png``.
    * **Coinmap** — prefer ``.json`` (API export) over ``.png`` so analysis can run
      without screenshots while keeping the same ordering as when images were used.
    * When **merged** files exist (see :func:`coinmap_merged_openai_files`), DXY 15m uses
      ``DXY_merged.json``; main M15 + M5 collapse to a single ``{MAIN}_merged.json`` attachment
      (9 total payloads with both merges vs 10 with raw per-TF).
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
        if src == "coinmap":
            if dxy_merged is not None and sym == "DXY" and iv == "15m":
                out.append(("json", dxy_merged))
                continue
            if main_merged is not None and sym == main_sym and iv == "15m":
                out.append(("json", main_merged))
                continue
            if main_merged is not None and sym == main_sym and iv == "5m":
                continue
            jp = charts_dir / f"{st}_coinmap_{sym}_{iv}.json"
            pp = charts_dir / f"{st}_coinmap_{sym}_{iv}.png"
            if jp.is_file():
                out.append(("json", jp))
            elif pp.is_file():
                out.append(("image", pp))
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
