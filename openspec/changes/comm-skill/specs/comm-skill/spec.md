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
`send` MUST 能不帶工單號送出一般訊息。給了 `--ticket <repo>#<n>`（格式 `^[A-Za-z0-9._-]+#[0-9]+$`，不符 exit 1）時，skill MUST 以 `--ref ticket=<repo>#<n>` 交給 Angareion，MUST NOT 改動訊息內文，MUST NOT 去讀工單。

#### Scenario: 不帶工單號
- **WHEN** `aicomm send --channel c --to a --body 'hi'`
- **THEN** 訊息送出，沒有 `ticket` 附件

#### Scenario: 帶工單號
- **WHEN** 加上 `--ticket MyAiStorage#212`
- **THEN** 訊息帶一個名為 `ticket`、值為 `MyAiStorage#212` 的 ref 附件

### Requirement: 叫醒的對象與條件
`send` 寫入成功後，MUST 只對 `--to` 與每個 `--mention` 的 AI 名各判斷一次是否叫醒；沒有 `--to` 的廣播 MUST NOT 叫醒任何人；寄給自己 MUST NOT 叫醒自己；`--no-wake` MUST NOT 叫醒任何人。skill MUST NOT 依角色（PM、PMO、隊員）分支；所有角色行為相同。預設戳為不中斷（`interrupt: false`）；給了 `--interrupt` 才帶 `interrupt: true`（`--interrupt` 與 `--no-wake` 互斥，同時給 exit 1）。對每個名字，本機 sidecar 的 agent 查詢回 200（agent 在這台）且 `state` 是 `idle` 或 `running` 才戳（不打斷：`idle` 馬上收到，`running` 排到這一輪之後）。判斷 MUST NOT 依賴 agent 是 tui 或 headless，MUST NOT 讀 `attach` 欄位。`starting`、`waiting-quota`、`exited` MUST NOT 戳。對 `running` 的收件人，預設的不中斷戳照送；給了 `--interrupt` 才帶 `interrupt: true`。skill MUST NOT 對同一次 `send` 的同一個對象戳兩次；不同次 `send` 之間 MUST NOT 去重（連送兩則＝兩次戳）。

#### Scenario: 閒置的同台 agent
- **WHEN** 收件人在本機 sidecar、`state` 為 `idle`（不論 tui 或 headless）
- **THEN** skill 對它戳一次，不帶 interrupt

#### Scenario: 忙碌中也戳（不打斷）
- **WHEN** 收件人的 `state` 為 `running`，沒給 `--interrupt`
- **THEN** 送出一次不中斷的戳（`interrupt` 為 false）

#### Scenario: 連送兩則
- **WHEN** 對 `running` 的同一收件人連續兩次 `send`
- **THEN** 對假 sidecar 共兩次戳，沒有去重

#### Scenario: 明確中斷
- **WHEN** 收件人的 `state` 為 `running`，且給了 `--interrupt`
- **THEN** 送出一次戳，`interrupt` 為 true

#### Scenario: 預設不中斷
- **WHEN** 收件人 `idle`，沒給 `--interrupt`
- **THEN** 戳的 `interrupt` 為 false

#### Scenario: interrupt 與 no-wake 互斥
- **WHEN** 同時給 `--interrupt` 與 `--no-wake`
- **THEN** exit 1，沒有訊息被送出

#### Scenario: starting 不戳
- **WHEN** 收件人的 `state` 為 `starting`
- **THEN** 不戳

#### Scenario: 不在本機 sidecar
- **WHEN** 查詢本機 sidecar 回 404（不在這台）
- **THEN** 不戳；視為正常情形，stderr 記一行「不在本機」

#### Scenario: 廣播
- **WHEN** 沒給 `--to`
- **THEN** 不對 sidecar 發任何請求

#### Scenario: mention 也叫醒
- **WHEN** `--mention` 的對象符合上述兩項條件
- **THEN** 同樣戳一次

### Requirement: 戳的內容固定且不含訊息
戳的文字 MUST 是 skill 內固定的短句（指示收件人執行 `inbox`），MUST NOT 含訊息內文、附件、寄件者、工單號或任何訊息欄位。skill MUST NOT 對同一次 `send` 的同一個對象戳兩次，MUST NOT 在戳失敗或逾時後重送。

#### Scenario: 戳逾時
- **WHEN** 戳的請求逾時
- **THEN** 不重送；stderr 一行「叫醒結果未知」；exit 0

#### Scenario: 戳的文字
- **WHEN** 對假 sidecar 檢查收到的戳
- **THEN** 文字等於固定短句，不含寄件者的 AI 名、訊息 id 與內文

