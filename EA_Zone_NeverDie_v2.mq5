#property strict
#property description "EA Zone NeverDie MT5 v2.15"

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
   double             entry;
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
input double         InpLotSize                = 0.06;
input double         InpPlanFollowLotSize      = 0.03;
input double         InpMultiplier             = 1.25;
input int            InpGridStep               = 5000;
input int            InpMaxGridLevels          = 50;
input long           InpMagicNumber            = 20250215;
input int            InpTakeProfit             = 3000;
input ENUM_ND_DCA_TF InpDcaGridTimeframe       = ND_DCA_M15;
input int            InpDcaClosedBarsRequired  = 1;
input int            InpDcaPrevOrderDistance   = 12000;
input double         InpZoneActivateBand       = 3.0;

input group "=== DISPLAY ==="
input bool           InpShowPanel              = true;

input group "=== REMOTE ZONES JSON ==="
input string         InpZonesJsonUrl           = "https://res.cloudinary.com/easy-toeic/raw/upload/automation_tool/ea_neverdie/neverdie_XAUUSD.json";
input int            InpZonesPollSeconds       = 300;
input string         InpZonesBearer            = "";
input double         InpZonesSlBuffer          = 30.0;

input group "=== SESSION STOP (VN) ==="
input bool           InpSessionStopEnabled     = true;
input int            InpSessionStopHour        = 2;
input int            InpSessionStopMinute      = 0;
input int            InpSessionStopUtcOffsetMin = 420;

input group "=== DEBUG ==="
input bool           InpDebugLog               = true;
input bool           InpDebugTraceDecisions    = false;

const string EA_VERSION = "2.15";
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
long g_stoppedBuyZoneMagics[];
long g_stoppedSellZoneMagics[];
datetime g_dcaBarOpenSeen = 0;
int g_completedJsonFetchWindowKey = -1;
int g_zoneFetchSequence = 0;
int g_sessionStopClearedLocalDateKey = -1;
int g_morningResumeLocalDateKey = -1;
string g_panelPrefix = "ZoneNeverDieV2Panel";

string BoolText(const bool value)
  {
   return(value ? "true" : "false");
  }

string SideText(const ENUM_POSITION_TYPE side)
  {
   if(side == POSITION_TYPE_BUY) return("BUY");
   if(side == POSITION_TYPE_SELL) return("SELL");
   return("UNKNOWN");
  }

string StatusText(const ENUM_ZONE_STATUS status)
  {
   if(status == ZONE_STATUS_WATCH) return("WATCH");
   if(status == ZONE_STATUS_TRADE) return("TRADE");
   return("UNKNOWN");
  }

string PriceText(const double value)
  {
   return(DoubleToString(value, _Digits));
  }

void DebugLog(const string message)
  {
   if(!InpDebugLog) return;
   Print("[ZND_V2][DEBUG] ", message);
  }

void DebugTrace(const string message)
  {
   if(!InpDebugTraceDecisions) return;
   DebugLog("[TRACE] " + message);
  }

string ZoneDebugText(const ZoneData &zone)
  {
   return(StringFormat("side=%s status=%s label=%s magic=%s low=%s high=%s entry=%s sl=%s created=%s seq=%d",
                       SideText(zone.side),
                       StatusText(zone.status),
                       zone.label,
                       IntegerToString(zone.magic),
                       PriceText(zone.low),
                       PriceText(zone.high),
                       PriceText(zone.entry),
                       PriceText(zone.sl),
                       TimeToString(zone.createdAt, TIME_DATE | TIME_SECONDS),
                       zone.fetchSequence));
  }

string CampaignDebugText(const CampaignData &campaign)
  {
   return(StringFormat("side=%s magic=%s low=%s high=%s sl=%s baseLot=%.2f active=%s",
                       SideText(campaign.side),
                       IntegerToString(campaign.magic),
                       PriceText(campaign.low),
                       PriceText(campaign.high),
                       PriceText(campaign.sl),
                       campaign.baseLot,
                       BoolText(campaign.active)));
  }

string BasketDebugText(const BasketInfo &basket)
  {
   return(StringFormat("count=%d volume=%.2f avg=%s profit=%.2f lastPrice=%s lastTime=%s",
                       basket.count,
                       basket.totalVolume,
                       PriceText(basket.averagePrice),
                       basket.floatingProfit,
                       PriceText(basket.lastOpenPrice),
                       TimeToString(basket.lastOpenTime, TIME_DATE | TIME_SECONDS)));
  }

ENUM_TIMEFRAMES DcaTimeframe()
  {
   return(InpDcaGridTimeframe == ND_DCA_M15 ? PERIOD_M15 : PERIOD_M5);
  }

bool ValidateInputs()
  {
   if(InpLotSize <= 0.0) { DebugLog("Invalid input: InpLotSize must be > 0"); return(false); }
   if(InpPlanFollowLotSize <= 0.0) { DebugLog("Invalid input: InpPlanFollowLotSize must be > 0"); return(false); }
   if(InpMultiplier < 1.0) { DebugLog("Invalid input: InpMultiplier must be >= 1"); return(false); }
   if(InpGridStep <= 0) { DebugLog("Invalid input: InpGridStep must be > 0"); return(false); }
   if(InpMaxGridLevels < 1) { DebugLog("Invalid input: InpMaxGridLevels must be >= 1"); return(false); }
   if(InpTakeProfit <= 0) { DebugLog("Invalid input: InpTakeProfit must be > 0"); return(false); }
   if(InpDcaClosedBarsRequired < 1) { DebugLog("Invalid input: InpDcaClosedBarsRequired must be >= 1"); return(false); }
   if(InpDcaPrevOrderDistance < 0) { DebugLog("Invalid input: InpDcaPrevOrderDistance must be >= 0"); return(false); }
   if(InpZoneActivateBand < 0.0) { DebugLog("Invalid input: InpZoneActivateBand must be >= 0"); return(false); }
   if(InpZonesPollSeconds < 0) { DebugLog("Invalid input: InpZonesPollSeconds must be >= 0"); return(false); }
   if(InpSessionStopHour < 0 || InpSessionStopHour > 23) { DebugLog("Invalid input: InpSessionStopHour must be 0-23"); return(false); }
   if(InpSessionStopMinute < 0 || InpSessionStopMinute > 59) { DebugLog("Invalid input: InpSessionStopMinute must be 0-59"); return(false); }
   return(true);
  }

