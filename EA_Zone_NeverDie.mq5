#property strict
#property description "EA Zone NeverDie MT5"

#include <Trade/Trade.mqh>

enum ENUM_ZONE_MODE
  {
   ZONE_OFF   = 0,
   ZONE_TRADE = 1,
   ZONE_WATCH = 2
  };

// Only M5/M15 (do not use ENUM_TIMEFRAMES here — MT5 input dialog lists every TF and
// choosing anything else caused INIT_PARAMETERS_INCORRECT / "incorrect parameters".)
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
input double         InpLotSize            = 0.01;
input double         InpMultiplier         = 1.20;
input int            InpGridStep           = 15000;
input int            InpTakeProfit         = 5000;
input int            InpMaxGridLevels      = 50;
input long           InpMagicNumber        = 20241221;
input ENUM_ND_DCA_TF InpDcaGridTimeframe    = ND_DCA_M5; // DCA re-checks on new bar of this TF (M5 or M15 only)
input int            InpDcaClosedBarsRequired = 1;     // min closed bars on that TF since last entry (iBarShift >= this)

input group "=== BASKET TRAILING ==="
input bool           InpUseTrailingStop    = false;
input int            InpTrailingDistance   = 800;
input int            InpTrailingStep       = 200;

input group "=== RISK ==="
input double         InpCutLossFullCloseAt = 90.0;

input group "=== DISPLAY ==="
input bool           InpShowPanel          = true;

input group "=== REMOTE ZONES JSON (Cloudinary / HTTPS) ==="
input string         InpZonesJsonUrl       = "";
input int            InpZonesPollSeconds   = 15;
input string         InpZonesBearer        = "";

const int PANEL_LINE_COUNT      = 28;
const int PANEL_LINE_HEIGHT     = 14;

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

CTrade  g_trade;
datetime g_dcaGridBarOpenSeen = 0; // last DCA TF bar open time processed (OnInit + OnTick)
bool    g_buyBlocked       = false;
bool    g_sellBlocked      = false;
string  g_buyBlockReason   = "";
string  g_sellBlockReason  = "";
datetime g_buyBlockedAt    = 0;
datetime g_sellBlockedAt   = 0;
bool    g_buyTrailArmed    = false;
bool    g_sellTrailArmed   = false;
double  g_buyTrailExtreme  = 0.0;
double  g_sellTrailExtreme = 0.0;
string  g_panelPrefix      = "ZoneNeverDiePanel";

ENUM_ZONE_MODE g_dynBuyMode   = ZONE_OFF;
ENUM_ZONE_MODE g_dynSellMode  = ZONE_OFF;
double   g_dynBuyLow          = 0.0;
double   g_dynBuyHigh         = 0.0;
double   g_dynBuySL           = 0.0;
double   g_dynSellLow         = 0.0;
double   g_dynSellHigh        = 0.0;
double   g_dynSellSL          = 0.0;

bool NeverdieUseRemoteJson()
  {
   if(MQLInfoInteger(MQL_TESTER))
      return(false);
   if(InpZonesPollSeconds <= 0)
      return(false);
   return(StringLen(InpZonesJsonUrl) > 0);
  }

bool ExtractJsonObjectForKey(const string j, const string key, string &outObj)
  {
   string q = "\"" + key + "\"";
   int p = StringFind(j, q);
   if(p < 0)
      return(false);
   int b = StringFind(j, "{", p);
   if(b < 0)
      return(false);
   int depth = 0;
   for(int i = b; i < StringLen(j); i++)
     {
      ushort c = StringGetCharacter(j, i);
      if(c == '{')
         depth++;
      else if(c == '}')
        {
         depth--;
         if(depth == 0)
           {
            outObj = StringSubstr(j, b, i - b + 1);
            return(true);
           }
        }
     }
   return(false);
  }

string JsonExtractStringValue(const string o, const string key)
  {
   string pat = "\"" + key + "\"";
   int p = StringFind(o, pat);
   if(p < 0)
      return("");
   p += StringLen(pat);
   while(p < StringLen(o) && (StringGetCharacter(o, p) == ' ' || StringGetCharacter(o, p) == ':'))
      p++;
   while(p < StringLen(o) && StringGetCharacter(o, p) == ' ')
      p++;
   if(StringGetCharacter(o, p) != '"')
      return("");
   p++;
   int e = StringFind(o, "\"", p);
   if(e < 0)
      return("");
   return(StringSubstr(o, p, e - p));
  }

double JsonExtractDoubleValue(const string o, const string key)
  {
   string pat = "\"" + key + "\"";
   int p = StringFind(o, pat);
   if(p < 0)
      return(0.0);
   p += StringLen(pat);
   while(p < StringLen(o) && (StringGetCharacter(o, p) == ' ' || StringGetCharacter(o, p) == ':'))
      p++;
   while(p < StringLen(o) && StringGetCharacter(o, p) == ' ')
      p++;
   return(StringToDouble(StringSubstr(o, p)));
  }

ENUM_ZONE_MODE ModeFromJsonString(const string m)
  {
   if(StringCompare(m, "trade", false) == 0)
      return(ZONE_TRADE);
   if(StringCompare(m, "watch", false) == 0)
      return(ZONE_WATCH);
   return(ZONE_OFF);
  }

