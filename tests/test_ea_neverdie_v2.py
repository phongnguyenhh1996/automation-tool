import re
from pathlib import Path


EA_V2_SOURCE = Path(__file__).resolve().parents[1] / "EA_Zone_NeverDie_v2.mq5"


def _source() -> str:
    return EA_V2_SOURCE.read_text()


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


def test_v2_ea_file_declares_required_inputs_and_versioned_title() -> None:
    source = _source()

    assert '#property description "EA Zone NeverDie MT5 v2.19"' in source
    assert "input bool           InpSessionStopEnabled" in source
    assert "input int            InpSessionStopHour        = 2" in source
    assert "input int            InpSessionStopUtcOffsetMin = 420" in source
    assert "input int            InpMorningAutoResumeHour    = 10" in source
    assert "input int            InpMorningAutoResumeMinute  = 15" in source
    assert 'input string         InpZonesJsonUrl' in source
    assert 'input int            InpZonesPollSeconds' in source
    assert 'input int            InpTakeProfit' in source
    assert 'input double         InpPlanFollowLotSize' in source
    assert 'input double         InpZoneActivateBand' in source
    assert 'input int            InpDcaPrevOrderDistance' in source
    assert 'EA Zone NeverDie MT5 v.' in source


def test_v2_json_loads_new_zones_as_watch_and_keeps_poll_slots() -> None:
    source = _source()
    fetch_body = _function_body(source, "FetchZonesJson")

    assert 'string label;' in source
    assert 'JsonString(objectText, "label")' in source
    assert 'LoadWatchZone(POSITION_TYPE_BUY, low, high, sl, label)' in source
    assert 'LoadWatchZone(POSITION_TYPE_SELL, low, high, sl, label)' in source
    assert 'if(!IsPlanChinhLabel(label)) return;' in source
    assert 'zone.status = ZONE_STATUS_WATCH;' in source
    assert 'return(2 * 60 + 50);' in source
    assert 'return(7 * 60 + 45);' in source
    assert 'return(14 * 60 + 15);' in source
    assert 'return("plan_chinh__sang");' in source
    assert 'return("plan_chinh__chieu");' in source
    assert 'return("plan_chinh__toi");' in source
    assert 'IsExpectedJsonFetchLabel(label, expectedLabel)' in source
    assert 'FetchZonesJson(expectedLabel)' in source
    assert '"Cache-Control: no-cache\\r\\n"' in fetch_body
    assert '"Pragma: no-cache\\r\\n"' in fetch_body
    assert 'JsonFetchRequestUrl()' in fetch_body
    assert 'WebRequest("GET", requestUrl,' in fetch_body


def test_v2_json_fetch_slot_completion_persisted_in_globals() -> None:
    source = _source()

    assert 'string JsonFetchCompletedGlobalName(const int windowKey)' in source
    assert 'bool IsJsonFetchWindowCompleted(const int windowKey)' in source
    assert 'void MarkJsonFetchWindowCompleted(const int windowKey)' in source
    assert 'void RestoreJsonFetchWindowStateFromGlobals()' in source
    assert 'GlobalVariableSet(JsonFetchCompletedGlobalName(windowKey), (double)TimeGMT());' in source
    assert 'RestoreJsonFetchWindowStateFromGlobals();' in source
    assert source.index('RestoreMorningResumeStateFromGlobals();') < source.index(
        'RestoreJsonFetchWindowStateFromGlobals();'
    )
    assert source.index('RestoreJsonFetchWindowStateFromGlobals();') < source.index('FetchZonesOnInit();')


def test_v2_scheduled_json_fetch_only_completes_matching_slot() -> None:
    source = _source()
    apply_body = _function_body(source, "ApplyZonesJson")
    schedule_body = _function_body(source, "FetchZonesOnSchedule")
    init_body = _function_body(source, "FetchZonesOnInit")

    assert 'string expectedLabel = JsonFetchSlotExpectedLabel(JsonFetchSlotFromWindowKey(windowKey));' in schedule_body
    assert 'IsJsonFetchWindowCompleted(windowKey)' in schedule_body
    assert 'IsJsonFetchWindowCompleted(windowKey)' in init_body
    assert 'Initial JSON fetch skipped: fetch slot already completed today.' in init_body
    assert 'Scheduled JSON fetch skipped: fetch slot already completed today.' in schedule_body
    assert 'FetchZonesJson(expectedLabel)' in schedule_body
    assert 'MarkJsonFetchWindowCompleted(windowKey);' in schedule_body
    assert schedule_body.index('FetchZonesJson(expectedLabel)') < schedule_body.index(
        'MarkJsonFetchWindowCompleted(windowKey);'
    )
    assert 'MarkJsonFetchWindowCompleted(windowKey);' in init_body
    assert 'bool loadedExpectedSlot = false;' in apply_body
    assert 'loadedExpectedSlot = true;' in apply_body
    assert 'return(loadedExpectedSlot);' in apply_body
    assert 'NeverDie v2 JSON missing expected slot %s' in source


