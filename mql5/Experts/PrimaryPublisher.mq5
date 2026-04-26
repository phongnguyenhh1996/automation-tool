// PrimaryPublisher.mq5
// Publish primary trading events to Common Files as JSONL.

#property strict

#include <Trade/Trade.mqh>
#include "../Include/TradeCopierJson.mqh"
#include "../Include/TradeCopierState.mqh"

input string InpChannel = "default";
input long   InpMagic   = 9345021;
input int    InpSnapshotSeconds = 10;   // write periodic snapshot events
input int    InpDebounceMs = 200;        // basic debounce for rapid-fire modify/trailing
input string InpWriterTag = "PrimaryPublisher@repo"; // helps identify which EA writes the file

static datetime g_last_snapshot = 0;
static long     g_last_modify_pos_id = 0;
static long     g_last_modify_ms = 0;

long TsMs()
{
   // Avoid implicit datetime arithmetic causing malformed JSON on some builds.
   return ((long)TimeLocal()) * 1000L;
}

void PublishHello()
{
   // Emit a signature line so we can verify the active .ex5 really matches this source.
   const string hello = StringFormat(
      "{\"event_id\":\"%s\",\"ts_ms\":%I64d,\"primary_login\":%I64d,\"type\":\"HELLO\",\"writer_tag\":\"%s\",\"channel\":\"%s\",\"magic\":%I64d}",
      (NowMsStr()),
      TsMs(),
      (long)AccountInfoInteger(ACCOUNT_LOGIN),
      (InpWriterTag),
      (InpChannel),
      (long)InpMagic
   );
   AppendEventLine(hello);
}

string NowMsStr()
{
   // TimeLocal() is seconds; use GetTickCount64 for ms uniqueness.
   return (string)TimeLocal() + "_" + (string)(long)GetTickCount64();
}

long NowMs()
{
   return (long)GetTickCount64();
}

string EventBase(
   const string type,
   const string symbol,
   const string side,
   const string order_type,
   const long primary_ticket,
   const long primary_position_id
)
{
   const long login = (long)AccountInfoInteger(ACCOUNT_LOGIN);
   const long ts_ms = TsMs();

   string json = "{";
   json += "\"event_id\":\"" + NowMsStr() + "\",";
   json += "\"ts_ms\":" + (string)ts_ms + ",";
   json += "\"primary_login\":" + (string)login + ",";
   json += "\"type\":\"" + type + "\",";
   json += "\"symbol\":\"" + symbol + "\",";
   json += "\"side\":\"" + side + "\",";
   json += "\"order_type\":\"" + order_type + "\",";
   json += "\"magic\":" + (string)InpMagic + ",";
   json += "\"primary_ticket\":" + (string)primary_ticket + ",";
   json += "\"primary_position_id\":" + (string)primary_position_id;
   return json;
}

bool AppendEventLine(const string line)
{
   if(!CopierEnsureCommonDir(InpChannel))
   {
      Print("TradeCopier: failed to ensure common dir for channel=", InpChannel, " err=", GetLastError());
      return false;
   }

   const string path = CopierEventsPath(InpChannel);
   int h = FileOpen(path, FILE_READ|FILE_WRITE|FILE_TXT|FILE_COMMON);
   if(h == INVALID_HANDLE)
   {
      Print("TradeCopier: failed to open events file=", path, " err=", GetLastError());
      return false;
   }
   FileSeek(h, 0, SEEK_END);
   FileWriteString(h, line + "\n");
   FileClose(h);
   return true;
}

string SideFromType(const ENUM_DEAL_TYPE dealType)
{
   if(dealType == DEAL_TYPE_BUY || dealType == DEAL_TYPE_BUY_CANCELED) return "BUY";
   if(dealType == DEAL_TYPE_SELL || dealType == DEAL_TYPE_SELL_CANCELED) return "SELL";
   return "UNKNOWN";
}

