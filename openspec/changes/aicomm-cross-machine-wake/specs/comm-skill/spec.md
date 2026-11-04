## MODIFIED Requirements

### Requirement: send 先寫進 Angareion，成功才算送達
`send` MUST 以 Angareion 的 `send` 指令寫入訊息，並把 Angareion 印出的訊息 id 原樣印在 stdout 的第一行。Angareion 寫入失敗時 `send` MUST 以該失敗的結束碼結束，且 MUST NOT 嘗試叫醒任何人、MUST NOT 呼叫 `pool-peer`。Angareion 寫入成功後，叫醒失敗 MUST NOT 改變結束碼（仍為 0）；帶 `--require-wake` 時最多回 4，也仍代表訊息已送達（見「`send` 的結束碼與 `--require-wake`」）。叫醒的結果 MUST 以固定格式的 `wake:` 行呈現在 stderr（見「`wake:` 行的文法」）。

#### Scenario: 寫入成功、叫醒失敗
- **WHEN** Angareion 寫入成功，但本機 sidecar 連不上、也沒有可用的 `pool-peer`
- **THEN** exit 0，stdout 第一行是訊息 id，stderr 是 `wake: <agent> not-woken peer-unavailable`（本機與跨機都試過）

#### Scenario: 寫入失敗
- **WHEN** Angareion 回 exit 1（例如 channel 不存在）
- **THEN** `send` 以 exit 1 結束，沒有任何對 sidecar 的請求，也沒有呼叫 `pool-peer`

### Requirement: 叫醒的對象與條件
`send` 寫入成功後，MUST 只對 `--to` 與每個 `--mention` 的 AI 名各判斷一次是否叫醒；沒有 `--to` 的廣播 MUST NOT 叫醒任何人；寄給自己 MUST NOT 叫醒自己；`--no-wake` MUST NOT 叫醒任何人。skill MUST NOT 依角色（PM、PMO、隊員）分支；所有角色行為相同。預設戳為不中斷（`interrupt: false`）；給了 `--interrupt` 且收件人 `running` 才帶 `interrupt: true`，收件人 `idle` 時 `interrupt` 仍為 false（`--interrupt` 與 `--no-wake` 互斥，同時給 exit 1）。

對每個名字，MUST **先問本機 sidecar**（`GET /agents/<id>`）：回 200（agent 在這台）且 `state` 是 `idle` 或 `running` 才戳；回 200 但 `state` 是其他值（`starting`、`waiting-quota`、`exited` 等）MUST NOT 戳，也 MUST NOT 再查別台；回 200 但回應看不懂（body 不是 JSON 物件、`state` 不是字串），或 HTTP 層讀不懂，MUST 是 `not-woken bad-answer`，MUST NOT 戳，也 MUST NOT 再查別台；回其他 HTTP 碼（非 200、非 404）MUST NOT 戳，也 MUST NOT 再查別台。只有本機回 404、本機 sidecar 連不上（`OSError`／`URLError`）或 GET 逾時，才 MAY 依「跨機叫醒只經 `pool-peer` 的兩個用法」改查別台——這時還沒戳任何人，不會重複。本機戳之後，不論結果（含逾時與連線錯誤）MUST NOT 改查別台（可能已經戳到）。

判斷 MUST NOT 依賴 agent 是 tui 或 headless，MUST NOT 讀 `attach` 欄位。`starting`、`waiting-quota`、`exited` MUST NOT 戳。對 `running` 的收件人，預設的不中斷戳照送。不同次 `send` 之間 MUST NOT 去重（連送兩則＝兩次戳）。名字以 `-` 開頭時 MUST NOT 交給 `pool-peer`：直接是 `not-woken bad-name`。

#### Scenario: 閒置的同台 agent
- **WHEN** 收件人在本機 sidecar、`state` 為 `idle`（不論 tui 或 headless）
- **THEN** skill 對它戳一次，不帶 interrupt，stderr 是 `wake: <agent> woken local`，沒有呼叫 `pool-peer`

