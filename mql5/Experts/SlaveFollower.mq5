// SlaveFollower.mq5
// Follow primary events from Common Files JSONL and mirror trades.

#property strict

#include <Trade/Trade.mqh>
#include "../Include/TradeCopierJson.mqh"
#include "../Include/TradeCopierState.mqh"

input string InpChannel = "default";
input long   InpMagic   = 9345021;
input double InpFixedLots = 0.10;
input int    InpPollMs = 250;
input int    InpDeviationPoints = 30;

// Symbol mapping
input string InpSymbolPrefix = "";
input string InpSymbolSuffix = "";
input string InpSymbolMapFile = ""; // Common file path, e.g. trade_copier\\default\\symbol_map.csv with lines EURUSD=EURUSD.a

// Safety / resilience
input int    InpMaxMappings = 500;
input int    InpMaxRecentEventIds = 500;
input int    InpRetryCount = 3;
input int    InpRetrySleepMs = 250;

static CTrade g_trade;
static long   g_offset = 0;
static long   g_login = 0;

static CopierMappingRow g_rows[];
static int g_rows_count = 0;

static string g_recent_ids[];
static int g_recent_count = 0;

// Snapshot reconcile tracking (bounded SNAP_BEGIN ... SNAP_END)
static long g_active_snap_id = 0;
static long g_active_primary_login = 0;
static long g_seen_pos_ids[];
static int  g_seen_pos_count = 0;
static long g_seen_ord_ids[];
static int  g_seen_ord_count = 0;

string CopierComment(const long primary_login, const long primary_position_id, const long primary_ticket)
{
   if(primary_position_id > 0)
      return "COPIER:" + (string)primary_login + ":POS:" + (string)primary_position_id;
   if(primary_ticket > 0)
      return "COPIER:" + (string)primary_login + ":ORD:" + (string)primary_ticket;
   return "COPIER:" + (string)primary_login;
}

bool RecentHas(const string id)
{
   for(int i=0;i<g_recent_count;i++)
      if(g_recent_ids[i] == id) return true;
   return false;
}

void RecentAdd(const string id)
{
   if(RecentHas(id)) return;
   if(g_recent_count >= InpMaxRecentEventIds)
   {
      for(int i=1;i<g_recent_count;i++) g_recent_ids[i-1] = g_recent_ids[i];
      g_recent_count--;
   }
   g_recent_ids[g_recent_count] = id;
   g_recent_count++;
}

bool SeenHasLong(const long &arr[], const int count, const long v)
{
   for(int i=0;i<count;i++) if(arr[i] == v) return true;
   return false;
}

void SeenAddLong(long &arr[], int &count, const int maxCount, const long v)
{
   if(v <= 0) return;
   if(SeenHasLong(arr, count, v)) return;
   if(count >= maxCount)
   {
      for(int i=1;i<count;i++) arr[i-1] = arr[i];
      count--;
   }
   arr[count] = v;
   count++;
}

string MapSymbolCsv(const string primarySymbol)
{
   if(InpSymbolMapFile == "") return "";
   int h = FileOpen(InpSymbolMapFile, FILE_READ|FILE_TXT|FILE_COMMON);
   if(h == INVALID_HANDLE) return "";
   while(!FileIsEnding(h))
   {
      string line = FileReadString(h);
      StringTrimLeft(line); StringTrimRight(line);
      if(line == "" || StringGetCharacter(line, 0) == '#') continue;
      int eq = StringFind(line, "=");
      if(eq < 0) continue;
      string k = StringSubstr(line, 0, eq);
      string v = StringSubstr(line, eq + 1);
      StringTrimLeft(k); StringTrimRight(k);
      StringTrimLeft(v); StringTrimRight(v);
      if(k == primarySymbol)
      {
         FileClose(h);
         return v;
      }
   }
   FileClose(h);
   return "";
}

string MapSymbol(const string primarySymbol)
{
   string mapped = MapSymbolCsv(primarySymbol);
   if(mapped != "") return mapped;
   return InpSymbolPrefix + primarySymbol + InpSymbolSuffix;
}

bool EnsureSymbolSelected(const string symbol)
{
   if(SymbolSelect(symbol, true)) return true;
   return false;
}

