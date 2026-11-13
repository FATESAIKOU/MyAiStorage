## MODIFIED Requirements

### Requirement: send 先寫進 Angareion，成功才算送達
`send` MUST 以 Angareion 的 `send` 指令寫入訊息，並把 Angareion 印出的訊息 id 原樣印在 stdout 的第一行。Angareion 寫入失敗時 `send` MUST 以該失敗的結束碼結束，且 MUST NOT 嘗試叫醒任何人、MUST NOT 對任何 sidecar（本機或其他機器的）發出請求。Angareion 寫入成功後，叫醒失敗 MUST NOT 改變結束碼（仍為 0）；帶 `--require-wake` 時最多回 4，也仍代表訊息已送達（見「`send` 的結束碼與 `--require-wake`」）。叫醒的結果 MUST 以固定格式的 `wake:` 行呈現在 stderr（見「`wake:` 行的文法」）。

#### Scenario: 寫入成功、叫醒失敗
- **WHEN** Angareion 寫入成功，但本機 sidecar 連不上、sidecar-link 的心跳或埠清單也不可用
- **THEN** exit 0，stdout 第一行是訊息 id，stderr 是 `wake: <agent> not-woken peer-unavailable`（本機與跨機都試過）

#### Scenario: 寫入失敗
- **WHEN** Angareion 回 exit 1（例如 channel 不存在）
- **THEN** `send` 以 exit 1 結束，沒有任何對 sidecar 的請求（本機與其他機器的都沒有）

### Requirement: 叫醒的對象與條件
`send` 寫入成功後，MUST 只對 `--to` 與每個 `--mention` 的 AI 名各判斷一次是否叫醒；沒有 `--to` 的廣播 MUST NOT 叫醒任何人；寄給自己 MUST NOT 叫醒自己；`--no-wake` MUST NOT 叫醒任何人。skill MUST NOT 依角色（PM、PMO、隊員）分支；所有角色行為相同。預設戳為不中斷（`interrupt: false`）；給了 `--interrupt` 且收件人 `running` 才帶 `interrupt: true`，收件人 `idle` 時 `interrupt` 仍為 false（`--interrupt` 與 `--no-wake` 互斥，同時給 exit 1）。

對每個名字，MUST **先問本機 sidecar**（`GET /agents/<id>`）：回 200（agent 在這台）且 `state` 是 `idle` 或 `running` 才戳；回 200 但 `state` 是其他值（`starting`、`waiting-quota`、`exited` 等）MUST NOT 戳，也 MUST NOT 再查別台；回 200 但回應看不懂（body 不是 JSON 物件、`state` 不是字串），或 HTTP 層讀不懂，MUST 是 `not-woken bad-answer`，MUST NOT 戳，也 MUST NOT 再查別台；回其他 HTTP 碼（非 200、非 404）MUST NOT 戳，也 MUST NOT 再查別台。只有本機回 404、本機 sidecar 連不上（`OSError`／`URLError`）或 GET 逾時，才 MAY 依「跨機叫醒直連本機的 sidecar-link 埠」改查別台——這時還沒戳任何人，不會重複。本機戳之後，不論結果（含逾時與連線錯誤）MUST NOT 改查別台（可能已經戳到）。

判斷 MUST NOT 依賴 agent 是 tui 或 headless，MUST NOT 讀 `attach` 欄位。`starting`、`waiting-quota`、`exited` MUST NOT 戳。對 `running` 的收件人，預設的不中斷戳照送。不同次 `send` 之間 MUST NOT 去重（連送兩則＝兩次戳）。名字以 `-` 開頭時 MUST NOT 用埠清單查別台：直接是 `not-woken bad-name`（行為與舊版相同；這條原本是為了 `pool-peer` 的 argv，改成直連後技術上不再需要，改它會動 `wake:` 行的行為，另案處理）。

#### Scenario: 閒置的同台 agent
- **WHEN** 收件人在本機 sidecar、`state` 為 `idle`（不論 tui 或 headless）
- **THEN** skill 對它戳一次，不帶 interrupt，stderr 是 `wake: <agent> woken local`，沒有查別的埠

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
- **THEN** 不戳，stderr 是 `wake: <agent> not-woken state-starting`，沒有查別的埠

#### Scenario: 本機回 200 的其他 state 不查別台
- **WHEN** 本機 sidecar 回 200，`state` 是 `exited`
- **THEN** 不戳，stderr 是 `wake: <agent> not-woken state-exited`，沒有查別的埠

#### Scenario: 本機回其他 HTTP 碼不查別台
- **WHEN** 本機 sidecar 的查詢回 500
- **THEN** 不戳，stderr 是 `wake: <agent> not-woken http-500`，沒有查別的埠

#### Scenario: 本機回應看不懂
- **WHEN** 本機 sidecar 回 200，但 body 不是 JSON 物件（或 `state` 不是字串）
- **THEN** 不戳，stderr 是 `wake: <agent> not-woken bad-answer`，沒有查別的埠

#### Scenario: 本機查詢的 HTTP 層讀不懂
- **WHEN** 本機 sidecar 的 `GET /agents/<id>` 丟出 `http.client.HTTPException`（例如回應的狀態行壞掉），還沒戳任何人
- **THEN** 不戳，stderr 是 `wake: <agent> not-woken bad-answer`，沒有查別的埠

#### Scenario: 不在本機 sidecar
- **WHEN** 查詢本機 sidecar 回 404（不在這台），且埠清單上每一台的 sidecar 都回 404
- **THEN** 不戳任何人；stderr 是 `wake: <agent> not-woken not-found`

#### Scenario: 本機連不上時改查別台
- **WHEN** 本機 sidecar 連不上，埠清單有一個埠 P 的 sidecar 回 200 `idle`，戳回 200
- **THEN** 對那台有 `GET /agents/<quoted-id>` 與 `POST /agents/<quoted-id>/poke` 各一次；stderr 是 `wake: <agent> woken peer:P`；exit 0

#### Scenario: 本機戳逾時不改走 find
- **WHEN** 本機 `POST /agents/<id>/poke` 逾時
- **THEN** stderr 是 `wake: <agent> unknown poke-timeout`；沒有查別的埠，也沒有重送

#### Scenario: 名字以 - 開頭
- **WHEN** 收件人的名字以 `-` 開頭，且本機回 404
- **THEN** stderr 是 `wake: <agent> not-woken bad-name`；沒有查別的埠

#### Scenario: 廣播
- **WHEN** 沒給 `--to`
- **THEN** 不對任何 sidecar 發任何請求（本機與其他機器的都沒有）

#### Scenario: mention 也叫醒
- **WHEN** `--mention` 的對象符合上述兩項條件
- **THEN** 同樣戳一次

