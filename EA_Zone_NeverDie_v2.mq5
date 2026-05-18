#property strict
#property description "EA Zone NeverDie MT5 v2.6"

#include <Trade/Trade.mqh>

enum ENUM_ZONE_STATUS
  {
   ZONE_STATUS_WATCH = 0,
   ZONE_STATUS_TRADE = 1
  };

enum ENUM_ND_DCA_TF
  {
   ND_DCA_M5  = 0,
   ND_DCA_M15 = 1
  };

struct ZoneData
  {
   ENUM_POSITION_TYPE side;
   ENUM_ZONE_STATUS   status;
   double             low;
   double             high;
   double             sl;
   string             label;
   long               magic;
   datetime           createdAt;
   int                fetchSequence;
  };

struct CampaignData
  {
   ENUM_POSITION_TYPE side;
   double             low;
   double             high;
   double             sl;
   double             baseLot;
   long               magic;
   bool               active;
  };

struct BasketInfo
  {
   int      count;
   double   totalVolume;
   double   weightedPriceSum;
   double   averagePrice;
   double   floatingProfit;
   double   lastOpenPrice;
   datetime lastOpenTime;
  };

input group "=== TRADE SETTINGS ==="
input double         InpLotSize                = 0.05;
input double         InpPlanFollowLotSize      = 0.03;
input double         InpMultiplier             = 1.25;
input int            InpGridStep               = 3000;
input int            InpMaxGridLevels          = 50;
input long           InpMagicNumber            = 20250215;
input int            InpTakeProfit             = 3000;
input ENUM_ND_DCA_TF InpDcaGridTimeframe       = ND_DCA_M15;
input int            InpDcaClosedBarsRequired  = 1;
input int            InpDcaPrevOrderDistance   = 0;
input double         InpZoneActivateBand       = 3.0;

input group "=== DISPLAY ==="
input bool           InpShowPanel              = true;

input group "=== REMOTE ZONES JSON ==="
input string         InpZonesJsonUrl           = "https://res.cloudinary.com/easy-toeic/raw/upload/automation_tool/ea_neverdie/neverdie_XAUUSD.json";
input int            InpZonesPollSeconds       = 300;
input string         InpZonesBearer            = "";
input double         InpZonesSlBuffer          = 10.0;

const string EA_VERSION = "2.6";
const int JSON_FETCH_WINDOW_MINUTES = 30;
const int JSON_FETCH_SLOT_COUNT = 3;
const int PANEL_LINE_COUNT = 24;
const int PANEL_X = 12;
const int PANEL_Y = 30;
const int PANEL_MIN_WIDTH = 250;
const int PANEL_MAX_WIDTH = 430;
const int PANEL_LINE_HEIGHT = 16;
const int PANEL_TITLE_Y_OFFSET = 10;
const int PANEL_LINES_Y_OFFSET = 40;
const int PANEL_BOTTOM_PAD = 14;
const int PANEL_CHAR_WIDTH = 7;
const int PANEL_HORIZONTAL_PAD = 28;

CTrade g_trade;
ZoneData g_zones[];
CampaignData g_campaigns[];
datetime g_dcaBarOpenSeen = 0;
int g_completedJsonFetchWindowKey = -1;
int g_zoneFetchSequence = 0;
string g_panelPrefix = "ZoneNeverDieV2Panel";

ENUM_TIMEFRAMES DcaTimeframe()
  {
   return(InpDcaGridTimeframe == ND_DCA_M15 ? PERIOD_M15 : PERIOD_M5);
  }

bool ValidateInputs()
  {
   if(InpLotSize <= 0.0) return(false);
   if(InpPlanFollowLotSize <= 0.0) return(false);
   if(InpMultiplier < 1.0) return(false);
   if(InpGridStep <= 0) return(false);
   if(InpMaxGridLevels < 1) return(false);
   if(InpTakeProfit <= 0) return(false);
   if(InpDcaClosedBarsRequired < 1) return(false);
   if(InpDcaPrevOrderDistance < 0) return(false);
   if(InpZoneActivateBand < 0.0) return(false);
   if(InpZonesPollSeconds < 0) return(false);
   return(true);
  }

bool EnvironmentReady()
  {
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)) return(false);
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED)) return(false);
   return(Bars(_Symbol, _Period) >= 100);
  }

double MidPrice(const MqlTick &tick)
  {
   return((tick.bid + tick.ask) / 2.0);
  }

double OpenPriceForSide(const ENUM_POSITION_TYPE side, const MqlTick &tick)
  {
   return(side == POSITION_TYPE_BUY ? tick.ask : tick.bid);
  }

double ClosePriceForSide(const ENUM_POSITION_TYPE side, const MqlTick &tick)
  {
   return(side == POSITION_TYPE_BUY ? tick.bid : tick.ask);
  }

double DirectionMultiplier(const ENUM_POSITION_TYPE side)
  {
   return(side == POSITION_TYPE_BUY ? 1.0 : -1.0);
  }

bool RemoteJsonEnabled()
  {
   if(MQLInfoInteger(MQL_TESTER)) return(false);
   if(InpZonesPollSeconds <= 0) return(false);
   return(StringLen(InpZonesJsonUrl) > 0);
  }

int DateKey(const datetime value)
  {
   MqlDateTime tm;
   TimeToStruct(value, tm);
   return(tm.year * 10000 + tm.mon * 100 + tm.day);
  }

