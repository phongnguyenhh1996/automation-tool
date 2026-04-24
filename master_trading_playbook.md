# master_trading_playbook

> Bản master duy nhất dùng để ra quyết định sau này.  
> **Nguyên tắc ưu tiên:** nếu có trùng lặp hoặc mâu thuẫn, **ưu tiên memory mới hơn, khắt khe hơn, sát backtest hơn**.  
> **Mục tiêu:** chỉ dùng 1 file này làm chuẩn tham chiếu để phân tích, cập nhật intraday, quản lý lệnh, ra plan limit, và vận hành EA/Grid.

---

## 0. HỆ THỐNG ƯU TIÊN & NGUYÊN TẮC GỐC

### 0.1. Triết lý cốt lõi
- Thị trường luôn có 2 hướng; nhiệm vụ là chuẩn bị **bias chính + plan phụ**, không đoán tuyệt đối.
- Chỉ ưu tiên **lệnh limit đẹp, hợp lưu cao, dễ khớp, dễ TP**.
- Nếu chưa đủ xác nhận thì **ĐỨNG NGOÀI** hoặc chỉ đề xuất **VÙNG CHỜ**.
- Không giữ lệnh qua đêm với day trading.
- Không đổi bias trong ngày trừ khi vi phạm filter giữ limit hoặc H1 BOS ngược.
- Mọi đề xuất entry phải rà soát lại:
  - quy tắc,
  - bài học đã lưu,
  - backtest đã lưu,
  - quản lý SL/TP/RR,
  - trạng thái trend M15 hiện tại,
  - đang thuận trend hay ngược trend.

### 0.2. Điều kiện tổng hợp để được vào lệnh
Chỉ được phép vào lệnh khi tối thiểu có đủ 3 lớp xác nhận:
1. **Cấu trúc giá đúng hướng**
2. **Order Flow / CVD đồng thuận**
3. **Footprint xác nhận mạnh**

Công thức lõi:
- **Xu hướng rõ + dòng tiền đồng thuận + footprint xác nhận mạnh → MỚI ĐƯỢC VÀO**
- **VWAP–POC hợp lưu + CVD đồng thuận + trap rõ + stacked/absorption đúng vị trí + Re-check Before Touch đủ chuẩn → MỚI VÀO**
- Thiếu 1 mắt xích quan trọng → **CHỜ** hoặc **ĐỨNG NGOÀI**

### 0.3. Thang điểm hợp lưu 100
- Có **20 điều kiện**, chia thành 5 nhóm, mỗi tiêu chí 5 điểm:
  1. Cấu trúc giá (20đ)
  2. Order Flow – CVD (20đ)
  3. Footprint (20đ)
  4. Lọc nâng cao: OI, US10Y, VIX, tin tức (20đ)
  5. Quản lý & Thực thi (20đ)

### 0.4. Ngưỡng hành động theo điểm số
- **< 70 điểm** → Không đủ hợp lưu / chỉ backup / đứng ngoài
- **70–89 điểm** → Vùng chờ, chấp nhận được nhưng chưa đẹp
- **≥ 90 điểm** → Hợp lưu mạnh, có thể limit ngay
- **100/100** → Chuẩn tuyệt đối, lệnh đẹp nhất

### 0.5. Quy tắc ra quyết định chung
- Chỉ đề xuất **LIMIT + SL + TP** nếu đủ hợp lưu cao để vào ngay.
- Nếu cần re-check mới dám vào → không đưa entry ngay, chỉ đưa **VÙNG CHỜ**.
- Ưu tiên setup:
  - dễ khớp,
  - dễ TP,
  - RR đạt chuẩn,
  - không cần sửa lại liên tục.
- Nếu hợp lưu thấp nhưng vùng tốt → đề xuất limit xa hơn hoặc chờ tín hiệu sâu hơn.
- Chỉ cho phép vào lệnh khi hợp lưu trên **80 điểm**; tuy nhiên để **limit ngay** nên hướng tới **≥ 90 điểm**.

---

## 1. [FULL_ANALYSIS]

> Dùng cho **phân tích tổng thể đầu ngày** khi nhận đủ 10 data.

### 1.1. Input chuẩn bắt buộc
Khi chạy quy trình phân tích Forex chuẩn, user phải gửi đủ **10 hình / 10 data** trong 1 cuộc trò chuyện:

#### DXY
- H4
- H1
- M15
- Footprint M15

#### Cặp chính
- H4
- H1
- M15
- M5

#### Footprint cặp chính
- M15
- M5

### 1.2. Vai trò từng khung
- **H1** = khung chủ đạo xác định bias cho cả ngày
- **M15** = chọn POI / vùng entry / plan trong ngày
- **M5** = xác nhận entry
- **Footprint M15/M5** = trap, stacked, absorption, CVD, reclaim VWAP/POC
- **DXY** = bias macro, đặc biệt quan trọng với EURUSD / GBPUSD / USDJPY
- Có thể dùng thêm:
  - TPO
  - OI
  - US10Y
  - VIX
  - tin tức
  - session liquidity
  - VWAP / POC / Value Area

### 1.3. Trình tự phân tích bắt buộc
#### Bước 1 – DXY / Macro bias
- Phân tích DXY H4/H1/M15 + footprint M15
- Xác định USD mạnh / yếu / sideway
- Check:
  - cấu trúc HH/HL hay LH/LL
  - CHoCH / BOS
  - vị trí giá với VWAP / POC
  - delta / CVD / absorption / trap
