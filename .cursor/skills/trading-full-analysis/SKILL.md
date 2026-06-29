---
name: trading-full-analysis
description: "Capture TradingView + GoCharting data and produce agent self-analysis FULL_ANALYSIS (Schema A) with full phan_tich_cham_diem scoring output at the final step — without OpenAI API."
---

# Trading FULL_ANALYSIS — Agent Self-Analysis

Hướng dẫn AI agent capture đúng bộ data buổi sáng, validate slot, đọc theo thứ tự chuẩn, và **tự phân tích** ra JSON Schema A (`[FULL_ANALYSIS]`) theo `system-prompt.md` và `master_trading_playbook.md` §1.

**Không** gọi `coinmap-automation analyze` hay OpenAI Responses API trong luồng này.

## When to use

- User yêu cầu **FULL_ANALYSIS**, phân tích đầu ngày, morning analysis
- Cần capture TradingView + GoCharting footprint rồi agent tự ra Schema A
- Cần kiểm tra đủ 11 slot chart + `footprint_combined` JSON trước khi phân tích

## Do not use when

- Chỉ cần intraday update (`[INTRADAY_UPDATE]`) — dùng skill/workflow khác
- Muốn pipeline tự động qua OpenAI API — dùng `coinmap-automation all --gocharting`
- Chưa có `.venv`, Chrome, hoặc credentials GoCharting/TradingView

## FULL_ANALYSIS Legacy (vector store)

Dùng bộ rule từ **`OPENAI_VECTOR_STORE_IDS`** thay `master_trading_playbook.md`.

```bash
# Phase 0 — sync knowledge (chạy trước, hoặc khi đổi file trên vector store)
coinmap-automation sync-vector-store-knowledge

# Phase 1–2 — capture + validate legacy
coinmap-automation capture-full-analysis --gocharting
python scripts/full_analysis_manifest.py --legacy
```

Chi tiết: [`references/legacy-knowledge.md`](references/legacy-knowledge.md).

## Prerequisites

1. Activate venv: `source .venv/bin/activate`
2. `.env`: `TRADINGVIEW_PASSWORD`, `GOCHARTING_EMAIL`, `GOCHARTING_PASSWORD`
3. Google Chrome installed; khuyến nghị `coinmap-automation browser up` trước capture
4. Cặp chính từ `data/.main_chart_symbol` (mặc định XAUUSD) — **không** truyền `--main-symbol`

## Phase 1 — Capture

```bash
coinmap-automation browser up          # optional, khuyến nghị
coinmap-automation capture-full-analysis --gocharting
```

Lệnh này:
- Capture TradingView (11 slot: DXY + main pair, gồm `15m_ict`)
- Capture GoCharting DXY M15 + GC M15/M5 (PNG + CSV)
- Ghi `footprint_images/footprint_combined_15m.json` và `footprint_combined_5m.json` (WS)
- Validate slot; in manifest JSON ra stdout

Nếu login lần đầu thất bại: thêm `--headed`.

Flags hỗ trợ: `--headed`, `--charts-dir`, `--gocharting-config`, `--config` (coinmap.yaml).

## Phase 2 — Validate

Sau capture, chạy manifest (hoặc đọc JSON cuối output của capture):

```bash
python scripts/full_analysis_manifest.py
```

Exit code `0` = `ready_for_analysis: true`. Exit code `1` = thiếu slot hoặc footprint.

**Không phân tích** nếu `ready_for_analysis` là `false`. Recapture hoặc sửa lỗi trước.

Chi tiết slot: xem `references/data-slots.md`.

## Phase 3 — Đọc data

Đọc file theo thứ tự slot trong manifest (`slots[].index` 1→11), sau đó đọc `footprint_json[]`.

| Loại file | Cách đọc |
|-----------|----------|
| TradingView `.json` | OHLC `bars[]` — cấu trúc giá |
| TradingView `.png` / `.url` | Đọc ảnh / snapshot — indicators, session liquidity, ICT killzones (`15m_ict`) |
| GoCharting `.csv` | CVD, delta, volume theo nến |
| GoCharting `.png` | Overview orderflow chart |
| `footprint_combined_*.json` | `candles[].footprint[]` (buy/sell), `ohlc`, `time_gmt7` |