int JsonFetchSlotStartMinute(const int slot)
  {
   if(slot == 0) return(2 * 60 + 50);
   if(slot == 1) return(7 * 60 + 45);
   if(slot == 2) return(14 * 60 + 15);
   return(-1);
  }

int CurrentJsonFetchWindowKey()
  {
   datetime utcNow = TimeGMT();
   MqlDateTime tm;
   TimeToStruct(utcNow, tm);
   int currentMinute = tm.hour * 60 + tm.min;

   for(int slot = 0; slot < JSON_FETCH_SLOT_COUNT; slot++)
     {
      int startMinute = JsonFetchSlotStartMinute(slot);
      if(currentMinute >= startMinute && currentMinute < startMinute + JSON_FETCH_WINDOW_MINUTES)
         return(DateKey(utcNow) * 10 + slot);
     }
   return(-1);
  }

bool ExtractJsonObject(const string json, const string key, string &objectText)
  {
   string needle = "\"" + key + "\"";
   int keyPos = StringFind(json, needle);
   if(keyPos < 0) return(false);
   int start = StringFind(json, "{", keyPos);
   if(start < 0) return(false);

   int depth = 0;
   for(int i = start; i < StringLen(json); i++)
     {
      ushort ch = StringGetCharacter(json, i);
      if(ch == '{') depth++;
      if(ch == '}')
        {
         depth--;
         if(depth == 0)
           {
            objectText = StringSubstr(json, start, i - start + 1);
            return(true);
           }
        }
     }
   return(false);
  }

double JsonNumber(const string objectText, const string key)
  {
   string needle = "\"" + key + "\"";
   int pos = StringFind(objectText, needle);
   if(pos < 0) return(0.0);
   pos += StringLen(needle);
   while(pos < StringLen(objectText))
     {
      ushort ch = StringGetCharacter(objectText, pos);
      if(ch != ' ' && ch != ':') break;
      pos++;
     }
   return(StringToDouble(StringSubstr(objectText, pos)));
  }

string JsonString(const string objectText, const string key)
  {
   string needle = "\"" + key + "\"";
   int pos = StringFind(objectText, needle);
   if(pos < 0) return("");
   pos += StringLen(needle);
   while(pos < StringLen(objectText))
     {
      ushort ch = StringGetCharacter(objectText, pos);
      if(ch != ' ' && ch != ':') break;
      pos++;
     }
   if(pos >= StringLen(objectText) || StringGetCharacter(objectText, pos) != '"') return("");
   int start = pos + 1;
   bool escaped = false;
   for(int i = start; i < StringLen(objectText); i++)
     {
      ushort ch = StringGetCharacter(objectText, i);
      if(escaped)
        {
         escaped = false;
         continue;
        }
      if(ch == '\\')
        {
         escaped = true;
         continue;
        }
      if(ch == '"') return(StringSubstr(objectText, start, i - start));
     }
   return("");
  }

bool ParseSideZone(const string json, const string key, double &low, double &high, double &sl, string &label)
  {
   string objectText;
   if(!ExtractJsonObject(json, key, objectText)) return(false);
   low = JsonNumber(objectText, "low");
   high = JsonNumber(objectText, "high");
   sl = JsonNumber(objectText, "sl");
   label = JsonString(objectText, "label");
   return(low > 0.0 && high > 0.0);
  }

long StableZoneMagicWithSalt(const ENUM_POSITION_TYPE side, const double low, const double high, const int buySalt, const int sellSalt)
  {
   int sideSalt = (side == POSITION_TYPE_BUY ? buySalt : sellSalt);
   string key = _Symbol + "_" + IntegerToString(sideSalt) + "_" + DoubleToString(low, _Digits) + "_" + DoubleToString(high, _Digits);
   long hash = 2166136261;
   for(int i = 0; i < StringLen(key); i++)
     {
      hash = (hash ^ StringGetCharacter(key, i)) * 16777619;
      if(hash < 0) hash = -hash;
      hash %= 900000;
     }
   return(InpMagicNumber + 1 + hash);
  }

long StableZoneMagic(const ENUM_POSITION_TYPE side, const double low, const double high)
  {
   return(StableZoneMagicWithSalt(side, low, high, 17, 53));
  }

long StablePlanFollowMagic(const ENUM_POSITION_TYPE side, const double low, const double high)
  {
   return(StableZoneMagicWithSalt(side, low, high, 71, 89));
  }

bool IsOurMagic(const long magic)
  {
   return(magic > InpMagicNumber && magic <= InpMagicNumber + 900000);
  }

bool IsPlanChinhLabel(const string label)
  {
   string value = label;
   StringToLower(value);
   return(StringFind(value, "plan_chinh__") == 0);
  }

void NormalizeZonePrices(double &low, double &high)
  {
   double minPrice = MathMin(low, high);
   double maxPrice = MathMax(low, high);
   low = NormalizeDouble(minPrice, _Digits);
   high = NormalizeDouble(maxPrice, _Digits);
  }

int FindZoneIndex(const ENUM_POSITION_TYPE side, const double low, const double high)
  {
   for(int i = 0; i < ArraySize(g_zones); i++)
      if(g_zones[i].side == side && g_zones[i].low == low && g_zones[i].high == high)
         return(i);
   return(-1);
  }

int FindCampaignIndex(const long magic)
  {
   for(int i = 0; i < ArraySize(g_campaigns); i++)
      if(g_campaigns[i].magic == magic)
         return(i);
   return(-1);
  }

