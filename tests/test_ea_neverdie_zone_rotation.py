from pathlib import Path


EA_SOURCE = Path(__file__).resolve().parents[1] / "EA_Zone_NeverDie.mq5"


def _source() -> str:
    return EA_SOURCE.read_text()


def _function_body(source: str, name: str) -> str:
    marker = f"void {name}("
    start = source.index(marker)
    brace_start = source.index("{", start)
    depth = 0

    for pos in range(brace_start, len(source)):
        char = source[pos]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[brace_start : pos + 1]

    raise AssertionError(f"Could not parse function body for {name}")


def test_ea_no_longer_blocks_trade_sides() -> None:
    calls = [
        line.strip()
        for line in _source().splitlines()
        if "BlockSide(" in line and not line.strip().startswith("void BlockSide(")
    ]

    assert calls == []


def test_watch_zone_activation_allows_only_one_trade_zone_globally() -> None:
    source = _source()
    activate_body = _function_body(source, "ActivateWatchZones")
    single_activate_body = _function_body(source, "ActivateSingleWatchZone")

    assert "ActivateSingleWatchZone" in activate_body
    assert "g_buyZones[i].mode = ZONE_WATCH" in activate_body
    assert "g_sellZones[i].mode = ZONE_WATCH" in activate_body
    assert "mode = ZONE_TRADE" in single_activate_body
    assert "ActivateSideWatchZones(" not in source


def test_zone_turns_off_after_stop_loss_or_take_profit() -> None:
    source = _source()
    stops_body = _function_body(source, "ManageZoneStopsAndWatchers")
    trade_tx_body = _function_body(source, "OnTradeTransaction")
    manage_body = _function_body(source, "ManageAllZones")

    assert "DeactivateZoneByMagic" in source
    assert "DeactivateZoneByMagic(POSITION_TYPE_BUY, g_buyZones[i].magic)" in stops_body
    assert "DeactivateZoneByMagic(POSITION_TYPE_SELL, g_sellZones[i].magic)" in stops_body
    assert "DeactivateZoneByMagic(POSITION_TYPE_BUY, magic)" in trade_tx_body
    assert "DeactivateZoneByMagic(POSITION_TYPE_SELL, magic)" in trade_tx_body
    assert "DeactivateZoneByMagic(side, zone.magic)" in manage_body


def test_ea_draws_two_zone_lines_with_zone_color() -> None:
    source = _source()
    update_chart_body = _function_body(source, "UpdateZoneChartObjects")
    draw_body = _function_body(source, "DrawZoneLines")
    line_body = _function_body(source, "DrawZoneLine")

    assert 'OBJ_HLINE' in line_body
    assert 'OBJPROP_PRICE' in line_body
    assert 'DrawZoneLine(lowName, zone.low, zoneColor' in draw_body
    assert 'DrawZoneLine(highName, zone.high, zoneColor' in draw_body
    assert 'DrawZoneLineLabel(lowLabelName, zone.low, zoneColor' in draw_body
    assert 'DrawZoneLineLabel(highLabelName, zone.high, zoneColor' in draw_body
    assert 'ObjectSetInteger(0, lowName, OBJPROP_COLOR, zoneColor)' in draw_body
    assert 'ObjectSetInteger(0, highName, OBJPROP_COLOR, zoneColor)' in draw_body
    assert 'DrawZoneLines(POSITION_TYPE_BUY, g_buyZones[i], i, ZoneGlobalIndex(POSITION_TYPE_BUY, i))' in update_chart_body
    assert 'DrawZoneLines(POSITION_TYPE_SELL, g_sellZones[i], i, ZoneGlobalIndex(POSITION_TYPE_SELL, i))' in update_chart_body
    assert 'ChartRedraw(0)' in update_chart_body


def test_ea_uses_zone_color_for_panel_rows() -> None:
    source = _source()
    rows_body = _function_body(source, "AddZoneDetailRows")

    assert 'color rowColor = ZoneDisplayColor(ZoneGlobalIndex(side, bestIndex));' in rows_body


def test_ea_refreshes_zone_chart_objects_during_lifecycle() -> None:
    source = _source()
    deinit_body = _function_body(source, "OnDeinit")
    update_panel_body = _function_body(source, "UpdatePanel")

    assert 'UpdateZoneChartObjects();' in update_panel_body
    assert 'RemoveZoneChartObjects();' in deinit_body
