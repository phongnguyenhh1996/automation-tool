#property strict
#property description "EA Zone NeverDie MT5 - Multi-Zone Independent Campaigns"

#include <Trade/Trade.mqh>

enum ENUM_ZONE_MODE
  {
   ZONE_OFF   = 0,
   ZONE_TRADE = 1,
   ZONE_WATCH = 2
  };

// Only M5/M15
enum ENUM_ND_DCA_TF
  {
   ND_DCA_M5  = 0,
   ND_DCA_M15 = 1
  };

ENUM_TIMEFRAMES NdDcaTimeframe(const ENUM_ND_DCA_TF tf)
  {
   return(tf == ND_DCA_M15 ? PERIOD_M15 : PERIOD_M5);
  }

input group "=== TRADE SETTINGS ==="
input double         InpLotSize             = 0.05;      
input double         InpMultiplier          = 1.25;      
input int            InpGridStep            = 3000;      
input int            InpTakeProfit          = 3000;
input int            InpMaxGridLevels       = 50;
input long           InpMagicNumber         = 20241221;  
input ENUM_ND_DCA_TF InpDcaGridTimeframe    = ND_DCA_M15; 
input int            InpDcaClosedBarsRequired = 1;
input double         InpZoneActivateBand    = 3.0;

input group "=== RISK ==="
input double         InpCutLossFullCloseAt = 90.0;

input group "=== DISPLAY ==="
input bool           InpShowPanel          = true;

input group "=== REMOTE ZONES JSON (Cloudinary / HTTPS) ==="
input string         InpZonesJsonUrl       = "https://res.cloudinary.com/easy-toeic/raw/upload/automation_tool/ea_neverdie/neverdie_XAUUSD.json"; // URL đã được cập nhật mặc định
input int            InpZonesPollSeconds   = 300;
input string         InpZonesBearer        = "";
input double         InpZonesSlBuffer      = 3.0;

const int JSON_FETCH_WINDOW_MINUTES = 30;
const int JSON_FETCH_SLOT_COUNT     = 3;

const int PANEL_LINE_COUNT      = 60;
const int PANEL_LINE_HEIGHT     = 16;
const int PANEL_X               = 12;
const int PANEL_Y               = 30;
const int PANEL_MIN_WIDTH       = 230;
const int PANEL_MAX_WIDTH       = 360;
const int PANEL_HORIZONTAL_PAD  = 28;
const int PANEL_TITLE_Y_OFFSET  = 10;
const int PANEL_LINES_Y_OFFSET  = 40;
const int PANEL_BOTTOM_PAD      = 14;
const int PANEL_CHAR_WIDTH      = 7;

// --- DYNAMIC ZONE STRUCTURE ---
struct ZoneData
  {
   ENUM_ZONE_MODE mode;
   double         low;
   double         high;
   double         sl;
   datetime       expireTime;
   long           magic;
  };

struct BasketInfo
  {
   int      count;
   double   totalVolume;
   double   weightedPriceSum;
   double   averagePrice;
   double   floatingProfit;
   double   lastVolume;
   double   lastOpenPrice;
   datetime lastOpenTime;
  };

CTrade   g_trade;
datetime g_dcaGridBarOpenSeen = 0;
string   g_panelPrefix        = "ZoneNeverDiePanel";
string   g_zoneLinePrefix     = "ZoneNeverDieZoneLine";
int      g_completedJsonFetchWindowKey = -1;

// --- ZONE ARRAYS ---
ZoneData g_buyZones[];
ZoneData g_sellZones[];

// =======================================================================
// HELPER: TIME & MAGIC NUMBERS
// =======================================================================
datetime GetNext2AM()
  {
   MqlDateTime tm;
   TimeToStruct(TimeCurrent(), tm);
   tm.hour = 2; 
   tm.min  = 0; 
   tm.sec  = 0;
   datetime twoAM = StructToTime(tm);
   if(TimeCurrent() >= twoAM) twoAM += 86400; // Next day
   return(twoAM);
  }

long GetZoneMagic(double low, double high)
  {
   long lowPts = (long)MathRound(low * 100000.0);
   long highPts = (long)MathRound(high * 100000.0);
   long combined = lowPts + highPts;
   return InpMagicNumber + (combined % 999999);
  }

bool IsOurMagic(long magic)
  {
   return (magic >= InpMagicNumber && magic <= InpMagicNumber + 999999);
  }

// =======================================================================
// JSON PARSING & DATA HANDLING
// =======================================================================
bool NeverdieUseRemoteJson()
  {
   if(MQLInfoInteger(MQL_TESTER)) return(false);
   if(InpZonesPollSeconds <= 0) return(false);
   return(StringLen(InpZonesJsonUrl) > 0);
  }

int DateKeyFromTime(const datetime t)
  {
   MqlDateTime tm;
   TimeToStruct(t, tm);
   return(tm.year * 10000 + tm.mon * 100 + tm.day);
  }

int JsonFetchSlotStartMinute(const int slot)
  {
   // Requested VN times converted to UTC: 09:50, 14:45, 21:15 GMT+7.
   if(slot == 0) return(2 * 60 + 50);
   if(slot == 1) return(7 * 60 + 45);
   if(slot == 2) return(14 * 60 + 15);
   return(-1);
  }

int CurrentJsonFetchWindowKey(const datetime utcNow)
  {
   MqlDateTime tm;
   TimeToStruct(utcNow, tm);
   int currentMinute = tm.hour * 60 + tm.min;

   for(int slot = 0; slot < JSON_FETCH_SLOT_COUNT; slot++)
     {
      int startMinute = JsonFetchSlotStartMinute(slot);
      if(currentMinute >= startMinute && currentMinute < startMinute + JSON_FETCH_WINDOW_MINUTES)
         return(DateKeyFromTime(utcNow) * 10 + slot);
     }

   return(-1);
  }

bool ShouldFetchNeverdieJsonNow(int &windowKey)
  {
   windowKey = CurrentJsonFetchWindowKey(TimeGMT());
   if(windowKey < 0) return(false);
   return(windowKey != g_completedJsonFetchWindowKey);
  }

bool ExtractJsonObjectForKey(const string j, const string key, string &outObj)
  {
   string q = "\"" + key + "\"";
   int p = StringFind(j, q);
   if(p < 0) return(false);
   int b = StringFind(j, "{", p);
   if(b < 0) return(false);
   int depth = 0;
   for(int i = b; i < StringLen(j); i++)
     {
      ushort c = StringGetCharacter(j, i);
      if(c == '{') depth++;
      else if(c == '}')
        {
         depth--;
         if(depth == 0) { outObj = StringSubstr(j, b, i - b + 1); return(true); }
        }
     }
   return(false);
  }