#### Scenario: 忙碌中也戳（不打斷）
- **WHEN** 收件人的 `state` 為 `running`，沒給 `--interrupt`
- **THEN** 送出一次不中斷的戳（`interrupt` 為 false）

#### Scenario: 連送兩則
- **WHEN** 對 `running` 的同一收件人連續兩次 `send`
- **THEN** 對假 sidecar 共兩次戳，沒有去重

#### Scenario: 明確中斷
- **WHEN** 收件人的 `state` 為 `running`，且給了 `--interrupt`
- **THEN** 送出一次戳，`interrupt` 為 true，stderr 是 `wake: <agent> woken local interrupt`

#### Scenario: idle 時給 --interrupt
- **WHEN** 收件人 `idle`，且給了 `--interrupt`
- **THEN** 戳的 `interrupt` 仍為 false

#### Scenario: 預設不中斷
- **WHEN** 收件人 `idle`，沒給 `--interrupt`
- **THEN** 戳的 `interrupt` 為 false

#### Scenario: interrupt 與 no-wake 互斥
- **WHEN** 同時給 `--interrupt` 與 `--no-wake`
- **THEN** exit 1，沒有訊息被送出

#### Scenario: starting 不戳
- **WHEN** 收件人的 `state` 為 `starting`
- **THEN** 不戳，stderr 是 `wake: <agent> not-woken state-starting`，沒有呼叫 `pool-peer`

#### Scenario: 本機回 200 的其他 state 不查別台
- **WHEN** 本機 sidecar 回 200，`state` 是 `exited`
- **THEN** 不戳，stderr 是 `wake: <agent> not-woken state-exited`，沒有呼叫 `pool-peer`

#### Scenario: 本機回其他 HTTP 碼不查別台
- **WHEN** 本機 sidecar 的查詢回 500
- **THEN** 不戳，stderr 是 `wake: <agent> not-woken http-500`，沒有呼叫 `pool-peer`

#### Scenario: 本機回應看不懂
- **WHEN** 本機 sidecar 回 200，但 body 不是 JSON 物件（或 `state` 不是字串）
- **THEN** 不戳，stderr 是 `wake: <agent> not-woken bad-answer`，沒有呼叫 `pool-peer`

#### Scenario: 本機查詢的 HTTP 層讀不懂
- **WHEN** 本機 sidecar 的 `GET /agents/<id>` 丟出 `http.client.HTTPException`（例如回應的狀態行壞掉），還沒戳任何人
- **THEN** 不戳，stderr 是 `wake: <agent> not-woken bad-answer`，沒有呼叫 `pool-peer`

#### Scenario: 不在本機 sidecar
- **WHEN** 查詢本機 sidecar 回 404（不在這台），且 `pool-peer find` 回 1（每一台都確定沒有）
- **THEN** 不戳任何人；stderr 是 `wake: <agent> not-woken not-found`

#### Scenario: 本機連不上時改查別台
- **WHEN** 本機 sidecar 連不上，`pool-peer find` 回一台 `idle`（port 5201），`call` 回第一行 200
- **THEN** 有呼叫 `find <agent> --json --timeout 10` 與 `call 5201 POST /agents/<quoted-id>/poke --timeout 5` 各一次；stderr 是 `wake: <agent> woken peer:5201`

#### Scenario: 本機戳逾時不改走 find
- **WHEN** 本機 `POST /agents/<id>/poke` 逾時
- **THEN** stderr 是 `wake: <agent> unknown poke-timeout`；沒有呼叫 `pool-peer`，也沒有重送

#### Scenario: 名字以 - 開頭
- **WHEN** 收件人的名字以 `-` 開頭，且本機回 404
- **THEN** stderr 是 `wake: <agent> not-woken bad-name`；沒有呼叫 `pool-peer`

#### Scenario: 廣播
- **WHEN** 沒給 `--to`
- **THEN** 不對 sidecar 發任何請求，也不呼叫 `pool-peer`

#### Scenario: mention 也叫醒
- **WHEN** `--mention` 的對象符合上述兩項條件
- **THEN** 同樣戳一次

