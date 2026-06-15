from automation_tool.coinmap import _coinmap_effective_chart_cd


def test_chart_view_by_symbol_merges_usdindex_override() -> None:
    cd = {
        "chart_price_edge_pan_end_y_ratio": 0.33,
        "chart_view_by_symbol": {
            "USDINDEX": {"chart_price_edge_pan_end_y_ratio": 0.2},
        },
    }
    out = _coinmap_effective_chart_cd(cd, symbol="USDINDEX", export_symbol="DXY")
    assert out["chart_price_edge_pan_end_y_ratio"] == 0.2


def test_chart_view_by_symbol_keeps_default_for_other_symbols() -> None:
    cd = {
        "chart_price_edge_pan_end_y_ratio": 0.33,
        "chart_view_by_symbol": {
            "USDINDEX": {"chart_price_edge_pan_end_y_ratio": 0.2},
        },
    }
    out = _coinmap_effective_chart_cd(cd, symbol="XAUUSD")
    assert out["chart_price_edge_pan_end_y_ratio"] == 0.33


def test_chart_view_by_symbol_matches_export_symbol_key() -> None:
    cd = {
        "chart_price_edge_pan_end_y_ratio": 0.33,
        "chart_view_by_symbol": {
            "DXY": {"chart_price_edge_pan_end_y_ratio": 0.2},
        },
    }
    out = _coinmap_effective_chart_cd(cd, symbol="USDINDEX", export_symbol="DXY")
    assert out["chart_price_edge_pan_end_y_ratio"] == 0.2
