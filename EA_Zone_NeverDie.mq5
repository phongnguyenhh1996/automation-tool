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
input int            InpTakeProfit          = 4000;      
input int            InpMaxGridLevels       = 50;
input long           InpMagicNumber         = 20241221;  
input ENUM_ND_DCA_TF InpDcaGridTimeframe    = ND_DCA_M15; 
input int            InpDcaClosedBarsRequired = 1;

input group "=== BASKET TRAILING ==="
input bool           InpUseTrailingStop    = false;
input int            InpTrailingDistance   = 800;
input int            InpTrailingStep       = 200;

input group "=== RISK ==="
input double         InpCutLossFullCloseAt = 90.0;

input group "=== DISPLAY ==="
input bool           InpShowPanel          = true;

input group "=== REMOTE ZONES JSON (Cloudinary / HTTPS) ==="
input string         InpZonesJsonUrl       = "https://res.cloudinary.com/easy-toeic/raw/upload/automation_tool/ea_neverdie/neverdie_XAUUSD.json"; // URL đã được cập nhật mặc định
input int            InpZonesPollSeconds   = 300;
input string         InpZonesBearer        = "";

const int PANEL_LINE_COUNT      = 28;
const int PANEL_LINE_HEIGHT     = 16;

// --- DYNAMIC ZONE STRUCTURE ---
struct ZoneData
  {
   ENUM_ZONE_MODE mode;
   double         low;
   double         high;
   double         sl;
   datetime       expireTime;
   long           magic;
   bool           trailArmed;
   double         trailExtreme;
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
bool     g_buyBlocked         = false;
bool     g_sellBlocked        = false;
string   g_buyBlockReason     = "";
string   g_sellBlockReason    = "";
datetime g_buyBlockedAt       = 0;
datetime g_sellBlockedAt      = 0;
string   g_panelPrefix        = "ZoneNeverDiePanel";

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

void AddZoneIfNotExists(ENUM_POSITION_TYPE side, ENUM_ZONE_MODE mode, double low, double high, double sl)
  {
   double minPrice = MathMin(low, high);
   double maxPrice = MathMax(low, high);
   
   if(side == POSITION_TYPE_BUY)
     {
      for(int i = 0; i < ArraySize(g_buyZones); i++)
        {
         if(g_buyZones[i].low == minPrice && g_buyZones[i].high == maxPrice)
           {
            // Cập nhật Mode và SL nếu có thay đổi từ Server
            g_buyZones[i].mode = mode;
            g_buyZones[i].sl = sl;
            return;
           }
        }
      if(mode == ZONE_OFF) return; // Không thêm mới nếu đang tắt
      
      int size = ArraySize(g_buyZones);
      ArrayResize(g_buyZones, size + 1);
      g_buyZones[size].mode = mode;
      g_buyZones[size].low = minPrice;
      g_buyZones[size].high = maxPrice;
      g_buyZones[size].sl = sl;
      g_buyZones[size].expireTime = GetNext2AM();
      g_buyZones[size].magic = GetZoneMagic(minPrice, maxPrice);
      g_buyZones[size].trailArmed = false;
      g_buyZones[size].trailExtreme = 0.0;
     }
   else
     {
      for(int i = 0; i < ArraySize(g_sellZones); i++)
        {
         if(g_sellZones[i].low == minPrice && g_sellZones[i].high == maxPrice)
           {
            g_sellZones[i].mode = mode;
            g_sellZones[i].sl = sl;
            return;
           }
        }
      if(mode == ZONE_OFF) return;
      
      int size = ArraySize(g_sellZones);
      ArrayResize(g_sellZones, size + 1);
      g_sellZones[size].mode = mode;
      g_sellZones[size].low = minPrice;
      g_sellZones[size].high = maxPrice;
      g_sellZones[size].sl = sl;
      g_sellZones[size].expireTime = GetNext2AM();
      g_sellZones[size].magic = GetZoneMagic(minPrice, maxPrice);
      g_sellZones[size].trailArmed = false;
      g_sellZones[size].trailExtreme = 0.0;
     }
  }

bool ApplyNeverdieJson(const string json)
  {
   ENUM_ZONE_MODE bm, sm;
   double bl, bh, bs, sl, sh, ss;
   if(ParseNeverdieSide(json, "buy", bm, bl, bh, bs))
      AddZoneIfNotExists(POSITION_TYPE_BUY, bm, bl, bh, bs);
   if(ParseNeverdieSide(json, "sell", sm, sl, sh, ss))
      AddZoneIfNotExists(POSITION_TYPE_SELL, sm, sl, sh, ss);
   return(true);
  }

void FetchNeverdieJsonFromUrl()
  {
   uchar req[], res[];
   string headers_out;
   string hdr = (StringLen(InpZonesBearer) > 0) ? "Authorization: Bearer " + InpZonesBearer + "\r\n" : "";
   ResetLastError();
   int code = WebRequest("GET", InpZonesJsonUrl, hdr, 15000, req, res, headers_out);
   
   if(code == -1) { PrintFormat("EA NeverDie: WebRequest failed (Err: %d) - Check URL settings", GetLastError()); return; }
   if(code != 200) { PrintFormat("EA NeverDie: HTTP %d from URL", code); return; }
   
   string body = CharArrayToString(res);
   if(!ApplyNeverdieJson(body)) Print("EA NeverDie: JSON parse failed");
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
      FetchNeverdieJsonFromUrl();
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
  }

void OnTimer()
  {
   if(!NeverdieUseRemoteJson()) return;
   FetchNeverdieJsonFromUrl();
   UpdatePanel();
  }

void OnTick()
  {
   if(!IsEnvironmentReady()) { UpdatePanel(); return; }
   if(ManageRiskCutLoss()) { UpdatePanel(); return; }

   CleanupExpiredZones();
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
      BlockSide(POSITION_TYPE_BUY, "SL hit");
      CloseBasket(POSITION_TYPE_BUY, magic);
     }
   else if(dealType == DEAL_TYPE_BUY)
     {
      BlockSide(POSITION_TYPE_SELL, "SL hit");
      CloseBasket(POSITION_TYPE_SELL, magic);
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

void ManageZoneStopsAndWatchers()
  {
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick)) return;
   double monitorPrice = MidPrice(tick);

   // SL Checks
   for(int i = ArraySize(g_buyZones) - 1; i >= 0; i--)
     {
      if(g_buyZones[i].sl > 0 && tick.bid <= g_buyZones[i].sl && HasOpenPositions(g_buyZones[i].magic))
        {
         BlockSide(POSITION_TYPE_BUY, "SL price touched");
         CloseBasket(POSITION_TYPE_BUY, g_buyZones[i].magic);
         g_buyZones[i].trailArmed = false;
        }
     }

   for(int i = ArraySize(g_sellZones) - 1; i >= 0; i--)
     {
      if(g_sellZones[i].sl > 0 && tick.ask >= g_sellZones[i].sl && HasOpenPositions(g_sellZones[i].magic))
        {
         BlockSide(POSITION_TYPE_SELL, "SL price touched");
         CloseBasket(POSITION_TYPE_SELL, g_sellZones[i].magic);
         g_sellZones[i].trailArmed = false;
        }
     }

   // BLOCK LOGIC: Giá lọt vào vùng TRADE của phe đối lập
   if(GetActiveZoneIndex(POSITION_TYPE_SELL, monitorPrice) != -1 && TotalOpenPositions(POSITION_TYPE_BUY) > 0)
      BlockSide(POSITION_TYPE_BUY, "SELL trade zone hit");

   if(GetActiveZoneIndex(POSITION_TYPE_BUY, monitorPrice) != -1 && TotalOpenPositions(POSITION_TYPE_SELL) > 0)
      BlockSide(POSITION_TYPE_SELL, "BUY trade zone hit");
  }

