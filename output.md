[OUTPUT_CHI_TIET]
🔥 PHÂN TÍCH {symbol} – FULL QUY TRÌNH

——————————
🧭 1. BỐI CẢNH VĨ MÔ (DXY)

👉 KẾT LUẬN DXY:

————————————-
🧭 2. CẤU TRÚC {symbol}
🔴 H4

🔴 H1

🔴 M15


🧭 4. ORDER FLOW (CVD)
M15:

M5:


🚨 KẾT LUẬN CHÍNH
👉 Bias ngày:



👉 Trạng thái hiện tại: (ngắn gọn)



📍 PLAN CHÍNH 

📊 CHẤM ĐIỂM PLAN CHÍNH: thang điểm 100 (chỉ chấm điểm không phân tích)



⚡️ PLAN PHỤ 

📊 CHẤM ĐIỂM PLAN PHỤ: thang điểm 100 (chỉ chấm điểm không phân tích)

📊 SCALP:

📊 CHẤM ĐIỂM SCALP: thang điểm 100 (chỉ chấm điểm không phân tích)

🤖 EA GRID PLAN:

[OUTPUT_NGAN_GON]
👉 BỐI CẢNH VĨ MÔ: 
Trend DXY:
Trend {symbol} H1 - M15: 
Bias chính:

📍 PLAN CHÍNH VÙNG CHỜ: (chỉ đưa ra 1 plan điểm điểm cao nhất, hop_luu, lệnh trade_line)
📍 PLAN PHỤ VÙNG CHỜ: (chỉ đưa ra 1 plan điểm cao nhất, hop_luu, lệnh trade_line)
⚡️SCALP VÙNG: (chỉ đưa ra 1 plan điểm cao nhất, hop_luu, lệnh trade_line)


## đối với [OUTPUT_NGAN_GON]

Chèn dòng vào lệnh này vào dòng trước của "Hành động: VÀO LỆNH", Cấu trúc vào lệnh đúng format sau (số dùng dấu chấm thập phân; không thêm ký tự lạ giữa các phần):

   - Lệnh chờ **LIMIT** hoặc **STOP**:
     `SELL LIMIT 4565.0 | SL 4592.0 | TP1 4550.0 | TP2 4545.0 | Lot 0.02`
     (TP2 có thể bỏ nếu không dùng: `... | TP1 4550.0 | Lot 0.02`)

   - Lệnh **MARKET**:
     `BUY MARKET | SL 99.0 | TP1 101.0 | Lot 0.01`

   Thứ tự bắt buộc: giá entry (LIMIT/STOP) → `|` → `SL` → `|` → `TP1` → (tuỳ chọn) `| TP2` → `|` → `Lot`.

## lưu ý: PLACEHOLDER CONVENTION (bắt buộc tuân theo)
- {symbol}: cặp đang phân tích ở lần gọi này (ví dụ: EURUSD, USDJPY, XAUUSD).

- Nếu {symbol} không phải XAUUSD thì BỎ QUA/ KHÔNG ĐƯỢC sinh các mục sau trong [OUTPUT_CHI_TIET]:
  - 📊 SCALP:
  - ——————————
  - 🧭 1. BỐI CẢNH VĨ MÔ (DXY)
  - 👉 KẾT LUẬN DXY:
  - ————————————-
  - 🧭 2. CẤU TRÚC {symbol} (🔴 H4 / 🔴 H1 / 🔴 M15)
  - 🧭 4. ORDER FLOW (CVD) (M15 / M5)



