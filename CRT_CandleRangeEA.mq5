//+------------------------------------------------------------------+
//|                                           CRT_CandleRangeEA.mq5 |
//|                    Candle Range Theory (CRT) Expert Advisor     |
//|  Strategy inspired by @aftercrt1 CRT model:                     |
//|  HTF candle range -> liquidity sweep one side -> target opposite |
//+------------------------------------------------------------------+
#property copyright "CRT Candle Range EA"
#property link      ""
#property version   "1.00"

#include <Trade/Trade.mqh>
#include <Trade/PositionInfo.mqh>
#include <Trade/SymbolInfo.mqh>

//--- Inputs
input group "=== Core CRT ==="
input ENUM_TIMEFRAMES InpHTF              = PERIOD_H4;     // Higher timeframe (CRT range)
input ENUM_TIMEFRAMES InpLTF              = PERIOD_M15;    // Entry timeframe (chart should match)
input int             InpCRTShift         = 1;             // Closed HTF candle used as CRT (1=last closed)
input bool            InpRequireSweep     = true;          // Require liquidity sweep beyond CRT extreme
input double          InpMinSweepPoints   = 0.0;           // Min sweep beyond CRT high/low (points)
input double          InpMaxSweepPoints   = 500.0;         // Max sweep beyond CRT extreme (0=off)
input bool            InpRequireCloseBack = true;          // Sweep candle must close back inside CRT
input bool            InpTradeBothSides   = true;          // Trade both bullish & bearish CRT
input bool            InpAllowBuy         = true;          // Allow buy setups
input bool            InpAllowSell        = true;          // Allow sell setups

input group "=== Sweep Candle Filters ==="
input double          InpMinWickBodyRatio = 1.2;           // Sweep wick / body ratio
input double          InpMaxBodyATR       = 1.5;           // Max body size as ATR multiple (0=off)
input int             InpATRPeriod        = 14;            // ATR period on LTF
input int             InpLookbackBars     = 8;             // Bars after CRT open to look for sweep

input group "=== Entry Timing ==="
input bool            InpEnterOnSweepClose= true;          // Enter at close of sweep candle
input bool            InpUseLimitAtExtreme= false;         // Or place limit at CRT extreme after sweep
input int             InpSignalExpireBars = 6;             // Cancel pending/signal after N LTF bars
input bool            InpOneTradePerCRT   = true;          // Only one trade per CRT candle

input group "=== Risk / Targets ==="
input double          InpLots             = 0.10;          // Fixed lot size (if risk%=0)
input double          InpRiskPercent      = 0.5;           // Risk % of balance (0=use fixed lots)
input double          InpSLBufferPoints   = 20.0;          // SL buffer beyond sweep extreme
input int             InpTPMode           = 1;             // 1=opposite CRT, 2=R multiple, 3=both(min)
input double          InpRR               = 2.0;           // Reward:Risk if TPMode uses R
input double          InpPartialTPPercent = 50.0;          // Close % at mid-range (0=off)
input bool            InpMoveBEAtPartial  = true;          // Move SL to BE after partial
input double          InpBEOffsetPoints   = 5.0;           // BE offset in points

input group "=== Session / Filters ==="
input bool            InpUseSessionFilter = false;         // Restrict trading hours (server time)
input int             InpSessionStartHour = 7;             // Session start hour
input int             InpSessionEndHour   = 20;            // Session end hour
input bool            InpOnlyNewBar       = true;          // Process once per new LTF bar
input int             InpMaxSpreadPoints  = 40;            // Max spread (points)
input int             InpMagic            = 18072026;      // Magic number
input string          InpComment          = "CRT_EA";      // Order comment
input bool            InpDrawObjects      = true;          // Draw CRT / trade levels
input bool            InpVerboseLog       = true;          // Print detailed logs

//--- Globals
CTrade         g_trade;
CPositionInfo  g_pos;
CSymbolInfo    g_sym;

int      g_atrHandle = INVALID_HANDLE;
datetime g_lastLtfBar = 0;
datetime g_lastSignalBar = 0;
datetime g_activeCrtTime = 0;
ulong    g_partialTicket = 0;
bool     g_tradedThisCrt = false;
bool     g_partialDone = false;

struct CRTRange
{
   datetime time;
   double   high;
   double   low;
   double   open;
   double   close;
   bool     bullish;
   bool     valid;
};

