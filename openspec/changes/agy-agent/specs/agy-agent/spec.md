## ADDED Requirements

### Requirement: agy 的匯入（唯讀、經快照）
`agora import session --external-session-id <agy 對話 id> --agent agy` MUST 從 `$AGORA_AGY_HOME/conversations/<id>.db` 讀一個對話（`AGORA_AGY_HOME` 預設 `~/.gemini/antigravity-cli`，測試一律指到暫存目錄）。這條路徑 MUST 唯讀：MUST NOT 在 agy 的目錄裡開任何檔案寫入，MUST NOT 呼叫 agy 的 CLI。

讀取 MUST 經一份一致的快照，不直接匯出原檔：

- 目標旁沒有 `-wal`（閒置的 agy）時，MUST 複製單一 `.db`，用 `immutable=1` 開。
- 目標旁有 `-wal` 時（一個回合正在跑），MUST 複製 `.db` 與 `-wal`，在暫存目錄用一般方式開啟讓 SQLite 重放，再 `VACUUM INTO` 收成單檔。MUST NOT 複製 `-shm`（SQLite 會自己重建）。
- 複製前後 MUST 比對 `(size, mtime_ns)`；檔案被 checkpoint 移動或在複製途中消失時 MUST 重試，最多 3 次；三次都失敗 MUST 是明確的 `AgentError`，MUST NOT 吐半份快照。
- 匯出前 MUST 對快照做 `quick_check`；壞掉的 db MUST 拒絕，暫存目錄清乾淨。
- 對話 id MUST 只是一個單純的檔名：含路徑分隔、`..`、NUL 或過長時 MUST 拒絕；檔案不存在 MUST 明說找不到。

匯出的 raw MUST 包含整包 db（含 `gen_metadata` 等每一列，閒置路徑逐 byte 相同，不改寫）。匯入的 Session MUST 記下來源 agent（`agy`）與來源對話 id；標題取 summaries 的 `title`，`agora.source.dir` 取 `workspace_uris` 的第一筆並去掉 `file://`；`agent_version`、`created_at`、`model` 在 db 裡沒有已確認的欄位，MUST 留空。快照的步數與 summaries 的 `step_count` 不符時 MUST 只警告（以快照為準），MUST NOT 因此拒絕讀取。

#### Scenario: 閒置的對話
- **WHEN** 假 home 裡有一個沒有 `-wal` 的對話 db，執行 `agora import session --external-session-id <id> --agent agy`
- **THEN** 匯入成功，stdout 印出一個新的 `agora:<ULID>`；raw 的 `db_b64` 解出來與原 db 逐 byte 相同；`conversations/` 沒有被新增或改寫任何檔案

#### Scenario: 回合正在跑（有 `-wal`）
- **WHEN** 假對話 db 旁有一個有內容的 `-wal`，另外放一個 `-shm`
- **THEN** 快照包含 `-wal` 裡已提交的回合（閱讀版看得到）；`-shm` 沒有被複製；匯入成功

#### Scenario: 壞掉的 db
- **WHEN** `conversations/<id>.db` 不是可用的 SQLite 檔（或過不了 `quick_check`）
- **THEN** 匯入以 `AgentError` 拒絕，訊息說明快照讀不到；沒有留下暫存檔

#### Scenario: 走不出 conversations 的 id
- **WHEN** `--external-session-id` 是 `../x`、含 `/`、含 NUL 或過長
- **THEN** 拒絕，不會讀到 `conversations/` 以外的檔案

#### Scenario: 步數不符只警告
- **WHEN** summaries 的 `step_count` 與快照的步數不同
- **THEN** stderr 印一次「以快照為準」的警告；匯入照常完成

### Requirement: agy 的列表與搜尋只到標題與 preview
agy 的清單 MUST 來自 `conversation_summaries.db`，新到舊排序；標題取 `title`（去空白），沒有標題時取 `preview` 的第一行前 60 字；`dir` 取 `workspace_uris` 的第一筆去掉 `file://`。讀不到 summaries 檔時 MUST 只警告一次、其餘讀取照常；summaries 的表結構認不得時 MUST 是 `AgentError`（MUST NOT 回一個空清單假裝沒有對話）。

