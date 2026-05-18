from pathlib import Path


EA_V2_SOURCE = Path(__file__).resolve().parents[1] / "EA_Zone_NeverDie_v2.mq5"


def _source() -> str:
    return EA_V2_SOURCE.read_text()


def test_v2_ea_file_declares_required_inputs_and_versioned_title() -> None:
    source = _source()

    assert '#property description "EA Zone NeverDie MT5 v2.5"' in source
    assert 'input string         InpZonesJsonUrl' in source
    assert 'input int            InpZonesPollSeconds' in source
    assert 'input int            InpTakeProfit' in source
    assert 'input double         InpPlanFollowLotSize' in source
    assert 'input double         InpZoneActivateBand' in source
    assert 'input int            InpDcaPrevOrderDistance' in source
    assert 'EA Zone NeverDie MT5 v.' in source


def test_v2_json_loads_new_zones_as_watch_and_keeps_poll_slots() -> None:
    source = _source()

    assert 'string label;' in source
    assert 'JsonString(objectText, "label")' in source
    assert 'LoadWatchZone(POSITION_TYPE_BUY, low, high, sl, label)' in source
    assert 'LoadWatchZone(POSITION_TYPE_SELL, low, high, sl, label)' in source
    assert 'if(!IsPlanChinhLabel(label)) return;' in source
    assert 'zone.status = ZONE_STATUS_WATCH;' in source
    assert 'return(2 * 60 + 50);' in source
    assert 'return(7 * 60 + 45);' in source
    assert 'return(14 * 60 + 15);' in source


def test_v2_cleans_yesterday_zones_before_fetching_json() -> None:
    source = _source()

    assert 'void CleanupPreviousDayZonesBeforeJsonFetch()' in source
    assert 'if(DateKey(g_zones[i].createdAt) < todayKey)' in source
    assert 'CleanupPreviousDayZonesBeforeJsonFetch();\n\n   ResetLastError();\n   int code = WebRequest(' in source


def test_v2_activation_uses_symmetric_band_and_single_trade_zone() -> None:
    source = _source()

    assert 'price >= zone.high - InpZoneActivateBand && price <= zone.high + InpZoneActivateBand' in source
    assert 'price >= zone.low - InpZoneActivateBand && price <= zone.low + InpZoneActivateBand' in source
    assert 'long bestMagic = 0;' in source
    assert 'RemoveCurrentTradeZoneBeforeActivation();' in source
    assert 'ActivateNearestWatchZone();' in source


def test_v2_trade_zone_removal_and_campaign_retention() -> None:
    source = _source()

    assert 'RemoveTouchedTradeZone();' in source
    assert 'touchesLow = tick.ask <= zone.low' in source
    assert 'touchesHigh = tick.bid >= zone.high' in source
    assert 'touchesBuySl = (zone.sl > 0.0 && tick.bid <= zone.sl - InpZonesSlBuffer)' in source
    assert 'touchesSellSl = (zone.sl > 0.0 && tick.ask >= zone.sl + InpZonesSlBuffer)' in source
    assert 'KeepCampaignForZone(zone);' in source
    assert 'RestoreCampaignsFromOpenPositions();' in source
    assert 'CampaignZoneSlFromPosition(side, PositionGetDouble(POSITION_SL))' in source
    assert 'ManageCampaigns(onFirstTickOfNewDcaBar);' in source


def test_v2_orders_use_price_tp_dca_and_side_wide_sl() -> None:
    source = _source()

    assert 'double   averagePrice;' in source
    assert 'basket.averagePrice = basket.weightedPriceSum / basket.totalVolume;' in source
    assert 'basket.averagePrice + DirectionMultiplier(side) * InpTakeProfit * _Point' in source
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


def test_v2_magic_is_side_aware_and_panel_shows_today_summary() -> None:
    source = _source()

    assert 'StableZoneMagic(const ENUM_POSITION_TYPE side' in source
    assert 'StableZoneMagicWithSalt(side, low, high, 17, 53)' in source
    assert 'TodayClosedProfit()' in source
    assert 'TodayClosedOrderCount()' in source
    assert 'FindNearestWatchZoneForDisplay' in source
    assert '"Label: " + ZoneLabelText(zone)' in source
    assert '"SL: " + ZoneSlText(zone)' in source