string JsonExtractStringValue(const string o, const string key)
  {
   string pat = "\"" + key + "\"";
   int p = StringFind(o, pat);
   if(p < 0) return("");
   p += StringLen(pat);
   while(p < StringLen(o) && (StringGetCharacter(o, p) == ' ' || StringGetCharacter(o, p) == ':')) p++;
   while(p < StringLen(o) && StringGetCharacter(o, p) == ' ') p++;
   if(StringGetCharacter(o, p) != '"') return("");
   p++;
   int e = StringFind(o, "\"", p);
   if(e < 0) return("");
   return(StringSubstr(o, p, e - p));
  }

double JsonExtractDoubleValue(const string o, const string key)
  {
   string pat = "\"" + key + "\"";
   int p = StringFind(o, pat);
   if(p < 0) return(0.0);
   p += StringLen(pat);
   while(p < StringLen(o) && (StringGetCharacter(o, p) == ' ' || StringGetCharacter(o, p) == ':')) p++;
   while(p < StringLen(o) && StringGetCharacter(o, p) == ' ') p++;
   return(StringToDouble(StringSubstr(o, p)));
  }

ENUM_ZONE_MODE ModeFromJsonString(const string m)
  {
   if(StringCompare(m, "trade", false) == 0) return(ZONE_TRADE);
   if(StringCompare(m, "watch", false) == 0) return(ZONE_WATCH);
   return(ZONE_OFF);
  }

double BufferedJsonStopLoss(const ENUM_POSITION_TYPE side, const double sl)
  {
   if(sl <= 0.0) return(0.0);
   double buffer = MathMax(InpZonesSlBuffer, 0.0);
   if(side == POSITION_TYPE_BUY) return(NormalizeDouble(sl - buffer, _Digits));
   return(NormalizeDouble(sl + buffer, _Digits));
  }

bool ParseNeverdieSide(const string json, const string key, ENUM_ZONE_MODE &mode, double &lo, double &hi, double &sl)
  {
   string o;
   if(!ExtractJsonObjectForKey(json, key, o)) return(false);
   mode = ModeFromJsonString(JsonExtractStringValue(o, "mode"));
   lo   = JsonExtractDoubleValue(o, "low");
   hi   = JsonExtractDoubleValue(o, "high");
   sl   = JsonExtractDoubleValue(o, "sl");
   return(true);
  }

bool AddZoneIfNotExists(ENUM_POSITION_TYPE side, ENUM_ZONE_MODE mode, double low, double high, double sl)
  {
   double minPrice = MathMin(low, high);
   double maxPrice = MathMax(low, high);
   double stopLoss = BufferedJsonStopLoss(side, sl);
   
   if(side == POSITION_TYPE_BUY)
     {
      for(int i = 0; i < ArraySize(g_buyZones); i++)
        {
         if(g_buyZones[i].low == minPrice && g_buyZones[i].high == maxPrice)
           {
            // JSON chỉ nạp zone ở trạng thái WATCH; TRADE do giá kích hoạt.
            if(mode == ZONE_OFF) g_buyZones[i].mode = ZONE_OFF;
            else if(g_buyZones[i].mode != ZONE_TRADE) g_buyZones[i].mode = ZONE_WATCH;
            g_buyZones[i].sl = stopLoss;
            return(false);
           }
        }
      if(mode == ZONE_OFF) return(false); // Không thêm mới nếu đang tắt
      
      int size = ArraySize(g_buyZones);
      ArrayResize(g_buyZones, size + 1);
      g_buyZones[size].mode = ZONE_WATCH;
      g_buyZones[size].low = minPrice;
      g_buyZones[size].high = maxPrice;
      g_buyZones[size].sl = stopLoss;
      g_buyZones[size].expireTime = GetNext2AM();
      g_buyZones[size].magic = GetZoneMagic(minPrice, maxPrice);
      return(true);
     }
   else
     {
      for(int i = 0; i < ArraySize(g_sellZones); i++)
        {
         if(g_sellZones[i].low == minPrice && g_sellZones[i].high == maxPrice)
           {
            if(mode == ZONE_OFF) g_sellZones[i].mode = ZONE_OFF;
            else if(g_sellZones[i].mode != ZONE_TRADE) g_sellZones[i].mode = ZONE_WATCH;
            g_sellZones[i].sl = stopLoss;
            return(false);
           }
        }
      if(mode == ZONE_OFF) return(false);
      
      int size = ArraySize(g_sellZones);
      ArrayResize(g_sellZones, size + 1);
      g_sellZones[size].mode = ZONE_WATCH;
      g_sellZones[size].low = minPrice;
      g_sellZones[size].high = maxPrice;
      g_sellZones[size].sl = stopLoss;
      g_sellZones[size].expireTime = GetNext2AM();
      g_sellZones[size].magic = GetZoneMagic(minPrice, maxPrice);
      return(true);
     }

   return(false);
  }

bool ApplyNeverdieJson(const string json, bool &addedNewZone)
  {
   addedNewZone = false;
   bool parsed = false;
   ENUM_ZONE_MODE bm, sm;
   double bl, bh, bs, sl, sh, ss;
   if(ParseNeverdieSide(json, "buy", bm, bl, bh, bs))
     {
      parsed = true;
      if(AddZoneIfNotExists(POSITION_TYPE_BUY, bm, bl, bh, bs))
         addedNewZone = true;
     }
   if(ParseNeverdieSide(json, "sell", sm, sl, sh, ss))
     {
      parsed = true;
      if(AddZoneIfNotExists(POSITION_TYPE_SELL, sm, sl, sh, ss))
         addedNewZone = true;
     }
   return(parsed);
  }

bool FetchNeverdieJsonFromUrl()
  {
   uchar req[], res[];
   string headers_out;
   string hdr = (StringLen(InpZonesBearer) > 0) ? "Authorization: Bearer " + InpZonesBearer + "\r\n" : "";
   ResetLastError();
   PrintFormat("EA NeverDie: Fetch zones URL=[%s]", InpZonesJsonUrl);
   int code = WebRequest("GET", InpZonesJsonUrl, hdr, 15000, req, res, headers_out);
   
   if(code == -1) { PrintFormat("EA NeverDie: WebRequest failed (Err: %d) - Check URL settings", GetLastError()); return(false); }
   if(code != 200)
     {
      PrintFormat("EA NeverDie: HTTP %d from URL=[%s]", code, InpZonesJsonUrl);
      PrintFormat("EA NeverDie: Response headers=[%s]", headers_out);
      PrintFormat("EA NeverDie: Response body=[%s]", CharArrayToString(res));
      return(false);
     }
   
   string body = CharArrayToString(res);
   bool addedNewZone = false;
   if(!ApplyNeverdieJson(body, addedNewZone))
     {
      PrintFormat("EA NeverDie: JSON parse failed. Body=[%s]", body);
      return(false);
     }
   return(addedNewZone);
  }

