# Core Rules

Nguồn: tổng hợp từ `master_trading_playbook.md` section 0 và 10.

## Nguyên tắc gốc
- Không ép entry khi thiếu xác nhận.
- Ưu tiên lệnh limit đẹp, hợp lưu cao, dễ khớp, dễ TP.
- Nếu chưa đủ xác nhận: chỉ vùng chờ hoặc đứng ngoài.
- Không đổi bias trong ngày trừ khi filter vi phạm rõ.

## Điều kiện vào lệnh
Phải có đủ 3 lớp xác nhận:
1. Cấu trúc giá đúng hướng.
2. Order Flow / CVD đồng thuận.
3. Footprint xác nhận mạnh.

## Thang điểm hợp lưu
- `hop_luu` thang 0-100.
- 5 nhóm tiêu chí: cấu trúc, order flow/CVD, footprint, lọc nâng cao, quản lý/RR.
- Hành động tham chiếu:
  - <70: đứng ngoài/backup.
  - 70-89: vùng chờ.
  - >=90: hợp lưu mạnh.

## Công thức hủy lệnh
Hủy/loại nếu có cụm tín hiệu ngược mạnh:
- H1 BOS hoặc M15 CHoCH ngược.
- CVD đảo mạnh >= 3 nến.
- VWAP/POC shift mạnh + giá hold + footprint đảo chiều.

## Công thức quản lý lệnh
- Dời SL theo cấu trúc (HL/LH mới + buffer), không theo số giá cố định.
- Chỉ BE khi có xác nhận cấu trúc/flow.
- Thoát khi phá cấu trúc bảo vệ + CVD đảo + mất VWAP không reclaim.
