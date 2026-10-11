## ADDED Requirements

### Requirement: 四個動作
sidecar 對外 MUST 只有四個動作：開啟（open）、關閉（close）、搜集狀態（status）、戳（poke）。MUST NOT 有暫停或其他動作；「暫停」以 poke 加上中斷達成。

#### Scenario: 暫停就是戳加中斷
- **WHEN** 控制者對一個正在跑的 agent `poke`，`interrupt` 為真，內容是「停下來等指示」
- **THEN** agent 這一輪被打斷，接著收到那句話，停在提示處

### Requirement: 開啟
`open` MUST 收：呼叫方給的 `id`、agent 種類、工作目錄、繼承的 Session（new／continue／fork／merge）；subscription 的 id 與第一則輸入 MUST 是選填（subscription 省略時，sidecar 依挑選規則自己挑一個現在能用的）。`id` 對應的 agent 還在 starting／running／idle 時 MUST 拒絕；在 waiting-quota 或 exited 時 MUST 用同一個 id 重開。sidecar MUST 自己向 subscription 來源取得詳細資料（呼叫方不傳 key），把它轉成 agent 的環境變數，放在子行程的環境裡；這些值 MUST NOT 出現在命令列、log 或任何檔案。開出來的 Session MUST 在 agora 標頭的 tags 加上 `sidecar:<id>`。

#### Scenario: fork 一個 team 的 home Session
- **WHEN** `open`（id=t1、claude-code、fork 自 H、subscription=claude-personal、第一則輸入「讀 channel X」）
- **THEN** agora 上多一個從 H fork 出來、tags 含 `sidecar:t1` 的 Session；agent 在它上面開著，收到第一則輸入；H 不變

#### Scenario: id 重複
- **WHEN** t1 正在跑時再 `open` id=t1
- **THEN** 被拒絕（HTTP 409），什麼都沒開

#### Scenario: 重開已結束的 id
- **WHEN** t1 已經 exited，再 `open` id=t1（continue 它的 Session）
- **THEN** t1 重新開起來，接著同一個 Session

### Requirement: 關閉
`close` MUST 先溫和停下 agent，再把最後的對話寫回 agora；溫和停不下來時，逾時後強制結束，再寫回。

#### Scenario: 關閉
- **WHEN** 對 t1 `close`
- **THEN** agent 結束；它最後的對話在 agora 的那個 Session 上；`status` 顯示 exited

### Requirement: 搜集狀態
`status` MUST 只列這個 sidecar 開的 agent：id、agent 種類、agora Session、subscription id、狀態（starting／running／idle／waiting-quota／exited）、開始時間；waiting-quota 時附預定重開的時刻。MUST NOT 含任何 key。

#### Scenario: 不列別人的
- **WHEN** 同一台機器上有一個不是 sidecar 開的 Claude Code
- **THEN** `status` 不列它

### Requirement: 戳
`poke` MUST 把一則文字送進 agent，成為它讀得到的下一則輸入；`interrupt` 為真時 MUST 先打斷這一輪再送，為假時 MUST NOT 打斷（agent 正在跑時就排到這一輪之後）。

#### Scenario: 不打斷
- **WHEN** agent 正在跑一輪時 `poke`（不中斷）
- **THEN** 這一輪照常完成，之後 agent 讀到那則文字

### Requirement: 三種外部控制形式
控制者 MUST 能用 HTTP 以三種形式下四個動作：立即（直接呼叫）、定時（job 的 `at`）、輪詢（job 的 `every`，可設 `until`）。job 的動作 MUST 只能是四個動作之一。HTTP MUST 只綁本機（127.0.0.1），不做認證。

#### Scenario: 定時開啟
- **WHEN** 掛一個 job：`at` 明天 9:00、動作 `open`
- **THEN** 到時 sidecar 開那個 agent；`GET /jobs` 在那之前看得到這個 job

### Requirement: 額度用完自動換把
agent 回報額度用完時，sidecar MUST 向 subscription 來源登錄冷卻（窗只有 5h 與 1w 兩種，加上重置時刻），再從一覽挑一個現在能用、而且支援這個 agent 種類的 subscription（挑法是可替換的介面，預設照 priority）；挑到就停下 agent、寫回 agora、用 continue 同一個 Session 以新的 subscription 重開，id 不變。全部冷卻時 MUST 停下、寫回、狀態標 waiting-quota，並自動掛一個在最早解除冷卻的時刻 open 同一個 id（continue 同一個 Session、不指定 subscription）的 job。

#### Scenario: 換到另一把
- **WHEN** t1 用 claude-personal 時額度用完，claude-company 現在能用
- **THEN** claude-personal 被登錄冷卻；t1 以 claude-company 重開，接著同一個 Session

#### Scenario: 全部冷卻
- **WHEN** t1 額度用完，而所有支援 claude-code 的 subscription 都在冷卻
- **THEN** t1 停下、寫回，狀態 waiting-quota；`GET /jobs` 有一個在最早解除時刻 open t1（continue）的 job

### Requirement: 狀態揮發
agent 清單與 job 清單 MUST 只在記憶體。sidecar MUST NOT 在重啟後找回先前的 agent 或 job。

#### Scenario: 重啟
- **WHEN** sidecar 重新啟動
- **THEN** `status` 是空的、`GET /jobs` 是空的；先前的對話仍在 agora 上

### Requirement: 介面和實作分開
agent 種類（Claude Code、opencode）、Session 保存處（agora）、subscription 來源（LLMGateway、本機 JSON 檔）、挑選規則（priority）MUST 各自經由內部介面實作；換掉其中一個實作時，四個動作與 HTTP 介面 MUST NOT 改變。sidecar MUST NOT 依賴 Angareion，也 MUST NOT 依賴 herdr。

#### Scenario: 用假實作測試
- **WHEN** 測試以假的 agent driver、假的 agora、假的 subscription 來源啟動 sidecar
- **THEN** 四個動作與 job 的行為和真的實作相同，不開真的 agent、不連網