void ManageAllZones(const ENUM_POSITION_TYPE side, const bool onFirstTickDca)
  {
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick)) return;
   double price = GetSideOpenPrice(side, tick);
   int newestActiveIdx = GetActiveZoneIndex(side, price);
   int totalZones = (side == POSITION_TYPE_BUY) ? ArraySize(g_buyZones) : ArraySize(g_sellZones);

   for(int i = 0; i < totalZones; i++)
     {
      ZoneData zone = (side == POSITION_TYPE_BUY) ? g_buyZones[i] : g_sellZones[i];
      BasketInfo basket;
      BuildBasket(side, zone.magic, basket);

      if(basket.count > 0)
        {
         if(ShouldCloseBasketByTakeProfit(side, basket)) { CloseBasket(side, zone.magic); zone.trailArmed = false; }
         else if(ShouldCloseBasketByTrailing(side, zone, basket)) { CloseBasket(side, zone.magic); zone.trailArmed = false; }
         else if(ShouldOpenDca(side, zone, basket, onFirstTickDca))
           {
            double nextVolume = NormalizeVolume(InpLotSize * MathPow(InpMultiplier, basket.count));
            OpenPosition(side, zone, nextVolume, "DCA");
           }
        }
      else
        {
         // Chỉ mở lệnh Initial nếu Zone này là Zone mới nhất thỏa mãn điều kiện
         if(i == newestActiveIdx && ShouldOpenInitial(side, zone, price))
           {
            zone.trailArmed = false;
            zone.trailExtreme = 0.0;
            OpenPosition(side, zone, NormalizeVolume(InpLotSize), "START");
           }
        }

      // Ghi lại thay đổi state (Trail state)
      if(side == POSITION_TYPE_BUY) g_buyZones[i] = zone;
      else g_sellZones[i] = zone;
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
   if(IsSideBlocked(side)) return(false);

   if(side == POSITION_TYPE_BUY && zone.sl > 0 && price <= zone.sl) return(false);
   if(side == POSITION_TYPE_SELL && zone.sl > 0 && price >= zone.sl) return(false);

   return(price >= zone.low && price <= zone.high);
  }