void FetchNeverdieJsonOnSchedule()
  {
   int windowKey = -1;
   if(!ShouldFetchNeverdieJsonNow(windowKey)) return;

   if(FetchNeverdieJsonFromUrl())
     {
      g_completedJsonFetchWindowKey = windowKey;
      PrintFormat("EA NeverDie: New JSON zone found; fetch window %d completed", windowKey);
     }
  }

void FetchNeverdieJsonOnInit()
  {
   bool addedNewZone = FetchNeverdieJsonFromUrl();
   if(!addedNewZone) return;

   int windowKey = CurrentJsonFetchWindowKey(TimeGMT());
   if(windowKey >= 0)
      g_completedJsonFetchWindowKey = windowKey;
  }

// =======================================================================
// SYSTEM CORE & LIFECYCLE
// =======================================================================
int OnInit()
  {
   if(!ValidateInputs()) return(INIT_PARAMETERS_INCORRECT);

   g_trade.SetExpertMagicNumber(InpMagicNumber);
   g_trade.SetDeviationInPoints(20);
   g_trade.SetTypeFillingBySymbol(_Symbol);

   if(NeverdieUseRemoteJson())
     {
      EventSetTimer(InpZonesPollSeconds);
      FetchNeverdieJsonOnInit();
     }

   if(InpShowPanel) CreatePanel();
   else RemovePanel();

   g_dcaGridBarOpenSeen = iTime(_Symbol, NdDcaTimeframe(InpDcaGridTimeframe), 0);
   UpdatePanel();
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   RemovePanel();
   RemoveZoneChartObjects();
  }

void OnTimer()
  {
   if(!NeverdieUseRemoteJson()) return;
   FetchNeverdieJsonOnSchedule();
   UpdatePanel();
  }

void OnTick()
  {
   if(!IsEnvironmentReady()) { UpdatePanel(); return; }
   if(ManageRiskCutLoss()) { UpdatePanel(); return; }

   CleanupExpiredZones();
   ActivateWatchZones();
   ManageZoneStopsAndWatchers();

   bool onFirstTickOfNewDcaBar = false;
   datetime dcaBarOpen = iTime(_Symbol, NdDcaTimeframe(InpDcaGridTimeframe), 0);
   if(dcaBarOpen > 0 && dcaBarOpen != g_dcaGridBarOpenSeen)
     {
      g_dcaGridBarOpenSeen = dcaBarOpen;
      onFirstTickOfNewDcaBar = true;
     }

   ManageAllZones(POSITION_TYPE_BUY, onFirstTickOfNewDcaBar);
   ManageAllZones(POSITION_TYPE_SELL, onFirstTickOfNewDcaBar);
   UpdatePanel();
  }

void OnTradeTransaction(const MqlTradeTransaction &trans, const MqlTradeRequest &request, const MqlTradeResult &result)
  {
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD || trans.deal == 0) return;
   if(!HistoryDealSelect(trans.deal)) return;
   if(HistoryDealGetString(trans.deal, DEAL_SYMBOL) != _Symbol) return;
   
   long magic = HistoryDealGetInteger(trans.deal, DEAL_MAGIC);
   if(!IsOurMagic(magic)) return;

   long entry = HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
   if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY && entry != DEAL_ENTRY_INOUT) return;

   if(HistoryDealGetInteger(trans.deal, DEAL_REASON) != DEAL_REASON_SL) return;

   long dealType = HistoryDealGetInteger(trans.deal, DEAL_TYPE);
   if(dealType == DEAL_TYPE_SELL)
     {
      CloseBasket(POSITION_TYPE_BUY, magic);
      DeactivateZoneByMagic(POSITION_TYPE_BUY, magic);
     }
   else if(dealType == DEAL_TYPE_BUY)
     {
      CloseBasket(POSITION_TYPE_SELL, magic);
      DeactivateZoneByMagic(POSITION_TYPE_SELL, magic);
     }
  }

// =======================================================================
// ZONE MANAGEMENT LOGIC
// =======================================================================
int TotalOpenPositions(ENUM_POSITION_TYPE side)
  {
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket > 0 && PositionGetString(POSITION_SYMBOL) == _Symbol &&
         (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE) == side &&
         IsOurMagic(PositionGetInteger(POSITION_MAGIC))) count++;
     }
   return count;
  }

double SideFloatingProfit(ENUM_POSITION_TYPE side)
  {
   double totalProfit = 0.0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket > 0 && PositionGetString(POSITION_SYMBOL) == _Symbol &&
         (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE) == side &&
         IsOurMagic(PositionGetInteger(POSITION_MAGIC)))
         totalProfit += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
     }
   return totalProfit;
  }

bool HasOpenPositions(long magic)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket > 0 && PositionGetString(POSITION_SYMBOL) == _Symbol && PositionGetInteger(POSITION_MAGIC) == magic)
         return true;
     }
   return false;
  }

void DeactivateZoneByMagic(const ENUM_POSITION_TYPE side, const long magic)
  {
   if(side == POSITION_TYPE_BUY)
     {
      for(int i = 0; i < ArraySize(g_buyZones); i++)
        {
         if(g_buyZones[i].magic == magic)
           {
            g_buyZones[i].mode = ZONE_OFF;
            return;
           }
        }
      return;
     }

   for(int i = 0; i < ArraySize(g_sellZones); i++)
     {
      if(g_sellZones[i].magic == magic)
        {
         g_sellZones[i].mode = ZONE_OFF;
         return;
        }
     }
  }

void CleanupExpiredZones()
  {
   datetime now = TimeCurrent();
   for(int i = ArraySize(g_buyZones) - 1; i >= 0; i--)
     {
      if(now >= g_buyZones[i].expireTime && !HasOpenPositions(g_buyZones[i].magic))
         ArrayRemove(g_buyZones, i, 1);
     }
   for(int i = ArraySize(g_sellZones) - 1; i >= 0; i--)
     {
      if(now >= g_sellZones[i].expireTime && !HasOpenPositions(g_sellZones[i].magic))
         ArrayRemove(g_sellZones, i, 1);
     }
  }

int GetActiveZoneIndex(ENUM_POSITION_TYPE side, double price)
  {
   if(side == POSITION_TYPE_BUY)
     {
      for(int i = ArraySize(g_buyZones) - 1; i >= 0; i--)
         if(g_buyZones[i].mode == ZONE_TRADE && price >= g_buyZones[i].low && price <= g_buyZones[i].high) return i;
     }
   else
     {
      for(int i = ArraySize(g_sellZones) - 1; i >= 0; i--)
         if(g_sellZones[i].mode == ZONE_TRADE && price >= g_sellZones[i].low && price <= g_sellZones[i].high) return i;
     }
   return -1;
  }

bool IsPriceInZone(const ZoneData &zone, const double price)
  {
   return(price >= zone.low && price <= zone.high);
  }