struct CRTSignal
{
   bool     active;
   int      direction;      // +1 buy, -1 sell
   datetime crtTime;
   datetime signalTime;
   double   crtHigh;
   double   crtLow;
   double   sweepExtreme;
   double   entry;
   double   sl;
   double   tp;
   int      barsAlive;
};

CRTRange  g_crt;
CRTSignal g_signal;

//+------------------------------------------------------------------+
int OnInit()
{
   if(!g_sym.Name(_Symbol))
   {
      Print("Failed to init symbol ", _Symbol);
      return INIT_FAILED;
   }
   g_sym.Refresh();

   g_atrHandle = iATR(_Symbol, InpLTF, InpATRPeriod);
   if(g_atrHandle == INVALID_HANDLE)
   {
      Print("Failed to create ATR handle");
      return INIT_FAILED;
   }

   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints(20);
   g_trade.SetTypeFillingBySymbol(_Symbol);

   if(Period() != InpLTF && InpVerboseLog)
      Print("Warning: chart TF (", EnumToString(Period()),
            ") differs from InpLTF (", EnumToString(InpLTF), "). Signals use InpLTF.");

   ZeroMemory(g_crt);
   ZeroMemory(g_signal);

   if(InpVerboseLog)
      Print("CRT EA initialized | HTF=", EnumToString(InpHTF),
            " LTF=", EnumToString(InpLTF));

   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   if(g_atrHandle != INVALID_HANDLE)
   {
      IndicatorRelease(g_atrHandle);
      g_atrHandle = INVALID_HANDLE;
   }
   if(InpDrawObjects)
      ObjectsDeleteAll(0, "CRT_");
}

//+------------------------------------------------------------------+
void OnTick()
{
   g_sym.RefreshRates();

   if(InpOnlyNewBar)
   {
      datetime t = iTime(_Symbol, InpLTF, 0);
      if(t == 0 || t == g_lastLtfBar)
         return;
      g_lastLtfBar = t;
   }

   if(!IsSpreadOk())
      return;

   if(InpUseSessionFilter && !IsInSession())
      return;

   UpdateCRT();
   ManageOpenTrade();

   if(PositionsByMagic() > 0 || HasPendingByMagic())
      return;

   if(InpOneTradePerCRT && g_tradedThisCrt && g_crt.time == g_activeCrtTime)
      return;

   ScanForSweepSignal();
   TryExecuteSignal();
}

//+------------------------------------------------------------------+
bool IsSpreadOk()
{
   int spread = (int)SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);
   if(spread > InpMaxSpreadPoints)
   {
      if(InpVerboseLog)
         Print("Spread too high: ", spread);
      return false;
   }
   return true;
}

//+------------------------------------------------------------------+
bool IsInSession()
{
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   int h = dt.hour;
   if(InpSessionStartHour <= InpSessionEndHour)
      return (h >= InpSessionStartHour && h < InpSessionEndHour);
   return (h >= InpSessionStartHour || h < InpSessionEndHour);
}

//+------------------------------------------------------------------+
int PositionsByMagic()
{
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; --i)
   {
      if(!g_pos.SelectByIndex(i))
         continue;
      if(g_pos.Symbol() == _Symbol && g_pos.Magic() == InpMagic)
         count++;
   }
   return count;
}

//+------------------------------------------------------------------+
bool HasPendingByMagic()
{
   for(int i = OrdersTotal() - 1; i >= 0; --i)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0)
         continue;
      if(OrderGetString(ORDER_SYMBOL) != _Symbol)
         continue;
      if((long)OrderGetInteger(ORDER_MAGIC) != InpMagic)
         continue;
      return true;
   }
   return false;
}

//+------------------------------------------------------------------+
void UpdateCRT()
{
   CRTRange crt;
   if(!LoadCRT(crt))
      return;

   if(crt.time != g_crt.time)
   {
      g_crt = crt;
      g_activeCrtTime = crt.time;
      g_tradedThisCrt = false;
      g_partialDone = false;
      g_partialTicket = 0;
      g_lastSignalBar = 0;
      ZeroMemory(g_signal);

      if(InpVerboseLog)
         PrintFormat("New CRT @ %s | H=%.5f L=%.5f %s",
                     TimeToString(crt.time, TIME_DATE|TIME_MINUTES),
                     crt.high, crt.low,
                     crt.bullish ? "BULL" : "BEAR");

      DrawCRT(crt);
   }
}

