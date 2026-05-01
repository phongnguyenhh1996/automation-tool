# Full Analysis Knowledge

Nguồn: `master_trading_playbook.md` section 1, 6, 7.

## Mục tiêu
- Phân tích tổng thể đầu ngày.
- Kết luận bias, plan chính/phụ/scalp (nếu phù hợp), và mức độ sẵn sàng vào lệnh.

## Trình tự bắt buộc
1. DXY / macro bias.
2. Context cấu trúc H1.
3. M15 chọn vùng entry.
4. M5 xác nhận module entry.
5. Footprint confirm (M15/M5).
6. Lọc nâng cao (OI, US10Y, VIX, tin đỏ, session).
7. Chấm điểm hợp lưu và quyết định.

## Filter trước khi đề xuất entry
- Không BUY tại premium, không SELL tại discount.
- CVD đồng thuận >= 3 nến.
- Footprint phải có stacked/absorption/trap đủ chuẩn.
- Nếu thiếu xác nhận quan trọng thì hạ mức tin cậy hoặc chuyển chờ.

## Re-check Before Touch
Trong vùng sát entry cần check lại CVD, VWAP/POC, stacked/absorption, volume spike.
Thiếu xác nhận thì hủy limit.

## Output mong muốn
- Bias H1, trend M15, bối cảnh DXY.
- Main/backup/scalp plans với `hop_luu`.
- Kết luận: vào lệnh/chờ/loại.
- Nếu cần: đề xuất mode EA/Grid sau full analysis.
