## ADDED Requirements

### Requirement: 四個動作
sidecar 對外 MUST 只有四個動作：開啟（open）、關閉（close）、搜集狀態（status）、戳（poke）。MUST NOT 有暫停或其他動作；「暫停」以 poke 加上中斷達成。

#### Scenario: 暫停就是戳加中斷
- **WHEN** 控制者對一個正在跑的 agent `poke`，`interrupt` 為真，內容是「停下來等指示」
- **THEN** agent 這一輪被打斷，接著收到那句話，停在提示處

### Requirement: 開啟
`open` MUST 收：呼叫方給的 `id`、agent 種類、工作目錄（存在的絕對路徑，不存在 MUST 回 400，sidecar MUST NOT 建目錄）、繼承的 Session（new／continue／fork／merge；merge 可帶選填的 `by`，預設是 agent 種類）；subscription 的 id 與第一則輸入 MUST 是選填（subscription 省略時，sidecar 依挑選規則自己挑一個現在能用的）。呼叫方指定的 subscription 在冷卻中時 MUST 照辦。`id` 對應的 agent 還在 starting／running／idle 時 MUST 拒絕；在 waiting-quota 或 exited 時 MUST 用同一個 id 重開，waiting-quota 的自動重開 job MUST 一併刪掉。sidecar MUST 自己向 subscription 來源取得詳細資料（呼叫方不傳 key；merge 的 `by` 與 agent 種類不同時，替 `by` 另外用挑選規則挑一把），把它轉成 agent 的環境變數，放在子行程的環境裡；這些值 MUST NOT 出現在命令列、log 或任何檔案。開出來的 Session MUST 在 agora 標頭的 tags 附加 `sidecar:<id>`，原有的 tags MUST NOT 被取代。第一則輸入 MUST 排進該 agent 的佇列（等它起來再送），MUST NOT 因為它而讓 open 失敗；省略 subscription 而且沒有任何能用的候選時 MUST 回 409 `no_subscription`。

#### Scenario: fork 一個 team 的 home Session
- **WHEN** `open`（id=t1、claude-code、fork 自 H、subscription=claude-personal、第一則輸入「讀 channel X」）
- **THEN** agora 上多一個從 H fork 出來、tags 含 H 原有的 tags 與 `sidecar:t1` 的 Session；agent 在它上面開著，收到第一則輸入；H 不變；`status` 的 session 是新的那個 id

#### Scenario: id 重複
- **WHEN** t1 正在跑時再 `open` id=t1
- **THEN** 被拒絕（HTTP 409），什麼都沒開

#### Scenario: 重開已結束的 id
- **WHEN** t1 已經 exited，再 `open` id=t1（continue 它的 Session）
- **THEN** t1 重新開起來，接著同一個 Session

#### Scenario: 重開 waiting-quota 的 id
- **WHEN** t1 是 waiting-quota 而且有自動重開 job，控制者 `open` id=t1（continue）
- **THEN** t1 重新開起來，自動重開的 job 從 `GET /jobs` 消失

#### Scenario: 省略 subscription
- **WHEN** `open` 沒有給 subscription，而 `list` 裡 claude-personal（priority 10）與 claude-company（priority 20）都能用
- **THEN** 用 claude-personal 開

#### Scenario: 指定的 subscription 在冷卻中
- **WHEN** `open` 指定了正在冷卻的 claude-personal
- **THEN** 照指定的開（`get` 照回）；若一開起來就回報用完，走換把

#### Scenario: subscription 不能用
- **WHEN** 指定的 subscription 不支援這個 agent 種類（403）、不存在（404）或 key 沒部署（409）
- **THEN** 分別回 400 `subscription_not_allowed`、400 `subscription_not_found`、409 `not_deployed`，什麼都沒開

#### Scenario: LLMGateway 連不上
- **WHEN** `open` 時 subscription 來源回報無法確認（unreachable）
- **THEN** 回 503 `gateway_unreachable`，什麼都沒開、沒有掛 job

#### Scenario: merge 指定要約的 agent
- **WHEN** `open` 的 session 是 `merge`、`by` 是 opencode、agent 是 claude-code，subscription 指定 claude-personal
- **THEN** 另外替 opencode 用 `Selector` 挑一把（經 `get(id, opencode)`），要約用 opencode 寫、環境變數來自那一把；之後以 claude-personal 的 claude-code 接續那個新 Session

#### Scenario: merge 的要約沒有可用的 subscription
- **WHEN** `by` 的種類沒有任何能用的 subscription
- **THEN** 回 409 `no_subscription_for_merge`，什麼都沒開

#### Scenario: 工作目錄不存在
- **WHEN** `open` 的 `workdir` 不存在
- **THEN** 回 400，什麼都沒開

### Requirement: 關閉
`close` MUST 先溫和停下 agent，再讓 agora 把最後的對話寫回；溫和停不下來時，逾時後強制結束。`close` 不存在的 id MUST 回 404。

