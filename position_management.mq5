#property strict
#property description "Position Management EA (TP-to-entry rules)"

// Manages open positions (by symbol + magic) with rules:
// - When position opens: log entry, tp, sl, open time, comment
// - Within first 5 minutes: if price goes -0.5R adverse, move TP to entry (except comment plan_chinh__*)
// - After 30 minutes (except plan_chinh__*): if +0.5R favorable => close; if negative => move TP to entry

input group "=== SCOPE ==="
input string InpSymbol = "";                 // empty = current chart symbol
input long   InpMagicNumber = 0;             // 0 = manage any magic

input group "=== RULES ==="
input int    InpFirstMinutesWindow = 5;      // minutes
input double InpFirstWindowAdverseR = 0.5;   // 0.5R adverse => move TP to entry
input int    InpSecondMinutesWindow = 30;    // minutes
input double InpSecondWindowPositiveR = 0.5; // +0.5R favorable => close

input group "=== LOGGING ==="
input bool   InpDebugLog = true;
input string InpLogFileName = "position_management_log.csv";

// ----------------------------
// Utilities
// ----------------------------
string BoolText(const bool value) { return(value ? "true" : "false"); }

void DebugLog(const string message)
  {
   if(!InpDebugLog) return;
   Print("[PM_EA] ", message);
  }

string ManagedSymbol()
  {
   if(StringLen(InpSymbol) > 0) return(InpSymbol);
   return(_Symbol);
  }

bool IsPlanChinhComment(const string comment)
  {
   string value = comment;
   StringToLower(value);
   return(StringFind(value, "plan_chinh__") == 0);
  }

double NormalizePrice(const string symbol, const double value)
  {
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   return(NormalizeDouble(value, digits));
  }

// ----------------------------
// File logging
// ----------------------------
bool EnsureLogHeader()
  {
   int handle = FileOpen(InpLogFileName, FILE_READ | FILE_WRITE | FILE_CSV | FILE_ANSI);
   if(handle == INVALID_HANDLE)
     {
      DebugLog("Failed to open log file for header. err=" + IntegerToString(GetLastError()));
      return(false);
     }

   if(FileSize(handle) == 0)
     {
      FileWrite(handle,
                "time_logged",
                "ticket",
                "symbol",
                "type",
                "volume",
                "open_time",
                "entry",
                "sl",
                "tp",
                "comment");
     }
   FileClose(handle);
   return(true);
  }

void AppendPositionLog(const ulong ticket,
                       const string symbol,
                       const ENUM_POSITION_TYPE type,
                       const double volume,
                       const datetime openTime,
                       const double entry,
                       const double sl,
                       const double tp,
                       const string comment)
  {
   int handle = FileOpen(InpLogFileName, FILE_READ | FILE_WRITE | FILE_CSV | FILE_ANSI);
   if(handle == INVALID_HANDLE)
     {
      DebugLog("Failed to open log file for append. err=" + IntegerToString(GetLastError()));
      return;
     }

   FileSeek(handle, 0, SEEK_END);
   FileWrite(handle,
             TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS),
             (long)ticket,
             symbol,
             EnumToString(type),
             DoubleToString(volume, 2),
             TimeToString(openTime, TIME_DATE | TIME_SECONDS),
             DoubleToString(entry, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)),
             DoubleToString(sl, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)),
             DoubleToString(tp, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)),
             comment);
   FileClose(handle);
  }