### Requirement: 戳的內容固定且不含訊息
戳的文字 MUST 是 skill 內固定的短句（指示收件人執行 `inbox`），MUST NOT 含訊息內文、附件、寄件者、工單號或任何訊息欄位。skill MUST NOT 對同一次 `send` 的同一個對象的同一台 sidecar 戳兩次，MUST NOT 在戳失敗或逾時後重送。跨機的戳 MUST 用同一個短句、同一個 body（只有 `text` 與 `interrupt` 兩個欄位），經 `pool-peer call <port> POST /agents/<id>/poke` 送出。

#### Scenario: 戳逾時
- **WHEN** 本機的戳逾時
- **THEN** 不重送；stderr 一行 `wake: <agent> unknown poke-timeout`；沒帶 `--require-wake` 時 exit 0（帶了回 4）

#### Scenario: 戳的文字
- **WHEN** 對假 sidecar 檢查收到的戳
- **THEN** 文字等於固定短句，不含寄件者的 AI 名、訊息 id 與內文

#### Scenario: 跨機的戳與本機相同
- **WHEN** 跨機戳一台 `idle` 的 agent（沒給 `--interrupt`）
- **THEN** `pool-peer call` 的 stdin 恰是 `{"text": <固定短句>, "interrupt": false}`，不含寄件者、訊息 id 與內文

### Requirement: sidecar 位址固定為預設 loopback 位址
skill MUST 使用 sidecar 文件的預設位址（loopback）；MUST NOT 讀任何設定檔，MUST NOT 提供使用者可設定的位址。測試用覆寫（環境變數 `AICOMM_SIDECAR`，作者提議 P5，可被拒絕）若存在，MUST 只收單一位址、MUST 為 loopback；需要叫醒時覆寫值不是 loopback MUST 以 exit 1 拒絕，且 MUST 在寫進 Angareion 之前拒絕。「需要叫醒」＝沒有 `--no-wake`，且（`--to` 加 `--mention`）排除自己後的候選不是空的。不需要叫醒時 MUST NOT 對 sidecar 發任何請求。這個檢查 MUST NOT 被宣稱為防止跨機叫醒（轉發埠本身是 loopback）。

跨機叫醒用的執行檔另由環境變數 `AICOMM_POOL_PEER` 指定（只給測試與手動用；sidecar 開的 agent 的環境是白名單，拿不到這個變數）；沒設就從 PATH 找 `pool-peer`。這個變數只決定呼叫哪一個執行檔與 argv，MUST NOT 被當成 sidecar 位址，也 MUST NOT 被當成使用者可設定的叫醒位址（跨機能打哪些 port 由 `pool-peer` 與埠段決定）。

#### Scenario: 預設位址
- **WHEN** 沒有覆寫
- **THEN** 對預設的 loopback 位址查詢與戳，不讀任何設定檔；跨機的執行檔從 PATH 找 `pool-peer`

#### Scenario: 覆寫為非 loopback 且要叫醒
- **WHEN** 覆寫值是非 loopback 主機，且 `send` 帶 `--to`、沒有 `--no-wake`
- **THEN** exit 1，沒有訊息被送出、沒有請求被送出

#### Scenario: 覆寫為非 loopback 但不需要叫醒
- **WHEN** 覆寫值是非 loopback 主機，且 `send` 帶 `--no-wake`（或是廣播）
- **THEN** 訊息照常送出，exit 0，沒有任何對 sidecar 的請求

#### Scenario: 跨機執行檔的測試掛鉤
- **WHEN** `AICOMM_POOL_PEER` 指到一個假的 `pool-peer`，且本機回 404
- **THEN** 用那個執行檔（argv，不經 shell）呼叫 `find`；它 MUST NOT 被當成 sidecar 位址，`GET /agents/<id>` 仍然只打預設的 loopback 位址

