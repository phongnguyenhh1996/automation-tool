import re
from pathlib import Path


EA_SOURCE = Path(__file__).resolve().parents[1] / "EA_Zone_NeverDie.mq5"


def _source() -> str:
    return EA_SOURCE.read_text()


def _function_body(source: str, name: str) -> str:
    match = re.search(rf"(?m)^[A-Za-z_][A-Za-z0-9_<>&:\s*]*\b{name}\(", source)
    if match is None:
        raise AssertionError(f"Could not find function definition for {name}")
    start = match.start()
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
    retire_body = _function_body(source, "RemoveOrRetireActiveTradeZonesExcept")

    assert "ActivateSingleWatchZone" in activate_body
    assert "RemoveOrRetireActiveTradeZonesExcept(activatedSide, activatedMagic)" in activate_body
    assert "ArrayRemove(g_buyZones, i, 1)" in retire_body
    assert "ArrayRemove(g_sellZones, i, 1)" in retire_body
    assert "AddRetiredZone(POSITION_TYPE_BUY, g_buyZones[i])" in retire_body
    assert "AddRetiredZone(POSITION_TYPE_SELL, g_sellZones[i])" in retire_body
    assert "mode = ZONE_TRADE" in single_activate_body
    assert "ActivateSideWatchZones(" not in source


def test_watch_zone_activation_only_triggers_when_entering_activation_band() -> None:
    source = _source()
    should_activate_body = _function_body(source, "ShouldActivateWatchZone")
    refresh_body = _function_body(source, "RefreshActivationBandState")
    activate_body = _function_body(source, "ActivateWatchZones")

    assert "bool           wasInActivationBand;" in source
    assert "if(zone.wasInActivationBand) return(false);" in should_activate_body
    assert (
        "g_buyZones[i].wasInActivationBand = "
        "IsPriceInActivationBand(POSITION_TYPE_BUY, g_buyZones[i], price);"
    ) in refresh_body
    assert (
        "g_sellZones[i].wasInActivationBand = "
        "IsPriceInActivationBand(POSITION_TYPE_SELL, g_sellZones[i], price);"
    ) in refresh_body
    assert "RefreshActivationBandState(price);" in activate_body


def test_zone_is_removed_after_stop_loss_but_not_unconditionally_after_take_profit() -> None:
    source = _source()
    stops_body = _function_body(source, "ManageZoneStopsAndWatchers")
    trade_tx_body = _function_body(source, "OnTradeTransaction")
    manage_body = _function_body(source, "ManageAllZones")
    remove_body = _function_body(source, "RemoveZoneByMagic")

    assert "RemoveZoneByMagic" in source
    assert "ArrayRemove(g_buyZones, i, 1)" in remove_body
    assert "ArrayRemove(g_sellZones, i, 1)" in remove_body
    assert "RemoveZoneByMagic(POSITION_TYPE_BUY, g_buyZones[i].magic)" in stops_body
    assert "RemoveZoneByMagic(POSITION_TYPE_SELL, g_sellZones[i].magic)" in stops_body
    assert "RemoveZoneByMagic(POSITION_TYPE_BUY, magic)" in trade_tx_body
    assert "RemoveZoneByMagic(POSITION_TYPE_SELL, magic)" in trade_tx_body
    assert "ShouldDeactivateAfterBasketTakeProfit(side, zone, GetSideClosePrice(side, tick))" in manage_body
    assert "CloseBasket(side, zone.magic);\n            RemoveZoneByMagic(side, zone.magic);" not in manage_body