datetime SessionLocalNow()
  {
   return(TimeGMT() + (datetime)InpSessionStopUtcOffsetMin * 60);
  }

int SessionLocalDateKey()
  {
   MqlDateTime tm;
   TimeToStruct(SessionLocalNow(), tm);
   return(tm.year * 10000 + tm.mon * 100 + tm.day);
  }

int SessionLocalMinuteOfDay()
  {
   MqlDateTime tm;
   TimeToStruct(SessionLocalNow(), tm);
   return(tm.hour * 60 + tm.min);
  }

int SessionStopMinuteOfDay()
  {
   return(InpSessionStopHour * 60 + InpSessionStopMinute);
  }

string MorningResumeGlobalNameForDate(const int localDateKey)
  {
   return("ZND_V2_MR_" + IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN)) + "_" + _Symbol + "_" + IntegerToString(localDateKey));
  }

bool HasMorningSlotResumeForLocalDay(const int localDateKey)
  {
   if(g_morningResumeLocalDateKey == localDateKey) return(true);
   return(GlobalVariableCheck(MorningResumeGlobalNameForDate(localDateKey)));
  }

void MarkMorningSlotResumeForLocalDay()
  {
   int localDate = SessionLocalDateKey();
   g_morningResumeLocalDateKey = localDate;
   GlobalVariableSet(MorningResumeGlobalNameForDate(localDate), (double)TimeGMT());
   DebugLog("Morning slot resume marked for local date. dateKey=" + IntegerToString(localDate));
  }

void RestoreMorningResumeStateFromGlobals()
  {
   int localDate = SessionLocalDateKey();
   if(HasMorningSlotResumeForLocalDay(localDate))
      g_morningResumeLocalDateKey = localDate;
  }

bool IsExpectedMorningSlotLabel(const string expectedLabel)
  {
   string lower = expectedLabel;
   StringToLower(lower);
   return(lower == "plan_chinh__sang");
  }

void RecordMorningSlotResumeAfterFetch(const string expectedLabel)
  {
   if(!IsExpectedMorningSlotLabel(expectedLabel)) return;
   MarkMorningSlotResumeForLocalDay();
  }

bool IsSessionTradingPaused()
  {
   if(!InpSessionStopEnabled || !RemoteJsonEnabled()) return(false);
   if(SessionLocalMinuteOfDay() < SessionStopMinuteOfDay()) return(false);
   return(!HasMorningSlotResumeForLocalDay(SessionLocalDateKey()));
  }

void ClearAllZonesForSessionStop()
  {
   for(int i = ArraySize(g_zones) - 1; i >= 0; i--)
     {
      if(g_zones[i].status == ZONE_STATUS_TRADE)
         KeepCampaignForZone(g_zones[i]);
      KeepPlanFollowCampaignForZone(g_zones[i]);
      DebugLog("Session stop removed zone. " + ZoneDebugText(g_zones[i]));
      ArrayRemove(g_zones, i, 1);
     }
  }

void EnsureSessionStopZonesCleared()
  {
   if(!IsSessionTradingPaused()) return;

   int localDate = SessionLocalDateKey();
   if(g_sessionStopClearedLocalDateKey == localDate) return;

   ClearAllZonesForSessionStop();
   g_sessionStopClearedLocalDateKey = localDate;
   DebugLog("Session stop applied: zones cleared, open baskets kept for DCA/TP. localDateKey=" + IntegerToString(localDate));
  }

bool EnvironmentReady()
  {
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
     {
      DebugTrace("Environment not ready: terminal trade is disabled");
      return(false);
     }
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED))
     {
      DebugTrace("Environment not ready: EA trade is disabled");
      return(false);
     }
   int bars = Bars(_Symbol, _Period);
   if(bars < 100)
     {
      DebugTrace("Environment not ready: insufficient bars=" + IntegerToString(bars));
      return(false);
     }
   return(true);
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

string JsonFetchRequestUrl()
  {
   string separator = (StringFind(InpZonesJsonUrl, "?") >= 0 ? "&" : "?");
   return(InpZonesJsonUrl + separator + "t=" + IntegerToString((long)TimeGMT()));
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

long StableMagicFromKey(const string key)
  {
   long hash = 2166136261;
   for(int i = 0; i < StringLen(key); i++)
     {
      hash = (hash ^ StringGetCharacter(key, i)) * 16777619;
      if(hash < 0) hash = -hash;
      hash %= 900000;
     }
   return(InpMagicNumber + 1 + hash);
  }

long StableZoneMagicWithSalt(const ENUM_POSITION_TYPE side, const double low, const double high, const int buySalt, const int sellSalt)
  {
   int sideSalt = (side == POSITION_TYPE_BUY ? buySalt : sellSalt);
   string key = _Symbol + "_" + IntegerToString(sideSalt) + "_" + DoubleToString(low, _Digits) + "_" + DoubleToString(high, _Digits);
   return(StableMagicFromKey(key));
  }

long StableDailyZoneMagic(const ENUM_POSITION_TYPE side)
  {
   int sideSalt = (side == POSITION_TYPE_BUY ? 17 : 53);
   string key = _Symbol + "_daily_zone_" + IntegerToString(sideSalt);
   return(StableMagicFromKey(key));
  }

long StableDailyPlanFollowMagic(const ENUM_POSITION_TYPE side)
  {
   int sideSalt = (side == POSITION_TYPE_BUY ? 71 : 89);
   string key = _Symbol + "_daily_follow_" + IntegerToString(sideSalt);
   return(StableMagicFromKey(key));
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

string StoppedOutGlobalName(const ENUM_POSITION_TYPE side, const long magic)
  {
   string sideKey = (side == POSITION_TYPE_BUY ? "BUY" : "SELL");
   return("ZND_V2_SL_" + IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN)) + "_" + _Symbol + "_" + sideKey + "_" + IntegerToString(magic));
  }

bool IsZoneStoppedOut(const ENUM_POSITION_TYPE side, const long magic)
  {
   if(side == POSITION_TYPE_BUY)
     {
      for(int i = 0; i < ArraySize(g_stoppedBuyZoneMagics); i++)
         if(g_stoppedBuyZoneMagics[i] == magic) return(true);
     }
   else
     {
      for(int i = 0; i < ArraySize(g_stoppedSellZoneMagics); i++)
         if(g_stoppedSellZoneMagics[i] == magic) return(true);
     }

   if(GlobalVariableCheck(StoppedOutGlobalName(side, magic))) return(true);
   return(false);
  }

