# FULL_ANALYSIS — 8 Bước Phân Tích

Tóm tắt từ `master_trading_playbook.md` §1.3. Agent phải đọc section đầy đủ trước khi chấm điểm.

## Bước 1 — DXY / Macro bias

- DXY H4, H1, M15 (TradingView) + footprint M15 (GoCharting CSV + GC footprint nếu DXY)
- Xác định USD mạnh / yếu / sideway
- Check: HH/HL vs LH/LL, CHoCH/BOS, VWAP/POC, CVD/delta/absorption/trap
- Map sang main pair: DXY tăng → ưu tiên SELL XAUUSD / BUY USDJPY (v.v.)

## Bước 2 — Context H4, H1 (main pair)

- Trend H4/H1, premium/discount/equilibrium
- POI mạnh nhất, BOS/CHoCH hợp lệ, valid pullback
- Liquidity đang bị nhắm
- SMC: sweep liquidity trước entry LTF; ưu tiên Extreme/Decisional OB, FVG, flip zone

## Bước 3 — M15 chọn vùng / plan

- OB/FVG/EQ, VWAP/POC/VAH/VAL, HL/LH, session liquidity
- **Không BUY tại premium; không SELL tại discount**
- Không limit ngay tại liquidity — chờ sweep + CHoCH LTF + footprint confirm
- Ưu tiên POC+VWAP+OB hợp lưu

## Bước 4 — M5 xác nhận entry

- CHoCH/BOS đúng hướng, sweep, internal structure
- Entry module: Sweep+CHoCH, SCOB, flip zone, retest POC

## Bước 5 — Footprint confirm (M15/M5)

Bắt buộc check:
- Trap tại đỉnh/đáy + volume spike
- CVD đồng thuận **≥ 3 nến**
- Stacked BID/ASK **≥ 2 nến**, **RL ≥ 4.0x**, sát HL/VWAP/vùng vào
- Absorption đúng vị trí
- Reclaim VWAP/POC rõ

## Bước 6 — Lọc nâng cao

- Session timing, tin đỏ 30 phút, fake-break 18h–19h
- OI, US10Y, VIX nếu có data

## Bước 7 — Chấm điểm 100 (mục 0.3)

4 nhóm điểm mỗi plan:

| Nhóm | Max | Nội dung |
|------|-----|----------|
| Cấu trúc giá | 30 | Trend, POI, premium/discount, sweep |
| Order Flow – CVD | 25 | CVD M15/M5, delta shift, phân kỳ |
| Footprint | 25 | Trap, stacked, absorption, POC/VWAP |
| Quản lý & Thực thi | 20 | RR, SL placement, session |

- `hop_luu > 75` mới xét entry chính
- XAUUSD: `plan_chinh`, `plan_phu`, `scalp`
- Non-XAUUSD: có thể bỏ scalp

## Bước 8 — Điều kiện limit

- Plan đủ chuẩn → `prices[]` + `trade_line`
- `intraday_hanh_dong`: `"VÀO LỆNH"` nếu vào ngay; `"chờ"` nếu chờ vùng; `"loại"` nếu đứng ngoài
- Nếu phải re-check liên tục → tìm vùng mới xa hơn

## Bước 9 — In kết quả (bắt buộc ở bước cuối)

1. **In `phan_tich_cham_diem`** ra chat dạng markdown đầy đủ (xem format bên dưới) — user phải đọc được phân tích chấm điểm trực tiếp, không chỉ nằm trong JSON.
2. Sau đó gửi Schema A JSON (`context`, `phan_tich_cham_diem`, `output_ngan_gon`, `prices`, …).

## Format `phan_tich_cham_diem`

Mỗi plan:
- Tiêu đề: `📍 PLAN CHÍNH — 78/100`
- `🏷️ Trạng thái vùng:` fresh / used / mitigated / invalid
- 4 nhóm điểm với emoji (`🧭`, `💧`, `👣`, `🛡️`)
- Order Flow + Footprint: bắt buộc `→ Số liệu:` rồi `→ Phân tích chấm điểm:`
- Kết: `✅ Tổng plan_chinh: X/100`
- Cuối toàn bộ: `📊 ĐÁNH GIÁ DỮ LIỆU ĐẦU VÀO`