bool ParseNeverdieSide(const string json, const string key,
                       ENUM_ZONE_MODE &mode, double &lo, double &hi, double &sl)
  {
   string o;
   if(!ExtractJsonObjectForKey(json, key, o))
      return(false);
   string ms = JsonExtractStringValue(o, "mode");
   mode = ModeFromJsonString(ms);
   lo = JsonExtractDoubleValue(o, "low");
   hi = JsonExtractDoubleValue(o, "high");
   sl = JsonExtractDoubleValue(o, "sl");
   return(true);
  }

bool ApplyNeverdieJson(const string json)
  {
   ENUM_ZONE_MODE bm;
   ENUM_ZONE_MODE sm;
   double bl, bh, bs;
   double sl, sh, ss;
   if(!ParseNeverdieSide(json, "buy", bm, bl, bh, bs))
      return(false);
   if(!ParseNeverdieSide(json, "sell", sm, sl, sh, ss))
      return(false);
   g_dynBuyMode  = bm;
   g_dynBuyLow   = bl;
   g_dynBuyHigh  = bh;
   g_dynBuySL    = bs;
   g_dynSellMode = sm;
   g_dynSellLow  = sl;
   g_dynSellHigh = sh;
   g_dynSellSL   = ss;
   return(true);
  }

void FetchNeverdieJsonFromUrl()
  {
   uchar req[];
   uchar res[];
   string headers_out;
   ArrayResize(req, 0);
   string hdr = "";
   if(StringLen(InpZonesBearer) > 0)
      hdr = "Authorization: Bearer " + InpZonesBearer + "\r\n";
   ResetLastError();
   int code = WebRequest("GET", InpZonesJsonUrl, hdr, 15000, req, res, headers_out);
   if(code == -1)
     {
      PrintFormat("EA NeverDie: WebRequest failed | err=%d | allow URL in Terminal settings",
                  GetLastError());
      return;
     }
   if(code != 200)
     {
      PrintFormat("EA NeverDie: HTTP %d from zones JSON URL", code);
      return;
     }
   string body = CharArrayToString(res);
   if(!ApplyNeverdieJson(body))
      Print("EA NeverDie: JSON parse failed (expected buy/sell objects)");
  }

int OnInit()
  {
   if(!ValidateInputs())
      return(INIT_PARAMETERS_INCORRECT);

   g_trade.SetExpertMagicNumber(InpMagicNumber);
   g_trade.SetDeviationInPoints(20);
   g_trade.SetTypeFillingBySymbol(_Symbol);

   if(NeverdieUseRemoteJson())
     {
      EventSetTimer(InpZonesPollSeconds);
      FetchNeverdieJsonFromUrl();
     }

   if(InpShowPanel)
      CreatePanel();
   else
      RemovePanel();

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
   if(!NeverdieUseRemoteJson())
      return;
   FetchNeverdieJsonFromUrl();
   UpdatePanel();
  }

void OnTick()
  {
   if(!IsEnvironmentReady())
     {
      UpdatePanel();
      return;
     }

   if(ManageRiskCutLoss())
     {
      UpdatePanel();
      return;
     }

   ManageZoneStopsAndWatchers();

   bool onFirstTickOfNewDcaBar = false;
   datetime dcaBarOpen        = iTime(_Symbol, NdDcaTimeframe(InpDcaGridTimeframe), 0);
   if(dcaBarOpen > 0 && dcaBarOpen != g_dcaGridBarOpenSeen)
     {
      g_dcaGridBarOpenSeen      = dcaBarOpen;
      onFirstTickOfNewDcaBar = true;
     }

   ManageDirection(POSITION_TYPE_BUY, onFirstTickOfNewDcaBar);
   ManageDirection(POSITION_TYPE_SELL, onFirstTickOfNewDcaBar);
   UpdatePanel();
  }

void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
  {
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD || trans.deal == 0)
      return;

   if(!HistoryDealSelect(trans.deal))
      return;

   if(HistoryDealGetString(trans.deal, DEAL_SYMBOL) != _Symbol)
      return;

   if(HistoryDealGetInteger(trans.deal, DEAL_MAGIC) != InpMagicNumber)
      return;

   long entry = HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
   if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY && entry != DEAL_ENTRY_INOUT)
      return;

   if(HistoryDealGetInteger(trans.deal, DEAL_REASON) != DEAL_REASON_SL)
      return;

   long dealType = HistoryDealGetInteger(trans.deal, DEAL_TYPE);
   if(dealType == DEAL_TYPE_SELL)
     {
      BlockSide(POSITION_TYPE_BUY, "SL hit");
      CloseBasket(POSITION_TYPE_BUY);
     }
   else if(dealType == DEAL_TYPE_BUY)
     {
      BlockSide(POSITION_TYPE_SELL, "SL hit");
      CloseBasket(POSITION_TYPE_SELL);
     }
  }

