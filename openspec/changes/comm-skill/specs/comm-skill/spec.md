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
`send` 寫入成功後，MUST 只對 `--to` 與每個 `--mention` 的 AI 名各判斷一次是否叫醒；沒有 `--to` 的廣播 MUST NOT 叫醒任何人；寄給自己 MUST NOT 叫醒自己；名字在「不叫醒名單」（aicomm 設定檔的 `nowake`，語意為收件人所在那台機器的設定）上 MUST NOT 叫醒，不論寄件者是誰。對其餘每個名字，只有同時滿足下列兩項才戳：(1) 本機 sidecar 的 agent 查詢回 200（agent 在這台）；(2) 其 `state` 是 `idle`。判斷 MUST NOT 依賴 agent 是 tui 或 headless，MUST NOT 讀 `attach` 欄位。`starting`、`running`、`waiting-quota`、`exited` MUST NOT 戳。

#### Scenario: 閒置的同台 agent
- **WHEN** 收件人在本機 sidecar、`state` 為 `idle`（不論 tui 或 headless）
- **THEN** skill 對它戳一次，不帶 interrupt

#### Scenario: 忙碌中不戳
- **WHEN** 收件人的 `state` 為 `running`
- **THEN** 不戳；stderr 記一行「running，略過」

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

#### Scenario: 不叫醒名單
- **WHEN** 收件人的 AI 名在設定檔的 `nowake` 裡，且它在本機 sidecar 是 `idle`
- **THEN** 不戳，也不查詢；訊息照常寫進 Angareion

### Requirement: 戳的內容固定且不含訊息
戳的文字 MUST 是 skill 內固定的短句（指示收件人執行 `inbox`），MUST NOT 含訊息內文、附件、寄件者、工單號或任何訊息欄位。skill MUST NOT 對同一次 `send` 的同一個對象戳兩次，MUST NOT 在戳失敗或逾時後重送。

#### Scenario: 戳逾時
- **WHEN** 戳的請求逾時
- **THEN** 不重送；stderr 一行「叫醒結果未知」；exit 0

#### Scenario: 戳的文字
- **WHEN** 對假 sidecar 檢查收到的戳
- **THEN** 文字等於固定短句，不含寄件者的 AI 名、訊息 id 與內文

### Requirement: 設定只來自 aicomm 自己的設定檔
skill MUST 只從 aicomm 自己的設定檔取得 sidecar 位址與不叫醒名單：`$XDG_CONFIG_HOME/aicomm/config.json`（未設則 `$HOME/.config/aicomm/config.json`），欄位 `sidecar`（單一位址，可省略）與 `nowake`（AI 名清單，可省略）。skill MUST NOT 以其他環境變數取得這兩項；唯一的環境變數是路徑覆寫 `AICOMM_CONFIG`（供測試與本機手動使用，文件 MUST 寫明 sidecar 開的 agent 拿不到它）。讀取時 MUST 在邊界驗證。檔案不存在 MUST 視為預設位址與空名單。

「需要叫醒」定義為：沒有 `--no-wake`，且（`--to` 加 `--mention`）排除自己與 `nowake` 名單後的候選收件人不是空的。「可能要叫醒」＝有 `--to` 或 `--mention` 且沒有 `--no-wake`。

#### Scenario: 設定檔不存在
- **WHEN** 設定檔不存在
- **THEN** 使用預設位址與空名單，`send` 正常運作

#### Scenario: 設定檔壞掉且可能要叫醒
- **WHEN** 設定檔不是合法 JSON（或 `nowake` 含不合法的 AI 名），且 `send` 帶 `--to`、沒有 `--no-wake`
- **THEN** exit 1，在寫進 Angareion 之前拒絕，沒有訊息被送出

#### Scenario: 設定檔壞掉但不需要讀它
- **WHEN** 設定檔壞掉，且 `send` 帶 `--no-wake`（或是廣播）
- **THEN** 訊息照常送出，exit 0

### Requirement: sidecar 位址只能是單一個 loopback 位址
設定檔的 `sidecar` MUST 是單一位址且 MUST 為本機 loopback；需要叫醒（定義見上）時不是 loopback MUST 以 exit 1 拒絕，且 MUST 在寫進 Angareion 之前拒絕。位址 MUST 只在需要叫醒時才驗與使用。這個檢查 MUST NOT 被宣稱為防止跨機叫醒（轉發埠本身是 loopback）。

#### Scenario: 非 loopback 且要叫醒
- **WHEN** 位址設成非 loopback 的主機，且 `send` 帶 `--to`、沒有 `--no-wake`
- **THEN** exit 1，沒有訊息被送出、沒有請求被送出

#### Scenario: 非 loopback 但不需要叫醒
- **WHEN** 位址設成非 loopback 的主機，且 `send` 帶 `--no-wake`（或是廣播）
- **THEN** 訊息照常送出，exit 0，沒有任何對 sidecar 的請求

#### Scenario: 收件人在名單上、位址非 loopback
- **WHEN** `--to` 是 `nowake` 名單上的人（排除後候選為空），且 `sidecar` 設成非 loopback
- **THEN** 訊息照常送出，exit 0，沒有任何對 sidecar 的請求

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