搜尋 MUST 只比對正規化後的標題與 preview，MUST NOT 為了搜尋去讀對話內文。`agora --help` 與互動模式的內文搜尋說明 MUST 寫明「agy 只搜標題與 preview（不搜內文）」，讓「找不到」不被當成「不存在」。

「最後一則」MUST 在選取時取一份快照，role 由步驟型別決定（14 使用者、15 模型），跳過沒有文字的工具呼叫步，文字上限 2000 字；讀不到時 MUST 回 `None`，MUST NOT 讓整個清單失敗。

#### Scenario: 標題命中
- **WHEN** 某個對話的標題含關鍵字，執行搜尋
- **THEN** 該對話的 id 出現在結果裡

#### Scenario: 只有內文含關鍵字
- **WHEN** 關鍵字只出現在對話內文（標題與 preview 都沒有）
- **THEN** 未匯入的搜尋找不到它；互動模式的狀態列說明 agy 只搜標題與 preview

#### Scenario: 認不得的 summaries 表
- **WHEN** `conversation_summaries.db` 裡找不到可用的對話表
- **THEN** 列出 agy 對話時是 `AgentError`，不是空清單

#### Scenario: summaries 壞掉但匯入照常
- **WHEN** `conversation_summaries.db` 壞掉，但對話 db 本身完好
- **THEN** stderr 警告一次；該對話仍然可以匯出與閱讀

### Requirement: raw 信封 `agy-conversation/1`
agy 的 raw MUST 是一個 JSON 物件，`format` 恰為 `agy-conversation/1`，含 `agy_version`（不知道時 `null`）、`trajectory_id`（對話內部的 uuid）、`scan_text`（步驟裡可 UTF-8 解碼且 ≥ 4 bytes 的字串，不截斷、去重、保序）與 `db_b64`（整包 db 的 base64，串流寫出）。位元組順序 MUST 固定：同一份未變的快照匯兩次，raw 的 md5 與大小 MUST 相等。

`scan_text` MUST 涵蓋每個步驟的 `step_payload`／`render_info`／`metadata` 裡的所有 length-delimited 欄位；`gen_metadata`／`executor_metadata`／`trajectory_metadata_blob` 是設定與系統提示，MUST NOT 放進 `scan_text`（它們只在 `db_b64` 裡）。

閱讀版 MUST 只含 14（使用者）與 15（模型）的文字與工具呼叫行；工具結果 MUST NOT 出現在閱讀版；132（工具步驟）是已認得、無閱讀文字，MUST NOT 產生 `[skip]`；未知的步驟型別 MUST 看得到並只警告一次。解信封時 MUST 拒絕不是 JSON 或 `format` 不認得的 raw。

#### Scenario: 同一份 db 匯兩次
- **WHEN** 對同一份未變的假對話匯出兩次
- **THEN** 兩次的 raw md5 與大小相同；`db_b64` 解出來是同一份 db

#### Scenario: 工具輸出的兩個面
- **WHEN** 對話裡有一個工具呼叫與它的輸出
- **THEN** `scan_text` 有完整的工具輸出；閱讀版只有工具呼叫那一行，沒有工具結果

#### Scenario: 認不得的信封
- **WHEN** raw 不是 JSON，或 `format` 不是 `agy-conversation/1`
- **THEN** 以 `AgentError` 拒絕，訊息說明信封格式認不出來

### Requirement: `continue --agent agy` 只支援同 agent
`agora continue session <id> --agent agy` MUST 只在 Session 的來源 agent 是 agy、而且不是 merge 出來的時候執行。它 MUST 先還原對話檔到 `conversations/<cascade_id>.db`（檔名 MUST 用還原內容自己的 `trajectory_meta.cascade_id`，不是信封裡的 `trajectory_id`），再確保 `conversation_summaries.db` 有那個 id 的一列：沒有這一列，agy 會回 `conversation "<id>" not found` 並開一個空對話。

