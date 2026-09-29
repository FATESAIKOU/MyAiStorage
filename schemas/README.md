# AiStorage 共通項目 Metadata Schemas

本目錄定義 AiStorage 各儲存要素（Agora、Foundry、MyBrain、Atelier）通用的項目 metadata JSON Schema（基於 JSON Schema Draft 2020-12），以及欄位規範、時間語義、ID 產生與擴充規則。

---

## 一、檔案清單

- `metadata-inbox.schema.json`：收件匣項目的 metadata（由寫入者提供，**不含產生者 `producer`**；若寫入者自行夾帶將被剝離並忽略）。
- `metadata-record.schema.json`：真本項目的 metadata（由提交流程驗證簽章／認證身分後，蓋上 `producer` 戳章）。
- `inbox-sidecar.schema.json`：收件匣項目的 sidecar 規格（格式 `aistorage.inbox/v1`），包含寫入者 Profile、快照資訊、簽章與依型態分支之 Body。
- `readview-manifest.schema.json`：讀取視圖的 manifest（格式 `aistorage.readview/v1`），讀取介面的信任錨點。
- `reading-version.schema.json`：閱讀版的共通格式（格式 `aistorage.reading/v1`），跨來源應用共用。
- `context-package.schema.json`：**起點包**（格式 `aistorage.contextpackage/v1`），`agora checkout` 的產物；說明見 `context-package.md`。
- `identity-registry.schema.json`：身分登錄檔（簽章金鑰與收件匣的對應）。

---

## 二、欄位說明

共通 metadata 設計為六個核心欄位，外加預留欄位與自訂擴充支援：

| 欄位名 | 型態 | 收件匣 | 真本 | 說明 |
|---|---|---|---|---|
| `id` | `string` | 必填 | 必填 | 項目的唯一不可變識別碼。非空且不得包含空白字元，格式符合 ID 規範（依 type 限制）。 |
| `type` | `string` | 必填 | 必填 | 項目型態。收件匣維持封閉 enum（`session`、`handoff`、`claim`、`continuation`、`reference`、`rewrite`、`artifact`）；真本採用 `^[a-z][a-z_]*$` 以支援讀取端擴充相容性。 |
| `producer` | `string` | **不帶** | 必填 | 產生者身分（Profile 識別碼，例如 `profile:mac-opencode`，具體格式在 2.3 定案）。收件匣中不得採用寫入者自行宣稱的值；真本中由提交流程依驗章結果蓋章填入。 |
| `created_at` | `string` | 必填 | 必填 | 項目業務建立時間，遵循 RFC 3339 UTC `Z` 格式（`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$`）。Session 取自來源應用建立時間。 |
| `updated_at` | `string` | 必填 | 必填 | 項目內容最後修改時間，遵循 RFC 3339 UTC `Z` 格式。Session 取自來源應用最後修改時間。 |
| `case_id` | `string \| null` | 選填 | **必填（可為 null）** | 所屬案件識別碼（MyBrain 案件 ID，為不透明字串）。收件匣中可省略（提交流程自動補 `null`）；真本中鍵名必須存在。 |
| `provenance` | `string \| null` | 選填 | **必填（可為 null）** | 項目出處說明字串。收件匣中可省略（提交流程自動補 `null`）；真本中鍵名必須存在。 |
| `role` | `string \| null` | 選填 | 選填 | 預留選填：角色識別（期 1 不驗證其內部格式）。 |
| `role_version` | `string \| null` | 選填 | 選填 | 預留選填：角色版本（期 1 不驗證其內部格式）。 |

---

## 三、時間欄位與快照時間、提交時間的關係

在 AiStorage 生命週期中，存在多種不同的時間概念，其語義與層級關係如下：

1. **建立時間 (`created_at`)**：
   - 項目首次在寫入端產生的業務時間點（RFC 3339 UTC `Z`）。
   - Session 取自來源應用的時鐘（如 opencode 的 `info.time.created`）；交接單等其他項目則為寫入者建立當下的時間。
   - 一旦建立後，在項目的生命週期內保持不變。
2. **更新時間 (`updated_at`)**：
   - 項目業務內容最後被修改的時間點（RFC 3339 UTC `Z`）。
   - Session 取自來源應用時鐘（如 opencode 的 `info.time.updated`）；隨對話持續推進或內容修訂而更新。