bool ValidateInputs()
  {
   if(InpLotSize <= 0.0 || InpMultiplier < 1.0)
     {
      Print("EA NeverDie: InpLotSize must be > 0 and InpMultiplier must be >= 1.0");
      return(false);
     }

   if(InpGridStep <= 0 || InpTakeProfit <= 0 || InpMaxGridLevels < 1)
     {
      Print("EA NeverDie: InpGridStep and InpTakeProfit must be > 0; InpMaxGridLevels must be >= 1");
      return(false);
     }

   if(InpDcaClosedBarsRequired < 1)
     {
      Print("EA NeverDie: InpDcaClosedBarsRequired must be >= 1");
      return(false);
     }

   if(StringLen(InpZonesJsonUrl) == 0)
     {
      if(!ValidateZoneConfig(POSITION_TYPE_BUY))
         return(false);

      if(!ValidateZoneConfig(POSITION_TYPE_SELL))
         return(false);
     }

   return(true);
  }

bool ValidateZoneConfig(const ENUM_POSITION_TYPE side)
  {
   const string sideTxt = (side == POSITION_TYPE_BUY ? "BUY" : "SELL");
   ENUM_ZONE_MODE mode = GetZoneMode(side);
   if(mode == ZONE_OFF)
      return(true);

   double low  = GetZoneLow(side);
   double high = GetZoneHigh(side);
   if(low <= 0.0 || high <= 0.0)
     {
      PrintFormat("EA NeverDie: %s zone low/high must be > 0 (empty InpZonesJsonUrl — load zones from JSON or set URL)",
                  sideTxt);
      return(false);
     }

   if(mode == ZONE_TRADE)
     {
      double stop = GetZoneStopLoss(side);
      if(stop <= 0.0)
        {
         PrintFormat("EA NeverDie: %s ZONE_TRADE requires stop loss (sl) > 0 in static/JSON config", sideTxt);
         return(false);
        }

      if(side == POSITION_TYPE_BUY && stop >= MathMin(low, high))
        {
         PrintFormat("EA NeverDie: %s trade zone: SL must be below zone (sl < min(low,high))", sideTxt);
         return(false);
        }

      if(side == POSITION_TYPE_SELL && stop <= MathMax(low, high))
        {
         PrintFormat("EA NeverDie: %s trade zone: SL must be above zone (sl > max(low,high))", sideTxt);
         return(false);
        }
     }

   return(true);
  }

bool IsEnvironmentReady()
  {
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
      return(false);

   if(!MQLInfoInteger(MQL_TRADE_ALLOWED))
      return(false);

   if(Bars(_Symbol, _Period) < 100)
      return(false);

   return(true);
  }

void ManageZoneStopsAndWatchers()
  {
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
      return;

   BasketInfo buyBasket;
   BasketInfo sellBasket;
   BuildBasket(POSITION_TYPE_BUY, buyBasket);
   BuildBasket(POSITION_TYPE_SELL, sellBasket);

   if(buyBasket.count > 0 && IsZoneStopHit(POSITION_TYPE_BUY, tick.bid))
     {
      BlockSide(POSITION_TYPE_BUY, "SL price touched");
      CloseBasket(POSITION_TYPE_BUY);
     }

   if(sellBasket.count > 0 && IsZoneStopHit(POSITION_TYPE_SELL, tick.ask))
     {
      BlockSide(POSITION_TYPE_SELL, "SL price touched");
      CloseBasket(POSITION_TYPE_SELL);
     }

   double monitorPrice = MidPrice(tick);

   if(buyBasket.count > 0 &&
      GetZoneMode(POSITION_TYPE_SELL) == ZONE_WATCH &&
      IsInsideTradeZone(POSITION_TYPE_SELL, monitorPrice))
      BlockSide(POSITION_TYPE_BUY, "SELL watch zone hit");

   if(sellBasket.count > 0 &&
      GetZoneMode(POSITION_TYPE_BUY) == ZONE_WATCH &&
      IsInsideTradeZone(POSITION_TYPE_BUY, monitorPrice))
      BlockSide(POSITION_TYPE_SELL, "BUY watch zone hit");
  }

void ManageDirection(const ENUM_POSITION_TYPE side, const bool onFirstTickOfNewDcaBar)
  {
   BasketInfo basket;
   BuildBasket(side, basket);

   if(basket.count <= 0)
     {
      ResetTrailState(side);

      if(ShouldOpenInitial(side))
         OpenPosition(side, NormalizeVolume(InpLotSize), "START");

      return;
     }

   if(ShouldCloseBasketByTakeProfit(side, basket))
     {
      CloseBasket(side);
      return;
     }

   if(ShouldCloseBasketByTrailing(side, basket))
     {
      CloseBasket(side);
      return;
     }

   if(ShouldOpenDca(side, basket, onFirstTickOfNewDcaBar))
     {
      double nextVolume = NormalizeVolume(InpLotSize * MathPow(InpMultiplier, basket.count));
      OpenPosition(side, nextVolume, "DCA");
     }
  }