- Ứng dụng:
  - DXY tăng mạnh → ưu tiên SELL EURUSD / GBPUSD
  - DXY giảm mạnh → ưu tiên BUY EURUSD / GBPUSD
  - DXY không rõ → thận trọng / đứng ngoài / chỉ lấy setup cực mạnh

#### Bước 2 – Context & cấu trúc H1
- Xác định:
  - trend H1 hiện tại
  - giá đang ở premium / discount / equilibrium
  - POI nào mạnh nhất
  - có BOS / CHoCH hợp lệ chưa
  - valid pullback hay chưa
  - liquidity nào đang bị nhắm tới
- Áp dụng SMC / Trading Hub 3.0:
  - BOS/CHoCH phải phá swing rõ bằng nến đóng
  - valid pullback về POI hợp lệ, không phá cấu trúc chính
  - ưu tiên sweep liquidity trước khi tìm entry LTF
  - POI ưu tiên:
    - Extreme OB
    - Decisional OB
    - SCOB
    - FVG
    - flip zone
  - tránh:
    - OB đã mitigate
    - vùng rộng, yếu
    - vùng giữa range không rõ edge

#### Bước 3 – M15 chọn vùng entry / plan trong ngày
- Vẽ:
  - OB / FVG / EQ / premium / discount
  - VWAP / POC / VAH / VAL
  - HL / LH / BOS / CHoCH
  - liquidity pool / EQH / EQL / session highs-lows
- Lưu ý:
  - Không BUY tại premium
  - Không SELL tại discount
  - Không đặt limit ngay tại liquidity
  - Nếu vùng trùng liquidity → chỉ vào sau **sweep + CHoCH LTF + footprint confirm**
- Ưu tiên vùng:
  - POC + VWAP + OB/FVG hợp lưu
  - VAL → POC cho BUY
  - POC → VAH cho SELL
  - vùng edge of balance / LVN / fresh delta
- Tránh:
  - vùng HVN dày nếu kỳ vọng breakout
  - vùng volume trống nhưng thiếu confirm
  - vùng đã bị mitigate nhiều lần

#### Bước 4 – M5 xác nhận entry module
- Dùng M5 để xem:
  - CHoCH / BOS đúng hướng
  - sweep liquidity
  - internal structure
  - ping pong nếu đang range
  - breakout false / spring / upthrust / reclaim
- Entry module ưu tiên:
  - Sweep + CHoCH
  - SCOB
  - Single Candle Mitigation
  - Flip zone
  - retest POC / edge of balance sau breakout

#### Bước 5 – Footprint confirm (M15/M5)
Phải check cực kỹ:
- Trap rõ tại đỉnh / đáy
- Volume spike đúng vị trí
- CVD đồng thuận ít nhất **≥ 3 nến**
- stacked BID/ASK ít nhất **2 nến liên tiếp**, **RL ≥ 4.0x**
- stacked phải nằm **sát HL / VWAP / vùng vào**, không tính stacked lạc vùng
- absorption đúng vị trí
- reclaim VWAP / POC rõ ràng
- follow-through ngay sau trap

#### Bước 6 – Lọc nâng cao
Bắt buộc rà thêm nếu có dữ liệu:
- OI
- US10Y
- VIX
- tin tức đỏ trong vòng 30 phút
- timing phiên
- volume profile / TPO / VA shift / POC shift
- fake-break window 18h–19h
- London / New York liquidity

#### Bước 7 – Chấm điểm 100 & quyết định
- Tự chấm plan theo thang 100
- Kết luận:
  - Main plan
  - Backup plan
  - Scalp plan (nếu là XAUUSD)
  - EA mode (nếu cần)
- Chỉ ra rõ:
  - trend M15 hiện tại
  - đang trade thuận trend hay ngược trend
  - plan nào đủ chuẩn limit ngay
  - plan nào chỉ là vùng chờ

### 1.4. Bộ lọc bắt buộc trước khi đề xuất entry
#### A. Cấu trúc giá
- H1 – M15 phải rõ xu hướng
- Entry tại:
  - HL / LH
  - EQ
  - discount / premium đúng hướng
  - OB / FVG / SCOB đúng vị trí
- Có BOS hoặc CHoCH gần nhất xác nhận
- Không trade ngược xu hướng
- Không BUY tại premium
- Không SELL tại discount
- Không vào khi giá nằm giữa range mà không có POI đẹp

#### B. Dòng tiền / Order Flow
- CVD đồng thuận **≥ 3 nến**
- BUY:
  - CD không âm sâu bất thường
  - tốt nhất đang đi lên / bật lên rõ
- SELL:
  - CD không dương mạnh ngược hướng
  - tốt nhất đang đi xuống / giảm rõ
- Không vào nếu:
  - CD đi ngược hướng trade
  - CD bất ổn
  - giá và CD lệch pha mà không có trap xác nhận

#### C. Footprint xác nhận
Phải có ít nhất 1 bộ mạnh:
- **stacked BID/ASK ≥ 2 nến liên tiếp, RL ≥ 4.0x**
- hoặc **trap + absorption + volume spike rõ**
- giá phải:
  - đóng trên POC / reclaim VWAP khi BUY
  - đóng dưới POC / mất VWAP khi SELL