bool TradeRetryOpenMarket(const string side, const string symbol, const double lots, const double sl, const double tp, const string comment)
{
   for(int i=0;i<InpRetryCount;i++)
   {
      bool ok = false;
      if(side == "BUY") ok = g_trade.Buy(lots, symbol, 0.0, sl, tp, comment);
      else if(side == "SELL") ok = g_trade.Sell(lots, symbol, 0.0, sl, tp, comment);
      if(ok) return true;
      Sleep(InpRetrySleepMs);
   }
   return false;
}

bool TradeRetryOpenPending(const string order_type, const string side, const string symbol, const double lots, const double price, const double sl, const double tp, const string comment)
{
   for(int i=0;i<InpRetryCount;i++)
   {
      bool ok = false;
      if(order_type == "LIMIT")
      {
         if(side == "BUY") ok = g_trade.BuyLimit(lots, price, symbol, sl, tp, ORDER_TIME_GTC, 0, comment);
         else ok = g_trade.SellLimit(lots, price, symbol, sl, tp, ORDER_TIME_GTC, 0, comment);
      }
      else if(order_type == "STOP")
      {
         if(side == "BUY") ok = g_trade.BuyStop(lots, price, symbol, sl, tp, ORDER_TIME_GTC, 0, comment);
         else ok = g_trade.SellStop(lots, price, symbol, sl, tp, ORDER_TIME_GTC, 0, comment);
      }
      else if(order_type == "STOP_LIMIT")
      {
         // Not all brokers support stop-limit from MQL5; skip with log.
         ok = false;
      }
      if(ok) return true;
      Sleep(InpRetrySleepMs);
   }
   return false;
}

long FindMappedTicket(const long primary_id, const string kind)
{
   int idx = CopierFindMappingIndex(g_rows, g_rows_count, primary_id, kind);
   if(idx < 0) return 0;
   return g_rows[idx].slave_ticket;
}

void UpsertMappedTicket(const long primary_id, const long slave_ticket, const string kind)
{
   CopierUpsertMapping(g_rows, g_rows_count, primary_id, slave_ticket, kind, InpMaxMappings);
}

bool PositionIsOursByComment(const string comment)
{
   return StringFind(comment, "COPIER:") == 0;
}

bool PositionIsOursForPrimary(const string comment, const long primary_login)
{
   string prefix = "COPIER:" + (string)primary_login + ":";
   return StringFind(comment, prefix) == 0;
}

bool OrderIsOursForPrimary(const string comment, const long primary_login)
{
   string prefix = "COPIER:" + (string)primary_login + ":";
   return StringFind(comment, prefix) == 0;
}

void HandleOpen(const long primary_login, const long primary_ticket, const long primary_pos_id, const string symbolPrimary, const string side, const string order_type, const double sl, const double tp, const double price)
{
   string sym = MapSymbol(symbolPrimary);
   if(sym == "" || !EnsureSymbolSelected(sym))
   {
      Print("TradeCopier Slave: symbol not available: ", sym, " (from ", symbolPrimary, ")");
      return;
   }

   string comment = CopierComment(primary_login, primary_pos_id, primary_ticket);
   g_trade.SetDeviationInPoints(InpDeviationPoints);
   g_trade.SetExpertMagicNumber((ulong)InpMagic);

   if(order_type == "MARKET" || order_type == "" || order_type == "UNKNOWN")
   {
      bool ok = TradeRetryOpenMarket(side, sym, InpFixedLots, sl, tp, comment);
      if(!ok)
      {
         Print("TradeCopier Slave: OPEN market failed. sym=", sym, " side=", side, " err=", GetLastError());
         return;
      }

      // mapping for hedging: attempt to capture last position ticket if possible
      // Note: In netting, there is only one position per symbol.
      if(primary_pos_id > 0)
      {
         // Best-effort: record current position ticket/identifier
         if(PositionSelect(sym))
         {
            long ticket = (long)PositionGetInteger(POSITION_TICKET);
            UpsertMappedTicket(primary_pos_id, ticket, "POS");
         }
      }
      return;
   }

   // pending
   bool okp = TradeRetryOpenPending(order_type, side, sym, InpFixedLots, price, sl, tp, comment);
   if(!okp)
   {
      Print("TradeCopier Slave: OPEN pending failed. sym=", sym, " type=", order_type, " side=", side);
      return;
   }
   // store mapping by primary_ticket (pending order)
   if(primary_ticket > 0)
   {
      // Best-effort: store last order ticket (terminal last order isn't exposed; we'll scan for our comment)
      for(int i=0;i<OrdersTotal();i++)
      {
         ulong ot = OrderGetTicket(i);
         if(ot == 0) continue;
         if(!OrderSelect(ot)) continue;
         string cmt = OrderGetString(ORDER_COMMENT);
         if(cmt == comment)
         {
            long slave_ticket = (long)OrderGetInteger(ORDER_TICKET);
            UpsertMappedTicket(primary_ticket, slave_ticket, "ORD");
            break;
         }
      }
   }
}