string OrderTypeFromOrder(const ENUM_ORDER_TYPE t)
{
   if(t == ORDER_TYPE_BUY || t == ORDER_TYPE_SELL) return "MARKET";
   if(t == ORDER_TYPE_BUY_LIMIT || t == ORDER_TYPE_SELL_LIMIT) return "LIMIT";
   if(t == ORDER_TYPE_BUY_STOP || t == ORDER_TYPE_SELL_STOP) return "STOP";
   if(t == ORDER_TYPE_BUY_STOP_LIMIT || t == ORDER_TYPE_SELL_STOP_LIMIT) return "STOP_LIMIT";
   return "UNKNOWN";
}

void PublishSnapshot()
{
   // Snapshot is emitted as a bounded sequence to keep parsing simple:
   // SNAP_BEGIN, SNAP_POS (one per position), SNAP_ORD (one per order), SNAP_END
   const long snap_id = (long)GetTickCount64();
   const string snap_begin = StringFormat(
      "{\"event_id\":\"%s\",\"ts_ms\":%I64d,\"primary_login\":%I64d,\"type\":\"SNAP_BEGIN\",\"snap_id\":%I64d}",
      NowMsStr(),
      TsMs(),
      (long)AccountInfoInteger(ACCOUNT_LOGIN),
      snap_id
   );
   AppendEventLine(snap_begin);

   for(int i=0;i<PositionsTotal();i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(!PositionSelectByTicket(ticket)) continue;
      string symbol = PositionGetString(POSITION_SYMBOL);
      long   pos_id = (long)PositionGetInteger(POSITION_IDENTIFIER);
      double vol    = PositionGetDouble(POSITION_VOLUME);
      double price  = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl     = PositionGetDouble(POSITION_SL);
      double tp     = PositionGetDouble(POSITION_TP);
      long   ptype  = (long)PositionGetInteger(POSITION_TYPE);
      string side   = (ptype == POSITION_TYPE_BUY ? "BUY" : "SELL");

      string j = "{";
      j += "\"event_id\":\"" + NowMsStr() + "\",";
      j += "\"ts_ms\":" + (string)TsMs() + ",";
      j += "\"primary_login\":" + (string)AccountInfoInteger(ACCOUNT_LOGIN) + ",";
      j += "\"type\":\"SNAP_POS\",";
      j += "\"snap_id\":" + (string)snap_id + ",";
      j += "\"symbol\":\"" + symbol + "\",";
      j += "\"side\":\"" + side + "\",";
      j += "\"lots\":" + DoubleToString(vol, 2) + ",";
      j += "\"price\":" + DoubleToString(price, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)) + ",";
      j += "\"sl\":" + DoubleToString(sl, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)) + ",";
      j += "\"tp\":" + DoubleToString(tp, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)) + ",";
      j += "\"magic\":" + (string)InpMagic + ",";
      j += "\"primary_position_id\":" + (string)pos_id;
      j += "}";
      AppendEventLine(j);
   }

   for(int i=0;i<OrdersTotal();i++)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0) continue;
      if(!OrderSelect(ticket)) continue;
      long state = (long)OrderGetInteger(ORDER_STATE);
      if(state != ORDER_STATE_PLACED && state != ORDER_STATE_PARTIAL) continue;

      string symbol = OrderGetString(ORDER_SYMBOL);
      ENUM_ORDER_TYPE t = (ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
      string orderType = OrderTypeFromOrder(t);
      string side = (t == ORDER_TYPE_BUY || t == ORDER_TYPE_BUY_LIMIT || t == ORDER_TYPE_BUY_STOP || t == ORDER_TYPE_BUY_STOP_LIMIT) ? "BUY" : "SELL";

      double vol = OrderGetDouble(ORDER_VOLUME_CURRENT);
      double price = OrderGetDouble(ORDER_PRICE_OPEN);
      double sl = OrderGetDouble(ORDER_SL);
      double tp = OrderGetDouble(ORDER_TP);

      string j = "{";
      j += "\"event_id\":\"" + NowMsStr() + "\",";
      j += "\"ts_ms\":" + (string)TsMs() + ",";
      j += "\"primary_login\":" + (string)AccountInfoInteger(ACCOUNT_LOGIN) + ",";
      j += "\"type\":\"SNAP_ORD\",";
      j += "\"snap_id\":" + (string)snap_id + ",";
      j += "\"symbol\":\"" + symbol + "\",";
      j += "\"side\":\"" + side + "\",";
      j += "\"order_type\":\"" + orderType + "\",";
      j += "\"lots\":" + DoubleToString(vol, 2) + ",";
      j += "\"price\":" + DoubleToString(price, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)) + ",";
      j += "\"sl\":" + DoubleToString(sl, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)) + ",";
      j += "\"tp\":" + DoubleToString(tp, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)) + ",";
      j += "\"magic\":" + (string)InpMagic + ",";
      j += "\"primary_ticket\":" + (string)(long)ticket;
      j += "}";
      AppendEventLine(j);
   }

   const string snap_end = StringFormat(
      "{\"event_id\":\"%s\",\"ts_ms\":%I64d,\"primary_login\":%I64d,\"type\":\"SNAP_END\",\"snap_id\":%I64d}",
      NowMsStr(),
      TsMs(),
      (long)AccountInfoInteger(ACCOUNT_LOGIN),
      snap_id
   );
   AppendEventLine(snap_end);
}