#### D. Checklist đảo chiều thị trường (ít nhất 3/4)
1. CVD đảo chiều rõ với momentum  
2. Trap + volume spike + phản ứng ngược chiều mạnh  
3. stacked BID/ASK ≥ 2 nến, RL ≥ 4.0x, đúng vị trí  
4. Giá reclaim lại VWAP/POC rõ  

Nếu:
- trap không follow-through
- CD không đảo chiều
- không có stacked rõ
- giá không reclaim VWAP/POC  
→ loại bỏ lệnh

### 1.5. Bộ xác nhận thay thế khi KHÔNG có stacked
Có thể chấp nhận nếu đủ **≥ 3/4**:
1. Vị trí premium/discount đúng hướng + nằm ở vùng chờ đã đánh dấu
2. CVD đảo chiều mạnh (**±250–300** đơn vị) trong 1–3 nến
3. Nến breakout theo hướng trade kèm volume spike
4. Phá VWAP và không reclaim ngược trong 2–3 nến

Áp dụng mạnh cho SELL:
- premium / EQH / vùng chờ
- trap BUY hoặc quét đỉnh
- breakdown mạnh
- seller takeover rõ

### 1.6. Re-check Before Touch (bắt buộc)
Trong **2–3 pip / giá trước entry**, phải kiểm lại:
1. CVD đồng thuận ≥ 3 nến
2. VWAP/POC không dịch ngược > 3–5 pip
3. Có stacked hoặc absorption sát HL / VWAP / vùng vào
4. Có volume spike xác nhận

**Thiếu 1/4 → HUỶ LIMIT**

### 1.7. Filter giữ / hủy limit trong ngày
GIỮ LIMIT khi:
- H1 chưa BOS ngược
- M15 chưa CHoCH ngược
- CVD không đảo ≥ 3 nến
- VWAP/POC có dịch nhưng:
  + Không volume spike
  + Giá không hold ≥ 2–3 nến
  + Không có footprint đảo chiều

HUỶ LIMIT khi:
- H1 BOS hoặc M15 CHoCH ngược
- CVD đảo ≥ 3 nến mạnh
- VWAP/POC shift >5 pip + giá hold + footprint đảo chiều
- Có full combo: trap + stacked + CD đảo + follow-through

Vi phạm **1 điều kiện** → **HUỶ LIMIT**

### 1.8. Timing & phiên giao dịch
Chỉ ưu tiên trade trong giờ vàng:
- 13h
- 14h
- 16h30
- 19h
- 20h30
- 22h
(GMT+7)

Loại bỏ / hạn chế mạnh:
- ngoài giờ vàng
- sát mở phiên Âu/Mỹ mà chưa có volume xác nhận
- 30 phút trước / sau tin đỏ lớn
- giữ limit qua tin lớn như CPI / FOMC

### 1.9. Output chuẩn của [FULL_ANALYSIS]
Khi trả kết quả phải có đủ:
1. Bias H1
2. Trend M15 hiện tại
3. DXY ủng hộ hay ngược
4. Vùng POI chính
5. Main plan
6. Backup plan
7. Scalp plan (nếu XAUUSD)
8. Điểm số từng plan /100
9. Kết luận:
   - limit ngay / vùng chờ / đứng ngoài

### 1.10. Quy tắc riêng theo cặp
#### XAUUSD
- Cặp chính
- Mặc định:
  - **1 plan chính**
  - **2 plan phụ / scalp trong ngày**
- SELL tại đỉnh phân phối, BUY tại đáy tích lũy
- Entry ưu tiên ở:
  - VWAP–POC
  - VAH/VAL
  - premium/discount
  - POC shift / VA shift
- SL thường:
  - 6–8 giá cho scalp confirm mạnh
  - anti-sweep khắt khe hơn nếu plan intraday lớn
- TP1:
  - VWAP / HL gần nhất
  - RR tối thiểu ≥ 1:1.6
- Thêm các điểm có tính đảo chiều cao để scalp **5–7 giá**
- Không BUY khi dưới VWAP nhiều ngày mà chưa reclaim
- Không SELL nếu dưới POC mà không có trap / volume
- Tránh sideway > 8h không volume spike
- Ưu tiên phiên Âu–Mỹ

#### EURUSD
- Chỉ plan chính
- Bias H1 phải trùng DXY
- Ưu tiên risk 2–3%
- SL rất nhỏ 3–5 pip nếu setup cực đẹp theo Elliott/Wyckoff/Market Profile
- RR mục tiêu cao hơn, có thể 1:10 trong setup chuẩn đặc biệt
- Ưu tiên VAL–POC có absorption / spring / upthrust rõ
- Không scalp nếu plan không đủ rõ

#### USDJPY
- Chỉ plan chính
- Với SELL:
  - SL phải **anti-sweep riêng**
  - giấu sau liquidity pool (EQH / stop cluster) + buffer 3–5 pip
  - có thể dời SL xa thêm 10 pip và giảm lot cụm để giữ risk
- Không giữ limit qua tin đỏ lớn

#### GBPJPY
- Chỉ plan chính
- Vẫn áp dụng khung cấu trúc + footprint + order flow như cặp phụ
- Không scalp nếu không có lợi thế rõ

### 1.11. Quản lý vốn / RR / lot
Có một số mốc từ file cũ, nhưng theo memory cần ưu tiên như sau:
- Lệnh phải đủ đẹp để limit không cần re-check thêm ngoài bộ lọc đã định
- Entry đầu tiên:
  - tối thiểu RR **≥ 1:1**
  - chuẩn ưu tiên RR **≥ 1:1.6**
