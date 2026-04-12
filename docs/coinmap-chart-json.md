# Hướng dẫn đọc file JSON export Coinmap (chart API)

Tài liệu này mô tả **cấu trúc và ý nghĩa từng field** trong file JSON do tool automation ghi ra (một file mỗi lần chụp chart, cùng tên stem với ảnh PNG). Mục tiêu: **bất kỳ mô hình AI nào** cũng có thể phân tích OHLC, order flow và VWAP **chỉ từ JSON**, không cần xem ảnh biểu đồ.

---

## 1. Bối cảnh file

| Thuộc tính | Ý nghĩa |
|------------|---------|
| **Nguồn** | **Mặc định (`api_data_export.mode: bearer_request`):** bắt Bearer từ trình duyệt rồi gọi cm-api bằng httpx — 3 endpoint song song mỗi shot (`bearer_http_parallel`, mặc định bật); API-only nhiều bước: các shot `capture_plan` có thể song song (`bearer_parallel_max_concurrency`). `bearer_skip_chart_ui: true` (mặc định trong `coinmap.yaml`) = không mở sidebar multi-shot Coinmap. **`network_capture`:** bắt response thật từ gateway khi chart load; `network_capture_max_responses_per_endpoint`, `merge_repeated_endpoint_responses`, v.v. **`bearer_request` + `bearer_skip_chart_ui: false`:** UI multi-shot tuần tự như `network_capture`, sau mỗi bước gọi API (httpx song song nếu bật parallel). |
| **Tên file** | `{stamp}_coinmap_{SYMBOL}_{interval}.json` (ví dụ `20260329_164526_coinmap_XAUUSD_5m.json`). |
| **Không bọc lỗi HTTP** | Mỗi khóa API chứa **body JSON đã parse** (mảng/object) hoặc `null` nếu không bắt được response. |
| **Đúng khung thời gian** | Khi ghi file, tool **chỉ giữ** các phần tử có `i` trùng `interval` của shot (và `s` trùng `symbol` watchlist nếu có). Nếu sau lọc strict không còn dòng và `relax_symbol_filter_if_empty: true`, tool thử lọc **chỉ theo `i`** (cùng khung thời gian). |
| **query_template (bearer_request / request)** | Placeholder: `{symbol}` (mã watchlist từng bước, ví dụ `USDINDEX`), `{export_symbol}` (nhãn file như `DXY` nếu có; không có thì bằng `{symbol}`), `{interval}`, `{watchlist_category}`, `{resolution}`, `{from_ms}` / `{to_ms}`, `{countback}`. **`{main_symbol}`** = mã active từ CLI (`--main-symbol`) — **cùng một giá trị cho mọi shot**; nếu `query_template.symbol` dùng `{main_symbol}` trong khi `capture_plan` có DXY/USDINDEX và main là XAUUSD, API sẽ trả dữ liệu XAUUSD cho mọi file (sai nghĩa). Multi-shot nhiều công cụ: dùng **`symbol: "{symbol}"`** (hoặc đúng mã gateway). File thử: `config/coinmap_bearer_test.yaml`; full: `config/coinmap_bearer_full.yaml`. |

### Các khóa ở root

| Khóa | Kiểu | Mô tả |
|------|------|--------|
| `generated_at` | string (ISO 8601, UTC) | Thời điểm tool ghi file. |
| `stamp` | string | Mã chụp batch (trùng prefix với PNG). |
| `symbol` | string | Nhãn hiển thị/ghi file (thường trùng TradingView; ví dụ `DXY` khi watchlist Coinmap là `USDINDEX` và cấu hình `export_symbol: DXY`). |
| `coinmap_symbol` | string \| omitted | Chỉ có khi `symbol` khác mã watchlist: mã thật trên Coinmap (ví dụ `USDINDEX`). |
| `interval` | string | Khung thời gian (ví dụ `5m`, `15m`, `1h`). |
| `watchlist_category` | string \| null | Danh mục watchlist nếu có. |
| `getcandlehistory` | array \| null | Lịch sử nến + chỉ số tổng hợp. |
| `getorderflowhistory` | array \| null | Order flow theo **mức giá** trong từng nến. |
| `getindicatorsvwap` | array \| null | Chuỗi điểm **VWAP + dải độ lệch chuẩn**. |

Ba khóa API tương ứng endpoint:

- `getcandlehistory` → `/getcandlehistory`
- `getorderflowhistory` → `/getorderflowhistory`
- `getindicatorsvwap` → `/getindicatorsvwap`

---

## 2. Thời gian và thứ tự nến

- **`t`**: Unix timestamp **mili giây (UTC)**, **mở đầu** nến (open time).
- **`ct`**: Unix timestamp **mili giây (UTC)**, **kết thúc** nến (close time), thường `t + interval - 1ms`.