void BuildBasket(const ENUM_POSITION_TYPE side, BasketInfo &basket)
  {
   basket.count            = 0;
   basket.totalVolume      = 0.0;
   basket.weightedPriceSum = 0.0;
   basket.averagePrice     = 0.0;
   basket.floatingProfit   = 0.0;
   basket.lastVolume       = 0.0;
   basket.lastOpenPrice    = 0.0;
   basket.lastOpenTime     = 0;

   for(int index = PositionsTotal() - 1; index >= 0; index--)
     {
      ulong ticket = PositionGetTicket(index);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;

      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;

      if((long)PositionGetInteger(POSITION_MAGIC) != InpMagicNumber)
         continue;

      if((ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE) != side)
         continue;

      double volume    = PositionGetDouble(POSITION_VOLUME);
      double openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
      double profit    = PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
      datetime opened  = (datetime)PositionGetInteger(POSITION_TIME);

      basket.count++;
      basket.totalVolume      += volume;
      basket.weightedPriceSum += openPrice * volume;
      basket.floatingProfit   += profit;

      if(opened >= basket.lastOpenTime)
        {
         basket.lastOpenTime  = opened;
         basket.lastOpenPrice = openPrice;
         basket.lastVolume    = volume;
        }
     }

   if(basket.totalVolume > 0.0)
      basket.averagePrice = basket.weightedPriceSum / basket.totalVolume;
  }

bool ShouldOpenInitial(const ENUM_POSITION_TYPE side)
  {
   if(GetZoneMode(side) != ZONE_TRADE)
      return(false);

   if(IsSideBlocked(side))
      return(false);

   if(!HasConfiguredZone(side))
      return(false);

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
      return(false);

   double price = GetSideOpenPrice(side, tick);

   if(IsZoneStopHit(side, price))
     {
      BlockSide(side, "SL already touched");
      return(false);
     }

   return(IsInsideTradeZone(side, price));
  }

bool ShouldOpenDca(const ENUM_POSITION_TYPE side, const BasketInfo &basket, const bool onFirstTickOfNewDcaBar)
  {
   if(!onFirstTickOfNewDcaBar)
      return(false);

   if(GetZoneMode(side) != ZONE_TRADE)
      return(false);

   if(IsSideBlocked(side))
      return(false);

   if(basket.count <= 0 || basket.count >= InpMaxGridLevels)
      return(false);

   if(basket.floatingProfit >= 0.0)
      return(false);

   int shiftSinceOpen = iBarShift(_Symbol, NdDcaTimeframe(InpDcaGridTimeframe), basket.lastOpenTime, false);
   if(shiftSinceOpen < InpDcaClosedBarsRequired)
      return(false);

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
      return(false);

   double price    = GetSideOpenPrice(side, tick);
   double distance = MathAbs(price - basket.lastOpenPrice) / _Point;

   if(distance < InpGridStep)
      return(false);

   if(side == POSITION_TYPE_BUY && price >= basket.lastOpenPrice)
      return(false);

   if(side == POSITION_TYPE_SELL && price <= basket.lastOpenPrice)
      return(false);

   if(IsZoneStopHit(side, price))
     {
      BlockSide(side, "SL already touched");
      return(false);
     }

   return(true);
  }

bool ShouldCloseBasketByTakeProfit(const ENUM_POSITION_TYPE side, const BasketInfo &basket)
  {
   if(basket.count <= 0 || basket.totalVolume <= 0.0)
      return(false);

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
      return(false);

   double currentPrice = GetSideClosePrice(side, tick);
   double targetPrice  = basket.averagePrice + DirectionMultiplier(side) * InpTakeProfit * _Point;

   if(side == POSITION_TYPE_BUY && currentPrice >= targetPrice && basket.floatingProfit > 0.0)
      return(true);

   if(side == POSITION_TYPE_SELL && currentPrice <= targetPrice && basket.floatingProfit > 0.0)
      return(true);

   return(false);
  }

bool ShouldCloseBasketByTrailing(const ENUM_POSITION_TYPE side, const BasketInfo &basket)
  {
   if(!InpUseTrailingStop || basket.count <= 0)
     {
      ResetTrailState(side);
      return(false);
     }

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
      return(false);

   double currentPrice = GetSideClosePrice(side, tick);
   double activateAt   = basket.averagePrice + DirectionMultiplier(side) * InpTrailingDistance * _Point;
   double retrace      = InpTrailingStep * _Point;

   if(side == POSITION_TYPE_BUY)
     {
      if(currentPrice >= activateAt)
        {
         if(!g_buyTrailArmed)
           {
            g_buyTrailArmed   = true;
            g_buyTrailExtreme = currentPrice;
           }
         else if(currentPrice > g_buyTrailExtreme)
            g_buyTrailExtreme = currentPrice;
        }

      if(g_buyTrailArmed && currentPrice <= g_buyTrailExtreme - retrace && basket.floatingProfit > 0.0)
         return(true);

      return(false);
     }

   if(currentPrice <= activateAt)
     {
      if(!g_sellTrailArmed)
        {
         g_sellTrailArmed   = true;
         g_sellTrailExtreme = currentPrice;
        }
      else if(currentPrice < g_sellTrailExtreme)
         g_sellTrailExtreme = currentPrice;
     }

   if(g_sellTrailArmed && currentPrice >= g_sellTrailExtreme + retrace && basket.floatingProfit > 0.0)
      return(true);

   return(false);
  }

void ResetTrailState(const ENUM_POSITION_TYPE side)
  {
   if(side == POSITION_TYPE_BUY)
     {
      g_buyTrailArmed   = false;
      g_buyTrailExtreme = 0.0;
     }
   else
     {
      g_sellTrailArmed   = false;
      g_sellTrailExtreme = 0.0;
     }
  }

