# comm-skill Specification

## Purpose
AI 之間溝通的指令 `aicomm`：`send` 寫進 Angareion，收件人若是 pool 裡（本機 sidecar，查不到時經 `pool-peer` 找別台）閒置或執行中的 agent 就順便戳一下，結果寫成 stderr 的 `wake:` 行；`inbox` 讀寄給自己的信。叫醒的判斷只放在這一層，Angareion、sidecar 與 `pool-peer` 各自只守自己的介面；`wake:` 行附 sidecar 紀錄 id，便於對照（設計見 `openspec/changes/archive/2026-10-25-comm-skill/` 與 `openspec/changes/archive/2026-11-04-aicomm-cross-machine-wake/`，MyAiStorage#212、#368）。

## Requirements

### Requirement: send 先寫進 Angareion，成功才算送達
`send` MUST 以 Angareion 的 `send` 指令寫入訊息，並把 Angareion 印出的訊息 id 原樣印在 stdout 的第一行。Angareion 寫入失敗時 `send` MUST 以該失敗的結束碼結束，且 MUST NOT 嘗試叫醒任何人、MUST NOT 呼叫 `pool-peer`。Angareion 寫入成功後，叫醒失敗 MUST NOT 改變結束碼（仍為 0）；帶 `--require-wake` 時最多回 4，也仍代表訊息已送達（見「`send` 的結束碼與 `--require-wake`」）。叫醒的結果 MUST 以固定格式的 `wake:` 行呈現在 stderr（見「`wake:` 行的文法」）。

#### Scenario: 寫入成功、叫醒失敗
- **WHEN** Angareion 寫入成功，但本機 sidecar 連不上、也沒有可用的 `pool-peer`
- **THEN** exit 0，stdout 第一行是訊息 id，stderr 是 `wake: <agent> not-woken peer-unavailable`（本機與跨機都試過）

#### Scenario: 寫入失敗
- **WHEN** Angareion 回 exit 1（例如 channel 不存在）
- **THEN** `send` 以 exit 1 結束，沒有任何對 sidecar 的請求，也沒有呼叫 `pool-peer`

### Requirement: 工單號選填
`send` MUST 能不帶工單號送出一般訊息。給了 `--ticket <repo>#<n>`（格式 `^[A-Za-z0-9._-]+#[0-9]+$`，不符 exit 1）時，skill MUST 以 `--ref ticket=<repo>#<n>` 交給 Angareion，MUST NOT 改動訊息內文，MUST NOT 去讀工單。

#### Scenario: 不帶工單號
- **WHEN** `aicomm send --channel c --to a --body 'hi'`
- **THEN** 訊息送出，沒有 `ticket` 附件

#### Scenario: 帶工單號
- **WHEN** 加上 `--ticket MyAiStorage#212`
- **THEN** 訊息帶一個名為 `ticket`、值為 `MyAiStorage#212` 的 ref 附件

### Requirement: 其他參照 `--ref` 原樣轉給 Angareion
`send` MUST 提供可重複的 `--ref <name>=<locator>`，每個值 MUST 原樣轉成一個 `--ref=<name>=<locator>` token 交給 Angareion，MUST NOT 改動訊息內文，MUST NOT 去讀被指到的東西。每個值都是一個 `--ref=...` token，值以 `-` 開頭 MUST NOT 被當成旗標。值裡沒有 `=` 時 MUST 以 exit 1 結束，且 MUST 在寫進 Angareion 之前就拒絕：MUST NOT 送出訊息、MUST NOT 對 sidecar 發任何請求。與 `--ticket` 並用時 MUST 都送：`ticket` 的 ref 在前，其餘 `--ref` 照給的順序。叫醒 MUST 仍只看 `--to`、`--mention`、`--no-wake` 與 `--interrupt`——`aicomm send --ref` 照樣叫醒收件人，與 `angareion send --ref` 不叫醒不同（Angareion 那一層不碰 sidecar）。

