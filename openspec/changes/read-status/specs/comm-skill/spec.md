## RENAMED Requirements

- FROM: `### Requirement: 對外只有 send、inbox、log 三個指令`
- TO: `### Requirement: 對外只有 send、inbox、log、wake 四個指令`

## MODIFIED Requirements

### Requirement: 對外只有 send、inbox、log、wake 四個指令
skill MUST 對外只提供 `send`、`inbox`、`log` 與 `wake` 四個子指令（另有 `--version`／`--help`）。skill MUST NOT 提供 channel 建刪、開關 agent、狀態查詢或 ack 等指令。單獨戳 agent MUST 只由 `wake` 做（語意見「wake 只戳不送」）；除此之外 MUST NOT 有其他單獨戳人的途徑。

#### Scenario: 不認得的子指令
- **WHEN** 呼叫 `aicomm status`、`aicomm sent`，或 `aicomm poke`
- **THEN** exit 1，stderr 說明只有 `send`、`inbox`、`log` 與 `wake`

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
- **THEN** 有「被叫醒先 inbox；要前後文或補讀用 log」與「不要用 `read --since` 反覆撈」的明文，並把 `log` 列進指令清單（四個指令，含 `wake`）

## ADDED Requirements

### Requirement: wake 只戳不送
`aicomm wake` MUST 只對收件人發出叫醒（戳），MUST NOT 寫入任何訊息。指令形式 `aicomm wake --to <AI 名> [--mention <AI 名>]... [--interrupt]`。

- `--to` 與 `--mention` 至少要有一個候選（扣掉自己之後）；沒有候選 MUST exit 1。自己（`ANGAREION_IDENTITY`）MUST NOT 被戳。
- `wake` MUST NOT 呼叫 Angareion（不開子行程、不讀游標、不寫任何東西）；MUST NOT 接受 `--body`、`--channel`、`--in-reply-to`、`--ticket`、`--ref`，帶了 exit 1。
- 叫醒條件 MUST 與 `send` 相同：只有本機 sidecar 上狀態為 `idle` 或 `running` 的收件人才戳；不在本機（查詢回 404）、`starting`、`waiting-quota`、`exited` 都 MUST NOT 戳。
- `--interrupt` MUST 只在收件人狀態為 `running` 時送出打斷；狀態為 `idle` 時 MUST 送不打斷的戳。
- 戳的內容 MUST 固定（沿用 comm-skill「戳的內容固定且不含訊息」的條款）；`wake` MUST NOT 把任何訊息內文放進戳。
- 覆寫的 sidecar 位址 MUST 是 loopback，否則 MUST exit 1，且 MUST 在發出任何請求之前拒絕（同 `send` 的需要叫醒時的檢查）。
- **stderr（固定格式）**：每個候選一行，格式為 `wake <AI 名>: <結果>`，結果只能是下列之一：`poked`（不打斷的戳已送出）、`poked interrupt`（打斷的戳已送出，只在收件人為 `running` 時）、`skipped not-local`（sidecar 查詢回 404）、`skipped state=<狀態字面>`（狀態不是 `idle`／`running`）、`failed <原因字面>`（sidecar 連線錯、非 200、非 JSON）。stderr MUST NOT 含「已送達」字樣（`wake` 沒有送達這回事）。
- **exit（待拍板 D-wake-exit；本段寫的是 PM 建議的選項 B，見 design D5.3）**：0 表示至少戳到一個收件人（至少一行是 `poked` 或 `poked interrupt`）；3 表示有候選但沒有任何一個被戳到，且沒有請求失敗；2 表示沒有任何戳成功、而且至少一次請求失敗；1 表示用法錯（含沒有候選、覆寫為非 loopback、寫入旗標）。判定優先序：有戳成功 → 0；否則有請求失敗 → 2；否則 → 3。
- **exit 3 的意義只屬於 `wake`**：`aicomm inbox --wait` 逾時也是 3，兩者同數字、不同指令；文件 MUST 分指令寫明各自的 3，腳本 MUST 依指令判讀，不得跨指令共用。