### Requirement: 沒有硬控制，叫醒的節制由 SKILL.md 軟規則承擔
skill MUST NOT 實作名單、角色或權限類的叫醒限制。`skills/aicomm/SKILL.md` MUST 寫明三條軟規則：(1) `send` 預設不中斷，要中斷才加 `--interrupt`；(2) 隊員基本上不要中斷上位職（PM、PMO）；(3) 「收到」這類沒有要對方動作的回覆加 `--no-wake`，不要叫醒對方。SKILL.md MUST 寫明：之後要硬控制，不在 aicomm 做——本機走 sidecar open 帶白名單（MyLinuxPool#346），跨機（經 `pool-peer`）的存取控制併在 MyPMO#297 再議；並寫明代價：pool 內任何 agent（含別台機器上的）都能叫醒 PMO，與 M2 D6 的 watch 規則並存。SKILL.md MUST NOT 宣稱 aicomm 或 sidecar 白名單擋得住跨機的叫醒。

#### Scenario: 不依角色分支
- **WHEN** 以任何 AI 名呼叫 `send --to <任一收件人>`
- **THEN** 行為只取決於旗標、收件人在本機 sidecar 或 `pool-peer find` 回報的 `state`，不取決於寄件者或收件人的角色

#### Scenario: SKILL.md 含三條軟規則
- **WHEN** 檢查 `skills/aicomm/SKILL.md`
- **THEN** 三條軟規則、「本機走 sidecar open 白名單（MyLinuxPool#346）」與「跨機的存取控制在 MyPMO#297」皆有明文

#### Scenario: SKILL.md 的代價說的是整個 pool
- **WHEN** 檢查 `skills/aicomm/SKILL.md` 的代價段
- **THEN** 寫的是「pool 內任何 agent（含別台機器上的）都能叫醒 PMO」，沒有「同台」的說法

### Requirement: 叫醒是盡力而為，可關閉
`send` MUST 提供 `--no-wake`，給了就完全不碰 sidecar，也 MUST NOT 呼叫 `pool-peer`。sidecar 不在、回應看不懂或任何非預期錯誤，MUST 只影響叫醒結果，MUST NOT 影響訊息已送達這件事（`--require-wake` 時最多是 exit 4，仍代表已送達）。預期外的例外 MUST NOT 讓 `send` 失敗：叫醒的兜底是 `wake: <agent> unknown internal-error`。

#### Scenario: 關閉叫醒
- **WHEN** `--no-wake`
- **THEN** 不對 sidecar 發任何請求，也不呼叫 `pool-peer`

#### Scenario: 預期外的例外
- **WHEN** 叫醒的過程丟出一個沒有對應原因的例外
- **THEN** stderr 是 `wake: <agent> unknown internal-error`；訊息已送達，exit 0（沒帶 `--require-wake`）

## ADDED Requirements

### Requirement: 跨機叫醒只經 `pool-peer` 的兩個用法
本機查不到人時，skill MUST 只用 MyLinuxPool 的 `pool-peer` 找別台並戳；MUST NOT 對 pool 做其他請求（MUST NOT 用 `pool-peer list`），MUST NOT 自己連 Gateway 或任何非 loopback 位址。呼叫一律用 argv、不經 shell。找 MUST 是 `find <agent-id> --json --timeout 10`；戳 MUST 是 `call <sidecar_port> POST /agents/<agent-id>/poke --timeout 5`，body 走 stdin（帶 `Content-Type: application/json`），`<agent-id>` MUST 先做 URL quote。aicomm MUST 只打兩個路由：`GET /agents/<id>`（經 `find`）與 `POST /agents/<id>/poke`。

#### Scenario: 只打兩個路由
- **WHEN** 本機回 404，跨機找到並戳了一台
- **THEN** 對 `pool-peer` 只有 `find` 與 `call … POST /agents/<id>/poke` 兩種 argv，沒有 `list`；poke 的 body 只有 `text` 與 `interrupt`，不含寄件者、訊息 id 或內文

