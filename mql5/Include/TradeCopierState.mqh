// TradeCopierState.mqh
// Persist follower state (offset + simple mappings) in Common Files.

#pragma once

struct CopierMappingRow
{
   long primary_id;     // primary_position_id (positions) or primary_ticket (orders)
   long slave_ticket;   // slave position ticket or slave order ticket
   string kind;         // "POS" or "ORD"
};

string CopierCommonDir(const string channel)
{
   return "trade_copier\\" + channel;
}

string CopierEventsPath(const string channel)
{
   return CopierCommonDir(channel) + "\\events.jsonl";
}

string CopierStatePath(const string channel, const long slave_login)
{
   return CopierCommonDir(channel) + "\\slave_" + (string)slave_login + "_state.csv";
}

bool CopierEnsureCommonDir(const string channel)
{
   // MQL5 doesn't have mkdir; FileOpen will create file but not directories.
   // We create a small marker file in nested path; terminals create dirs on demand on Windows,
   // but on some setups you must pre-create. We'll attempt FileOpen and report error.
   string marker = CopierCommonDir(channel) + "\\.dir";
   int h = FileOpen(marker, FILE_WRITE|FILE_TXT|FILE_COMMON);
   if(h == INVALID_HANDLE) return false;
   FileWriteString(h, "ok\n");
   FileClose(h);
   return true;
}

bool CopierLoadOffset(const string channel, const long slave_login, long &offsetOut)
{
   offsetOut = 0;
   string path = CopierStatePath(channel, slave_login);
   int h = FileOpen(path, FILE_READ|FILE_TXT|FILE_COMMON);
   if(h == INVALID_HANDLE) return false;

   while(!FileIsEnding(h))
   {
      string line = FileReadString(h);
      if(StringFind(line, "offset,") == 0)
      {
         string v = StringSubstr(line, StringLen("offset,"));
         StringTrimLeft(v); StringTrimRight(v);
         offsetOut = (long)StringToInteger(v);
         FileClose(h);
         return true;
      }
   }
   FileClose(h);
   return true;
}

bool CopierSaveOffsetAndMappings(
   const string channel,
   const long slave_login,
   const long offset,
   CopierMappingRow &rows[],
   const int rowsCount
)
{
   string path = CopierStatePath(channel, slave_login);
   int h = FileOpen(path, FILE_WRITE|FILE_TXT|FILE_COMMON);
   if(h == INVALID_HANDLE) return false;

   FileWriteString(h, "offset," + (string)offset + "\n");
   FileWriteString(h, "primary_id,slave_ticket,kind\n");
   for(int i=0;i<rowsCount;i++)
   {
      FileWriteString(
         h,
         (string)rows[i].primary_id + "," + (string)rows[i].slave_ticket + "," + rows[i].kind + "\n"
      );
   }
   FileClose(h);
   return true;
}

int CopierFindMappingIndex(CopierMappingRow &rows[], const int rowsCount, const long primary_id, const string kind)
{
   for(int i=0;i<rowsCount;i++)
   {
      if(rows[i].primary_id == primary_id && rows[i].kind == kind) return i;
   }
   return -1;
}

void CopierUpsertMapping(CopierMappingRow &rows[], int &rowsCount, const long primary_id, const long slave_ticket, const string kind, const int maxRows)
{
   int idx = CopierFindMappingIndex(rows, rowsCount, primary_id, kind);
   if(idx >= 0)
   {
      rows[idx].slave_ticket = slave_ticket;
      return;
   }
   if(rowsCount >= maxRows)
   {
      // simple eviction: drop oldest
      for(int i=1;i<rowsCount;i++) rows[i-1] = rows[i];
      rowsCount = rowsCount - 1;
   }
   rows[rowsCount].primary_id = primary_id;
   rows[rowsCount].slave_ticket = slave_ticket;
   rows[rowsCount].kind = kind;
   rowsCount++;
}

