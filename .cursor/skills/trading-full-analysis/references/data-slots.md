# FULL_ANALYSIS Data Slots

11 slot chart (footprint source = GoCharting) + 2 footprint JSON.

Thứ tự khớp `chart_image_order_for_main_symbol()` trong `src/automation_tool/images.py`.

| # | Source | Symbol | Interval | Files |
|---|--------|--------|----------|-------|
| 1 | tradingview | DXY | 4h | `{stamp}_tradingview_DXY_4h.json` / `.png` / `.url` |
| 2 | tradingview | DXY | 1h | `{stamp}_tradingview_DXY_1h.*` |
| 3 | tradingview | DXY | 15m | `{stamp}_tradingview_DXY_15m.*` |
| 4 | tradingview | {main} | 4h | `{stamp}_tradingview_{SYM}_4h.*` |
| 5 | tradingview | {main} | 1h | `{stamp}_tradingview_{SYM}_1h.*` |
| 6 | tradingview | {main} | 15m | `{stamp}_tradingview_{SYM}_15m.*` |
| 7 | tradingview | {main} | 15m_ict | `{stamp}_tradingview_{SYM}_15m_ict.*` — Session Liquidity / ICT Killzones |
| 8 | tradingview | {main} | 5m | `{stamp}_tradingview_{SYM}_5m.*` |
| 9 | gocharting | DXY | 15m | `{stamp}_gocharting_DXY_15m.csv` + `.png` |
| 10 | gocharting | GC | 15m | `{stamp}_gocharting_GC_15m.csv` + `.png` |
| 11 | gocharting | GC | 5m | `{stamp}_gocharting_GC_5m.csv` + `.png` |

**Footprint WS (bổ sung, không tính trong 11 slot):**

| File | Nội dung |
|------|----------|
| `footprint_images/footprint_combined_15m.json` | GC M15: `candles[].footprint[]`, `ohlc`, `time_gmt7` |
| `footprint_images/footprint_combined_5m.json` | GC M5: tương tự |

`{stamp}` = `YYYYMMDD_HHMMSS` từ capture mới nhất. `{main}` / `{SYM}` = cặp active (`data/.main_chart_symbol`).

## Cách đọc từng loại

### TradingView JSON (`source: tvdatafeed`)

- `bars[]`: OHLC theo thời gian
- Dùng cho cấu trúc HH/HL, BOS/CHoCH, premium/discount

### TradingView PNG / URL

- Indicators trên chart (VWAP, POC, session marks)
- Slot `15m_ict`: liquidity pools, killzones

### GoCharting CSV

- Cột orderflow: delta, CVD, volume theo nến
- DXY M15: macro order flow
- GC M15/M5: footprint overview (không có bid/ask per level — dùng `footprint_combined` JSON)

### footprint_combined JSON

Mỗi candle:

```json
{
  "time_gmt7": "Fri Jun 26 2026 13:40:00 GMT+0700",
  "footprint": [{"price": 4042.2, "buy": 3, "sell": 0}],
  "ohlc": {"open": 4040.0, "high": 4043.0, "low": 4039.0, "close": 4042.0}
}
```

Dùng cho: trap, stacked BID/ASK, absorption, reclaim VWAP/POC.

## GC vs spot

- GoCharting chart = **GC1!** (COMEX gold futures)
- Main pair TV/MT5 = spot (vd. XAUUSD)
- Phân tích footprint GC; map bias sang spot qua correlation — không gán giá GC = giá spot
