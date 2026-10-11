## Purpose
worker 上 AI agent 的監督程式：為它負責的身分收 Angareion 的訊息、依緊急度投遞給 agent，並啟動與終止 team；A2A 憑證由本人設定、只給 sidecar，模型憑證一律走 LLMGateway。

## ADDED Requirements

### Requirement: 收信與持久佇列
sidecar MUST 為它負責的每個身分輪詢 Angareion 的未 ack 訊息；收到的訊息 MUST 先寫進本機的持久佇列，之後才可能被投遞與 ack。sidecar 自己重啟（程式重跑、機器重開）MUST NOT 讓任何還沒投遞的訊息遺失；同一則訊息重新出現時 MUST 由訊息 `id` 去重，MUST NOT 重複投遞。

#### Scenario: 收到之後 sidecar 當掉
- **WHEN** 一則訊息進了佇列、還沒投遞，sidecar 就被強制結束
- **THEN** sidecar 再啟動後那一則還在佇列裡，之後照投遞政策送出

#### Scenario: 沒有新訊息
- **WHEN** Angareion 上沒有新訊息
- **THEN** sidecar 不寫任何留言、不新增任何訊息，agent 不受打擾

### Requirement: 依緊急度投遞
投遞 MUST 依訊息 `urgency`：

- `urgency` ≥ 8：MUST 立刻送進 agent。agent 正在跑一輪時，MUST 先中斷這一輪（不結束 agent），確認回到輸入提示再送；只送不中斷會變成排隊到下一輪，不算立刻；
- `urgency` < 8：MUST 進佇列，在下一次 hand-off 送出；v0 保證的 hand-off 是 agent 程序結束後、下一次啟動（`agora continue`）時先送；執行中能確認 agent 回到輸入提示時 MAY 提前送。

`urgency` 是 0～9 的整數；超出範圍的 MUST 回錯誤，MUST NOT 靜默丟掉。同一批要送的訊息 MUST 依 `urgency` 由高到低、同 `urgency` 依 `at` 由舊到新。投遞成功之後 sidecar MUST 以 ack 記在信道上（見 `angareion` 的〈ack 與 inbox 的游標〉）。

#### Scenario: 高緊急度打斷
- **WHEN** `urgency: 9` 的訊息在 agent 正在執行時到達
- **THEN** 它被送進 agent 的輸入，agent 下一輪就看得到；信道上很快出現 ack

#### Scenario: 低緊急度等下一次
- **WHEN** `urgency: 3` 的訊息到達而 agent 正在跑
- **THEN** agent 這一輪 MUST NOT 被中斷；下一次啟動 agent 時這則訊息先送進去

#### Scenario: 佇列順序
- **WHEN** 佇列裡同時有 `urgency: 5` 與 `urgency: 9` 各一則
- **THEN** 9 先送

### Requirement: 啟動與終止 team
啟動 agent MUST 用 `agora continue session <id> --agent … --sync-period <分鐘>`（單位和 agora 相同是分鐘，預設 5）；同一個 team 的任務之間 MUST 用 `--fork` 隔離（每個任務一個新 Session，團隊的 home Session 不被改動）；fork 出來的 Session MUST 帶 `sidecar.identity` 與 `sidecar.message` 標頭，記下是哪個身分、哪則訊息開的。送 harness 與信之前 MUST 確認 agent 已經準備好收輸入（不是只看到畫面有字）。某個身分收到信、而它沒有在跑的 agent 時，sidecar MUST 啟動 team；同一個身分同時最多一個 agent。命令裡來自外部的值（訊息 id、身分、路徑、標頭）MUST 經過 shell 引號處理。終止 MUST 對 agent 的整個 process group 依序送 SIGINT、等待、SIGTERM、等待、SIGKILL（等待時間有上限，不無限拖）。agent 結束後，這次的對話 MUST 被存進 agora：用 `agora continue` 啟動的由它的最後一次寫回負責；不是的話 MUST 用 `agora import` 匯出。

#### Scenario: 啟動一個任務
- **WHEN** sidecar 啟動一個 team 任務
- **THEN** agent 用 `agora continue` 跑起來，這個任務的對話寫進一個新的（`--fork` 出來的）agora Session，團隊的 home Session 不變

#### Scenario: 收到信而沒有在跑的 agent
- **WHEN** 某個身分收到一則信，而這個身分目前沒有 agent 在跑
- **THEN** sidecar 用 `--fork` 啟動 team，agent 準備好之後先收到 harness，再收到這則信

#### Scenario: 終止
- **WHEN** sidecar 要停掉一個還在跑的 agent
- **THEN** agent 先收到 SIGINT 有機會收尾；超過等待時間才升級到 SIGTERM、再 SIGKILL；結束後它的對話在 agora 上

### Requirement: 登錄與狀態
sidecar MUST 持久記錄它控制的每個 agent：身分／角色、agora Session、agent 種類、pid 或等價的辨識、啟動時間、狀態、log 的位置。sidecar 重啟後 MUST 能重新找到並繼續控制還在跑的 agent（送訊息、終止）。它 MUST NOT 把不是它啟動的 agent 列成自己控制的。

#### Scenario: sidecar 重啟而 agent 還在跑
- **WHEN** sidecar 重新啟動，而它之前啟動的一個 agent 還在執行
- **THEN** 狀態清單仍然列出那個 agent，還能對它送訊息與終止

#### Scenario: 外來的 agent
- **WHEN** worker 上有一個不是 sidecar 啟動的 agent
- **THEN** sidecar 的清單 MUST NOT 把它列成自己控制的