void HandleModify(const long primary_login, const long primary_ticket, const long primary_pos_id, const string symbolPrimary, const string order_type, const double sl, const double tp, const double price)
{
   string sym = MapSymbol(symbolPrimary);
   if(sym == "" || !EnsureSymbolSelected(sym)) return;

   g_trade.SetExpertMagicNumber((ulong)InpMagic);
   g_trade.SetDeviationInPoints(InpDeviationPoints);

   if(primary_pos_id > 0)
   {
      // Position modify
      bool selected = PositionSelect(sym);
      if(!selected)
      {
         // fallback: if we have a mapped ticket, select by ticket
         long t = FindMappedTicket(primary_pos_id, "POS");
         if(t > 0) selected = PositionSelectByTicket((ulong)t);
      }
      if(!selected) return;

      bool ok = g_trade.PositionModify(sym, sl, tp);
      if(!ok)
         Print("TradeCopier Slave: MODIFY position failed. sym=", sym, " err=", GetLastError());
      return;
   }

   if(primary_ticket > 0)
   {
      // Pending modify
      long slave_ticket = FindMappedTicket(primary_ticket, "ORD");
      if(slave_ticket <= 0)
      {
         // try locate by comment prefix
         string comment = CopierComment(primary_login, 0, primary_ticket);
         for(int i=0;i<OrdersTotal();i++)
         {
            ulong ot = OrderGetTicket(i);
            if(ot == 0) continue;
            if(!OrderSelect(ot)) continue;
            if(OrderGetString(ORDER_COMMENT) == comment)
            {
               slave_ticket = (long)OrderGetInteger(ORDER_TICKET);
               UpsertMappedTicket(primary_ticket, slave_ticket, "ORD");
               break;
            }
         }
      }
      if(slave_ticket <= 0) return;
      if(!OrderSelect((ulong)slave_ticket)) return;

      // Some MT5 builds don't expose ORDER_TIME_TYPE property; use safe defaults.
      ENUM_ORDER_TYPE_TIME time_type = ORDER_TIME_GTC;
      datetime expiration = 0;
      double stoplimit = 0.0;
      bool ok = g_trade.OrderModify((ulong)slave_ticket, price, sl, tp, time_type, expiration, stoplimit);
      if(!ok)
         Print("TradeCopier Slave: MODIFY order failed. ticket=", slave_ticket, " err=", GetLastError());
      return;
   }
}

void HandleSnapshotOrd(const long primary_login, const long snap_id, const long primary_ticket, const string symbolPrimary, const string side, const string order_type, const double lots, const double price, const double sl, const double tp)
{
   if(primary_ticket <= 0) return;
   string sym = MapSymbol(symbolPrimary);
   if(sym == "" || !EnsureSymbolSelected(sym)) return;

   long slave_ticket = FindMappedTicket(primary_ticket, "ORD");
   if(slave_ticket > 0 && OrderSelect((ulong)slave_ticket))
   {
      ENUM_ORDER_TYPE_TIME time_type = ORDER_TIME_GTC;
      datetime expiration = 0;
      double stoplimit = 0.0;
      g_trade.OrderModify((ulong)slave_ticket, price, sl, tp, time_type, expiration, stoplimit);
      return;
   }

   string comment = CopierComment(primary_login, 0, primary_ticket);
   for(int i=0;i<OrdersTotal();i++)
   {
      ulong ot = OrderGetTicket(i);
      if(ot == 0) continue;
      if(!OrderSelect(ot)) continue;
      if(OrderGetString(ORDER_COMMENT) == comment)
      {
         long t = (long)OrderGetInteger(ORDER_TICKET);
         UpsertMappedTicket(primary_ticket, t, "ORD");
         ENUM_ORDER_TYPE_TIME time_type = ORDER_TIME_GTC;
         datetime expiration = 0;
         double stoplimit = 0.0;
         g_trade.OrderModify((ulong)t, price, sl, tp, time_type, expiration, stoplimit);
         return;
      }
   }

   // Create missing pending order (fixed lots).
   g_trade.SetDeviationInPoints(InpDeviationPoints);
   g_trade.SetExpertMagicNumber((ulong)InpMagic);
   bool ok = TradeRetryOpenPending(order_type, side, sym, InpFixedLots, price, sl, tp, comment);
   if(!ok) return;

   for(int i=0;i<OrdersTotal();i++)
   {
      ulong ot = OrderGetTicket(i);
      if(ot == 0) continue;
      if(!OrderSelect(ot)) continue;
      if(OrderGetString(ORDER_COMMENT) == comment)
      {
         long t = (long)OrderGetInteger(ORDER_TICKET);
         UpsertMappedTicket(primary_ticket, t, "ORD");
         return;
      }
   }
}