### Requirement: sidecar 位址固定為預設 loopback 位址
skill MUST 使用 sidecar 文件的預設位址（loopback）；MUST NOT 讀任何設定檔，MUST NOT 提供使用者可設定的位址。測試用覆寫（環境變數 `AICOMM_SIDECAR`，作者提議 P5，可被拒絕）若存在，MUST 只收單一位址、MUST 為 loopback；需要叫醒時覆寫值不是 loopback MUST 以 exit 1 拒絕，且 MUST 在寫進 Angareion 之前拒絕。「需要叫醒」＝沒有 `--no-wake`，且（`--to` 加 `--mention`）排除自己後的候選不是空的。不需要叫醒時 MUST NOT 對 sidecar 發任何請求。這個檢查 MUST NOT 被宣稱為防止跨機叫醒（轉發埠本身是 loopback）。

#### Scenario: 預設位址
- **WHEN** 沒有覆寫
- **THEN** 對預設的 loopback 位址查詢與戳，不讀任何設定檔

#### Scenario: 覆寫為非 loopback 且要叫醒
- **WHEN** 覆寫值是非 loopback 主機，且 `send` 帶 `--to`、沒有 `--no-wake`
- **THEN** exit 1，沒有訊息被送出、沒有請求被送出

#### Scenario: 覆寫為非 loopback 但不需要叫醒
- **WHEN** 覆寫值是非 loopback 主機，且 `send` 帶 `--no-wake`（或是廣播）
- **THEN** 訊息照常送出，exit 0，沒有任何對 sidecar 的請求

### Requirement: 沒有硬控制，叫醒的節制由 SKILL.md 軟規則承擔
skill MUST NOT 實作名單、角色或權限類的叫醒限制。`skills/aicomm/SKILL.md` MUST 寫明三條軟規則：(1) `send` 預設不中斷，要中斷才加 `--interrupt`；(2) 隊員基本上不要中斷上位職（PM、PMO）；(3) 「收到」這類沒有要對方動作的回覆加 `--no-wake`，不要叫醒對方。SKILL.md MUST 寫明：之後要硬控制，走 sidecar open 帶白名單（MyLinuxPool#346），不在 aicomm 做；並寫明代價：任何同台 agent 都能叫醒 PMO，與 M2 D6 的 watch 規則並存。

#### Scenario: 不依角色分支
- **WHEN** 以任何 AI 名呼叫 `send --to <任一收件人>`
- **THEN** 行為只取決於旗標與收件人的 sidecar `state`，不取決於寄件者或收件人的角色

#### Scenario: SKILL.md 含三條軟規則
- **WHEN** 檢查 `skills/aicomm/SKILL.md`
- **THEN** 三條軟規則與「走 sidecar open 白名單」皆有明文

### Requirement: 叫醒是盡力而為，可關閉
`send` MUST 提供 `--no-wake`，給了就完全不碰 sidecar。sidecar 不在、回應看不懂或任何非預期錯誤，MUST 只影響叫醒結果，MUST NOT 影響訊息已送達這件事。

#### Scenario: 關閉叫醒
- **WHEN** `--no-wake`
- **THEN** 不對 sidecar 發任何請求

### Requirement: inbox 固定讀寄給自己的信
`inbox` MUST 以 Angareion 的 `read`（不帶 `--channel`）讀信；MUST NOT 提供 `--channel` 選項。`--wait <秒>` MUST 先以 Angareion 的 `wait` 阻塞：`wait` exit 0 才 `read`；exit 3 MUST 以 3 結束且不讀；exit 2 MUST 以 2 原樣結束且不讀（不是「沒有新訊息」）。`--mentioned` MUST 同時轉給 `wait` 與 `read`。`--json` MUST 原樣轉出 Angareion 的輸出。

#### Scenario: 等信逾時
- **WHEN** `aicomm inbox --wait 5` 且 5 秒內沒有新訊息
- **THEN** exit 3，游標不動

#### Scenario: 等信遇到後端錯誤
- **WHEN** Angareion 的 `wait` 回 exit 2
- **THEN** `inbox` 以 exit 2 結束，沒有呼叫 `read`

#### Scenario: 有信
- **WHEN** 有寄給自己的新訊息
- **THEN** 印出訊息、游標前進，exit 0

#### Scenario: mentioned 同時給 wait 與 read
- **WHEN** `aicomm inbox --wait 5 --mentioned`
- **THEN** 對 Angareion 的 `wait` 與 `read` 兩次呼叫都帶 `--mentioned`

### Requirement: 不改 Angareion 與 sidecar，不攜帶私人資訊
skill MUST NOT 修改 Angareion 的協定或程式，MUST NOT 要求 sidecar 新增介面；它對 Angareion 只用其公開的指令列介面，對 sidecar 只用其公開的 HTTP 介面（查 agent、戳）。skill 的說明檔與輸出 MUST NOT 含憑證、機器位址或私人資訊。

#### Scenario: 對 sidecar 的請求種類
- **WHEN** 檢查 skill 對 sidecar 發出的所有請求
- **THEN** 只有「查單一 agent」與「戳單一 agent」兩種