double ZoneActivationTriggerPrice(const ENUM_POSITION_TYPE side, const ZoneData &zone)
  {
   return(side == POSITION_TYPE_BUY ? zone.low : zone.high);
  }

double ZoneActivationDistance(const ENUM_POSITION_TYPE side, const ZoneData &zone, const double price)
  {
   return(MathAbs(price - ZoneActivationTriggerPrice(side, zone)));
  }

bool ShouldActivateWatchZone(const ENUM_POSITION_TYPE side, const ZoneData &zone, const double price)
  {
   if(zone.mode != ZONE_WATCH) return(false);
   return(ZoneActivationDistance(side, zone, price) <= InpZoneActivateBand);
  }

void ActivateSingleWatchZone(const ENUM_POSITION_TYPE side, const int index)
  {
   if(side == POSITION_TYPE_BUY)
     {
      g_buyZones[index].mode = ZONE_TRADE;
      PrintFormat("BUY zone activated: %.2f - %.2f", g_buyZones[index].low, g_buyZones[index].high);
      return;
     }

   g_sellZones[index].mode = ZONE_TRADE;
   PrintFormat("SELL zone activated: %.2f - %.2f", g_sellZones[index].low, g_sellZones[index].high);
  }

void ActivateWatchZones()
  {
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick)) return;
   double price = MidPrice(tick);

   ENUM_POSITION_TYPE activatedSide = POSITION_TYPE_BUY;
   int activatedIndex = -1;
   double activatedDistance = 1.0e100;

   for(int i = 0; i < ArraySize(g_buyZones); i++)
     {
      if(ShouldActivateWatchZone(POSITION_TYPE_BUY, g_buyZones[i], price))
        {
         double distance = ZoneActivationDistance(POSITION_TYPE_BUY, g_buyZones[i], price);
         if(distance <= activatedDistance)
           {
            activatedSide = POSITION_TYPE_BUY;
            activatedIndex = i;
            activatedDistance = distance;
           }
        }
     }

   for(int i = 0; i < ArraySize(g_sellZones); i++)
     {
      if(ShouldActivateWatchZone(POSITION_TYPE_SELL, g_sellZones[i], price))
        {
         double distance = ZoneActivationDistance(POSITION_TYPE_SELL, g_sellZones[i], price);
         if(distance <= activatedDistance)
           {
            activatedSide = POSITION_TYPE_SELL;
            activatedIndex = i;
            activatedDistance = distance;
           }
        }
     }

   if(activatedIndex < 0) return;

   for(int i = 0; i < ArraySize(g_buyZones); i++)
      if(g_buyZones[i].mode == ZONE_TRADE) g_buyZones[i].mode = ZONE_WATCH;

   for(int i = 0; i < ArraySize(g_sellZones); i++)
      if(g_sellZones[i].mode == ZONE_TRADE) g_sellZones[i].mode = ZONE_WATCH;

   ActivateSingleWatchZone(activatedSide, activatedIndex);
  }

int GetTradableZoneIndex(ENUM_POSITION_TYPE side, double price)
  {
   if(side == POSITION_TYPE_BUY)
     {
      for(int i = ArraySize(g_buyZones) - 1; i >= 0; i--)
         if(g_buyZones[i].mode == ZONE_TRADE && IsPriceInZone(g_buyZones[i], price)) return i;
     }
   else
     {
      for(int i = ArraySize(g_sellZones) - 1; i >= 0; i--)
         if(g_sellZones[i].mode == ZONE_TRADE && IsPriceInZone(g_sellZones[i], price)) return i;
     }
   return -1;
  }

void ManageZoneStopsAndWatchers()
  {
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick)) return;

   // SL Checks
   for(int i = ArraySize(g_buyZones) - 1; i >= 0; i--)
     {
      if(g_buyZones[i].sl > 0 && tick.bid <= g_buyZones[i].sl && HasOpenPositions(g_buyZones[i].magic))
        {
         CloseBasket(POSITION_TYPE_BUY, g_buyZones[i].magic);
         DeactivateZoneByMagic(POSITION_TYPE_BUY, g_buyZones[i].magic);
        }
     }

   for(int i = ArraySize(g_sellZones) - 1; i >= 0; i--)
     {
      if(g_sellZones[i].sl > 0 && tick.ask >= g_sellZones[i].sl && HasOpenPositions(g_sellZones[i].magic))
        {
         CloseBasket(POSITION_TYPE_SELL, g_sellZones[i].magic);
         DeactivateZoneByMagic(POSITION_TYPE_SELL, g_sellZones[i].magic);
        }
     }
  }

void ManageAllZones(const ENUM_POSITION_TYPE side, const bool onFirstTickDca)
  {
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick)) return;
   double price = GetSideOpenPrice(side, tick);
   int newestTradableIdx = GetTradableZoneIndex(side, price);
   int totalZones = (side == POSITION_TYPE_BUY) ? ArraySize(g_buyZones) : ArraySize(g_sellZones);

   for(int i = 0; i < totalZones; i++)
     {
      ZoneData zone = (side == POSITION_TYPE_BUY) ? g_buyZones[i] : g_sellZones[i];
      BasketInfo basket;
      BuildBasket(side, zone.magic, basket);

      if(basket.count > 0)
        {
         if(ShouldCloseBasketByTakeProfit(side, zone, basket))
           {
            CloseBasket(side, zone.magic);
            DeactivateZoneByMagic(side, zone.magic);
           }
         else if(ShouldOpenDca(side, zone, basket, onFirstTickDca))
           {
            double nextVolume = NormalizeVolume(InpLotSize * MathPow(InpMultiplier, basket.count));
            OpenPosition(side, zone, nextVolume, "DCA");
           }
        }
      else
        {
         // Mở Initial khi giá nằm trong zone TRADE mới nhất.
         if(i == newestTradableIdx && ShouldOpenInitial(side, zone, price))
           {
            OpenPosition(side, zone, NormalizeVolume(InpLotSize), "START");
           }
        }
     }
  }

void BuildBasket(const ENUM_POSITION_TYPE side, const long magic, BasketInfo &basket)
  {
   basket.count = 0; basket.totalVolume = 0.0; basket.weightedPriceSum = 0.0;
   basket.averagePrice = 0.0; basket.floatingProfit = 0.0; basket.lastVolume = 0.0;
   basket.lastOpenPrice = 0.0; basket.lastOpenTime = 0;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic) continue;
      if((ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE) != side) continue;

      double vol = PositionGetDouble(POSITION_VOLUME);
      double opPrice = PositionGetDouble(POSITION_PRICE_OPEN);
      double profit = PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
      datetime opened = (datetime)PositionGetInteger(POSITION_TIME);

      basket.count++;
      basket.totalVolume += vol;
      basket.weightedPriceSum += opPrice * vol;
      basket.floatingProfit += profit;

      if(opened >= basket.lastOpenTime)
        {
         basket.lastOpenTime = opened;
         basket.lastOpenPrice = opPrice;
         basket.lastVolume = vol;
        }
     }
   if(basket.totalVolume > 0.0) basket.averagePrice = basket.weightedPriceSum / basket.totalVolume;
  }