def test_v2_cleans_yesterday_zones_before_fetching_json() -> None:
    source = _source()

    assert 'void CleanupPreviousDayZonesBeforeJsonFetch()' in source
    assert 'if(DateKey(g_zones[i].createdAt) < todayKey)' in source
    assert 'CleanupPreviousDayZonesBeforeJsonFetch();\n\n   ResetLastError();\n   int code = WebRequest(' in source


def test_v2_keeps_single_main_zone_per_side_and_replaces_json_updates() -> None:
    source = _source()
    load_body = _function_body(source, "LoadWatchZone")
    apply_body = _function_body(source, "ApplyZonesJson")
    fetch_body = _function_body(source, "FetchZonesJson")
    prune_body = _function_body(source, "PruneDuplicateMainZones")

    assert "int FindMainZoneIndexBySide(const ENUM_POSITION_TYPE side)" in source
    assert "double MergedZoneSl(const ENUM_POSITION_TYPE side" not in source
    assert "void MergeZoneBounds(ZoneData &zone" not in source
    assert "void MergeZoneEntry(ZoneData &zone" not in source
    assert "long StableDailyZoneMagic(const ENUM_POSITION_TYPE side)" in source
    assert "long StableDailyPlanFollowMagic(const ENUM_POSITION_TYPE side)" in source
    assert "void RemoveOppositeMainZone(const ENUM_POSITION_TYPE side)" not in source
    assert "RemoveOppositeMainZone(side)" not in load_body
    clear_body = _function_body(source, "ClearAllZonesBeforeJsonFetch")
    assert "MarkZoneStoppedOut(g_zones[i].side, g_zones[i].magic);" in clear_body
    assert "Locked and removed zone before JSON apply." in source
    assert "FindMainZoneIndexBySide(side)" in load_body
    assert "double             entry;" in source
    assert "double jsonLow = low;" in load_body
    assert "ApplyFreshWatchZoneFromJson(g_zones[index], side, low, high, jsonLow, sl, label, magic);" in load_body
    assert "Replaced main zone from JSON." in source
    assert "Merged main zone from JSON" not in source
    assert "StableDailyZoneMagic(side)" in load_body
    assert "PruneDuplicateMainZones();" in apply_body
    assert "SyncMainZoneCampaignsAfterMerge();" in apply_body
    assert "Removed duplicate main zone." in prune_body
    assert "Merging duplicate main zone" not in source
    assert 'void ClearAllZonesBeforeJsonFetch()' in source
    assert 'ClearAllZonesBeforeJsonFetch();' in fetch_body
    assert fetch_body.index('ClearAllZonesBeforeJsonFetch()') < fetch_body.index(
        'ApplyZonesJson(body, expectedLabel)'
    )
    assert 'expectedLower == "plan_chinh__sang"' not in fetch_body


def test_v2_json_fetch_keeps_both_sides_when_present() -> None:
    source = _source()
    apply_body = _function_body(source, "ApplyZonesJson")

    assert apply_body.index("LoadWatchZone(POSITION_TYPE_BUY") < apply_body.index(
        "LoadWatchZone(POSITION_TYPE_SELL"
    )
    assert "RemoveOppositeMainZone" not in apply_body


def test_v2_activation_uses_side_specific_entry_ranges() -> None:
    source = _source()
    activation_body = _function_body(source, "IsInActivationBand")

    assert "ZoneActivationRangeMin(const ZoneData &zone)" in source
    assert "ZoneActivationRangeMax(const ZoneData &zone)" in source
    assert "if(zone.side == POSITION_TYPE_BUY)" in activation_body
    assert "price >= zone.low - InpZoneActivateBand && price <= zone.entry + InpZoneActivateBand" in activation_body
    assert "price >= zone.entry - InpZoneActivateBand && price <= zone.high + InpZoneActivateBand" in activation_body
    assert "if(zone.entry <= 0.0) return(false);" in activation_body
    assert 'long bestMagic = 0;' in source
    assert 'RemoveCurrentTradeZoneBeforeActivation();' in source
    assert 'ActivateNearestWatchZone();' in source


