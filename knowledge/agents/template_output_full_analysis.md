# Template Output for Full Analysis

Nguồn: `master_trading_playbook.md` section 9.5.

## Mục đích
- Chỉ áp dụng cho nội dung text của Schema A:
  - `out_chi_tiet`
  - `output_ngan_gon`

## Yêu cầu format chính
- `out_chi_tiet` bám cấu trúc phân tích chi tiết theo template đã chuẩn hóa.
- `output_ngan_gon` phải có đủ cho từng plan:
  - trade_line tham khảo
  - hop_luu
  - điều kiện vào lệnh

## Quy tắc placeholder
- `{symbol}` thay đúng cặp đang phân tích.

## Phạm vi áp dụng
- Không dùng template này cho:
  - `[INTRADAY_ALERT]`
  - `[INTRADAY_UPDATE]`
  - `[TRADE_MANAGEMENT]`