Trong `getcandlehistory`, thứ tự phần tử trong mảng thường là **nến mới → cũ** (nến đầu mảng = bar gần nhất với thời điểm chụp). Khi phân tích chuỗi thời gian, nên **sắp xếp theo `t` tăng dần** nếu cần trình tự thời gian chuẩn.

---

## 3. `getcandlehistory` — từng field trong mỗi phần tử

Mỗi phần tử là **một nến (bar)**.

| Field | Kiểu (điển hình) | Ý nghĩa |
|-------|------------------|---------|
| `tz` | string | Múi giờ (`"UTC"`). |
| `a` | string | Thường rỗng; có thể là account/context. |
| `s` | string | Symbol. |
| `i` | string | Interval (trùng `interval` ở root). |
| `t` | number | Open time (ms). |
| `ct` | number | Close time (ms). |
| `o`, `h`, `l`, `c` | number | Open, High, Low, Close — **giá OHLC**. |
| `v` | number | **Tổng volume** trong nến (đơn vị theo sàn). |
| `bv` | number | **Buy volume** tổng hợp trong nến. |
| `sv` | number | **Sell volume** tổng hợp trong nến. |
| `d` | number | **Delta** = imbalance buy vs sell: trên dữ liệu đã kiểm tra, **`d` = `bv - sv`**. |
| `dMax` | number | Delta **tối đa** đạt được trong nến (running max của imbalance). |
| `dMin` | number | Delta **tối thiểu** trong nến. |
| `n` | number | Thường là số **mức giá / tick bins** hoặc độ phân giải liên quan order flow (số nguyên nhỏ, ví dụ 70–80). |
| `q`, `bq` | number | Có thể là các loại volume phụ (ví dụ quote); nhiều symbol hiển thị `0`. |

**Gợi ý phân tích không cần chart:** vẽ OHLC từ `o,h,l,c`; histogram Delta từ `d`; so sánh `bv`/`sv`; phát hiện nến mạnh qua `v` và độ dốc `c` vs `o`.

---

## 4. `getorderflowhistory` — footprint theo giá

Mỗi phần tử tương ứng **một nến** (cùng `t` với `getcandlehistory` khi export đầy đủ).

### Metadata mỗi bar order flow

| Field | Ý nghĩa |
|-------|---------|
| `tz`, `a`, `s`, `i` | Giống nến. |
| `t`, `ct` | Trùng ý nghĩa với nến cùng thời điểm. |
| `f`, `ls` | Cờ / trạng thái (số nguyên; phụ thuộc backend). |
| `tv` | Có thể là **tick volume** hoặc tham số tổng hợp (số thực nhỏ). |

### Mảng `aggs` — volume theo mức giá

Mỗi phần tử trong `aggs` là một **mức giá** trong nến:

| Field | Ý nghĩa |
|-------|---------|
| `tp` | **Trade price** — mức giá (tick price). |
| `v` | Volume tại mức đó. |
| `bv` | Buy volume tại mức đó. |
| `sv` | Sell volume tại mức đó. |

**Quan hệ đã xác minh trên dữ liệu mẫu:**

1. **Khóa thời gian:** Số phần tử order flow **bằng** số nến; chuỗi **`t` khớp 1-1** với `getcandlehistory` (cùng thứ tự trong file).
2. **Delta:** Với mỗi nến,  
   \(\sum_{\text{aggs}} (bv - sv) =\) field **`d`** trên nến tương ứng.  
   → Order flow **giải thích** Delta: tổng imbalance theo từng mức giá = Delta tổng hợp trên chart.
3. **Volume:** \(\sum_{\text{aggs}} v\) **gần như** luôn bằng **`v`** trên nến; có thể có **rất ít** nến lệch (volume ngoài các bậc giá trong `aggs`, làm tròn, hoặc khác biệt pipeline).

**Cách đọc không cần ảnh:** Với mỗi `t`, join nến ↔ order flow; trong order flow, sắp `aggs` theo `tp` để thấy **hình dạng footprint** (chồng buy/sell theo giá).

---

## 5. `getindicatorsvwap` — VWAP và dải band

Mỗi phần tử là **một mốc thời gian** có snapshot chỉ báo VWAP (không nhất thiết trùng số lượng với số nến trong cửa sổ hiển thị).

| Field | Ý nghĩa |
|-------|---------|
| `i`, `a`, `s` | Interval, account, symbol. |
| `t`, `ct` | Open/close time của **bar** mà VWAP gắn vào (ms). |
| `indicatorName` | Thường `"VWAP"`. |
| `source` | Ví dụ `"hlc3"` — VWAP tính trên giá **typical price** \((H+L+C)/3\) (theo convention phổ biến). |
| `data` | Object số liệu (xem bảng dưới). |