bool OpenPosition(const ENUM_POSITION_TYPE side, const double volume, const string tag)
  {
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
      return(false);

   double sl   = CalculateStopLoss(side);
   string note = StringFormat("ZONE_ND_%s_%s", SideToText(side), tag);
   bool sent   = false;

   g_trade.SetExpertMagicNumber(InpMagicNumber);
   g_trade.SetTypeFillingBySymbol(_Symbol);

   if(side == POSITION_TYPE_BUY)
      sent = g_trade.Buy(volume, _Symbol, 0.0, sl, 0.0, note);
   else
      sent = g_trade.Sell(volume, _Symbol, 0.0, sl, 0.0, note);

   if(!sent)
      PrintFormat("OpenPosition failed for %s %.2f. retcode=%d comment=%s",
                  SideToText(side), volume, g_trade.ResultRetcode(), g_trade.ResultComment());

   return(sent);
  }

double CalculateStopLoss(const ENUM_POSITION_TYPE side)
  {
   return(NormalizeDouble(GetZoneStopLoss(side), (int)_Digits));
  }

bool CloseBasket(const ENUM_POSITION_TYPE side)
  {
   bool allClosed = true;

   for(int index = PositionsTotal() - 1; index >= 0; index--)
     {
      ulong ticket = PositionGetTicket(index);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;

      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;

      if((long)PositionGetInteger(POSITION_MAGIC) != InpMagicNumber)
         continue;

      if((ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE) != side)
         continue;

      if(!g_trade.PositionClose(ticket))
        {
         PrintFormat("Failed to close ticket %I64u. retcode=%d comment=%s",
                     ticket, g_trade.ResultRetcode(), g_trade.ResultComment());
         allClosed = false;
        }
     }

   if(allClosed)
      ResetTrailState(side);

   return(allClosed);
  }

bool ManageRiskCutLoss()
  {
   if(InpCutLossFullCloseAt <= 0.0)
      return(false);

   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   if(balance <= 0.0)
      return(false);

   double totalLoss = 0.0;

   for(int index = PositionsTotal() - 1; index >= 0; index--)
     {
      ulong ticket = PositionGetTicket(index);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;

      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;

      if((long)PositionGetInteger(POSITION_MAGIC) != InpMagicNumber)
         continue;

      double profit = PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
      if(profit < 0.0)
         totalLoss += -profit;
     }

   if((totalLoss / balance) * 100.0 < InpCutLossFullCloseAt)
      return(false);

   CloseAllEaPositions();
   return(true);
  }

void CloseAllEaPositions()
  {
   for(int index = PositionsTotal() - 1; index >= 0; index--)
     {
      ulong ticket = PositionGetTicket(index);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;

      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;

      if((long)PositionGetInteger(POSITION_MAGIC) != InpMagicNumber)
         continue;

      g_trade.PositionClose(ticket);
     }

   ResetTrailState(POSITION_TYPE_BUY);
   ResetTrailState(POSITION_TYPE_SELL);
  }

void BlockSide(const ENUM_POSITION_TYPE side, const string reason)
  {
   if(side == POSITION_TYPE_BUY)
     {
      if(g_buyBlocked && g_buyBlockReason == reason)
         return;

      g_buyBlocked     = true;
      g_buyBlockReason = reason;
      g_buyBlockedAt   = TimeCurrent();
     }
   else
     {
      if(g_sellBlocked && g_sellBlockReason == reason)
         return;

      g_sellBlocked     = true;
      g_sellBlockReason = reason;
      g_sellBlockedAt   = TimeCurrent();
     }

   Print(SideToText(side), " side blocked: ", reason);
  }

bool IsSideBlocked(const ENUM_POSITION_TYPE side)
  {
   return(side == POSITION_TYPE_BUY ? g_buyBlocked : g_sellBlocked);
  }

ENUM_ZONE_MODE GetZoneMode(const ENUM_POSITION_TYPE side)
  {
   return(side == POSITION_TYPE_BUY ? g_dynBuyMode : g_dynSellMode);
  }

double GetZoneLow(const ENUM_POSITION_TYPE side)
  {
   return(side == POSITION_TYPE_BUY ? g_dynBuyLow : g_dynSellLow);
  }

double GetZoneHigh(const ENUM_POSITION_TYPE side)
  {
   return(side == POSITION_TYPE_BUY ? g_dynBuyHigh : g_dynSellHigh);
  }

double GetZoneStopLoss(const ENUM_POSITION_TYPE side)
  {
   return(side == POSITION_TYPE_BUY ? g_dynBuySL : g_dynSellSL);
  }

bool HasConfiguredZone(const ENUM_POSITION_TYPE side)
  {
   ENUM_ZONE_MODE mode = GetZoneMode(side);
   if(mode == ZONE_OFF)
      return(false);

   double low  = GetZoneLow(side);
   double high = GetZoneHigh(side);
   if(low <= 0.0 || high <= 0.0)
      return(false);

   if(mode == ZONE_TRADE && GetZoneStopLoss(side) <= 0.0)
      return(false);

   return(true);
  }

