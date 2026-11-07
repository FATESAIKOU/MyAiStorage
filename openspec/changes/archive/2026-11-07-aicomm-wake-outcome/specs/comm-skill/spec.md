## MODIFIED Requirements

### Requirement: 不改 Angareion 與 sidecar，不攜帶私人資訊
skill MUST NOT 修改 Angareion 的協定或程式；它對 Angareion 只用其公開的指令列介面。對 sidecar，skill MUST 只**使用**「查單一 agent」（`GET /agents/<id>`）與「戳單一 agent」（`POST /agents/<id>/poke`）兩種請求；skill MAY 讀戳回應中的選用欄位 `poke_id`／`poke_outcome`（MyLinuxPool#786）；欄位缺少時行為 MUST 與舊版相同。skill MUST NOT 另外對 sidecar 發出查紀錄的請求（例如 `GET /pokes/{id}`）。skill 的說明檔與輸出 MUST NOT 含憑證、機器位址或私人資訊。

#### Scenario: 對 sidecar 的請求種類
- **WHEN** 檢查 skill 對 sidecar 發出的所有請求
- **THEN** 只有「查單一 agent」與「戳單一 agent」兩種

#### Scenario: 不發查紀錄的請求
- **WHEN** `send` 的叫醒結果帶 `record=`，檢查 aicomm 期間對 sidecar 與 `pool-peer` 的所有請求
- **THEN** 對 sidecar 只有 `GET /agents/<id>` 與 `POST /agents/<id>/poke`，沒有 `GET /pokes/…`；對 `pool-peer` 只有 `find` 與 `call … POST /agents/<id>/poke`；poke 的 body 仍只有 `text` 與 `interrupt`；子指令仍只有 `send`、`inbox`、`log`

### Requirement: 戳的回應怎麼判讀（本機與跨機共用）
本機 sidecar 對戳的 HTTP 回應，與 `pool-peer call` rc 0 時 stdout 第一行的狀態碼及其後的 body，MUST 用同一張表判讀：200 MUST 是這台叫醒。409 時 MUST 依序找 body 的巢狀錯誤碼（`error` 是 JSON 物件且 `error.code` 是字串時）、頂層的 `code`、頂層字串的 `error`，**第一個是字串的就定案**；定案的值不認得，或三處都不是字串時，MUST 是 `not-woken http-409`，MUST NOT 再往下找。`agent_not_running` MUST 是 `not-woken agent-not-running`，`agent_awaiting_answer` MUST 是 `not-woken awaiting-answer`，其他、沒有或 body 看不懂 MUST 是 `not-woken http-409`。其他任何碼（含 200 以外的 2xx）MUST 是 `not-woken http-<碼>`。本機的 409 MUST 讀出錯誤回應的 body 再判斷。MUST NOT 重送。

判讀的同時，skill MAY 讀 body 頂層的兩個選用欄位（MyLinuxPool#786，poke-records-v1 已談定）：`poke_id` MUST 只有在是字串且完全符合 `^[A-Za-z0-9_-]{1,64}$` 時才採用，否則 MUST 視為沒有；`poke_outcome` MUST 只有值是 `"queued"` 才算「對方忙（排隊）」，其他值（含錯誤回應的 `refused`／`failed`）、型別不對或沒有這個欄位 MUST 視為沒有。**任何 HTTP 碼**的回應，只要 `poke_id` 合格就 MUST 採用；200 另外讀 `poke_outcome`。這兩個欄位 MUST NOT 改變上面判讀出來的種類與原因碼，MUST NOT 改變戳的次數，也 MUST NOT 讓 skill 另外發出請求。`interrupt` 為真時 MUST NOT 印 ` queued`（見「`wake:` 行的文法」）。

#### Scenario: 本機戳回 409 與其他碼
- **WHEN** 本機 `GET` 回 200 `idle`，戳回 409 且 body 是 `{"code": "agent_awaiting_answer"}`／`{"error": "agent_not_running"}`，或戳回 503
- **THEN** 分別是 `wake: <agent> not-woken awaiting-answer`、`not-woken agent-not-running`、`not-woken http-503`；只戳一次，沒有呼叫 `pool-peer`