void LoadWatchZone(const ENUM_POSITION_TYPE side, double low, double high, const double sl, const string label)
  {
   if(!IsPlanChinhLabel(label)) return;

   NormalizeZonePrices(low, high);
   long magic = StableZoneMagic(side, low, high);
   int index = FindZoneIndex(side, low, high);
   g_zoneFetchSequence++;

   if(index >= 0)
     {
      g_zones[index].sl = NormalizeDouble(sl, _Digits);
      g_zones[index].label = label;
      g_zones[index].magic = magic;
      g_zones[index].fetchSequence = g_zoneFetchSequence;
      if(g_zones[index].status != ZONE_STATUS_TRADE)
         g_zones[index].status = ZONE_STATUS_WATCH;
      return;
     }

   int size = ArraySize(g_zones);
   ArrayResize(g_zones, size + 1);
   ZoneData zone;
   zone.side = side;
   zone.status = ZONE_STATUS_WATCH;
   zone.low = low;
   zone.high = high;
   zone.sl = NormalizeDouble(sl, _Digits);
   zone.label = label;
   zone.magic = magic;
   zone.createdAt = TimeCurrent();
   zone.fetchSequence = g_zoneFetchSequence;
   g_zones[size] = zone;
  }

bool ApplyZonesJson(const string json)
  {
   bool parsed = false;
   double low;
   double high;
   double sl;
   string label;

   if(ParseSideZone(json, "buy", low, high, sl, label))
     {
      LoadWatchZone(POSITION_TYPE_BUY, low, high, sl, label);
      parsed = true;
     }

   if(ParseSideZone(json, "sell", low, high, sl, label))
     {
      LoadWatchZone(POSITION_TYPE_SELL, low, high, sl, label);
      parsed = true;
     }

   return(parsed);
  }

void CleanupPreviousDayZonesBeforeJsonFetch()
  {
   int todayKey = DateKey(TimeCurrent());
   for(int i = ArraySize(g_zones) - 1; i >= 0; i--)
     {
      if(DateKey(g_zones[i].createdAt) < todayKey)
        {
         if(g_zones[i].status == ZONE_STATUS_TRADE)
            KeepCampaignForZone(g_zones[i]);
         ArrayRemove(g_zones, i, 1);
        }
     }
  }

bool FetchZonesJson()
  {
   uchar request[];
   uchar result[];
   string responseHeaders;
   string headers = "";
   if(StringLen(InpZonesBearer) > 0)
      headers = "Authorization: Bearer " + InpZonesBearer + "\r\n";

   CleanupPreviousDayZonesBeforeJsonFetch();

   ResetLastError();
   int code = WebRequest("GET", InpZonesJsonUrl, headers, 15000, request, result, responseHeaders);
   if(code != 200)
     {
      PrintFormat("NeverDie v2 JSON fetch failed. code=%d error=%d", code, GetLastError());
      return(false);
     }

   string body = CharArrayToString(result);
   if(!ApplyZonesJson(body))
     {
      PrintFormat("NeverDie v2 JSON parse failed: %s", body);
      return(false);
     }
   return(true);
  }

void FetchZonesOnInit()
  {
   if(!RemoteJsonEnabled()) return;
   if(!FetchZonesJson()) return;
   int windowKey = CurrentJsonFetchWindowKey();
   if(windowKey >= 0) g_completedJsonFetchWindowKey = windowKey;
  }

void FetchZonesOnSchedule()
  {
   if(!RemoteJsonEnabled()) return;
   int windowKey = CurrentJsonFetchWindowKey();
   if(windowKey < 0 || windowKey == g_completedJsonFetchWindowKey) return;
   if(FetchZonesJson()) g_completedJsonFetchWindowKey = windowKey;
  }

bool IsInActivationBand(const ZoneData &zone, const double price)
  {
   if(zone.side == POSITION_TYPE_SELL)
      return(price >= zone.high - InpZoneActivateBand && price <= zone.high + InpZoneActivateBand);
   return(price >= zone.low - InpZoneActivateBand && price <= zone.low + InpZoneActivateBand);
  }

double ActivationDistance(const ZoneData &zone, const double price)
  {
   double trigger = (zone.side == POSITION_TYPE_BUY ? zone.low : zone.high);
   return(MathAbs(price - trigger));
  }

void KeepCampaignForZoneWithMagic(const ZoneData &zone, const long magic, const double baseLot)
  {
   int index = FindCampaignIndex(magic);
   if(index >= 0)
     {
      g_campaigns[index].side = zone.side;
      g_campaigns[index].low = zone.low;
      g_campaigns[index].high = zone.high;
      g_campaigns[index].sl = zone.sl;
      g_campaigns[index].baseLot = baseLot;
      g_campaigns[index].active = true;
      return;
     }

   int size = ArraySize(g_campaigns);
   ArrayResize(g_campaigns, size + 1);
   g_campaigns[size].side = zone.side;
   g_campaigns[size].low = zone.low;
   g_campaigns[size].high = zone.high;
   g_campaigns[size].sl = zone.sl;
   g_campaigns[size].baseLot = baseLot;
   g_campaigns[size].magic = magic;
   g_campaigns[size].active = true;
  }

void KeepCampaignForZone(const ZoneData &zone)
  {
   KeepCampaignForZoneWithMagic(zone, zone.magic, InpLotSize);
  }

bool HasOpenPositions(const long magic)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) == magic) return(true);
     }
   return(false);
  }