void SnapshotBegin(const long primary_login, const long snap_id)
{
   g_active_snap_id = snap_id;
   g_active_primary_login = primary_login;
   g_seen_pos_count = 0;
   g_seen_ord_count = 0;
}

void SnapshotEnd(const long primary_login, const long snap_id)
{
   if(g_active_snap_id != snap_id || g_active_primary_login != primary_login) return;

   // Delete copier pending orders missing from snapshot.
   for(int i=OrdersTotal()-1;i>=0;i--)
   {
      ulong ot_sel = OrderGetTicket(i);
      if(ot_sel == 0) continue;
      if(!OrderSelect(ot_sel)) continue;
      string cmt = OrderGetString(ORDER_COMMENT);
      if(!OrderIsOursForPrimary(cmt, primary_login)) continue;

      int p = StringFind(cmt, ":ORD:");
      if(p < 0) continue;
      long pt = (long)StringToInteger(StringSubstr(cmt, p + StringLen(":ORD:")));
      if(pt <= 0) continue;
      if(!SeenHasLong(g_seen_ord_ids, g_seen_ord_count, pt))
      {
         ulong ot = (ulong)OrderGetInteger(ORDER_TICKET);
         g_trade.OrderDelete(ot);
      }
   }

   // Close copier positions missing from snapshot.
   for(int i=PositionsTotal()-1;i>=0;i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(!PositionSelectByTicket(ticket)) continue;
      string cmt = PositionGetString(POSITION_COMMENT);
      if(!PositionIsOursForPrimary(cmt, primary_login)) continue;

      int p = StringFind(cmt, ":POS:");
      if(p < 0) continue;
      long pid = (long)StringToInteger(StringSubstr(cmt, p + StringLen(":POS:")));
      if(pid <= 0) continue;
      if(!SeenHasLong(g_seen_pos_ids, g_seen_pos_count, pid))
      {
         g_trade.PositionClose(ticket);
      }
   }

   g_active_snap_id = 0;
   g_active_primary_login = 0;
}

void HandleCancel(const long primary_login, const long primary_ticket, const string symbolPrimary)
{
   string sym = MapSymbol(symbolPrimary);
   if(sym != "") EnsureSymbolSelected(sym);

   g_trade.SetExpertMagicNumber((ulong)InpMagic);

   long slave_ticket = FindMappedTicket(primary_ticket, "ORD");
   if(slave_ticket <= 0)
   {
      // try locate by comment
      string comment = CopierComment(primary_login, 0, primary_ticket);
      for(int i=0;i<OrdersTotal();i++)
      {
         ulong ot = OrderGetTicket(i);
         if(ot == 0) continue;
         if(!OrderSelect(ot)) continue;
         if(OrderGetString(ORDER_COMMENT) == comment)
         {
            slave_ticket = (long)OrderGetInteger(ORDER_TICKET);
            UpsertMappedTicket(primary_ticket, slave_ticket, "ORD");
            break;
         }
      }
   }
   if(slave_ticket <= 0) return;

   bool ok = g_trade.OrderDelete((ulong)slave_ticket);
   if(!ok)
      Print("TradeCopier Slave: CANCEL failed. ticket=", slave_ticket, " err=", GetLastError());
}