#### Scenario: 關閉
- **WHEN** 對 t1 `close`
- **THEN** agent 結束；它最後的對話在 agora 的那個 Session 上；`status` 顯示 exited

#### Scenario: 關閉不存在的 id
- **WHEN** 對沒開過的 id `close`
- **THEN** 回 404

### Requirement: 搜集狀態
`status` MUST 只列這個 sidecar 開的 agent：id、agent 種類、agora Session（目前跑的那個）、subscription id、狀態（starting／running／idle／waiting-quota／exited）、開始時間；waiting-quota 而且有自動重開 job 時附預定重開的時刻（`reopen_at`），否則是 null。MUST NOT 含任何 key。`status` MUST 讀記憶體裡最後一次的狀態（最多晚一個 tick），MUST NOT 因為被查詢而觸發任何狀態轉移或網路操作。

#### Scenario: 不列別人的
- **WHEN** 同一台機器上有一個不是 sidecar 開的 Claude Code
- **THEN** `status` 不列它

#### Scenario: waiting-quota 的預定時刻
- **WHEN** t1 因全部冷卻而 waiting-quota
- **THEN** `status` 的 t1 有 `reopen_at`，等於自動重開 job 的 `at`；`status` 的任何欄位都沒有 key

### Requirement: 戳
`poke` MUST 把一則文字送進 agent，成為它讀得到的下一則輸入；`interrupt` 為真時 MUST 先打斷這一輪再送，為假時 MUST NOT 打斷（agent 正在跑時就排到這一輪之後）。id 不存在 MUST 回 404；agent 是 exited 或 waiting-quota 時 MUST 回 409 且不排隊；agent 是 starting 時 MUST 排隊到它起來之後再送，逾時 MUST 回 409。

#### Scenario: 不打斷
- **WHEN** agent 正在跑一輪時 `poke`（不中斷）
- **THEN** 這一輪照常完成，之後 agent 讀到那則文字

#### Scenario: 戳不存在或已結束的
- **WHEN** `poke` 一個沒開過的 id，或 exited 的 id
- **THEN** 前者 404，後者 409

#### Scenario: starting 時排隊
- **WHEN** agent 還在 starting 時 `poke`
- **THEN** 起來之後收到那則文字；逾時還沒起來則回 409

### Requirement: 三種外部控制形式
控制者 MUST 能用 HTTP 以三種形式下四個動作：立即（直接呼叫）、定時（job 的 `at`）、輪詢（job 的 `every`，可設 `until`）。job 的動作 MUST 只能是四個動作之一，否則 MUST 回 400；帶 `session.mode == "merge"` 的 `open` MUST NOT 出現在 job 裡（回 400 `merge_not_allowed_in_job`）；`at` 在過去 MUST 回 400。`GET /jobs` 的每一筆 MUST 有 `last_run` 與 `last_error`（不含 key）。HTTP MUST 只綁本機（127.0.0.1），不做認證。請求本文超過 1 MB MUST 回 413。`every` 的下一次執行時刻 MUST 是原本節奏上第一個晚於現在的預定點，MUST NOT 在停頓之後補跑（只補執行一次，之後照原本節奏）。

#### Scenario: 定時開啟
- **WHEN** 掛一個 job：`at` 明天 9:00、動作 `open`
- **THEN** 到時 sidecar 開那個 agent；`GET /jobs` 在那之前看得到這個 job

#### Scenario: 輪詢
- **WHEN** 掛一個 job：`every` 600、動作 `poke`、`until` 一小時後
- **THEN** 每 10 分鐘戳一次，到 `until` 之後不再執行；`last_run` 是最近一次的時刻

#### Scenario: 刪除與拒絕
- **WHEN** `DELETE /jobs/{id}`；或掛一個動作不是四個之一的 job
- **THEN** 前者之後 `GET /jobs` 沒有它；後者回 400

#### Scenario: job 失敗留下紀錄
- **WHEN** 一個 `every` 的 `open` 撞到 409
- **THEN** 該筆的 `last_error` 有 409 與訊息，沒有 key；job 不自動重試

#### Scenario: 只綁本機
- **WHEN** sidecar 啟動
- **THEN** 它 listen 的位址是 127.0.0.1

