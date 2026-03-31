"""Filter merged API arrays by chart step symbol/interval."""

from automation_tool.coinmap import _filter_coinmap_api_array_by_step


def test_filters_other_interval():
    rows = [
        {"i": "5m", "s": "XAUUSD", "t": 1},
        {"i": "15m", "s": "XAUUSD", "t": 2},
        {"i": "5m", "s": "XAUUSD", "t": 3},
    ]
    out = _filter_coinmap_api_array_by_step(rows, symbol="XAUUSD", interval="5m")
    assert [x["t"] for x in out] == [1, 3]


def test_symbol_mismatch_dropped_when_symbol_set():
    rows = [
        {"i": "5m", "s": "XAUUSD", "t": 1},
        {"i": "5m", "s": "EURUSD", "t": 2},
    ]
    out = _filter_coinmap_api_array_by_step(rows, symbol="XAUUSD", interval="5m")
    assert len(out) == 1 and out[0]["t"] == 1


def test_no_interval_returns_unchanged_list_ref():
    rows = [{"i": "5m", "t": 1}]
    assert _filter_coinmap_api_array_by_step(rows, symbol="X", interval=None) is rows


def test_relax_symbol_keeps_interval_when_strict_empty():
    rows = [{"i": "5m", "s": "ALT", "t": 1}]
    strict = _filter_coinmap_api_array_by_step(
        rows, symbol="XAUUSD", interval="5m", relax_symbol_if_empty=False
    )
    assert strict == []
    relaxed = _filter_coinmap_api_array_by_step(
        rows, symbol="XAUUSD", interval="5m", relax_symbol_if_empty=True
    )
    assert len(relaxed) == 1 and relaxed[0]["t"] == 1