void MarkZoneStoppedOut(const ENUM_POSITION_TYPE side, const long magic)
  {
   if(IsZoneStoppedOut(side, magic))
     {
      DebugTrace("Zone already marked stopped out. side=" + SideText(side) + " magic=" + IntegerToString(magic));
      return;
     }
   GlobalVariableSet(StoppedOutGlobalName(side, magic), (double)TimeCurrent());
   DebugLog("Marked zone stopped out. side=" + SideText(side) + " magic=" + IntegerToString(magic));

   if(side == POSITION_TYPE_BUY)
     {
      int size = ArraySize(g_stoppedBuyZoneMagics);
      ArrayResize(g_stoppedBuyZoneMagics, size + 1);
      g_stoppedBuyZoneMagics[size] = magic;
      return;
     }

   int size = ArraySize(g_stoppedSellZoneMagics);
   ArrayResize(g_stoppedSellZoneMagics, size + 1);
   g_stoppedSellZoneMagics[size] = magic;
  }

void ClearZoneStoppedOut(const ENUM_POSITION_TYPE side, const long magic)
  {
   string gvName = StoppedOutGlobalName(side, magic);
   if(GlobalVariableCheck(gvName))
      GlobalVariableDel(gvName);

   if(side == POSITION_TYPE_BUY)
     {
      for(int i = ArraySize(g_stoppedBuyZoneMagics) - 1; i >= 0; i--)
        {
         if(g_stoppedBuyZoneMagics[i] != magic) continue;
         ArrayRemove(g_stoppedBuyZoneMagics, i, 1);
        }
      return;
     }

   for(int i = ArraySize(g_stoppedSellZoneMagics) - 1; i >= 0; i--)
     {
      if(g_stoppedSellZoneMagics[i] != magic) continue;
      ArrayRemove(g_stoppedSellZoneMagics, i, 1);
     }
  }

bool IsPlanChinhLabel(const string label)
  {
   string value = label;
   StringToLower(value);
   return(StringFind(value, "plan_chinh__") == 0);
  }

string JsonFetchSlotExpectedLabel(const int slot)
  {
   if(slot == 0) return("plan_chinh__sang");
   if(slot == 1) return("plan_chinh__chieu");
   if(slot == 2) return("plan_chinh__toi");
   return("");
  }

int JsonFetchSlotFromWindowKey(const int windowKey)
  {
   if(windowKey < 0) return(-1);
   return(windowKey % 10);
  }

bool IsExpectedJsonFetchLabel(const string label, const string expectedLabel)
  {
   if(StringLen(expectedLabel) <= 0)
      return(IsPlanChinhLabel(label));

   string value = label;
   string expected = expectedLabel;
   StringToLower(value);
   StringToLower(expected);
   return(value == expected);
  }

void NormalizeZonePrices(double &low, double &high)
  {
   double minPrice = MathMin(low, high);
   double maxPrice = MathMax(low, high);
   low = NormalizeDouble(minPrice, _Digits);
   high = NormalizeDouble(maxPrice, _Digits);
  }

int FindMainZoneIndexBySide(const ENUM_POSITION_TYPE side)
  {
   for(int i = 0; i < ArraySize(g_zones); i++)
      if(g_zones[i].side == side)
         return(i);
   return(-1);
  }

double MergedZoneSl(const ENUM_POSITION_TYPE side, const double existingSl, const double newSl)
  {
   if(newSl <= 0.0) return(existingSl);
   if(existingSl <= 0.0) return(NormalizeDouble(newSl, _Digits));
   if(side == POSITION_TYPE_BUY)
      return(NormalizeDouble(MathMin(existingSl, newSl), _Digits));
   return(NormalizeDouble(MathMax(existingSl, newSl), _Digits));
  }

void MergeZoneEntry(ZoneData &zone, const double jsonLow)
  {
   if(jsonLow <= 0.0) return;
   if(zone.entry <= 0.0)
      zone.entry = NormalizeDouble(jsonLow, _Digits);
   else if(zone.side == POSITION_TYPE_BUY)
      zone.entry = NormalizeDouble(MathMax(zone.entry, jsonLow), _Digits);
   else
      zone.entry = NormalizeDouble(MathMin(zone.entry, jsonLow), _Digits);
  }

void MergeZoneBounds(ZoneData &zone, const double newLow, const double newHigh, const double newSl)
  {
   double low = newLow;
   double high = newHigh;
   NormalizeZonePrices(low, high);
   zone.low = (zone.low <= 0.0 ? low : NormalizeDouble(MathMin(zone.low, low), _Digits));
   zone.high = (zone.high <= 0.0 ? high : NormalizeDouble(MathMax(zone.high, high), _Digits));
   zone.sl = MergedZoneSl(zone.side, zone.sl, newSl);
  }

void ApplyFreshWatchZoneFromJson(ZoneData &zone,
                                 const ENUM_POSITION_TYPE side,
                                 const double low,
                                 const double high,
                                 const double jsonLow,
                                 const double sl,
                                 const string label,
                                 const long magic)
  {
   zone.side = side;
   zone.status = ZONE_STATUS_WATCH;
   zone.low = low;
   zone.high = high;
   zone.entry = NormalizeDouble(jsonLow, _Digits);
   zone.sl = MergedZoneSl(side, 0.0, sl);
   zone.label = label;
   zone.magic = magic;
   zone.fetchSequence = g_zoneFetchSequence;
  }

