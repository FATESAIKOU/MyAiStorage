## ADDED Requirements

### Requirement: 對外只有 send 與 inbox 兩個指令
skill MUST 對外只提供 `send` 與 `inbox` 兩個子指令（另有 `--version`／`--help`）。skill MUST NOT 提供 channel 建刪、開關 agent、單獨戳 agent、狀態查詢或 ack 等指令。

#### Scenario: 不認得的子指令
- **WHEN** 呼叫 `aicomm poke` 或 `aicomm status`
- **THEN** exit 1，stderr 說明只有 `send` 與 `inbox`

### Requirement: send 先寫進 Angareion，成功才算送達
`send` MUST 以 Angareion 的 `send` 指令寫入訊息，並把 Angareion 印出的訊息 id 原樣印在 stdout 的第一行。Angareion 寫入失敗時 `send` MUST 以該失敗的結束碼結束，且 MUST NOT 嘗試叫醒任何人。Angareion 寫入成功後，叫醒失敗 MUST NOT 改變結束碼（仍為 0）。

#### Scenario: 寫入成功、叫醒失敗
- **WHEN** Angareion 寫入成功，但本機 sidecar 連不上
- **THEN** exit 0，stdout 第一行是訊息 id，stderr 一行說明「已送達、未叫醒」及原因

#### Scenario: 寫入失敗
- **WHEN** Angareion 回 exit 1（例如 channel 不存在）
- **THEN** `send` 以 exit 1 結束，沒有任何對 sidecar 的請求

### Requirement: 工單號選填
`send` MUST 能不帶工單號送出一般訊息。給了 `--ticket <repo>#<n>` 時，skill MUST 以 `--ref ticket=<repo>#<n>` 交給 Angareion，MUST NOT 改動訊息內文，MUST NOT 去讀工單。

#### Scenario: 不帶工單號
- **WHEN** `aicomm send --channel c --to a --body 'hi'`
- **THEN** 訊息送出，沒有 `ticket` 附件

#### Scenario: 帶工單號
- **WHEN** 加上 `--ticket MyAiStorage#212`
- **THEN** 訊息帶一個名為 `ticket`、值為 `MyAiStorage#212` 的 ref 附件

### Requirement: 叫醒的對象與條件
`send` 寫入成功後，MUST 只對 `--to` 與每個 `--mention` 的 AI 名各判斷一次是否叫醒；沒有 `--to` 的廣播 MUST NOT 叫醒任何人；寄給自己 MUST NOT 叫醒自己。對每個名字，只有同時滿足下列三項才戳：(1) 本機 sidecar 的 agent 清單有這個 id；(2) 該 agent 是 tui（sidecar 回報的 `attach` 有值）；(3) 其 `state` 是 `idle` 或 `starting`。`running`、`waiting-quota`、`exited` MUST NOT 戳。

#### Scenario: 閒置的同台 tui
- **WHEN** 收件人在本機 sidecar、是 tui、`state` 為 `idle`
- **THEN** skill 對它戳一次，不帶 interrupt

#### Scenario: 忙碌中不戳
- **WHEN** 收件人的 `state` 為 `running`
- **THEN** 不戳；stderr 記一行「running，略過」

#### Scenario: 不在本機 sidecar
- **WHEN** 查詢本機 sidecar 回 404（不在這台）
- **THEN** 不戳；視為正常情形，stderr 記一行「不在本機」

#### Scenario: 廣播
- **WHEN** 沒給 `--to`
- **THEN** 不對 sidecar 發任何請求

#### Scenario: mention 也叫醒
- **WHEN** `--mention` 的對象符合上述三項條件
- **THEN** 同樣戳一次

### Requirement: 戳的內容固定且不含訊息
戳的文字 MUST 是 skill 內固定的短句（指示收件人執行 `inbox`），MUST NOT 含訊息內文、附件、寄件者以外的任何訊息欄位。skill MUST NOT 對同一次 `send` 的同一個對象戳兩次，MUST NOT 在戳失敗或逾時後重送。

#### Scenario: 戳逾時
- **WHEN** 戳的請求逾時
- **THEN** 不重送；stderr 一行「叫醒結果未知」；exit 0

### Requirement: sidecar 位址只能是 loopback
skill MUST 只接受指向本機 loopback 的 sidecar 位址（預設或環境設定）；設成其他主機 MUST 以 exit 1 拒絕，且 MUST NOT 送出任何請求。

#### Scenario: 非 loopback
- **WHEN** sidecar 位址設成非 loopback 的主機
- **THEN** exit 1，沒有訊息被送出、沒有請求被送出

### Requirement: 叫醒是盡力而為，可關閉
`send` MUST 提供 `--no-wake`，給了就完全不碰 sidecar。sidecar 不在、回應看不懂或任何非預期錯誤，MUST 只影響叫醒結果，MUST NOT 影響訊息已送達這件事。

#### Scenario: 關閉叫醒
- **WHEN** `--no-wake`
- **THEN** 不對 sidecar 發任何請求

### Requirement: inbox 固定讀寄給自己的信
`inbox` MUST 以 Angareion 的 `read`（不帶 `--channel`）讀信；MUST NOT 提供 `--channel` 選項。`--wait <秒>` MUST 先以 Angareion 的 `wait` 阻塞，逾時沒有新訊息 MUST 以 exit 3 結束且不讀。`--mentioned` MUST 轉成 Angareion 的 `--mentioned`。`--json` MUST 原樣轉出 Angareion 的輸出。

#### Scenario: 等信逾時
- **WHEN** `aicomm inbox --wait 5` 且 5 秒內沒有新訊息
- **THEN** exit 3，游標不動

#### Scenario: 有信
- **WHEN** 有寄給自己的新訊息
- **THEN** 印出訊息、游標前進，exit 0

### Requirement: 不改 Angareion 與 sidecar，不攜帶私人資訊
skill MUST NOT 修改 Angareion 的協定或程式，MUST NOT 要求 sidecar 新增介面；它對 Angareion 只用其公開的指令列介面，對 sidecar 只用其公開的 HTTP 介面（查 agent、戳）。skill 的說明檔與輸出 MUST NOT 含憑證、機器位址或私人資訊。

#### Scenario: 對 sidecar 的請求種類
- **WHEN** 檢查 skill 對 sidecar 發出的所有請求
- **THEN** 只有「查單一 agent」與「戳單一 agent」兩種