### Requirement: 戳的內容固定且不含訊息
戳的**文字**（`text`）MUST 是 skill 內固定的短句（指示收件人執行 `inbox`），MUST NOT 含訊息內文、附件、寄件者、工單號或任何訊息欄位。戳的 body MUST 只有 `text`、`interrupt`、`via`，以及選用的 `ref` 四種鍵，MUST NOT 有其他鍵。`via` 本機 MUST 是 `"aicomm"`、跨機 MUST 是 `"aicomm-peer"`，兩者 MUST 一律帶，且 MUST 完全符合 `^[a-z][a-z0-9-]{0,31}$`。`ref` MUST 是 `send` 的訊息 id（Angareion `send` 的 stdout 第一行，去掉頭尾空白），而且 MUST 只在它完全符合 `^[A-Za-z0-9_-]{1,64}$`（以 fullmatch 檢查）時才帶；不合格時 MUST NOT 帶這個鍵（不寫 `null` 也不寫 `""`），並 MUST 照樣戳一次，MUST NOT 另外輸出 stderr 行或原因碼。`ref` 只是 sidecar 紀錄用的中繼資料，MUST NOT 貼進對方的輸入；body MUST NOT 含寄件者、內文、附件、工單號或 `send --ref` 的值。同一次 `send` 的所有戳（所有收件人、所有機器）MUST 帶同一個 `ref`。本機與跨機的 body MUST 由同一個函式組出，只差 `via` 的值。skill MUST NOT 對同一次 `send` 的同一個對象的同一台 sidecar 戳兩次，MUST NOT 在戳失敗或逾時後重送。跨機的戳 MUST 用同一個短句、同一個 body 組法，直接以 `POST http://127.0.0.1:<port>/agents/<id>/poke` 送出（`<port>` 是埠清單裡的一個埠，`<id>` MUST 先做 URL quote）。

#### Scenario: 戳逾時
- **WHEN** 本機的戳逾時
- **THEN** 不重送；stderr 一行 `wake: <agent> unknown poke-timeout`；沒帶 `--require-wake` 時 exit 0（帶了回 4）

#### Scenario: 戳的文字
- **WHEN** 對假 sidecar 檢查收到的戳（本機 `idle`、angareion 印 `<訊息 id>`、沒給 `--interrupt`）
- **THEN** 文字等於固定短句，不含寄件者的 AI 名、訊息 id 與內文；body 恰為 `{"text": <固定短句>, "interrupt": false, "via": "aicomm", "ref": "<訊息 id>"}`；只戳一次；`wake:` 行與沒有 `via`／`ref` 時一模一樣；body 的鍵集合只會是 `{text, interrupt, via}` 或 `{text, interrupt, via, ref}`

#### Scenario: 跨機的戳與本機相同
- **WHEN** 跨機戳一台 `idle` 的 agent（沒給 `--interrupt`）
- **THEN** 那一台 sidecar 收到的 body 恰是 `{"text": <固定短句>, "interrupt": false, "via": "aicomm-peer", "ref": "<訊息 id>"}`，`text` 不含寄件者、訊息 id 與內文；請求是 `POST /agents/<quoted-id>/poke`，目標是 `127.0.0.1:<port>`

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
- **THEN** 對任何 sidecar（本機與其他機器的）都沒有任何請求（也就沒有任何 `via`／`ref`）

#### Scenario: 寫入失敗時不叫醒也不算 ref
- **WHEN** angareion 的 `send` 失敗（exit 不為 0）
- **THEN** 不叫醒，不對任何 sidecar 發任何請求；`send` 的結束碼與現行相同

### Requirement: sidecar 位址固定為預設 loopback 位址
skill MUST 使用 sidecar 文件的預設位址（loopback）；MUST NOT 讀任何設定檔，MUST NOT 提供使用者可設定的位址。測試用覆寫（環境變數 `AICOMM_SIDECAR`，作者提議 P5，可被拒絕）若存在，MUST 只收單一位址、MUST 為 loopback；需要叫醒時覆寫值不是 loopback MUST 以 exit 1 拒絕，且 MUST 在寫進 Angareion 之前拒絕。「需要叫醒」＝沒有 `--no-wake`，且（`--to` 加 `--mention`）排除自己後的候選不是空的。不需要叫醒時 MUST NOT 對 sidecar 發任何請求。這個檢查 MUST NOT 被宣稱為防止跨機叫醒（轉發埠本身是 loopback）。

跨機叫醒要讀的心跳與埠清單，目錄另由環境變數 `AICOMM_SIDECAR_LINK` 指定（只給測試與手動用；sidecar 開的 agent 的環境是白名單，拿不到這個變數）；沒設 MUST 用 `~/.mylinuxpool/sidecar-link`（見「讀 sidecar-link 的心跳」與「讀 sidecar-link 的埠清單」）。這個變數只決定讀哪一個目錄，MUST NOT 被當成 sidecar 位址，也 MUST NOT 被當成使用者可設定的叫醒位址（跨機能打哪些埠由清單決定）。skill MUST NOT 為此讀任何其他設定檔，MUST NOT 從這個目錄讀心跳與埠清單以外的檔。

#### Scenario: 預設位址
- **WHEN** 沒有覆寫
- **THEN** 對預設的 loopback 位址查詢與戳，不讀任何設定檔；跨機讀預設的 `~/.mylinuxpool/sidecar-link/{heartbeat,ports}`

#### Scenario: 覆寫為非 loopback 且要叫醒
- **WHEN** 覆寫值是非 loopback 主機，且 `send` 帶 `--to`、沒有 `--no-wake`
- **THEN** exit 1，沒有訊息被送出、沒有請求被送出

#### Scenario: 覆寫為非 loopback 但不需要叫醒
- **WHEN** 覆寫值是非 loopback 主機，且 `send` 帶 `--no-wake`（或是廣播）
- **THEN** 訊息照常送出，exit 0，沒有任何對 sidecar 的請求

#### Scenario: 跨機執行檔的測試掛鉤
- **WHEN** `AICOMM_SIDECAR_LINK` 指到一個假的清單目錄，且本機回 404
- **THEN** 用它讀心跳與埠清單；它 MUST NOT 被當成 sidecar 位址，查詢與戳仍只打本機的 loopback 位址與清單上各埠的 `127.0.0.1:<port>`

### Requirement: 沒有硬控制，叫醒的節制由 SKILL.md 軟規則承擔
skill MUST NOT 實作名單、角色或權限類的叫醒限制。`skills/aicomm/SKILL.md` MUST 寫明三條軟規則：(1) `send` 預設不中斷，要中斷才加 `--interrupt`；(2) 隊員基本上不要中斷上位職（PM、PMO）；(3) 「收到」這類沒有要對方動作的回覆加 `--no-wake`，不要叫醒對方。SKILL.md MUST 寫明：之後要硬控制，不在 aicomm 做——本機走 sidecar open 帶白名單（MyLinuxPool#346），跨機（經 `127.0.0.1:<port>` 的 sidecar）的存取控制併在 MyPMO#297 再議；並寫明代價：pool 內任何 agent（含別台機器上的）都能叫醒 PMO，與 M2 D6 的 watch 規則並存。SKILL.md MUST NOT 宣稱 aicomm 或 sidecar 白名單擋得住跨機的叫醒。