- TP1:
  - dễ đạt
  - đặt tại VWAP / HL gần nhất / vùng volume trống
- TP2:
  - xa hơn tại premium/discount HTF hoặc OB/FVG lớn
- Nếu TP1 quá xa hoặc RR không chuẩn → tự điều chỉnh lại
- SL luôn anti-sweep:
  - sau liquidity
  - buffer 3–5 pip / giá
- Với forex:
  - SL thường 6–8 pip nếu rất đẹp
  - có thể 7–12 pip ở cặp phụ
- Với XAUUSD:
  - scalp 5–7 giá
  - kế hoạch intraday cần anti-sweep theo cấu trúc thật
- Mọi chốt plan đều phải:
  - **✅ check lại SL và TP**

### 1.12. EA Grid / Smart Control (bắt buộc sau FULL_ANALYSIS)
Sau khi hoàn tất phân tích Hybrid Dual Plan, phải đề xuất:
- chế độ EA:
  - Buy
  - Sell
  - Both
- vùng bật
- vùng tắt / vùng giảm hoạt động
- vùng flip bias
- logic Smart Control:
  - hoạt động liên tục theo bias chính
  - tự chuyển mode khi giá chạm plan phụ
  - dừng khi H1 BOS ngược hoặc M15 CHoCH ngược

---

## 2. [INTRADAY_ALERT]

> Dùng khi **giá chạm vùng chờ**, hoặc cần đánh giá lại ngay sau khi chạm vùng đã đánh dấu trước đó.  
> Input chính: **Footprint M5**, có thể kèm footprint M15 nếu có.

### 2.1. Mục tiêu của block này
- Không phân tích lại toàn bộ ngày từ đầu
- Tập trung:
  - vùng chờ đã có
  - phản ứng thực tế của giá tại vùng
  - footprint M5 để chốt:
    - vào ngay
    - chờ thêm
    - hủy vùng

### 2.2. Những gì phải kiểm khi giá chạm vùng
1. Giá đang chạm đúng vùng chờ hay mới chỉ gần vùng
2. Vùng đó có còn hợp lệ không:
   - H1 còn bias cũ không
   - M15 có CHoCH/BOS ngược chưa
   - VWAP/POC có dịch mạnh chưa
3. Phản ứng tại vùng:
   - trap
   - absorption
   - volume spike
   - reclaim / lose VWAP
   - stacked đúng vị trí
   - CVD đảo chiều đủ 3 nến chưa

### 2.3. Checklist quyết định entry tại chỗ
#### A. Nếu BUY
- Vị trí ở discount / HL / demand / VAL / POC hỗ trợ
- Có trap SELL hoặc hấp thụ rõ tại đáy
- CVD bật lên ≥ 3 nến
- stacked BID ≥ 2 nến, RL ≥ 4.0x, đúng vị trí
- Giá reclaim VWAP / POC và giữ được
- Có nến xác nhận sau trap, không chỉ là chớm reclaim

#### B. Nếu SELL
- Vị trí ở premium / EQH / supply / VAH / POC kháng cự
- Có trap BUY hoặc absorption ở đỉnh
- CVD giảm ≥ 3 nến
- stacked ASK ≥ 2 nến, RL ≥ 4.0x, đúng vị trí
- Giá phá VWAP / mất VWAP và không reclaim nhanh
- Có nến breakdown xác nhận + volume spike

### 2.4. Bộ loại bỏ ngay
LOẠI khi có ≥ 2–3 yếu tố sau:

- Không có stacked + không trap + không absorption
- CVD ngược ≥ 3 nến + momentum mạnh
- Giá dưới VWAP/POC + không reclaim (≥ 2 nến + volume)
- Trap nhỏ + không follow-through + CD không đồng thuận
- Stacked sai vị trí (không sát HL/VWAP)
- Gần phiên Âu/Mỹ + không có volume spike
- M5 CHoCH + follow-through + CD đồng thuận
- VWAP/POC shift >5 pip + giá hold + footprint đảo chiều

### 2.5. Trường hợp không có stacked
Cho phép vào nếu đủ ≥ 3/4:
1. vị trí đúng premium/discount + vùng chờ
2. CVD đảo chiều mạnh ±250–300 trong 1–3 nến
3. có nến breakout/breakdown đúng hướng + volume spike
4. phá VWAP và không reclaim lại 2–3 nến

### 2.6. Output chuẩn của [INTRADAY_ALERT]
Phải trả lời theo logic:
1. Vùng chờ còn hợp lệ hay đã mất hiệu lực
2. Trend M5 hiện tại
3. M15 đang thuận hay ngược vùng entry này
4. Footprint xác nhận gì
5. Điểm hợp lưu /100
6. Kết luận 1 trong 3:
   - **ENTRY NGAY**
   - **CHỜ THÊM**
   - **HUỶ VÙNG**
7. Nếu ENTRY NGAY:
   - Entry
   - SL anti-sweep
   - TP1 dễ đạt
   - TP2 nếu có
   - RR

### 2.7. Mẫu logic hành động
- Nếu đủ 3 lớp:
  - cấu trúc đúng
  - CVD đồng thuận
  - footprint confirm mạnh  
  → có thể đề xuất **limit / market logic tại vùng**