double CampaignZoneSlFromPosition(const ENUM_POSITION_TYPE side, const double positionSl)
  {
   if(positionSl <= 0.0) return(0.0);
   if(side == POSITION_TYPE_BUY)
      return(NormalizeDouble(positionSl + InpZonesSlBuffer, _Digits));
   return(NormalizeDouble(MathMax(positionSl - InpZonesSlBuffer, 0.0), _Digits));
  }

double CampaignBaseLotFromPositionComment(const string comment)
  {
   if(StringFind(comment, "FOLLOW") >= 0)
      return(InpPlanFollowLotSize);
   return(InpLotSize);
  }

void RestoreCampaignsFromOpenPositions()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;

      long magic = PositionGetInteger(POSITION_MAGIC);
      if(!IsOurMagic(magic)) continue;
      if(FindCampaignIndex(magic) >= 0) continue;

      int size = ArraySize(g_campaigns);
      ArrayResize(g_campaigns, size + 1);
      ENUM_POSITION_TYPE side = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      g_campaigns[size].side = side;
      g_campaigns[size].low = 0.0;
      g_campaigns[size].high = 0.0;
      g_campaigns[size].sl = CampaignZoneSlFromPosition(side, PositionGetDouble(POSITION_SL));
      g_campaigns[size].baseLot = CampaignBaseLotFromPositionComment(PositionGetString(POSITION_COMMENT));
      g_campaigns[size].magic = magic;
      g_campaigns[size].active = true;
     }
  }

void RemoveCurrentTradeZoneBeforeActivation()
  {
   for(int i = ArraySize(g_zones) - 1; i >= 0; i--)
     {
      if(g_zones[i].status != ZONE_STATUS_TRADE) continue;
      KeepCampaignForZone(g_zones[i]);
      ArrayRemove(g_zones, i, 1);
     }
  }

void ActivateNearestWatchZone()
  {
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick)) return;
   double price = MidPrice(tick);
   int bestIndex = -1;
   long bestMagic = 0;
   double bestDistance = 1.0e100;

   for(int i = 0; i < ArraySize(g_zones); i++)
     {
      if(g_zones[i].status != ZONE_STATUS_WATCH) continue;
      if(!IsInActivationBand(g_zones[i], price)) continue;

      double distance = ActivationDistance(g_zones[i], price);
      if(distance <= bestDistance)
        {
         bestDistance = distance;
         bestIndex = i;
         bestMagic = g_zones[i].magic;
        }
     }

   if(bestIndex < 0) return;
   RemoveCurrentTradeZoneBeforeActivation();
   for(int i = 0; i < ArraySize(g_zones); i++)
     {
      if(g_zones[i].magic != bestMagic) continue;
      g_zones[i].status = ZONE_STATUS_TRADE;
      KeepCampaignForZone(g_zones[i]);
      return;
     }
  }

void RemoveZoneAt(const int index)
  {
   if(index < 0 || index >= ArraySize(g_zones)) return;
   KeepCampaignForZone(g_zones[index]);
   ArrayRemove(g_zones, index, 1);
  }

void RemoveTouchedTradeZone()
  {
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick)) return;

   for(int i = ArraySize(g_zones) - 1; i >= 0; i--)
     {
      if(g_zones[i].status != ZONE_STATUS_TRADE) continue;
      ZoneData zone = g_zones[i];
      bool touchesLow = tick.ask <= zone.low;
      bool touchesHigh = tick.bid >= zone.high;
      bool touchesBuySl = (zone.sl > 0.0 && tick.bid <= zone.sl);
      bool touchesSellSl = (zone.sl > 0.0 && tick.ask >= zone.sl);

      if(zone.side == POSITION_TYPE_SELL && (touchesLow || touchesSellSl))
         RemoveZoneAt(i);
      else if(zone.side == POSITION_TYPE_BUY && (touchesHigh || touchesBuySl))
         RemoveZoneAt(i);
     }
  }

int ActiveTradeZoneIndex()
  {
   for(int i = ArraySize(g_zones) - 1; i >= 0; i--)
      if(g_zones[i].status == ZONE_STATUS_TRADE)
         return(i);
   return(-1);
  }

int LatestPlanChinhZoneIndex()
  {
   int bestIndex = -1;
   int bestFetchSequence = -1;

   for(int i = 0; i < ArraySize(g_zones); i++)
     {
      if(!IsPlanChinhLabel(g_zones[i].label)) continue;
      if(g_zones[i].fetchSequence >= bestFetchSequence)
        {
         bestFetchSequence = g_zones[i].fetchSequence;
         bestIndex = i;
        }
     }

   return(bestIndex);
  }

void ManagePlanChinhFollowEntry()
  {
   if(ActiveTradeZoneIndex() >= 0) return;

   int zoneIndex = LatestPlanChinhZoneIndex();
   if(zoneIndex < 0) return;

   ZoneData zone = g_zones[zoneIndex];
   long followMagic = StablePlanFollowMagic(zone.side, zone.low, zone.high);
   KeepCampaignForZoneWithMagic(zone, followMagic, InpPlanFollowLotSize);

   BasketInfo basket;
   BuildBasket(zone.side, followMagic, basket);
   if(basket.count > 0) return;

   int campaignIndex = FindCampaignIndex(followMagic);
   if(campaignIndex < 0) return;
   OpenCampaignOrder(g_campaigns[campaignIndex], NormalizeVolume(InpPlanFollowLotSize), "FOLLOW");
  }