void RemoveOppositeMainZone(const ENUM_POSITION_TYPE side)
  {
   ENUM_POSITION_TYPE opposite = (side == POSITION_TYPE_BUY ? POSITION_TYPE_SELL : POSITION_TYPE_BUY);
   for(int i = ArraySize(g_zones) - 1; i >= 0; i--)
     {
      if(g_zones[i].side != opposite) continue;
      if(g_zones[i].status == ZONE_STATUS_TRADE)
         KeepCampaignForZone(g_zones[i]);
      DebugLog("Removed opposite-direction main zone before applying new plan_chinh. incomingSide=" + SideText(side) + " removed={" + ZoneDebugText(g_zones[i]) + "}");
      ArrayRemove(g_zones, i, 1);
     }
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
   if(!IsPlanChinhLabel(label))
      DebugTrace("Skip JSON zone with non-plan_chinh label. side=" + SideText(side) + " label=" + label);
   if(!IsPlanChinhLabel(label)) return;

   RemoveOppositeMainZone(side);
   double jsonLow = low;
   NormalizeZonePrices(low, high);
   long magic = StableDailyZoneMagic(side);
   bool resetStoppedOutZone = IsZoneStoppedOut(side, magic);
   if(resetStoppedOutZone)
     {
      ClearZoneStoppedOut(side, magic);
      DebugLog("Cleared stopped-out flag for new JSON fetch window. side=" + SideText(side) + " magic=" + IntegerToString(magic) + " label=" + label);
     }
   int index = FindMainZoneIndexBySide(side);
   g_zoneFetchSequence++;

   if(resetStoppedOutZone)
     {
      if(index >= 0)
        {
         ApplyFreshWatchZoneFromJson(g_zones[index], side, low, high, jsonLow, sl, label, magic);
         g_zones[index].createdAt = TimeCurrent();
         DebugLog("Replaced main zone from JSON after stopped-out reset. " + ZoneDebugText(g_zones[index]));
         return;
        }

      int size = ArraySize(g_zones);
      ArrayResize(g_zones, size + 1);
      ZoneData zone;
      ApplyFreshWatchZoneFromJson(zone, side, low, high, jsonLow, sl, label, magic);
      zone.createdAt = TimeCurrent();
      g_zones[size] = zone;
      DebugLog("Loaded new main zone from JSON after stopped-out reset. " + ZoneDebugText(zone));
      return;
     }

   if(index >= 0)
     {
      MergeZoneBounds(g_zones[index], low, high, sl);
      MergeZoneEntry(g_zones[index], jsonLow);
      g_zones[index].label = label;
      g_zones[index].magic = magic;
      g_zones[index].fetchSequence = g_zoneFetchSequence;
      if(g_zones[index].status != ZONE_STATUS_TRADE)
         g_zones[index].status = ZONE_STATUS_WATCH;
      DebugLog("Merged main zone from JSON. " + ZoneDebugText(g_zones[index]));
      return;
     }

   int size = ArraySize(g_zones);
   ArrayResize(g_zones, size + 1);
   ZoneData zone;
   ApplyFreshWatchZoneFromJson(zone, side, low, high, jsonLow, sl, label, magic);
   zone.createdAt = TimeCurrent();
   g_zones[size] = zone;
   DebugLog("Loaded new main zone from JSON. " + ZoneDebugText(zone));
  }

bool ApplyZonesJson(const string json, const string expectedLabel)
  {
   bool loadedExpectedSlot = false;
   double low;
   double high;
   double sl;
   string label;

   if(ParseSideZone(json, "buy", low, high, sl, label))
     {
      DebugLog("Parsed BUY JSON zone. low=" + PriceText(low) + " high=" + PriceText(high) + " sl=" + PriceText(sl) + " label=" + label);
      if(IsExpectedJsonFetchLabel(label, expectedLabel))
        {
         LoadWatchZone(POSITION_TYPE_BUY, low, high, sl, label);
         loadedExpectedSlot = true;
        }
      else
         DebugLog("Skip BUY JSON zone for different fetch slot. expected=" + expectedLabel + " label=" + label);
     }
   else
      DebugTrace("No valid BUY zone found in JSON");

   if(ParseSideZone(json, "sell", low, high, sl, label))
     {
      DebugLog("Parsed SELL JSON zone. low=" + PriceText(low) + " high=" + PriceText(high) + " sl=" + PriceText(sl) + " label=" + label);
      if(IsExpectedJsonFetchLabel(label, expectedLabel))
        {
         LoadWatchZone(POSITION_TYPE_SELL, low, high, sl, label);
         loadedExpectedSlot = true;
        }
      else
         DebugLog("Skip SELL JSON zone for different fetch slot. expected=" + expectedLabel + " label=" + label);
     }
   else
      DebugTrace("No valid SELL zone found in JSON");

   PruneDuplicateMainZones();
   SyncMainZoneCampaignsAfterMerge();
   return(loadedExpectedSlot);
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
         DebugLog("Removed previous-day zone before JSON fetch. " + ZoneDebugText(g_zones[i]));
         ArrayRemove(g_zones, i, 1);
        }
     }
  }

void ClearAllZonesBeforeMorningJsonFetch()
  {
   for(int i = ArraySize(g_zones) - 1; i >= 0; i--)
     {
      if(g_zones[i].status == ZONE_STATUS_TRADE)
         KeepCampaignForZone(g_zones[i]);
      DebugLog("Removed zone before morning JSON apply. " + ZoneDebugText(g_zones[i]));
      ArrayRemove(g_zones, i, 1);
     }
  }

bool FetchZonesJson(const string expectedLabel)
  {
   uchar request[];
   uchar result[];
   string responseHeaders;
   string headers = "";
   if(StringLen(InpZonesBearer) > 0)
      headers = "Authorization: Bearer " + InpZonesBearer + "\r\n";
   headers += "Cache-Control: no-cache\r\n";
   headers += "Pragma: no-cache\r\n";

   string requestUrl = JsonFetchRequestUrl();
   DebugLog("Fetching zones JSON. url=" + requestUrl + " expectedLabel=" + expectedLabel + " bearerSet=" + BoolText(StringLen(InpZonesBearer) > 0));
   CleanupPreviousDayZonesBeforeJsonFetch();

   ResetLastError();
   int code = WebRequest("GET", requestUrl, headers, 15000, request, result, responseHeaders);
   if(code != 200)
     {
      PrintFormat("NeverDie v2 JSON fetch failed. code=%d error=%d", code, GetLastError());
      return(false);
     }

   string body = CharArrayToString(result);
   DebugLog("Fetched zones JSON. bytes=" + IntegerToString(ArraySize(result)) + " bodyLength=" + IntegerToString(StringLen(body)));

   string expectedLower = expectedLabel;
   StringToLower(expectedLower);
   if(expectedLower == "plan_chinh__sang")
     {
      ClearAllZonesBeforeMorningJsonFetch();
      g_zoneFetchSequence++;
      DebugLog("Morning JSON fetch: cleared today's zones and will create fresh main zones. fetchSequence=" + IntegerToString(g_zoneFetchSequence));
     }

   if(!ApplyZonesJson(body, expectedLabel))
     {
      if(StringLen(expectedLabel) > 0)
         PrintFormat("NeverDie v2 JSON missing expected slot %s: %s", expectedLabel, body);
      else
         PrintFormat("NeverDie v2 JSON parse failed: %s", body);
      return(false);
     }
   return(true);
  }