// =======================================================================
// TRADING LOGIC
// =======================================================================
bool ShouldOpenInitial(const ENUM_POSITION_TYPE side, const ZoneData &zone, double price)
  {
   if(zone.mode != ZONE_TRADE) return(false);

   if(side == POSITION_TYPE_BUY && zone.sl > 0 && price <= zone.sl) return(false);
   if(side == POSITION_TYPE_SELL && zone.sl > 0 && price >= zone.sl) return(false);

   return(IsPriceInZone(zone, price));
  }

bool ShouldOpenDca(const ENUM_POSITION_TYPE side, const ZoneData &zone, const BasketInfo &basket, const bool onFirstTick)
  {
   if(!onFirstTick) return(false);
   if(basket.count <= 0 || basket.count >= InpMaxGridLevels) return(false);
   if(basket.floatingProfit >= 0.0) return(false);

   int shiftSinceOpen = iBarShift(_Symbol, NdDcaTimeframe(InpDcaGridTimeframe), basket.lastOpenTime, false);
   if(shiftSinceOpen < InpDcaClosedBarsRequired) return(false);

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick)) return(false);
   double price = GetSideOpenPrice(side, tick);
   double distance = MathAbs(price - basket.lastOpenPrice) / _Point;

   if(distance < InpGridStep) return(false);
   if(side == POSITION_TYPE_BUY && price >= basket.lastOpenPrice) return(false);
   if(side == POSITION_TYPE_SELL && price <= basket.lastOpenPrice) return(false);

   if(side == POSITION_TYPE_BUY && zone.sl > 0 && price <= zone.sl) return(false);
   if(side == POSITION_TYPE_SELL && zone.sl > 0 && price >= zone.sl) return(false);

   return(true);
  }

bool ShouldCloseBasketByTakeProfit(const ENUM_POSITION_TYPE side, const ZoneData &zone, const BasketInfo &basket)
  {
   if(basket.count <= 0 || basket.totalVolume <= 0.0) return(false);
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick)) return(false);
   
   double currentPrice = GetSideClosePrice(side, tick);
   double targetPrice = zone.low;
   if(side == POSITION_TYPE_BUY) targetPrice = zone.high;

   if(side == POSITION_TYPE_BUY && currentPrice >= targetPrice && basket.floatingProfit > 0.0) return(true);
   if(side == POSITION_TYPE_SELL && currentPrice <= targetPrice && basket.floatingProfit > 0.0) return(true);
   return(false);
  }

bool OpenPosition(const ENUM_POSITION_TYPE side, const ZoneData &zone, const double volume, const string tag)
  {
   double sl = NormalizeDouble(zone.sl, _Digits);
   string note = StringFormat("ZONE_ND_%s_%s", side == POSITION_TYPE_BUY ? "BUY" : "SELL", tag);
   
   g_trade.SetExpertMagicNumber(zone.magic);
   g_trade.SetTypeFillingBySymbol(_Symbol);

   bool sent = false;
   if(side == POSITION_TYPE_BUY) sent = g_trade.Buy(volume, _Symbol, 0.0, sl, 0.0, note);
   else sent = g_trade.Sell(volume, _Symbol, 0.0, sl, 0.0, note);

   if(!sent) PrintFormat("OpenPosition failed. Retcode=%d", g_trade.ResultRetcode());
   return(sent);
  }

bool CloseBasket(const ENUM_POSITION_TYPE side, const long magic)
  {
   bool allClosed = true;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if(PositionGetInteger(POSITION_MAGIC) != magic) continue;
      if((ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE) != side) continue;

      if(!g_trade.PositionClose(ticket)) allClosed = false;
     }
   return(allClosed);
  }

// =======================================================================
// RISK & SAFETY
// =======================================================================
bool ManageRiskCutLoss()
  {
   if(InpCutLossFullCloseAt <= 0.0) return(false);
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   if(balance <= 0.0) return(false);

   double totalLoss = 0.0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket > 0 && PositionGetString(POSITION_SYMBOL) == _Symbol && IsOurMagic(PositionGetInteger(POSITION_MAGIC)))
        {
         double profit = PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
         if(profit < 0.0) totalLoss += -profit;
        }
     }

   if((totalLoss / balance) * 100.0 < InpCutLossFullCloseAt) return(false);
   CloseAllEaPositions();
   return(true);
  }

void CloseAllEaPositions()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket > 0 && PositionGetString(POSITION_SYMBOL) == _Symbol && IsOurMagic(PositionGetInteger(POSITION_MAGIC)))
         g_trade.PositionClose(ticket);
     }
  }

bool ValidateInputs()
  {
   if(InpLotSize <= 0.0 || InpMultiplier < 1.0) return(false);
   if(InpGridStep <= 0 || InpTakeProfit <= 0 || InpMaxGridLevels < 1) return(false);
   if(InpDcaClosedBarsRequired < 1) return(false);
   if(InpZoneActivateBand < 0.0) return(false);
   return(true);
  }

bool IsEnvironmentReady()
  {
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)) return(false);
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED)) return(false);
   if(Bars(_Symbol, _Period) < 100) return(false);
   return(true);
  }

double NormalizeVolume(const double requested)
  {
   double minVol = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxVol = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double stepVol = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double vol = MathMax(requested, minVol);
   vol = MathMin(vol, maxVol);
   if(stepVol > 0.0) vol = MathFloor(vol / stepVol + 0.0000001) * stepVol;
   return(NormalizeDouble(vol, VolumeDigits(stepVol)));
  }

int VolumeDigits(const double step)
  {
   double current = step; int digits = 0;
   while(current > 0.0 && current < 1.0 && digits < 8) { current *= 10.0; digits++; }
   return(digits);
  }

double DirectionMultiplier(const ENUM_POSITION_TYPE side) { return(side == POSITION_TYPE_BUY ? 1.0 : -1.0); }
double GetSideOpenPrice(const ENUM_POSITION_TYPE side, const MqlTick &tick) { return(side == POSITION_TYPE_BUY ? tick.ask : tick.bid); }
double GetSideClosePrice(const ENUM_POSITION_TYPE side, const MqlTick &tick) { return(side == POSITION_TYPE_BUY ? tick.bid : tick.ask); }
double MidPrice(const MqlTick &tick) { return((tick.bid + tick.ask) / 2.0); }