#### Scenario: 只戳閒著的 agent，不寫 Angareion
- **WHEN** `aicomm wake --to <X>`，X 在本機 sidecar 上是 `idle`
- **THEN** 對 X 發一次不打斷的戳（`interrupt` 為 false）；stderr 一行 `wake <X>: poked`；exit 0；對假的 `angareion` 檢查：沒有任何子行程被呼叫、假後端沒有任何寫入

#### Scenario: 打斷只對 running 生效
- **WHEN** `aicomm wake --to <X> --interrupt`，X 是 `running`
- **THEN** 戳送出 `interrupt` 為 true，stderr 一行 `wake <X>: poked interrupt`；X 是 `idle` 時同一指令送出的戳 `interrupt` 為 false，stderr 一行 `wake <X>: poked`

#### Scenario: 不戳不在本機或不可叫醒的 agent
- **WHEN** `aicomm wake --to <X>`，X 在 sidecar 上查回 404，或狀態是 `exited`、`starting`、`waiting-quota`
- **THEN** 不發任何戳；stderr 一行 `wake <X>: skipped not-local` 或 `wake <X>: skipped state=<狀態>`；exit 3

#### Scenario: 部分成功是 exit 0
- **WHEN** `aicomm wake --to <A> --mention <B>`，A 是 `idle`（被戳），B 查回 404
- **THEN** stderr 兩行（`wake <A>: poked`、`wake <B>: skipped not-local`）；exit 0

#### Scenario: sidecar 連線失敗是 exit 2
- **WHEN** `aicomm wake --to <X>`，sidecar 連線失敗（沒有任何戳成功）
- **THEN** stderr 一行 `wake <X>: failed <原因>`；exit 2

#### Scenario: 沒有候選是用法錯
- **WHEN** `aicomm wake`（沒有 `--to`、沒有 `--mention`），或只有自己的名字
- **THEN** exit 1，stdout 沒有內容，沒有任何請求送給 sidecar

#### Scenario: 不接受寫入旗標
- **WHEN** `aicomm wake --to <X> --body hi`
- **THEN** exit 1（不認得的旗標；由 aicomm 的解析擋下）；不呼叫 Angareion，不對 sidecar 發任何請求

#### Scenario: 覆寫為非 loopback 位址
- **WHEN** 覆寫值是非 loopback 主機，執行 `aicomm wake --to <X>`
- **THEN** exit 1，在發出任何請求之前拒絕

### Requirement: 送出的三種模式寫進文件
`docs/aicomm.md` 與 `skills/aicomm/SKILL.md` MUST 有一節「送出的三種模式」，描述預設（不中斷）、`--interrupt`（中斷）、`--no-wake`（不叫醒）。文件 MUST 寫明：

- 沒給 `--to`／`--mention`（廣播）時誰都不叫醒；叫醒只對本機 sidecar 上狀態為 `idle`、`running` 的收件人。
- `--interrupt` 套用到 `--mention` 點名的每一位被叫醒者，並且只在對方 `running` 時才打斷；`--interrupt` 與 `--no-wake` 不能同時給。
- `--no-wake` 只寫入 Angareion、誰都不戳；對方要自己執行 `aicomm inbox` 才看得到。
- `exit 0` 只代表訊息已寫入；叫醒結果看 stderr。
- 排到對方這一輪結束之後才叫醒，是 sidecar 的行為，文件 MUST NOT 寫成 aicomm 的保證。
- `--interrupt` 只用在急件，且只有 PM、PMO 自己決定（軟規則）。

#### Scenario: 文件有三種模式
- **WHEN** 檢查 `docs/aicomm.md` 與 `skills/aicomm/SKILL.md`
- **THEN** 兩份都有「送出的三種模式」的字樣，並各自涵蓋上列的每一條；`--no-wake` 與 `--interrupt` 互斥的說明在兩份都有

#### Scenario: 文字與程式對得上
- **WHEN** 比對文件的說法與 `src/aicomm/cli.py` 的 `--to`、`--mention`、`--interrupt`、`--no-wake` 的 help 與叫醒條件
- **THEN** 廣播不叫醒、狀態條件（`idle`／`running`）、`--interrupt` 只在 `running` 生效 三者都對得上