### Requirement: 額度用完自動換把
agent 回報額度用完時，sidecar MUST 向 subscription 來源登錄冷卻（窗只有 5h 與 1w 兩種；重置時刻選填，拿不到時 MUST NOT 自己猜），再從一覽挑一個現在能用、支援這個 agent 種類、而且不是剛用完的那一把的 subscription（挑法是可替換的介面，預設照 priority）；挑到就停下 agent、等 agora 寫回、用 continue 同一個 Session 以新的 subscription 重開，id 不變，成功後 MUST 送一則 `resume_prompt`（設定；空字串就不送）。登錄冷卻失敗（被拒絕或無法確認）時 MUST 仍然換把，而且 MUST NOT 挑回同一把。全部冷卻時 MUST 停下、寫回、狀態標 waiting-quota，並自動掛一個在最早解除冷卻的時刻 open 同一個 id（continue 同一個 Session、不指定 subscription）的 job；最早解除的時刻 MUST 是各候選（支援這個種類、key 已部署）的兩個窗口冷卻中較晚者的最小值。沒有任何候選時 MUST NOT 掛 job。自動重開的 open 失敗時 MUST 重新計算再掛一次；subscription 來源無法確認時 MUST 在固定間隔（`retry_interval`）之後再掛一次。

#### Scenario: 換到另一把
- **WHEN** t1 用 claude-personal 時額度用完，claude-company 現在能用
- **THEN** claude-personal 被登錄冷卻；t1 以 claude-company 重開，接著同一個 Session，並收到 `resume_prompt`

#### Scenario: 全部冷卻
- **WHEN** t1 額度用完，而所有支援 claude-code 的 subscription 都在冷卻
- **THEN** t1 停下、寫回，狀態 waiting-quota；`GET /jobs` 有一個在最早解除時刻 open t1（continue）的 job

#### Scenario: 解除時刻看兩個窗口
- **WHEN** claude-personal 的 5h 冷卻到 15:02、1w 冷卻到 10-18，claude-company 的 5h 冷卻到 16:02，其餘的 key 沒部署
- **THEN** job 的 `at` 是 16:02（claude-personal 要 10-18 才能用，沒部署的不算）

#### Scenario: 冷卻登錄失敗不原地打轉
- **WHEN** t1 用完時 `cooldown` 被拒絕，而 `list` 仍把 claude-personal 當 available
- **THEN** 不會挑回 claude-personal；有其他能用的就換過去，沒有就走全部冷卻

#### Scenario: 重置時刻被拒絕時去掉重送
- **WHEN** driver 給的 `resets_at` 被 LLMGateway 以 `invalid_resets_at` 或 `resets_at_out_of_range` 拒絕
- **THEN** sidecar 不帶 `resets_at` 重送一次

#### Scenario: 無法確認
- **WHEN** 換把時 subscription 來源無法確認
- **THEN** t1 停下、寫回，狀態 waiting-quota，`GET /jobs` 有一個在固定間隔之後 open t1 的 job

#### Scenario: 自動重開時還是挑不到
- **WHEN** 自動重開的 job 到時，沒有能用的 subscription
- **THEN** 重新計算最早解除的時刻，再掛一個 job；t1 維持 waiting-quota

#### Scenario: 刪掉自動重開的 job
- **WHEN** 控制者 `DELETE` 那個 job
- **THEN** t1 維持 waiting-quota，`reopen_at` 是 null

### Requirement: 狀態揮發與結束
agent 清單與 job 清單 MUST 只在記憶體。sidecar MUST NOT 在重啟後找回先前的 agent 或 job。收到 SIGTERM 時 MUST 先對每個 agent 做 close（停下、寫回），全部做完才結束。連線設定與其他設定的改變 MUST NOT 需要重啟 sidecar。

#### Scenario: 重啟
- **WHEN** sidecar 重新啟動
- **THEN** `status` 是空的、`GET /jobs` 是空的；先前的對話仍在 agora 上

#### Scenario: SIGTERM
- **WHEN** sidecar 收到 SIGTERM，有兩個 agent 在跑
- **THEN** 兩個都先停下、寫回 agora，之後 sidecar 才結束

### Requirement: LLMGateway 的連線設定
LLMGatewaySource MUST 從獨立的連線設定檔（預設 `~/.config/sidecar/gateway-connect.json`，主設定的 `gateway_connect_file` 可改）取得連線資訊，**每一次** LLMGateway 操作都重新讀取，MUST NOT 快取。讀不到或格式錯誤時 MUST 當作無法確認，不丟出未處理的例外。sidecar MUST NOT 讀 MyLinuxPool 的任何檔。

#### Scenario: 連線設定被改寫
- **WHEN** 外部以原子方式（寫暫存檔再 rename）換掉連線設定檔，sidecar 沒有重啟，之後有一次 `open`
- **THEN** 那次 `open` 用新的連線資訊

#### Scenario: 連線設定壞掉
- **WHEN** 連線設定檔不存在或不是合法的 JSON
- **THEN** `open` 回 503 `gateway_unreachable`，log 說明原因

### Requirement: 介面和實作分開
agent 種類（Claude Code、opencode）、Session 保存處（agora）、subscription 來源（LLMGateway、本機 JSON 檔）、挑選規則（priority）MUST 各自經由內部介面實作；換掉其中一個實作時，四個動作與 HTTP 介面 MUST NOT 改變。sidecar MUST NOT 依賴任何信道工具，也 MUST NOT 依賴 herdr。