bool IsInsideTradeZone(const ENUM_POSITION_TYPE side, const double price)
  {
   if(!HasConfiguredZone(side))
      return(false);

   double low  = MathMin(GetZoneLow(side), GetZoneHigh(side));
   double high = MathMax(GetZoneLow(side), GetZoneHigh(side));
   return(price >= low && price <= high);
  }

bool IsZoneStopHit(const ENUM_POSITION_TYPE side, const double price)
  {
   if(GetZoneMode(side) != ZONE_TRADE)
      return(false);

   double stopLoss = GetZoneStopLoss(side);
   if(stopLoss <= 0.0)
      return(false);

   if(side == POSITION_TYPE_BUY)
      return(price <= stopLoss);

   return(price >= stopLoss);
  }

double NormalizeVolume(const double requested)
  {
   double minVol  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxVol  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double stepVol = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double volume  = MathMax(requested, minVol);

   volume = MathMin(volume, maxVol);

   if(stepVol > 0.0)
      volume = MathFloor(volume / stepVol + 0.0000001) * stepVol;

   return(NormalizeDouble(volume, VolumeDigits(stepVol)));
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

double DirectionMultiplier(const ENUM_POSITION_TYPE side)
  {
   return(side == POSITION_TYPE_BUY ? 1.0 : -1.0);
  }

double GetSideOpenPrice(const ENUM_POSITION_TYPE side, const MqlTick &tick)
  {
   return(side == POSITION_TYPE_BUY ? tick.ask : tick.bid);
  }

double GetSideClosePrice(const ENUM_POSITION_TYPE side, const MqlTick &tick)
  {
   return(side == POSITION_TYPE_BUY ? tick.bid : tick.ask);
  }

double MidPrice(const MqlTick &tick)
  {
   return((tick.bid + tick.ask) / 2.0);
  }

string SideToText(const ENUM_POSITION_TYPE side)
  {
   return(side == POSITION_TYPE_BUY ? "BUY" : "SELL");
  }

string ZoneModeText(const ENUM_ZONE_MODE mode)
  {
   if(mode == ZONE_TRADE)
      return("TRADE");

   if(mode == ZONE_WATCH)
      return("WATCH");

   return("OFF");
  }

string SideStatusText(const ENUM_POSITION_TYPE side)
  {
   if(IsSideBlocked(side))
      return("BLOCKED");

   if(GetZoneMode(side) == ZONE_TRADE)
      return("READY");

   if(GetZoneMode(side) == ZONE_WATCH)
      return("WATCHING");

   return("OFF");
  }

string SideReasonText(const ENUM_POSITION_TYPE side)
  {
   if(!IsSideBlocked(side))
      return("-");

   return(side == POSITION_TYPE_BUY ? g_buyBlockReason : g_sellBlockReason);
  }

string OverallStatusText()
  {
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED))
      return("Disabled");

   if(g_buyBlocked && g_sellBlocked)
      return("Buy/Sell Blocked");

   if(g_buyBlocked)
      return("Buy Blocked");

   if(g_sellBlocked)
      return("Sell Blocked");

   return("Trading");
  }

string ZoneRangeText(const ENUM_POSITION_TYPE side)
  {
   if(!HasConfiguredZone(side))
      return("OFF");

   return(StringFormat("%.2f - %.2f",
                       MathMin(GetZoneLow(side), GetZoneHigh(side)),
                       MathMax(GetZoneLow(side), GetZoneHigh(side))));
  }

string ZoneStopText(const ENUM_POSITION_TYPE side)
  {
   if(GetZoneMode(side) != ZONE_TRADE || GetZoneStopLoss(side) <= 0.0)
      return("-");

   return(DoubleToString(GetZoneStopLoss(side), (int)_Digits));
  }

void CreatePanel()
  {
   string bg = g_panelPrefix + "_BG";
   string title = g_panelPrefix + "_TITLE";

   if(ObjectFind(0, bg) == -1)
      ObjectCreate(0, bg, OBJ_RECTANGLE_LABEL, 0, 0, 0);

   ObjectSetInteger(0, bg, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, bg, OBJPROP_XDISTANCE, 12);
   ObjectSetInteger(0, bg, OBJPROP_YDISTANCE, 30);
   ObjectSetInteger(0, bg, OBJPROP_XSIZE, 220);
   ObjectSetInteger(0, bg, OBJPROP_YSIZE, 450);
   ObjectSetInteger(0, bg, OBJPROP_BGCOLOR, clrWhiteSmoke);
   ObjectSetInteger(0, bg, OBJPROP_COLOR, clrBlack);
   ObjectSetInteger(0, bg, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, bg, OBJPROP_HIDDEN, true);

   if(ObjectFind(0, title) == -1)
      ObjectCreate(0, title, OBJ_LABEL, 0, 0, 0);

   ObjectSetInteger(0, title, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, title, OBJPROP_XDISTANCE, 58);
   ObjectSetInteger(0, title, OBJPROP_YDISTANCE, 38);
   ObjectSetInteger(0, title, OBJPROP_COLOR, clrBlack);
   ObjectSetInteger(0, title, OBJPROP_FONTSIZE, 14);
   ObjectSetString(0, title, OBJPROP_FONT, "Tahoma Bold");
   ObjectSetString(0, title, OBJPROP_TEXT, "EA Zone NeverDie");
   ObjectSetInteger(0, title, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, title, OBJPROP_HIDDEN, true);

   for(int index = 0; index < PANEL_LINE_COUNT; index++)
      CreatePanelLine(index);
  }

