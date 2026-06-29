# FULL_ANALYSIS Legacy — Vector Store Knowledge

Dùng khi cần phân tích theo **bộ rule cũ** trong OpenAI Vector Store (`OPENAI_VECTOR_STORE_IDS`), **không** đọc `master_trading_playbook.md`.

## Khác biệt so với mode mặc định

| | Mặc định (playbook) | Legacy (vector store) |
|---|---------------------|------------------------|
| Nguồn rule | `master_trading_playbook.md` §1 + §0.3 | File đã sync từ `OPENAI_VECTOR_STORE_IDS` |
| Scoring / filter | Playbook mục 0.3 | Theo nội dung file trong vector store |
| Schema output | Vẫn `system-prompt.md` Schema A | Vẫn Schema A |
| OpenAI API lúc phân tích | Không | Không (chỉ sync file về local) |

## Phase 0 — Sync knowledge (bắt buộc trước khi phân tích legacy)

```bash
source .venv/bin/activate
coinmap-automation sync-vector-store-knowledge
# hoặc
python scripts/sync_vector_store_knowledge.py
```

Yêu cầu `.env`:
- `OPENAI_API_KEY`
- `OPENAI_VECTOR_STORE_IDS=vs_...` (có thể nhiều id, phân cách bằng dấu phẩy)

Output mặc định: `data/vector_store_knowledge/`

```
data/vector_store_knowledge/
  manifest.json          # metadata sync + danh sách file
  files/
    {vs_id}__{file_id}__{filename}
```

Override thư mục: `OPENAI_VECTOR_STORE_KNOWLEDGE_DIR=/path/to/knowledge`

Chạy lại sync khi cập nhật file trên vector store (không tự động).

**Lưu ý:** File gốc upload với `purpose: assistants` **không** tải được qua `files.content`. Sync dùng API `vector_stores.files.content` → lưu **text đã parse** (`.txt`). Với `.md` gốc vẫn lưu dạng `.txt` chứa nội dung parsed.

## Phase 1–3 — Giống mode mặc định

Capture + validate như SKILL chính:

```bash
coinmap-automation browser up
coinmap-automation capture-full-analysis --gocharting
python scripts/full_analysis_manifest.py --legacy
```

Manifest legacy thêm các key:
- `analysis_mode`: `"legacy"`
- `knowledge_dir`, `knowledge_manifest`, `knowledge_files[]`
- `knowledge_ready`: `true` khi đã sync đủ
- `ready_for_analysis`: slots + footprint + `knowledge_ready`

**Không phân tích** nếu `knowledge_ready` là `false` — chạy sync trước.

## Phase 4 — Phân tích (legacy)

**Bắt buộc đọc trước:**
1. [`system-prompt.md`](../../system-prompt.md) — mode `[FULL_ANALYSIS]`, Schema A, format `phan_tich_cham_diem`
2. **Toàn bộ file** trong `manifest.knowledge_files[]` (theo thứ tự tên file hoặc thứ tự trong manifest)

**Không** đọc `master_trading_playbook.md` trong mode legacy.

Footprint derived (stacked / absorption): tính từ `footprint_combined_*.json` qua logic `gocharting_footprint_derived` hoặc enrich trước khi đọc — CSV GoCharting **không** có BID/ASK từng mức.

## Phase 5 — Output

Giống mode mặc định: in `phan_tich_cham_diem` markdown rồi JSON Schema A.

Lưu tùy chọn: `data/{SYM}/morning_full_analysis.json`