#### Scenario: 送出帶 ref 且照樣叫醒
- **WHEN** `aicomm send --channel c --to a --ref pr=https://github.com/x/y/pull/1 --body 'hi'`，且 `a` 在本機 sidecar 上 `idle`
- **THEN** 對 Angareion 只多一個 `--ref=pr=https://github.com/x/y/pull/1` token，訊息照常送出，`a` 照 `--to` 的規則被叫醒（exit 0）

#### Scenario: 缺 `=` 被拒不送不 poke
- **WHEN** `aicomm send --channel c --to a --ref pr --body 'hi'`
- **THEN** exit 1，沒有呼叫 Angareion 的 `send`，也沒有對 sidecar 發任何請求（查 agent 與戳都沒有），stderr 說明值要是 `<name>=<locator>`

#### Scenario: 與 --ticket 並用時 ticket 的 ref 在前
- **WHEN** `aicomm send --channel c --to a --ticket MyAiStorage#278 --ref pr=https://github.com/x/y/pull/2 --ref design=openspec/specs/comm-skill/spec.md --body 'hi'`
- **THEN** 交給 Angareion 的三個 ref token 依序是 `--ref=ticket=MyAiStorage#278`、`--ref=pr=https://github.com/x/y/pull/2`、`--ref=design=openspec/specs/comm-skill/spec.md`

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

跨機叫醒用的執行檔另由環境變數 `AICOMM_POOL_PEER` 指定（只給測試與手動用；sidecar 開的 agent 的環境是白名單，拿不到這個變數）；沒設就從 PATH 找 `pool-peer`，PATH 沒有再找 `~/.mylinuxpool/bin/pool-peer`（見「找 `pool-peer` 執行檔」）。這個變數只決定呼叫哪一個執行檔與 argv，MUST NOT 被當成 sidecar 位址，也 MUST NOT 被當成使用者可設定的叫醒位址（跨機能打哪些 port 由 `pool-peer` 與埠段決定）。

#### Scenario: 預設位址
- **WHEN** 沒有覆寫
- **THEN** 對預設的 loopback 位址查詢與戳，不讀任何設定檔；跨機的執行檔從 PATH 找 `pool-peer`，PATH 沒有再找 `~/.mylinuxpool/bin/pool-peer`

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
skill MUST NOT 修改 Angareion 的協定或程式；它對 Angareion 只用其公開的指令列介面。對 sidecar，skill MUST 只**使用**「查單一 agent」（`GET /agents/<id>`）與「戳單一 agent」（`POST /agents/<id>/poke`）兩種請求；skill MAY 讀戳回應中的選用欄位 `poke_id`／`poke_outcome`（MyLinuxPool#786）；欄位缺少時行為 MUST 與舊版相同。skill MUST NOT 另外對 sidecar 發出查紀錄的請求（例如 `GET /pokes/{id}`）。skill 的說明檔與輸出 MUST NOT 含憑證、機器位址或私人資訊。

#### Scenario: 對 sidecar 的請求種類
- **WHEN** 檢查 skill 對 sidecar 發出的所有請求
- **THEN** 只有「查單一 agent」與「戳單一 agent」兩種

#### Scenario: 不發查紀錄的請求
- **WHEN** `send` 的叫醒結果帶 `record=`，檢查 aicomm 期間對 sidecar 與 `pool-peer` 的所有請求
- **THEN** 對 sidecar 只有 `GET /agents/<id>` 與 `POST /agents/<id>/poke`，沒有 `GET /pokes/…`；對 `pool-peer` 只有 `find` 與 `call … POST /agents/<id>/poke`；poke 的 body 仍只有 `text` 與 `interrupt`；子指令仍只有 `send`、`inbox`、`log`

### Requirement: 對外只有 send、inbox、log 三個指令
skill MUST 對外只提供 `send`、`inbox` 與 `log` 三個子指令（另有 `--version`／`--help`）。skill MUST NOT 提供 channel 建刪、開關 agent、單獨戳 agent、狀態查詢或 ack 等指令。

