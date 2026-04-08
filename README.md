# Coinmap → OpenAI Responses (prompt id) → Telegram

Python CLI that:

1. **capture** — Before running, optionally **clears** `data/charts/*.png` / `.json` (keeps `.gitkeep`) when `clear_charts_before_capture` is true. Then opens Coinmap in **Google Chrome** via Playwright. If **`chart_download.coinmap_screenshot_enabled`** is **false**, Coinmap skips fullscreen PNGs and only saves **API JSON** next to where PNGs would be (`{stamp}_coinmap_{SYMBOL}_{interval}.json`). If **true** (legacy), it also saves Coinmap screenshots. With **`api_data_export`**, JSON is always written when enabled. If **`tradingview_capture.enabled`** is true, it opens a **second tab** for TradingView and saves **`data/charts/*_tradingview_*.png`**. (The two sites run one after another.)
2. **analyze** / **chatgpt-project** — **OpenAI Responses API** with a **dashboard prompt id**. One **multimodal** user message: prompt text plus charts in **fixed order** (`automation_tool/images.py` → `CHART_IMAGE_ORDER`): **TradingView** slots use **PNG** images; **Coinmap** slots use **JSON** when the file exists (otherwise PNG fallback). Extra batches (if over `--max-images-per-call`) chain with `previous_response_id`. Optional **`COINMAP_JSON_MAX_CHARS`** caps embedded JSON size. Default prompt asks for **structured JSON** output (see `DEFAULT_ANALYSIS_PROMPT` in `openai_prompt_flow.py`). **Telegram** receives the analysis output; stdout prints the same (multi-batch runs join with `---`).
3. **all** — Runs **capture**, then the same OpenAI flow as **analyze**, then **sends Telegram** (unless `--no-telegram`), then parses three zone prices, persists them, **syncs TradingView alerts**, and **by default runs `tv-journal-monitor`** (long-running; use `--no-tv-journal-monitor` to stop after sync). Skipped when `--no-tradingview` or zone prices could not be parsed.

## Requirements