void CreatePanelLine(const int index)
  {
   string name = PanelLineName(index);

   if(ObjectFind(0, name) == -1)
      ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);

   ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, 22);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, 76 + index * PANEL_LINE_HEIGHT);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clrBlack);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 9);
   ObjectSetString(0, name, OBJPROP_FONT, "Tahoma");
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
  }

void RemovePanel()
  {
   ObjectDelete(0, g_panelPrefix + "_BG");
   ObjectDelete(0, g_panelPrefix + "_TITLE");

   for(int index = 0; index < PANEL_LINE_COUNT; index++)
      ObjectDelete(0, PanelLineName(index));
  }

void UpdatePanel()
  {
   if(!InpShowPanel)
     {
      RemovePanel();
      return;
     }

   if(ObjectFind(0, g_panelPrefix + "_BG") == -1)
      CreatePanel();

   string lines[];
   color colors[];
   ArrayResize(lines, PANEL_LINE_COUNT);
   ArrayResize(colors, PANEL_LINE_COUNT);

   for(int index = 0; index < PANEL_LINE_COUNT; index++)
     {
      lines[index] = "";
      colors[index] = clrBlack;
     }

   double balance      = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity       = AccountInfoDouble(ACCOUNT_EQUITY);
   double totalProfit  = equity - balance;
   double todayProfit  = ClosedProfitFrom(StartOfDay(), TimeCurrent());
   double weekProfit   = ClosedProfitFrom(StartOfWeek(), TimeCurrent());
   double monthProfit  = ClosedProfitFrom(StartOfMonth(), TimeCurrent());
   double symbolProfit = CurrentSymbolFloatingProfit();
   double symbolVolume = CurrentSymbolVolume();
   int    symbolOrders = CurrentSymbolOrders();
   double ddPercent    = CurrentSymbolDrawdownPercent(balance);

   int row = 0;
   AddPanelRow(lines, colors, row, "---- Account Data ----", clrDimGray);
   AddPanelRow(lines, colors, row, "Balance: " + DoubleToString(balance, 2), clrBlack);
   AddPanelRow(lines, colors, row, "Equity:  " + DoubleToString(equity, 2), clrBlack);
   AddPanelRow(lines, colors, row, "Profit:  " + DoubleToString(totalProfit, 2), ProfitColor(totalProfit));
   AddPanelRow(lines, colors, row, "", clrBlack);

   AddPanelRow(lines, colors, row, "------- Profit -------", clrDimGray);
   AddPanelRow(lines, colors, row, "Today:   " + DoubleToString(todayProfit, 2), ProfitColor(todayProfit));
   AddPanelRow(lines, colors, row, "Week:    " + DoubleToString(weekProfit, 2), ProfitColor(weekProfit));
   AddPanelRow(lines, colors, row, "Month:   " + DoubleToString(monthProfit, 2), ProfitColor(monthProfit));
   AddPanelRow(lines, colors, row, "", clrBlack);

   AddPanelRow(lines, colors, row, "------ " + _Symbol + " ------", clrDimGray);
   AddPanelRow(lines, colors, row, "Profit:  " + DoubleToString(symbolProfit, 2), ProfitColor(symbolProfit));
   AddPanelRow(lines, colors, row, "DD:      " + DoubleToString(ddPercent, 1) + "%", clrBlack);
   AddPanelRow(lines, colors, row, "Volume:  " + DoubleToString(symbolVolume, 2), clrBlack);
   AddPanelRow(lines, colors, row, "Orders:  " + IntegerToString(symbolOrders), clrBlack);
   AddPanelRow(lines, colors, row, "", clrBlack);

   AddPanelRow(lines, colors, row, "------- Status -------", clrDimGray);
   AddPanelRow(lines, colors, row, "Buy Mode:  " + ZoneModeText(GetZoneMode(POSITION_TYPE_BUY)),
               ModeColor(GetZoneMode(POSITION_TYPE_BUY)));
   AddPanelRow(lines, colors, row, "Buy Zone:  " + ZoneRangeText(POSITION_TYPE_BUY), clrBlack);
   AddPanelRow(lines, colors, row, "Buy SL:    " + ZoneStopText(POSITION_TYPE_BUY), clrBlack);
   AddPanelRow(lines, colors, row, "Buy State: " + SideStatusText(POSITION_TYPE_BUY), StateColor(POSITION_TYPE_BUY));
   AddPanelRow(lines, colors, row, "Buy Note:  " + SideReasonText(POSITION_TYPE_BUY), clrBlack);
   AddPanelRow(lines, colors, row, "Sell Mode: " + ZoneModeText(GetZoneMode(POSITION_TYPE_SELL)),
               ModeColor(GetZoneMode(POSITION_TYPE_SELL)));
   AddPanelRow(lines, colors, row, "Sell Zone: " + ZoneRangeText(POSITION_TYPE_SELL), clrBlack);
   AddPanelRow(lines, colors, row, "Sell SL:   " + ZoneStopText(POSITION_TYPE_SELL), clrBlack);
   AddPanelRow(lines, colors, row, "Sell State:" + " " + SideStatusText(POSITION_TYPE_SELL), StateColor(POSITION_TYPE_SELL));
   AddPanelRow(lines, colors, row, "Sell Note: " + SideReasonText(POSITION_TYPE_SELL), clrBlack);
   AddPanelRow(lines, colors, row, "Status:    " + OverallStatusText(), clrDarkGreen);

   for(int index = 0; index < PANEL_LINE_COUNT; index++)
      SetPanelLine(index, lines[index], colors[index]);
  }

