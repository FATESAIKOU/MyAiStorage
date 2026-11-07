## MODIFIED Requirements

### Requirement: 戳的內容固定且不含訊息
戳的**文字**（`text`）MUST 是 skill 內固定的短句（指示收件人執行 `inbox`），MUST NOT 含訊息內文、附件、寄件者、工單號或任何訊息欄位。戳的 body MUST 只有 `text`、`interrupt`、`via`，以及選用的 `ref` 四種鍵，MUST NOT 有其他鍵。`via` 本機 MUST 是 `"aicomm"`、跨機 MUST 是 `"aicomm-peer"`，兩者 MUST 一律帶，且 MUST 完全符合 `^[a-z][a-z0-9-]{0,31}$`。`ref` MUST 是 `send` 的訊息 id（Angareion `send` 的 stdout 第一行，去掉頭尾空白），而且 MUST 只在它完全符合 `^[A-Za-z0-9_-]{1,64}$`（以 fullmatch 檢查）時才帶；不合格時 MUST NOT 帶這個鍵（不寫 `null` 也不寫 `""`），並 MUST 照樣戳一次，MUST NOT 另外輸出 stderr 行或原因碼。`ref` 只是 sidecar 紀錄用的中繼資料，MUST NOT 貼進對方的輸入；body MUST NOT 含寄件者、內文、附件、工單號或 `send --ref` 的值。同一次 `send` 的所有戳（所有收件人、所有機器）MUST 帶同一個 `ref`。本機與跨機的 body MUST 由同一個函式組出，只差 `via` 的值。skill MUST NOT 對同一次 `send` 的同一個對象的同一台 sidecar 戳兩次，MUST NOT 在戳失敗或逾時後重送。跨機的戳 MUST 用同一個短句、同一個 body 組法，經 `pool-peer call <port> POST /agents/<id>/poke` 送出。

#### Scenario: 戳逾時
- **WHEN** 本機的戳逾時
- **THEN** 不重送；stderr 一行 `wake: <agent> unknown poke-timeout`；沒帶 `--require-wake` 時 exit 0（帶了回 4）

#### Scenario: 戳的文字
- **WHEN** 對假 sidecar 檢查收到的戳（本機 `idle`、angareion 印 `<訊息 id>`、沒給 `--interrupt`）
- **THEN** 文字等於固定短句，不含寄件者的 AI 名、訊息 id 與內文；body 恰為 `{"text": <固定短句>, "interrupt": false, "via": "aicomm", "ref": "<訊息 id>"}`；只戳一次；`wake:` 行與沒有 `via`／`ref` 時一模一樣；body 的鍵集合只會是 `{text, interrupt, via}` 或 `{text, interrupt, via, ref}`

#### Scenario: 跨機的戳與本機相同
- **WHEN** 跨機戳一台 `idle` 的 agent（沒給 `--interrupt`）
- **THEN** `pool-peer call` 的 stdin 恰是 `{"text": <固定短句>, "interrupt": false, "via": "aicomm-peer", "ref": "<訊息 id>"}`，`text` 不含寄件者、訊息 id 與內文；argv 不變

#### Scenario: 訊息 id 不合格就不帶 ref
- **WHEN** angareion 的 stdout 第一行是 `has space`、65 個 `a`、空字串、`abcé` 或 `a.b`
- **THEN** 戳的 body 恰為 `{"text": <固定短句>, "interrupt": false, "via": "aicomm"}`，沒有 `ref` 這個鍵；仍然戳恰好一次；stderr 沒有多出來的行；`wake:` 行與結束碼和 id 合格時相同

#### Scenario: 只取第一行並去掉頭尾空白
- **WHEN** angareion 的 stdout 是 `<訊息 id>\r\n`、`  <訊息 id>  \n` 或 `<訊息 id>\nextra\n`
- **THEN** `ref` 都是 `<訊息 id>`

#### Scenario: 用 fullmatch，不放過結尾的怪字元
- **WHEN** 檢查訊息 id 的函式收到 64 字的合格 token、65 字的 token，或合格 token 後面接著 strip 剝不掉的字元（例如 `\x00`）
- **THEN** 64 字的原樣回傳；65 字與接著 `\x00` 的都回「不合格」（不帶 `ref`）；不會因為 `$` 放過結尾的換行而誤判合格