#### Scenario: 找到一台並戳成功
- **WHEN** 本機回 404，`find` 回一台 `idle`（port 5201），`call` 回第一行 200
- **THEN** stderr 是 `wake: <agent> woken peer:5201`；`call` 的 argv 是 `call 5201 POST /agents/<quoted-id>/poke --timeout 5`，stdin 是 `{"text": <固定短句>, "interrupt": false}`；exit 0

### Requirement: 找 `pool-peer` 執行檔
設了環境變數 `AICOMM_POOL_PEER` 時 MUST 用它，沒設 MUST 從 PATH 找 `pool-peer`。找不到或不能執行 MUST 是 `not-woken peer-unavailable`，不算錯（不影響結束碼，`--require-wake` 除外）。

#### Scenario: 沒有 `pool-peer`
- **WHEN** 本機回 404，`AICOMM_POOL_PEER` 指向不存在的檔，PATH 上也沒有 `pool-peer`
- **THEN** stderr 是 `wake: <agent> not-woken peer-unavailable`；exit 0

### Requirement: 跨機叫醒的時間預算
收件人 MUST 一個一個處理。`find` 的 10 秒與 `call` 的 5 秒 MUST 用 `--timeout` 傳給 `pool-peer`；aicomm 對子行程 MUST 另外多等 2 秒當上限，超過就自己收掉子行程，MUST NOT 讓 `send` 卡住或失敗。

#### Scenario: 子行程卡住
- **WHEN** `find` 或 `call` 卡住超過 `--timeout` 加 2 秒
- **THEN** aicomm 自己收掉子行程；結果分別是 `wake: <agent> not-woken lookup-incomplete` 或 `wake: <agent> unknown poke-timeout`；`send` 仍然 exit 0（沒帶 `--require-wake`）

### Requirement: 讀 `pool-peer find` 的結束碼
rc 0 MUST 照「解析 `pool-peer find` 的陣列」處理。rc 1 MUST 是 `not-woken not-found`。rc 2 或執行失敗（`OSError`）MUST 是 `not-woken peer-unavailable`。rc 3，或 aicomm 自己的子行程逾時，MUST 是 `not-woken lookup-incomplete`——不知道對方在不在，但 aicomm 確定沒有戳任何人，所以是 `not-woken`，不是 `unknown`。其他 rc MUST 是 `not-woken peer-bad-answer`。

#### Scenario: `find` 回 1
- **WHEN** 本機回 404，`find` 的結束碼是 1（每一台都確定沒有）
- **THEN** stderr 是 `wake: <agent> not-woken not-found`

#### Scenario: `find` 回 2
- **WHEN** 本機回 404，`find` 的結束碼是 2（設定或 Gateway 錯）
- **THEN** stderr 是 `wake: <agent> not-woken peer-unavailable`

#### Scenario: 執行 `find` 失敗
- **WHEN** 本機回 404，執行 `pool-peer find` 時丟出 `OSError`（子行程沒起來）
- **THEN** stderr 是 `wake: <agent> not-woken peer-unavailable`

#### Scenario: `find` 回 3
- **WHEN** 本機回 404，`find` 的結束碼是 3（找到 0 台，但有的台查不到）
- **THEN** stderr 是 `wake: <agent> not-woken lookup-incomplete`；沒有呼叫 `call`

### Requirement: 解析 `pool-peer find` 的陣列
rc 0 時 stdout MUST 被當成 JSON 陣列解析；不是 JSON 陣列 MUST 是 `not-woken peer-bad-answer`。MUST 丟掉格式不對的項目（缺 `port`、`port` 不是整數或是 bool、`agent.state` 不是字串等），同一個 `port` 只留一筆（保留第一次出現的那筆）；一筆合格的都沒有 MUST 是 `not-woken peer-bad-answer`。rc 0 時其他台可能還沒查到，aicomm MUST NOT 另外標示：只回報找到的那幾台的結果。

#### Scenario: `find` 的回答看不懂
- **WHEN** `find` 回 0，但 stdout 不是 JSON 陣列，或陣列裡沒有任何合格項目
- **THEN** stderr 是 `wake: <agent> not-woken peer-bad-answer`；沒有呼叫 `call`