//+------------------------------------------------------------------+
bool LoadCRT(CRTRange &crt)
{
   ZeroMemory(crt);
   int shift = MathMax(1, InpCRTShift);

   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   if(CopyRates(_Symbol, InpHTF, shift, 1, rates) < 1)
      return false;

   crt.time    = rates[0].time;
   crt.high    = rates[0].high;
   crt.low     = rates[0].low;
   crt.open    = rates[0].open;
   crt.close   = rates[0].close;
   crt.bullish = (crt.close >= crt.open);
   crt.valid   = (crt.high > crt.low);
   return crt.valid;
}

//+------------------------------------------------------------------+
double PointValue()
{
   return _Point;
}

//+------------------------------------------------------------------+
double ATR(const int shift=1)
{
   if(g_atrHandle == INVALID_HANDLE)
      return 0.0;
   double buf[];
   ArraySetAsSeries(buf, true);
   if(CopyBuffer(g_atrHandle, 0, shift, 1, buf) < 1)
      return 0.0;
   return buf[0];
}

//+------------------------------------------------------------------+
bool CandleIsSweepBuy(const MqlRates &bar, const CRTRange &crt, double &extreme)
{
   // Bullish CRT model: sweep CRT LOW then reverse up toward CRT HIGH
   double wickLow = MathMin(bar.open, bar.close) - bar.low;
   double body    = MathAbs(bar.close - bar.open);
   if(body <= 0.0)
      body = PointValue();

   bool swept = (bar.low < crt.low - InpMinSweepPoints * PointValue());
   if(InpRequireSweep && !swept)
      return false;
   if(!InpRequireSweep && bar.low > crt.low)
      return false;

   double beyond = crt.low - bar.low;
   if(beyond < 0.0)
      beyond = 0.0;
   if(InpRequireSweep && beyond < InpMinSweepPoints * PointValue())
      return false;
   if(InpMaxSweepPoints > 0.0 && beyond > InpMaxSweepPoints * PointValue())
      return false;

   if(InpRequireCloseBack && bar.close < crt.low)
      return false;

   if(InpRequireSweep && wickLow / body < InpMinWickBodyRatio)
      return false;

   if(InpMaxBodyATR > 0.0)
   {
      double atr = ATR(1);
      if(atr > 0.0 && body > atr * InpMaxBodyATR)
         return false;
   }

   extreme = bar.low;
   return true;
}

//+------------------------------------------------------------------+
bool CandleIsSweepSell(const MqlRates &bar, const CRTRange &crt, double &extreme)
{
   // Bearish CRT model: sweep CRT HIGH then reverse down toward CRT LOW
   double wickHigh = bar.high - MathMax(bar.open, bar.close);
   double body     = MathAbs(bar.close - bar.open);
   if(body <= 0.0)
      body = PointValue();

   bool swept = (bar.high > crt.high + InpMinSweepPoints * PointValue());
   if(InpRequireSweep && !swept)
      return false;
   if(!InpRequireSweep && bar.high < crt.high)
      return false;

   double beyond = bar.high - crt.high;
   if(beyond < 0.0)
      beyond = 0.0;
   if(InpRequireSweep && beyond < InpMinSweepPoints * PointValue())
      return false;
   if(InpMaxSweepPoints > 0.0 && beyond > InpMaxSweepPoints * PointValue())
      return false;

   if(InpRequireCloseBack && bar.close > crt.high)
      return false;

   if(InpRequireSweep && wickHigh / body < InpMinWickBodyRatio)
      return false;

   if(InpMaxBodyATR > 0.0)
   {
      double atr = ATR(1);
      if(atr > 0.0 && body > atr * InpMaxBodyATR)
         return false;
   }

   extreme = bar.high;
   return true;
}

