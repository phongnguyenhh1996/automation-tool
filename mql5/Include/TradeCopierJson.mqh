// TradeCopierJson.mqh
// Minimal JSON helpers for predictable, flat JSON objects.
// Assumption: each event is a single-line JSON object with primitive values only.

#pragma once

string _JsonTrim(const string s)
{
   string out = s;
   StringTrimLeft(out);
   StringTrimRight(out);
   return out;
}

// Returns the raw value substring (unquoted/unescaped not guaranteed) for "key":VALUE
bool JsonTryGetRaw(const string json, const string key, string &rawOut)
{
   const string needle = "\"" + key + "\"";
   int k = StringFind(json, needle, 0);
   if(k < 0) return false;

   int colon = StringFind(json, ":", k + StringLen(needle));
   if(colon < 0) return false;

   int i = colon + 1;
   while(i < StringLen(json))
   {
      ushort c = StringGetCharacter(json, i);
      if(c != ' ' && c != '\t' && c != '\r') break;
      i++;
   }
   if(i >= StringLen(json)) return false;

   ushort first = StringGetCharacter(json, i);
   if(first == '\"')
   {
      // parse JSON string until next unescaped quote
      int j = i + 1;
      bool esc = false;
      while(j < StringLen(json))
      {
         ushort cj = StringGetCharacter(json, j);
         if(esc) { esc = false; j++; continue; }
         if(cj == '\\') { esc = true; j++; continue; }
         if(cj == '\"') break;
         j++;
      }
      if(j >= StringLen(json)) return false;
      rawOut = StringSubstr(json, i, (j - i) + 1);
      return true;
   }

   // number / true / false / null: read until comma or closing brace
   int j = i;
   while(j < StringLen(json))
   {
      ushort cj = StringGetCharacter(json, j);
      if(cj == ',' || cj == '}' || cj == '\n' || cj == '\r') break;
      j++;
   }
   rawOut = _JsonTrim(StringSubstr(json, i, j - i));
   return rawOut != "";
}

bool JsonTryGetString(const string json, const string key, string &valueOut)
{
   string raw;
   if(!JsonTryGetRaw(json, key, raw)) return false;
   raw = _JsonTrim(raw);
   if(StringLen(raw) < 2) return false;
   if(StringGetCharacter(raw, 0) != '\"') return false;
   if(StringGetCharacter(raw, StringLen(raw) - 1) != '\"') return false;

   // NOTE: minimal unescape: handles \", \\ only (good enough for our own emitted JSON)
   string inner = StringSubstr(raw, 1, StringLen(raw) - 2);
   inner = StringReplace(inner, "\\\"", "\"");
   inner = StringReplace(inner, "\\\\", "\\");
   valueOut = inner;
   return true;
}

bool JsonTryGetLong(const string json, const string key, long &valueOut)
{
   string raw;
   if(!JsonTryGetRaw(json, key, raw)) return false;
   raw = _JsonTrim(raw);
   if(raw == "null" || raw == "") return false;
   valueOut = (long)StringToInteger(raw);
   return true;
}

bool JsonTryGetDouble(const string json, const string key, double &valueOut)
{
   string raw;
   if(!JsonTryGetRaw(json, key, raw)) return false;
   raw = _JsonTrim(raw);
   if(raw == "null" || raw == "") return false;
   valueOut = StringToDouble(raw);
   return true;
}

bool JsonTryGetBool(const string json, const string key, bool &valueOut)
{
   string raw;
   if(!JsonTryGetRaw(json, key, raw)) return false;
   raw = _JsonTrim(raw);
   if(raw == "true") { valueOut = true; return true; }
   if(raw == "false") { valueOut = false; return true; }
   return false;
}

string JsonEscape(const string s)
{
   string out = s;
   out = StringReplace(out, "\\", "\\\\");
   out = StringReplace(out, "\"", "\\\"");
   out = StringReplace(out, "\r", "\\r");
   out = StringReplace(out, "\n", "\\n");
   return out;
}