#### Scenario: 不依角色分支
- **WHEN** 以任何 AI 名呼叫 `send --to <任一收件人>`
- **THEN** 行為只取決於旗標、收件人在本機 sidecar 或埠清單上那一台 sidecar 回報的 `state`，不取決於寄件者或收件人的角色

#### Scenario: SKILL.md 含三條軟規則
- **WHEN** 檢查 `skills/aicomm/SKILL.md`
- **THEN** 三條軟規則、「本機走 sidecar open 白名單（MyLinuxPool#346）」與「跨機的存取控制在 MyPMO#297」皆有明文

#### Scenario: SKILL.md 的代價說的是整個 pool
- **WHEN** 檢查 `skills/aicomm/SKILL.md` 的代價段
- **THEN** 寫的是「pool 內任何 agent（含別台機器上的）都能叫醒 PMO」，沒有「同台」的說法

### Requirement: 叫醒是盡力而為，可關閉
`send` MUST 提供 `--no-wake`，給了就完全不碰 sidecar，也 MUST NOT 讀 sidecar-link 的心跳與埠清單、MUST NOT 對任何一台別的 sidecar 發請求。sidecar 不在、回應看不懂或任何非預期錯誤，MUST 只影響叫醒結果，MUST NOT 影響訊息已送達這件事（`--require-wake` 時最多是 exit 4，仍代表已送達）。預期外的例外 MUST NOT 讓 `send` 失敗：叫醒的兜底是 `wake: <agent> unknown internal-error`。

#### Scenario: 關閉叫醒
- **WHEN** `--no-wake`
- **THEN** 不對任何 sidecar 發任何請求（本機與其他機器的都沒有），也不讀 sidecar-link 的心跳與埠清單

#### Scenario: 預期外的例外
- **WHEN** 叫醒的過程丟出一個沒有對應原因的例外
- **THEN** stderr 是 `wake: <agent> unknown internal-error`；訊息已送達，exit 0（沒帶 `--require-wake`）

### Requirement: 不改 Angareion 與 sidecar，不攜帶私人資訊
skill MUST NOT 修改 Angareion 的協定或程式；它對 Angareion 只用其公開的指令列介面。對 sidecar，skill MUST 只**使用**「查單一 agent」（`GET /agents/<id>`）與「戳單一 agent」（`POST /agents/<id>/poke`）兩種請求——本機的 sidecar 與埠清單上的每一台都一樣；skill MUST NOT 對其中任何一台發出第三種請求。skill MAY 讀戳回應中的選用欄位 `poke_id`／`poke_outcome`（MyLinuxPool#786）；欄位缺少時行為 MUST 與舊版相同。skill MAY 在戳的 body 帶選用的 `via`／`ref`（MyLinuxPool#786 D3；舊版 sidecar 會忽略這兩個鍵）。skill MUST NOT 另外對任何 sidecar 發出查紀錄的請求（例如 `GET /pokes/{id}`）。對 sidecar-link，skill MUST 只讀 `heartbeat` 與 `ports` 兩個檔，MUST NOT 讀該目錄的其他檔、MUST NOT 把清單內容或回應中的機器位址寫進輸出。skill 的說明檔與輸出 MUST NOT 含憑證、機器位址或私人資訊。

#### Scenario: 對 sidecar 的請求種類
- **WHEN** 檢查 skill 對每一台 sidecar（本機與埠清單上的）發出的所有請求
- **THEN** 都只有「查單一 agent」與「戳單一 agent」兩種

#### Scenario: 不發查紀錄的請求
- **WHEN** `send` 的叫醒結果帶 `record=`，檢查 aicomm 期間對所有 sidecar 與 sidecar-link 的存取
- **THEN** 對每一台 sidecar 只有 `GET /agents/<id>` 與 `POST /agents/<id>/poke`，沒有 `GET /pokes/…`；對 sidecar-link 只讀 `heartbeat` 與 `ports`；poke 的 body 只有 `text`、`interrupt`、`via` 與選用的 `ref`；子指令仍只有 `send`、`inbox`、`log`

### Requirement: 戳的回應怎麼判讀（本機與跨機共用）
本機 sidecar 對戳的 HTTP 回應，與埠清單上每一台 sidecar 對戳的 HTTP 回應，MUST 用同一張表判讀：200 MUST 是這台叫醒。409 時 MUST 依序找 body 的巢狀錯誤碼（`error` 是 JSON 物件且 `error.code` 是字串時）、頂層的 `code`、頂層字串的 `error`，**第一個是字串的就定案**；定案的值不認得，或三處都不是字串時，MUST 是 `not-woken http-409`，MUST NOT 再往下找。`agent_not_running` MUST 是 `not-woken agent-not-running`，`agent_awaiting_answer` MUST 是 `not-woken awaiting-answer`，其他、沒有或 body 看不懂 MUST 是 `not-woken http-409`。其他任何碼（含 200 以外的 2xx）MUST 是 `not-woken http-<碼>`。409 MUST 讀出錯誤回應的 body 再判斷（本機與別台都一樣）。MUST NOT 重送。

判讀的同時，skill MAY 讀 body 頂層的兩個選用欄位（MyLinuxPool#786，poke-records-v1 已談定）：`poke_id` MUST 只有在是字串且完全符合 `^[A-Za-z0-9_-]{1,64}$` 時才採用，否則 MUST 視為沒有；`poke_outcome` MUST 只有值是 `"queued"` 才算「對方忙（排隊）」，其他值（含錯誤回應的 `refused`／`failed`）、型別不對或沒有這個欄位 MUST 視為沒有。**任何 HTTP 碼**的回應，只要 `poke_id` 合格就 MUST 採用；200 另外讀 `poke_outcome`。這兩個欄位 MUST NOT 改變上面判讀出來的種類與原因碼，MUST NOT 改變戳的次數，也 MUST NOT 讓 skill 另外發出請求。`interrupt` 為真時 MUST NOT 印 ` queued`（見「`wake:` 行的文法」）。

#### Scenario: 本機戳回 409 與其他碼
- **WHEN** 本機 `GET` 回 200 `idle`，戳回 409 且 body 是 `{"code": "agent_awaiting_answer"}`／`{"error": "agent_not_running"}`，或戳回 503
- **THEN** 分別是 `wake: <agent> not-woken awaiting-answer`、`not-woken agent-not-running`、`not-woken http-503`；只戳一次，沒有查別的埠