def test_v2_trade_zone_removal_and_campaign_retention() -> None:
    source = _source()
    remove_body = _function_body(source, "RemoveZoneAt")
    follow_body = _function_body(source, "ManagePlanChinhFollowEntry")

    assert 'RemoveTouchedTradeZone();' in source
    assert 'touchesLow = tick.ask <= zone.low' in source
    assert 'touchesHigh = tick.bid >= zone.high' in source
    assert 'touchesBuySl = (zone.sl > 0.0 && tick.bid <= zone.sl - InpZonesSlBuffer)' in source
    assert 'touchesSellSl = (zone.sl > 0.0 && tick.ask >= zone.sl + InpZonesSlBuffer)' in source
    assert 'KeepCampaignForZone(zone);' in source
    assert 'KeepPlanFollowCampaignForZone(zone);' in remove_body
    assert 'LatestPlanFollowCampaignIndex();' in follow_body
    assert 'OpenPlanFollowCampaign(campaignIndex, "removed-zone fallback");' in follow_body
    assert 'RestoreCampaignsFromOpenPositions();' in source
    assert 'CampaignZoneSlFromPosition(side, PositionGetDouble(POSITION_SL))' in source
    assert 'ManageCampaigns(onFirstTickOfNewDcaBar);' in source


def test_v2_stopped_zone_is_refreshed_on_new_json_fetch_window() -> None:
    source = _source()
    load_body = _function_body(source, "LoadWatchZone")
    remove_body = _function_body(source, "RemoveZoneAt")
    is_stopped_body = _function_body(source, "IsZoneStoppedOut")
    mark_stopped_body = _function_body(source, "MarkZoneStoppedOut")
    clear_stopped_body = _function_body(source, "ClearZoneStoppedOut")

    assert "long g_stoppedBuyZoneMagics[]" in source
    assert "long g_stoppedSellZoneMagics[]" in source
    assert "StoppedOutGlobalName" in source
    assert "if(IsZoneStoppedOut(side, magic))" in load_body
    assert "ClearZoneStoppedOut(side, magic);" in load_body
    assert "ApplyFreshWatchZoneFromJson(g_zones[index], side, low, high, jsonLow, sl, label, magic);" in load_body
    assert "Replaced main zone from JSON." in load_body
    assert "Skip stopped-out JSON zone." not in load_body
    assert "MarkZoneStoppedOut(g_zones[index].side, g_zones[index].magic)" in remove_body
    assert "GlobalVariableCheck(StoppedOutGlobalName(side, magic))" in is_stopped_body
    assert "GlobalVariableSet(StoppedOutGlobalName(side, magic)" in mark_stopped_body
    assert "GlobalVariableDel(gvName)" in clear_stopped_body


def test_v2_orders_use_price_tp_dca_and_side_wide_sl() -> None:
    source = _source()

    assert 'double   averagePrice;' in source
    assert 'basket.averagePrice = basket.weightedPriceSum / basket.totalVolume;' in source
    assert 'basket.averagePrice + DirectionMultiplier(side) * takeProfitPoints * _Point' in source
    assert 'OpenCampaignOrder(g_campaigns[campaignIndex], NormalizeVolume(InpPlanFollowLotSize), "FOLLOW")' in source
    assert 'KeepCampaignForZoneWithMagic(zone, followMagic, InpPlanFollowLotSize)' in source
    assert "int                fetchSequence;" in source
    assert "g_zoneFetchSequence++;" in source
    assert "LatestPlanChinhZoneIndex();" in source
    assert "if(g_zones[i].fetchSequence >= bestFetchSequence)" in source
    assert 'basket.floatingProfit >= 0.0' in source
    assert 'distance < InpGridStep' in source
    assert 'DcaPrevOrderDistanceReached(distance)' in source
    assert 'if(!prevOrderDistanceReached)' in source
    assert 'g_campaigns[i].baseLot * MathPow(InpMultiplier, basket.count)' in source
    assert 'HighestSellStopLoss() + InpZonesSlBuffer' in source
    assert 'LowestBuyStopLoss() - InpZonesSlBuffer' in source