#### Scenario: 不認得的子指令
- **WHEN** 呼叫 `aicomm poke` 或 `aicomm status`
- **THEN** exit 1，stderr 說明只有 `send`、`inbox` 與 `log`

### Requirement: log 薄包 angareion log，原樣轉發、不叫醒
`aicomm log` MUST 以 Angareion 的 `log` 指令查詢：

- 除 `--to`／`--mentioned` 的注入規則外，MUST NOT 自行解析或驗證 `log` 的旗標——`log` 之後的引數原樣交給 `angareion log`（subprocess），stdout／stderr 原樣轉發，結束碼跟著 Angareion（成功 0、用法錯 1、後端或設定錯 2）；錯誤由 Angareion 報，skill 不重複實作旗標語意。
- **預設 `--to me`**：使用者沒有給 `--to` 也沒有給 `--mentioned` 時，MUST 自動補 `--to me`；使用者給了任一個，MUST NOT 注入（以使用者的為準）。「使用者給了」的判斷 MUST 認得等價寫法，並 MUST **只看旗標位置的引數**、MUST NOT 把旗標的**值**當成旗標（中-7）：對 `log` 之後**以 `--` 開頭**的每個引數，取 `=` 之前的字面、去掉 `--`，字面**非空**且恰是 `to` 或 `mentioned` 的**前綴**，才算使用者已指定收件人——涵蓋 `--to <值>`、`--to=<值>`（`=` 之後是值、不看）、`--mentioned`，與 argparse 會接受的未歧義縮寫（`--m`、`--men`）。不是以 `--` 開頭的引數是前一個旗標的值，MUST NOT 參與判斷：`--from me`、`--grep to` MUST 照常注入 `--to me`（值撞名造成的漏注入，是靜默違反本條的 MUST）；`--` 單獨一個（選項結束寫法）去 `--` 後字面為空，MUST NOT 算數。argparse 的**歧義**縮寫（如 `--t`——與 `--thread` 撞；`--t` 本身不是宣告的旗標、只是縮寫）照同一條字面前綴規則處理（`t` 是 `to` 的前綴 → 不注入），原樣轉發後由 Angareion 報歧義 exit 1。注入 MUST 放在使用者引數**之前**（argparse 同名旗標重複時「後給的贏」，放前面保證使用者值即使漏判也贖得回）。補的是字面 `me`，skill MUST NOT 自己解析身分（skill 不讀 Angareion 的設定檔；`me` 由 angareion 解析為「寄給我的」＝`to` 是我、或廣播、或 `mentions` 有我）。
- **不叫醒**：`log` MUST NOT 對 sidecar 發出任何請求（查 agent 或戳都不發）；叫醒是 `send` 的副作用。
- `log` MUST NOT 提供 `--wait`／`--interrupt`／`--no-wake`（沒有等待與叫醒語意）；引數裡出現 Angareion 不認得的旗標時照 Angareion 的回應（exit 1）轉發。
- `--json` MUST 原樣轉發 Angareion 的 JSON 輸出。
- `log` 是 inbox 之後的補查工具：skill 的說明 MUST 寫明「被叫醒先 `aicomm inbox`；要前後文或補讀用 `aicomm log`，不要用 `angareion read --since` 反覆撈」與「`log` 不動游標」。

#### Scenario: 預設補 --to me
- **WHEN** `aicomm log --since 2026-10-25T00:00:00Z`
- **THEN** 對 angareion 實際送出 `log --to me --since 2026-10-25T00:00:00Z`（注入在使用者引數**之前**）；輸出與結束碼原樣

#### Scenario: 旗標的值不是旗標——--from me 照常注入
- **WHEN** `aicomm log --from me`
- **THEN** 對 angareion 送出 `log --to me --from me`（值的 `me` 不參與「使用者已指定收件人」的判斷；`--from` 不是 `to`／`mentioned` 的前綴——照常補 `--to me`）