// =======================================================================
// PANEL & DISPLAY
// =======================================================================
ZoneData GetLatestZone(ENUM_POSITION_TYPE side)
  {
   ZoneData empty; empty.mode = ZONE_OFF; empty.low = 0; empty.high = 0; empty.sl = 0;
   int count = (side == POSITION_TYPE_BUY) ? ArraySize(g_buyZones) : ArraySize(g_sellZones);
   if(count == 0) return empty;
   return (side == POSITION_TYPE_BUY) ? g_buyZones[count-1] : g_sellZones[count-1];
  }

string ZoneModeText(const ENUM_ZONE_MODE mode) { return(mode == ZONE_TRADE ? "TRADE" : (mode == ZONE_WATCH ? "WATCH" : "OFF")); }

string SideStatusText(const ENUM_POSITION_TYPE side)
  {
   ZoneData z = GetLatestZone(side);
   if(z.mode == ZONE_TRADE) return("READY");
   if(z.mode == ZONE_WATCH) return("WATCHING");
   return("OFF");
  }

string SideReasonText()
  {
   return("-");
  }

string OverallStatusText()
  {
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED)) return("Disabled");
   return("Trading");
  }

string ZoneProximityText(const ENUM_POSITION_TYPE side, const ZoneData &zone, const double price, const bool hasPrice)
  {
   if(!hasPrice) return("price n/a");

   double triggerPrice = ZoneActivationTriggerPrice(side, zone);
   double distance = ZoneActivationDistance(side, zone, price);

   if(IsPriceInZone(zone, price)) return(StringFormat("IN ZONE, trg %.2f", triggerPrice));
   if(zone.mode == ZONE_WATCH && distance <= InpZoneActivateBand)
      return(StringFormat("TOUCHING, %.2f away", distance));

   if(price < triggerPrice)
      return(StringFormat("need +%.2f to %.2f", triggerPrice - price, triggerPrice));
   return(StringFormat("need -%.2f to %.2f", price - triggerPrice, triggerPrice));
  }

string ZoneDetailText(const ENUM_POSITION_TYPE side, const ZoneData &zone, const double price, const bool hasPrice)
  {
   string text = StringFormat("%.2f - %.2f [%s]", zone.low, zone.high, ZoneModeText(zone.mode));
   if(zone.sl > 0.0) text += " SL " + DoubleToString(zone.sl, _Digits);
   return(text);
  }

int ZoneGlobalIndex(const ENUM_POSITION_TYPE side, const int index)
  {
   if(side == POSITION_TYPE_BUY) return(index);
   return(ArraySize(g_buyZones) + index);
  }

void AddZoneDetailRows(string &lines[], color &colors[], int &row, const ENUM_POSITION_TYPE side, const string prefix, const double price, const bool hasPrice)
  {
   int count = (side == POSITION_TYPE_BUY) ? ArraySize(g_buyZones) : ArraySize(g_sellZones);
   if(count == 0)
     {
      AddPanelRow(lines, colors, row, prefix + " Zone:  OFF", clrBlack);
      return;
     }

   bool displayed[];
   ArrayResize(displayed, count);
   for(int i = 0; i < count; i++) displayed[i] = false;

   for(int rank = 0; rank < count; rank++)
     {
      int bestIndex = -1;
      double bestDistance = 1.0e100;

      for(int i = 0; i < count; i++)
        {
         if(displayed[i]) continue;
         ZoneData candidate = (side == POSITION_TYPE_BUY) ? g_buyZones[i] : g_sellZones[i];
         double distance = hasPrice ? ZoneActivationDistance(side, candidate, price) : (double)i;
         if(bestIndex < 0 || distance < bestDistance)
           {
            bestIndex = i;
            bestDistance = distance;
           }
        }

      if(bestIndex < 0) return;
      displayed[bestIndex] = true;

      ZoneData zone = (side == POSITION_TYPE_BUY) ? g_buyZones[bestIndex] : g_sellZones[bestIndex];
      BasketInfo basket;
      BuildBasket(side, zone.magic, basket);

      string label = prefix + " Zone " + IntegerToString(bestIndex + 1) + ": ";
      string detail = ZoneDetailText(side, zone, price, hasPrice);
      color rowColor = ZoneDisplayColor(ZoneGlobalIndex(side, bestIndex));

      AddPanelRow(lines, colors, row, label + detail, rowColor);

      string distanceText = "   Distance: " + ZoneProximityText(side, zone, price, hasPrice);
      if(basket.count > 0)
         distanceText += StringFormat(" | Orders %d | P/L %.2f", basket.count, basket.floatingProfit);
      AddPanelRow(lines, colors, row, distanceText, rowColor);
     }
  }

string ZoneStopText(const ENUM_POSITION_TYPE side)
  {
   ZoneData z = GetLatestZone(side);
   if(z.mode != ZONE_TRADE || z.sl <= 0.0) return("-");
   return(DoubleToString(z.sl, _Digits));
  }

void CreatePanel()
  {
   string bg = g_panelPrefix + "_BG";
   string title = g_panelPrefix + "_TITLE";

   if(ObjectFind(0, bg) == -1) ObjectCreate(0, bg, OBJ_RECTANGLE_LABEL, 0, 0, 0);
   ObjectSetInteger(0, bg, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, bg, OBJPROP_XDISTANCE, PANEL_X); ObjectSetInteger(0, bg, OBJPROP_YDISTANCE, PANEL_Y);
   ObjectSetInteger(0, bg, OBJPROP_XSIZE, PANEL_MIN_WIDTH); ObjectSetInteger(0, bg, OBJPROP_YSIZE, 120);
   ObjectSetInteger(0, bg, OBJPROP_BGCOLOR, clrWhiteSmoke); ObjectSetInteger(0, bg, OBJPROP_COLOR, clrBlack);
   ObjectSetInteger(0, bg, OBJPROP_SELECTABLE, false); ObjectSetInteger(0, bg, OBJPROP_HIDDEN, true);

   if(ObjectFind(0, title) == -1) ObjectCreate(0, title, OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, title, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, title, OBJPROP_XDISTANCE, PANEL_X + PANEL_MIN_WIDTH / 2);
   ObjectSetInteger(0, title, OBJPROP_YDISTANCE, PANEL_Y + PANEL_TITLE_Y_OFFSET);
   ObjectSetInteger(0, title, OBJPROP_ANCHOR, ANCHOR_UPPER);
   ObjectSetInteger(0, title, OBJPROP_COLOR, clrBlack); ObjectSetInteger(0, title, OBJPROP_FONTSIZE, 11);
   ObjectSetString(0, title, OBJPROP_FONT, "Tahoma Bold"); 
   ObjectSetString(0, title, OBJPROP_TEXT, "EA Zone NeverDie");
   ObjectSetInteger(0, title, OBJPROP_SELECTABLE, false); ObjectSetInteger(0, title, OBJPROP_HIDDEN, true);

   for(int i = 0; i < PANEL_LINE_COUNT; i++) CreatePanelLine(i);
  }