- 還原 MUST 用 Session 記的 `agora.source.session_id` 當預期 id；Session 沒有記來源 id 時 MUST 在寫入任何東西之前拒絕；raw 指到別的對話時也 MUST 拒絕。
- 碰 `conversations/` 之前 MUST 先唯讀預檢 summaries：檔案存在、表認得、要寫的列建得出來（`NOT NULL` 欄位都有值）；預檢失敗 MUST NOT 寫對話檔。
- 目標旁邊有任何 `-wal` 或 `-shm` 時 MUST 拒絕，訊息要帶完整路徑；`-wal` 是 0 位元組時訊息 MUST 說 agy 沒在跑時可以手動刪掉，有內容時 MUST 說不要刪、等 agy 結束（或讓 sqlite3 開一次 checkpoint）。
- 啟動 MUST 是 `agy --conversation <cascade_id>`，工作目錄是 `--dir`；MUST NOT 自己發明 agy 的旗標。`--headless` 與 `--harness-file` MUST 在啟動前拒絕。
- 接續中的每次收集 MUST 取一份快照；對話沒有比啟動時多出內容時 MUST 回 `None`；對話 db 整個不見時 MUST 是明確的 `AgentError`，訊息說明 agy 可能換了對話 id，MUST NOT 安靜地當成沒有新內容。

#### Scenario: 對話檔不見了
- **WHEN** 匯入過的 agy 對話已從 agy 刪掉，執行 `agora continue session <id> --agent agy`
- **THEN** agora 還原對話檔、補上 summaries 列，開 `agy --conversation <cascade_id>`；agy 看得到原本的對話

#### Scenario: 列已經在了
- **WHEN** 目標對話與 summaries 列都還在
- **THEN** 對話檔的內容裁決照常（見下一則 Requirement），已有的列不被改寫；agy 照常開啟

#### Scenario: Session 沒有來源 id
- **WHEN** Session 的 header 沒有 `agora.source.session_id`
- **THEN** 在寫入任何東西之前拒絕，訊息說明不猜是哪個對話

#### Scenario: raw 指到別的對話
- **WHEN** raw 的 `cascade_id` 與 Session 記的來源 id 不同
- **THEN** 拒絕，活體與 summaries 都沒被動到

#### Scenario: 旁邊有 sidecar
- **WHEN** 目標對話旁有 `-wal`／`-shm`
- **THEN** 拒絕，訊息帶路徑；`-wal` 是 0 位元組時說可以手動刪，有內容時說不要刪

#### Scenario: headless
- **WHEN** `agora continue session <id> --agent agy --headless`
- **THEN** 在同步與任何寫入之前拒絕，exit 1，訊息說明 agy 的 headless 在後續

#### Scenario: harness 檔
- **WHEN** `agora continue session <id> --agent agy --harness-file <path>`
- **THEN** 在啟動之前拒絕，exit 1，訊息說明 `--harness-file` 不支援 agent agy

#### Scenario: 接續中 db 不見
- **WHEN** 接續期間 agy 把對話換了 id（db 不見）
- **THEN** 收集以 `AgentError` 明說「agy 可能換了對話 id」，不會當成沒有新內容

### Requirement: 接續的內容裁決與失敗收回
還原 MUST 用內容決定要不要覆蓋活體：比對 `(count(*), max(idx))` 與 summaries 的時刻，MUST NOT 用 mtime。

- 活體相同或較新時 MUST NOT 寫入，直接開活體；活體較新時 MUST 在 stderr 說明兩邊的步數。
- 活體較舊、要覆蓋時 MUST 先備份活體到 agora 自己的 state（`agy-backups/`，原子、權限 0700／0600）：第一份是 `<id>.db`（使用者原本的 agy 版本，永久保留），之後是 `<id>.<UTC 時間>.db`，只留最新的 3 份；備份失敗 MUST 停止覆蓋。覆蓋 MUST 經 `conversations/` 裡的暫存檔、先 `quick_check` 再 `os.replace`。
- 補 summaries 列失敗時（鎖逾時、`NOT NULL`、磁碟，含 Ctrl-C）MUST 收回這次的還原：目標本來不存在就刪掉剛寫的檔；活體被覆蓋過就從備份放回去（暫存檔＋`os.replace`）；只被讀過（內容相同、沒寫）的檔 MUST NOT 動。錯誤 MUST 說明補列失敗的原因與收回了什麼。
- 前次中斷留下的 `.restore-*.tmp` MUST 在超過 1 小時後由後續的還原清掉；較新的 MUST NOT 動。