#### Scenario: `call` 回 409 與其他碼
- **WHEN** 埠清單上那一台戳回 409 且 body 的 `error` 是 `agent_awaiting_answer`／`agent_not_running`，或回 500
- **THEN** 分別是 `wake: <agent> not-woken awaiting-answer`、`not-woken agent-not-running`、`not-woken http-500`

#### Scenario: 409 的 body 看不懂或沒有錯誤碼
- **WHEN** 戳回 409，但 body 不是 JSON 物件，或 `error.code`／`code`／字串 `error` 都不是字串
- **THEN** stderr 是 `wake: <agent> not-woken http-409`；只戳一次，沒有重送

#### Scenario: 409 的巢狀錯誤碼
- **WHEN** 本機或別台的戳回 409，body 是 `{"error": {"code": "agent_awaiting_answer", "message": "…"}}`、`{"error": {"code": "agent_not_running"}}` 或 `{"error": {"code": "poke_timeout"}}`
- **THEN** 分別是 `wake: <agent> not-woken awaiting-answer`、`not-woken agent-not-running`、`not-woken http-409`；平面形狀（`{"code": …}`、`{"error": "<字串>"}`）的結果和現在相同

#### Scenario: 409 的錯誤碼優先序
- **WHEN** 戳回 409，body 是 `{"error": {"code": "weird"}, "code": "agent_not_running"}`、`{"error": {"code": 5}, "code": "agent_not_running"}` 或 `{"error": {"message": "x"}}`
- **THEN** 分別是 `wake: <agent> not-woken http-409`（巢狀的碼是字串就定案，不認得也不往下找）、`wake: <agent> not-woken agent-not-running`（巢狀的碼不是字串，才往下找頂層的 `code`）、`wake: <agent> not-woken http-409`

#### Scenario: 200 以外的 2xx
- **WHEN** 戳回 201（或任何不是 200 的 2xx）
- **THEN** stderr 是 `wake: <agent> not-woken http-201`（`http-<碼>`）；沒有重送

#### Scenario: 本機戳回 200 並附紀錄 id
- **WHEN** 本機 `GET` 回 200 `idle`，戳回 200 且 body 含 `"poke_id": "<ID>", "poke_outcome": "sent"`
- **THEN** stderr 恰為 `wake: <agent> woken local record=local/<ID>`；exit 0；只戳一次

#### Scenario: 本機戳回 200 且對方忙（排隊）
- **WHEN** 本機 `GET` 回 200 `running`、沒帶 `--interrupt`，戳回 200 且 `"poke_outcome": "queued"`，並附合格的 `poke_id`
- **THEN** stderr 是 `wake: <agent> woken local queued record=local/<ID>`；沒帶與帶 `--require-wake` 都是 exit 0

#### Scenario: 舊版 sidecar 的 200 沒有欄位
- **WHEN** 戳回 200，body 沒有 `poke_id` 也沒有 `poke_outcome`（舊版 sidecar）
- **THEN** stderr 是 `wake: <agent> woken local`（和現在一字不差）；沒有 `record=`

#### Scenario: interrupt 與排隊同時回報
- **WHEN** 本機 `GET` 回 200 `running`、帶 `--interrupt`，戳回 200 且 `"poke_outcome": "queued"`，並附合格的 `poke_id`
- **THEN** stderr 是 `wake: <agent> woken local interrupt record=local/<ID>`；不印 ` queued`

#### Scenario: 不合格的 poke_id 與 poke_outcome
- **WHEN** 戳回 200，`poke_id` 是 `"a b"`、含換行的字串、65 個字元、數字 `123` 或 `null`，且 `poke_outcome` 是 `"later"`、`1` 或 `null`
- **THEN** 不印 `record=`，也不印 ` queued`；種類不變；每位收件人仍恰好一行

#### Scenario: 409 與 503 的紀錄 id
- **WHEN** 戳回 409 且 body 是 `{"code": "agent_awaiting_answer", "poke_id": "<ID>"}`，或戳回 503 且 body 含合格的 `poke_id`（`"unavailable"`）
- **THEN** 分別是 `wake: <agent> not-woken awaiting-answer record=local/<ID>` 與 `wake: <agent> not-woken http-503 record=local/<ID>`；只戳一次，沒有重送

#### Scenario: 跨機戳的紀錄 id
- **WHEN** 埠清單有 5201，那一台是 `running`；戳回 200 且 body 是 `{"poke_id":"<ID>","poke_outcome":"queued"}`
- **THEN** stderr 是 `wake: <agent> woken peer:5201 queued record=peer:5201/<ID>`；沒帶與帶 `--require-wake` 都是 exit 0

#### Scenario: 404 與 500 的紀錄 id
- **WHEN** 戳回 404 或 500，錯誤 body 帶合格的 `poke_id`
- **THEN** 原本的那一行尾端加上 ` record=local/<ID>`（例如 `wake: <agent> not-woken http-404 record=local/<ID>`、`wake: <agent> not-woken http-500 record=local/<ID>`）；只戳一次，沒有重送

### Requirement: 本機戳的連線結果
本機戳沒有拿到 HTTP 回應時：逾時 MUST 是 `unknown poke-timeout`；其他連線錯誤（`OSError`／`URLError`，含連線被拒、被重置）或 HTTP 層讀不懂（`http.client.HTTPException`）MUST 是 `unknown poke-error`——請求可能已經送出，aicomm 分不出來，MUST NOT 說成 `not-woken`。兩者都 MUST NOT 重送，MUST NOT 改查別台。

#### Scenario: 本機戳的連線被重置
- **WHEN** 本機 `GET` 回 200 `idle`，戳的時候連線被對方重置
- **THEN** stderr 是 `wake: <agent> unknown poke-error`；戳只有一次，沒有查別的埠；沒帶 `--require-wake` 時 exit 0

#### Scenario: 本機戳的連線被拒
- **WHEN** 本機 `GET` 回 200 `idle`，戳的時候連線被拒（`ConnectionRefusedError`）
- **THEN** stderr 是 `wake: <agent> unknown poke-error`；戳只有一次，沒有查別的埠；沒帶 `--require-wake` 時 exit 0

### Requirement: 跨機叫醒的時間預算
收件人 MUST 一個一個處理。查埠清單 MUST 有總預算 10 秒（從第一台開始查起算）：每個埠的 `GET /agents/<id>` 逾時 MUST 是 3 秒與「剩下預算」中較小的一個；預算用完還沒查的埠 MUST 當成「查不到」（見「每一個埠怎麼查」）。能戳的每一台，戳 MUST 各用 5 秒，不另設總預算。超過上限的那一次請求的結果照「跨機查詢與戳的連線結果」判讀，MUST NOT 讓 `send` 卡住或失敗。

#### Scenario: 查詢超過預算
- **WHEN** 埠清單有若干台，前幾台的查詢吃光 10 秒預算，還有埠沒查
- **THEN** 沒查的埠當成「查不到」；若沒有任何一台給出可讀的回答，結果是 `wake: <agent> not-woken lookup-incomplete`；`send` 仍然 exit 0（沒帶 `--require-wake`）