void FetchZonesOnInit()
  {
   if(!RemoteJsonEnabled())
     {
      DebugLog("Remote JSON disabled on init. tester=" + BoolText((bool)MQLInfoInteger(MQL_TESTER)) + " pollSeconds=" + IntegerToString(InpZonesPollSeconds) + " urlLength=" + IntegerToString(StringLen(InpZonesJsonUrl)));
      return;
     }
   int windowKey = CurrentJsonFetchWindowKey();
   string expectedLabel = "";
   if(windowKey >= 0)
      expectedLabel = JsonFetchSlotExpectedLabel(JsonFetchSlotFromWindowKey(windowKey));
   if(!FetchZonesJson(expectedLabel)) return;
   if(windowKey >= 0) g_completedJsonFetchWindowKey = windowKey;
   RecordMorningSlotResumeAfterFetch(expectedLabel);
   DebugLog("Initial zones JSON fetch complete. windowKey=" + IntegerToString(windowKey) + " zones=" + IntegerToString(ArraySize(g_zones)));
  }

void FetchZonesOnSchedule()
  {
   if(!RemoteJsonEnabled()) return;
   int windowKey = CurrentJsonFetchWindowKey();
   if(windowKey < 0)
     {
      DebugTrace("Scheduled JSON fetch skipped: outside allowed window");
      return;
     }
   if(windowKey == g_completedJsonFetchWindowKey)
     {
      DebugTrace("Scheduled JSON fetch skipped: window already completed. windowKey=" + IntegerToString(windowKey));
      return;
     }
   string expectedLabel = JsonFetchSlotExpectedLabel(JsonFetchSlotFromWindowKey(windowKey));
   DebugLog("Scheduled zones JSON fetch started. windowKey=" + IntegerToString(windowKey) + " expectedLabel=" + expectedLabel);
   if(FetchZonesJson(expectedLabel))
     {
      g_completedJsonFetchWindowKey = windowKey;
      RecordMorningSlotResumeAfterFetch(expectedLabel);
      DebugLog("Scheduled zones JSON fetch complete. windowKey=" + IntegerToString(windowKey) + " expectedLabel=" + expectedLabel + " zones=" + IntegerToString(ArraySize(g_zones)));
     }
  }

double ZoneActivationRangeMin(const ZoneData &zone)
  {
   if(zone.side == POSITION_TYPE_SELL)
      return(zone.entry);
   return(zone.low);
  }

double ZoneActivationRangeMax(const ZoneData &zone)
  {
   if(zone.side == POSITION_TYPE_SELL)
      return(zone.high);
   return(zone.entry);
  }

bool IsInActivationBand(const ZoneData &zone, const double price)
  {
   if(zone.entry <= 0.0) return(false);
   if(zone.side == POSITION_TYPE_BUY)
     {
      if(zone.low <= 0.0) return(false);
      return(price >= zone.low - InpZoneActivateBand && price <= zone.entry + InpZoneActivateBand);
     }
   if(zone.high <= 0.0) return(false);
   return(price >= zone.entry - InpZoneActivateBand && price <= zone.high + InpZoneActivateBand);
  }

double ActivationDistance(const ZoneData &zone, const double price)
  {
   double rangeMin = ZoneActivationRangeMin(zone);
   double rangeMax = ZoneActivationRangeMax(zone);
   if(price < rangeMin) return(rangeMin - price);
   if(price > rangeMax) return(price - rangeMax);
   return(0.0);
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
      DebugTrace("Updated campaign for zone. " + CampaignDebugText(g_campaigns[index]) + " zone={" + ZoneDebugText(zone) + "}");
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
   DebugLog("Created campaign for zone. " + CampaignDebugText(g_campaigns[size]) + " zone={" + ZoneDebugText(zone) + "}");
  }

void KeepCampaignForZone(const ZoneData &zone)
  {
   KeepCampaignForZoneWithMagic(zone, zone.magic, InpLotSize);
  }

void KeepPlanFollowCampaignForZone(const ZoneData &zone)
  {
   long followMagic = StableDailyPlanFollowMagic(zone.side);
   KeepCampaignForZoneWithMagic(zone, followMagic, InpPlanFollowLotSize);
  }

bool IsPlanFollowCampaign(const CampaignData &campaign)
  {
   return(campaign.magic == StableDailyPlanFollowMagic(campaign.side));
  }

void PruneDuplicateMainZones()
  {
   for(int sideIdx = 0; sideIdx < 2; sideIdx++)
     {
      ENUM_POSITION_TYPE side = (sideIdx == 0 ? POSITION_TYPE_BUY : POSITION_TYPE_SELL);
      int keepIndex = FindMainZoneIndexBySide(side);
      if(keepIndex < 0) continue;

      for(int i = ArraySize(g_zones) - 1; i >= 0; i--)
        {
         if(i == keepIndex || g_zones[i].side != side) continue;
         DebugLog("Merging duplicate main zone into kept zone. kept={" + ZoneDebugText(g_zones[keepIndex]) + "} duplicate={" + ZoneDebugText(g_zones[i]) + "}");
         MergeZoneBounds(g_zones[keepIndex], g_zones[i].low, g_zones[i].high, g_zones[i].sl);
         MergeZoneEntry(g_zones[keepIndex], g_zones[i].entry);
         if(g_zones[i].fetchSequence > g_zones[keepIndex].fetchSequence)
           {
            g_zones[keepIndex].fetchSequence = g_zones[i].fetchSequence;
            g_zones[keepIndex].label = g_zones[i].label;
           }
         if(g_zones[i].status == ZONE_STATUS_TRADE)
            g_zones[keepIndex].status = ZONE_STATUS_TRADE;
         ArrayRemove(g_zones, i, 1);
         if(i < keepIndex) keepIndex--;
        }
     }
  }

void SyncMainZoneCampaignsAfterMerge()
  {
   for(int i = 0; i < ArraySize(g_zones); i++)
     {
      if(g_zones[i].status == ZONE_STATUS_TRADE)
         KeepCampaignForZone(g_zones[i]);
      KeepPlanFollowCampaignForZone(g_zones[i]);
     }
  }

int LatestPlanFollowCampaignIndex()
  {
   for(int i = ArraySize(g_campaigns) - 1; i >= 0; i--)
      if(g_campaigns[i].active && IsPlanFollowCampaign(g_campaigns[i]))
         return(i);
   return(-1);
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
      DebugLog("Restored campaign from open position. ticket=" + IntegerToString((long)ticket) + " comment=" + PositionGetString(POSITION_COMMENT) + " " + CampaignDebugText(g_campaigns[size]));
     }
  }