void BuildBasket(const ENUM_POSITION_TYPE side, const long magic, BasketInfo &basket)
  {
   basket.count = 0;
   basket.totalVolume = 0.0;
   basket.weightedPriceSum = 0.0;
   basket.averagePrice = 0.0;
   basket.floatingProfit = 0.0;
   basket.lastOpenPrice = 0.0;
   basket.lastOpenTime = 0;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic) continue;
      if((ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE) != side) continue;

      double volume = PositionGetDouble(POSITION_VOLUME);
      double openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
      datetime opened = (datetime)PositionGetInteger(POSITION_TIME);
      basket.count++;
      basket.totalVolume += volume;
      basket.weightedPriceSum += openPrice * volume;
      basket.floatingProfit += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);

      if(opened >= basket.lastOpenTime)
        {
         basket.lastOpenTime = opened;
         basket.lastOpenPrice = openPrice;
        }
     }

   if(basket.totalVolume > 0.0)
      basket.averagePrice = basket.weightedPriceSum / basket.totalVolume;
  }

int VolumeDigits(const double step)
  {
   double current = step;
   int digits = 0;
   while(current > 0.0 && current < 1.0 && digits < 8)
     {
      current *= 10.0;
      digits++;
     }
   return(digits);
  }

double NormalizeVolume(const double requested)
  {
   double minVolume = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxVolume = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double stepVolume = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double volume = MathMax(requested, minVolume);
   volume = MathMin(volume, maxVolume);
   if(stepVolume > 0.0)
      volume = MathFloor(volume / stepVolume + 0.0000001) * stepVolume;
   return(NormalizeDouble(volume, VolumeDigits(stepVolume)));
  }

double LowestBuyStopLoss()
  {
   double value = 0.0;
   for(int i = 0; i < ArraySize(g_zones); i++)
     {
      if(g_zones[i].side != POSITION_TYPE_BUY || g_zones[i].sl <= 0.0) continue;
      value = (value <= 0.0 ? g_zones[i].sl : MathMin(value, g_zones[i].sl));
     }
   for(int i = 0; i < ArraySize(g_campaigns); i++)
     {
      if(g_campaigns[i].side != POSITION_TYPE_BUY || g_campaigns[i].sl <= 0.0) continue;
      value = (value <= 0.0 ? g_campaigns[i].sl : MathMin(value, g_campaigns[i].sl));
     }
   return(value);
  }

double HighestSellStopLoss()
  {
   double value = 0.0;
   for(int i = 0; i < ArraySize(g_zones); i++)
     {
      if(g_zones[i].side != POSITION_TYPE_SELL || g_zones[i].sl <= 0.0) continue;
      value = MathMax(value, g_zones[i].sl);
     }
   for(int i = 0; i < ArraySize(g_campaigns); i++)
     {
      if(g_campaigns[i].side != POSITION_TYPE_SELL || g_campaigns[i].sl <= 0.0) continue;
      value = MathMax(value, g_campaigns[i].sl);
     }
   return(value);
  }

double OrderStopLoss(const ENUM_POSITION_TYPE side)
  {
   if(side == POSITION_TYPE_SELL)
     {
      double sl = HighestSellStopLoss() + InpZonesSlBuffer;
      return(sl > InpZonesSlBuffer ? NormalizeDouble(sl, _Digits) : 0.0);
     }

   double sl = LowestBuyStopLoss() - InpZonesSlBuffer;
   return(sl > 0.0 ? NormalizeDouble(sl, _Digits) : 0.0);
  }

bool OpenCampaignOrder(const CampaignData &campaign, const double volume, const string tag)
  {
   g_trade.SetExpertMagicNumber(campaign.magic);
   g_trade.SetTypeFillingBySymbol(_Symbol);
   string comment = "ZND_V2_" + (campaign.side == POSITION_TYPE_BUY ? "BUY_" : "SELL_") + tag;
   double sl = OrderStopLoss(campaign.side);

   if(campaign.side == POSITION_TYPE_BUY)
      return(g_trade.Buy(volume, _Symbol, 0.0, sl, 0.0, comment));
   return(g_trade.Sell(volume, _Symbol, 0.0, sl, 0.0, comment));
  }

bool CampaignTakeProfitReached(const ENUM_POSITION_TYPE side, const BasketInfo &basket)
  {
   if(basket.count <= 0 || basket.totalVolume <= 0.0) return(false);

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick)) return(false);

   double currentPrice = ClosePriceForSide(side, tick);
   double targetPrice = basket.averagePrice + DirectionMultiplier(side) * InpTakeProfit * _Point;

   if(side == POSITION_TYPE_BUY && currentPrice >= targetPrice && basket.floatingProfit > 0.0) return(true);
   if(side == POSITION_TYPE_SELL && currentPrice <= targetPrice && basket.floatingProfit > 0.0) return(true);
   return(false);
  }

bool CloseCampaign(const CampaignData &campaign)
  {
   bool allClosed = true;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != campaign.magic) continue;
      if((ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE) != campaign.side) continue;
      if(!g_trade.PositionClose(ticket)) allClosed = false;
     }
   return(allClosed);
  }

bool DcaPrevOrderDistanceReached(const double distance)
  {
   if(InpDcaPrevOrderDistance <= 0) return(false);
   return(distance >= InpDcaPrevOrderDistance);
  }