def test_stop_loss_blocks_zone_from_being_reloaded_from_json() -> None:
    source = _source()
    add_body = _function_body(source, "AddZoneIfNotExists")
    stops_body = _function_body(source, "ManageZoneStopsAndWatchers")
    trade_tx_body = _function_body(source, "OnTradeTransaction")
    is_stopped_body = _function_body(source, "IsZoneStoppedOut")
    mark_stopped_body = _function_body(source, "MarkZoneStoppedOut")

    assert "long g_stoppedBuyZoneMagics[]" in source
    assert "long g_stoppedSellZoneMagics[]" in source
    assert "StoppedOutGlobalName" in source
    assert "IsZoneStoppedOut(side, magic)" in add_body
    assert "GlobalVariableCheck(StoppedOutGlobalName(side, magic))" in is_stopped_body
    assert "GlobalVariableSet(StoppedOutGlobalName(side, magic)" in mark_stopped_body
    assert "MarkZoneStoppedOut(POSITION_TYPE_BUY, g_buyZones[i].magic)" in stops_body
    assert "MarkZoneStoppedOut(POSITION_TYPE_SELL, g_sellZones[i].magic)" in stops_body
    assert "MarkZoneStoppedOut(POSITION_TYPE_BUY, magic)" in trade_tx_body
    assert "MarkZoneStoppedOut(POSITION_TYPE_SELL, magic)" in trade_tx_body


def test_off_json_removes_matching_zone_instead_of_marking_it_off() -> None:
    source = _source()
    add_body = _function_body(source, "AddZoneIfNotExists")

    assert "ArrayRemove(g_buyZones, i, 1)" in add_body
    assert "ArrayRemove(g_sellZones, i, 1)" in add_body
    assert "mode = ZONE_OFF" not in add_body


def test_take_profit_deactivates_only_outside_zone_or_after_stop_loss() -> None:
    source = _source()
    after_tp_body = _function_body(source, "ShouldDeactivateAfterBasketTakeProfit")

    assert "closePrice <= zone.sl" in after_tp_body
    assert "closePrice >= zone.sl" in after_tp_body
    assert "!IsPriceInZone(zone, closePrice)" in after_tp_body


def test_basket_take_profit_uses_configured_average_price_offset() -> None:
    source = _source()
    should_close_body = _function_body(source, "ShouldCloseBasketByTakeProfit")
    manage_body = _function_body(source, "ManageAllZones")

    assert "const ZoneData &zone" in source
    assert "ShouldCloseBasketByTakeProfit(side, zone, basket)" in manage_body
    assert "basket.averagePrice + DirectionMultiplier(side) * InpTakeProfit * _Point" in should_close_body
    assert "targetPrice = zone.high" not in should_close_body
    assert "targetPrice = zone.low" not in should_close_body


def test_open_basket_dca_continues_even_after_zone_rotation() -> None:
    source = _source()
    should_dca_body = _function_body(source, "ShouldOpenDca")
    manage_body = _function_body(source, "ManageAllZones")
    retired_body = _function_body(source, "ManageRetiredBaskets")

    assert "ShouldOpenDca(side, zone, basket, onFirstTickDca)" in manage_body
    assert "ManageRetiredBaskets(side, onFirstTickDca);" in manage_body
    assert "ShouldOpenDca(side, zone, basket, onFirstTickDca)" in retired_body
    assert "zone.mode != ZONE_TRADE" not in should_dca_body


def test_zone_sl_stays_raw_and_order_stop_loss_applies_single_buffer() -> None:
    source = _source()
    add_body = _function_body(source, "AddZoneIfNotExists")
    order_sl_body = _function_body(source, "OrderStopLossForSide")
    open_body = _function_body(source, "OpenPosition")

    assert "double         rawSl;" not in source
    assert "double stopLoss = NormalizeJsonStopLoss(sl);" in add_body
    assert "BufferedJsonStopLoss(side, sl)" not in add_body
    assert "g_buyZones[i].sl" in order_sl_body
    assert "g_retiredBuyZones[i].sl" in order_sl_body
    assert "g_sellZones[i].sl" in order_sl_body
    assert "g_retiredSellZones[i].sl" in order_sl_body
    assert "MathMin(extremeSl, g_buyZones[i].sl)" in order_sl_body
    assert "MathMax(extremeSl, g_sellZones[i].sl)" in order_sl_body
    assert "BufferedJsonStopLoss(side, extremeSl)" in order_sl_body
    assert "double sl = OrderStopLossForSide(side);" in open_body
    assert "double sl = NormalizeDouble(zone.sl, _Digits);" not in open_body