void CreatePanelLine(const int index)
  {
   string name = g_panelPrefix + "_LINE_" + IntegerToString(index);
   if(ObjectFind(0, name) == -1) ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, PANEL_X + PANEL_MIN_WIDTH / 2);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, PANEL_Y + PANEL_LINES_Y_OFFSET + index * PANEL_LINE_HEIGHT);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, ANCHOR_UPPER);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clrBlack); ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 9);
   ObjectSetString(0, name, OBJPROP_FONT, "Tahoma"); ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
   ObjectSetString(0, name, OBJPROP_TEXT, " ");
  }

void RemovePanel()
  {
   ObjectDelete(0, g_panelPrefix + "_BG"); ObjectDelete(0, g_panelPrefix + "_TITLE");
   for(int i = 0; i < PANEL_LINE_COUNT; i++) ObjectDelete(0, g_panelPrefix + "_LINE_" + IntegerToString(i));
  }

void UpdatePanel()
  {
   UpdateZoneChartObjects();

   if(!InpShowPanel) { RemovePanel(); return; }
   if(ObjectFind(0, g_panelPrefix + "_BG") == -1) CreatePanel();

   string lines[]; color colors[];
   ArrayResize(lines, PANEL_LINE_COUNT); ArrayResize(colors, PANEL_LINE_COUNT);
   
   // Diệt lỗi "Label" triệt để ở mảng động
   for(int i = 0; i < PANEL_LINE_COUNT; i++) { lines[i] = " "; colors[i] = clrBlack; }

   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double ddPercent = (balance > 0) ? ((balance - equity) / balance) * 100.0 : 0.0;
   if(ddPercent < 0) ddPercent = 0.0;
   double buyProfit = SideFloatingProfit(POSITION_TYPE_BUY);
   double sellProfit = SideFloatingProfit(POSITION_TYPE_SELL);
   double netProfit = buyProfit + sellProfit;

   MqlTick displayTick;
   bool hasDisplayPrice = SymbolInfoTick(_Symbol, displayTick);
   double displayPrice = hasDisplayPrice ? MidPrice(displayTick) : 0.0;

   ZoneData lBuy = GetLatestZone(POSITION_TYPE_BUY);
   ZoneData lSell = GetLatestZone(POSITION_TYPE_SELL);

   int row = 0;
   AddPanelRow(lines, colors, row, "---- Account Data ----", clrDimGray);
   AddPanelRow(lines, colors, row, "Balance: " + DoubleToString(balance, 2), clrBlack);
   AddPanelRow(lines, colors, row, "Equity:  " + DoubleToString(equity, 2), clrBlack);
   AddPanelRow(lines, colors, row, "DD:      " + DoubleToString(ddPercent, 1) + "%", clrBlack);
   AddPanelRow(lines, colors, row, " ", clrBlack); // Dùng " " thay vì ""

   AddPanelRow(lines, colors, row, "------ " + _Symbol + " ------", clrDimGray);
   AddPanelRow(lines, colors, row, "Price: " + (hasDisplayPrice ? DoubleToString(displayPrice, _Digits) : "-"), clrBlack);
   AddPanelRow(lines, colors, row, "Total Orders: " + IntegerToString(TotalOpenPositions(POSITION_TYPE_BUY)+TotalOpenPositions(POSITION_TYPE_SELL)), clrBlack);
   AddPanelRow(lines, colors, row, "Buy Profit:  " + DoubleToString(buyProfit, 2), ProfitColor(buyProfit));
   AddPanelRow(lines, colors, row, "Sell Profit: " + DoubleToString(sellProfit, 2), ProfitColor(sellProfit));
   AddPanelRow(lines, colors, row, "Net Profit:  " + DoubleToString(netProfit, 2), ProfitColor(netProfit));
   AddPanelRow(lines, colors, row, " ", clrBlack); // Dùng " " thay vì ""

   AddPanelRow(lines, colors, row, "------- Status -------", clrDimGray);
   AddPanelRow(lines, colors, row, "Buy Mode:  " + ZoneModeText(lBuy.mode), ModeColor(lBuy.mode));
   AddZoneDetailRows(lines, colors, row, POSITION_TYPE_BUY, "Buy", displayPrice, hasDisplayPrice);
   AddPanelRow(lines, colors, row, "Buy SL:    " + ZoneStopText(POSITION_TYPE_BUY), clrBlack);
   AddPanelRow(lines, colors, row, "Buy State: " + SideStatusText(POSITION_TYPE_BUY), StateColor(lBuy.mode));
   AddPanelRow(lines, colors, row, "Buy Note:  " + SideReasonText(), clrBlack);
   AddPanelRow(lines, colors, row, " ", clrBlack); // Tạo khoảng cách nhỏ giữa Buy và Sell

   AddPanelRow(lines, colors, row, "Sell Mode: " + ZoneModeText(lSell.mode), ModeColor(lSell.mode));
   AddZoneDetailRows(lines, colors, row, POSITION_TYPE_SELL, "Sell", displayPrice, hasDisplayPrice);
   AddPanelRow(lines, colors, row, "Sell SL:   " + ZoneStopText(POSITION_TYPE_SELL), clrBlack);
   AddPanelRow(lines, colors, row, "Sell State:" + " " + SideStatusText(POSITION_TYPE_SELL), StateColor(lSell.mode));
   AddPanelRow(lines, colors, row, "Sell Note: " + SideReasonText(), clrBlack);
   AddPanelRow(lines, colors, row, " ", clrBlack);

   AddPanelRow(lines, colors, row, "Status:    " + OverallStatusText(), clrDarkGreen);

   ResizePanelForContent(lines, row);

   for(int i = 0; i < PANEL_LINE_COUNT; i++)
     {
      string name = g_panelPrefix + "_LINE_" + IntegerToString(i);
      ObjectSetString(0, name, OBJPROP_TEXT, lines[i]);
      ObjectSetInteger(0, name, OBJPROP_COLOR, colors[i]);
     }
  }

void AddPanelRow(string &lines[], color &colors[], int &row, const string text, const color lineColor)
  {
   if(row >= ArraySize(lines)) return;
   lines[row] = text; colors[row] = lineColor; row++;
  }

int ClampPanelWidth(const int width)
  {
   if(width < PANEL_MIN_WIDTH) return(PANEL_MIN_WIDTH);
   if(width > PANEL_MAX_WIDTH) return(PANEL_MAX_WIDTH);
   return(width);
  }

int PanelTextWidth(const string text)
  {
   return(StringLen(text) * PANEL_CHAR_WIDTH + PANEL_HORIZONTAL_PAD * 2);
  }

