import pytest

from automation_tool.tv_watchlist_daemon import _source_blocks_scalp_entry
from automation_tool.zones_state import Zone


@pytest.mark.parametrize("source", ["all", "update", " ALL ", " Update "])
def test_source_blocks_scalp_entry_for_all_and_update(source: str) -> None:
    z = Zone(
        id="scalp__sang",
        label="scalp",
        vung_cho="100-101",
        side="BUY",
        source=source,
    )

    assert _source_blocks_scalp_entry(z) is True


@pytest.mark.parametrize("source", ["update-scalp", "", "manual"])
def test_source_allows_scalp_entry_for_non_all_update_sources(source: str) -> None:
    z = Zone(
        id="scalp__sang",
        label="scalp_1",
        vung_cho="100-101",
        side="BUY",
        source=source,
    )

    assert _source_blocks_scalp_entry(z) is False


def test_source_allows_plan_entry_for_all_update_sources() -> None:
    z = Zone(
        id="plan_chinh__sang",
        label="plan_chinh",
        vung_cho="100-101",
        side="BUY",
        source="all",
    )

    assert _source_blocks_scalp_entry(z) is False
