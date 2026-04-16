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

_SIZE = 8
_FMT = "<d"


def _sanitize_symbol(sym: str) -> str:
    s = (sym or "XAUUSD").strip().upper()
    return re.sub(r"[^A-Za-z0-9_]", "_", s)


def shared_memory_name(symbol: str) -> str:
    """Stable name for ``SharedMemory`` (no backslashes; Windows-safe)."""
    return f"automation_last_{_sanitize_symbol(symbol)}"


def open_writer_shared_memory(symbol: str) -> shared_memory.SharedMemory:
    """
    Create or attach writer segment (8 bytes, double). New segments are initialized to NaN.
    """
    name = shared_memory_name(symbol)
    created = False
    try:
        shm = shared_memory.SharedMemory(name=name, create=True, size=_SIZE)
        created = True
    except FileExistsError:
        shm = shared_memory.SharedMemory(name=name, create=False)
    if created:
        struct.pack_into(_FMT, shm.buf, 0, float("nan"))
    return shm


def write_last_price_shared(shm: shared_memory.SharedMemory, price: float) -> None:
    struct.pack_into(_FMT, shm.buf, 0, float(price))


def _get_cached_reader_shm(symbol: str) -> Optional[shared_memory.SharedMemory]:
    name = shared_memory_name(symbol)
    if name in _reader_shm:
        return _reader_shm[name]
    try:
        shm = shared_memory.SharedMemory(name=name, create=False)
    except FileNotFoundError:
        return None
    _reader_shm[name] = shm
    return shm


def read_last_price_shared(symbol: str) -> Optional[float]:
    """
    Read Last from shared memory if segment exists and value is not NaN.
    Returns ``None`` if segment missing or not yet written.
    """
    shm = _get_cached_reader_shm(symbol)
    if shm is None:
        return None
    v = struct.unpack_from(_FMT, shm.buf, 0)[0]
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


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