3. **快照時間 (`snapshot_at`)**：
   - 傳輸與 Sidecar 層記錄的時間戳記（放置於 2.2 收件匣描述檔的 `session.snapshot_at`，非共通 metadata 欄位）。
   - 代表同步器或匯出工具在容器中讀取來源狀態該瞬間的物理時鐘。
4. **提交時間 (`commit_time` / `committed_at`)**：
   - 提交流程（Committer）在 runner 上驗證收件匣並推入真本 Git 歷史的時間點。
   - 體現於真本 Git Commit 的 Committer Date，並由提交流程寫進真本 metadata 的擴充欄位 `committed_at`（在 tasks 3.3 實作）。
5. **時序關係**：
   $$\text{created\_at} \le \text{updated\_at} \le \text{snapshot\_time} \le \text{commit\_time}$$
   *註：上述時序關係在物理正常情況下成立；考慮到來源應用、容器與 runner 之間的時鐘漂移，驗證器**不以**時序顛倒為由拒收項目。*

---

## 四、ID 產生與生命週期規則

項目的 ID 具有**全域唯一性**與**不可變性**，不因搬移、重新命名或改寫而改變：

1. **Session 項目**：
   - 格式：`<source>:<source_session_id>`
   - `source`：來源應用（例如 `opencode`、`claude-desktop`），**非空、不得包含冒號 `:` 或空白，且不得使用保留型態名（`session`、`handoff`、`claim`、`continuation`、`reference`、`rewrite`、`artifact`）**。
   - `source_session_id`：來源應用自身的 Session ID，非空且不得包含空白字元。
   - 範例：`opencode:ses_01J8Z9X0P1Q2R3S4T5U6V7W8X9`
2. **其他項目（handoff、claim、continuation、reference、rewrite、artifact）**：
   - 格式：`<type>:<ULID>`
   - `type`：項目型態（如 `handoff`、`claim`、`continuation` 等；`make_item_id` 拒絕 `session` 型態）。
   - `ULID`：26 字元之 Crockford's Base32 字串（48-bit 毫秒時間戳 + 80-bit 加密安全隨機數），保證時間可排序與全域唯一性。
   - 範例：`handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV`
3. **分類與衝突處理（`classify_id`）**：
   - **`new`**：真本中尚無此 ID 的項目（`existing is None`），直接新增。
   - **`update`**：真本中已存在相同 `id`，且新舊項目的 `producer` 與 `type` 完全一致，視為同一個項目的正當版本更新。
   - **`collision`**：真本中已存在相同 `id`，但 `producer` 或 `type` 不符，判定為撞號衝突，提交流程必須拒絕寫入。
   - **例外處理**：若傳入之 `existing` 與 `incoming` 的 `id` 不相同，或任一方缺少必填的 `id` 或 `producer`，均視為呼叫端邏輯錯誤，直接丟出 `ValueError`。

---

## 五、擴充規則

1. **向後相容**：
   - Schema 設定 `additionalProperties: true`。各儲存要素或未來功能模組（如 LLMGateway 隱私標籤等）可在共通六欄位之外加入特定欄位。
2. **讀取端行為**：
   - 所有讀取者與驗證器遇到不認識的額外欄位時，必須安全忽略，不得將其視為驗證錯誤。

---

## 六、收件匣項目格式（Inbox Item 與分離式簽章）

收件匣為非信任寫入端（Worker / Sync Tool）與提交流程之間的非同步轉接區。依據設計 D2、D3、D4、D10 與 review-2.2 決策，收件匣項目的實體組織與規範如下：

