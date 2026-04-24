"""
Cross-process Last price via ``multiprocessing.shared_memory`` (same machine).

Python globals are per-process; this is the lightweight IPC substitute for a shared
variable. Optional fallback: :func:`automation_tool.zones_paths.read_last_price_file`.
"""

from __future__ import annotations

import math
import re
import struct
from multiprocessing import shared_memory
from pathlib import Path
from typing import Optional

from automation_tool.zones_paths import read_last_price_file

# Reader-side attach cache (one handle per symbol per process).
_reader_shm: dict[str, shared_memory.SharedMemory] = {}

_SIZE_V1 = 8
_FMT_V1 = "<d"

_PRICES_MAX = 15
# seq:uint64, count:uint32, pad:uint32, prices[15]:double
_FMT_V2 = "<QII" + ("d" * _PRICES_MAX)
_SIZE_V2 = struct.calcsize(_FMT_V2)


def _sanitize_symbol(sym: str) -> str:
    s = (sym or "XAUUSD").strip().upper()
    return re.sub(r"[^A-Za-z0-9_]", "_", s)


def shared_memory_name(symbol: str) -> str:
    """Stable name for ``SharedMemory`` (no backslashes; Windows-safe)."""
    return f"automation_last_{_sanitize_symbol(symbol)}"

def shared_memory_name_v2(symbol: str) -> str:
    """Versioned shared memory name for multi-price buffer."""
    return f"automation_last2_{_sanitize_symbol(symbol)}"


def open_writer_shared_memory(symbol: str) -> shared_memory.SharedMemory:
    """
    Create or attach writer segment (8 bytes, double). New segments are initialized to NaN.
    """
    name = shared_memory_name(symbol)
    created = False
    try:
        shm = shared_memory.SharedMemory(name=name, create=True, size=_SIZE_V1)
        created = True
    except FileExistsError:
        shm = shared_memory.SharedMemory(name=name, create=False)
    if created:
        struct.pack_into(_FMT_V1, shm.buf, 0, float("nan"))
    return shm


def write_last_price_shared(shm: shared_memory.SharedMemory, price: float) -> None:
    """
    Backward-compatible writer for single last price.
    If shm is v2 segment, writes as a 1-item buffer (dedup is the caller's job).
    """
    try:
        if len(shm.buf) >= _SIZE_V2:
            write_last_prices_shared(shm, [float(price)])
            return
    except Exception:
        pass
    struct.pack_into(_FMT_V1, shm.buf, 0, float(price))


def open_writer_shared_memory_v2(symbol: str) -> shared_memory.SharedMemory:
    """
    Create or attach writer segment for multi-price buffer (v2).
    New segments are initialized to seq=0, count=0, prices=NaN.
    """
    name = shared_memory_name_v2(symbol)
    created = False
    try:
        shm = shared_memory.SharedMemory(name=name, create=True, size=_SIZE_V2)
        created = True
    except FileExistsError:
        shm = shared_memory.SharedMemory(name=name, create=False)
    if created:
        buf = [float("nan")] * _PRICES_MAX
        struct.pack_into(_FMT_V2, shm.buf, 0, 0, 0, 0, *buf)
    return shm


def write_last_prices_shared(shm: shared_memory.SharedMemory, prices: list[float]) -> None:
    """
    Write a bounded list of recent prices into v2 shared memory segment.
    Prices are written as-is (caller should apply dedup/trim rules).
    """
    if not prices:
        return
    ps = [float(p) for p in prices[-_PRICES_MAX:]]
    count = len(ps)
    padded = ps + [float("nan")] * (_PRICES_MAX - count)
    try:
        if len(shm.buf) < _SIZE_V2:
            # Not a v2 segment; fall back to v1 single-last write.
            struct.pack_into(_FMT_V1, shm.buf, 0, float(ps[-1]))
            return
    except Exception:
        struct.pack_into(_FMT_V1, shm.buf, 0, float(ps[-1]))
        return
    try:
        seq = struct.unpack_from("<Q", shm.buf, 0)[0]
    except Exception:
        seq = 0
    seq = int(seq) + 1
    struct.pack_into(_FMT_V2, shm.buf, 0, seq, int(count), 0, *padded)


def _get_cached_reader_shm(symbol: str) -> Optional[shared_memory.SharedMemory]:
    # Prefer v2 segment; fallback to v1.
    for name in (shared_memory_name_v2(symbol), shared_memory_name(symbol)):
        if name in _reader_shm:
            return _reader_shm[name]
        try:
            shm = shared_memory.SharedMemory(name=name, create=False)
        except FileNotFoundError:
            continue
        _reader_shm[name] = shm
        return shm
    return None


def read_last_prices_shared(symbol: str) -> Optional[tuple[int, list[float]]]:
    """
    Read (seq, prices[]) from shared memory v2 segment.
    Returns None if segment missing or not yet written.
    """
    name = shared_memory_name_v2(symbol)
    shm = _reader_shm.get(name)
    if shm is None:
        try:
            shm = shared_memory.SharedMemory(name=name, create=False)
        except FileNotFoundError:
            return None
        _reader_shm[name] = shm
    try:
        if len(shm.buf) < _SIZE_V2:
            return None
        seq, count, _pad, *vals = struct.unpack_from(_FMT_V2, shm.buf, 0)
        if int(count) <= 0:
            return None
        out: list[float] = []
        for v in vals[: int(count)]:
            if isinstance(v, float) and math.isnan(v):
                continue
            out.append(float(v))
        if not out:
            return None
        return int(seq), out
    except Exception:
        return None


def read_last_price_shared(symbol: str) -> Optional[float]:
    """
    Read Last from shared memory if segment exists and value is not NaN.
    Returns ``None`` if segment missing or not yet written.
    """
    v2 = read_last_prices_shared(symbol)
    if v2 is not None:
        _seq, ps = v2
        if ps:
            return float(ps[-1])
    shm = _get_cached_reader_shm(symbol)
    if shm is None:
        return None
    try:
        v = struct.unpack_from(_FMT_V1, shm.buf, 0)[0]
    except Exception:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return float(v)


def read_last_prices_for_daemon_plan(
    symbol: str,
    file_path: Optional[Path] = None,
) -> tuple[Optional[int], list[float]]:
    """
    Prefer shared memory v2 (15-price buffer). Fallback to v1 shared memory, then optional last.txt.
    Returns (seq, prices). seq is None when not sourced from v2.
    """
    v2 = read_last_prices_shared(symbol)
    if v2 is not None:
        return v2[0], list(v2[1])
    p1 = read_last_price_shared(symbol)
    if p1 is not None:
        return None, [float(p1)]
    pfile = read_last_price_file(file_path, symbol=symbol)
    if pfile is not None:
        return None, [float(pfile)]
    return None, []


def read_last_price_for_daemon_plan(
    symbol: str,
    file_path: Optional[Path] = None,
) -> Optional[float]:
    """
    Prefer shared memory (daemon giá), then optional ``last.txt`` fallback.
    """
    p = read_last_price_shared(symbol)
    if p is not None:
        return p
    return read_last_price_file(file_path, symbol=symbol)