#### Scenario: 用假實作測試
- **WHEN** 測試以假的 agent driver、假的 agora、假的 subscription 來源啟動 sidecar
- **THEN** 四個動作與 job 的行為和真的實作相同，不開真的 agent、不連網

#### Scenario: 環境變數不出現在命令列與 log
- **WHEN** 用假的 subscription（key 是一個可辨認的字串）開一個 agent
- **THEN** 該字串只出現在子行程的環境；命令列、sidecar 的 log、任何檔案裡都找不到

### Requirement: 狀態只由 scheduler 改變，每個 agent 同時只有一個轉移
agent 的狀態轉移（刷新、換把、自動重開、job 的執行）MUST 只在 scheduler 執行緒做；HTTP 執行緒 MUST NOT 觸發它們。每個 agent 同一時間 MUST 只有一個狀態轉移在進行；一個 agent 的錯誤 MUST NOT 影響其他 agent 的 `status`，也 MUST NOT 讓 `GET /agents` 回 500。`JobRegistry` MUST 是執行緒安全的。driver 與 store 的呼叫 MUST 在鎖外進行。

#### Scenario: GET /agents 不觸發換把
- **WHEN** driver 已經回報 t1 額度用完，控制者在 scheduler 的下一個 tick 之前呼叫 `GET /agents`
- **THEN** 回應是上一次的狀態（t1 仍是 running 或 idle）；沒有任何 `cooldown`、`list`、`get` 或 `stop` 被呼叫；之後的 tick 才換把

#### Scenario: 兩個執行緒同時看到額度用完只換一次
- **WHEN** t1 額度用完，兩個執行緒同時對它做狀態刷新，而 `cooldown` 要花 0.4 秒
- **THEN** `cooldown` 只登錄一次、`stop` 只一次、`continue` 只開一個新行程；t1 最後是 running（或 idle）在新的 subscription 上，沒有被第二次停掉

#### Scenario: 一個 agent 的錯誤不影響別的
- **WHEN** t1 的換把途中 `SessionStore.start` 丟例外，t2 正常在跑
- **THEN** `GET /agents` 回 200，t2 照常列出；t1 是 waiting-quota

#### Scenario: /jobs 併發不出錯
- **WHEN** 數個執行緒同時對 `/jobs` 做 `POST`、`GET`、`DELETE`，scheduler 同時在 tick，持續 2 秒
- **THEN** 沒有任何 500 或例外；job 的 id 沒有重複；`GET /jobs` 每次都回完整的清單

#### Scenario: every 停頓之後不補跑
- **WHEN** 一個 `every` 600 的 job，scheduler 停了 3000 秒後恢復
- **THEN** 只補執行一次，下一次是現在加上（到下一個預定點的）餘數，不是連續執行 5 次

#### Scenario: 本文太大
- **WHEN** `POST /agents` 的本文是 2 MB
- **THEN** 回 413，什麼都沒開

### Requirement: 關閉遇到 starting 或轉移中
`close` 遇到還在 `starting`（open 進行中）或正在做狀態轉移（`busy`，例如換把中）的 agent 時 MUST 記下 `closing`、回 200（不回 409）；做轉移的執行緒在寫回新的 handle 之前 MUST 檢查 `closing`，看到就停下剛啟動的行程並標 `exited`；scheduler 刷新時 ACTIVE 狀態的 agent 看到 `closing` MUST 停下並標 `exited`。最後的狀態 MUST 是 `exited`，行程確實被停掉、沒有殘留，也 MUST NOT 因為換把而復活。

#### Scenario: 關閉遇到 starting
- **WHEN** `open` 還在啟動 agent（`launch` 還沒回來）時，對同一個 id `close`，之後啟動完成
- **THEN** `close` 回 200；`status` 最後是 exited；那個剛啟動的行程被停掉（`stop` 被呼叫一次），沒有殘留

#### Scenario: 關閉與換把同時
- **WHEN** t1 的換把進行中（舊的已經停下、`SessionStore.start` 還沒回來），對 t1 `close`，之後換把的新行程啟動完成
- **THEN** `close` 回 200；新行程被停掉（`stop` 被呼叫），`status` 最後是 exited，不是 running／starting；`driver` 沒有留下沒被停的 handle

#### Scenario: 刷新時看到 closing
- **WHEN** t1 的 open 期間 `driver.state` 暫時丟了一次例外（狀態停在 starting），這時對 t1 `close`，之後 scheduler 刷新、driver 回 running
- **THEN** 這次刷新停下 t1 的行程並標 exited；不會留下一個 `idle` 卻已被關閉的 agent

#### Scenario: 換把完成後仍能再換
- **WHEN** t1 換把成功之後又回報額度用完
- **THEN** 會再換一次（上一次轉移的 `busy` 已經釋放）

