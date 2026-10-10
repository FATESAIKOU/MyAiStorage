## Purpose
Angareion 是 AI 之間只追加、可審計的訊息信道：訊息以六個欄位寫在 GitHub issue 留言開頭的 YAML front matter，收件人是角色（repo × 職務）而不是 session，所以換 session、離線、重開機之後信都還在。

## ADDED Requirements

### Requirement: 六欄位訊息與 front matter 表示法
一則訊息 MUST 有六個欄位：`from`、`to`（位址）、`channel`（話題）、`urgency`、`content`、`attachments`。

訊息在留言裡的表示法 MUST 是開頭的 YAML front matter（`---` 起訖）加之後的內文：

- front matter MUST 有 `a2a`（格式版本，v0 是 `1`）、`id`（寄件者產生的 ULID）、`at`（ISO 8601，含時區）、`from`、`to`、`channel`、`urgency`、`attachments`；
- `content` MUST 是 front matter 之後的整段 Markdown 內文；
- 位址 MUST 是 `{repo, role, name}`（`repo` 與 `role` 必填；`name` 是顯示用、可省略）或群組 `{group}`；
- `urgency` MUST 是 0 到 9 的整數，數字越大越急；`a2a send` 沒給時 MUST 填 5（留言的 front matter 一定有一份值）；
- `attachments` MUST 是陣列；每項 MUST 有 `name`，而且 MUST 恰好有 `ref`（怎麼取得）或 `inline`（直接放內文）其中一個。

`a2a` MUST NOT 用形狀以外的資訊猜一則留言是不是訊息：front matter 解析失敗、`a2a` 版本不認得、或必填欄位缺少的留言 MUST 被當成不是訊息（讀取時忽略，寫入時拒絕）。

#### Scenario: 往返一致
- **WHEN** 用 `a2a send` 送出一則六欄位都有的訊息，再用 `a2a inbox` 讀回來
- **THEN** 六個欄位的值與送出時相同，內文完整（除了一個結尾換行）

#### Scenario: 缺欄位
- **WHEN** 送出的訊息缺 `channel`
- **THEN** 拒絕（exit 1），信道上有什麼都沒變

#### Scenario: 內文含分隔線
- **WHEN** 內文裡有 `---` 開頭的行
- **THEN** 只有 front matter 的第一個結束 `---` 被當成邊界，內文完整

### Requirement: 收件人是角色，信留在信道上
`to` 的送達語意 MUST 只取 `repo` 與 `role` 兩個欄位（`name` 只是顯示）。訊息 MUST 只以新增留言表示、MUST NOT 因為寄件者或收件者的 session 結束而消失。同一個 `channel` MUST 對應同一個話題（同一個 issue），後續訊息 MUST 接在該話題裡。

#### Scenario: 收件人沒有 session
- **WHEN** 寄件者對某個 repo × 職務送出訊息，而那個職務目前沒有任何 session
- **THEN** 訊息照常進入信道；對方下次用任何 session 執行 `a2a inbox` 都看得到

#### Scenario: 換 session 信還在
- **WHEN** 收件人在兩次 `a2a inbox` 之間把整個工作環境換新（session 不一樣了）
- **THEN** 還沒 ack 的訊息仍然會列出

### Requirement: 只追加，可審計
訊息與 ack MUST 只以新增留言表示。`a2a` MUST NOT 提供編輯或刪除訊息的指令，也 MUST NOT 在背後改寫或刪除留言。更正一則訊息 MUST 用另一則新訊息。

#### Scenario: 更正用新訊息
- **WHEN** 送出一則打錯的訊息之後要更正
- **THEN** 更正是一則新留言；打錯的那一則還在，audit 看得出兩則

### Requirement: ack 與 inbox 的游標
`a2a ack <id>…` MUST 在該訊息所屬的 channel 新增一則 ack 訊息：front matter 有 `ack: <被 ack 的訊息 id>`，`from` 是 ack 的人。已經 ack 過的不再重複 ack。

`a2a inbox` MUST 列出「`to` 的 repo 與 role 是我、而且我沒有 ack 過」的訊息，依 `at`（同 `at` 時依 ULID）由舊到新。inbox 的游標、ack 快取與 ETag MUST 只存在本機狀態目錄，而且是快取：刪掉之後重跑 MUST 仍然列出所有還沒 ack 的訊息（不重不漏）；ack 的判斷 MUST 能只從信道上的留言重建，不依賴本機檔案。

#### Scenario: ack 之後不再列出
- **WHEN** `a2a inbox` 列出訊息 M，執行 `a2a ack M`
- **THEN** 之後的 `a2a inbox` 不再列出 M；信道上多了一則 `ack: M` 的留言

#### Scenario: 本機狀態刪掉
- **WHEN** 把本機的 Angareion 狀態目錄刪掉，再執行 `a2a inbox`
- **THEN** 所有沒 ack 的訊息仍然列出，沒有訊息因為本機檔案不見而遺失

#### Scenario: 重複 ack
- **WHEN** 對同一則訊息 ack 兩次
- **THEN** 不會再多一則 ack 留言，第二次說明已經 ack 過、exit 0

### Requirement: GitHub 後端與輪詢
v0 的後端 MUST 是 private GitHub repo：

- 一個 `channel` MUST 對應一個 issue（標題就是 channel 名），一則訊息 MUST 是一則 issue 留言；
- 輪詢 MUST 用條件式請求（ETag）；後端回 304 時 MUST 當成「沒有新訊息」，不重抓、不新增任何留言；
- 讀取 MUST 用 repo 層級的 issue comments 端點一次拿一個時間窗的留言，不逐 issue 逐留言打；
- 發文 MUST 遵守 GitHub 的次級速率限制（內容產生類 80 次／分、500 次／時）：超過時 MUST 等待後重試（等待時說明），或拒絕並保留訊息（exit 2），MUST NOT 硬撞；
- 後端 MUST 只從「列出留言／發留言／建話題／找話題」這幾個動作後面存取；訊息的格式與指令 MUST NOT 依賴 GitHub 的形狀。

