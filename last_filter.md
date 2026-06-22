# last_filter

> Bộ lọc cuối cùng trước khi chốt output. Áp dụng **bắt buộc** cho `[FULL_ANALYSIS]` và `[INTRADAY_UPDATE]` — chạy sau khi đã phân tích chart/playbook, **ngay trước** khi ghi `prices` / `trade_line` / `vung_cho` vào JSON.

## Nguyên tắc gốc (bắt buộc)

**Chỉ được chọn vùng fresh.** Mọi plan trong `prices` phải gắn vùng **chưa bị chạm** tại thời điểm publish.

Nếu vùng **không fresh** (đã chạm, used, mitigated, invalid) → **không** giữ plan tại vùng đó; **bắt buộc quay lại** playbook chọn **vùng khác** (POI/OB/FVG/EQ fresh khác cùng bias). Không downgrade, không retest cùng `vung_cho`, không ngoại lệ “có xác nhận mới” để giữ lại vùng cũ.

Nếu không tìm được vùng fresh đủ chất lượng → `prices` có thể ít hơn (1 plan, 0 plan) hoặc kết luận đứng ngoài — **không** cố giữ vùng đã chạm.

---

## A. Kiểm tra bắt buộc trước khi chốt output

Trước khi xuất `[FULL_ANALYSIS]` hoặc `[INTRADAY_UPDATE]`, **rà 3 câu hỏi cho từng zone / plan** (plan_chinh, plan_phu, scalp, hoặc candidate trong `prices`):

| # | Câu hỏi | Hành động |
|---|---------|-----------|
| 1 | **Vùng đã bị chạm chưa?** | Giá (wick/body) đã vào `vung_cho` hoặc chạm mức limit/POI trên chart hiện tại. **Có → không fresh → loại; chọn vùng khác.** |
| 2 | **Nếu đã chạm, phản ứng đó đã consume thanh khoản / vùng chưa?** | Dùng để **ghi lý do loại** (used / mitigated). Không dùng để giữ plan. |
| 3 | **Vùng còn đủ điều kiện fresh trên chart hiện tại không?** | Chỉ **Có** khi giá **chưa** test vùng. **Không** → loại; tìm POI fresh khác. |

### Quy tắc tự động

```
không fresh (đã chạm / used / mitigated / invalid)
  → loại khỏi prices
  → quay lại chọn vùng KHÁC (bắt buộc)
  → không trade_line limit tại vùng cũ
```

Không có nhánh downgrade. Không giữ vùng cũ dù HTF vẫn đẹp.

---

## B. Phân loại zone: fresh / used / invalid

Mỗi zone candidate phải được gán **một** trạng thái trước khi vào `prices`:

### Fresh (duy nhất được publish)
- Giá **chưa chạm** vùng (chưa test POI / `vung_cho`).
- Được đưa vào `prices` nếu đủ `hop_luu` và pass playbook.
- Được đặt limit thụ động / EA vùng chờ.

### Used
- Đã chạm **ít nhất một lần** (có phản ứng hoặc chưa).
- **Không** đưa vào `prices`. **Bắt buộc** tìm vùng fresh khác — không retest cùng range.

### Invalid
- Đã chạm và cấu trúc / CVD / VWAP **đi ngược** bias plan.
- **Loại ngay**; tìm vùng fresh khác hoặc đứng ngoài nếu không có.

### Bảng quyết định

| Trạng thái | Trong `prices` | Hành động |
|------------|----------------|-----------|
| **fresh** | Có (nếu đủ hop_luu) | Publish limit tại vùng đó |
| used | **Không** | Chọn vùng fresh khác (bắt buộc) |
| invalid | **Không** | Chọn vùng fresh khác hoặc đứng ngoài |
| mitigated (xem mục C) | **Không** | Chọn vùng fresh khác (bắt buộc) |

---

## C. Không giữ plan nếu vùng đã mitigated

**Mitigated** = OB/FVG/EQ/POI đã được giá phản ứng qua; thanh khoản tại đó đã xử lý; hoặc test + reject không còn edge.

→ Coi như **không fresh** → hủy plan, **chọn vùng fresh khác** (playbook Bước 3 M15 / Bước 8).

Khi update intraday: plan sáng gắn vùng đã mitigated trước thời điểm update → **bỏ hẳn**; không kéo `vung_cho` cũ sang `prices`.

---

## E. Quy tắc xuất plan (`prices`) sau lọc

### E.1. Trước thời điểm publish
- Zone không fresh trước khi gửi phân tích → **không publish** tại zone đó.
- **Bắt buộc** thay bằng vùng fresh khác đủ chất lượng, hoặc giảm số plan / đứng ngoài.

### E.2. Nội dung `prices` — chỉ fresh

Chỉ được giữ trong `prices` vùng **fresh** (chưa chạm tại thời điểm xuất).

**Cấm** trong `prices`:
- used (đã chạm);
- mitigated;
- invalid;
- premium/discount sai hướng (playbook 1.3 Bước 3);
- retest / “xác nhận mới” tại **cùng** `vung_cho` đã từng chạm.

Vùng fresh thay thế phải là **POI/range mới** trên chart (OB/FVG/EQ/LVN/liquidity pool chưa test), không phải mức giá cũ được đổi tên label.

### E.3. Thứ tự thao tác khi chốt JSON
1. Liệt kê zone candidate từ phân tích.
2. Gán fresh / used / invalid / mitigated.
3. **Loại mọi zone không fresh.**
4. Với mỗi plan bị loại: **chọn lại vùng fresh khác** hoặc bỏ plan.
5. Chỉ sau đó mới điền `prices`, `vung_cho`, `trade_line`, `hop_luu`.

### E.4. Ghi chú trong output text

Trong `phan_tich_cham_diem` (FULL_ANALYSIS) hoặc `phan_tich_update` (INTRADAY_UPDATE), **bắt buộc**:
- Zone nào bị loại vì không fresh (used / mitigated / invalid).
- Zone fresh nào được **thay thế** (nếu có) và vì sao vùng cũ không dùng được.
- Nếu `prices` ít hơn sáng: nêu “đã lọc last_filter — chỉ giữ fresh”; không cố tạo đủ 3 plan bằng vùng đã chạm.

---

## Liên kết playbook

- Chọn vùng ban đầu: `master_trading_playbook.md` → `1.3` Bước 3, `1.4`, Bước 8.
- Update intraday: `3.2`, `3.3`.
- SnR fresh vs used: Appendix `6.3` — **last_filter siết hơn: chỉ publish fresh.**
- File này là **lớp lọc cuối**; không thay playbook nhưng **ghi đè** mọi ngoại lệ giữ vùng đã chạm.