void HandleCloseOrPartial(const long primary_login, const long primary_pos_id, const string symbolPrimary, const string type, const double closed_lots, const double remaining_lots)
{
   string sym = MapSymbol(symbolPrimary);
   if(sym == "" || !EnsureSymbolSelected(sym)) return;

   g_trade.SetDeviationInPoints(InpDeviationPoints);
   g_trade.SetExpertMagicNumber((ulong)InpMagic);

   // Attempt hedging-first close by mapped ticket
   long mapped = (primary_pos_id > 0) ? FindMappedTicket(primary_pos_id, "POS") : 0;
   if(mapped > 0)
   {
      if(!PositionSelectByTicket((ulong)mapped))
      {
         // stale mapping
         mapped = 0;
      }
   }

   if(mapped > 0)
   {
      if(type == "PARTIAL" && closed_lots > 0.0)
      {
         bool ok = g_trade.PositionClosePartial((ulong)mapped, closed_lots);
         if(!ok)
            Print("TradeCopier Slave: PARTIAL close (hedging) failed. ticket=", mapped, " err=", GetLastError());
      }
      else
      {
         bool ok = g_trade.PositionClose((ulong)mapped);
         if(!ok)
            Print("TradeCopier Slave: CLOSE (hedging) failed. ticket=", mapped, " err=", GetLastError());
      }
      return;
   }

   // Netting fallback: operate on symbol position
   if(!PositionSelect(sym)) return;

   double cur = PositionGetDouble(POSITION_VOLUME);
   if(type == "PARTIAL" && closed_lots > 0.0 && closed_lots < cur)
   {
      bool ok = g_trade.PositionClosePartial(sym, closed_lots);
      if(!ok)
         Print("TradeCopier Slave: PARTIAL close (netting) failed. sym=", sym, " err=", GetLastError());
      return;
   }

   bool okc = g_trade.PositionClose(sym);
   if(!okc)
      Print("TradeCopier Slave: CLOSE (netting) failed. sym=", sym, " err=", GetLastError());
}

void HandleSnapshotPos(const long primary_login, const long snap_id, const long primary_pos_id, const string symbolPrimary, const string side, const double lots, const double sl, const double tp)
{
   // Reconcile minimal: if we don't have our position for this primary_pos_id, open one.
   if(primary_pos_id <= 0) return;

   if(g_active_snap_id == snap_id && g_active_primary_login == primary_login)
      SeenAddLong(g_seen_pos_ids, g_seen_pos_count, 2000, primary_pos_id);

   string sym = MapSymbol(symbolPrimary);
   if(sym == "" || !EnsureSymbolSelected(sym)) return;

   long mapped = FindMappedTicket(primary_pos_id, "POS");
   if(mapped > 0 && PositionSelectByTicket((ulong)mapped))
   {
      // ensure SL/TP
      g_trade.PositionModify(sym, sl, tp);
      return;
   }

   // try find by comment tag
   string comment = CopierComment(primary_login, primary_pos_id, 0);
   bool found = false;
   for(int i=0;i<PositionsTotal();i++)
   {
      ulong pt = PositionGetTicket(i);
      if(!PositionSelectByTicket(pt)) continue;
      if(PositionGetString(POSITION_SYMBOL) != sym) continue;
      if(PositionGetString(POSITION_COMMENT) == comment)
      {
         long ticket = (long)PositionGetInteger(POSITION_TICKET);
         UpsertMappedTicket(primary_pos_id, ticket, "POS");
         found = true;
         g_trade.PositionModify(sym, sl, tp);
         break;
      }
   }
   if(found) return;

   // If primary has an open position and we don't, create it (fixed lots).
   g_trade.SetDeviationInPoints(InpDeviationPoints);
   g_trade.SetExpertMagicNumber((ulong)InpMagic);
   bool ok = TradeRetryOpenMarket(side, sym, InpFixedLots, sl, tp, comment);
   if(!ok) return;
   if(PositionSelect(sym))
   {
      long ticket = (long)PositionGetInteger(POSITION_TICKET);
      UpsertMappedTicket(primary_pos_id, ticket, "POS");
   }
}