//+------------------------------------------------------------------+
void ScanForSweepSignal()
{
   if(!g_crt.valid)
      return;

   if(g_signal.active)
   {
      g_signal.barsAlive++;
      if(g_signal.barsAlive > InpSignalExpireBars)
      {
         if(InpVerboseLog)
            Print("Signal expired");
         ZeroMemory(g_signal);
      }
      else
         return;
   }

   // Map CRT time onto LTF bars: search recent closed LTF bars
   MqlRates ltf[];
   ArraySetAsSeries(ltf, true);
   int need = InpLookbackBars + 2;
   if(CopyRates(_Symbol, InpLTF, 1, need, ltf) < 2)
      return;

   for(int i = 0; i < InpLookbackBars; ++i)
   {
      // Only consider LTF bars at/after CRT open; skip already used signal bars
      if(ltf[i].time < g_crt.time)
         continue;
      if(ltf[i].time <= g_lastSignalBar)
         continue;

      double extreme = 0.0;

      // Classic CRT in the clip: after a bullish CRT candle, look for high sweep -> sell;
      // after a bearish CRT candle, look for low sweep -> buy.
      // TradeBothSides=true allows either direction on any CRT.
      bool tryBuy  = InpAllowBuy  && (InpTradeBothSides || !g_crt.bullish);
      bool trySell = InpAllowSell && (InpTradeBothSides || g_crt.bullish);

      if(tryBuy && CandleIsSweepBuy(ltf[i], g_crt, extreme))
      {
         BuildSignal(+1, ltf[i], extreme);
         return;
      }
      if(trySell && CandleIsSweepSell(ltf[i], g_crt, extreme))
      {
         BuildSignal(-1, ltf[i], extreme);
         return;
      }
   }
}

//+------------------------------------------------------------------+
void BuildSignal(const int direction, const MqlRates &bar, const double extreme)
{
   ZeroMemory(g_signal);
   g_signal.active      = true;
   g_signal.direction   = direction;
   g_signal.crtTime     = g_crt.time;
   g_signal.signalTime  = bar.time;
   g_signal.crtHigh     = g_crt.high;
   g_signal.crtLow      = g_crt.low;
   g_signal.sweepExtreme= extreme;
   g_signal.barsAlive   = 0;
   g_lastSignalBar      = bar.time;

   double buffer = InpSLBufferPoints * PointValue();
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);

   if(direction > 0)
   {
      g_signal.entry = InpUseLimitAtExtreme ? g_crt.low : ask;
      g_signal.sl    = extreme - buffer;
      g_signal.tp    = CalcTP(+1, g_signal.entry, g_signal.sl, g_crt.high);
   }
   else
   {
      g_signal.entry = InpUseLimitAtExtreme ? g_crt.high : bid;
      g_signal.sl    = extreme + buffer;
      g_signal.tp    = CalcTP(-1, g_signal.entry, g_signal.sl, g_crt.low);
   }

   NormalizeLevels(g_signal.entry, g_signal.sl, g_signal.tp);

   if(InpVerboseLog)
      PrintFormat("CRT signal %s | entry=%.5f SL=%.5f TP=%.5f | CRT[%s] H=%.5f L=%.5f sweep=%.5f",
                  direction > 0 ? "BUY" : "SELL",
                  g_signal.entry, g_signal.sl, g_signal.tp,
                  TimeToString(g_crt.time, TIME_DATE|TIME_MINUTES),
                  g_crt.high, g_crt.low, extreme);

   DrawSignal(g_signal);
}

//+------------------------------------------------------------------+
double CalcTP(const int direction, const double entry, const double sl, const double opposite)
{
   double risk = MathAbs(entry - sl);
   double tpR  = (direction > 0) ? entry + risk * InpRR : entry - risk * InpRR;
   double tpCRT= opposite;

   if(InpTPMode == 2)
      return tpR;
   if(InpTPMode == 3)
   {
      // Take the nearer of CRT opposite and R-multiple (more conservative)
      if(direction > 0)
         return MathMin(tpCRT, tpR);
      return MathMax(tpCRT, tpR);
   }
   // default mode 1: opposite CRT extreme
   return tpCRT;
}

//+------------------------------------------------------------------+
void NormalizeLevels(double &entry, double &sl, double &tp)
{
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   entry = NormalizeDouble(entry, digits);
   sl    = NormalizeDouble(sl, digits);
   tp    = NormalizeDouble(tp, digits);
}