#### Scenario: 活體較新
- **WHEN** agy 那邊的對話比 Session 的 raw 新
- **THEN** 不覆蓋、不備份，直接開活體；stderr 說明兩邊的步數

#### Scenario: 活體較舊
- **WHEN** 活體比 raw 舊
- **THEN** 覆蓋前備份到 agora 的 state 並在 stderr 說明；agy 開到的是 raw 的內容

#### Scenario: 補列失敗
- **WHEN** 還原寫了檔，但 summaries 的插入失敗
- **THEN** 剛寫的檔被刪掉（或活體被放回），不留「有對話檔、沒有 summaries 列」的狀態；錯誤說明原因

### Requirement: 不支援的動作在邊界拒絕
下列動作 MUST 在讀取來源內容、啟動 agent 或呼叫模型之前以 exit 1 拒絕（本機索引已知的來源會在同步之前就拒絕），訊息 MUST 用中文說明是 agy 第一版的邊界，讓呼叫方不會把它當成模型回得不好而重試：

- `agora merge --agent agy`，以及任何來源是 agy 匯入 Session 的 merge（以 agy 寫要約）。
- 跨 agent 的 continue：agy 匯入的 Session 用 claude／opencode 接續，或反過來；agy 接續一個 merge 出來的 Session 也算。
- `agora continue --agent agy --fork` 與 `--headless`。
- 轉接器自己的 `native`、`append_native`、`summarize` MUST 以同一個唯讀訊息拒絕。

#### Scenario: merge 用 agy
- **WHEN** `agora merge session <id1> <id2> --agent agy`
- **THEN** exit 1，訊息說明 merge 要用 opencode／claude；沒有同步、沒有讀來源、沒有叫模型

#### Scenario: 跨 agent continue
- **WHEN** 來源是 agy 匯入的 Session，`agora continue session <id> --agent claude`（或來源是 claude、目標 agy）
- **THEN** exit 1，訊息說明接續只支援同 agent；agent 沒有被啟動

#### Scenario: fork 或 headless
- **WHEN** `agora continue session <id> --agent agy --fork`（或 `--headless`）
- **THEN** exit 1，在讀取與任何寫入之前拒絕

#### Scenario: 轉接器的三個方法
- **WHEN** 呼叫 `native`、`append_native` 或 `summarize`
- **THEN** 都丟同一個唯讀 `AgentError`（agy 第一版只支援 import／讀取與同 agent 的互動接續）

### Requirement: agy 的憑證檔不讀
agy 的轉接器 MUST 只碰 `$AGORA_AGY_HOME` 下的 `conversations/` 與 `conversation_summaries.db`（接續的備份寫在 agora 自己的 state，不寫進 agy 的 home）。MUST NOT 讀 agy 目錄裡的憑證檔（例如 `google_accounts.json`、`antigravity-oauth-token`），也 MUST NOT 為了 import／列表／搜尋／預覽／最後一則去讀 `brain/` 等其他目錄。

#### Scenario: 憑證檔放在假 home
- **WHEN** 假 `AGORA_AGY_HOME` 裡放了 `google_accounts.json` 與 `antigravity-oauth-token`，然後列表、匯入、預覽與最後一則
- **THEN** 這兩個檔案都沒有被打開；agy 的 home 只有 `conversations/` 與 `conversation_summaries.db` 被讀

#### Scenario: 接續只寫兩個地方
- **WHEN** 執行 `agora continue --agent agy`
- **THEN** agy 的 home 裡只有 `conversations/<id>.db` 與 `conversation_summaries.db` 被寫；活體備份在 agora 的 state