#### Scenario: `call` 回 409 與其他碼
- **WHEN** `call` 回 409 且 body 的 `error` 是 `agent_awaiting_answer`／`agent_not_running`，或回 500
- **THEN** 分別是 `wake: <agent> not-woken awaiting-answer`、`not-woken agent-not-running`、`not-woken http-500`

#### Scenario: 409 的 body 看不懂或沒有錯誤碼
- **WHEN** 戳回 409，但 body 不是 JSON 物件，或 `error.code`／`code`／字串 `error` 都不是字串
- **THEN** stderr 是 `wake: <agent> not-woken http-409`；只戳一次，沒有重送

#### Scenario: 409 的巢狀錯誤碼
- **WHEN** 本機或 `call` 的戳回 409，body 是 `{"error": {"code": "agent_awaiting_answer", "message": "…"}}`、`{"error": {"code": "agent_not_running"}}` 或 `{"error": {"code": "poke_timeout"}}`
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
- **WHEN** `find` 回 5201 `running`；`call` 的 rc 0，stdout 是 `200` 後接 `{"poke_id":"<ID>","poke_outcome":"queued"}`
- **THEN** stderr 是 `wake: <agent> woken peer:5201 queued record=peer:5201/<ID>`；沒帶與帶 `--require-wake` 都是 exit 0

#### Scenario: 404 與 500 的紀錄 id
- **WHEN** 戳回 404 或 500，錯誤 body 帶合格的 `poke_id`
- **THEN** 原本的那一行尾端加上 ` record=local/<ID>`（例如 `wake: <agent> not-woken http-404 record=local/<ID>`、`wake: <agent> not-woken http-500 record=local/<ID>`）；只戳一次，沒有重送

### Requirement: `wake:` 行的文法
叫醒的結果 MUST 在 stderr 以固定格式呈現，每一個收件人剛好一行，本機與跨機共用同一個格式：

    wake: <agent> woken local[ interrupt| queued][ record=<records>]
    wake: <agent> woken peer:<port>[,<port>…][ interrupt| queued][ record=<records>]
    wake: <agent> not-woken <原因碼>[ record=<records>]
    wake: <agent> unknown <原因碼>[ record=<records>]

    <records> := <rec>[,<rec>…]
    <rec>     := local/<id> | peer:<port>/<id>
    <id>      := [A-Za-z0-9_-]{1,64}

每一行之後 MUST NOT 有其他文字。` interrupt` 後綴 MUST 只在至少有一台**成功（200）**的戳帶 `interrupt: true` 時加上。` interrupt` 與 ` queued` MUST 互斥；sidecar 同時回報 interrupt 與排隊時，MUST 以 ` interrupt` 為準。` queued` MUST 只在收件人的每一次成功（200）的戳都回報排隊時才加上（見「同一個收件人戳了好幾台時取一個結果」）。` record=` MUST 只在至少有一個合格的 sidecar 紀錄 id 時才寫；位置 MUST 是 `local`（本機）或 `peer:<port>`（跨機），多筆依位置以逗號串接（port 由小到大）；沒有 id 時整段 MUST NOT 寫，該行的其餘部分 MUST 與舊版一字不差。id 與排隊的來源、採用條件見「戳的回應怎麼判讀（本機與跨機共用）」。` record=` 的用途是對照 sidecar 的紀錄：本機用 `GET /pokes/<id>`（文件寫 `curl -s 127.0.0.1:8471/pokes/<id>`），跨機用 `pool-peer call <port> GET /pokes/<id>`；這兩條查法是文件給人的用法，不是 skill 自己發的請求。`queued` MUST NOT 被當成原因碼。