bool ShouldOpenDca(const ENUM_POSITION_TYPE side, const ZoneData &zone, const BasketInfo &basket, const bool onFirstTick)
  {
   if(!onFirstTick) return(false);
   if(zone.mode != ZONE_TRADE) return(false);
   if(IsSideBlocked(side)) return(false);
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

bool ShouldCloseBasketByTakeProfit(const ENUM_POSITION_TYPE side, const BasketInfo &basket)
  {
   if(basket.count <= 0 || basket.totalVolume <= 0.0) return(false);
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick)) return(false);
   
   double currentPrice = GetSideClosePrice(side, tick);
   double targetPrice = basket.averagePrice + DirectionMultiplier(side) * InpTakeProfit * _Point;

   if(side == POSITION_TYPE_BUY && currentPrice >= targetPrice && basket.floatingProfit > 0.0) return(true);
   if(side == POSITION_TYPE_SELL && currentPrice <= targetPrice && basket.floatingProfit > 0.0) return(true);
   return(false);
  }

bool ShouldCloseBasketByTrailing(const ENUM_POSITION_TYPE side, ZoneData &zone, const BasketInfo &basket)
  {
   if(!InpUseTrailingStop || basket.count <= 0)
     {
      zone.trailArmed = false;
      return(false);
     }

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick)) return(false);

   double currentPrice = GetSideClosePrice(side, tick);
   double activateAt = basket.averagePrice + DirectionMultiplier(side) * InpTrailingDistance * _Point;
   double retrace = InpTrailingStep * _Point;

   if(side == POSITION_TYPE_BUY)
     {
      if(currentPrice >= activateAt)
        {
         if(!zone.trailArmed) { zone.trailArmed = true; zone.trailExtreme = currentPrice; }
         else if(currentPrice > zone.trailExtreme) zone.trailExtreme = currentPrice;
        }
      if(zone.trailArmed && currentPrice <= zone.trailExtreme - retrace && basket.floatingProfit > 0.0) return(true);
     }
   else
     {
      if(currentPrice <= activateAt)
        {
         if(!zone.trailArmed) { zone.trailArmed = true; zone.trailExtreme = currentPrice; }
         else if(currentPrice < zone.trailExtreme) zone.trailExtreme = currentPrice;
        }
      if(zone.trailArmed && currentPrice >= zone.trailExtreme + retrace && basket.floatingProfit > 0.0) return(true);
     }
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
   for(int i = 0; i < ArraySize(g_buyZones); i++) g_buyZones[i].trailArmed = false;
   for(int i = 0; i < ArraySize(g_sellZones); i++) g_sellZones[i].trailArmed = false;
  }