//+------------------------------------------------------------------+
double CalcLot(const double entry, const double sl)
{
   if(InpRiskPercent <= 0.0)
      return NormalizeLot(InpLots);

   double riskMoney = AccountInfoDouble(ACCOUNT_BALANCE) * InpRiskPercent / 100.0;
   double tickSize  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   double tickValue = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   if(tickSize <= 0.0 || tickValue <= 0.0)
      return NormalizeLot(InpLots);

   double stopDist = MathAbs(entry - sl);
   if(stopDist <= 0.0)
      return NormalizeLot(InpLots);

   double lossPerLot = (stopDist / tickSize) * tickValue;
   if(lossPerLot <= 0.0)
      return NormalizeLot(InpLots);

   return NormalizeLot(riskMoney / lossPerLot);
}

//+------------------------------------------------------------------+
double NormalizeLot(double lots)
{
   double minLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double step    = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0.0)
      step = 0.01;

   lots = MathFloor(lots / step) * step;
   lots = MathMax(minLot, MathMin(maxLot, lots));
   return NormalizeDouble(lots, 2);
}

//+------------------------------------------------------------------+
void TryExecuteSignal()
{
   if(!g_signal.active)
      return;

   // Validate RR / distance
   if(g_signal.direction > 0 && !(g_signal.tp > g_signal.entry && g_signal.sl < g_signal.entry))
   {
      if(InpVerboseLog) Print("Invalid buy levels, dropping signal");
      ZeroMemory(g_signal);
      return;
   }
   if(g_signal.direction < 0 && !(g_signal.tp < g_signal.entry && g_signal.sl > g_signal.entry))
   {
      if(InpVerboseLog) Print("Invalid sell levels, dropping signal");
      ZeroMemory(g_signal);
      return;
   }

   double lots = CalcLot(g_signal.entry, g_signal.sl);
   bool ok = false;

   if(InpUseLimitAtExtreme)
   {
      if(g_signal.direction > 0)
         ok = g_trade.BuyLimit(lots, g_signal.entry, _Symbol, g_signal.sl, g_signal.tp,
                               ORDER_TIME_GTC, 0, InpComment);
      else
         ok = g_trade.SellLimit(lots, g_signal.entry, _Symbol, g_signal.sl, g_signal.tp,
                                ORDER_TIME_GTC, 0, InpComment);
   }
   else
   {
      if(!InpEnterOnSweepClose)
         return;

      if(g_signal.direction > 0)
         ok = g_trade.Buy(lots, _Symbol, 0.0, g_signal.sl, g_signal.tp, InpComment);
      else
         ok = g_trade.Sell(lots, _Symbol, 0.0, g_signal.sl, g_signal.tp, InpComment);
   }

   if(ok)
   {
      g_tradedThisCrt = true;
      g_activeCrtTime = g_signal.crtTime;
      g_partialDone = false;
      if(InpVerboseLog)
         Print("Order sent: ", g_trade.ResultRetcodeDescription());
      ZeroMemory(g_signal);
   }
   else if(InpVerboseLog)
   {
      Print("Order failed: ", g_trade.ResultRetcode(), " ", g_trade.ResultRetcodeDescription());
   }
}

//+------------------------------------------------------------------+
void ManageOpenTrade()
{
   if(InpPartialTPPercent <= 0.0)
      return;

   for(int i = PositionsTotal() - 1; i >= 0; --i)
   {
      if(!g_pos.SelectByIndex(i))
         continue;
      if(g_pos.Symbol() != _Symbol || g_pos.Magic() != InpMagic)
         continue;

      ulong ticket = g_pos.Ticket();
      if(g_partialDone && g_partialTicket == ticket)
         continue;

      double entry = g_pos.PriceOpen();
      double sl    = g_pos.StopLoss();
      double tp    = g_pos.TakeProfit();
      double vol   = g_pos.Volume();
      long   type  = g_pos.PositionType();

      // Mid of CRT range as partial target when we still know CRT
      double mid = (g_crt.high + g_crt.low) * 0.5;
      if(!g_crt.valid)
         continue;

      double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      bool hit = false;

      if(type == POSITION_TYPE_BUY && bid >= mid)
         hit = true;
      if(type == POSITION_TYPE_SELL && ask <= mid)
         hit = true;

      if(!hit)
         continue;

      double closeVol = NormalizeLot(vol * InpPartialTPPercent / 100.0);
      if(closeVol < SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN))
         continue;
      if(closeVol >= vol)
         closeVol = NormalizeLot(vol - SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN));
      if(closeVol <= 0.0)
         continue;

      if(g_trade.PositionClosePartial(ticket, closeVol))
      {
         g_partialDone = true;
         g_partialTicket = ticket;
         if(InpVerboseLog)
            Print("Partial TP closed ", closeVol, " lots at mid CRT");

         if(InpMoveBEAtPartial)
         {
            double be = entry;
            double offset = InpBEOffsetPoints * PointValue();
            if(type == POSITION_TYPE_BUY)
               be = entry + offset;
            else
               be = entry - offset;
            be = NormalizeDouble(be, (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS));
            g_trade.PositionModify(ticket, be, tp);
         }
      }
   }
}