### Requirement: 結束時先擋新請求再逐一關閉
sidecar 收到 SIGTERM 後 MUST 先進入「關閉中」：`open`、`poke`、`POST /jobs` MUST 回 503；到期的 job MUST NOT 再執行（不會再開 agent）；停掉 scheduler（`join` 有逾時：`close_timeout` 加 5 秒；逾時只記 log）之後才逐一 close 每個 agent，全部做完才結束。scheduler 的某一次 tick 卡住 MUST NOT 讓 SIGTERM 卡住或讓 agent 成為孤兒。`docs/sidecar.md` MUST 寫明 supervisor 的 kill timeout 要大於 `close_timeout × agent 數`。

#### Scenario: 關閉中到期的 job 不會再開 agent
- **WHEN** sidecar 進入關閉中的同時，一個 `open` 的 job 剛好到期
- **THEN** 那個 job 沒有執行，沒有新的 agent 被開；`GET /jobs` 看得到它還在（或 `last_error` 是 503），sidecar 結束

#### Scenario: 關閉中的新請求
- **WHEN** 關閉進行中收到 `POST /agents`
- **THEN** 回 503 `shutting_down`，什麼都沒開

#### Scenario: scheduler 卡住時 SIGTERM 照樣關閉
- **WHEN** scheduler 正在做的那一次 tick 卡在一個不回來的呼叫上，sidecar 收到 SIGTERM
- **THEN** 等 `close_timeout + 5` 秒後記 log，仍逐一 close 每個 agent 並結束；沒有 agent 因此沒被關閉

### Requirement: 換把中途失敗
換把途中任何一步失敗（停下之後的 `SessionStore.start`、`launch`、`get` 之後的任何例外）時，agent MUST 走 `waiting-quota`，MUST 掛一個 `now + retry_interval` 的 open job，MUST log 原因（不含 key），MUST NOT 讓 HTTP 回應變成 500，也 MUST NOT 影響其他 agent。

#### Scenario: 換把中途失敗
- **WHEN** t1 額度用完，已經挑到下一把並停下，但 `SessionStore.start` 失敗
- **THEN** t1 是 waiting-quota，`reopen_at` 是現在加 `retry_interval`，`GET /jobs` 有對應的 open job；到時重開成功，t1 回到 running

### Requirement: lgw 回應的對應
`LLMGatewaySource` MUST 把 `lgw.client` 的 `Result` 與 `lgw.connect` 的例外依下表對應；MUST NOT 把 `ok=False` 當成功；進入 `with lgw.connect.connect(...)` 時的例外 MUST 在來源內轉成 `Unreachable`，MUST NOT 穿出去：

| lgw | 回覆 |
|---|---|
| `ok` | 值 |
| `unreachable=True`、`Unavailable`、`load_config` 的 `OSError`／`ConfigError` | `Unreachable`（只寫種類，不寫 `data`） |
| `status` 403／404／409 | `agent_not_allowed`／`not_found`／`not_deployed` |
| `status` 400 | `SourceError(error 碼)`：只有 `invalid_resets_at`／`resets_at_out_of_range` 讓 `resets_at` 去掉重送；`bad_request`、`invalid_window` 不重送 |
| `status` 413 | `SourceError("body_too_large")` |
| `error` 是 `bad_response`，或其他沒列出的 `status` | `Unreachable` |

#### Scenario: 403
- **WHEN** `get(id, "opencode")` 對只允許 claude-code 的 subscription，lgw 回 `ok=False, status=403, error="agent_not_allowed"`
- **THEN** `SourceError("agent_not_allowed")`；open 回 400 `subscription_not_allowed`

#### Scenario: 404 與 409
- **WHEN** lgw 對 `get` 回 `status=404`，另一次回 `status=409, error="not_deployed"`
- **THEN** 分別是 `not_found`（open 400 `subscription_not_found`）與 `not_deployed`（open 409 `not_deployed`）

#### Scenario: cooldown 的 resets_at 被拒絕
- **WHEN** `cooldown(id, "5h", resets_at)` 回 `ok=False, status=400, error="resets_at_out_of_range"`（或 `invalid_resets_at`）
- **THEN** 回覆是 `SourceError`（不是成功）；換把去掉 `resets_at` 重送一次

#### Scenario: 其他的 400 不重送
- **WHEN** `cooldown` 回 `ok=False, status=400, error="invalid_window"`
- **THEN** 回覆是 `SourceError("invalid_window")`；不重送；換把仍排除剛用完的那一把繼續

#### Scenario: 回覆不是 JSON 物件
- **WHEN** lgw 回 `error="bad_response"`
- **THEN** 當作 `Unreachable`，走重試；不是成功