### Requirement: `find` 找到的每一台怎麼戳
對每一筆合格的項目：`agent.state` 不是 `idle`／`running` 時 MUST 是 `not-woken state-<state>`；`port` 不在 5200–5399（含）時 MUST 是 `not-woken peer-port-out-of-range`，且 MUST NOT 呼叫 `call`（在 aicomm 這邊先擋，才不會和「連不到」都變成 rc 2 而分不出來）；其他就戳，好幾台都可以戳時 MUST 全部戳。每一台的 `interrupt` MUST 各自判斷：只有那一台是 `running` 且呼叫端帶 `--interrupt` 才是 true。

#### Scenario: 埠超出範圍
- **WHEN** `find` 回一個 port 5100 或 5400
- **THEN** 不呼叫 `call`；stderr 是 `wake: <agent> not-woken peer-port-out-of-range`

#### Scenario: 多台都可戳
- **WHEN** `find` 回兩台都是 `idle`，`call` 一台回 200、另一台回 500
- **THEN** 兩台都戳；stderr 是 `wake: <agent> woken peer:<成功的那個 port>`（只列成功的 port）

#### Scenario: 多台時只要有一台被打斷就加 interrupt
- **WHEN** 本機回 404，`find` 回兩台：5201 是 `running`、5202 是 `idle`，給了 `--interrupt`，兩台的 `call` 都回 200
- **THEN** 5201 的 stdin 是 `{"text": <固定短句>, "interrupt": true}`，5202 的是 `{"text": <固定短句>, "interrupt": false}`；stderr 是 `wake: <agent> woken peer:5201,5202 interrupt`

#### Scenario: 被打斷的那台沒成功就不加 interrupt
- **WHEN** 同上，但 5201 的 `call` 回 500、5202 回 200
- **THEN** stderr 是 `wake: <agent> woken peer:5202`（沒有 ` interrupt`）

### Requirement: 戳的回應怎麼判讀（本機與跨機共用）
本機 sidecar 對戳的 HTTP 回應，與 `pool-peer call` rc 0 時 stdout 第一行的狀態碼及其後的 body，MUST 用同一張表判讀：200 MUST 是這台叫醒。409 時取 body 的錯誤碼（JSON 物件的 `code`，沒有就 `error`，要是字串）：`agent_not_running` MUST 是 `not-woken agent-not-running`，`agent_awaiting_answer` MUST 是 `not-woken awaiting-answer`，其他、沒有或 body 看不懂 MUST 是 `not-woken http-409`。其他任何碼（含 200 以外的 2xx）MUST 是 `not-woken http-<碼>`。本機的 409 MUST 讀出錯誤回應的 body 再判斷。MUST NOT 重送。

#### Scenario: 本機戳回 409 與其他碼
- **WHEN** 本機 `GET` 回 200 `idle`，戳回 409 且 body 是 `{"code": "agent_awaiting_answer"}`／`{"error": "agent_not_running"}`，或戳回 503
- **THEN** 分別是 `wake: <agent> not-woken awaiting-answer`、`not-woken agent-not-running`、`not-woken http-503`；只戳一次，沒有呼叫 `pool-peer`

#### Scenario: `call` 回 409 與其他碼
- **WHEN** `call` 回 409 且 body 的 `error` 是 `agent_awaiting_answer`／`agent_not_running`，或回 500
- **THEN** 分別是 `wake: <agent> not-woken awaiting-answer`、`not-woken agent-not-running`、`not-woken http-500`

#### Scenario: 409 的 body 看不懂或沒有錯誤碼
- **WHEN** 戳回 409，但 body 不是 JSON 物件，或 `code`／`error` 都不是字串
- **THEN** stderr 是 `wake: <agent> not-woken http-409`；只戳一次，沒有重送

#### Scenario: 200 以外的 2xx
- **WHEN** 戳回 201（或任何不是 200 的 2xx）
- **THEN** stderr 是 `wake: <agent> not-woken http-201`（`http-<碼>`）；沒有重送

