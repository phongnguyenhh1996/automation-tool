# Intraday Alert Knowledge

Nguồn: `master_trading_playbook.md` section 2.

## Mục tiêu
- Đánh giá phản ứng tại vùng chờ khi giá chạm.
- Quyết định nhanh: `VÀO LỆNH`, `chờ`, hoặc `loại`.

## Những gì phải kiểm
- Vùng chờ còn hợp lệ không.
- H1/M15 có đảo cấu trúc chưa.
- VWAP/POC có shift mạnh không.
- Footprint M5: trap, absorption, stacked đúng vị trí, CVD >= 3 nến.

## Checklist quyết định
- BUY: discount/HL/demand + trap SELL + CVD bật + reclaim VWAP/POC.
- SELL: premium/LH/supply + trap BUY + CVD giảm + mất VWAP/POC.

## Bộ loại bỏ ngay
- Không có xác nhận footprint quan trọng.
- CVD đi ngược rõ.
- Giá mất VWAP/POC và không reclaim.
- Tín hiệu ngược có follow-through.

## Trường hợp không có stacked
Chỉ cân nhắc khi đủ cụm thay thế (vị trí đúng + CVD đảo mạnh + breakout/breakdown có volume + giữ trạng thái VWAP).