- Nếu chỉ có vị trí đẹp nhưng footprint yếu  
  → chỉ **CHỜ**
- Nếu đã mất cấu trúc / mất VWAP / CD ngược  
  → **HUỶ VÙNG**

---

## 3. [INTRADAY_UPDATE]

> Dùng cho cập nhật định kỳ trong ngày, ví dụ **14h / 21h**, sau [FULL_ANALYSIS] hoặc nối tiếp sau [INTRADAY_UPDATE] trước đó.  
> Input chính: **2 footprint hiện tại M15/M5**.

### 3.1. Mục tiêu
- Không dựng lại toàn bộ phân tích như buổi sáng nếu không cần
- Kiểm tra xem bias sáng còn đúng không
- Kiểm tra các vùng chờ / limit có còn giữ được không
- Cập nhật:
  - plan nào còn hiệu lực
  - plan nào phải hủy
  - plan nào chuyển thành scalp / backup
  - EA mode có cần đổi không

### 3.2. Những gì phải rà ở mỗi lần update
1. H1 có BOS ngược chưa
2. M15 có CHoCH ngược chưa
3. POC/VWAP đã dịch > 3–5 pip chưa
4. CVD đã đảo chiều ngược bias ≥ 3 nến chưa
5. giá có đang nằm gần vùng chờ không
6. footprint M15/M5 đang ủng hộ bias cũ hay không
7. timing hiện tại có còn đẹp không
8. có tin tức nào sắp tới không
9. có dấu hiệu trap ngược bias không

### 3.3. Quy tắc xử lý limit theo update
#### Giữ limit nếu:
- H1 chưa BOS ngược
- M15 chưa CHoCH ngược
- POC/VWAP không dịch xa
- CVD chưa đảo chiều mạnh ngược bias
- vùng chưa bị phản ứng xấu / không bị consume quá nhiều

#### Hủy limit nếu:
- vi phạm 1 trong 4 filter trên
- trước khi chạm limit mà cấu trúc LTF đảo chiều
- CVD đảo chiều mạnh và giữ ≥ 3 nến
- giá phá VWAP và không reclaim theo hướng bias cũ
- có tin đỏ lớn sắp tới

### 3.4. Khi update mà giá đã đi được một đoạn
- Nếu plan chính đã bỏ lỡ:
  - không chase
  - chỉ tìm re-entry nếu có pullback chuẩn vào POI mới
- Nếu TP gần vùng mạnh:
  - ưu tiên chốt nhanh
- Nếu footprint cho thấy lực suy yếu trước TP:
  - chốt non hoặc dời BE
- Nếu đầu phiên Mỹ / London có trap ngược bias:
  - thu hẹp kỳ vọng TP

### 3.5. Output chuẩn của [INTRADAY_UPDATE]
1. Bias sáng còn giữ hay không
2. Trend M15 hiện tại
3. Footprint M15/M5 đang nghiêng bên nào
4. Các limit cũ:
   - giữ / hủy / dời
5. Có xuất hiện setup mới không
6. EA mode giữ hay đổi
7. Kết luận:
   - tiếp tục chờ
   - active vùng cũ
   - đổi sang vùng mới
   - đứng ngoài

### 3.6. Mốc thời gian vận hành
Theo framework cũ:
- XAUUSD: thường check 14h – 19h/20h30 – 23h
- Cặp phụ: 14h và 23h
Nhưng theo memory mới:
- ưu tiên update tại các mốc workflow như 14h30 / 21h hoặc theo lịch user đang chạy
- vẫn phải tuân thủ giờ vàng và tránh tin đỏ

---

## 4. [TRADE_MANAGEMENT]

> Dùng sau khi đã có lệnh, hoặc để quản lý sau entry / đang chạy lệnh.

### 4.1. Nguyên tắc chung
- Quản lý phải đủ linh hoạt để không bị quét oan, nhưng vẫn giữ an toàn
- Không cứng nhắc dời SL quá sớm nếu market còn đang re-test hợp lý
- Tuy nhiên, nếu mất xác nhận quan trọng ở 1–3 nến đầu thì phải hành động nhanh

### 4.2. Bộ kiểm tra sau entry (1–3 nến đầu)
Phải duy trì ít nhất **3/4** yếu tố:
1. CVD giữ nguyên hoặc tiếp tục đồng thuận
2. Giá giữ trên/dưới VWAP đúng hướng
3. stacked BID/ASK vẫn còn hiệu lực hoặc absorption vẫn giữ
4. Không có nến phá HL/LH gần nhất ngược hướng

Nếu mất nhiều xác nhận:
- thoát sớm
- hoặc cắt 50% vị thế

### 4.3. Giữ lệnh nếu có ≥ 2 dấu hiệu tốt
- CVD tiếp tục đi đúng hướng
- Nến rời vùng entry nhanh
- Footprint có imbalance thuận hướng tại vùng giữ
- Có absorption trap đúng hướng
- Volume follow-through tốt
- Giá vượt đỉnh/đáy gần nhất theo hướng trade
- VWAP / EMA / POC giữ đúng vai trò hỗ trợ/kháng cự

### 4.4. Thoát lệnh ngay nếu có ≥ 1 dấu hiệu xấu mạnh
- CVD đảo chiều mạnh sau entry
- Không có follow-through, giá chỉ giằng co
- Footprint không có buyer/seller chủ động như kỳ vọng
- xuất hiện imbalance ngược mạnh
- nến ngược thân lớn ngay sau entry
- giá mất VWAP / mất POC
- chạm SL quá nhanh trong 1 nến mà không có pullback
- 2 nến đầu đi ngang hoặc hồi > 50% nến breakout