### Object `data`

| Field | Ý nghĩa |
|-------|---------|
| `vwap` | Giá VWAP tại mốc `t`. |
| `sd` | Độ lệch chuẩn (dùng cho band). |
| `topBand1` … `topBand3` | Dải trên VWAP (thường VWAP + k×σ). |
| `botBand1` … `botBand3` | Dải dưới VWAP. |
| `sumVol` | Tích lũy volume phục vụ VWAP (theo định nghĩa backend). |
| `sumScrVol`, `sumScrScrVol` | Tích lũy (giá×volume) và tích lũy bậc hai phục vụ công thức VWAP/σ — **dùng để tái tính hoặc kiểm tra**, không phải OHLC. |

**Quan hệ với nến:**

- Số điểm VWAP **có thể lớn hơn** số nến trong `getcandlehistory`: VWAP là **chuỗi lịch sử dài hơn** (session / cumulative), nên API trả thêm các mốc **cũ hơn** nến đầu tiên trong mảng nến hiện tại.
- Mọi `t` xuất hiện trong `getcandlehistory` **thường đều có** trong VWAP (tập thời gian VWAP là **superset** hoặc khớp phần hiển thị).
- **VWAP tại một `t` không bằng HLC3 của đúng nến đó** — VWAP là trung bình gia quyền **tích lũy**, không phải typical price của một nến đơn lẻ.

**Cách đọc không cần ảnh:** Lọc các phần tử VWAP có `t` trùng tập `t` của nến để overlay; hoặc vẽ toàn bộ chuỗi VWAP + band theo `t` tăng dần.

---

## 6. Checklist cho AI khi phân tích tự động

1. **Kiểm tra null:** Nếu `getcandlehistory` hoặc `getorderflowhistory` là `null`, không suy luận chéo giữa hai khối.
2. **Join theo `t`:** Map `getcandlehistory[i]` với `getorderflowhistory[j]` bằng **`t`** (hoặc sort rồi zip nếu đã xác nhận cùng độ dài và cùng thứ tự).
3. **Xác thực nội bộ (nếu cần):**  
   - `d` ≈ `bv - sv` trên nến.  
   - `d` ≈ \(\sum (bv - sv)\) trên `aggs` của cùng `t`.  
   - `v` ≈ \(\sum v\) trên `aggs` (cho phép ngoại lệ hiếm).
4. **VWAP:** Không ép số phần tử VWAP = số nến; join inner theo `t` nếu chỉ cần khớp cửa sổ nến.
5. **Thứ tự thời gian:** Sort theo `t` trước khi tính return, MA, hoặc so sánh nến liên tiếp.

---

## 7. Cắt gọn khi ghi đĩa (mặc định)

Khi **`chart_download.api_data_export.slim_export_on_disk: true`** (mặc định), file `*_coinmap_*.json` **đã được rút gọn** trước khi lưu (không chỉ lúc gửi OpenAI). Tắt: `slim_export_on_disk: false` để lưu nguyên payload API.

| Interval | Nến (`getcandlehistory`) | Footprint (`getorderflowhistory`) |
|----------|---------------------------|-------------------------------------|
| **15m** | 30 bar | 11 bar gần nhất (mục tiêu 10–12; env `COINMAP_OPENAI_FP_15M`) |
| **5m** | 35 bar (mục tiêu 30–40; `COINMAP_OPENAI_BARS_5M`) | 16 bar (mục tiêu 12–20; `COINMAP_OPENAI_FP_5M`) |

`getindicatorsvwap` được lọc theo `t` của các nến giữ lại. Khung khác (ví dụ 1h) — không đổi.

**OpenAI (analyze / all):** mặc định **không** slim thêm khi đọc file (tránh trùng). Nếu bạn vẫn có file cũ full-size trên đĩa, bật slim lúc đọc: `COINMAP_OPENAI_SLIM=true`.

---

## 8. Tóm tắt một dòng

| Khối | Nội dung chính |
|------|----------------|
| **getcandlehistory** | OHLC + volume + buy/sell + Delta tổng hợp + min/max Delta trong nến. |
| **getorderflowhistory** | Cùng số nến, cùng `t`; chi tiết **volume buy/sell theo từng mức giá**; Delta nến = tổng imbalance `aggs`. |
| **getindicatorsvwap** | Chuỗi **VWAP + σ-bands** theo thời gian, thường **dài hơn** vùng nến hiển thị. |

Với tài liệu này, phân tích kỹ thuật (xu hướng, vùng cân bằng, footprint, độ lệch giá so với VWAP) có thể thực hiện **hoàn toàn trên JSON**.