bool ShouldOpenDca(const CampaignData &campaign, const BasketInfo &basket, const bool onFirstTick)
  {
   if(basket.count <= 0 || basket.count >= InpMaxGridLevels) return(false);
   if(basket.floatingProfit >= 0.0) return(false);

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick)) return(false);
   double price = OpenPriceForSide(campaign.side, tick);
   double distance = MathAbs(price - basket.lastOpenPrice) / _Point;

   if(campaign.side == POSITION_TYPE_BUY && price >= basket.lastOpenPrice) return(false);
   if(campaign.side == POSITION_TYPE_SELL && price <= basket.lastOpenPrice) return(false);

   bool prevOrderDistanceReached = DcaPrevOrderDistanceReached(distance);
   if(!prevOrderDistanceReached)
     {
      if(distance < InpGridStep) return(false);
      if(!onFirstTick) return(false);

      int shiftSinceOpen = iBarShift(_Symbol, DcaTimeframe(), basket.lastOpenTime, false);
      if(shiftSinceOpen < InpDcaClosedBarsRequired) return(false);
     }

   return(true);
  }

void ManageActiveTradeEntry()
  {
   int zoneIndex = ActiveTradeZoneIndex();
   if(zoneIndex < 0) return;

   ZoneData zone = g_zones[zoneIndex];
   KeepCampaignForZone(zone);
   BasketInfo basket;
   BuildBasket(zone.side, zone.magic, basket);
   if(basket.count > 0) return;

   int campaignIndex = FindCampaignIndex(zone.magic);
   if(campaignIndex < 0) return;
   OpenCampaignOrder(g_campaigns[campaignIndex], NormalizeVolume(InpLotSize), "START");
  }

void ManageCampaigns(const bool onFirstTickOfNewDcaBar)
  {
   for(int i = ArraySize(g_campaigns) - 1; i >= 0; i--)
     {
      if(!g_campaigns[i].active) continue;
      BasketInfo basket;
      BuildBasket(g_campaigns[i].side, g_campaigns[i].magic, basket);

      if(basket.count <= 0)
        {
         ArrayRemove(g_campaigns, i, 1);
         continue;
        }

      if(CampaignTakeProfitReached(g_campaigns[i].side, basket))
        {
         if(CloseCampaign(g_campaigns[i]))
            ArrayRemove(g_campaigns, i, 1);
         continue;
        }

      if(ShouldOpenDca(g_campaigns[i], basket, onFirstTickOfNewDcaBar))
        {
         double nextVolume = NormalizeVolume(g_campaigns[i].baseLot * MathPow(InpMultiplier, basket.count));
         OpenCampaignOrder(g_campaigns[i], nextVolume, "DCA");
        }
     }
  }

void CleanupCampaignsWithoutPositions()
  {
   for(int i = ArraySize(g_campaigns) - 1; i >= 0; i--)
      if(!HasOpenPositions(g_campaigns[i].magic))
         ArrayRemove(g_campaigns, i, 1);
  }

datetime TodayStart()
  {
   MqlDateTime tm;
   TimeToStruct(TimeCurrent(), tm);
   tm.hour = 0;
   tm.min = 0;
   tm.sec = 0;
   return(StructToTime(tm));
  }

double TodayClosedProfit()
  {
   double profit = 0.0;
   if(!HistorySelect(TodayStart(), TimeCurrent())) return(0.0);

   for(int i = HistoryDealsTotal() - 1; i >= 0; i--)
     {
      ulong deal = HistoryDealGetTicket(i);
      if(deal == 0) continue;
      if(HistoryDealGetString(deal, DEAL_SYMBOL) != _Symbol) continue;
      if(!IsOurMagic(HistoryDealGetInteger(deal, DEAL_MAGIC))) continue;
      long entry = HistoryDealGetInteger(deal, DEAL_ENTRY);
      if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY && entry != DEAL_ENTRY_INOUT) continue;
      profit += HistoryDealGetDouble(deal, DEAL_PROFIT) + HistoryDealGetDouble(deal, DEAL_SWAP) + HistoryDealGetDouble(deal, DEAL_COMMISSION);
     }
   return(profit);
  }

int TodayClosedOrderCount()
  {
   int count = 0;
   if(!HistorySelect(TodayStart(), TimeCurrent())) return(0);

   for(int i = HistoryDealsTotal() - 1; i >= 0; i--)
     {
      ulong deal = HistoryDealGetTicket(i);
      if(deal == 0) continue;
      if(HistoryDealGetString(deal, DEAL_SYMBOL) != _Symbol) continue;
      if(!IsOurMagic(HistoryDealGetInteger(deal, DEAL_MAGIC))) continue;
      long entry = HistoryDealGetInteger(deal, DEAL_ENTRY);
      if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_OUT_BY || entry == DEAL_ENTRY_INOUT) count++;
     }
   return(count);
  }

int TotalOpenEaOrders()
  {
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if(IsOurMagic(PositionGetInteger(POSITION_MAGIC))) count++;
     }
   return(count);
  }

bool FindNearestWatchZoneForDisplay(ZoneData &zone, double &distance)
  {
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick)) return(false);
   double price = MidPrice(tick);
   int bestIndex = -1;
   distance = 1.0e100;

   for(int i = 0; i < ArraySize(g_zones); i++)
     {
      if(g_zones[i].status != ZONE_STATUS_WATCH) continue;
      double candidateDistance = ActivationDistance(g_zones[i], price);
      if(candidateDistance < distance)
        {
         distance = candidateDistance;
         bestIndex = i;
        }
     }

   if(bestIndex < 0) return(false);
   zone = g_zones[bestIndex];
   return(true);
  }