// ----------------------------
// Trade ops (hedging-safe via requests)
// ----------------------------
bool ModifyPositionSLTP(const ulong ticket, const string symbol, const double sl, const double tp)
  {
   MqlTradeRequest req;
   MqlTradeResult res;
   ZeroMemory(req);
   ZeroMemory(res);

   req.action   = TRADE_ACTION_SLTP;
   req.symbol   = symbol;
   req.position = ticket;
   req.sl       = (sl > 0.0 ? NormalizePrice(symbol, sl) : 0.0);
   req.tp       = (tp > 0.0 ? NormalizePrice(symbol, tp) : 0.0);

   ResetLastError();
   bool ok = OrderSend(req, res);
   if(!ok)
     {
      DebugLog("Modify SLTP failed. ticket=" + IntegerToString((long)ticket) + " err=" + IntegerToString(GetLastError()));
      return(false);
     }
   if(res.retcode != TRADE_RETCODE_DONE && res.retcode != TRADE_RETCODE_DONE_PARTIAL)
     {
      DebugLog("Modify SLTP rejected. ticket=" + IntegerToString((long)ticket) + " retcode=" + IntegerToString((long)res.retcode) + " comment=" + res.comment);
      return(false);
     }

   DebugLog("Modify SLTP ok. ticket=" + IntegerToString((long)ticket) +
            " sl=" + DoubleToString(req.sl, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)) +
            " tp=" + DoubleToString(req.tp, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)));
   return(true);
  }

bool ClosePositionByTicket(const ulong ticket, const string symbol, const ENUM_POSITION_TYPE type, const double volume)
  {
   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick))
     {
      DebugLog("Close failed: no tick. symbol=" + symbol);
      return(false);
     }

   MqlTradeRequest req;
   MqlTradeResult res;
   ZeroMemory(req);
   ZeroMemory(res);

   req.action   = TRADE_ACTION_DEAL;
   req.symbol   = symbol;
   req.position = ticket;
   req.volume   = volume;
   req.deviation = 30;

   if(type == POSITION_TYPE_BUY)
     {
      req.type  = ORDER_TYPE_SELL;
      req.price = tick.bid;
     }
   else
     {
      req.type  = ORDER_TYPE_BUY;
      req.price = tick.ask;
     }

   ResetLastError();
   bool ok = OrderSend(req, res);
   if(!ok)
     {
      DebugLog("Close failed. ticket=" + IntegerToString((long)ticket) + " err=" + IntegerToString(GetLastError()));
      return(false);
     }
   if(res.retcode != TRADE_RETCODE_DONE && res.retcode != TRADE_RETCODE_DONE_PARTIAL)
     {
      DebugLog("Close rejected. ticket=" + IntegerToString((long)ticket) + " retcode=" + IntegerToString((long)res.retcode) + " comment=" + res.comment);
      return(false);
     }

   DebugLog("Close ok. ticket=" + IntegerToString((long)ticket) + " deal=" + IntegerToString((long)res.deal));
   return(true);
  }

// ----------------------------
// State: track which tickets have been logged
// ----------------------------
ulong g_loggedTickets[];

bool IsTicketLogged(const ulong ticket)
  {
   for(int i = 0; i < ArraySize(g_loggedTickets); i++)
      if(g_loggedTickets[i] == ticket) return(true);
   return(false);
  }

void MarkTicketLogged(const ulong ticket)
  {
   if(IsTicketLogged(ticket)) return;
   int n = ArraySize(g_loggedTickets);
   ArrayResize(g_loggedTickets, n + 1);
   g_loggedTickets[n] = ticket;
  }

// ----------------------------
// R calculations
// ----------------------------
bool ComputeR(const double entry, const double sl, double &rDistance)
  {
   if(sl <= 0.0) return(false);
   rDistance = MathAbs(entry - sl);
   return(rDistance > 0.0);
  }

double CurrentMoveInR(const string symbol, const ENUM_POSITION_TYPE type, const double entry, const double rDistance)
  {
   if(rDistance <= 0.0) return(0.0);
   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick)) return(0.0);
   double price = (type == POSITION_TYPE_BUY ? tick.bid : tick.ask);
   double move = (type == POSITION_TYPE_BUY ? (price - entry) : (entry - price));
   return(move / rDistance);
  }

bool IsAdverseMoveReached(const string symbol, const ENUM_POSITION_TYPE type, const double entry, const double rDistance, const double adverseR)
  {
   if(rDistance <= 0.0) return(false);
   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick)) return(false);

   if(type == POSITION_TYPE_BUY)
      return(tick.bid <= entry - adverseR * rDistance);
   return(tick.ask >= entry + adverseR * rDistance);
  }