**GC vs spot:** GoCharting footprint là **GC1!** (COMEX futures), không phải spot XAUUSD. Map correlation; không mix symbol khi chấm điểm footprint.

## Phase 4 — Phân tích

**Bắt buộc đọc trước:**
1. [`system-prompt.md`](../../system-prompt.md) — mode `[FULL_ANALYSIS]`, Schema A, format `phan_tich_cham_diem`
2. [`master_trading_playbook.md`](../../master_trading_playbook.md) §1 — 8 bước, scoring, filters

**Trình tự 8 bước** (playbook §1.3): xem `references/analysis-steps.md`.

Tóm tắt:
1. DXY H4/H1/M15 + footprint M15 → macro bias USD
2. Main H4/H1 → trend, premium/discount, POI
3. Main M15 → vùng/plan (không BUY premium / SELL discount)
4. Main M5 → confirm entry module
5. Footprint M15/M5 → trap, stacked, absorption, CVD ≥3 nến
6. Filters (anti-sweep, RR, session)
7. Scoring `hop_luu` theo mục 0.3 (4 nhóm điểm)
8. Limit conditions → `prices[]`, `intraday_hanh_dong`

## Phase 5 — Output (bước cuối)

**Bắt buộc in ra 2 phần theo thứ tự sau** — không chỉ gửi JSON im lặng.

### 5.1. In `phan_tich_cham_diem` (markdown, đọc được trên chat)

Trước tiên, **in đầy đủ** nội dung phân tích chấm điểm ra message cho user (plain markdown, không bọc JSON):

- Phân tích **đủ từng plan** trong `prices`: `plan_chinh`, `plan_phu`, `scalp` (XAUUSD; non-XAUUSD có thể bỏ scalp)
- Mỗi plan: tiêu đề `📍 PLAN … — X/100`, block `🏷️ Trạng thái vùng`, 4 nhóm điểm (`🧭` `💧` `👣` `🛡️`)
- Order Flow + Footprint: bắt buộc `→ Số liệu:` (con số cụ thể từ CSV/footprint JSON) rồi `→ Phân tích chấm điểm:`
- Kết mỗi plan: `✅ Tổng <label>: X/100`
- **Kết toàn bộ** bằng đoạn `📊 ĐÁNH GIÁ DỮ LIỆU ĐẦU VÀO`

Chi tiết format: `system-prompt.md` Schema A + `references/analysis-steps.md` § Format `phan_tich_cham_diem`.

**Không** rút gọn thành vài câu — đây là deliverable chính của bước cuối.

### 5.2. Schema A JSON

Sau phần markdown trên, trả block ` ```json ` với đủ keys:

- `context` — `DXY` (H4, H1, M15, Footprint_M15) + `{main_symbol}` (H4, H1)
- `phan_tich_cham_diem` — **cùng nội dung đã in ở 5.1** (copy vào JSON, escape newline `\n`)
- `output_ngan_gon` — tóm tắt hành động
- `prices[]` — `plan_chinh`, `plan_phu`, `scalp` (XAUUSD); mỗi item có `label`, `value`, `vung_cho`, `hop_luu`, `trade_line`
- `intraday_hanh_dong` — `"VÀO LỆNH"` | `"chờ"` | `"loại"`
- `trade_line_chinh` — pipe MT5 khi vào ngay; `""` nếu chờ

Ví dụ: `references/schema-a-template.json`.

Tùy chọn lưu: `data/{SYM}/morning_full_analysis.json`.

## Troubleshooting

| Vấn đề | Hướng xử lý |
|--------|-------------|
| GoCharting stale | Data quá cũ — recapture trong giờ giao dịch |
| Thiếu `footprint_combined` | Kiểm tra `footprint_ws.enabled: true` trong `config/gocharting.yaml`; WS timeout |
| Login fail | `--headed`, kiểm tra `GOCHARTING_EMAIL/PASSWORD` |
| Slot TV thiếu | `tradingview_capture` enabled trong `config/coinmap.yaml` |
| `ready_for_analysis: false` | Đọc `slots[].reason` trong manifest |

## Resources

- `references/data-slots.md` — 11 slot + file patterns
- `references/analysis-steps.md` — 8 bước + scoring checklist
- `references/schema-a-template.json` — ví dụ Schema A
