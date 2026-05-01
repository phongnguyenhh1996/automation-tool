# Trade Management Knowledge

Nguồn: `master_trading_playbook.md` section 4.

## Mục tiêu
- Quản lý lệnh sau entry theo trạng thái cấu trúc + order flow + footprint.

## Bộ kiểm tra sau entry (1-3 nến đầu)
Cần giữ được phần lớn các yếu tố:
- CVD đồng thuận.
- Giá giữ VWAP đúng hướng.
- Stacked/absorption còn hiệu lực.
- Không phá cấu trúc bảo vệ gần nhất.

## Hành động
- `giu_nguyen`: khi lệnh còn khỏe và flow thuận.
- `chinh_trade_line`: khi cần cập nhật SL/TP theo vùng bảo vệ mới.
- `loại`: khi xuất hiện dấu hiệu xấu mạnh (CVD đảo, mất VWAP/POC, phá cấu trúc).

## Quy tắc SL/TP
- Dời SL theo HL/LH mới + buffer.
- Không dời SL chỉ vì đạt số giá lợi nhuận.
- TP1 ưu tiên vùng dễ đạt; TP2 theo vùng phản ứng mạnh kế tiếp.
- Nếu flow suy yếu trước mục tiêu: có thể chốt non/chốt bớt.