#### Scenario: 子行程卡住
- **WHEN** 某一台的查詢超過 3 秒（或剩下的預算），或某一台的戳超過 5 秒
- **THEN** aicomm 自己放棄那一次請求（沒有子行程可收，直接放棄連線）；查詢算「查不到」，戳是 `wake: <agent> unknown poke-timeout`；`send` 仍然 exit 0（沒帶 `--require-wake`）

### Requirement: `wake:` 行的文法
叫醒的結果 MUST 在 stderr 以固定格式呈現，每一個收件人剛好一行，本機與跨機共用同一個格式：

    wake: <agent> woken local[ interrupt| queued][ record=<records>]
    wake: <agent> woken peer:<port>[,<port>…][ interrupt| queued][ record=<records>]
    wake: <agent> not-woken <原因碼>[ record=<records>]
    wake: <agent> unknown <原因碼>[ record=<records>]

    <records> := <rec>[,<rec>…]
    <rec>     := local/<id> | peer:<port>/<id>
    <id>      := [A-Za-z0-9_-]{1,64}

每一行之後 MUST NOT 有其他文字。` peer:<port>` 的埠 MUST 是 sidecar-link 埠清單裡的一個埠，MUST NOT 是本機 sidecar 的埠。` interrupt` 後綴 MUST 只在至少有一台**成功（200）**的戳帶 `interrupt: true` 時加上。` interrupt` 與 ` queued` MUST 互斥；sidecar 同時回報 interrupt 與排隊時，MUST 以 ` interrupt` 為準。` queued` MUST 只在收件人的每一次成功（200）的戳都回報排隊時才加上（見「同一個收件人戳了好幾台時取一個結果」）。` record=` MUST 只在至少有一個合格的 sidecar 紀錄 id 時才寫；位置 MUST 是 `local`（本機）或 `peer:<port>`（跨機），多筆依位置以逗號串接（port 由小到大）；沒有 id 時整段 MUST NOT 寫，該行的其餘部分 MUST 與舊版一字不差。id 與排隊的來源、採用條件見「戳的回應怎麼判讀（本機與跨機共用）」。` record=` 的用途是對照 sidecar 的紀錄：本機用 `GET /pokes/<id>`（文件寫 `curl -s 127.0.0.1:8471/pokes/<id>`），跨機用同一個路由（文件寫 `curl -s 127.0.0.1:<port>/pokes/<id>`）；這兩條查法是文件給人的用法，不是 skill 自己發的請求。`queued` MUST NOT 被當成原因碼。

#### Scenario: 每個收件人剛好一行
- **WHEN** `send` 帶多個收件人（`--to` 加 `--mention`），叫醒結果各不相同
- **THEN** stderr 每個收件人恰一行，且每一行以 `re.fullmatch` 符合 `^wake: \S+ (woken (local|peer:\d+(,\d+)*)( interrupt| queued)?|(not-woken|unknown) [A-Za-z0-9_-]+)( record=(local|peer:\d+)/[A-Za-z0-9_-]{1,64}(,(local|peer:\d+)/[A-Za-z0-9_-]{1,64})*)?$`；以下反例 MUST NOT 符合：`wake: a woken local interrupt queued`、`wake: a woken local queued interrupt record=local/x`、`wake: a woken local bogus`、`wake: a woken local record=x/y`、`wake: a woken local record=local/a b`、`wake: a woken local record=local/` 加 65 個字元

#### Scenario: 沒有戳就沒有 record
- **WHEN** 叫醒結果是 `state-exited`、`not-found`、`bad-name`、`peer-unavailable`、`lookup-incomplete` 或 `peer-port-out-of-range`（沒有戳任何人）
- **THEN** 沒有 `record=`；該行與現在一字不差

#### Scenario: 戳的傳輸失敗沒有 record
- **WHEN** 戳逾時、連線錯誤，或某一台的戳連線被拒、回應 HTTP 層讀不懂（回應沒有可用 body）
- **THEN** 沒有 `record=`；結果是 `unknown poke-timeout`、`unknown poke-error`、`not-woken peer-unavailable` 或 `unknown peer-bad-answer` 等，與現在一字不差

### Requirement: 同一個收件人戳了好幾台時取一個結果
同一個收件人在埠清單上有一台以上的結果時，MUST 照這個順序取一個：只要有一台 200 就是 `woken`（列出成功的那幾個 port，由小到大）；否則只要有一台 `unknown`，就是 `unknown`；都不是就是 `not-woken`。`unknown` 或 `not-woken` 有好幾台時，原因碼 MUST 取 port 由小到大的第一個。`record=` MUST 列出每一台有拿到合格 `poke_id` 的戳（含失敗的那幾台），依 port 由小到大。` queued` MUST 只在成功（200）的每一台都回報排隊時才加；只要有一台是當下送出，MUST NOT 加 ` queued`。

#### Scenario: 多台取一個結果
- **WHEN** 同一個收件人在兩台都可戳，一台 200、另一台的戳逾時；成功的那台沒有回 id（舊版 sidecar）
- **THEN** stderr 是 `wake: <agent> woken peer:<成功的 port>`（有一台 200 就是 `woken`，不寫 `unknown`）；逾時的那台沒有紀錄 id

#### Scenario: 一台當下送、一台排隊不印 queued
- **WHEN** 收件人在 5201 與 5202 都可戳；5201 回 200 `"poke_outcome": "sent"` 並附 `<A>`，5202 回 200 `"poke_outcome": "queued"` 並附 `<B>`
- **THEN** stderr 是 `wake: <agent> woken peer:5201,5202 record=peer:5201/<A>,peer:5202/<B>`；不印 ` queued`

#### Scenario: 多台都排隊才印 queued
- **WHEN** 同一個收件人在 5201 與 5202 都回 200 `"poke_outcome": "queued"`，兩台都有合格的 `poke_id`
- **THEN** stderr 是 `wake: <agent> woken peer:5201,5202 queued record=peer:5201/<A>,peer:5202/<B>`

#### Scenario: 失敗的那台也列紀錄 id
- **WHEN** 收件人在 5201 回 409 並附 `<A>`，在 5202 回 200 `"poke_outcome": "queued"` 並附 `<B>`
- **THEN** stderr 是 `wake: <agent> woken peer:5202 queued record=peer:5201/<A>,peer:5202/<B>`