#### Scenario: list 的形狀
- **WHEN** lgw 的 `list()` 回 `ok=True, data={"schema": 1, "subscriptions": [...]}`
- **THEN** 轉成 `Subscription` 的列表（不含 key）；`get` 的 `data` 是平的一筆，含 key

#### Scenario: 連不上
- **WHEN** lgw 回 `unreachable=True`
- **THEN** `Unreachable`；open 回 503 `gateway_unreachable`

#### Scenario: 進入 with 時 Unavailable
- **WHEN** `lgw.connect.connect(cfg)` 在進入 `with` 時丟 `Unavailable("timeout")`
- **THEN** 來源回 `Unreachable`，沒有例外穿出去；open 回 503（不是 500），換把走 `waiting-quota` 與重試 job

### Requirement: 自動重開的候選與時刻
自動重開的候選 MUST 只有「支援這個 agent 種類而且 key 已部署」的 subscription；沒有部署 key 的（`available` 為 false 而且兩個窗口都沒在冷卻）MUST NOT 是候選。算出的解除時刻已過但候選仍不可用時，`at` MUST 是 `now + retry_interval`，MUST NOT 每個 tick 重掛。

#### Scenario: 未部署的不是候選
- **WHEN** t1 額度用完，唯一其他的 claude-code subscription 沒有部署 key（`available` false、兩個窗口都沒冷卻）
- **THEN** 沒有候選：t1 是 waiting-quota，`reopen_at` 是 null，`GET /jobs` 沒有自動重開的 job

#### Scenario: 解除時刻已過仍不可用
- **WHEN** 自動重開的 job 到時，`list` 仍回 claude-personal 冷卻（`until` 在 30 秒前）
- **THEN** 新掛的 job 的 `at` 是現在加 `retry_interval`，不是過去的時刻，也不是每個 tick 重掛

### Requirement: new 模式的換把與關閉先 import
`new` 模式（沒有 agora 行程）的 agent 在 close 與換把時 MUST 先用 `agora import` 存進 agora（拿到 agora id，已有的就是原地更新），再停下（溫和），再 import 一次寫回最後的對話；換把接著 `continue` 那個 agora id。close 前的 import 失敗 MUST 仍然停下 agent，並在回應裡說明沒有寫回（`exit.reason`）；close 不重試。換把前的 import 失敗的處理由下一條 Requirement 規定。

#### Scenario: new 模式換把先 import
- **WHEN** t1 是 `new` 模式、`status.session` 還是 null，額度用完且有可用的下一把
- **THEN** 順序是：import（取得 agora id）→ stop → import → `continue` 那個 agora id；`status.session` 變成那個 id，tags 含 `sidecar:t1`

#### Scenario: new 模式 close
- **WHEN** 對 `new` 模式的 t1 `close`
- **THEN** 先 import 再停下再 import；agora 上有這個 Session；`status` 是 exited

#### Scenario: close 前的 import 失敗仍然停下
- **WHEN** 對 `new` 模式的 t1 `close`，而 import 失敗（agora 或網路）
- **THEN** t1 仍然被停下、`status` 是 exited，`exit.reason` 說明沒有寫回；close 不重試，沒有留下活著的行程

### Requirement: 沒有 driver 時開不了 agent
在 driver 與 session store 實作之前，`sidecar run` MUST 仍然啟動、`GET /agents` MUST 有回應（健康檢查過），但 `POST /agents` MUST 回 400 `unsupported agent kind`；`docs/sidecar.md` MUST 說明這一點。

#### Scenario: 沒有 driver
- **WHEN** 沒有註冊任何 agent 種類的 `sidecar run`，`POST /agents`
- **THEN** 回 400 `unsupported agent kind`；`GET /agents` 回 200 與空清單

### Requirement: driver 的 env_for 規則
`env_for` MUST 依 design D6 的「`env_for` 規則」產生子行程的環境，並以純單元測試驗證輸出（不啟動真的 agent）。沒有可用的模型來源時 MUST 拒絕（open 回 400 `no_model`），MUST NOT 讓 agent 自己挑預設模型。打真 binary 的測試 MUST NOT 在預設的 unit 裡跑（預設 skip，`tests/integration/` 或 `SIDECAR_REAL_AGENT_TESTS=1`）。

#### Scenario: opencode 的 Zen provider id
- **WHEN** `OpenCodeDriver.env_for` 收到 `provider="opencode-zen"`、`models.default="big-pickle"` 的 subscription
- **THEN** `OPENCODE_CONFIG_CONTENT` 的 provider 鍵是 `opencode`（不是 `opencode-zen`），`model` 與 `small_model` 都是 `opencode/big-pickle`

#### Scenario: opencode 的 provider 對應不到
- **WHEN** `provider` 是對應表裡沒有的名稱
- **THEN** `env_for` 丟錯，open 回 4xx，什麼都沒開（不用原名猜）