### 1. 項目組成與寫入原子性
一個收件匣項目以一個全域唯一的 `item_key`（ULID）為名，由二或三個實體檔案組成：
- `<item_key>.raw`：**本體資料（選填／條件必備）**。僅有 `session`、附帶實體檔案的 `artifact`（`kind: contained`）、`rewrite` 包含本體。為來源端匯出的原始位元組，寫入端不作任何格式轉換。單檔大小預設上限為 100 MiB (104,857,600 位元組)；schema 定義之 100 MiB 為預設值，實際大小限制以提交流程設定為準。
- `<item_key>.sidecar.json`：**描述檔**。記載項目型態、快照時間、本體雜湊等資訊（**不含簽章**）。
- `<item_key>.sig`：**分離式簽章檔（最後寫入）**。JSON 格式：`{"alg": "ed25519", "key_id": "<id>", "value": "<base64>"}`。
- **不可分單位原則**：寫入端必須嚴格遵循 **`raw`（若有）→ `sidecar.json` → `.sig`** 之順序寫入。**收件匣中必須具備 `<item_key>.sig` 才算完整項目**；若缺少 `.sig` 視為「只看到一半的項目」，提交流程會保留至下一輪處理；若超過 24 小時仍未見完整檔案，視為逾時孤兒項目進行清掃。提交流程對沒有 `.sig` 或驗章失敗的項目一律拒收。

### 2. Sidecar 核心欄位規範（格式 `aistorage.inbox/v1`）
```json
{
  "format": "aistorage.inbox/v1",
  "item_key": "<ULID>",
  "profile": "mac-opencode",
  "metadata": { ... },
  "raw": { "sha256": "<小寫 hex>", "size": 123 },
  "session": { ... },
  "body": { ... }
}
```
- `format`（必填）：固定為 `"aistorage.inbox/v1"`。
- `item_key`（必填）：26 字元 Crockford Base32 之 ULID。
- `profile`（必填）：寫入者宣稱的 Profile（如 `mac-opencode`）。**提交流程只在該值等於驗章公開金鑰所屬的 Profile 時才接受寫入**；產生者最終以登錄的身分為準。
- `metadata`（必填）：共通收件匣 metadata，符合 `metadata-inbox.schema.json`（不含 `producer`）。若為 `type=session`，`metadata.id` 必須嚴格等於 `<source>:<source_session_id>`。
- `raw`（條件必填／可為 null）：若項目有本體，必須記載本體的 `sha256`（64 字元小寫 hex）與 `size`（位元組大小）；無本體之項目為 `null`。
- `session`（`type=session` 時必填）：
  - `source`：來源應用（如 `opencode`，不得為保留型態名稱，不得含冒號或空白）。
  - `source_session_id`：來源端內部 Session ID（不得含空白）。
  - `snapshot_at`：快照時間（RFC 3339 UTC `Z`）。
  - `status`：`"running"` 或 `"stopped"`。
  - `stopped_at`：當 `status="stopped"` 時必填 RFC 3339 UTC `Z` 字串；運作中為 `null`。
  - `in_progress`：布林值，表示最後一則 Assistant 訊息是否尚未生成完畢。
  - `parent_id`：若為子 Session 則記錄母 Session ID（需符合 Session ID 規範），否則為 `null`。

### 3. 依項目型態區分之 `body` 結構
- **`session`**：`{}`（空物件，主要資料位於 `session` 與 `.raw`）。
- **`handoff`（交接單）**：
  ```json
  {
    "target_session_id": "opencode:ses_001",
    "continuation": {
      "snapshot_sha256": "3a7b...",
      "message_id": "msg_010"
    },
    "content": "交接說明與承接上下文"
  }
  ```
- **`claim`（認領單）**：
  ```json
  {
    "handoff_id": "handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV",
    "claimer_session_id": "opencode:ses_002"
  }
  ```
  認領者必須已經在 Agora 裡——**唯一的例外是「預留」**：`agora checkout` 會讓這
  筆認領**自己帶著** claimer 的第一份快照（一個**零則訊息**的空匯出檔），sidecar
  多出 `session` 與 `raw`：

  ```json
  {
    "raw": { "sha256": "9c1e...", "size": 128 },
    "session": {
      "source": "opencode", "source_session_id": "ses_002",
      "snapshot_at": "2026-09-28T08:00:00.000Z",
      "status": "running", "in_progress": false, "parent_id": null,
      "reserving": true
    },
    "body": { "handoff_id": "handoff:01ARZ…", "claimer_session_id": "opencode:ses_002" }
  }
  ```

  為什麼：交接單只能被認領一次，所以認領被拒時 Agora 裡**不該留下任何東西**。
  預留在 `apply_claim` 的寫入階段才落進真本（順序：預留 → link → handoff →
  claim），所以被拒時連那個空紀錄都不會有。`session.reserving` 標記它是預留而不是
  真的同步；`session.source` + `source_session_id` 必須等於 `claimer_session_id`。
  帶 `session` 的 claim **必須**有 `raw`（反過來，沒帶預留的 claim 不該有 raw）。
