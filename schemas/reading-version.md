# Agora Session 閱讀版格式規範 (`aistorage.reading/v1`)

閱讀版是從 Agora 保存的 Session 原始紀錄轉出、專供人與 AI 閱讀、全文搜尋索引以及接續點定位使用的共通規格。

- **原始紀錄為真本**：原始紀錄保留來源應用的完整輸出，閱讀版一律可從原始紀錄重新產生。
- **統一格式**：所有來源應用的轉換器（如 3.5 opencode、3.6 Claude Code）皆輸出此格式。
- **Schema 檔案**：[`schemas/reading-version.schema.json`](reading-version.schema.json)（Draft 2020-12，`$id: urn:aistorage:schema:reading-version`）。

---

## 結構說明

### 1. 頂層欄位 (Header)

| 欄位名 | 型態 | 說明 |
|---|---|---|
| `format` | 字串 | 固定為 `"aistorage.reading/v1"`。 |
| `session_id` | 字串 | Session 唯一識別碼，格式為 `<source>:<source_session_id>`（例如 `opencode:ses_123`），遵循 2.1 Session ID 規範（不得以保留型態為 source）。 |
| `source` | 字串 | 來源應用識別（例如 `opencode`、`claude_code`）。 |
| `title` | 字串 或 `null` | 對話標題；若來源端未命名或無標題則為 `null`。**註：標題由 4.2 全文搜尋模組另行獨立索引，不進入各訊息的內文全文索引**。 |
| `parent_id` | 字串 或 `null` | 若此 Session 為子代理 Session，記錄母 Session 識別碼；頂層 Session 為 `null`。 |
| `snapshot_sha256` | 字串 | 轉換來源之原始紀錄快照內容的 SHA256 雜湊值（64 字元小寫十六進位 hex）。接續點釘在此快照上。 |
| `in_progress` | 布林 | Session 目前是否在生成/執行中（例如來源端正在串流或未終止）。 |
| `messages` | 陣列 | 對話訊息列表，依 `index` 嚴格遞增排序。 |

---

### 2. 訊息欄位 (`messages[]`)

| 欄位名 | 型態 | 說明 |
|---|---|---|
| `message_id` | 字串 | 來源應用自身的訊息唯一識別碼。**整份閱讀版中必須唯一**。**接續點與交接單依賴此欄位定位**。 |
| `index` | 整數 | 訊息在閱讀版中之順序（**必須從 0 起算、連續且嚴格遞增**）。此規則由 Python 驗證器檢查。 |
| `role` | 字串 | 角色識別，限制為 `"user"`、`"assistant"`、`"system"` 之一。 |
| `created_at` | 字串 或 `null` | 訊息發送時間（RFC 3339 UTC 格式，例如 `"2026-09-27T08:00:00Z"`）；未知時為 `null`。 |
| `completed` | 布林 | Assistant 訊息是否已生成完畢。**未完成（`completed: false`）或已被撤銷（`reverted: true`）之訊息不能作為接續點**。 |
| `reverted` | 布林 | 來源端是否已撤銷此訊息（例如使用者執行 `/undo` 或重寫歷史）。讀取介面**預設不呈現**撤銷訊息，但閱讀版與原始紀錄皆完整保留。 |
| `parts` | 陣列 | 訊息的內容段落清單。 |

---

### 3. 內容段落型態 (`parts[]`)

訊息內容依型態分為以下幾類：

#### (1) 文字段落 (`type: "text"`)
一般使用者對話或助理回覆：
```json
{
  "type": "text",
  "text": "請幫我分析資料庫的連線問題。"
}
```

#### (2) 工具呼叫段落 (`type: "tool_call"`)
工具呼叫不保存完整的龐大輸出，僅記錄輸入與輸出摘要（各上限 4,000 個 Unicode code point，截斷後包含結尾標記 `…` 總字元數不超過 4,000 字元）。若工具失敗，在 `output_summary` 開頭標示 `[ERROR]`。若呼叫啟動了子代理，在 `child_session_id` 記錄該子 Session 之識別碼：
```json
{
  "type": "tool_call",
  "name": "view_file",
  "input_summary": "path: /src/db.py",
  "output_summary": "class DatabaseConnection: ...",
  "child_session_id": null
}
```

#### (3) 圖片段落 (`type: "image"`)
**不得內嵌 base64**，僅記錄媒體 MIME 類型、小寫 SHA256 雜湊與位元組大小：
```json
{
  "type": "image",
  "media_type": "image/png",
  "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "size": 15420
}
```

#### (4) 壓縮摘要段落 (`type: "compaction"`)
來源應用執行對話壓縮（如 `/compact`）時生成的摘要段落：
```json
{
  "type": "compaction",
  "summary": "前置討論總結：已確認資料庫連線逾時係因連線池耗盡，決定改用連線複用架構。"
}
```