### 4.5. Quy tắc dời SL / chốt lời
**Quản lý SL theo cấu trúc (không dùng mốc giá cố định)**

1. Giữ SL gốc (anti-sweep) cho tới khi thị trường xác nhận vùng bảo vệ mới.

2. Chỉ dời SL khi có ít nhất 1 trong các điều kiện:
   - Xuất hiện HL/LH mới rõ trên M5/M15.
   - Giá reclaim và giữ VWAP kèm follow-through.
   - Xuất hiện defended zone (stacked BID/ASK hoặc absorption rõ tại HL/VWAP).

3. SL được dời theo vùng:
   - **BUY**: đặt dưới HL mới nhất + buffer 2–3 giá.
   - **SELL**: đặt trên LH mới nhất + buffer 2–3 giá.

4. Chỉ đưa SL về BE khi:
   - Giá đã chạy qua vùng TP1 tiềm năng.
   - Hoặc có ít nhất 2 nhịp HL/LH cùng hướng.
   - Hoặc CVD đồng thuận ≥ 3 nến sau entry.

5. Không dời SL chỉ vì đạt số giá lợi nhuận.

### 4.6. Quy tắc TP
**Quản lý TP theo vùng thị trường**

TP1 (50% khối lượng):
- VWAP / HL/LH gần nhất / LVN gần nhất.
- Mục tiêu: dễ đạt, RR ≥ 1:1.6.

TP2 (30% khối lượng):
- POC / VAH / VAL / OB/FVG đối diện.
- Mục tiêu: vùng phản ứng mạnh.

Runner (20% khối lượng):
- Giữ theo cấu trúc nếu:
  - CVD chưa đảo chiều.
  - Giá chưa mất VWAP.
  - Chưa xuất hiện trap ngược rõ.

Lưu ý:
- Nếu gần TP1 mà CVD yếu dần → chốt phần lớn sớm.
- Nếu TP rơi vào HVN/POC → ưu tiên chốt nhanh, không tham.

### 4.7. Quản lý limit cũ trong ngày
- Nếu POC/VWAP intraday dịch xa hơn **> 3–5 pip** so với plan đầu ngày → cập nhật POI
- Nếu CVD đảo chiều mạnh ngược bias ≥ 3 nến → huỷ limit cũ
- Nếu trước khi chạm limit mà LTF đảo chiều → huỷ limit
- Chỉ giữ limit nguyên ngày nếu bias HTF + LTF còn ổn định

### 4.8. Bài học quản lý đã lưu
#### EURUSD SELL win
- Bias H1 trùng DXY
- Entry premium + OB + FVG + POC intraday
- Footprint có trap BUY + CVD giảm + volume spike
- TP gần vùng hỗ trợ mạnh thì nên chốt phần lớn
- Khi CD giảm yếu dần gần TP → chốt sớm hợp lý

#### USDJPY SELL win
- Entry tại premium + EQH + VWAP/POC
- stacked ASK + absorption + CVD giảm rõ
- breakdown và mất VWAP xác nhận seller takeover
- cần re-check before touch cực kỹ

#### XAUUSD BUY SL
- lỗi vì M5 đã CHoCH giảm trước khi khớp
- CVD giảm, giá dưới VWAP, không có stacked BID mới
- không có volume spike BUY
- vi phạm re-check before touch
- bài học: nếu M5 BOS/CHoCH ngược bias → huỷ limit ngay

### 4.9. Output chuẩn của [TRADE_MANAGEMENT]
1. Lệnh đang khỏe hay yếu
2. 1–3 nến đầu đã giữ được những gì
3. Giữ / giảm / BE / chốt phần / thoát ngay
4. TP1 / TP2 có cần chỉnh không
5. Vùng nào market đang để lại để dời SL / TP

---

## 5. APPENDIX – PAIR-SPECIFIC EXECUTION RULES

### 5.1. XAUUSD – khung mặc định
- 1 plan chính + 2 plan phụ/scalp
- scalp 5–7 giá là cấu trúc mặc định cần đề xuất thêm
- ưu tiên:
  - VWAP–POC hợp lưu
  - CD đồng thuận
  - trap rõ
  - stacked đúng vị trí
- tránh:
  - dưới VWAP nhiều ngày chưa reclaim mà BUY
  - sideway > 8h không volume spike
  - SELL khi dưới POC mà không có trap/volume

### 5.2. EURUSD – quy tắc tăng độ chọn lọc
- Bias H1 phải đi cùng DXY
- Ưu tiên setup ít nhưng cực chuẩn
- Có thể áp dụng Elliott + Wyckoff + Market Profile
- Vùng đẹp:
  - VAL–POC
  - spring / upthrust
  - absorption mạnh
- quản lý vốn chuẩn riêng:
  - risk 2–3%
  - SL 3–5 pip
  - RR lớn

### 5.3. USDJPY – anti-sweep riêng
- SELL phải giấu SL sau EQH / stop cluster + buffer 3–5 pip
- nếu cần có thể dời SL xa thêm 10 pip
- giảm lot cụm để giữ risk tổng cố định
- không giữ qua CPI/FOMC

### 5.4. GBPJPY
- chỉ lấy plan chính nếu cấu trúc + footprint thật rõ
- không cố scalp