- **`continuation`（接續單）**：**接續不需要交接單**——`agora checkout` 以
  `<session>[@<訊息>]` 為起點時送它，提交流程收進 Agora 後建出接續 Link。形狀與
  帶預留的 `claim` 相同（`session` ＋ `raw` 一起帶上，所以被拒時 Agora 裡連預留都
  不會有）：
  ```json
  {
    "raw": { "sha256": "9c1e...", "size": 128 },
    "session": {
      "source": "opencode", "source_session_id": "ses_002",
      "snapshot_at": "2026-09-28T08:00:00.000Z",
      "status": "running", "in_progress": false, "parent_id": null,
      "reserving": true
    },
    "body": {
      "target_session_id": "opencode:ses_001",
      "new_session_id": "opencode:ses_002",
      "continuation": { "snapshot_sha256": "3f7a…", "message_id": "msg_002" }
    }
  }
  ```
  `target_session_id` 是**被接續**的 Session（接續 Link 的 to 端，與交接單同一個
  詞）；`new_session_id` 是這次建出來的新 Session（Link 的 from 端，也是預留的那
  個，必須等於 `session.source`:`session.source_session_id`）。`continuation` 是接續
  點：`snapshot_sha256` MUST 是 `target_session_id` 的一份**既有快照**，`message_id`
  MUST 是該快照裡已完成、未撤銷的一則訊息（**不**要求是最後一則——直接起點可以停在
  中間）。同一個新 Session 對同一個起點只留一條 Link，重複送冪等。
- **`reference`（參考 Link）**：
  ```json
  {
    "from_session_id": "opencode:ses_001",
    "to_session_id": "opencode:ses_002",
    "read_snapshot_at": "2026-09-27T08:00:00Z"
  }
  ```
- **`rewrite`（改寫提案）**：
  ```json
  {
    "target_session_id": "opencode:ses_001",
    "base_snapshot_sha256": "4b8c...",
    "reason": "修訂機敏資訊與過時決策"
  }
  ```
  *註：新的原始紀錄本體必須放置於 `<item_key>.raw`。*
- **`artifact`（產出登錄）**：
  ```json
  {
    "kind": "link" | "contained",
    "content_type": "application/pdf",
    "produced_by_session_id": "opencode:ses_001",
    "filename": "summary.pdf",
    "link": "https://..." | null,
    "repo": "org/repo" | null,
    "path": "docs/summary.pdf" | null
  }
  ```
  *註：當 `kind="contained"` 時，`content_type` 必填，本體放置於 `<item_key>.raw`（預設上限 100 MiB，實際大小限制以提交流程設定為準）；當 `kind="link"` 時，`link` 欄位必填、`content_type` 選填且 `raw` 為 `null`，可選填 `repo` 與 `path`。*

### 4. 簽章範圍與防重放機制
- **分離式簽章演算法**：**Ed25519**。
- **簽章資料計算**：
  $$\text{Payload} = \mathtt{b"aistorage.inbox/v1\backslash n"} + \text{sidecar\_bytes}$$
  簽章直接針對 `<item_key>.sidecar.json` 檔案的原始位元組加上格式前綴進行計算，不依賴 JSON 物件的重新序列化（免除跨語言 canonical JSON 實作分歧）。
- **完整性傳遞**：由於 `raw.sha256` 記錄於 sidecar 檔案中，對 sidecar 檔案的簽章同等保證了 `.raw` 本體的完整性。
- **防重放機制（Anti-Replay）**：
  1. 檔名的 `item_key` 必須與 sidecar 內部的 `item_key` 欄位完全相符。
  2. 提交流程會拒收比真本現有版本舊或相同的更新（`session` 依據 `snapshot_at` 與雜湊判斷、`reference` 比對 `read_snapshot_at`、其他項目比對 `updated_at`）。
  3. 提交流程將已處理之 `item_key` 記於真本清冊中，拒絕重複消費。
  4. 讀取視圖僅發佈真本項目，不發佈收件匣之 sidecar 與簽章檔案原文。