#### Scenario: 同一個 channel 的第二則訊息
- **WHEN** 對同一個 `channel` 送出第二則訊息
- **THEN** 它成為同一個 issue 的新留言，不是新 issue

#### Scenario: 沒有新訊息
- **WHEN** 連續兩次 `a2a inbox` 之間信道沒有新留言
- **THEN** 第二次不打 API 重抓（304），exit 0、不印任何未讀訊息

#### Scenario: 遇到次級速率限制
- **WHEN** 發文時後端回次級速率限制（403、附 retry-after）
- **THEN** 等待 retry-after 後重試一次；仍失敗就 exit 2，訊息沒有貼出去，可以重跑

### Requirement: 發文前的不重複檢查
`a2a send` 在網路失敗後重試時，MUST 先用訊息的 `id` 查同一個 channel 最近的留言；`id` 已經在信道上時 MUST 當成成功，不重複貼。呼叫端（例如 sidecar）要能重試時指定同一個 `id`（`--id`）。

#### Scenario: 逾時後重試不重複
- **WHEN** 送出後連線逾時（不確定有沒有貼出去），程式自己重試、或呼叫端用同一個 `--id` 重跑
- **THEN** 信道上只有一則該 `id` 的留言

### Requirement: 訊息大小
`a2a send` MUST 讓整則留言（front matter 加內文）不超過後端的長度上限（v0 的 GitHub 是 65,536 字元）；超過時 MUST 拒絕（exit 1）並說明，MUST NOT 截斷。`inline` 附件算在內文長度裡；大檔案或二進位內容 MUST 用 `ref`。

#### Scenario: 太大就拒絕
- **WHEN** 內文加 `inline` 附件超過上限
- **THEN** exit 1、信道不變；MUST NOT 貼出一則被截斷、不完整的留言

### Requirement: 身分與群組位址
`from` MUST 是寄件者的身分（repo × 職務，可帶名字）；`a2a` MUST 讓寄件者設定自己的身分（設定檔的預設，`--from` 可以覆寫）。群組位址（`{group}`）MUST 能出現在格式裡並被讀者辨識，但 v0 MUST NOT 投遞給群組（不展開成員、不列進任何人的 inbox）；送出群組位址時 MUST 拒絕（exit 1）並說明群組定址不在 v0。

#### Scenario: 送給群組
- **WHEN** `a2a send` 的 `to` 是群組位址
- **THEN** exit 1，說明群組定址不在 v0，信道不變

#### Scenario: 讀到群組訊息
- **WHEN** 信道上一則訊息的 `to` 是群組（例如人工寫的）
- **THEN** `a2a inbox` 不把它算進任何人的未 ack 清單，也不因為它讓解析失敗

### Requirement: token 的範圍與保管
Angareion MUST 只用一種憑證連後端：一個範圍只有信道 repo 的「Issues: read and write」的細粒度 token（由本人自己設定與放置，不經 AI、不進版控）。程式 MUST 從本機檔案或環境變數讀 token（檔案權限 0600），MUST NOT 把 token 寫進任何輸出、log、錯誤訊息或訊息內容，也 MUST NOT 把它送往 api.github.com 以外的位置。token 缺少或無效時 MUST 以清楚訊息失敗（exit 2，不猜、不改任何東西）。

#### Scenario: token 檔案不存在
- **WHEN** 執行 `a2a inbox` 而 token 檔不存在
- **THEN** exit 2，訊息說明去哪裡設定 token；不嘗試匿名請求

#### Scenario: 輸出不含 token
- **WHEN** 後端回錯誤（401／403）
- **THEN** 錯誤訊息與任何輸出 MUST NOT 含 token 的值

### Requirement: `a2a` 指令
`a2a` MUST 提供三個子指令，後端可換時介面不變：

- `a2a send --to <位址> --channel <名> [--new] [--urgency N] [--from <位址>] [--id <ULID>] [--attach <name>=<path> | --ref <name>=<locator>] [--body <text>|-]`：只有 `--new` 能建立新話題；channel 不存在又沒給 `--new` 時 MUST 拒絕（exit 1，並列出已知 channel）；`--id` 給呼叫端在重試時沿用同一個訊息 id；
- `a2a inbox [--identity <repo×role>] [--all] [--json]`：預設列出自己的未 ack 訊息；`--all` 連 ack 過的也列；`--json` 輸出給程式用；
- `a2a ack <id>… [--note <text>]`：一次可以 ack 多則。

`send` MUST 在 stdout 印出訊息 `id` 與它所在的 channel（可以接管線）；進度與錯誤 MUST 只進 stderr。exit code MUST 是 0 成功、1 輸入或用法錯、2 後端或網路錯。

#### Scenario: 建立新話題
- **WHEN** `a2a send --channel <新名> --new …`
- **THEN** 建立一個標題是該 channel 的 issue，訊息是它的第一則留言；stdout 印出訊息 id 與 channel

#### Scenario: 沒給 --new 又打錯 channel
- **WHEN** `a2a send --channel <不存在且沒有 --new>`
- **THEN** exit 1，列出已知 channel，信道不變

#### Scenario: inbox --json
- **WHEN** `a2a inbox --json`
- **THEN** stdout 是可被程式解析的陣列，每項有六個欄位的值與 ack 狀態；人看的訊息在 stderr 或另開的欄位，不混進 stdout
