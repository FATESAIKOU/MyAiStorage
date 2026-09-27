# 搜尋索引格式與查詢語意規格（TS 用戶端與 Python 共用）

依據：`docs/impl/group4-modules.md` 第 3 節（PM 決定 1、8 全數採用）。
索引同時是讀取視圖的目錄（catalog）。實作見 `src/aistorage/search/`；
schema 以 `src/aistorage/search/schema.sql` 為準（TS 照抄該檔）。

## 1. 表

- `meta`：`format='aistorage.searchindex/v1'`、`generation`（整數世代）、
  `built_at`、`agora_main_sha`、`converter_versions`（JSON）。
- `sessions`：每個 Session 一列，最新快照的 metadata（欄位見 schema.sql）。
  `reading_status='failed'` 的 Session 只有 metadata，沒有全文。
- `snapshots`：每個 Session 的快照歷史（`via` 為 sync／rewrite／import）。
- `readings`：有發佈 reading 檔的快照（最新＋被釘住的），`is_latest` 標最新；
  `file_id`／`sha256`／`size` 供讀者下載驗證。
- `links`：`kind` 為 `continuation`（附 `handoff_id`、`claim_id`、
  `snapshot_sha256`、`message_id`）或 `reference`（附 `reference_id`、
  `read_snapshot_at`）；`links_to` 索引供反向查詢。
- `handoffs`：`body_json` 是寫入者提供的交接內容（原樣）；
  `claimed_by_*` 是提交流程寫入的狀態；未認領時三者皆為 NULL。
  `author_session_id` 是寫這張交接單的 Session id（publisher 填入）：
  真本裡的來源依序為 `body.author_session_id`、metadata 的
  `author_session_id`（`metadata-record` 允許擴充欄位）。兩者都沒有就是
  **作者不明 → NULL**，提交流程不猜測、也不以 `target_session_id` 頂替。
  讀取端列待認領交接單時只看作者為**主** Session 的（`parent_id` 為空）；
  作者為 NULL、或在 `sessions` 表查無此 id，都視為作者不明而排除
  （fail-closed：寧可少列，不可把子 Session 寫的交接單當成主 Session 寫的）。
  舊世代 index 若無此欄，讀取端必須報錯而非默默不篩。
- `rejections`：只有 `item_key`、`code`、`at`、`item_id`，
  `authenticated` 標是否通過驗章；**沒有內容**。
- `message_fts`／`title_fts`：FTS5（`tokenize='trigram'`），只放**最新快照**
  的閱讀版；`message_fts.text` 取每則**非撤銷**訊息的文字（不含 reasoning），
  撤銷的訊息不索引。

## 2. 時間格式

索引裡的時間一律存成 UTC 到毫秒的**固定寬度**字串
（形如 `2026-09-27T08:00:00.000Z`），字串比較與時間比較的順序一致。
查詢帶入的時間（`updated_after`／`updated_before`、cursor）一律先正規化
到同一形狀再比較。

## 3. 全文語意（`text`）

- 「命中」的定義：訊息文字**包含** query 字串。**子字串比對，只有 ASCII
  不分大小寫；不做 NFKC 或全形半形的轉換。**這等於 SQLite trigram
  tokenizer 的預設語意，也等於 `LIKE`（ASCII 不分大小寫）的語意。
- `text` 同時比對**訊息文字與標題**（子字串，同一 AND 語意；只有 ASCII
  不分大小寫，不做 NFKC）。只命中標題的 Session 也回傳，`matches` 為空
  （標題見 `session.title`）。
- 多個詞以空白分隔，**全部**都要命中（AND，同一則訊息內）；
  支援 `"…"` 引號包住的片語；**不支援** FTS5 的運算子語法
  （query 在程式裡逐字跳脫，全部按字面比對）。
- 3 字元以上走 FTS，未滿 3 字元（含查詢中任一片語／詞不足 3 字元）走
  LIKE，**兩者結果完全相同**。`matched_by`（`fts`／`like`／`filter`）
  只是除錯資訊，不屬於規格。
- 回傳命中的 `message_id` 與 `index`（訊息在閱讀版中的位置），以及 snippet。
  snippet 的標記與前後文長度**不屬於規格**（兩邊實作可以不同）；
  黃金測資只比 Session 順序與 `message_id`。

## 4. 篩選語意

- `title_contains`：子字串，語意同第 3 節（標題欄位）。
- `case_id`、`source`、`status`、`producer`：相等比對。
- `updated_after`／`updated_before`：RFC 3339，依 datetime 比較
 （含邊界；先正規化到第 2 節形狀）。
- `parent_id`：`None` 表示不限；`""` 表示只要主 Session。
- `in_progress`：布林相等比對。

## 5. 排序與分頁

- 一律 `updated_at` 由新到舊，平手時依 `session_id`（**不使用** bm25），
  Python 與 TS 的結果在逐筆比較時完全一致。
- `limit`（預設 50，上限 500）＋`cursor`（上一頁最後一筆的
  `(updated_at, session_id)`）。`limit` 內筆數不足時 `next_cursor` 為空。

## 6. 新鮮度規則（4.4 `reader/freshness` 實作，查詢語意的一部分）
- 讀取可指定新鮮度要求（最多落後多久）。每筆結果都附快照時間；
  Session＝`snapshot_at`，交接單與 Link＝該世代的 `published_at`。
- 沒有指定要求時不產生警告，但一定附上快照時間。
- `status == "stopped"` 而且 `snapshot_at` 在 `stopped_at` 之後
  （datetime 比較）→ 視為符合，不論落後多久。
- 否則以「讀者時鐘 − 快照時間」是否在要求內判定；未達時照樣回傳，
  附警告與實際快照時間。讀取不觸發任何同步或提交流程。

## 7. 讀取介面 find 的輸出形狀（4.3 reader）

- `find_sessions` 回傳每筆各帶自己的 Freshness：
  `{hit: {session: {...}, matches: [{message_id, index, snippet}]}, freshness: {...}}`，
  排序與第 5 節相同；清單整體的 freshness 以最舊的一筆為準。
  TS 實作 reader 時照此形狀輸出，`snippet` 內容與 `matched_by` 不列入比對。