#### Scenario: 每個收件人剛好一行
- **WHEN** `send` 帶多個收件人（`--to` 加 `--mention`），叫醒結果各不相同
- **THEN** stderr 每個收件人恰一行，且每一行以 `re.fullmatch` 符合 `^wake: \S+ (woken (local|peer:\d+(,\d+)*)( interrupt| queued)?|(not-woken|unknown) [A-Za-z0-9_-]+)( record=(local|peer:\d+)/[A-Za-z0-9_-]{1,64}(,(local|peer:\d+)/[A-Za-z0-9_-]{1,64})*)?$`；以下反例 MUST NOT 符合：`wake: a woken local interrupt queued`、`wake: a woken local queued interrupt record=local/x`、`wake: a woken local bogus`、`wake: a woken local record=x/y`、`wake: a woken local record=local/a b`、`wake: a woken local record=local/` 加 65 個字元

#### Scenario: 沒有戳就沒有 record
- **WHEN** 叫醒結果是 `state-exited`、`not-found`、`bad-name`、`peer-unavailable`、`lookup-incomplete` 或 `peer-port-out-of-range`（沒有戳任何人）
- **THEN** 沒有 `record=`；該行與現在一字不差

#### Scenario: 戳的傳輸失敗沒有 record
- **WHEN** 戳逾時、連線錯誤，或 `call` 回 rc 2、rc 3、約定外的 rc（回應沒有可用 body）
- **THEN** 沒有 `record=`；結果是 `unknown poke-timeout`、`unknown poke-error` 或 `not-woken peer-unavailable` 等，與現在一字不差

### Requirement: 同一個收件人戳了好幾台時取一個結果
同一個收件人有好幾台的結果時，MUST 照這個順序取一個：只要有一台 200 就是 `woken`（列出成功的那幾個 port，由小到大）；否則只要有一台 `unknown`，就是 `unknown`；都不是就是 `not-woken`。`unknown` 或 `not-woken` 有好幾台時，原因碼 MUST 取 port 由小到大的第一個。`record=` MUST 列出每一台有拿到合格 `poke_id` 的戳（含失敗的那幾台），依 port 由小到大。` queued` MUST 只在成功（200）的每一台都回報排隊時才加；只要有一台是當下送出，MUST NOT 加 ` queued`。

#### Scenario: 多台取一個結果
- **WHEN** 同一個收件人在兩台都可戳，一台 200、一台 `call` 逾時；成功的那台沒有回 id（舊版 sidecar）
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

### Requirement: `send` 的結束碼與 `--require-wake`
`send` 的結束碼 MUST 不因叫醒失敗而改變（仍為 0）。選用旗標 `--require-wake` 給了時：只要有任何一位是 `not-woken` 或 `unknown`，MUST 以 **4** 結束（訊息已送達，不要重送）；全部叫醒（`woken`，**含 `woken … queued`**），或沒有人要叫醒（廣播、只寄給自己），MUST 以 0 結束。`--require-wake` 與 `--no-wake` 同時給 MUST 是 exit 1，且 MUST 在寫進 Angareion 之前就拒絕（MUST NOT 送出訊息、MUST NOT 對 sidecar 或 `pool-peer` 發任何請求）。

`docs/aicomm.md` 與 `skills/aicomm/SKILL.md` MUST 寫明「exit 0 只代表已送達；看 stderr 的 `wake:` 行；exit 4 不要重送」，`woken … queued` 的意思是對方忙（排隊）、不要重送，`record=` 的意思與兩條查法（本機 `GET /pokes/<id>`，跨機 `pool-peer call <port> GET /pokes/<id>`），以及「回報一律帶 `--to`」。原因碼的清單 MUST 與 cli 的常數一致，`queued` MUST NOT 列為原因碼。

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

#### Scenario: 排隊算叫醒
- **WHEN** `send --require-wake`，所有收件人都是 `woken … queued`
- **THEN** exit 0（排隊不需要重送）

#### Scenario: 文件寫明排隊與紀錄 id 的用法
- **WHEN** 檢查 `docs/aicomm.md` 與 `skills/aicomm/SKILL.md`
- **THEN** 兩者都含 `woken local queued`、`record=`、「回報一律帶 `--to`」、`GET /pokes/`（本機與跨機兩條查法），以及「`queued` 不要重送」；`queued` 沒有被列為原因碼