- Python 3.9+
- **Google Chrome** installed on the machine (Playwright uses `channel="chrome"` for **capture** only)
- OpenAI API key and a **Prompt** created in the OpenAI dashboard (id like `pmpt_...`)
- Telegram bot token from [@BotFather](https://t.me/BotFather) and your `chat_id` (if you use Telegram delivery)

#### Persistent Chrome profile (capture only)

Playwright uses the same Chrome app, but by default a **new empty profile** each run. To reuse one automation profile (cookies, logins), set in `.env`:

`PLAYWRIGHT_CHROME_USER_DATA_DIR=/absolute/or/project/path/data/chrome_user_data`

Use a **dedicated** directory (e.g. `data/chrome_user_data`). Do **not** point this at your main Chrome profile while Chrome is running. See `playwright_browser.py`.

## Setup

```bash
cd automation-tool
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
playwright install   # OS/browser glue for Playwright; Chrome itself must be installed separately
cp .env.example .env
# Edit .env: OPENAI_API_KEY, OPENAI_PROMPT_ID, OPENAI_VECTOR_STORE_IDS (optional), Telegram, Coinmap
```

**`command not found: coinmap-automation`** — activate the venv in each new terminal (`source .venv/bin/activate`), or call the script by path: `.venv/bin/coinmap-automation …` from the project root.

## Configuration

| File | Purpose |
|------|---------|
| `.env` | `OPENAI_API_KEY`, `OPENAI_PROMPT_ID`, optional `OPENAI_PROMPT_VERSION`, optional `OPENAI_VECTOR_STORE_IDS`, `OPENAI_RESPONSES_STORE`, `OPENAI_RESPONSES_INCLUDE`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, optional `TELEGRAM_LOG_CHAT_ID` (channel nhận log bước chạy + **log phản hồi đầu**: hop_luu, vùng chọn, lý do bỏ qua MT5), optional `TELEGRAM_ANALYSIS_DETAIL_CHAT_ID` (kênh nhận **OUTPUT_CHI_TIET** cho luồng `analyze-many` khi set `--telegram-detail-chat-id` hoặc dùng mặc định từ `.env`), optional `TELEGRAM_OUTPUT_NGAN_GON_CHAT_ID` (OUTPUT_NGAN_GON từ phân tích; **log MT5** chỉ gửi tới `TELEGRAM_CHAT_ID`), optional `TELEGRAM_PARSE_MODE`, optional `COINMAP_*` |

**Telegram formatting:** mặc định không có `parse_mode` → plain text. Để **in đậm / tiêu đề** từ output kiểu markdown của model, đặt **`TELEGRAM_PARSE_MODE=HTML`**. Code sẽ chuyển `**bold**`, dòng `## tiêu đề`, và `` `code` `` sang thẻ HTML mà Telegram hỗ trợ (`<b>`, `<code>`), đồng thời escape `& < >` ở phần còn lại. **`MarkdownV2`** vẫn được hỗ trợ nhưng toàn bộ nội dung bị escape nên **trông như chữ thường** (tránh lỗi 400 với `+`, v.v.) — không dùng MDV2 nếu bạn muốn thấy định dạng.
| `config/coinmap.yaml` | Login URL, **`chart_download`**, optional **`tradingview_capture`**, and optional canvas screenshot selectors |

#### Chart page screenshot (`chart_download`)

After login, the tool navigates to `chart_page_url`, optionally switches toward **dark mode** (`dark_mode_theme_button_selector`: if the button does not contain `dark_mode_sun_icon_selector`, e.g. `span.anticon-sun`, it clicks once), then optionally dismisses a **light theme** modal (`light_theme_confirm_selector`), then runs symbol search and fullscreen screenshot as above.

Use `[class*="…"]`-style class prefixes so CSS-module hashes can change. Set `screenshot_after_chart_download: true` if you also want the legacy per-canvas screenshot loop.

#### TradingView (`tradingview_capture`)

When `enabled`, after the Coinmap step the tool opens a new page, loads `chart_url`, sets the interval, clicks `#header-toolbar-fullscreen`, then waits for the fullscreen “panels hidden” notice (`tradingview_fullscreen_notice_selector`, default matches `container-default-*` + `notice-*`) to become **hidden**, then applies `fullscreen_settle_ms` and saves **`{timestamp}_tradingview_fullscreen.png`**. Set `tradingview_fullscreen_notice_wait_disabled: true` to skip that wait. Cookie banners or sign-in may still need manual handling.

### OpenAI Responses (prompt template)

- Set **`OPENAI_PROMPT_ID`** to your dashboard prompt (required for `analyze` / `all` / `chatgpt-project`).
- Optional **`OPENAI_PROMPT_VERSION`** — if unset, the `version` field is omitted from the API request.
- **`OPENAI_VECTOR_STORE_IDS`** — comma-separated vector store ids; enables the **file_search** tool when non-empty.
- **`OPENAI_RESPONSES_STORE`** — default `true` (server-side conversation storage).
- **`OPENAI_RESPONSES_INCLUDE`** — comma-separated include list; default adds `reasoning.encrypted_content` and `web_search_call.action.sources`.

### Telegram `chat_id`

Message [@userinfobot](https://t.me/userinfobot) or call `https://api.telegram.org/bot<TOKEN>/getUpdates` after messaging your bot.

## Usage

```bash
# Capture charts only
coinmap-automation capture --headed

# Multimodal analysis on existing charts, send to Telegram
coinmap-automation analyze

# Full pipeline
coinmap-automation all

coinmap-automation chatgpt-project --no-telegram
```

Options:

- `--prompt` — User message before chart images (default: Vietnamese XAUUSD workflow + JSON schema hints).
- `--max-images-per-call` — Split images across multiple API calls (default `10`).
- `--no-telegram` — Print only; do not send to Telegram (`analyze`, `all`, `chatgpt-project`).
- `--no-tradingview` / `--no-tv-journal-monitor` — On **`all`**, skip TradingView alert sync / skip the journal monitor step after sync (see `coinmap-automation all --help`).
- **`all`** — Before capture, deletes `last_alert_prices.json` for the active symbol (or `--last-alert-json` path) so the run does not inherit yesterday’s statuses / `trade_line` / tickets. Pass **`--no-clear-last-alert`** to keep the existing file and merge as before.
- **`analyze` / `all` / `chatgpt-project`**: On the **first** OpenAI response for the chart step, if the JSON includes a complete triple (`plan_chinh`, `plan_phu`, `scalp`) with per-zone **`hop_luu`** (0–100) and **`trade_line`** (one MT5 pipe line per zone), the tool merges prices into `last_alert_prices.json`. **Auto-MT5** runs (**live** by default) only when at least one zone has **`hop_luu` > 80** and a non-empty `trade_line`: the tool picks that zone (highest score; tie-break `plan_chinh` → `plan_phu` → `scalp`), sets `vao_lenh` for that label, sets `entry_manual_by_label` to `false`, and calls `execute_trade` on that zone’s `trade_line`. If no zone is above 80, prices are still written but statuses stay non-terminal for auto-entry. Use `--no-mt5-execute` to skip MT5; `--mt5-dry-run` to simulate. Override file path with `--last-alert-json` (default `data/{SYMBOL}/last_alert_prices.json` per active symbol).
- **`all` / `tv-journal-monitor`**: same MT5 flags: `--last-alert-json`, `--no-mt5-execute`, `--mt5-symbol`, `--mt5-dry-run` — after a valid `VÀO LỆNH` + parseable `trade_line`, `execute_trade` runs **live** by default; pass `--mt5-dry-run` for simulation only.
- **`update`**: `--no-journal-monitor-after-update` — when **before** writing new prices all three plans are already terminal (`vao_lenh` or `loai` in `data/last_alert_prices.json`), after a successful merged write the tool still syncs TradingView and Telegram, but **skips** the automatic `tv-journal-monitor` run (default: run monitor in that situation).
- `--storage-state` — Path to Playwright storage state JSON for **capture** (default `data/storage_state.json`).

### `data/last_alert_prices.json` and `tv-journal-monitor`

The file stores the current triple of zone prices plus **`status_by_label`** and optional **`entry_manual_by_label`** (bool per label: `true` if you record the fill as **manual** off-bot; `false` when set by this tool / MT5). For each of `plan_chinh`, `plan_phu`, `scalp`, status is one of:

| Value | Meaning |
|-------|---------|
| `vung_cho` | Still waiting (journal not finished for this plan). |
| `vao_lenh` | Inner loop returned `VÀO LỆNH` with a parseable `trade_line`, or morning auto-MT5 ran for that zone. |
| `loai` | Inner loop returned `loại`. |

Older files without `status_by_label` are treated as all `vung_cho`; missing `entry_manual_by_label` defaults to `false` for every label. When **`all`** or **`update`** writes a **new** triple, only labels whose **price changed** are reset to `vung_cho` and their **manual** flag to `false`; unchanged prices keep their status and manual flag.

**`tv-journal-monitor`** reloads this file each outer cycle, matches the TradingView journal only against prices that are still `vung_cho`, and continues until all three are terminal or the **session cutoff** is reached. At start it writes `data/{{SYMBOL}}/journal_monitor_first_run.json` with `started_at` and `session_cutoff_end`: if **first run is before 13:00** (local `--timezone`, default Asia/Ho_Chi_Minh) the monitor stops at **13:00 same day**; if **first run is from 13:00 onward** it runs until **02:00 the next morning** (next calendar day’s 02:00). If `session_cutoff_end` were not set (internal use), the CLI falls back to `--until-hour`. Model action **`chờ`** does not set a terminal status. If the model says `VÀO LỆNH` but `trade_line` is missing or does not parse, the plan stays `vung_cho` and the inner loop waits and retries (same as `chờ`).

**`update`**: TradingView sync runs **after** the merged write of `last_alert_prices.json`. If the pre-write state had all three plans terminal and you are writing a new triple, the CLI **also** starts `tv-journal-monitor` (unless `--no-journal-monitor-after-update`).

### Log tới Telegram (channel)

Đặt **`TELEGRAM_LOG_CHAT_ID`** (ví dụ supergroup/channel `-100…`) cùng bot đã được thêm làm admin: mọi log mức INFO của gói `automation_tool` (lệnh `coinmap-automation`, đồng bộ cảnh báo TV, toàn bộ dòng `tv-journal`) được gửi **plain text** tới channel đó (tin nhắn dài được chia chunk). Stderr vẫn in giống hệt.

## Security

- Do not commit `.env` or `data/storage_state.json`.
- Prefer saving storage state after a successful login and rotating passwords if exposed.

## License

Use at your own risk; comply with Coinmap, OpenAI, and Telegram terms of service.