#### Scenario: opencode 沒有模型來源
- **WHEN** subscription 的 `models.default` 是 null，`models.allow` 只有 `["*:free"]`（只有萬用字元）
- **THEN** open 回 400 `no_model`，什麼都沒開

#### Scenario: opencode 用 allow 的第一個明確模型
- **WHEN** `models.default` 是 null，`models.allow` 是 `["*:free", "acme/spike-model"]`
- **THEN** `model` 與 `small_model` 都是 `openrouter/acme/spike-model`，`provider.openrouter.models` 宣告了 `acme/spike-model`

#### Scenario: opencode 的環境變數
- **WHEN** 任何 `OpenCodeDriver.env_for` 的輸出
- **THEN** 含 `OPENCODE_DISABLE_AUTOUPDATE=1` 與 `OPENCODE_DISABLE_MODELS_FETCH=1`；apiKey 與 baseURL 只在 `OPENCODE_CONFIG_CONTENT` 裡，不在命令列

#### Scenario: opencode 首次回應的模型不符
- **WHEN** 第一則回應的 `info.providerID`／`modelID` 不是選出的那一組
- **THEN** 視為錯誤：`exit.reason` 與 log 說明期望與實際（不含 key），不當作正常運作

#### Scenario: Claude 的 Anthropic 訂閱與 api-key
- **WHEN** `ClaudeCodeDriver.env_for` 收到 `kind=claude-subscription`；另一次收到 `provider="anthropic"`、`kind=api-key`
- **THEN** 前者輸出 `CLAUDE_CODE_OAUTH_TOKEN`，後者輸出 `ANTHROPIC_API_KEY`；兩者都含 `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` 與 `DISABLE_AUTOUPDATER=1`

#### Scenario: Claude 的非 Anthropic api-key
- **WHEN** `provider="openrouter"`、`kind=api-key`、`endpoint="https://…"`、`models.default="acme/model-x"`
- **THEN** 輸出 `ANTHROPIC_AUTH_TOKEN=<key>`、`ANTHROPIC_API_KEY=""`、`ANTHROPIC_BASE_URL`、`ANTHROPIC_MODEL` 與 `ANTHROPIC_SMALL_FAST_MODEL`（都是 `acme/model-x`）

#### Scenario: Claude 的非 Anthropic provider 沒有模型來源
- **WHEN** `provider="openrouter"`、`kind=api-key`，`models.default` 是 null 而 `allow` 只有萬用字元
- **THEN** open 回 400 `no_model`

#### Scenario: 父行程的憑證不進子行程
- **WHEN** 父行程的環境有 `ANTHROPIC_API_KEY`、`OPENAI_API_KEY`、`OPENCODE_CONFIG_CONTENT`，開一個 agent
- **THEN** 子行程的環境裡沒有這些（只有 `env_for` 給的），子行程的環境變數不出現在命令列、log 或 `status`

#### Scenario: 驅動錯誤帶 stderr 末幾行
- **WHEN** agent 行程啟動失敗或模型請求以沒有原因的 HTTP 500 失敗
- **THEN** 錯誤訊息帶子行程 stderr 的末幾行，其中 key 與 `env_for` 給的值被遮掉

### Requirement: close 占用轉移
`close` MUST 在同一個鎖內檢查並占用 agent 的 `busy`（轉移）再停下行程；已經 `busy` 時才記 `closing`。close 進行中，scheduler MUST NOT 對同一個 agent 開始換把或刷新。

#### Scenario: close 先到、之後換把
- **WHEN** 對 t1 `close`，而它的 `stop` 還沒有結束時，driver 回報額度用完、scheduler 刷新
- **THEN** 刷新不會對 t1 換把；`close` 結束後 `status` 是 exited，而且沒有任何仍活著的行程（所有 handle 都被停掉）

### Requirement: new 模式換把前 import 失敗
`new` 模式的 agent 額度用完而換把前的 `agora import` 失敗時，MUST 照樣停下 agent，標 `waiting-quota`、記住 `external_session_id`，由 scheduler 每 `retry_interval` 重試 import；import 成功後 MUST 才掛一般的自動重開 job。換把失敗後，scheduler MUST NOT 在退避時間之前再次進入換把（不會每個 tick 登錄 cooldown、list、import）。

#### Scenario: import 失敗後停下並重試
- **WHEN** `new` 模式的 n1 額度用完，`import_external` 一律失敗，連續 5 次 scheduler 刷新（都在 `retry_interval` 之內）
- **THEN** n1 被停下、狀態 waiting-quota；`cooldown` 只登錄一次、`list` 只一次、`import_external` 只一次；沒有自動重開的 job

#### Scenario: 重試 import 成功後掛 job
- **WHEN** 上面的 n1 在 `retry_interval` 之後 import 成功（回 agora id）
- **THEN** 掛一個一般的自動重開 job（continue 那個 agora id、算出的解除時刻）；`status.session` 變成那個 id；沒有其他的 import 或換把動作被重複執行

