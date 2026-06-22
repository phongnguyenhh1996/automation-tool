# last_filter

> Bộ lọc cuối cùng trước khi chốt output. Áp dụng **bắt buộc** cho `[FULL_ANALYSIS]` và `[INTRADAY_UPDATE]` — chạy sau khi đã phân tích chart/playbook, **ngay trước** khi ghi `prices` / `trade_line` / `vung_cho` vào JSON.

## Nguyên tắc gốc

**Ưu tiên vùng fresh.** Mọi plan trong `prices` nên gắn vùng **chưa bị chạm** tại thời điểm publish.

**Used zone** (đã chạm một lần) **có thể** giữ trong `prices` — nhưng **không nới lỏng** so với fresh; phải **nâng điều kiện** và chỉ hợp lệ khi có **tín hiệu mới tại lần retest**, không dùng “ký ức” phản ứng cũ.

**Mitigated / invalid** → vẫn **loại**; tìm vùng fresh khác hoặc used đủ chuẩn 4/5 (mục D) nếu cùng narrative.

Nếu không có vùng fresh hoặc used đủ chuẩn → `prices` ít hơn hoặc đứng ngoài; không cố giữ vùng yếu.

---

## A. Kiểm tra bắt buộc trước khi chốt output

Trước khi xuất `[FULL_ANALYSIS]` hoặc `[INTRADAY_UPDATE]`, **rà cho từng zone / plan** (plan_chinh, plan_phu, scalp, candidate trong `prices`):

| # | Câu hỏi | Hành động |
|---|---------|-----------|
| 1 | **Vùng đã bị chạm chưa?** | **Chưa** → fresh. **Có** → used; chuyển sang mục D (4/5). |
| 2 | **Đã mitigated / invalid chưa?** | Mitigated hoặc cấu trúc/CVD/VWAP ngược bias → **loại**; tìm vùng khác. |
| 3 | **Đủ điều kiện publish không?** | Fresh: pass playbook + hop_luu. Used: **≥ 4/5** mục D. Thiếu → loại hoặc chọn vùng khác. |

### Quy tắc tự động

```
fresh + đủ hop_luu + pass playbook
  → được publish

used + đạt ≥ 4/5 (mục D) + tín hiệu MỚI tại retest
  → được publish (ghi rõ "used + retest" trong phân tích)

used không đủ 4/5 / mitigated / invalid / chỉ dựa phản ứng cũ
  → loại khỏi prices → ưu tiên vùng fresh khác
```

---

## B. Phân loại zone: fresh / used / invalid

### Fresh
- Giá **chưa chạm** vùng (chưa test POI / `vung_cho`).
- Đưa vào `prices` nếu đủ `hop_luu` và pass playbook.

### Used (điều kiện nâng cao — mục D)
- Đã chạm **ít nhất một lần**.
- **Chỉ** publish nếu đạt **tối thiểu 4/5** checklist mục D tại **lần retest hiện tại**.
- Không limit thụ động “mù” — phải có xác nhận footprint mới; không tái dùng stacked/trap của lần test trước.

### Invalid
- Đã chạm; cấu trúc / CVD / VWAP **đi ngược** bias; hoặc breakout xa khỏi zone sau lần chạm.
- **Loại ngay**; không dùng nhánh used.

### Bảng quyết định

| Trạng thái | Trong `prices` | Điều kiện |
|------------|----------------|-----------|
| **fresh** | Có (nếu đủ hop_luu) | Playbook |
| **used** | Có **chỉ khi** ≥ 4/5 mục D | Tín hiệu **mới** tại retest |
| invalid | **Không** | Loại / vùng khác |
| mitigated | **Không** | Loại / vùng fresh khác |

---

## C. Không giữ plan nếu vùng đã mitigated

**Mitigated** = OB/FVG/EQ/POI đã được giá phản ứng qua; thanh khoản xử lý xong; test + reject không còn edge.