string SideText(const ENUM_POSITION_TYPE side)
  {
   return(side == POSITION_TYPE_BUY ? "BUY" : "SELL");
  }

string ZoneLabelText(const ZoneData &zone)
  {
   return(StringLen(zone.label) > 0 ? zone.label : "-");
  }

string ZoneSlText(const ZoneData &zone)
  {
   return(zone.sl > 0.0 ? DoubleToString(zone.sl, _Digits) : "-");
  }

color ProfitColor(const double value)
  {
   if(value > 0.0) return(clrDarkGreen);
   if(value < 0.0) return(clrCrimson);
   return(clrBlack);
  }

void AddPanelRow(string &lines[], color &colors[], int &row, const string text, const color rowColor)
  {
   if(row >= ArraySize(lines)) return;
   lines[row] = text;
   colors[row] = rowColor;
   row++;
  }

void CreatePanelLine(const int index)
  {
   string name = g_panelPrefix + "_LINE_" + IntegerToString(index);
   if(ObjectFind(0, name) == -1) ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, ANCHOR_UPPER);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 9);
   ObjectSetString(0, name, OBJPROP_FONT, "Tahoma");
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
  }

void CreatePanel()
  {
   string bg = g_panelPrefix + "_BG";
   string title = g_panelPrefix + "_TITLE";

   if(ObjectFind(0, bg) == -1) ObjectCreate(0, bg, OBJ_RECTANGLE_LABEL, 0, 0, 0);
   ObjectSetInteger(0, bg, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, bg, OBJPROP_XDISTANCE, PANEL_X);
   ObjectSetInteger(0, bg, OBJPROP_YDISTANCE, PANEL_Y);
   ObjectSetInteger(0, bg, OBJPROP_BGCOLOR, clrWhiteSmoke);
   ObjectSetInteger(0, bg, OBJPROP_COLOR, clrBlack);
   ObjectSetInteger(0, bg, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, bg, OBJPROP_HIDDEN, true);

   if(ObjectFind(0, title) == -1) ObjectCreate(0, title, OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, title, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, title, OBJPROP_YDISTANCE, PANEL_Y + PANEL_TITLE_Y_OFFSET);
   ObjectSetInteger(0, title, OBJPROP_ANCHOR, ANCHOR_UPPER);
   ObjectSetInteger(0, title, OBJPROP_COLOR, clrBlack);
   ObjectSetInteger(0, title, OBJPROP_FONTSIZE, 11);
   ObjectSetString(0, title, OBJPROP_FONT, "Tahoma Bold");
   ObjectSetString(0, title, OBJPROP_TEXT, "EA Zone NeverDie MT5 v." + EA_VERSION);
   ObjectSetInteger(0, title, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, title, OBJPROP_HIDDEN, true);

   for(int i = 0; i < PANEL_LINE_COUNT; i++) CreatePanelLine(i);
  }

void RemovePanel()
  {
   ObjectDelete(0, g_panelPrefix + "_BG");
   ObjectDelete(0, g_panelPrefix + "_TITLE");
   for(int i = 0; i < PANEL_LINE_COUNT; i++)
      ObjectDelete(0, g_panelPrefix + "_LINE_" + IntegerToString(i));
  }

int ClampPanelWidth(const int width)
  {
   if(width < PANEL_MIN_WIDTH) return(PANEL_MIN_WIDTH);
   if(width > PANEL_MAX_WIDTH) return(PANEL_MAX_WIDTH);
   return(width);
  }

void ResizePanelForContent(const string &lines[], const int usedRows)
  {
   int panelWidth = StringLen("EA Zone NeverDie MT5 v." + EA_VERSION) * PANEL_CHAR_WIDTH + PANEL_HORIZONTAL_PAD * 2;
   for(int i = 0; i < usedRows; i++)
     {
      int lineWidth = StringLen(lines[i]) * PANEL_CHAR_WIDTH + PANEL_HORIZONTAL_PAD * 2;
      if(lineWidth > panelWidth) panelWidth = lineWidth;
     }

   panelWidth = ClampPanelWidth(panelWidth);
   int panelCenterX = PANEL_X + panelWidth / 2;
   int panelHeight = PANEL_LINES_Y_OFFSET + MathMax(usedRows, 1) * PANEL_LINE_HEIGHT + PANEL_BOTTOM_PAD;

   ObjectSetInteger(0, g_panelPrefix + "_BG", OBJPROP_XSIZE, panelWidth);
   ObjectSetInteger(0, g_panelPrefix + "_BG", OBJPROP_YSIZE, panelHeight);
   ObjectSetInteger(0, g_panelPrefix + "_TITLE", OBJPROP_XDISTANCE, panelCenterX);
   for(int i = 0; i < PANEL_LINE_COUNT; i++)
      ObjectSetInteger(0, g_panelPrefix + "_LINE_" + IntegerToString(i), OBJPROP_XDISTANCE, panelCenterX);
  }