### Requirement: A2A 憑證只在 sidecar
A2A token MUST 由本人設定（能力發放，不經 AI、不進版控），MUST 只有 sidecar 讀取（檔案 MUST 只有擁有者有權限：0400 或 0600；worker 上是 `/run/mlp-secrets/angareion-token`，0400 唯讀掛載）。被 sidecar 啟動的 agent MUST NOT 在環境變數或檔案裡拿到 A2A token：agent 的環境只附加設定裡為該 team 列的變數與 LLMGateway 租約給的變數，MUST NOT 繼承 sidecar 自己的整個環境。agent 要回報 Angareion MUST 經 sidecar 的中繼；以這種方式送出的訊息 MUST 以該 team 的身分（`from`）出現在信道上。

#### Scenario: agent 的環境沒有 token
- **WHEN** 檢查 sidecar 啟動的 agent 行程的環境與它讀得到的設定檔
- **THEN** 找不到 A2A token

#### Scenario: 中繼回報
- **WHEN** team 經中繼送出一則回報
- **THEN** 信道上那則訊息的 `from` 是這個 team 的身分

### Requirement: 模型憑證走 LLMGateway
sidecar MUST NOT 持有、讀取或寫下任何模型供應商的金鑰。要啟動 agent 之前 MUST 先向 LLMGateway 申請租約（帶 instance、worker、角色、agent 種類、模型），只有拿到租約才啟動；agent 要用的模型存取 MUST 只來自租約給的環境變數（套用 `env`、去掉 `unset`），這些值 MUST NOT 出現在命令列、log、registry 或任何檔案。agent 準備好後 MUST 向 LLMGateway 確認（start）；租約要定期續約；額度用完時 MUST 回報（帶重置時間），拿到下一張租約就安全結束 agent、換環境重開、用 `agora continue` 接回同一個 Session。被拒絕時 MUST NOT 啟動，並把原因與最早的重置時間記下來（log 與 registry）。agent 結束後 MUST 釋放租約。（v0 用假 broker；介面照 LLMGateway 的定案。）

#### Scenario: LLMGateway 拒絕
- **WHEN** LLMGateway 拒絕這次開 agent 的申請
- **THEN** agent 不被啟動，原因記在 sidecar 的 log 與 registry

#### Scenario: sidecar 不碰模型金鑰
- **WHEN** 檢查 sidecar 的設定、狀態與檔案
- **THEN** 沒有模型供應商的金鑰；agent 的存取來自 LLMGateway 的准許

### Requirement: 以一個 shared-config 單元分發
sidecar MUST 以 MyLinuxPool 的一個 shared-config 單元（`shared-configs/agent-sidecar/`）分發；任何 profile 都可以帶它，MUST NOT 要求新增 profile。單元由 MyLinuxPool 維護（MyLinuxPool#52）：建映像時以 worker 身分安裝固定版本；容器沒有 systemd，由 entrypoint 拉起 supervisor，先起 herdr server（session 名 `worker`）再跑 `sidecar run`，崩潰時退避重啟。install.sh MUST 冪等、重跑結果一樣；`--check` MUST 比對版本並呼叫 `sidecar check --json`，MUST NOT 在檢查不過時假裝健康。

#### Scenario: 安裝
- **WHEN** 在帶了這個單元的 worker 上跑 install.sh，再跑 `install.sh --check`
- **THEN** sidecar 的程式與 supervisor 腳本就定位，`--check` 回 0

#### Scenario: 還沒有 token
- **WHEN** 本人還沒放 A2A token
- **THEN** `--check` 不通過（回 1 或 2，照 MyLinuxPool 的規則），服務不啟動、不假裝健康

### Requirement: Atelier harness 載入
啟動 team 時，sidecar MUST 依 Atelier（#32）的設計把該團隊的 harness 帶進 agent（載入點由 #32 定；v0 至少是 agent 的第一則輸入）。sidecar MUST NOT 改寫、摘要或解釋 harness 的內容。

#### Scenario: 啟動帶 harness
- **WHEN** sidecar 啟動一個 team
- **THEN** agent 的第一則輸入含該團隊 harness 的全文

### Requirement: 健康檢查與版本
`sidecar --version` MUST 印出安裝的版本（固定的 commit；拿不到時印套件版本並說明）。`sidecar check [--json]` MUST 驗證：設定可讀、A2A token 檔存在且只有擁有者有權限、herdr server 在跑且版本相容、token 能讀信道（一次讀取）。回傳碼 MUST 是三態：0 全部成立；1 至少一項確定不成立（例如 401／403、token 檔不存在或權限不對、設定壞掉、herdr 不相容）；2 沒有確定不成立、但至少一項無法確認（例如 5xx、連不上）。`--json` MUST 列出每一項的結果與原因，MUST NOT 含任何秘密。

#### Scenario: token 還沒放
- **WHEN** token 檔不存在時執行 `sidecar check --json`
- **THEN** 回傳碼 1，JSON 裡 token 那一項寫明找不到檔案

#### Scenario: 信道暫時連不上
- **WHEN** 其他都正常，但讀信道得到 5xx 或連不上
- **THEN** 回傳碼 2

### Requirement: 容器重建後以 agora 為準
sidecar 的本機狀態（registry、佇列、游標）MUST NOT 是找回工作的唯一依據：容器重建後本機狀態可能全部消失。`sidecar status` MUST 能列出 agora 上屬於自己負責的身分、還沒結束的 fork Session（依 `sidecar.identity` 標頭）。Angareion 的本機狀態 MUST 能從信道重建；已 ack 的訊息 MUST NOT 再投遞。v0 MUST NOT 自動接續做到一半的 Session，由人或下一則訊息決定。

#### Scenario: 容器重建
- **WHEN** worker 容器重建，sidecar 的本機狀態全部消失後重新啟動
- **THEN** 已 ack 的訊息不再投遞；`sidecar status` 列出 agora 上屬於自己身分、還沒結束的 fork Session