#### (5) 推理/思考段落 (`type: "reasoning"`)
模型生成的推理或思考過程（如 opencode 的 reasoning、Claude Code 的 thinking）：
```json
{
  "type": "reasoning",
  "text": "分析使用者需求：資料庫負載過高，需要檢查連線池設定並計算容量..."
}
```
*註：`plain_text` 函式預設不包含 `reasoning` 段落，以維護全文搜尋聚焦與隱私。*

#### (6) 檔案附件段落 (`type: "file"`)
非圖片之檔案或附件（如 PDF、文字檔等），不內嵌檔案本體，記錄其中繼資料：
```json
{
  "type": "file",
  "media_type": "application/pdf",
  "sha256": "b5a2c9b149afbf4c8996fb92427ae41e4649b934ca495991b7852b855e3b0c44",
  "size": 245100,
  "name": "schema_design.pdf"
}
```

---

## 來源格式 → 閱讀版轉換對應表

為確保轉換器（3.5 opencode、3.6 Claude Code）行為一致，依下表規則對照：

| 來源應用 | 來源原始格式 / 事件 | 閱讀版對應規則 | 說明 |
|---|---|---|---|
| **opencode** | `text` part | `type: "text"` | 純文字段落（若 assistant 訊息之 `info.summary is True` 則轉為 `type: "compaction"`）。 |
| **opencode** | `reasoning` part | `type: "reasoning"` | 思考過程，`plain_text` 預設不納入。 |
| **opencode** | `tool` call & response | `type: "tool_call"` | 擷取摘要各 ≤ 4,000 code point；若 `state.status == error`，錯誤訊息取自 `state.error`，於 `output_summary` 開頭標示 `[ERROR]`。 |
| **opencode** | `file` part (`data:` URL, 非圖片) | `type: "file"` | 解碼計算小寫 `sha256` 與 `size`，填入 `media_type`、`name`。 |
| **opencode** | `file` part (`data:` URL, `mime=image/*`) | `type: "image"` | opencode 圖片亦為 `type: "file"`；解碼計算小寫 `sha256` 與 `size`，轉為 `type: "image"`。 |
| **opencode** | `file` part (非 `data:` URL) | `type: "text"` | 因原始內容不在匯出中，轉為文字段落 `[附件：<filename> <mime>，內容不在匯出中]`，不捏造雜湊。 |
| **opencode** | `step-start` / `step-finish` / `snapshot` / `patch` | **丟棄** | 內部步驟與暫存標記，不進入閱讀版。 |
| **opencode** | user 訊息之 `compaction` part | **丟棄** | 壓縮邊界標記，不進入閱讀版。 |
| **opencode** | assistant 訊息之 `info.summary is True` | `type: "compaction"` | 壓縮摘要對話，其 text part 轉為 `type: "compaction"`。 |
| **opencode** | `info.revert` | `reverted: true` | 在 revert 指標之後被回滾的訊息標為 `reverted: true`（若有 `partID` 則該則保留有效，自下一則起標記；若指到不存在訊息拋出 `ConversionError`）。 |
| **opencode** | prune 標記 (`state.time.compacted`) | **忽略** | 內容仍在，按一般訊息轉換。 |
| **opencode** | 未知或未支援段落型態 | `type: "text"` | 輸出 `[未支援的段落型態：<type>]`，不靜默丟棄。若訊息段落全數被過濾，補空文字段落。 |
| **Claude Code** | 樹狀分支 (`uuid` / `parentUuid`) | **沿最新葉節點展開** | 沿最新主幹展開為主流程，其餘分支分支保留並標註 `reverted: true`。 |
| **Claude Code** | `tool_use` (assistant) + `tool_result` (user) | `type: "tool_call"` | 依 `tool_use_id` 配對合併為單一 `tool_call` 段落，依需要截斷摘要。 |
| **Claude Code** | `thinking` block | `type: "reasoning"` | 對應為推理段落。 |
| **Claude Code** | 子代理 (`isSidechain` 或 subagent jsonl) | `child_session_id` | 對應填入子 Session ID。 |
| **Claude Code** | Base64 圖片區塊 | `type: "image"` | 解碼計算小寫 SHA-256 與大小，儲存中繼資料。 |
| **Claude Code** | 訊息識別碼 | `message_id: uuid` | 直接採用 Claude Code 之 `uuid` 作為 `message_id`。 |

---

## 範例 1：一般 Session (Regular Session)

一般對話流程，包含使用者提問、助理推理、呼叫查詢工具並給出回覆：

