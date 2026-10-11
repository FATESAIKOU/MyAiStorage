## ADDED Requirements

### Requirement: 四個動作
sidecar 對外 MUST 只有四個動作：開啟（open）、關閉（close）、搜集狀態（status）、戳（poke）。MUST NOT 有暫停或其他動作；「暫停」以 poke 加上中斷達成。

#### Scenario: 暫停就是戳加中斷
- **WHEN** 控制者對一個正在跑的 agent `poke`，`interrupt` 為真，內容是「停下來等指示」
- **THEN** agent 這一輪被打斷，接著收到那句話，停在提示處

### Requirement: 開啟
`open` MUST 收：呼叫方給的 `id`、agent 種類、工作目錄（存在的絕對路徑，不存在 MUST 回 400，sidecar MUST NOT 建目錄）、繼承的 Session（new／continue／fork／merge；merge 可帶選填的 `by`，預設是 agent 種類）；subscription 的 id 與第一則輸入 MUST 是選填（subscription 省略時，sidecar 依挑選規則自己挑一個現在能用的）。呼叫方指定的 subscription 在冷卻中時 MUST 照辦。`id` 對應的 agent 還在 starting／running／idle 時 MUST 拒絕；在 waiting-quota 或 exited 時 MUST 用同一個 id 重開，waiting-quota 的自動重開 job MUST 一併刪掉。sidecar MUST 自己向 subscription 來源取得詳細資料（呼叫方不傳 key），把它轉成 agent 的環境變數，放在子行程的環境裡；這些值 MUST NOT 出現在命令列、log 或任何檔案。開出來的 Session MUST 在 agora 標頭的 tags 附加 `sidecar:<id>`，原有的 tags MUST NOT 被取代。

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
- **WHEN** `open` 的 session 是 `merge`、`by` 是 opencode、agent 是 claude-code
- **THEN** 要約用 opencode 寫，環境變數來自 subscription；之後以 claude-code 接續那個新 Session

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
`status` MUST 只列這個 sidecar 開的 agent：id、agent 種類、agora Session（目前跑的那個）、subscription id、狀態（starting／running／idle／waiting-quota／exited）、開始時間；waiting-quota 而且有自動重開 job 時附預定重開的時刻（`reopen_at`），否則是 null。MUST NOT 含任何 key。

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
控制者 MUST 能用 HTTP 以三種形式下四個動作：立即（直接呼叫）、定時（job 的 `at`）、輪詢（job 的 `every`，可設 `until`）。job 的動作 MUST 只能是四個動作之一，否則 MUST 回 400；`at` 在過去 MUST 回 400。`GET /jobs` 的每一筆 MUST 有 `last_run` 與 `last_error`（不含 key）。HTTP MUST 只綁本機（127.0.0.1），不做認證。

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
- **WHEN** driver 給的 `resets_at` 被 LLMGateway 以 400 拒絕
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