#### Scenario: 還沒跑過任何一輪
- **WHEN** `new` 模式的 n2 還沒有 `external_session_id` 就額度用完
- **THEN** 停下、waiting-quota、沒有 job

### Requirement: Claude 額度的輔助訊號
`ClaudeCodeDriver` 的輔助訊號（`system/api_retry` 的 `rate_limit`、`result` 文字）MUST 只在 `kind == api-key` 時使用；`api_retry` MUST 在重試用盡（`attempt >= max_retries`）或該回合以 `is_error: true` 的 `result` 結束時才算額度用完；`result` 文字 MUST 同時 `is_error: true` 才算。`kind == claude-subscription` MUST 只看 `rate_limit_event`。

#### Scenario: 暫時性 429 之後成功
- **WHEN** api-key 類的一個回合出現一次 `api_retry`（`attempt 1/10`），之後 `result` 是 `is_error: false` 的成功
- **THEN** driver 的狀態不是額度用完；沒有登錄 cooldown，agent 沒有被停下

#### Scenario: 重試用盡
- **WHEN** api-key 類的 `api_retry` 出現 `attempt 10/10`，之後 `result` 是 `is_error: true`
- **THEN** 視為額度用完（窗口 5h、不帶 `resets_at`）

#### Scenario: 文字巧合
- **WHEN** 一個回合的 `result` 是 `is_error: false`、文字是「You've hit your goal of five tests.」
- **THEN** 不算額度用完

#### Scenario: 訂閱類不看輔助訊號
- **WHEN** `kind == claude-subscription` 的回合出現 `api_retry` rate_limit 與以 `You've hit your` 開頭的 `is_error: true` 的 result，但沒有 `rejected` 的 `rate_limit_event`
- **THEN** 不算額度用完

### Requirement: opencode 的送出與狀態
`OpenCodeDriver` 的 `POST /session/{id}/message` MUST 在背景執行緒用長逾時（上限 1 小時）；狀態 MUST 以背景送出是否在跑為準（跑＝running、結束＝idle）；`/session/status` MUST 只用來偵測 `retry`。送出結束時回應的 `info.error` 或非 2xx MUST 寫進 `last_error`／`exit.reason`，不是只 log。

#### Scenario: 長回合不被切斷、結束後 idle
- **WHEN** 一個回合要 30 秒（超過一般請求的 10 秒逾時），`/session/status` 的 map 一直不含這個 session
- **THEN** 回合期間 `status` 是 running；回合結束後是 idle；請求沒有被逾時切斷

#### Scenario: 回應帶 info.error
- **WHEN** 回應是 HTTP 200 但 `info.error` 有內容（例如 provider 的授權錯誤），或回應是 HTTP 500
- **THEN** `status` 的 `exit.reason` 或 `GET /jobs` 的 `last_error` 帶那個錯誤（不含 key）；狀態不是永遠的 running

### Requirement: 錯誤訊息的遮罩與 stderr
driver 與 store 的錯誤訊息 MUST 帶子行程 stderr 的最新末幾行（包含啟動之後才印出的）；遮罩 MUST 只遮真正的秘密（key 與 `OPENCODE_CONFIG_CONTENT` 裡的 `apiKey`），MUST NOT 遮 `env_for` 的非秘密值。driver 的例外（agent 已結束時 `send` 失敗）MUST 轉成 `409 agent_not_running` 並把狀態標 exited，MUST NOT 變成 500。

#### Scenario: 數字不被遮掉
- **WHEN** 子行程 stderr 的末幾行是「agent exited with code 1 after 12 retries」，env 含 `DISABLE_AUTOUPDATER=1` 與 key `K-SECRET`
- **THEN** 錯誤訊息裡 `code 1`、`12` 原樣保留；`K-SECRET` 被遮掉

#### Scenario: 啟動之後的 stderr
- **WHEN** agora 啟動後才在 stderr 印出一行錯誤，之後 driver 的請求失敗
- **THEN** 錯誤訊息的 stderr 末幾行含這一行

#### Scenario: poke 已結束的 agent
- **WHEN** agent 行程剛結束、scheduler 還沒刷新時對它 `poke`
- **THEN** 回 409 `agent_not_running`，不是 500；狀態標 exited

### Requirement: 帶 merge 的 open 不能是 job
`POST /jobs` 的 `open` 帶 `session.mode == "merge"` MUST 回 400 `merge_not_allowed_in_job`。

#### Scenario: job 帶 merge
- **WHEN** `POST /jobs {"action": "open", "args": {…, "session": {"mode": "merge", "from": ["agora:A", "agora:B"]}}, "at": …}`
- **THEN** 回 400 `merge_not_allowed_in_job`，沒有掛任何 job；同樣的內容用 `POST /agents` 直接呼叫仍可行

