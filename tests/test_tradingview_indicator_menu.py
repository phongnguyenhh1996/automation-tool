from automation_tool.coinmap import _tradingview_is_delete_indicator_label


def test_delete_indicator_label_accepts_chart_delete_actions():
    assert _tradingview_is_delete_indicator_label("Xóa 1 chỉ báo")
    assert _tradingview_is_delete_indicator_label("Xoá 2 chỉ báo")
    assert _tradingview_is_delete_indicator_label("Remove 1 indicator")


def test_delete_indicator_label_rejects_favorite_actions():
    assert not _tradingview_is_delete_indicator_label("Xóa chỉ báo này khỏi mục yêu thích")
    assert not _tradingview_is_delete_indicator_label("Remove this indicator from favorites")

