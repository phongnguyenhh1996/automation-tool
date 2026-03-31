"""Tests for merging repeated Coinmap API array responses by bar timestamp ``t``."""

from automation_tool.coinmap import _merge_coinmap_bar_arrays


def test_merge_dedupes_by_t_later_wins():
    older = [{"t": 100, "c": 1}, {"t": 50, "c": 2}]
    newer_overlap = [{"t": 100, "c": 9}, {"t": 25, "c": 3}]
    out = _merge_coinmap_bar_arrays([older, newer_overlap])
    ts = [x["t"] for x in out]
    assert ts == [100, 50, 25]
    assert next(x for x in out if x["t"] == 100)["c"] == 9


def test_merge_single_list():
    one = [{"t": 10, "x": 1}]
    assert _merge_coinmap_bar_arrays([one]) == one


def test_merge_empty_inputs():
    assert _merge_coinmap_bar_arrays([]) == []
    assert _merge_coinmap_bar_arrays([[]]) == []