def test_removed_trade_zone_with_open_basket_is_kept_as_retired_until_done() -> None:
    source = _source()
    add_retired_body = _function_body(source, "AddRetiredZone")
    retired_body = _function_body(source, "ManageRetiredBaskets")
    remove_body = _function_body(source, "RemoveZoneByMagic")
    stops_body = _function_body(source, "ManageZoneStopsAndWatchers")
    add_body = _function_body(source, "AddZoneIfNotExists")

    assert "ZoneData g_retiredBuyZones[]" in source
    assert "ZoneData g_retiredSellZones[]" in source
    assert "zone.mode = ZONE_OFF;" in add_retired_body
    assert "BuildBasket(side, zone.magic, basket);" in retired_body
    assert "if(basket.count <= 0)" in retired_body
    assert "RemoveRetiredZoneByMagic(side, zone.magic);" in retired_body
    assert "RemoveRetiredZoneByMagic(side, magic);" in remove_body
    assert "g_retiredBuyZones[i].sl > 0" in stops_body
    assert "g_retiredSellZones[i].sl > 0" in stops_body
    assert "UpdateRetiredZoneIfOpen(side, magic, stopLoss)" in add_body


def test_initial_order_opens_for_trade_zone_before_stop_loss_without_activation_band() -> None:
    source = _source()
    tradable_body = _function_body(source, "GetTradableZoneIndex")
    initial_body = _function_body(source, "ShouldOpenInitial")

    assert "IsPriceInActivationBand" not in tradable_body
    assert "IsPriceInActivationBand" not in initial_body
    assert "if(g_buyZones[i].mode == ZONE_TRADE) return i;" in tradable_body
    assert "if(g_sellZones[i].mode == ZONE_TRADE) return i;" in tradable_body
    assert "return(true);" in initial_body


def test_ea_draws_two_trade_zone_lines_with_zone_color() -> None:
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
    assert 'if(g_buyZones[i].mode != ZONE_TRADE) continue;' in update_chart_body
    assert 'if(g_sellZones[i].mode != ZONE_TRADE) continue;' in update_chart_body
    assert 'DrawZoneLines(POSITION_TYPE_BUY, g_buyZones[i], i, ZoneGlobalIndex(POSITION_TYPE_BUY, i))' in update_chart_body
    assert 'DrawZoneLines(POSITION_TYPE_SELL, g_sellZones[i], i, ZoneGlobalIndex(POSITION_TYPE_SELL, i))' in update_chart_body
    assert 'ChartRedraw(0)' in update_chart_body


def test_ea_panel_shows_only_trade_zone_details_with_other_zone_summary() -> None:
    source = _source()
    rows_body = _function_body(source, "AddZoneDetailRows")

    assert 'int tradeIndex = ActiveTradeZoneIndex(side);' in rows_body
    assert 'if(i == tradeIndex) continue;' in rows_body
    assert 'otherZones++' in rows_body
    assert 'AddPanelRow(lines, colors, row, prefix + " Other Zones: " + IntegerToString(otherZones)' in rows_body
    assert 'color rowColor = ZoneDisplayColor(ZoneGlobalIndex(side, tradeIndex));' in rows_body


def test_ea_panel_mode_and_sl_use_active_trade_zone() -> None:
    source = _source()
    mode_body = _function_body(source, "GetDisplayZone")
    stop_body = _function_body(source, "ZoneStopText")

    assert 'int tradeIndex = ActiveTradeZoneIndex(side);' in mode_body
    assert 'if(tradeIndex >= 0)' in mode_body
    assert 'ZoneData z = GetDisplayZone(side);' in stop_body


def test_ea_refreshes_zone_chart_objects_during_lifecycle() -> None:
    source = _source()
    deinit_body = _function_body(source, "OnDeinit")
    update_panel_body = _function_body(source, "UpdatePanel")

    assert 'UpdateZoneChartObjects();' in update_panel_body
    assert 'RemoveZoneChartObjects();' in deinit_body