### Requirement: 叫醒的原因碼清單
原因碼 MUST 只有這些（新增要改 `design.md` 或後續 change，並同步 cli 的常數與文件）：`not-woken` 是 `not-found`、`peer-unavailable`、`lookup-incomplete`、`peer-port-out-of-range`、`peer-bad-answer`、`bad-answer`、`bad-name`、`state-<state>`、`agent-not-running`、`awaiting-answer`、`http-<碼>`；`unknown` 是 `poke-timeout`、`poke-error`、`peer-bad-answer`、`internal-error`。跨機的碼 MUST 對應到「讀 sidecar-link 的心跳」、「讀 sidecar-link 的埠清單」、「每一個埠怎麼查」與「每一個埠怎麼戳」的結果：`not-found`＝清單上每一台都 404；`lookup-incomplete`＝至少一台查不到、而且沒有任何一台給出可讀的回答；`peer-unavailable`＝心跳或清單不可用，或某一台的戳連線被拒；`peer-port-out-of-range`＝清單的某一行是整數但不在 1–65535；`peer-bad-answer`＝清單格式不對、某一台的查詢回應讀不懂，或某一台的戳回應 HTTP 層讀不懂。`unknown` MUST 只用於「可能已經戳到」的情況：看到它 MUST NOT 重送。

#### Scenario: 原因碼與常數一致
- **WHEN** 檢查 `src/aicomm/cli.py` 的原因碼常數
- **THEN** `not-woken` 與 `unknown` 兩組恰好是上面的清單（`state-<state>`、`http-<碼>` 以前綴表示），沒有多也沒有少

### Requirement: `send` 的結束碼與 `--require-wake`
`send` 的結束碼 MUST 不因叫醒失敗而改變（仍為 0）。選用旗標 `--require-wake` 給了時：只要有任何一位是 `not-woken` 或 `unknown`，MUST 以 **4** 結束（訊息已送達，不要重送）；全部叫醒（`woken`，**含 `woken … queued`**），或沒有人要叫醒（廣播、只寄給自己），MUST 以 0 結束。`--require-wake` 與 `--no-wake` 同時給 MUST 是 exit 1，且 MUST 在寫進 Angareion 之前就拒絕（MUST NOT 送出訊息、MUST NOT 對任何 sidecar 發任何請求、MUST NOT 讀 sidecar-link 的心跳與埠清單）。

`docs/aicomm.md` 與 `skills/aicomm/SKILL.md` MUST 寫明「exit 0 只代表已送達；看 stderr 的 `wake:` 行；exit 4 不要重送」，`woken … queued` 的意思是對方忙（排隊）、不要重送，`record=` 的意思與兩條查法（本機 `GET /pokes/<id>`，跨機同一個路由 `curl -s 127.0.0.1:<port>/pokes/<id>`），以及「回報一律帶 `--to`」。原因碼的清單 MUST 與 cli 的常數一致，`queued` MUST NOT 列為原因碼。

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
- **THEN** exit 1，而且是在寫進 Angareion 之前就拒絕：沒有呼叫 `angareion send`，也沒有對任何 sidecar 發任何請求，也沒有讀 sidecar-link 的心跳與埠清單

#### Scenario: 不加旗標時結束碼不變
- **WHEN** 沒帶 `--require-wake`，叫醒失敗或結果未知
- **THEN** exit 0

#### Scenario: 文件與常數一致
- **WHEN** 檢查 `docs/aicomm.md` 與 `skills/aicomm/SKILL.md`
- **THEN** 兩者都寫明「exit 0 只代表已送達、看 stderr 的 `wake:` 行、exit 4 不要重送」，且列出的原因碼與 `src/aicomm/cli.py` 的常數一致

#### Scenario: 排隊算叫醒
- **WHEN** `send --require-wake`，所有收件人都是 `woken … queued`
- **THEN** exit 0（排隊不需要重送）

#### Scenario: 文件寫明排隊與紀錄 id 的用法
- **WHEN** 檢查 `docs/aicomm.md` 與 `skills/aicomm/SKILL.md`
- **THEN** 兩者都含 `woken local queued`、`record=`、「回報一律帶 `--to`」、`GET /pokes/`（本機與跨機兩條查法），以及「`queued` 不要重送」；`queued` 沒有被列為原因碼

## ADDED Requirements

### Requirement: 跨機叫醒直連本機的 sidecar-link 埠
本機查不到人時，skill MUST 依「讀 sidecar-link 的心跳」與「讀 sidecar-link 的埠清單」讀出埠清單，並對清單裡**每一個**埠 P（跳過與本機 sidecar 同一個埠）直接以 `GET http://127.0.0.1:P/agents/<id>` 查、以 `POST http://127.0.0.1:P/agents/<id>/poke` 戳；MUST NOT 呼叫 `pool-peer` 或任何其他外部指令，MUST NOT 自己連 Gateway 或任何非 loopback 位址。`<id>` MUST 先做 URL quote。aicomm 對每一台 sidecar MUST 只打兩個路由：`GET /agents/<id>` 與 `POST /agents/<id>/poke`。同一個 id 在好幾台上時 MUST 全部戳（與舊版 `pool-peer find` 都列出的語意相同），結果合併成一行（見「同一個收件人戳了好幾台時取一個結果」）。

#### Scenario: 只打兩個路由
- **WHEN** 本機回 404，跨機在兩台找到並戳了
- **THEN** 對那兩台都只有 `GET /agents/<id>` 與 `POST /agents/<id>/poke`，沒有任何第三種請求；poke 的 body 只有 `text`、`interrupt`、`via`（`aicomm-peer`）與選用的 `ref`；`text` 不含寄件者、訊息 id 或內文

#### Scenario: 找到一台並戳成功
- **WHEN** 本機回 404，埠清單有一個埠 5201，那一台的 `GET` 回 200 `idle`、戳回 200
- **THEN** stderr 是 `wake: <agent> woken peer:5201`；對 `127.0.0.1:5201` 有 `GET /agents/<quoted-id>` 與 `POST /agents/<quoted-id>/poke` 各一次；exit 0

#### Scenario: 同一個 id 在兩台
- **WHEN** 本機回 404，埠清單的兩台都回 200 `idle`
- **THEN** 兩台都戳；stderr 是 `wake: <agent> woken peer:<兩個埠，由小到大>`

### Requirement: 讀 sidecar-link 的心跳
跨機叫醒用的目錄 MUST 是 `~/.mylinuxpool/sidecar-link`（或 `AICOMM_SIDECAR_LINK` 指定的目錄，只給測試與手動用）。skill MUST 先讀 `heartbeat`：讀不到、mtime 與現在的差超過 90 秒（絕對值；mtime 遠在未來也算不新鮮）、內容不是符合 `^[1-9][0-9]*$` 的 pid、或 `os.kill(pid, 0)` 說那個 pid 不在（`ProcessLookupError`；`PermissionError` 算活著，其他 `OSError` 算不在），MUST 視為「查不了」（見「查不了」的結果）。MUST NOT 把 0 或負數交給 `os.kill`。

#### Scenario: 沒有心跳
- **WHEN** 本機回 404，目錄裡沒有 `heartbeat`
- **THEN** stderr 是 `wake: <agent> not-woken peer-unavailable`；exit 0；沒有任何對別的 sidecar 的請求