#### Scenario: 使用者給了 --to 就不注入
- **WHEN** `aicomm log --to mypmo-pm-m1 --grep blocker`
- **THEN** 對 angareion 送出 `log --to mypmo-pm-m1 --grep blocker`（原樣，沒有補 `--to me`）

#### Scenario: 等號的 --to=<值> 也不注入（不會被靜默蓋掉）
- **WHEN** `aicomm log --to=mypmo-pm-m1 --grep blocker`
- **THEN** 對 angareion 送出 `log --to=mypmo-pm-m1 --grep blocker`（原樣，沒有補 `--to me`——argparse 合法的等號寫法不會被掃描漏掉）

#### Scenario: 縮寫的 --mentioned 也不注入
- **WHEN** `aicomm log --m --grep blocker`（argparse 的未歧義縮寫＝`--mentioned`）
- **THEN** 不注入；對 angareion 轉出 `log --m --grep blocker`（原樣），`--to me` 沒有悄悄與它並存

#### Scenario: 使用者給了 --mentioned 就不注入
- **WHEN** `aicomm log --mentioned --channel team-alpha`
- **THEN** 對 angareion 送出 `log --mentioned --channel team-alpha`（原樣，沒有補 `--to me`）

#### Scenario: 值撞名也照常注入——--grep to
- **WHEN** `aicomm log --grep to`
- **THEN** 對 angareion 送出 `log --to me --grep to`（搜尋字串 `to` 是 `--grep` 的值，不是旗標；照常補 `--to me`）

#### Scenario: 原樣轉發結束碼與輸出
- **WHEN** Angareion 對 `log` 回 exit 1（例如旗標用法錯）或 exit 2（後端錯）
- **THEN** `aicomm log` 以同一結束碼結束，stdout／stderr 內容與 Angareion 相同（aicomm 不加提示，除了原本就可能有的 sidecar 導向輸出不會出現在 log）

#### Scenario: log 不碰 sidecar
- **WHEN** 對假 sidecar 檢查 `aicomm log` 期間的所有請求
- **THEN** 沒有任何請求（GET／POST 都沒有），無論收件人身分為何

#### Scenario: SKILL.md 補 log 的分工句
- **WHEN** 檢查 `skills/aicomm/SKILL.md`
- **THEN** 有「被叫醒先 inbox；要前後文或補讀用 log」與「不要用 `read --since` 反覆撈」的明文，並把 `log` 列進指令清單（三個指令）

### Requirement: 跨機叫醒只經 `pool-peer` 的兩個用法
本機查不到人時，skill MUST 只用 MyLinuxPool 的 `pool-peer` 找別台並戳；MUST NOT 對 pool 做其他請求（MUST NOT 用 `pool-peer list`），MUST NOT 自己連 Gateway 或任何非 loopback 位址。呼叫一律用 argv、不經 shell。找 MUST 是 `find <agent-id> --json --timeout 10`；戳 MUST 是 `call <sidecar_port> POST /agents/<agent-id>/poke --timeout 5`，body 走 stdin（帶 `Content-Type: application/json`），`<agent-id>` MUST 先做 URL quote。aicomm MUST 只打兩個路由：`GET /agents/<id>`（經 `find`）與 `POST /agents/<id>/poke`。

#### Scenario: 只打兩個路由
- **WHEN** 本機回 404，跨機找到並戳了一台
- **THEN** 對 `pool-peer` 只有 `find` 與 `call … POST /agents/<id>/poke` 兩種 argv，沒有 `list`；poke 的 body 只有 `text` 與 `interrupt`，不含寄件者、訊息 id 或內文

#### Scenario: 找到一台並戳成功
- **WHEN** 本機回 404，`find` 回一台 `idle`（port 5201），`call` 回第一行 200
- **THEN** stderr 是 `wake: <agent> woken peer:5201`；`call` 的 argv 是 `call 5201 POST /agents/<quoted-id>/poke --timeout 5`，stdin 是 `{"text": <固定短句>, "interrupt": false}`；exit 0