### Requirement: 本機戳的連線結果
本機戳沒有拿到 HTTP 回應時：逾時 MUST 是 `unknown poke-timeout`；其他連線錯誤（`OSError`／`URLError`，含連線被拒、被重置）或 HTTP 層讀不懂（`http.client.HTTPException`）MUST 是 `unknown poke-error`——請求可能已經送出，aicomm 分不出來，MUST NOT 說成 `not-woken`。兩者都 MUST NOT 重送，MUST NOT 改查別台。

#### Scenario: 本機戳的連線被重置
- **WHEN** 本機 `GET` 回 200 `idle`，戳的時候連線被對方重置
- **THEN** stderr 是 `wake: <agent> unknown poke-error`；戳只有一次，沒有呼叫 `pool-peer`；沒帶 `--require-wake` 時 exit 0

#### Scenario: 本機戳的連線被拒
- **WHEN** 本機 `GET` 回 200 `idle`，戳的時候連線被拒（`ConnectionRefusedError`）
- **THEN** stderr 是 `wake: <agent> unknown poke-error`；戳只有一次，沒有呼叫 `pool-peer`；沒帶 `--require-wake` 時 exit 0

### Requirement: 讀 `pool-peer call` 的結束碼
rc 0 MUST 照「戳的回應怎麼判讀（本機與跨機共用）」處理；但 stdout 第一行不是 HTTP 狀態碼時 MUST 是 `unknown peer-bad-answer`（對方回了東西，可能已經戳到）。rc 2 或執行 `call` 失敗（`OSError`）MUST 是 `not-woken peer-unavailable`。rc 3 或子行程逾時 MUST 是 `unknown poke-timeout`。其他 rc MUST 是 `unknown peer-bad-answer`（`pool-peer` 沒照約定回答，不知道送了沒）。MUST NOT 重送。

#### Scenario: `call` 逾時
- **WHEN** `call` 的結束碼是 3
- **THEN** stderr 是 `wake: <agent> unknown poke-timeout`；`call` 只有一次，沒有重送

#### Scenario: `call` 的第一行看不懂
- **WHEN** `call` 回 0，但 stdout 第一行不是 HTTP 狀態碼
- **THEN** stderr 是 `wake: <agent> unknown peer-bad-answer`（對方回了東西，可能已經戳到）

#### Scenario: 執行 `call` 失敗
- **WHEN** `find` 回一台可戳的 sidecar，但執行 `pool-peer call` 時丟出 `OSError`（子行程沒起來）
- **THEN** stderr 是 `wake: <agent> not-woken peer-unavailable`；沒有重送

#### Scenario: `call` 回 2
- **WHEN** `call` 的結束碼是 2（連不到）
- **THEN** stderr 是 `wake: <agent> not-woken peer-unavailable`；`call` 只有一次，沒有重送

#### Scenario: `call` 回了約定外的結束碼
- **WHEN** `call` 的結束碼是 1
- **THEN** stderr 是 `wake: <agent> unknown peer-bad-answer`；`call` 只有一次，沒有重送

### Requirement: `wake:` 行的文法
叫醒的結果 MUST 在 stderr 以固定格式呈現，每一個收件人剛好一行，本機與跨機共用同一個格式：

    wake: <agent> woken local[ interrupt]
    wake: <agent> woken peer:<port>[,<port>…][ interrupt]
    wake: <agent> not-woken <原因碼>[ <說明>]
    wake: <agent> unknown <原因碼>[ <說明>]

`<說明>` MUST 只有一行、不含換行。` interrupt` 後綴 MUST 只在至少有一台**成功（200）**的戳帶 `interrupt: true` 時加上。

#### Scenario: 每個收件人剛好一行
- **WHEN** `send` 帶多個收件人（`--to` 加 `--mention`），叫醒結果各不相同
- **THEN** stderr 每個收件人恰一行，且每一行符合 `^wake: \S+ (woken (local|peer:\d+(,\d+)*)( interrupt)?|not-woken \S+( .*)?|unknown \S+( .*)?)$`