void BlockSide(const ENUM_POSITION_TYPE side, const string reason)
  {
   if(side == POSITION_TYPE_BUY)
     {
      if(g_buyBlocked && g_buyBlockReason == reason) return;
      g_buyBlocked = true; g_buyBlockReason = reason; g_buyBlockedAt = TimeCurrent();
     }
   else
     {
      if(g_sellBlocked && g_sellBlockReason == reason) return;
      g_sellBlocked = true; g_sellBlockReason = reason; g_sellBlockedAt = TimeCurrent();
     }
   Print(side == POSITION_TYPE_BUY ? "BUY" : "SELL", " side blocked: ", reason);
  }

bool IsSideBlocked(const ENUM_POSITION_TYPE side) { return(side == POSITION_TYPE_BUY ? g_buyBlocked : g_sellBlocked); }

bool ValidateInputs()
  {
   if(InpLotSize <= 0.0 || InpMultiplier < 1.0) return(false);
   if(InpGridStep <= 0 || InpTakeProfit <= 0 || InpMaxGridLevels < 1) return(false);
   if(InpDcaClosedBarsRequired < 1) return(false);
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
   if(IsSideBlocked(side)) return("BLOCKED");
   ZoneData z = GetLatestZone(side);
   if(z.mode == ZONE_TRADE) return("READY");
   if(z.mode == ZONE_WATCH) return("WATCHING");
   return("OFF");
  }

string SideReasonText(const ENUM_POSITION_TYPE side)
  {
   if(!IsSideBlocked(side)) return("-");
   return(side == POSITION_TYPE_BUY ? g_buyBlockReason : g_sellBlockReason);
  }

string OverallStatusText()
  {
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED)) return("Disabled");
   if(g_buyBlocked && g_sellBlocked) return("Buy/Sell Blocked");
   if(g_buyBlocked) return("Buy Blocked");
   if(g_sellBlocked) return("Sell Blocked");
   return("Trading");
  }

string ZoneRangeText(const ENUM_POSITION_TYPE side)
  {
   int count = (side == POSITION_TYPE_BUY) ? ArraySize(g_buyZones) : ArraySize(g_sellZones);
   if(count == 0) return("OFF");
   ZoneData z = GetLatestZone(side);
   string txt = StringFormat("%.2f - %.2f", z.low, z.high);
   if(count > 1) txt += StringFormat(" (+%d zones)", count - 1);
   return txt;
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
   ObjectSetInteger(0, bg, OBJPROP_XDISTANCE, 12); ObjectSetInteger(0, bg, OBJPROP_YDISTANCE, 30);
   ObjectSetInteger(0, bg, OBJPROP_XSIZE, 220); ObjectSetInteger(0, bg, OBJPROP_YSIZE, 470); // Kéo dài background một chút
   ObjectSetInteger(0, bg, OBJPROP_BGCOLOR, clrWhiteSmoke); ObjectSetInteger(0, bg, OBJPROP_COLOR, clrBlack);
   ObjectSetInteger(0, bg, OBJPROP_SELECTABLE, false); ObjectSetInteger(0, bg, OBJPROP_HIDDEN, true);

   if(ObjectFind(0, title) == -1) ObjectCreate(0, title, OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, title, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, title, OBJPROP_XDISTANCE, 45); // Căn giữa lại do title ngắn đi
   ObjectSetInteger(0, title, OBJPROP_YDISTANCE, 40);
   ObjectSetInteger(0, title, OBJPROP_COLOR, clrBlack); ObjectSetInteger(0, title, OBJPROP_FONTSIZE, 11);
   ObjectSetString(0, title, OBJPROP_FONT, "Tahoma Bold"); 
   ObjectSetString(0, title, OBJPROP_TEXT, "EA Zone NeverDie"); // Title rút gọn theo ý bạn
   ObjectSetInteger(0, title, OBJPROP_SELECTABLE, false); ObjectSetInteger(0, title, OBJPROP_HIDDEN, true);

   for(int i = 0; i < PANEL_LINE_COUNT; i++) CreatePanelLine(i);
  }

void CreatePanelLine(const int index)
  {
   string name = g_panelPrefix + "_LINE_" + IntegerToString(index);
   if(ObjectFind(0, name) == -1) ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, 22); 
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, 70 + index * PANEL_LINE_HEIGHT); // Chỉnh lại tọa độ Y cho cân đối
   ObjectSetInteger(0, name, OBJPROP_COLOR, clrBlack); ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 9);
   ObjectSetString(0, name, OBJPROP_FONT, "Tahoma"); ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
   ObjectSetString(0, name, OBJPROP_TEXT, " "); // Khởi tạo text bằng khoảng trắng để diệt lỗi "Label"
  }