### Requirement: 找 `pool-peer` 執行檔
設了環境變數 `AICOMM_POOL_PEER` 時 MUST 用它，沒設 MUST 改依序找 PATH 上的 `pool-peer` 與 `~/.mylinuxpool/bin/pool-peer`（先 PATH，再 bindir；bindir 那個必須存在且可執行才算，取家目錄失敗或 bindir 不可讀視為沒有）。都沒有或不能執行 MUST 是 `not-woken peer-unavailable`，不算錯（不影響結束碼，`--require-wake` 除外）。

#### Scenario: 沒有 `pool-peer`
- **WHEN** 本機回 404，`AICOMM_POOL_PEER` 指向不存在的檔，PATH 上也沒有 `pool-peer`
- **THEN** stderr 是 `wake: <agent> not-woken peer-unavailable`；exit 0

#### Scenario: PATH 沒有、bindir 有且可執行
- **WHEN** 本機回 404，`AICOMM_POOL_PEER` 沒設，PATH 上沒有 `pool-peer`，但 `~/.mylinuxpool/bin/pool-peer` 存在且可執行
- **THEN** 用 `~/.mylinuxpool/bin/pool-peer` 當 `pool-peer` 執行檔

#### Scenario: PATH 與 bindir 都有
- **WHEN** 本機回 404，`AICOMM_POOL_PEER` 沒設，PATH 上與 `~/.mylinuxpool/bin/pool-peer` 都有可執行的 `pool-peer`
- **THEN** 用 PATH 上那一個當 `pool-peer` 執行檔

#### Scenario: bindir 檔存在但不可執行
- **WHEN** 本機回 404，`AICOMM_POOL_PEER` 沒設，PATH 上沒有 `pool-peer`，`~/.mylinuxpool/bin/pool-peer` 存在但不可執行
- **THEN** 視為沒有，stderr 是 `wake: <agent> not-woken peer-unavailable`；exit 0

#### Scenario: 取家目錄失敗
- **WHEN** 本機回 404，`AICOMM_POOL_PEER` 沒設，PATH 上沒有 `pool-peer`，取家目錄失敗（無法判斷 `~/.mylinuxpool/bin`）
- **THEN** 視為沒有，stderr 是 `wake: <agent> not-woken peer-unavailable`（不是 internal-error）；exit 0

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
- **THEN** 5201 的 stdin 是 `{"text": <固定短句>, "interrupt": true}`，5202 的是 `{"text": <固定短句>, "interrupt": false}`；stderr 是 `wake: <agent> woken peer:5201,5202 interrupt`

#### Scenario: 被打斷的那台沒成功就不加 interrupt
- **WHEN** 同上，但 5201 的 `call` 回 500、5202 回 200
- **THEN** stderr 是 `wake: <agent> woken peer:5202`（沒有 ` interrupt`）

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

### Requirement: 叫醒的原因碼清單
原因碼 MUST 只有這些（新增要改 `design.md` 或後續 change，並同步 cli 的常數與文件）：`not-woken` 是 `not-found`、`peer-unavailable`、`lookup-incomplete`、`peer-port-out-of-range`、`peer-bad-answer`、`bad-answer`、`bad-name`、`state-<state>`、`agent-not-running`、`awaiting-answer`、`http-<碼>`；`unknown` 是 `poke-timeout`、`poke-error`、`peer-bad-answer`、`internal-error`。`unknown` MUST 只用於「可能已經戳到」的情況：看到它 MUST NOT 重送。

#### Scenario: 原因碼與常數一致
- **WHEN** 檢查 `src/aicomm/cli.py` 的原因碼常數
- **THEN** `not-woken` 與 `unknown` 兩組恰好是上面的清單（`state-<state>`、`http-<碼>` 以前綴表示），沒有多也沒有少

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