void ResizePanelForContent(const string &lines[], const int usedRows)
  {
   int panelWidth = PanelTextWidth("EA Zone NeverDie");
   for(int i = 0; i < usedRows && i < ArraySize(lines); i++)
      if(StringLen(lines[i]) > 1)
        {
         int lineWidth = PanelTextWidth(lines[i]);
         if(lineWidth > panelWidth) panelWidth = lineWidth;
        }

   panelWidth = ClampPanelWidth(panelWidth);
   int visibleRows = usedRows > 1 ? usedRows : 1;
   int panelHeight = PANEL_LINES_Y_OFFSET + visibleRows * PANEL_LINE_HEIGHT + PANEL_BOTTOM_PAD;
   int panelCenterX = PANEL_X + panelWidth / 2;

   ObjectSetInteger(0, g_panelPrefix + "_BG", OBJPROP_XSIZE, panelWidth);
   ObjectSetInteger(0, g_panelPrefix + "_BG", OBJPROP_YSIZE, panelHeight);
   ObjectSetInteger(0, g_panelPrefix + "_TITLE", OBJPROP_XDISTANCE, panelCenterX);

   for(int i = 0; i < PANEL_LINE_COUNT; i++)
     {
      string name = g_panelPrefix + "_LINE_" + IntegerToString(i);
      ObjectSetInteger(0, name, OBJPROP_XDISTANCE, panelCenterX);
     }
  }

color ZoneDisplayColor(const int globalIndex)
  {
   int slot = globalIndex % 12;
   if(slot == 0) return(C'30,144,255');   // Dodger blue
   if(slot == 1) return(C'255,140,0');    // Dark orange
   if(slot == 2) return(C'50,205,50');    // Lime green
   if(slot == 3) return(C'220,20,60');    // Crimson
   if(slot == 4) return(C'148,0,211');    // Violet
   if(slot == 5) return(C'0,191,255');    // Deep sky blue
   if(slot == 6) return(C'255,215,0');    // Gold
   if(slot == 7) return(C'255,105,180');  // Hot pink
   if(slot == 8) return(C'0,128,128');    // Teal
   if(slot == 9) return(C'178,34,34');    // Firebrick
   if(slot == 10) return(C'46,139,87');   // Sea green
   return(C'123,104,238');                // Medium slate blue
  }

string ZoneSideName(const ENUM_POSITION_TYPE side)
  {
   return(side == POSITION_TYPE_BUY ? "BUY" : "SELL");
  }

string ZoneLineName(const ENUM_POSITION_TYPE side, const ZoneData &zone, const int index, const string edge)
  {
   return(g_zoneLinePrefix + "_" + ZoneSideName(side) + "_" + IntegerToString(index + 1) + "_" + IntegerToString(zone.magic) + "_" + edge);
  }

void RemoveZoneChartObjects()
  {
   for(int i = ObjectsTotal(0, -1, -1) - 1; i >= 0; i--)
     {
      string name = ObjectName(0, i, -1, -1);
      if(StringFind(name, g_zoneLinePrefix) == 0)
         ObjectDelete(0, name);
     }
  }

void DrawZoneLine(const string name, const double price, const color zoneColor, const string text)
  {
   if(ObjectFind(0, name) == -1) ObjectCreate(0, name, OBJ_HLINE, 0, 0, price);
   ObjectSetDouble(0, name, OBJPROP_PRICE, price);
   ObjectSetInteger(0, name, OBJPROP_COLOR, zoneColor);
   ObjectSetInteger(0, name, OBJPROP_STYLE, STYLE_SOLID);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, 2);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, false);
   ObjectSetString(0, name, OBJPROP_TEXT, text);
  }

void DrawZoneLineLabel(const string name, const double price, const color zoneColor, const string text)
  {
   datetime labelTime = iTime(_Symbol, _Period, 0);
   if(labelTime <= 0) labelTime = TimeCurrent();

   if(ObjectFind(0, name) == -1) ObjectCreate(0, name, OBJ_TEXT, 0, labelTime, price);
   ObjectMove(0, name, 0, labelTime, price);
   ObjectSetInteger(0, name, OBJPROP_COLOR, zoneColor);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 9);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, ANCHOR_LEFT);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, false);
   ObjectSetString(0, name, OBJPROP_FONT, "Tahoma Bold");
   ObjectSetString(0, name, OBJPROP_TEXT, text);
  }

void DrawZoneLines(const ENUM_POSITION_TYPE side, const ZoneData &zone, const int index, const int globalIndex)
  {
   color zoneColor = ZoneDisplayColor(globalIndex);
   string sideName = ZoneSideName(side);
   string zoneLabel = sideName + " Zone " + IntegerToString(index + 1);
   string lowName = ZoneLineName(side, zone, index, "LOW");
   string highName = ZoneLineName(side, zone, index, "HIGH");
   string lowLabelName = ZoneLineName(side, zone, index, "LOW_LABEL");
   string highLabelName = ZoneLineName(side, zone, index, "HIGH_LABEL");

   DrawZoneLine(lowName, zone.low, zoneColor, zoneLabel + " LOW " + DoubleToString(zone.low, _Digits));
   DrawZoneLine(highName, zone.high, zoneColor, zoneLabel + " HIGH " + DoubleToString(zone.high, _Digits));
   DrawZoneLineLabel(lowLabelName, zone.low, zoneColor, zoneLabel + " LOW");
   DrawZoneLineLabel(highLabelName, zone.high, zoneColor, zoneLabel + " HIGH");
   ObjectSetInteger(0, lowName, OBJPROP_COLOR, zoneColor);
   ObjectSetInteger(0, highName, OBJPROP_COLOR, zoneColor);
  }

void UpdateZoneChartObjects()
  {
   RemoveZoneChartObjects();

   for(int i = 0; i < ArraySize(g_buyZones); i++)
     {
      if(g_buyZones[i].mode == ZONE_OFF) continue;
      DrawZoneLines(POSITION_TYPE_BUY, g_buyZones[i], i, ZoneGlobalIndex(POSITION_TYPE_BUY, i));
     }

   for(int i = 0; i < ArraySize(g_sellZones); i++)
     {
      if(g_sellZones[i].mode == ZONE_OFF) continue;
      DrawZoneLines(POSITION_TYPE_SELL, g_sellZones[i], i, ZoneGlobalIndex(POSITION_TYPE_SELL, i));
     }

   ChartRedraw(0);
  }

color ModeColor(const ENUM_ZONE_MODE mode)
  {
   if(mode == ZONE_TRADE) return(clrDodgerBlue);
   if(mode == ZONE_WATCH) return(clrDarkOrange);
   return(clrGray);
  }

color ProfitColor(const double profit)
  {
   if(profit > 0.0) return(clrForestGreen);
   if(profit < 0.0) return(clrTomato);
   return(clrBlack);
  }

color StateColor(const ENUM_ZONE_MODE mode)
  {
   if(mode == ZONE_TRADE) return(clrForestGreen);
   if(mode == ZONE_WATCH) return(clrDarkOrange);
   return(clrGray);
  }