void RemoveCurrentTradeZoneBeforeActivation()
  {
   for(int i = ArraySize(g_zones) - 1; i >= 0; i--)
     {
      if(g_zones[i].status != ZONE_STATUS_TRADE) continue;
      KeepCampaignForZone(g_zones[i]);
      DebugLog("Removed existing TRADE zone before activating nearest zone. " + ZoneDebugText(g_zones[i]));
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

   if(bestIndex < 0)
     {
      DebugTrace("No watch zone in activation band. price=" + PriceText(price) + " zones=" + IntegerToString(ArraySize(g_zones)));
      return;
     }
   DebugLog("Activating nearest watch zone. price=" + PriceText(price) + " distancePoints=" + DoubleToString(bestDistance / _Point, 1) + " zone={" + ZoneDebugText(g_zones[bestIndex]) + "}");
   RemoveCurrentTradeZoneBeforeActivation();
   for(int i = 0; i < ArraySize(g_zones); i++)
     {
      if(g_zones[i].magic != bestMagic) continue;
      g_zones[i].status = ZONE_STATUS_TRADE;
      KeepCampaignForZone(g_zones[i]);
      DebugLog("Activated TRADE zone. " + ZoneDebugText(g_zones[i]));
      return;
     }
  }

void RemoveZoneAt(const int index)
  {
   if(index < 0 || index >= ArraySize(g_zones)) return;
   DebugLog("Removing touched trade zone. " + ZoneDebugText(g_zones[index]));
   ZoneData zone = g_zones[index];
   KeepCampaignForZone(g_zones[index]);
   KeepPlanFollowCampaignForZone(zone);
   MarkZoneStoppedOut(g_zones[index].side, g_zones[index].magic);
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
      bool touchesBuySl = (zone.sl > 0.0 && tick.bid <= zone.sl - InpZonesSlBuffer);
      bool touchesSellSl = (zone.sl > 0.0 && tick.ask >= zone.sl + InpZonesSlBuffer);

      if(zone.side == POSITION_TYPE_SELL && (touchesLow || touchesSellSl))
        {
         DebugLog("SELL trade zone touched boundary. bid=" + PriceText(tick.bid) + " ask=" + PriceText(tick.ask) + " touchesLow=" + BoolText(touchesLow) + " touchesSellSl=" + BoolText(touchesSellSl) + " zone={" + ZoneDebugText(zone) + "}");
         RemoveZoneAt(i);
        }
      else if(zone.side == POSITION_TYPE_BUY && (touchesHigh || touchesBuySl))
        {
         DebugLog("BUY trade zone touched boundary. bid=" + PriceText(tick.bid) + " ask=" + PriceText(tick.ask) + " touchesHigh=" + BoolText(touchesHigh) + " touchesBuySl=" + BoolText(touchesBuySl) + " zone={" + ZoneDebugText(zone) + "}");
         RemoveZoneAt(i);
        }
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
   if(ActiveTradeZoneIndex() >= 0)
      DebugTrace("FOLLOW entry skipped: active TRADE zone exists");
   if(ActiveTradeZoneIndex() >= 0) return;

   int zoneIndex = LatestPlanChinhZoneIndex();
   if(zoneIndex < 0)
     {
      int campaignIndex = LatestPlanFollowCampaignIndex();
      if(campaignIndex < 0)
        {
         DebugTrace("FOLLOW entry skipped: no latest plan_chinh zone or retained follow campaign");
         return;
        }
      OpenPlanFollowCampaign(campaignIndex, "removed-zone fallback");
      return;
     }

   ZoneData zone = g_zones[zoneIndex];
   long followMagic = StableDailyPlanFollowMagic(zone.side);
   KeepPlanFollowCampaignForZone(zone);
   int campaignIndex = FindCampaignIndex(followMagic);
   OpenPlanFollowCampaign(campaignIndex, "latest-zone");
  }

void OpenPlanFollowCampaign(const int campaignIndex, const string sourceReason)
  {
   if(campaignIndex < 0 || campaignIndex >= ArraySize(g_campaigns))
     {
      DebugLog("FOLLOW entry skipped: campaign not found. index=" + IntegerToString(campaignIndex) + " reason=" + sourceReason);
      return;
     }

   CampaignData campaign = g_campaigns[campaignIndex];
   BasketInfo basket;
   BuildBasket(campaign.side, campaign.magic, basket);
   if(basket.count > 0)
     {
      DebugTrace("FOLLOW entry skipped: basket already has positions. reason=" + sourceReason + " magic=" + IntegerToString(campaign.magic) + " " + BasketDebugText(basket));
      return;
     }

   DebugLog("Opening FOLLOW order. reason=" + sourceReason + " campaign={" + CampaignDebugText(g_campaigns[campaignIndex]) + "}");
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
   DebugLog("Sending order. tag=" + tag + " side=" + SideText(campaign.side) + " volume=" + DoubleToString(volume, 2) + " sl=" + PriceText(sl) + " comment=" + comment + " campaign={" + CampaignDebugText(campaign) + "}");

   bool ok = false;
   if(campaign.side == POSITION_TYPE_BUY)
      ok = g_trade.Buy(volume, _Symbol, 0.0, sl, 0.0, comment);
   else
      ok = g_trade.Sell(volume, _Symbol, 0.0, sl, 0.0, comment);

   DebugLog("Order result. ok=" + BoolText(ok) + " tag=" + tag + " retcode=" + IntegerToString((long)g_trade.ResultRetcode()) + " desc=" + g_trade.ResultRetcodeDescription() + " deal=" + IntegerToString((long)g_trade.ResultDeal()) + " order=" + IntegerToString((long)g_trade.ResultOrder()) + " price=" + PriceText(g_trade.ResultPrice()));
   return(ok);
  }

double CampaignTakeProfitPoints(const BasketInfo &basket)
  {
   if(basket.count >= 6)
     {
      double reduction = 0.15 + (basket.count - 6) * 0.02;
      return(InpTakeProfit * (1.0 - reduction));
     }
   return((double)InpTakeProfit);
  }

bool CampaignTakeProfitReached(const ENUM_POSITION_TYPE side, const BasketInfo &basket)
  {
   if(basket.count <= 0 || basket.totalVolume <= 0.0) return(false);

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick)) return(false);

   double currentPrice = ClosePriceForSide(side, tick);
   double takeProfitPoints = CampaignTakeProfitPoints(basket);
   double targetPrice = basket.averagePrice + DirectionMultiplier(side) * takeProfitPoints * _Point;

   if(side == POSITION_TYPE_BUY && currentPrice >= targetPrice && basket.floatingProfit > 0.0)
     {
      DebugLog("Campaign TP reached. side=BUY current=" + PriceText(currentPrice) + " target=" + PriceText(targetPrice) + " " + BasketDebugText(basket));
      return(true);
     }
   if(side == POSITION_TYPE_SELL && currentPrice <= targetPrice && basket.floatingProfit > 0.0)
     {
      DebugLog("Campaign TP reached. side=SELL current=" + PriceText(currentPrice) + " target=" + PriceText(targetPrice) + " " + BasketDebugText(basket));
      return(true);
     }
   DebugTrace("Campaign TP not reached. side=" + SideText(side) + " current=" + PriceText(currentPrice) + " target=" + PriceText(targetPrice) + " " + BasketDebugText(basket));
   return(false);
  }