void UpdatePanel()
  {
   if(!InpShowPanel)
     {
      RemovePanel();
      return;
     }
   if(ObjectFind(0, g_panelPrefix + "_BG") == -1) CreatePanel();

   string lines[];
   color colors[];
   ArrayResize(lines, PANEL_LINE_COUNT);
   ArrayResize(colors, PANEL_LINE_COUNT);
   for(int i = 0; i < PANEL_LINE_COUNT; i++)
     {
      lines[i] = " ";
      colors[i] = clrBlack;
     }

   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double ddPercent = (balance > 0.0 ? MathMax((balance - equity) / balance * 100.0, 0.0) : 0.0);
   int row = 0;

   AddPanelRow(lines, colors, row, "---- Account Data ----", clrDimGray);
   AddPanelRow(lines, colors, row, "Balance: " + DoubleToString(balance, 2), clrBlack);
   AddPanelRow(lines, colors, row, "Equity:  " + DoubleToString(equity, 2), clrBlack);
   AddPanelRow(lines, colors, row, "DD:      " + DoubleToString(ddPercent, 1) + "%", clrBlack);
   AddPanelRow(lines, colors, row, " ", clrBlack);
   AddPanelRow(lines, colors, row, "---- Today ----", clrDimGray);
   double todayProfit = TodayClosedProfit();
   AddPanelRow(lines, colors, row, "Profit Today: " + DoubleToString(todayProfit, 2), ProfitColor(todayProfit));
   AddPanelRow(lines, colors, row, "Orders Today: " + IntegerToString(TodayClosedOrderCount()), clrBlack);
   AddPanelRow(lines, colors, row, "Open Orders:  " + IntegerToString(TotalOpenEaOrders()), clrBlack);
   AddPanelRow(lines, colors, row, " ", clrBlack);

   int tradeIndex = ActiveTradeZoneIndex();
   if(tradeIndex >= 0)
     {
      ZoneData zone = g_zones[tradeIndex];
      AddPanelRow(lines, colors, row, "Zone TRADE: " + SideText(zone.side), clrDarkGreen);
      AddPanelRow(lines, colors, row, "Label: " + ZoneLabelText(zone), clrBlack);
      AddPanelRow(lines, colors, row, "Low/High: " + DoubleToString(zone.low, _Digits) + " - " + DoubleToString(zone.high, _Digits), clrBlack);
      AddPanelRow(lines, colors, row, "SL: " + ZoneSlText(zone), clrBlack);
     }
   else
     {
      ZoneData watchZone;
      double distance = 0.0;
      if(FindNearestWatchZoneForDisplay(watchZone, distance))
        {
         AddPanelRow(lines, colors, row, "Zone WATCH Next: " + SideText(watchZone.side), clrDarkOrange);
         AddPanelRow(lines, colors, row, "Label: " + ZoneLabelText(watchZone), clrBlack);
         AddPanelRow(lines, colors, row, "Low/High: " + DoubleToString(watchZone.low, _Digits) + " - " + DoubleToString(watchZone.high, _Digits), clrBlack);
         AddPanelRow(lines, colors, row, "SL: " + ZoneSlText(watchZone), clrBlack);
         AddPanelRow(lines, colors, row, "Distance: " + DoubleToString(distance, _Digits), clrBlack);
        }
      else
        {
         AddPanelRow(lines, colors, row, "Zone: none", clrGray);
        }
     }

   ResizePanelForContent(lines, row);
   for(int i = 0; i < PANEL_LINE_COUNT; i++)
     {
      string name = g_panelPrefix + "_LINE_" + IntegerToString(i);
      ObjectSetString(0, name, OBJPROP_TEXT, lines[i]);
      ObjectSetInteger(0, name, OBJPROP_COLOR, colors[i]);
      ObjectSetInteger(0, name, OBJPROP_YDISTANCE, PANEL_Y + PANEL_LINES_Y_OFFSET + i * PANEL_LINE_HEIGHT);
      ObjectSetInteger(0, name, OBJPROP_ANCHOR, ANCHOR_UPPER);
     }
  }

int OnInit()
  {
   if(!ValidateInputs()) return(INIT_PARAMETERS_INCORRECT);
   g_trade.SetExpertMagicNumber(InpMagicNumber);
   g_trade.SetDeviationInPoints(20);
   g_trade.SetTypeFillingBySymbol(_Symbol);
   if(RemoteJsonEnabled()) EventSetTimer(InpZonesPollSeconds);
   FetchZonesOnInit();
   RestoreCampaignsFromOpenPositions();
   if(InpShowPanel) CreatePanel();
   g_dcaBarOpenSeen = iTime(_Symbol, DcaTimeframe(), 0);
   UpdatePanel();
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   RemovePanel();
  }

void OnTimer()
  {
   FetchZonesOnSchedule();
   UpdatePanel();
  }

void OnTick()
  {
   if(!EnvironmentReady())
     {
      UpdatePanel();
      return;
     }

   RestoreCampaignsFromOpenPositions();
   CleanupCampaignsWithoutPositions();
   ActivateNearestWatchZone();
   RemoveTouchedTradeZone();
   ManagePlanChinhFollowEntry();

   bool onFirstTickOfNewDcaBar = false;
   datetime dcaBarOpen = iTime(_Symbol, DcaTimeframe(), 0);
   if(dcaBarOpen > 0 && dcaBarOpen != g_dcaBarOpenSeen)
     {
      g_dcaBarOpenSeen = dcaBarOpen;
      onFirstTickOfNewDcaBar = true;
     }

   ManageActiveTradeEntry();
   ManageCampaigns(onFirstTickOfNewDcaBar);
   UpdatePanel();
  }
