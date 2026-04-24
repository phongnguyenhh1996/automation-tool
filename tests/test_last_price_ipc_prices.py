from __future__ import annotations

from automation_tool.last_price_ipc import (
    _PRICES_MAX,
    _SIZE_V2,
    read_last_price_shared,
    read_last_prices_shared,
    shared_memory_name_v2,
    write_last_prices_shared,
)


class _FakeShm:
    def __init__(self, size: int) -> None:
        self.buf = bytearray(size)


def test_last_price_ipc_v2_roundtrip_and_trim() -> None:
    sym = "TEST_XAUUSD"
    shm = _FakeShm(_SIZE_V2)
    prices = [1000.0 + float(i) for i in range(_PRICES_MAX + 5)]
    write_last_prices_shared(shm, prices)

    # Inject fake shm into module reader cache by using the v2 name key.
    # (The implementation caches by name; we mimic that by directly calling the reader on the same symbol
    # after placing a real v2 segment is not possible in sandboxed CI environments.)
    from automation_tool import last_price_ipc as ipc  # local import to mutate module state

    ipc._reader_shm[shared_memory_name_v2(sym)] = shm  # type: ignore[assignment]

    got = read_last_prices_shared(sym)
    assert got is not None
    seq, arr = got
    assert isinstance(seq, int)
    assert len(arr) == _PRICES_MAX
    assert arr == prices[-_PRICES_MAX:]
    assert read_last_price_shared(sym) == prices[-1]


def test_last_price_ipc_v2_seq_increments() -> None:
    sym = "TEST_SEQ"
    shm = _FakeShm(_SIZE_V2)
    from automation_tool import last_price_ipc as ipc  # local import to mutate module state

    ipc._reader_shm[shared_memory_name_v2(sym)] = shm  # type: ignore[assignment]

    write_last_prices_shared(shm, [1.0])
    a = read_last_prices_shared(sym)
    assert a is not None
    seq1, arr1 = a
    assert arr1 == [1.0]

    write_last_prices_shared(shm, [1.0, 2.0])
    b = read_last_prices_shared(sym)
    assert b is not None
    seq2, arr2 = b
    assert seq2 > seq1
    assert arr2 == [1.0, 2.0]


def test_last_price_ipc_v2_name_is_versioned() -> None:
    assert shared_memory_name_v2("XAUUSD").startswith("automation_last2_")