//+------------------------------------------------------------------+
void DrawCRT(const CRTRange &crt)
{
   if(!InpDrawObjects)
      return;

   string prefix = "CRT_" + TimeToString(crt.time, TIME_DATE|TIME_MINUTES);
   ObjectDelete(0, prefix + "_H");
   ObjectDelete(0, prefix + "_L");
   ObjectDelete(0, prefix + "_BOX");

   datetime t1 = crt.time;
   datetime t2 = crt.time + (datetime)PeriodSeconds(InpHTF) * 3;

   ObjectCreate(0, prefix + "_H", OBJ_TREND, 0, t1, crt.high, t2, crt.high);
   ObjectSetInteger(0, prefix + "_H", OBJPROP_COLOR, clrDodgerBlue);
   ObjectSetInteger(0, prefix + "_H", OBJPROP_WIDTH, 2);
   ObjectSetInteger(0, prefix + "_H", OBJPROP_RAY_RIGHT, true);
   ObjectSetString(0, prefix + "_H", OBJPROP_TEXT, "CRT HIGH");

   ObjectCreate(0, prefix + "_L", OBJ_TREND, 0, t1, crt.low, t2, crt.low);
   ObjectSetInteger(0, prefix + "_L", OBJPROP_COLOR, clrOrangeRed);
   ObjectSetInteger(0, prefix + "_L", OBJPROP_WIDTH, 2);
   ObjectSetInteger(0, prefix + "_L", OBJPROP_RAY_RIGHT, true);
   ObjectSetString(0, prefix + "_L", OBJPROP_TEXT, "CRT LOW");

   ObjectCreate(0, prefix + "_BOX", OBJ_RECTANGLE, 0, t1, crt.high, t2, crt.low);
   ObjectSetInteger(0, prefix + "_BOX", OBJPROP_COLOR, clrDimGray);
   ObjectSetInteger(0, prefix + "_BOX", OBJPROP_FILL, true);
   ObjectSetInteger(0, prefix + "_BOX", OBJPROP_BACK, true);
   ObjectSetInteger(0, prefix + "_BOX", OBJPROP_STYLE, STYLE_DOT);
}

//+------------------------------------------------------------------+
void DrawSignal(const CRTSignal &sig)
{
   if(!InpDrawObjects || !sig.active)
      return;

   string prefix = "CRT_SIG_" + IntegerToString((int)sig.signalTime);
   ObjectDelete(0, prefix + "_E");
   ObjectDelete(0, prefix + "_SL");
   ObjectDelete(0, prefix + "_TP");

   datetime t1 = sig.signalTime;
   datetime t2 = t1 + (datetime)PeriodSeconds(InpLTF) * 10;

   ObjectCreate(0, prefix + "_E", OBJ_TREND, 0, t1, sig.entry, t2, sig.entry);
   ObjectSetInteger(0, prefix + "_E", OBJPROP_COLOR, clrYellow);
   ObjectSetInteger(0, prefix + "_E", OBJPROP_WIDTH, 1);

   ObjectCreate(0, prefix + "_SL", OBJ_TREND, 0, t1, sig.sl, t2, sig.sl);
   ObjectSetInteger(0, prefix + "_SL", OBJPROP_COLOR, clrRed);
   ObjectSetInteger(0, prefix + "_SL", OBJPROP_WIDTH, 1);

   ObjectCreate(0, prefix + "_TP", OBJ_TREND, 0, t1, sig.tp, t2, sig.tp);
   ObjectSetInteger(0, prefix + "_TP", OBJPROP_COLOR, clrLime);
   ObjectSetInteger(0, prefix + "_TP", OBJPROP_WIDTH, 1);
}

//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction& trans,
                        const MqlTradeRequest& request,
                        const MqlTradeResult& result)
{
   // Keep for future expansion / journal hooks
}

//+------------------------------------------------------------------+