bool CloseCampaign(const CampaignData &campaign)
  {
   bool allClosed = true;
   DebugLog("Closing campaign. " + CampaignDebugText(campaign));
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != campaign.magic) continue;
      if((ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE) != campaign.side) continue;
      bool ok = g_trade.PositionClose(ticket);
      DebugLog("Close position result. ok=" + BoolText(ok) + " ticket=" + IntegerToString((long)ticket) + " retcode=" + IntegerToString((long)g_trade.ResultRetcode()) + " desc=" + g_trade.ResultRetcodeDescription());
      if(!ok) allClosed = false;
     }
   DebugLog("Close campaign complete. allClosed=" + BoolText(allClosed) + " " + CampaignDebugText(campaign));
   return(allClosed);
  }

bool DcaPrevOrderDistanceReached(const double distance)
  {
   if(InpDcaPrevOrderDistance <= 0) return(false);
   return(distance >= InpDcaPrevOrderDistance);
  }

bool ShouldOpenDca(const CampaignData &campaign, const BasketInfo &basket, const bool onFirstTick)
  {
   if(basket.count <= 0)
     {
      DebugTrace("DCA skipped: basket has no positions. " + CampaignDebugText(campaign));
      return(false);
     }
   if(basket.count >= InpMaxGridLevels)
     {
      DebugTrace("DCA skipped: max grid levels reached. max=" + IntegerToString(InpMaxGridLevels) + " " + BasketDebugText(basket));
      return(false);
     }
   if(basket.floatingProfit >= 0.0)
     {
      DebugTrace("DCA skipped: basket profit is non-negative. " + BasketDebugText(basket));
      return(false);
     }

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
     {
      DebugTrace("DCA skipped: SymbolInfoTick failed");
      return(false);
     }
   double price = OpenPriceForSide(campaign.side, tick);
   double distance = MathAbs(price - basket.lastOpenPrice) / _Point;

   if(campaign.side == POSITION_TYPE_BUY && price >= basket.lastOpenPrice)
     {
      DebugTrace("DCA skipped: BUY price has not moved against last order. price=" + PriceText(price) + " last=" + PriceText(basket.lastOpenPrice));
      return(false);
     }
   if(campaign.side == POSITION_TYPE_SELL && price <= basket.lastOpenPrice)
     {
      DebugTrace("DCA skipped: SELL price has not moved against last order. price=" + PriceText(price) + " last=" + PriceText(basket.lastOpenPrice));
      return(false);
     }

   bool prevOrderDistanceReached = DcaPrevOrderDistanceReached(distance);
   if(!prevOrderDistanceReached)
     {
      if(distance < InpGridStep)
        {
         DebugTrace("DCA skipped: grid distance too small. distance=" + DoubleToString(distance, 1) + " required=" + IntegerToString(InpGridStep));
         return(false);
        }
      if(!onFirstTick)
        {
         DebugTrace("DCA skipped: waiting for first tick of new DCA bar. distance=" + DoubleToString(distance, 1));
         return(false);
        }

      int shiftSinceOpen = iBarShift(_Symbol, DcaTimeframe(), basket.lastOpenTime, false);
      if(shiftSinceOpen < InpDcaClosedBarsRequired)
        {
         DebugTrace("DCA skipped: not enough closed DCA bars. shiftSinceOpen=" + IntegerToString(shiftSinceOpen) + " required=" + IntegerToString(InpDcaClosedBarsRequired));
         return(false);
        }
     }

   DebugLog("DCA conditions met. distance=" + DoubleToString(distance, 1) + " prevOrderDistanceReached=" + BoolText(prevOrderDistanceReached) + " onFirstTick=" + BoolText(onFirstTick) + " campaign={" + CampaignDebugText(campaign) + "} basket={" + BasketDebugText(basket) + "}");
   return(true);
  }

void ManageActiveTradeEntry()
  {
   int zoneIndex = ActiveTradeZoneIndex();
   if(zoneIndex < 0)
     {
      DebugTrace("START entry skipped: no active TRADE zone");
      return;
     }

   ZoneData zone = g_zones[zoneIndex];
   KeepCampaignForZone(zone);
   BasketInfo basket;
   BuildBasket(zone.side, zone.magic, basket);
   if(basket.count > 0)
     {
      DebugTrace("START entry skipped: basket already has positions. " + BasketDebugText(basket) + " zone={" + ZoneDebugText(zone) + "}");
      return;
     }

   int campaignIndex = FindCampaignIndex(zone.magic);
   if(campaignIndex < 0)
     {
      DebugLog("START entry skipped: campaign not found. zone={" + ZoneDebugText(zone) + "}");
      return;
     }
   DebugLog("Opening START order. campaign={" + CampaignDebugText(g_campaigns[campaignIndex]) + "} zone={" + ZoneDebugText(zone) + "}");
   OpenCampaignOrder(g_campaigns[campaignIndex], NormalizeVolume(InpLotSize), "START");
  }