// ----------------------------
// Core manager
// ----------------------------
void ManagePositions()
  {
   string symbol = ManagedSymbol();

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(!PositionSelectByTicket(ticket)) continue;

      string posSymbol = PositionGetString(POSITION_SYMBOL);
      if(posSymbol != symbol) continue;

      long magic = PositionGetInteger(POSITION_MAGIC);
      if(InpMagicNumber != 0 && magic != InpMagicNumber) continue;

      ENUM_POSITION_TYPE type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      datetime openTime = (datetime)PositionGetInteger(POSITION_TIME);
      double entry = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl = PositionGetDouble(POSITION_SL);
      double tp = PositionGetDouble(POSITION_TP);
      double volume = PositionGetDouble(POSITION_VOLUME);
      string comment = PositionGetString(POSITION_COMMENT);

      // Log once per ticket
      if(!IsTicketLogged(ticket))
        {
         AppendPositionLog(ticket, posSymbol, type, volume, openTime, entry, sl, tp, comment);
         MarkTicketLogged(ticket);
        }

      bool isPlanChinh = IsPlanChinhComment(comment);
      datetime now = TimeCurrent();
      int ageSeconds = (int)(now - openTime);

      double rDistance = 0.0;
      bool hasR = ComputeR(entry, sl, rDistance);
      if(!hasR) continue; // can't apply R-based rules without SL

      // Rule 1: first 5 minutes, adverse -0.5R => move TP to entry (except plan_chinh__*)
      if(!isPlanChinh && ageSeconds >= 0 && ageSeconds <= InpFirstMinutesWindow * 60)
        {
         if(IsAdverseMoveReached(posSymbol, type, entry, rDistance, InpFirstWindowAdverseR))
           {
            double newTp = NormalizePrice(posSymbol, entry);
            if(tp != newTp)
              {
               DebugLog("Rule1 hit: move TP to entry. ticket=" + IntegerToString((long)ticket) +
                        " ageSec=" + IntegerToString(ageSeconds) +
                        " comment=" + comment);
               ModifyPositionSLTP(ticket, posSymbol, sl, newTp);
              }
           }
        }

      // Rule 2: after 30 minutes (except plan_chinh__*)
      if(!isPlanChinh && ageSeconds >= InpSecondMinutesWindow * 60)
        {
         double moveR = CurrentMoveInR(posSymbol, type, entry, rDistance);
         if(moveR >= InpSecondWindowPositiveR)
           {
            DebugLog("Rule2 hit: +R reached => close. ticket=" + IntegerToString((long)ticket) +
                     " moveR=" + DoubleToString(moveR, 2) +
                     " comment=" + comment);
            ClosePositionByTicket(ticket, posSymbol, type, volume);
            continue;
           }

         if(moveR < 0.0)
           {
            double newTp = NormalizePrice(posSymbol, entry);
            if(tp != newTp)
              {
               DebugLog("Rule2 hit: negative => move TP to entry. ticket=" + IntegerToString((long)ticket) +
                        " moveR=" + DoubleToString(moveR, 2) +
                        " comment=" + comment);
               ModifyPositionSLTP(ticket, posSymbol, sl, newTp);
              }
           }
        }
     }
  }

int OnInit()
  {
   DebugLog("OnInit. symbol=" + ManagedSymbol() +
            " magic=" + IntegerToString(InpMagicNumber) +
            " firstWindowMin=" + IntegerToString(InpFirstMinutesWindow) +
            " secondWindowMin=" + IntegerToString(InpSecondMinutesWindow));
   EnsureLogHeader();
   EventSetTimer(1);
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   DebugLog("OnDeinit. reason=" + IntegerToString(reason));
   EventKillTimer();
  }

void OnTick()
  {
   ManagePositions();
  }

void OnTimer()
  {
   // Backup in case symbol has low ticks
   ManagePositions();
  }