---

## 6. APPENDIX – SMC / TPO / WYCKOFF / ORDER FLOW INTEGRATION

### 6.1. Trading Hub 3.0 – SMC
- BOS/CHoCH phải rõ
- valid pullback
- IDM / inducement
- liquidity: EQH/EQL, trendline, session highs/lows
- POI ưu tiên:
  - Extreme OB
  - Decisional OB
  - SCOB
  - flip zone
- LTF entry:
  - sweep + CHoCH
  - SCOB
  - single candle mitigation

### 6.2. TPO / Volume Profile
- BUY: VAL → POC
- SELL: POC → VAH
- ưu tiên:
  - fresh delta
  - absorption
  - price action OSB
  - trailing liquidity
- LVN → breakout dễ trend
- HVN → dễ sideway
- VA break lần 1 fail → lần 2 có thể clean break
- dùng POC shift / VA shift để đọc control

### 6.3. Wyckoff / Storyline / Malaysia SNR
- Storyline Weekly / Daily / H4 để làm bias HTF
- “Kết thúc của một cốt truyện là khởi đầu cốt truyện ngược lại”
- Phân biệt SnR fresh vs used
- Dùng session Á / London / Mỹ làm hợp lưu vùng
- Không dùng đơn lẻ làm entry chính; vẫn phải overlay footprint + CVD

### 6.4. Footprint nâng cao
- Trap mạnh = volume spike > 10k + stacked + delta đảo chiều
- CD tăng mà stacked BID giữ = buyer thật
- Giá tăng nhưng CD giảm = lực giả → dễ trap BUY
- Trap SELL mạnh / Trap BUY mạnh phải có follow-through

---

## 7. APPENDIX – EA GRID / AI PLAN EXECUTION CONTEXT

### 7.1. Sau mỗi FULL_ANALYSIS phải có
- EA mode:
  - BUY / SELL / BOTH
- vùng bật
- vùng giảm hoạt động / tắt
- vùng flip
- logic Smart Control

### 7.2. Logic đề xuất
- chạy theo bias chính
- khi chạm plan phụ thì:
  - giảm mode cũ
  - hoặc chuyển BOTH / flip tùy footprint mới
- dừng mạnh nếu:
  - H1 BOS ngược
  - M15 CHoCH ngược
  - footprint đảo hoàn toàn

---

## 8. APPENDIX – SPOT CRYPTO WORKFLOW (LƯU ĐỂ KHÔNG MẤT)

> Không phải trọng tâm file forex, nhưng đã nằm trong memory nên vẫn giữ để không bỏ sót.

### 8.1. Workflow Spot DCA
- Macro Flow:
  - USDT.D
  - BTC.D
  - TOTAL2/3
- Coin filter:
  - volume ≥ 10M
  - footprint nhạy
  - loại coin nhiễu
- cấu trúc:
  - CHoCH + HL + HH
  - OB/FVG ở discount
  - fibo 0.5–0.618
- footprint confirm:
  - trap SELL
  - CD đảo chiều ≥ 3 nến
  - stacked BID tại HL/VWAP
  - reclaim VWAP
- DCA 3 điểm:
  - 0.5 fibo
  - 0.618 fibo
  - HL extreme / POC
- 1 SL chung
- TP1 = fibo 0.618
- TP2 = fibo 1.0
- risk cố định 40$

### 8.2. Filter bổ sung trước khi confirm entry Spot
- Google Trends
- LunarCrush
- chart W1 nếu chưa có
- CoinMarketCal / Twitter / Messari
- TokenUnlocks
- CoinDesk / CoinTelegraph / The Block
- CryptoQuant / Whale Alert / Nansen
- funding rate / OI
- nếu có unlock lớn / funding quá cao / inflow xấu / tin xấu → delay hoặc loại bỏ

---

## 9. APPENDIX – CÂU MẪU OUTPUT NÊN GIỮ CỐ ĐỊNH

### 9.2. Cuối [INTRADAY_ALERT]
- Vùng chờ:
- Footprint M5:
- CVD:
- stacked / absorption:
- Kết luận: ENTRY NGAY / CHỜ / HUỶ
- Entry / SL / TP:
- Điểm số:

### 9.3. Cuối [INTRADAY_UPDATE]
- Bias sáng còn giữ hay không:
- Limit cũ:
- Setup mới:
- EA Mode:
- Kết luận:

### 9.4. Cuối [TRADE_MANAGEMENT]
- Lệnh khỏe / yếu:
- CVD:
- VWAP / POC:
- stacked / absorption:
- Hành động:
- TP / SL update:
---


### 9.5. TEMPLATE OUTPUT TÍCH HỢP (HỢP NHẤT TỪ output.md)

> Phần này được hợp nhất từ `output.md` để hệ thống chỉ cần đọc **1 file duy nhất**.  
> **Chỉ dùng cho [FULL_ANALYSIS] / Schema A** để sinh `out_chi_tiet` và `output_ngan_gon`.  
> Với `[INTRADAY_ALERT]`, `[INTRADAY_UPDATE]`, `[TRADE_MANAGEMENT]` **không dùng template dưới đây**, chỉ dùng schema JSON tương ứng.