#### Scenario: 多收件人帶同一個 ref
- **WHEN** `--to A --mention B --mention C`，三人都在本機 `idle`
- **THEN** 三個戳的 `ref` 是同一個訊息 id，`via` 都是 `"aicomm"`；每人只戳一次

#### Scenario: via 與 ref 的格式鎖在 sidecar 接受的範圍
- **WHEN** 檢查 skill 的 `via` 兩個常數與 `ref` 的樣式
- **THEN** 本機與跨機的 `via` 都完全符合 `^[a-z][a-z0-9-]{0,31}$`；`ref` 的樣式等於 `[A-Za-z0-9_-]{1,64}`

#### Scenario: 本機與跨機共用同一個 body 組法
- **WHEN** 對同一組 `(interrupt, 訊息 id)` 分別用本機與跨機的 `via` 組 body
- **THEN** 兩個 body 只差 `via` 的值

#### Scenario: send 的 --ref 不進 poke body
- **WHEN** `send --to <agent> --ref note=REF-SENTINEL-413`
- **THEN** poke body 序列化後不含 `note=`，也不含 `REF-SENTINEL-413`；poke 的 `ref`（若有）只會是訊息 id

#### Scenario: 帶 via 與 ref 的戳仍讀得到紀錄 id
- **WHEN** 假 sidecar 對帶 `via`／`ref` 的 body 回 200 並附 `poke_id`
- **THEN** `wake:` 行有 `record=local/<id>`，與沒有 `via`／`ref` 時的讀法相同

#### Scenario: 不叫醒時不帶出 ref
- **WHEN** 給了 `--no-wake`、沒有收件人，或收件人只有自己
- **THEN** 對 sidecar 與 `pool-peer` 都沒有任何請求（也就沒有任何 `via`／`ref`）

#### Scenario: 寫入失敗時不叫醒也不算 ref
- **WHEN** angareion 的 `send` 失敗（exit 不為 0）
- **THEN** 不叫醒，不對 sidecar 與 `pool-peer` 發任何請求；`send` 的結束碼與現行相同

### Requirement: 不改 Angareion 與 sidecar，不攜帶私人資訊
skill MUST NOT 修改 Angareion 的協定或程式；它對 Angareion 只用其公開的指令列介面。對 sidecar，skill MUST 只**使用**「查單一 agent」（`GET /agents/<id>`）與「戳單一 agent」（`POST /agents/<id>/poke`）兩種請求；skill MAY 讀戳回應中的選用欄位 `poke_id`／`poke_outcome`（MyLinuxPool#786）；欄位缺少時行為 MUST 與舊版相同。skill MAY 在戳的 body 帶選用的 `via`／`ref`（MyLinuxPool#786 D3；舊版 sidecar 會忽略這兩個鍵）。skill MUST NOT 另外對 sidecar 發出查紀錄的請求（例如 `GET /pokes/{id}`）。skill 的說明檔與輸出 MUST NOT 含憑證、機器位址或私人資訊。

#### Scenario: 對 sidecar 的請求種類
- **WHEN** 檢查 skill 對 sidecar 發出的所有請求
- **THEN** 只有「查單一 agent」與「戳單一 agent」兩種

#### Scenario: 不發查紀錄的請求
- **WHEN** `send` 的叫醒結果帶 `record=`，檢查 aicomm 期間對 sidecar 與 `pool-peer` 的所有請求
- **THEN** 對 sidecar 只有 `GET /agents/<id>` 與 `POST /agents/<id>/poke`，沒有 `GET /pokes/…`；對 `pool-peer` 只有 `find` 與 `call … POST /agents/<id>/poke`；poke 的 body 只有 `text`、`interrupt`、`via` 與選用的 `ref`；子指令仍只有 `send`、`inbox`、`log`