#### Scenario: 心跳不新鮮
- **WHEN** 本機回 404，`heartbeat` 的 mtime 是 91 秒前，或檔內 pid 已經不在
- **THEN** stderr 是 `wake: <agent> not-woken peer-unavailable`；沒有任何對別的 sidecar 的請求

#### Scenario: 心跳剛好在新鮮窗內
- **WHEN** 本機回 404，`heartbeat` 的 mtime 是 89 秒前、內容是活著的 pid，`ports` 有一個可用的埠
- **THEN** 照那個埠查；不是 `peer-unavailable`

### Requirement: 讀 sidecar-link 的埠清單
心跳新鮮時 skill MUST 讀同一個目錄的 `ports`：每行一個十進位埠；重複的埠 MUST 只留第一次出現的那一筆，處理時由小到大。讀不到、或沒有任何可用的埠 MUST 視為「查不了」（見「查不了」的結果）；不是 UTF-8、有空行（只允許最後一個換行造成的一個空字串）、或某一行不是 `^[0-9]{1,5}$` MUST 是 `not-woken peer-bad-answer`；某一行是整數但不在 1–65535 MUST 是 `not-woken peer-port-out-of-range`。清單含本機 sidecar 的埠時 MUST 跳過它。

#### Scenario: 沒有埠清單或清單是空的
- **WHEN** 本機回 404，心跳新鮮，但目錄裡沒有 `ports`，或 `ports` 是空的
- **THEN** stderr 是 `wake: <agent> not-woken peer-unavailable`（MAS-2：查不了，不改走別的路徑）

#### Scenario: 清單格式不對
- **WHEN** `ports` 不是 UTF-8、中間有空行、或某一行不是十進位（例如 `52x1`）
- **THEN** stderr 是 `wake: <agent> not-woken peer-bad-answer`；不戳任何人

#### Scenario: 清單的埠超出範圍
- **WHEN** `ports` 有一行是 `0` 或 `70000`
- **THEN** stderr 是 `wake: <agent> not-woken peer-port-out-of-range`；不戳任何人

#### Scenario: 同一個埠出現兩次
- **WHEN** `ports` 有兩行同一個埠
- **THEN** 只查、只戳那個埠一次；不重複

#### Scenario: 本機的埠在清單裡
- **WHEN** `ports` 含著本機 sidecar 的埠（預設 8471，或 `AICOMM_SIDECAR` 的埠）
- **THEN** 跳過那個埠；不會對同一個 sidecar 查第二次或戳第二次

### Requirement: 「查不了」的結果
「查不了」＝心跳或埠清單不可用。MUST 是 `not-woken peer-unavailable`（MAS-2；與舊版 `pool-peer` rc 2 同一個語意），且 MUST NOT 改走別的路徑（MUST NOT 退回 `pool-peer`、MUST NOT 讀別的檔、MUST NOT 試非 loopback 位址）。skill MUST NOT 快取 `heartbeat` 與 `ports`：每一位收件人的後援都重新讀。

#### Scenario: 查不了不改走別的路徑
- **WHEN** 本機回 404，心跳或埠清單不可用，而 PATH 上（或 `AICOMM_POOL_PEER`）有一個可用的 `pool-peer`
- **THEN** 仍然是 `wake: <agent> not-woken peer-unavailable`；那一個 `pool-peer` MUST NOT 被執行

#### Scenario: 不快取
- **WHEN** 一次 `send` 有兩個收件人都要走跨機後援，兩次之間 `ports` 的內容變了
- **THEN** 第二位收件人用新的清單（每一位都重新讀，沒有快取）

### Requirement: 每一個埠怎麼查
對清單裡每一個埠 P（跳過本機的埠），MUST 以 `GET http://127.0.0.1:P/agents/<id>` 查（`<id>` 先 URL quote），結果 MUST 分成四類：404＝這台確定沒有；200 且 body 是 JSON 物件、`state` 是字串＝這台有回答；200 但 body／`state` 讀不懂＝讀不懂；連不上、逾時、HTTP 層讀不懂、200／404 以外的狀態碼、或查詢預算用完＝查不到。404 與查不到的埠 MUST NOT 列入結果清單（與舊版只列找到的機器相同）；所有埠都 404 MUST 是 `not-woken not-found`；不是所有埠都 404（至少一台查不到）且沒有任何一台給出回答（全部是 404 或查不到），MUST 是 `not-woken lookup-incomplete`。

#### Scenario: 全部 404
- **WHEN** 清單的每一個埠都回 404
- **THEN** 不戳任何人；stderr 是 `wake: <agent> not-woken not-found`

#### Scenario: 部分查不到
- **WHEN** 清單有兩個埠，一個回 404、一個連不上
- **THEN** 不戳任何人；stderr 是 `wake: <agent> not-woken lookup-incomplete`（不知道那一台在不在）

#### Scenario: 別台的查詢回應讀不懂
- **WHEN** 清單有一個埠回 200，但 body 不是 JSON 物件或 `state` 不是字串
- **THEN** 不戳那一台，stderr 是 `wake: <agent> not-woken peer-bad-answer`

### Requirement: 每一個埠怎麼戳
對「有回答」的每一台：`state` 是 `idle`／`running` MUST 戳（`POST http://127.0.0.1:P/agents/<id>/poke`，body 與本機同一個組法、`via` 是 `"aicomm-peer"`）；其他值 MUST NOT 戳，結果是 `not-woken state-<state>`；`state` 不是一個 token（`[A-Za-z0-9_-]+`）MUST NOT 戳，結果是 `not-woken peer-bad-answer`。「讀不懂」的那一台 MUST NOT 戳，結果是 `not-woken peer-bad-answer`。能戳的每一台 MUST 都戳（MAS-3）；每一台的 `interrupt` MUST 各自判斷（只有那一台是 `running` 且呼叫端帶 `--interrupt` 才是 true）；`via`／`ref` 每一台都相同。

#### Scenario: 只有一台有這個 id
- **WHEN** 清單有三個埠，兩個回 404、一個回 200 `idle`
- **THEN** 只戳那一台；stderr 是 `wake: <agent> woken peer:<那一個埠>`

#### Scenario: 別台回的 state 不是一個 token
- **WHEN** 清單有一個埠回 200、`state` 是 `"idle\nwake: x woken local"`
- **THEN** 不戳那一台，stderr 只有一行 `wake: <agent> not-woken peer-bad-answer`

#### Scenario: 多台都可戳
- **WHEN** 清單有兩台都是 `idle`，一台戳回 200、另一台回 500
- **THEN** 兩台都戳；stderr 是 `wake: <agent> woken peer:<成功的那個 port>`（只列成功的 port）

