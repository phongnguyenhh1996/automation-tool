# Intraday Update Knowledge

Nguồn: `master_trading_playbook.md` section 3.

## Mục tiêu
- Cập nhật định kỳ plan intraday sau full analysis.
- Quyết định giữ/hủy/dời plan cũ và phát hiện setup mới.

## Checklist mỗi lần update
- H1 BOS ngược?
- M15 CHoCH ngược?
- VWAP/POC shift mạnh + hold?
- CVD đảo >= 3 nến liên tiếp?
- Vùng chờ còn gần giá, còn ý nghĩa?
- Có combo footprint đảo chiều rõ?

## Quy tắc giữ/hủy limit
- Giữ nếu cấu trúc lớn chưa vi phạm và flow chưa đảo rõ.
- Hủy nếu vi phạm các filter chính hoặc có tín hiệu ngược mạnh có follow-through.

## Khi plan cũ đã lỡ
- Không chase.
- Chỉ tìm re-entry nếu có pullback chuẩn vào POI mới.
- Nếu flow yếu gần TP thì ưu tiên chốt nhanh/chốt non theo bối cảnh.