```json
{
  "format": "aistorage.reading/v1",
  "session_id": "opencode:ses_20260927_001",
  "source": "opencode",
  "title": "診斷資料庫連線池問題",
  "parent_id": null,
  "snapshot_sha256": "4a7d1ed414474e4033ac29ccb8653d9b110e11894d7c0f16599b4f6cf4c93540",
  "in_progress": false,
  "messages": [
    {
      "message_id": "msg_001",
      "index": 0,
      "role": "user",
      "created_at": "2026-09-27T08:00:00Z",
      "completed": true,
      "reverted": false,
      "parts": [
        {
          "type": "text",
          "text": "請檢視目前的連線池設定檔，並評估在 100 個並發查詢時是否足夠。"
        }
      ]
    },
    {
      "message_id": "msg_002",
      "index": 1,
      "role": "assistant",
      "created_at": "2026-09-27T08:00:05Z",
      "completed": true,
      "reverted": false,
      "parts": [
        {
          "type": "reasoning",
          "text": "使用者欲知連線池是否足以支撐 100 個並發，首先應讀取設定檔確認上限。"
        },
        {
          "type": "text",
          "text": "好的，我先讀取連線池設定檔。"
        },
        {
          "type": "tool_call",
          "name": "view_file",
          "input_summary": "path: config/database.json",
          "output_summary": "{\"pool_size\": 20, \"max_overflow\": 10, \"timeout\": 30}",
          "child_session_id": null
        },
        {
          "type": "text",
          "text": "目前的 `pool_size` 設為 20、`max_overflow` 為 10，最大容量僅 30。面對 100 個並發查詢時將發生排隊或逾時，建議調整至 50 並啟用連線複用。"
        }
      ]
    }
  ]
}
```

---

## 範例 2：含子代理與撤銷的 Session (Session with Sub-agents & Reverts)

包含子代理呼叫（透過 `task` 工具）、對話壓縮摘要，以及使用者在撤銷（`/undo`）一段錯誤指令後重新提問的場景：

```json
{
  "format": "aistorage.reading/v1",
  "session_id": "opencode:ses_20260927_002",
  "source": "opencode",
  "title": "大規模架構重構與子代理派工",
  "parent_id": null,
  "snapshot_sha256": "9b110e11894d7c0f16599b4f6cf4c935404a7d1ed414474e4033ac29ccb8653d",
  "in_progress": false,
  "messages": [
    {
      "message_id": "msg_010",
      "index": 0,
      "role": "user",
      "created_at": "2026-09-27T08:10:00Z",
      "completed": true,
      "reverted": false,
      "parts": [
        {
          "type": "text",
          "text": "請啟動子代理去搜尋專案中所有使用舊版 API 的位置。"
        }
      ]
    },
    {
      "message_id": "msg_011",
      "index": 1,
      "role": "assistant",
      "created_at": "2026-09-27T08:10:04Z",
      "completed": true,
      "reverted": false,
      "parts": [
        {
          "type": "text",
          "text": "已派出研究型子代理進行搜尋。"
        },
        {
          "type": "tool_call",
          "name": "task",
          "input_summary": "prompt: 搜尋專案內所有 import legacy_api 的檔案",
          "output_summary": "搜尋完成，共發現 14 處調用。",
          "child_session_id": "opencode:ses_20260927_child_01"
        },
        {
          "type": "text",
          "text": "子代理回報完畢，共發現 14 處調用，已整理清單於 reports/legacy.md。"
        }
      ]
    },
    {
      "message_id": "msg_012",
      "index": 2,
      "role": "user",
      "created_at": "2026-09-27T08:15:00Z",
      "completed": true,
      "reverted": true,
      "parts": [
        {
          "type": "text",
          "text": "請立刻刪除那 14 個檔案。（註：使用者隨後發覺有誤，執行了 /undo 撤銷）"
        }
      ]
    },
    {
      "message_id": "msg_013",
      "index": 3,
      "role": "assistant",
      "created_at": "2026-09-27T08:15:02Z",
      "completed": false,
      "reverted": true,
      "parts": [
        {
          "type": "text",
          "text": "準備開始刪除...（生成中斷並隨 /undo 撤銷）"
        }
      ]
    },
    {
      "message_id": "msg_014",
      "index": 4,
      "role": "user",
      "created_at": "2026-09-27T08:16:00Z",
      "completed": true,
      "reverted": false,
      "parts": [
        {
          "type": "compaction",
          "summary": "前情提要：已由子代理找出 14 處舊版 API 調用，現準備執行安全相容包裝而非直接刪除。"
        },
        {
          "type": "text",
          "text": "更正：不要刪除檔案，請替它們撰寫相容性轉接層 (adapter)。"
        }
      ]
    },
    {
      "message_id": "msg_015",
      "index": 5,
      "role": "assistant",
      "created_at": "2026-09-27T08:16:15Z",
      "completed": true,
      "reverted": false,
      "parts": [
        {
          "type": "text",
          "text": "收到更正。我將開始在 `src/compat/` 建立轉接層，保持向後相容。"
        }
      ]
    }
  ]
}
```