def test_v2_basket_take_profit_scales_down_for_large_baskets() -> None:
    source = _source()
    tp_body = _function_body(source, "CampaignTakeProfitPoints")
    take_profit_body = _function_body(source, "CampaignTakeProfitReached")

    assert "double CampaignTakeProfitPoints(const BasketInfo &basket)" in source
    assert "if(basket.count >= 6)" in tp_body
    assert "double reduction = 0.15 + (basket.count - 6) * 0.02;" in tp_body
    assert "return(InpTakeProfit * (1.0 - reduction));" in tp_body
    assert "return((double)InpTakeProfit);" in tp_body
    assert "double takeProfitPoints = CampaignTakeProfitPoints(basket);" in take_profit_body
    assert "DirectionMultiplier(side) * takeProfitPoints * _Point" in take_profit_body


def test_v2_start_entry_blocks_when_another_main_campaign_has_positions() -> None:
    source = _source()
    start_body = _function_body(source, "ManageActiveTradeEntry")

    assert "bool HasOtherMainSideOpenPositions(const ENUM_POSITION_TYPE side, const long zoneMagic)" in source
    assert "if(IsPlanFollowCampaign(g_campaigns[i])) continue;" in _function_body(source, "HasOtherMainSideOpenPositions")
    assert "if(g_campaigns[i].magic == zoneMagic) continue;" in _function_body(source, "HasOtherMainSideOpenPositions")
    assert "HasOtherMainSideOpenPositions(zone.side, zone.magic)" in start_body
    assert "START entry skipped: another main campaign still has open positions." in source


def test_v2_follow_entry_is_disabled_while_a_trade_zone_is_active() -> None:
    source = _source()
    on_tick = source[source.index("void OnTick()") :]

    assert 'if(ActiveTradeZoneIndex() >= 0) return;' in source
    assert on_tick.index('ActivateNearestWatchZone();') < on_tick.index('ManagePlanChinhFollowEntry();')


def test_v2_session_stop_clears_zones_but_keeps_dca_baskets() -> None:
    source = _source()
    on_tick = source[source.index("void OnTick()") :]
    stop_body = _function_body(source, "ClearAllZonesForSessionStop")
    paused_body = _function_body(source, "IsSessionTradingPaused")
    schedule_body = _function_body(source, "FetchZonesOnSchedule")

    assert "bool IsSessionTradingPaused()" in source
    assert "SessionLocalNow()" in source
    assert "SessionStopMinuteOfDay()" in paused_body
    assert "IsPastMorningAutoResumeCutoff()" in paused_body
    assert "HasMorningSlotResumeForLocalDay" in paused_body
    assert "RecordMorningSlotResumeAfterFetch" in schedule_body
    assert 'expectedLabel = JsonFetchSlotExpectedLabel' in schedule_body
    assert "KeepCampaignForZone(g_zones[i])" in stop_body
    assert "KeepPlanFollowCampaignForZone(g_zones[i])" in stop_body
    assert "if(IsSessionTradingPaused())" in on_tick
    paused_block = on_tick[on_tick.index("if(IsSessionTradingPaused())") : on_tick.index("CleanupCampaignsWithoutPositions();")]
    assert "EnsureSessionStopZonesCleared();" in paused_block
    assert "ManageCampaigns(onFirstTickOfNewDcaBar);" in paused_block
    assert "return;" in paused_block
    assert "ActivateNearestWatchZone();" not in paused_block
    assert "ManagePlanChinhFollowEntry();" not in paused_block
    assert "ManageActiveTradeEntry();" not in paused_block
    assert 'return("plan_chinh__sang");' in source
    assert 'IsExpectedMorningSlotLabel(expectedLabel)' in source or "IsExpectedMorningSlotLabel" in source


def test_v2_magic_is_side_aware_and_panel_shows_today_summary() -> None:
    source = _source()
    zone_magic_body = _function_body(source, "StableDailyZoneMagic")
    follow_magic_body = _function_body(source, "StableDailyPlanFollowMagic")

    assert 'StableZoneMagic(const ENUM_POSITION_TYPE side' in source
    assert "DateKey(TimeCurrent())" not in zone_magic_body
    assert "DateKey(TimeCurrent())" not in follow_magic_body
    assert 'StableDailyPlanFollowMagic(zone.side)' in source
    assert 'StableDailyPlanFollowMagic(campaign.side)' in source
    assert 'TodayClosedProfit()' in source
    assert 'TodayClosedOrderCount()' in source
    assert 'FindNearestWatchZoneForDisplay' in source
    assert '"Label: " + ZoneLabelText(zone)' in source
    assert '"SL: " + ZoneSlText(zone)' in source