### Requirement: 同一個收件人戳了好幾台時取一個結果
同一個收件人有好幾台的結果時，MUST 照這個順序取一個：只要有一台 200 就是 `woken`（列出成功的那幾個 port，由小到大）；否則只要有一台 `unknown`，就是 `unknown`；都不是就是 `not-woken`。`unknown` 或 `not-woken` 有好幾台時，原因碼 MUST 取 port 由小到大的第一個。

#### Scenario: 多台取一個結果
- **WHEN** 同一個收件人在兩台都可戳，一台 200、一台 `call` 逾時
- **THEN** stderr 是 `wake: <agent> woken peer:<成功的 port>`（有一台 200 就是 `woken`，不寫 `unknown`）

### Requirement: 叫醒的原因碼清單
原因碼 MUST 只有這些（新增要改 `design.md` 或後續 change，並同步 cli 的常數與文件）：`not-woken` 是 `not-found`、`peer-unavailable`、`lookup-incomplete`、`peer-port-out-of-range`、`peer-bad-answer`、`bad-answer`、`bad-name`、`state-<state>`、`agent-not-running`、`awaiting-answer`、`http-<碼>`；`unknown` 是 `poke-timeout`、`poke-error`、`peer-bad-answer`、`internal-error`。`unknown` MUST 只用於「可能已經戳到」的情況：看到它 MUST NOT 重送。

#### Scenario: 原因碼與常數一致
- **WHEN** 檢查 `src/aicomm/cli.py` 的原因碼常數
- **THEN** `not-woken` 與 `unknown` 兩組恰好是上面的清單（`state-<state>`、`http-<碼>` 以前綴表示），沒有多也沒有少

### Requirement: `send` 的結束碼與 `--require-wake`
`send` 的結束碼 MUST 不因叫醒失敗而改變（仍為 0）。選用旗標 `--require-wake` 給了時：只要有任何一位是 `not-woken` 或 `unknown`，MUST 以 **4** 結束（訊息已送達，不要重送）；全部叫醒，或沒有人要叫醒（廣播、只寄給自己），MUST 以 0 結束。`--require-wake` 與 `--no-wake` 同時給 MUST 是 exit 1，且 MUST 在寫進 Angareion 之前就拒絕（MUST NOT 送出訊息、MUST NOT 對 sidecar 或 `pool-peer` 發任何請求）。

`docs/aicomm.md` 與 `skills/aicomm/SKILL.md` MUST 寫明「exit 0 只代表已送達；看 stderr 的 `wake:` 行；exit 4 不要重送」，原因碼的清單 MUST 與 cli 的常數一致。

#### Scenario: `--require-wake` 全部叫醒
- **WHEN** `send --require-wake`，所有收件人都是 `woken`
- **THEN** exit 0

#### Scenario: `--require-wake` 有人沒叫醒
- **WHEN** `send --require-wake`，有一位是 `not-woken` 或 `unknown`
- **THEN** exit 4；訊息已寫進 Angareion（stdout 第一行仍是訊息 id）；不要重送

#### Scenario: `--require-wake` 沒有人要叫醒
- **WHEN** `send --require-wake` 是廣播，或只寄給自己
- **THEN** exit 0

#### Scenario: `--require-wake` 與 `--no-wake` 互斥
- **WHEN** 同時給 `--require-wake` 與 `--no-wake`
- **THEN** exit 1，而且是在寫進 Angareion 之前就拒絕：沒有呼叫 `angareion send`，也沒有對 sidecar 或 `pool-peer` 發任何請求

#### Scenario: 不加旗標時結束碼不變
- **WHEN** 沒帶 `--require-wake`，叫醒失敗或結果未知
- **THEN** exit 0

#### Scenario: 文件與常數一致
- **WHEN** 檢查 `docs/aicomm.md` 與 `skills/aicomm/SKILL.md`
- **THEN** 兩者都寫明「exit 0 只代表已送達、看 stderr 的 `wake:` 行、exit 4 不要重送」，且列出的原因碼與 `src/aicomm/cli.py` 的常數一致