void AddPanelRow(string &lines[], color &colors[], int &row, const string text, const color lineColor)
  {
   if(row >= ArraySize(lines))
      return;

   lines[row] = text;
   colors[row] = lineColor;
   row++;
  }

void SetPanelLine(const int index, const string text, const color lineColor)
  {
   string name = PanelLineName(index);
   if(ObjectFind(0, name) == -1)
      CreatePanelLine(index);

   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetInteger(0, name, OBJPROP_COLOR, lineColor);
  }

string PanelLineName(const int index)
  {
   return(g_panelPrefix + "_LINE_" + IntegerToString(index));
  }

color ProfitColor(const double value)
  {
   if(value > 0.0)
      return(clrForestGreen);

   if(value < 0.0)
      return(clrTomato);

   return(clrBlack);
  }

color ModeColor(const ENUM_ZONE_MODE mode)
  {
   if(mode == ZONE_TRADE)
      return(clrDodgerBlue);

   if(mode == ZONE_WATCH)
      return(clrDarkOrange);

   return(clrGray);
  }

color StateColor(const ENUM_POSITION_TYPE side)
  {
   if(IsSideBlocked(side))
      return(clrTomato);

   if(GetZoneMode(side) == ZONE_TRADE)
      return(clrForestGreen);

   if(GetZoneMode(side) == ZONE_WATCH)
      return(clrDarkOrange);

   return(clrGray);
  }

double ClosedProfitFrom(const datetime fromTime, const datetime toTime)
  {
   if(!HistorySelect(fromTime, toTime))
      return(0.0);

   double profit = 0.0;
   int total = HistoryDealsTotal();

   for(int index = 0; index < total; index++)
     {
      ulong deal = HistoryDealGetTicket(index);
      if(deal == 0)
         continue;

      long entry = HistoryDealGetInteger(deal, DEAL_ENTRY);
      if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY && entry != DEAL_ENTRY_INOUT)
         continue;

      profit += HistoryDealGetDouble(deal, DEAL_PROFIT);
      profit += HistoryDealGetDouble(deal, DEAL_SWAP);
      profit += HistoryDealGetDouble(deal, DEAL_COMMISSION);
     }

   return(profit);
  }

double CurrentSymbolFloatingProfit()
  {
   double total = 0.0;

   for(int index = PositionsTotal() - 1; index >= 0; index--)
     {
      ulong ticket = PositionGetTicket(index);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;

      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;

      if((long)PositionGetInteger(POSITION_MAGIC) != InpMagicNumber)
         continue;

      total += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
     }

   return(total);
  }

double CurrentSymbolVolume()
  {
   double total = 0.0;

   for(int index = PositionsTotal() - 1; index >= 0; index--)
     {
      ulong ticket = PositionGetTicket(index);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;

      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;

      if((long)PositionGetInteger(POSITION_MAGIC) != InpMagicNumber)
         continue;

      total += PositionGetDouble(POSITION_VOLUME);
     }

   return(total);
  }

int CurrentSymbolOrders()
  {
   int total = 0;

   for(int index = PositionsTotal() - 1; index >= 0; index--)
     {
      ulong ticket = PositionGetTicket(index);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;

      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;

      if((long)PositionGetInteger(POSITION_MAGIC) != InpMagicNumber)
         continue;

      total++;
     }

   return(total);
  }

double CurrentSymbolDrawdownPercent(const double balance)
  {
   if(balance <= 0.0)
      return(0.0);

   double loss = 0.0;

   for(int index = PositionsTotal() - 1; index >= 0; index--)
     {
      ulong ticket = PositionGetTicket(index);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;

      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;

      if((long)PositionGetInteger(POSITION_MAGIC) != InpMagicNumber)
         continue;

      double profit = PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
      if(profit < 0.0)
         loss += -profit;
     }

   return((loss / balance) * 100.0);
  }

datetime StartOfDay()
  {
   MqlDateTime value;
   TimeToStruct(TimeCurrent(), value);
   value.hour = 0;
   value.min  = 0;
   value.sec  = 0;
   return(StructToTime(value));
  }

datetime StartOfWeek()
  {
   MqlDateTime value;
   TimeToStruct(TimeCurrent(), value);
   value.hour = 0;
   value.min  = 0;
   value.sec  = 0;
   datetime today = StructToTime(value);
   int offset = (value.day_of_week == 0) ? 6 : (value.day_of_week - 1);
   return(today - offset * 86400);
  }

datetime StartOfMonth()
  {
   MqlDateTime value;
   TimeToStruct(TimeCurrent(), value);
   value.day  = 1;
   value.hour = 0;
   value.min  = 0;
   value.sec  = 0;
   return(StructToTime(value));
  }