int OnInit()
{
   CopierEnsureCommonDir(InpChannel);
   if(InpSnapshotSeconds > 0)
      EventSetTimer(1);
   PublishHello();
   Print("TradeCopier PrimaryPublisher started. channel=", InpChannel, " login=", AccountInfoInteger(ACCOUNT_LOGIN));
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTimer()
{
   if(InpSnapshotSeconds <= 0) return;
   datetime now = TimeLocal();
   if(g_last_snapshot == 0 || (now - g_last_snapshot) >= InpSnapshotSeconds)
   {
      g_last_snapshot = now;
      PublishSnapshot();
   }
}

void OnTradeTransaction(
   const MqlTradeTransaction& trans,
   const MqlTradeRequest& request,
   const MqlTradeResult& result
)
{
   // Focus on deals/orders that materially change exposure or risk controls.
   // We'll derive events from the transaction types and query current state from terminal.

   // 1) Deals: open/close/partial
   if(trans.type == TRADE_TRANSACTION_DEAL_ADD)
   {
      if(trans.deal == 0) return;
      if(!HistoryDealSelect(trans.deal)) return;

      ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
      ENUM_DEAL_TYPE  dtype = (ENUM_DEAL_TYPE)HistoryDealGetInteger(trans.deal, DEAL_TYPE);
      string symbol = HistoryDealGetString(trans.deal, DEAL_SYMBOL);
      double vol = HistoryDealGetDouble(trans.deal, DEAL_VOLUME);
      double price = HistoryDealGetDouble(trans.deal, DEAL_PRICE);
      long pos_id = (long)HistoryDealGetInteger(trans.deal, DEAL_POSITION_ID);
      long order_ticket = (long)HistoryDealGetInteger(trans.deal, DEAL_ORDER);
      string side = SideFromType(dtype);

      int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

      if(entry == DEAL_ENTRY_IN)
      {
         // OPEN market (or execution of a pending) — publish OPEN with latest SL/TP if available.
         double sl = 0.0, tp = 0.0;
         if(PositionSelect(symbol))
         {
            sl = PositionGetDouble(POSITION_SL);
            tp = PositionGetDouble(POSITION_TP);
         }

         string j = EventBase("OPEN", symbol, side, "MARKET", order_ticket, pos_id);
         j += ",\"lots\":" + DoubleToString(vol, 2);
         j += ",\"price\":" + DoubleToString(price, digits);
         j += ",\"sl\":" + DoubleToString(sl, digits);
         j += ",\"tp\":" + DoubleToString(tp, digits);
         j += "}";
         AppendEventLine(j);
         return;
      }

      if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_OUT_BY)
      {
         // CLOSE or PARTIAL — we don't know remaining volume on primary from the deal alone;
         // publish closed_volume and best-effort remaining_volume (if position still exists).
         double remaining = 0.0;
         bool still = PositionSelect(symbol);
         if(still) remaining = PositionGetDouble(POSITION_VOLUME);

         string typ = (still && remaining > 0.0) ? "PARTIAL" : "CLOSE";
         string j = EventBase(typ, symbol, side, "MARKET", order_ticket, pos_id);
         j += ",\"closed_lots\":" + DoubleToString(vol, 2);
         j += ",\"remaining_lots\":" + DoubleToString(remaining, 2);
         j += ",\"price\":" + DoubleToString(price, digits);
         j += "}";
         AppendEventLine(j);
         return;
      }
   }

   // 2) Order events: placement, modification, cancel for pending orders
   if(trans.type == TRADE_TRANSACTION_ORDER_ADD || trans.type == TRADE_TRANSACTION_ORDER_UPDATE || trans.type == TRADE_TRANSACTION_ORDER_DELETE)
   {
      long ticket = (long)trans.order;
      if(ticket <= 0) return;

      // For ORDER_DELETE we may not be able to select it; publish cancel best-effort.
      bool selected = OrderSelect((ulong)ticket);
      string symbol = selected ? OrderGetString(ORDER_SYMBOL) : trans.symbol;
      ENUM_ORDER_TYPE t = selected ? (ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE) : (ENUM_ORDER_TYPE)trans.order_type;
      string orderType = OrderTypeFromOrder(t);
      string side = (t == ORDER_TYPE_BUY || t == ORDER_TYPE_BUY_LIMIT || t == ORDER_TYPE_BUY_STOP || t == ORDER_TYPE_BUY_STOP_LIMIT) ? "BUY" : "SELL";
      int digits = (symbol != "" ? (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS) : 5);

      if(trans.type == TRADE_TRANSACTION_ORDER_DELETE)
      {
         string j = EventBase("CANCEL", symbol, side, orderType, ticket, 0);
         j += "}";
         AppendEventLine(j);
         return;
      }

      // For add/update: publish as OPEN (pending) or MODIFY (pending)
      double vol = selected ? OrderGetDouble(ORDER_VOLUME_CURRENT) : 0.0;
      double price = selected ? OrderGetDouble(ORDER_PRICE_OPEN) : 0.0;
      double sl = selected ? OrderGetDouble(ORDER_SL) : 0.0;
      double tp = selected ? OrderGetDouble(ORDER_TP) : 0.0;

      string typ = (trans.type == TRADE_TRANSACTION_ORDER_ADD ? "OPEN" : "MODIFY");
      string j = EventBase(typ, symbol, side, orderType, ticket, 0);
      j += ",\"lots\":" + DoubleToString(vol, 2);
      j += ",\"price\":" + DoubleToString(price, digits);
      j += ",\"sl\":" + DoubleToString(sl, digits);
      j += ",\"tp\":" + DoubleToString(tp, digits);
      j += "}";
      AppendEventLine(j);
      return;
   }

   // 3) Position SL/TP modify (often from trailing/BE): detect via POSITION_UPDATE transactions
   if(trans.type == TRADE_TRANSACTION_POSITION)
   {
      string symbol = trans.symbol;
      if(symbol == "") return;
      if(!PositionSelect(symbol)) return;

      long pos_id = (long)PositionGetInteger(POSITION_IDENTIFIER);
      long now_ms = NowMs();
      if(pos_id == g_last_modify_pos_id && (now_ms - g_last_modify_ms) < InpDebounceMs) return;
      g_last_modify_pos_id = pos_id;
      g_last_modify_ms = now_ms;

      long ptype = (long)PositionGetInteger(POSITION_TYPE);
      string side = (ptype == POSITION_TYPE_BUY ? "BUY" : "SELL");
      double sl = PositionGetDouble(POSITION_SL);
      double tp = PositionGetDouble(POSITION_TP);
      int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

      string j = EventBase("MODIFY", symbol, side, "MARKET", 0, pos_id);
      j += ",\"sl\":" + DoubleToString(sl, digits);
      j += ",\"tp\":" + DoubleToString(tp, digits);
      j += "}";
      AppendEventLine(j);
      return;
   }
}