void ProcessEventLine(const string line)
{
   string event_id, type, symbol, side, order_type;
   long primary_login=0, primary_ticket=0, primary_pos_id=0;
   double sl=0.0, tp=0.0, price=0.0, closed_lots=0.0, remaining_lots=0.0, lots=0.0;
   long snap_id=0;

   if(!JsonTryGetString(line, "event_id", event_id)) return;
   if(RecentHas(event_id)) return;
   RecentAdd(event_id);

   JsonTryGetString(line, "type", type);
   JsonTryGetLong(line, "primary_login", primary_login);
   JsonTryGetString(line, "symbol", symbol);
   JsonTryGetString(line, "side", side);
   JsonTryGetString(line, "order_type", order_type);
   JsonTryGetLong(line, "primary_ticket", primary_ticket);
   JsonTryGetLong(line, "primary_position_id", primary_pos_id);

   JsonTryGetDouble(line, "sl", sl);
   JsonTryGetDouble(line, "tp", tp);
   JsonTryGetDouble(line, "price", price);
   JsonTryGetDouble(line, "lots", lots);
   JsonTryGetDouble(line, "closed_lots", closed_lots);
   JsonTryGetDouble(line, "remaining_lots", remaining_lots);
   JsonTryGetLong(line, "snap_id", snap_id);

   if(type == "SNAP_BEGIN")
   {
      SnapshotBegin(primary_login, snap_id);
      return;
   }

   if(type == "OPEN")
   {
      HandleOpen(primary_login, primary_ticket, primary_pos_id, symbol, side, order_type, sl, tp, price);
      return;
   }
   if(type == "MODIFY")
   {
      HandleModify(primary_login, primary_ticket, primary_pos_id, symbol, order_type, sl, tp, price);
      return;
   }
   if(type == "CANCEL")
   {
      HandleCancel(primary_login, primary_ticket, symbol);
      return;
   }
   if(type == "CLOSE" || type == "PARTIAL")
   {
      HandleCloseOrPartial(primary_login, primary_pos_id, symbol, type, closed_lots, remaining_lots);
      return;
   }
   if(type == "SNAP_POS")
   {
      HandleSnapshotPos(primary_login, snap_id, primary_pos_id, symbol, side, lots, sl, tp);
      return;
   }

   if(type == "SNAP_ORD")
   {
      if(g_active_snap_id == snap_id && g_active_primary_login == primary_login)
         SeenAddLong(g_seen_ord_ids, g_seen_ord_count, 2000, primary_ticket);
      HandleSnapshotOrd(primary_login, snap_id, primary_ticket, symbol, side, order_type, lots, price, sl, tp);
      return;
   }

   if(type == "SNAP_END")
   {
      SnapshotEnd(primary_login, snap_id);
      return;
   }
}

void PollEvents()
{
   const string path = CopierEventsPath(InpChannel);
   int h = FileOpen(path, FILE_READ|FILE_TXT|FILE_COMMON);
   if(h == INVALID_HANDLE) return;

   // Clamp offset: if file truncated, reset.
   long size = (long)FileSize(h);
   if(g_offset > size) g_offset = 0;

   FileSeek(h, g_offset, SEEK_SET);
   while(!FileIsEnding(h))
   {
      long line_start = (long)FileTell(h);
      string line = FileReadString(h);
      // FileReadString on FILE_TXT reads one line; if it returns empty but not ending, continue.
      if(line == "" && !FileIsEnding(h)) continue;
      ProcessEventLine(line);
      g_offset = (long)FileTell(h);

      // Safety: prevent tight loops on huge backlog
      if((g_offset - line_start) <= 0) break;
   }
   FileClose(h);
}

int OnInit()
{
   g_login = (long)AccountInfoInteger(ACCOUNT_LOGIN);
   ArrayResize(g_rows, InpMaxMappings);
   ArrayResize(g_recent_ids, InpMaxRecentEventIds);
   ArrayResize(g_seen_pos_ids, 2000);
   ArrayResize(g_seen_ord_ids, 2000);

   CopierEnsureCommonDir(InpChannel);
   CopierLoadOffset(InpChannel, g_login, g_offset);

   g_trade.SetDeviationInPoints(InpDeviationPoints);
   g_trade.SetExpertMagicNumber((ulong)InpMagic);

   EventSetMillisecondTimer(InpPollMs);
   Print("TradeCopier SlaveFollower started. channel=", InpChannel, " login=", g_login);
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   CopierSaveOffsetAndMappings(InpChannel, g_login, g_offset, g_rows, g_rows_count);
}

void OnTimer()
{
   PollEvents();
   // Persist occasionally (cheap, but avoid writing every tick)
   static long last_save_ms = 0;
   long now_ms = (long)GetTickCount64();
   if(last_save_ms == 0 || (now_ms - last_save_ms) > 2000)
   {
      last_save_ms = now_ms;
      CopierSaveOffsetAndMappings(InpChannel, g_login, g_offset, g_rows, g_rows_count);
   }
}