#### Scenario: 多台時只要有一台被打斷就加 interrupt
- **WHEN** 本機回 404，清單有兩台：5201 是 `running`、5202 是 `idle`，給了 `--interrupt`，兩台的戳都回 200
- **THEN** 5201 的 body 是 `{"text": <固定短句>, "interrupt": true, "via": "aicomm-peer", "ref": "<訊息 id>"}`，5202 的是 `{"text": <固定短句>, "interrupt": false, "via": "aicomm-peer", "ref": "<訊息 id>"}`（兩台的 `via`、`ref` 相同）；stderr 是 `wake: <agent> woken peer:5201,5202 interrupt`

#### Scenario: 被打斷的那台沒成功就不加 interrupt
- **WHEN** 同上，但 5201 的戳回 500、5202 回 200
- **THEN** stderr 是 `wake: <agent> woken peer:5202`（沒有 ` interrupt`）

### Requirement: 跨機查詢與戳的連線結果
跨機查詢（`GET /agents/<id>`）沒有可用回答時：連不上、逾時、HTTP 層讀不懂（`http.client.HTTPException`）、200／404 以外的狀態碼，MUST 都算那一台「查不到」（見「每一個埠怎麼查」），MUST NOT 說成 `unknown`（還沒戳任何人）。跨機戳（`POST /agents/<id>/poke`）沒有拿到 HTTP 回應時：逾時 MUST 是 `unknown poke-timeout`；連線被拒（`ConnectionRefusedError`，含包在 `urllib.error.URLError` 裡的）MUST 是 `not-woken peer-unavailable`（確定沒送出去）；其他連線錯誤（被重置等）MUST 是 `unknown poke-error`；HTTP 層讀不懂 MUST 是 `unknown peer-bad-answer`（那一台回了東西，可能已經戳到）。每一種都 MUST NOT 重送、MUST NOT 改查別台。

#### Scenario: 某一台連不上
- **WHEN** 本機回 404，清單有一個埠沒有人監聽
- **THEN** 那一台算「查不到」；若沒有其他台給出回答，stderr 是 `wake: <agent> not-woken lookup-incomplete`

#### Scenario: 某一台戳的連線被拒
- **WHEN** 查詢回 200 `idle`，戳的時候連線被拒
- **THEN** stderr 是 `wake: <agent> not-woken peer-unavailable`；戳只有一次，沒有重送

#### Scenario: 某一台戳的連線被重置
- **WHEN** 查詢回 200 `idle`，戳的時候連線被重置
- **THEN** stderr 是 `wake: <agent> unknown poke-error`；戳只有一次，沒有重送

#### Scenario: 某一台戳逾時
- **WHEN** 查詢回 200 `idle`，戳超過 5 秒沒有回應
- **THEN** stderr 是 `wake: <agent> unknown poke-timeout`；戳只有一次，沒有重送

#### Scenario: 某一台戳的回應讀不懂
- **WHEN** 查詢回 200 `idle`，戳的時候 HTTP 層讀不懂（例如狀態行壞掉）
- **THEN** stderr 是 `wake: <agent> unknown peer-bad-answer`（那一台回了東西，可能已經戳到）；沒有重送

## REMOVED Requirements

### Requirement: 跨機叫醒只經 `pool-peer` 的兩個用法
**Reason**: aicomm 不再呼叫 `pool-peer`（MyAiStorage#457 的 MAS-1）；跨機叫醒改成直連本機的 sidecar-link 埠。舊條文只約束呼叫 `pool-peer` 的 argv 與兩個路由，argv 的部分沒有對象了；「只打兩個路由」的約束由「跨機叫醒直連本機的 sidecar-link 埠」與「不改 Angareion 與 sidecar，不攜帶私人資訊」承接。

**Migration**: 對每一台 sidecar 仍然只有 `GET /agents/<id>` 與 `POST /agents/<id>/poke`；原本經 `pool-peer call` 的戳改成直接 `POST http://127.0.0.1:<port>/agents/<id>/poke`。

### Requirement: 找 `pool-peer` 執行檔
**Reason**: 沒有子行程就沒有執行檔可找（MAS-1）；`AICOMM_POOL_PEER`、PATH 與 `~/.mylinuxpool/bin/pool-peer` 的搜尋全部移除。

**Migration**: 測試與手動的鉤子改成 `AICOMM_SIDECAR_LINK`（指定 sidecar-link 的目錄），見「sidecar 位址固定為預設 loopback 位址」與「讀 sidecar-link 的心跳」與「讀 sidecar-link 的埠清單」；「查不了」仍然是 `not-woken peer-unavailable`。

### Requirement: 讀 `pool-peer find` 的結束碼
**Reason**: 沒有 `find` 就沒有結束碼可讀；查詢的結果改成逐埠的 HTTP 結果（MAS-1）。

**Migration**: 舊的四個語意由「每一個埠怎麼查」與「每一個埠怎麼戳」承接：所有埠 404 → `not-found`（舊 rc 1）；心跳或清單不可用 → `peer-unavailable`（舊 rc 2）；有查不到的台且沒有回答 → `lookup-incomplete`（舊 rc 3）；清單格式不對 → `peer-bad-answer`（舊的約定外答案）。

### Requirement: 解析 `pool-peer find` 的陣列
**Reason**: 沒有 JSON 陣列可解析；埠清單是每行一個十進位埠的文字檔。

**Migration**: 清單的格式驗證（UTF-8、空行、十進位、1–65535、去重）見「讀 sidecar-link 的心跳」與「讀 sidecar-link 的埠清單」；「每一筆合格的項目」換成「每一個有回答的埠」，見「每一個埠怎麼查」與「每一個埠怎麼戳」。

### Requirement: 讀 `pool-peer call` 的結束碼
**Reason**: 沒有 `call` 就沒有結束碼；戳的回應現在是直接的 HTTP 回應（MAS-1）。

**Migration**: 舊的語意由「跨機查詢與戳的連線結果」承接：連線被拒 → `peer-unavailable`（舊 rc 2）；逾時 → `poke-timeout`（舊 rc 3 與子行程逾時）；HTTP 層讀不懂 → `unknown peer-bad-answer`（舊的「第一行不是狀態碼」與約定外的 rc）；200／409／其他碼則照「戳的回應怎麼判讀（本機與跨機共用）」。

### Requirement: `find` 找到的每一台怎麼戳
**Reason**: 這個 requirement 的前提（`find` 回報的每一台都帶著 `agent.state`）隨 `find` 消失；每一個埠要先自己查一次才知道有沒有這個 id（MAS-1）。

**Migration**: 內容由「每一個埠怎麼查」與「每一個埠怎麼戳」承接——狀態判斷、`state-<state>`、埠格式、多台都戳、每一台各自決定 `interrupt`、共用的 `via`／`ref` 都在那裡；埠的合法範圍從 5200–5399 改成 1–65535（埠由清單決定）。