[MASTER_OUTPUT_CONTEXT]
- Toàn bộ nội dung template dưới đây phải được generate dựa trên chính `master_trading_playbook.md`.
- Logic phân tích, checklist, filter, bài học, rule entry/quản lý → lấy từ các section chính của file này.
- Template bên dưới chỉ là **khung câu chữ cố định** cho `out_chi_tiet` và `output_ngan_gon` ở `[FULL_ANALYSIS]`.
- Nếu thiếu dữ liệu xác nhận quan trọng, đặc biệt footprint / CVD / reclaim VWAP / trend bias → không được ép ra entry.
- Nếu logic giao dịch và template có xung đột, ưu tiên **logic giao dịch trong master file**, nhưng vẫn phải giữ đúng cấu trúc nội dung của template.

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

📍 PLAN CHÍNH VÙNG CHỜ:
📍 PLAN PHỤ VÙNG CHỜ:
⚡️ SCALP VÙNG:

### 9.5.1. Quy tắc dùng template
- `out_chi_tiet` phải bám đúng phần sau marker `[OUTPUT_CHI_TIET]` và không in marker.
- `output_ngan_gon` phải bám đúng phần sau marker `[OUTPUT_NGAN_GON]` và không in marker.
- Trong `[OUTPUT_NGAN_GON]`, với từng plan bắt buộc có đủ:
  - `trade_line` tham khảo
  - `hop_luu`
  - `điều kiện vào lệnh`
- Nếu thiếu một trong ba mục trên thì output ngắn gọn được xem là chưa đạt chuẩn.

### 9.5.2. Placeholder convention
- `{symbol}` phải được thay bằng cặp đang phân tích trong lần gọi hiện tại. Ví dụ: `EURUSD`, `USDJPY`, `XAUUSD`.

### 9.5.3. Quy tắc bỏ qua mục khi không phải XAUUSD
Nếu `{symbol}` **không phải `XAUUSD`** thì trong `out_chi_tiet` phải **bỏ qua / không được sinh** các mục sau:
- `🧭 1. BỐI CẢNH VĨ MÔ (DXY)`
- `👉 KẾT LUẬN DXY:`
- `🧭 2. CẤU TRÚC {symbol}` với các mục con `🔴 H4 / 🔴 H1 / 🔴 M15`
- `🧭 4. ORDER FLOW (CVD)` gồm `M15 / M5`
- `📊 SCALP:`
- `🤖 EA GRID PLAN:`

### 9.5.4. Mối quan hệ giữa template và schema
- Phần template này chỉ phục vụ nội dung text của **Schema A**:
  - `out_chi_tiet`
  - `output_ngan_gon`
- Không dùng template này cho:
  - `Schema B / [INTRADAY_UPDATE]`
  - `Schema D / [TRADE_MANAGEMENT]`
  - `Schema E / [INTRADAY_ALERT]`
- Các mode intraday và trade management chỉ được trả đúng field JSON riêng theo system prompt, không được kéo văn bản dài từ template này vào.


## 10. KẾT LUẬN CHUẨN CỦA TOÀN HỆ THỐNG

### 10.1. Công thức vào lệnh
- **Trend H1 rõ + M15 đúng POI + M5 confirm + CVD đồng thuận ≥ 3 nến + trap/stacked/absorption đúng vị trí + reclaim VWAP/POC rõ + Re-check Before Touch đạt chuẩn → MỚI VÀO**

### 10.2. Công thức hủy lệnh
- **H1 BOS ngược hoặc M15 CHoCH ngược hoặc CVD đảo chiều ≥ 3 nến hoặc VWAP/POC dịch ngược > 3–5 pip hoặc footprint mất xác nhận → HUỶ LIMIT / ĐỨNG NGOÀI**

### 10.3. Công thức quản lý lệnh
- **Nguyên tắc quản lý lệnh (ưu tiên cấu trúc, không dùng mốc giá cố định)**
  1. KHÔNG sử dụng BE theo số giá cố định (5 giá, 10 giá...).
  2. SL chỉ được dời khi thị trường tạo vùng bảo vệ mới:
     - HL/LH rõ ràng.
     - Hoặc reclaim VWAP + giữ được.
     - Hoặc có defended zone (stacked/absorption).
  3. SL phải đặt theo vùng:
     - **BUY**: dưới HL mới + buffer 2–3 giá.
     - **SELL**: trên LH mới + buffer 2–3 giá.
  4. CHO PHÉP giá quét lại:
     - VWAP
     - POC intraday
     - HL/LH mới
     - Edge of Value Area
     
     MIỄN LÀ:
     - CVD chưa đảo chiều ≥ 3 nến.
     - Chưa phá cấu trúc.
     - Chưa có trap ngược rõ.
  5. CHỈ THOÁT LỆNH KHI:
     - Phá HL/LH bảo vệ gần nhất.
     - CVD đảo chiều ≥ 3 nến.
     - Mất VWAP và không reclaim.
     - Có trap ngược + volume spike + follow-through.
  6. QUẢN LÝ KHỐI LƯỢNG:
     - TP1: 50%
     - TP2: 30%
     - Runner: 20%
  7. MỤC TIÊU:
     - Tránh bị quét SL sớm.
     - Giữ được lệnh trend mạnh.
     - Vẫn đảm bảo an toàn vốn.

### 10.4. Tư duy cuối cùng
- Không thiếu dữ liệu thì mới ra lệnh đẹp
- Không ép ra entry
- Không trade vì sợ miss
- Đẹp mới limit
- Không đẹp thì chờ
- Không còn đẹp thì hủy