### Requirement: 跨機叫醒只經 `pool-peer` 的兩個用法
本機查不到人時，skill MUST 只用 MyLinuxPool 的 `pool-peer` 找別台並戳；MUST NOT 對 pool 做其他請求（MUST NOT 用 `pool-peer list`），MUST NOT 自己連 Gateway 或任何非 loopback 位址。呼叫一律用 argv、不經 shell。找 MUST 是 `find <agent-id> --json --timeout 10`；戳 MUST 是 `call <sidecar_port> POST /agents/<agent-id>/poke --timeout 5`，body 走 stdin（帶 `Content-Type: application/json`），`<agent-id>` MUST 先做 URL quote。aicomm MUST 只打兩個路由：`GET /agents/<id>`（經 `find`）與 `POST /agents/<id>/poke`。

#### Scenario: 只打兩個路由
- **WHEN** 本機回 404，跨機找到並戳了一台
- **THEN** 對 `pool-peer` 只有 `find` 與 `call … POST /agents/<id>/poke` 兩種 argv，沒有 `list`；poke 的 body 只有 `text`、`interrupt`、`via`（`aicomm-peer`）與選用的 `ref`；`text` 不含寄件者、訊息 id 或內文

#### Scenario: 找到一台並戳成功
- **WHEN** 本機回 404，`find` 回一台 `idle`（port 5201），`call` 回第一行 200
- **THEN** stderr 是 `wake: <agent> woken peer:5201`；`call` 的 argv 是 `call 5201 POST /agents/<quoted-id>/poke --timeout 5`，stdin 是 `{"text": <固定短句>, "interrupt": false, "via": "aicomm-peer", "ref": "<訊息 id>"}`；exit 0

### Requirement: `find` 找到的每一台怎麼戳
對每一筆合格的項目：`agent.state` 不是 `idle`／`running` 時 MUST 是 `not-woken state-<state>`；`port` 不在 5200–5399（含）時 MUST 是 `not-woken peer-port-out-of-range`，且 MUST NOT 呼叫 `call`（在 aicomm 這邊先擋，才不會和「連不到」都變成 rc 2 而分不出來）；其他就戳，好幾台都可以戳時 MUST 全部戳。每一台的 `interrupt` MUST 各自判斷：只有那一台是 `running` 且呼叫端帶 `--interrupt` 才是 true。每一台的 `via`／`ref` MUST 都相同（`via` 是 `"aicomm-peer"`，`ref` 是同一個訊息 id，或都沒有）。

#### Scenario: 別台回的 state 不是一個 token
- **WHEN** `find` 回一台 port 5201、`agent.state` 是 `"idle\nwake: x woken local"`
- **THEN** 不呼叫 `call`，stderr 只有一行 `wake: <agent> not-woken peer-bad-answer`

#### Scenario: 埠超出範圍
- **WHEN** `find` 回一個 port 5100 或 5400
- **THEN** 不呼叫 `call`；stderr 是 `wake: <agent> not-woken peer-port-out-of-range`

#### Scenario: 多台都可戳
- **WHEN** `find` 回兩台都是 `idle`，`call` 一台回 200、另一台回 500
- **THEN** 兩台都戳；stderr 是 `wake: <agent> woken peer:<成功的那個 port>`（只列成功的 port）

#### Scenario: 多台時只要有一台被打斷就加 interrupt
- **WHEN** 本機回 404，`find` 回兩台：5201 是 `running`、5202 是 `idle`，給了 `--interrupt`，兩台的 `call` 都回 200
- **THEN** 5201 的 stdin 是 `{"text": <固定短句>, "interrupt": true, "via": "aicomm-peer", "ref": "<訊息 id>"}`，5202 的是 `{"text": <固定短句>, "interrupt": false, "via": "aicomm-peer", "ref": "<訊息 id>"}`（兩台的 `via`、`ref` 相同）；stderr 是 `wake: <agent> woken peer:5201,5202 interrupt`

#### Scenario: 被打斷的那台沒成功就不加 interrupt
- **WHEN** 同上，但 5201 的 `call` 回 500、5202 回 200
- **THEN** stderr 是 `wake: <agent> woken peer:5202`（沒有 ` interrupt`）