void RemovePanel()
  {
   ObjectDelete(0, g_panelPrefix + "_BG"); ObjectDelete(0, g_panelPrefix + "_TITLE");
   for(int i = 0; i < PANEL_LINE_COUNT; i++) ObjectDelete(0, g_panelPrefix + "_LINE_" + IntegerToString(i));
  }

void UpdatePanel()
  {
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

   ZoneData lBuy = GetLatestZone(POSITION_TYPE_BUY);
   ZoneData lSell = GetLatestZone(POSITION_TYPE_SELL);

   int row = 0;
   AddPanelRow(lines, colors, row, "---- Account Data ----", clrDimGray);
   AddPanelRow(lines, colors, row, "Balance: " + DoubleToString(balance, 2), clrBlack);
   AddPanelRow(lines, colors, row, "Equity:  " + DoubleToString(equity, 2), clrBlack);
   AddPanelRow(lines, colors, row, "DD:      " + DoubleToString(ddPercent, 1) + "%", clrBlack);
   AddPanelRow(lines, colors, row, " ", clrBlack); // Dùng " " thay vì ""

   AddPanelRow(lines, colors, row, "------ " + _Symbol + " ------", clrDimGray);
   AddPanelRow(lines, colors, row, "Total Orders: " + IntegerToString(TotalOpenPositions(POSITION_TYPE_BUY)+TotalOpenPositions(POSITION_TYPE_SELL)), clrBlack);
   AddPanelRow(lines, colors, row, " ", clrBlack); // Dùng " " thay vì ""

   AddPanelRow(lines, colors, row, "------- Status -------", clrDimGray);
   AddPanelRow(lines, colors, row, "Buy Mode:  " + ZoneModeText(lBuy.mode), ModeColor(lBuy.mode));
   AddPanelRow(lines, colors, row, "Buy Zone:  " + ZoneRangeText(POSITION_TYPE_BUY), clrBlack);
   AddPanelRow(lines, colors, row, "Buy SL:    " + ZoneStopText(POSITION_TYPE_BUY), clrBlack);
   AddPanelRow(lines, colors, row, "Buy State: " + SideStatusText(POSITION_TYPE_BUY), StateColor(POSITION_TYPE_BUY, lBuy.mode));
   AddPanelRow(lines, colors, row, "Buy Note:  " + SideReasonText(POSITION_TYPE_BUY), clrBlack);
   AddPanelRow(lines, colors, row, " ", clrBlack); // Tạo khoảng cách nhỏ giữa Buy và Sell
   
   AddPanelRow(lines, colors, row, "Sell Mode: " + ZoneModeText(lSell.mode), ModeColor(lSell.mode));
   AddPanelRow(lines, colors, row, "Sell Zone: " + ZoneRangeText(POSITION_TYPE_SELL), clrBlack);
   AddPanelRow(lines, colors, row, "Sell SL:   " + ZoneStopText(POSITION_TYPE_SELL), clrBlack);
   AddPanelRow(lines, colors, row, "Sell State:" + " " + SideStatusText(POSITION_TYPE_SELL), StateColor(POSITION_TYPE_SELL, lSell.mode));
   AddPanelRow(lines, colors, row, "Sell Note: " + SideReasonText(POSITION_TYPE_SELL), clrBlack);
   AddPanelRow(lines, colors, row, " ", clrBlack);
   
   AddPanelRow(lines, colors, row, "Status:    " + OverallStatusText(), clrDarkGreen);

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

color ModeColor(const ENUM_ZONE_MODE mode)
  {
   if(mode == ZONE_TRADE) return(clrDodgerBlue);
   if(mode == ZONE_WATCH) return(clrDarkOrange);
   return(clrGray);
  }

color StateColor(const ENUM_POSITION_TYPE side, const ENUM_ZONE_MODE mode)
  {
   if(IsSideBlocked(side)) return(clrTomato);
   if(mode == ZONE_TRADE) return(clrForestGreen);
   if(mode == ZONE_WATCH) return(clrDarkOrange);
   return(clrGray);
  }