void ManageCampaigns(const bool onFirstTickOfNewDcaBar)
  {
   for(int i = ArraySize(g_campaigns) - 1; i >= 0; i--)
     {
      if(!g_campaigns[i].active)
        {
         DebugTrace("Campaign skipped: inactive. " + CampaignDebugText(g_campaigns[i]));
         continue;
        }
      BasketInfo basket;
      BuildBasket(g_campaigns[i].side, g_campaigns[i].magic, basket);

      if(basket.count <= 0)
        {
         DebugLog("Removing campaign without basket positions during manage. " + CampaignDebugText(g_campaigns[i]));
         ArrayRemove(g_campaigns, i, 1);
         continue;
        }

      if(CampaignTakeProfitReached(g_campaigns[i].side, basket))
        {
         DebugLog("Closing campaign after TP. campaign={" + CampaignDebugText(g_campaigns[i]) + "} basket={" + BasketDebugText(basket) + "}");
         if(CloseCampaign(g_campaigns[i]))
           {
            DebugLog("Removed campaign after successful TP close. " + CampaignDebugText(g_campaigns[i]));
            ArrayRemove(g_campaigns, i, 1);
           }
         continue;
        }

      if(ShouldOpenDca(g_campaigns[i], basket, onFirstTickOfNewDcaBar))
        {
         double nextVolume = NormalizeVolume(g_campaigns[i].baseLot * MathPow(InpMultiplier, basket.count));
         DebugLog("Opening DCA order. nextVolume=" + DoubleToString(nextVolume, 2) + " campaign={" + CampaignDebugText(g_campaigns[i]) + "} basket={" + BasketDebugText(basket) + "}");
         OpenCampaignOrder(g_campaigns[i], nextVolume, "DCA");
        }
     }
  }

void CleanupCampaignsWithoutPositions()
  {
   for(int i = ArraySize(g_campaigns) - 1; i >= 0; i--)
      if(!HasOpenPositions(g_campaigns[i].magic))
        {
         DebugLog("Cleanup removed campaign without open positions. " + CampaignDebugText(g_campaigns[i]));
         ArrayRemove(g_campaigns, i, 1);
        }
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

string ZoneLabelText(const ZoneData &zone)
  {
   return(StringLen(zone.label) > 0 ? zone.label : "-");
  }

string ZoneSlText(const ZoneData &zone)
  {
   return(zone.sl > 0.0 ? DoubleToString(zone.sl, _Digits) : "-");
  }

string ZoneEntryText(const ZoneData &zone)
  {
   return(zone.entry > 0.0 ? DoubleToString(zone.entry, _Digits) : "-");
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
   if(InpSessionStopEnabled && RemoteJsonEnabled())
     {
      if(IsSessionTradingPaused())
         AddPanelRow(lines, colors, row, "Session: PAUSED (DCA only)", clrCrimson);
      else
         AddPanelRow(lines, colors, row, "Session: ACTIVE", clrDarkGreen);
     }
   AddPanelRow(lines, colors, row, " ", clrBlack);

   int tradeIndex = ActiveTradeZoneIndex();
   if(tradeIndex >= 0)
     {
      ZoneData zone = g_zones[tradeIndex];
      AddPanelRow(lines, colors, row, "Zone TRADE: " + SideText(zone.side), clrDarkGreen);
      AddPanelRow(lines, colors, row, "Label: " + ZoneLabelText(zone), clrBlack);
      AddPanelRow(lines, colors, row, "Low/High: " + DoubleToString(zone.low, _Digits) + " - " + DoubleToString(zone.high, _Digits), clrBlack);
      AddPanelRow(lines, colors, row, "Entry: " + ZoneEntryText(zone), clrBlack);
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
         AddPanelRow(lines, colors, row, "Entry: " + ZoneEntryText(watchZone), clrBlack);
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
   DebugLog("OnInit started. version=" + EA_VERSION + " symbol=" + _Symbol + " period=" + IntegerToString(_Period) + " lot=" + DoubleToString(InpLotSize, 2) + " followLot=" + DoubleToString(InpPlanFollowLotSize, 2) + " multiplier=" + DoubleToString(InpMultiplier, 2) + " gridStep=" + IntegerToString(InpGridStep) + " maxLevels=" + IntegerToString(InpMaxGridLevels) + " tp=" + IntegerToString(InpTakeProfit) + " dcaTf=" + EnumToString(DcaTimeframe()) + " debugTrace=" + BoolText(InpDebugTraceDecisions));
   g_trade.SetExpertMagicNumber(InpMagicNumber);
   g_trade.SetDeviationInPoints(20);
   g_trade.SetTypeFillingBySymbol(_Symbol);
   if(RemoteJsonEnabled())
     {
      EventSetTimer(InpZonesPollSeconds);
      DebugLog("Timer enabled for remote JSON polling. seconds=" + IntegerToString(InpZonesPollSeconds));
     }
   RestoreMorningResumeStateFromGlobals();
   FetchZonesOnInit();
   RestoreCampaignsFromOpenPositions();
   EnsureSessionStopZonesCleared();
   if(InpShowPanel) CreatePanel();
   g_dcaBarOpenSeen = iTime(_Symbol, DcaTimeframe(), 0);
   UpdatePanel();
   DebugLog("OnInit complete. zones=" + IntegerToString(ArraySize(g_zones)) + " campaigns=" + IntegerToString(ArraySize(g_campaigns)) + " dcaBarOpenSeen=" + TimeToString(g_dcaBarOpenSeen, TIME_DATE | TIME_SECONDS));
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   DebugLog("OnDeinit. reason=" + IntegerToString(reason) + " zones=" + IntegerToString(ArraySize(g_zones)) + " campaigns=" + IntegerToString(ArraySize(g_campaigns)));
   EventKillTimer();
   RemovePanel();
  }

void OnTimer()
  {
   DebugTrace("OnTimer fired");
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

   bool onFirstTickOfNewDcaBar = false;
   datetime dcaBarOpen = iTime(_Symbol, DcaTimeframe(), 0);
   if(dcaBarOpen > 0 && dcaBarOpen != g_dcaBarOpenSeen)
     {
      g_dcaBarOpenSeen = dcaBarOpen;
      onFirstTickOfNewDcaBar = true;
      DebugLog("New DCA bar detected. timeframe=" + EnumToString(DcaTimeframe()) + " open=" + TimeToString(dcaBarOpen, TIME_DATE | TIME_SECONDS));
     }

   if(IsSessionTradingPaused())
     {
      EnsureSessionStopZonesCleared();
      ManageCampaigns(onFirstTickOfNewDcaBar);
      UpdatePanel();
      return;
     }

   CleanupCampaignsWithoutPositions();
   ActivateNearestWatchZone();
   RemoveTouchedTradeZone();
   ManagePlanChinhFollowEntry();
   ManageActiveTradeEntry();
   ManageCampaigns(onFirstTickOfNewDcaBar);
   UpdatePanel();
  }