→ **Không** nhánh used. Hủy plan; chọn POI **fresh** khác (playbook Bước 3 M15 / Bước 8).

Khi update intraday: vùng sáng đã mitigated trước thời điểm update → bỏ hẳn khỏi `prices`.

---

## D. Used zone — checklist 4/5 (bắt buộc)

Used zone **chỉ hợp lệ** nếu đạt **tối thiểu 4 trong 5** điều kiện sau, đánh giá trên chart **tại thời điểm xuất** (retest hiện tại):

| # | Điều kiện | Pass khi |
|---|-----------|----------|
| 1 | **Lần chạm trước phản ứng nông** | Không breakout xa khỏi zone; giá không “phá hủy” narrative; reject/sweep nông, vùng còn geometry trade được. |
| 2 | **Cấu trúc vùng còn giữ** | Zone vẫn giữ HL/LH hoặc M5 **chưa** BOS ngược với ý định giữ lệnh. |
| 3 | **CVD retest mới** | CVD tại lần retest **đồng thuận lại ≥ 3 nến** theo bias; **không** chỉ dựa CVD/phản ứng của lần chạm trước. |
| 4 | **Stacked / absorption mới** | Có stacked hoặc absorption **mới** đúng vị trí (sát HL/VWAP/vùng vào); **không** tính footprint của lần test cũ. |
| 5 | **VWAP reclaim rõ** | Giá **reclaim và giữ** VWAP (BUY) hoặc **mất VWAP và giữ** dưới (SELL) — **không** chấp nhận chỉ chớm/chạm VWAP rồi mất lại. |

**Ngắn gọn:** used zone chỉ hợp lệ nếu có **tín hiệu mới**; cấm publish dựa trên “ký ức” cú phản ứng cũ.

Trong `phan_tich_cham_diem` / `phan_tich_update`: ghi rõ used zone nào pass (liệt kê 4/5 điều kiện đạt) và điều kiện nào fail nếu vẫn giữ sát ngưỡng.

---

## E. Quy tắc xuất plan (`prices`) sau lọc

### E.1. Trước thời điểm publish
- Mitigated / invalid → không publish.
- Used không đủ 4/5 → không publish tại vùng đó.
- Ưu tiên fresh; used chỉ khi không có fresh đủ chất lượng **và** used đạt chuẩn nâng cao.

### E.2. Nội dung `prices` được phép

| Loại | Điều kiện vào `prices` |
|------|-------------------------|
| **fresh** | Chưa chạm + hop_luu + playbook |
| **used** | Đã chạm + **≥ 4/5 mục D** + tín hiệu mới |

**Cấm** trong `prices`:
- used < 4/5 hoặc chỉ dựa phản ứng cũ;
- mitigated / invalid;
- premium/discount sai hướng (playbook 1.3 Bước 3).

### E.3. Thứ tự thao tác khi chốt JSON
1. Liệt kê zone candidate.
2. Gán fresh / used / invalid / mitigated.
3. Loại mitigated / invalid.
4. Với **used**: chấm 4/5 mục D.
5. Với **fresh**: xác nhận chưa chạm + hop_luu.
6. Điền `prices`, `vung_cho`, `trade_line`, `hop_luu`.

### E.4. Ghi chú trong output text

Trong `phan_tich_cham_diem` / `phan_tich_update`, **bắt buộc**:
- Zone **fresh** vs **used (retest 4/5)** — nêu điểm đạt/không đạt.
- Zone bị loại và lý do (mitigated, invalid, used thiếu điều kiện).
- Không cố đủ 3 plan bằng vùng used yếu.

---

## Liên kết playbook

- Chọn vùng: `master_trading_playbook.md` → `1.3` Bước 3, `1.4`, Bước 8.
- Chạm vùng / entry tại chỗ: `2.2`, `2.3`, `2.4`.
- Update intraday: `3.2`, `3.3`.
- SnR fresh vs used: Appendix `6.3` — **last_filter: used chỉ khi 4/5 + tín hiệu mới.**
