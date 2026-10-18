# angareion Specification

## Purpose
TBD - created by archiving change angareion-v0. Update Purpose after archive.
## Requirements
### Requirement: 確認有沒有新訊息
`angareion wait` MUST 能不阻塞（`--timeout 0`：查一次就回）、限時阻塞（`--timeout N`）或一直阻塞（不給逾時）。有新訊息時 exit 0；逾時沒有新訊息時 exit 3。等待期間 MUST NOT 改動游標。**不給 `--timeout` 的預設仍然是一直播阻塞**（MyAiStorage#96 重新評估後維持：保留 ETag 之後閒置重啟的成本接近 0，限時是呼叫方／harness 的事）。

`wait` 的後端輪詢 MUST 在 `wait` 之間記住每個實體上一次「完整查過而且沒有新訊息」的條件式查詢狀態：當時實際查詢的 URL（含 `since`）、當時的游標、回應的 ETag，存在本機狀態檔（與游標檔分開；見「目標與游標」）。重新開始 `wait` 時，只要游標與收件模式（整個 channel／寄給我的）相同就 MUST 送條件式查詢（`If-None-Match`）；記錄的 URL 只是「那次查的是哪個視窗」，MUST NOT 要求與這次的 URL 相同——304 代表後端說「現在這份表示與記錄的那份相同」，而那份已經完整掃過且對同一個游標沒有新訊息。若記錄的視窗就是游標的視窗，`wait` MUST 優先查那個視窗（不採用更窄的掃描視窗），避免掃描視窗漂移讓每次重啟都多一次完整查詢。只有 304 才算「沒有新訊息」（304 不計 GitHub 額度）。

下列情況 MUST 用完整查詢（不送 `If-None-Match`）：狀態檔不存在、不是合法 JSON、版本不認得、記錄缺欄位或與現在的 URL／游標／收件模式不符、上一次回應有下一頁、上一次回應的第 1 頁已滿（回傳筆數等於 `per_page`，可能還有下一頁）、或該實體沒有可用的 ETag。查到新訊息時 MUST 清掉該筆記錄（`wait` exit 0 之後還沒 `read` 的訊息，下一次 `wait` 仍要叫得到）。304 以外、可以記錄的回應 MUST 用新的 ETag 覆寫該筆（ETag 失效時照常查到）。狀態檔壞掉或寫不進去 MUST NOT 讓 `wait` 失敗或漏訊息——最壞情況是下一次用完整查詢；寫不進去時 MUST 在 stderr 印一次提示（同一行程最多一次），stdout 與 exit code MUST NOT 因此改變。

#### Scenario: 不阻塞
- **WHEN** 沒有新訊息時執行 `angareion wait --timeout 0`
- **THEN** 立刻 exit 3

#### Scenario: 阻塞到有新訊息
- **WHEN** B 執行 `angareion wait --timeout 600`，之後 A 送一則給 B
- **THEN** B 的 `wait` 在那則送出後不久 exit 0

#### Scenario: 不給逾時就一直等
- **WHEN** B 執行 `angareion wait`（不給 `--timeout`），沒有新訊息
- **THEN** 一直阻塞，直到 A 送了一則給 B 才 exit 0

#### Scenario: wait 不動游標
- **WHEN** B 的 `wait` 因為有新訊息而 exit 0，B 隨後執行 `angareion read`
- **THEN** `read` 讀到那則訊息（`wait` 沒有把它標成已讀）

#### Scenario: 重啟 wait 不吃額度
- **WHEN** B 的 `wait --timeout 60` 逾時後，再 `wait --timeout 60`（其間沒有任何新訊息），連續多次
- **THEN** 第一次對每個 channel 的查詢是完整請求（建立記錄）；之後每次重啟的第一次查詢都帶 `If-None-Match`、後端回 304，計額度的完整請求接近 0

#### Scenario: 掃描視窗漂移後重啟
- **WHEN** 一則亂序的訊息（較小的 id、較晚的後端時刻）把行程內的掃描視窗推到游標視窗之後，B 的 `wait` 重啟 6 次（每次 run 兩次查詢）
- **THEN** 只有第一次 run 的第一次查詢是完整請求；其餘都是 304（修前每次 run 是 2 次完整請求）

#### Scenario: ETag 失效時照常查到
- **WHEN** 記錄的查詢被後端回 200（回應變了），而且裡面有寄給 B 的新訊息
- **THEN** `wait` exit 0；該筆記錄被清掉（不留下會把同一則訊息蓋成「沒有新訊息」的 ETag）

#### Scenario: 沒讀走的新訊息不會被 304 蓋掉
- **WHEN** `wait` 曾因有新訊息 exit 0，B 還沒 `read`，B 重新 `wait`
- **THEN** 那則訊息仍然叫得到（沒有可用的記錄時用完整查詢），不會被 304 當成沒有新訊息

#### Scenario: 狀態檔壞掉或不存在
- **WHEN** 狀態檔不存在、不是 JSON、版本不認得，或記錄與現在的游標／收件模式不符
- **THEN** 用完整查詢（不送 `If-None-Match`）；新訊息不會因此漏掉；壞掉的檔案在下次寫入時被改寫成合法內容

#### Scenario: 狀態檔寫不進去
- **WHEN** 狀態目錄是唯讀的（或磁碟寫入失敗），B 執行 `angareion wait`
- **THEN** stderr 印一次提示（同一行程不重複），`wait` 照常回 exit 0／3（不是 2），訊息不漏

#### Scenario: 多頁或第 1 頁已滿時不用 ETag
- **WHEN** 回應有下一頁，或第 1 頁回傳的筆數已滿（`per_page`）
- **THEN** 該實體不留 ETag；下一次用完整查詢（第 2 頁以後的新訊息不會因為第 1 頁不變的 304 被跳過）

#### Scenario: 游標前進之後
- **WHEN** B 用 `read` 把游標往前推，之後再 `wait`
- **THEN** 記錄因游標不同而不被採用：該 channel 用完整查詢，查過沒有新訊息後才重新記住；`wait` 仍不改動游標

#### Scenario: 兩個行程同時 wait
- **WHEN** 同一個身分、同一個狀態目錄的兩個 `wait` 同時在跑
- **THEN** 狀態檔不會被寫壞（整檔原子替換）；最壞情況是少記一筆、下一次用完整查詢，不會出現壞檔被當成 304 或漏訊息

### Requirement: 目標與游標
`wait` 與 `read` 的目標是某個 channel（`--channel`）或「寄給我的」（不給 `--channel`：所有 channel 裡 `to` 是自己 agent 名、或沒有 `to` 的訊息）。判斷訊息新不新 MUST 只看後端（到達位置與後端記錄的時刻），MUST NOT 使用寄件者填的 `at` 或 `id`。`read` 不給 `--since` 時 MUST 從本機游標之後讀起並把游標往前推；給了 `--since`（後端時刻）就回傳不早於它的訊息（含邊界），MUST NOT 改動本機游標。`read` 的每則輸出 MUST 帶後端記錄的時刻 `received_at`。游標刪掉時 MUST 從頭讀起。

`read`／`wait` 給了 `--channel` 時 MUST 在 stderr 印一行警告，說明這會讀到 channel 裡所有訊息（包含寄給別人的訊息）；不給 `--channel`（「寄給我的」，含廣播）時 MUST NOT 印。警告 MUST NOT 改變 exit code 或 stdout。

`wait` 的條件式查詢狀態（見「確認有沒有新訊息」）MUST NOT 寫進游標檔；`read` MUST NOT 讀它或受它影響。它是本機快取：刪掉 MUST 只造成下一次完整查詢，MUST NOT 讓訊息漏掉。

#### Scenario: 游標刪掉
- **WHEN** 刪掉本機游標後執行 `angareion read`
- **THEN** 讀到目標裡所有的訊息

#### Scenario: 寄給我的
- **WHEN** 兩個 channel 裡各有一則 `to` 是我的訊息、一則沒有 `to` 的訊息，另外有一則 `to` 是別人的；不給 `--channel` 執行 `angareion read`
- **THEN** 讀到 `to` 是我的那兩則與沒有 `to` 的那一則，不含別人的

#### Scenario: 同一秒、後到但 ULID 較小
- **WHEN** B 讀過 `at` 是 03:00:00、ULID 較大的一則之後，另一個寄件者的 `at` 同樣是 03:00:00、ULID 較小的一則才到，B 再執行 `angareion read`
- **THEN** 讀到後到的那一則，先前讀過的那則不會重複；`angareion wait --timeout 0` 在讀之前是 exit 0

#### Scenario: 寄件者的時鐘慢
- **WHEN** B 的游標在後端時刻 03:00:10 的那一則，之後到達的一則 `at` 填的是 03:00:08（寄件者的時鐘慢）
- **THEN** B 讀到它

#### Scenario: at 的時區不同
- **WHEN** B 已讀過 `at` 為 `…T17:00:00+09:00` 的一則，之後到達的一則 `at` 為 `…T09:00:00+00:00`（實際上晚一小時、字串比較較小）
- **THEN** B 讀到它

#### Scenario: 被編輯的留言
- **WHEN** 已讀過的一則留言在後端被編輯（後端的更新時間變新）
- **THEN** 下一次 `read` 不會把它當新訊息再回傳

#### Scenario: since 含邊界
- **WHEN** 兩則訊息的後端時刻都是 03:00:00，`angareion read --since <03:00:00 的時刻>`
- **THEN** 兩則都回傳（含邊界）；本機游標沒有被改動；每則都帶 `received_at` 與 `id`，呼叫方用 `id` 去重

#### Scenario: read 輸出後端時刻
- **WHEN** `angareion read --json`
- **THEN** 每則訊息都有 `received_at`，它是後端記錄的時刻，不是寄件者的 `at`

#### Scenario: 收訊加了 --channel
- **WHEN** B 執行 `angareion read --channel general`（或 `angareion wait --channel general`）
- **THEN** stderr 有一行警告說明這會包含寄給別人的訊息；stdout 與 exit code 與不加 `--channel` 時相同

#### Scenario: 寄給我的不加 --channel
- **WHEN** B 執行 `angareion read`（或 `angareion wait`），不給 `--channel`
- **THEN** 沒有那行警告；讀到的是寄給 B 的訊息（含廣播）

#### Scenario: 刪掉游標後 wait 仍完整查詢
- **WHEN** 刪掉游標檔之後 B 執行 `angareion wait`
- **THEN** 不採用先前的 ETag 記錄（游標不同），用完整查詢；有新訊息時 exit 0

### Requirement: 訊息格式
一則訊息 MUST 有 `from`、`to`、`channel`、`urgency`（0～9 的整數，預設 5）、`content`、`attachments`，加上寄件者產生的 `id`（ULID）與 `at`（含時區）；`id` 與 `at` 只用來辨識與顯示，MUST NOT 用來判斷新舊。`from`、`to` 與自己的身分 MUST 是 agent 名：驗證用的正規式是 `^[A-Za-z0-9][A-Za-z0-9._-]*-(pm|arch|impl|test)(-[A-Za-z0-9_.]+)?$`（repo 段含 `-`；複審 M1）；**驗證與解析是兩步**——regex 只驗整體形狀，另做由右而左的解析（先取可省的 `-<id>`、再取最右邊的角色字，左邊剩下的都是 repo 名；例 `MyLinuxPool-impl-2`、`LLMGateway-pm`、`my-repo-impl-2` → repo `my-repo`、角色 `impl`、id `2`；同角色只有一個時省略 id；角色字只有小寫的 `pm|arch|impl|test`）；`to` 選填（不給＝發到整個 channel）。`--json` 輸出的 `from`／`to` 也是這個字串。不做嚴格的身分認證（任何人可以用任何名字送）。送出時內文 MUST 來自 `--body <文字>` 或 `--body -`（stdin），沒給 `--body` MUST exit 1。

#### Scenario: urgency 超出範圍
- **WHEN** `angareion send --channel X --urgency 12 --body hi`
- **THEN** exit 1，什麼都沒送

#### Scenario: 群組位址
- **WHEN** `angareion send --channel X --to 'group=全員' --body hi`
- **THEN** exit 1（新模型裡沒有群組位址，`group=` 不是合法的 agent 名），什麼都沒送

#### Scenario: 位址不是 agent 名
- **WHEN** `angareion send --channel X --to 'repo=owner/team,role=pm' --body hi`
- **THEN** exit 1（位址格式錯誤），什麼都沒送

#### Scenario: 位址文法（接受）
- **WHEN** `--to` 依序給 `LLMGateway-pm`、`MyLinuxPool-impl-2`、`my-repo-impl-2`、`repo_1.2-arch`
- **THEN** 都接受（照常送出；stdout 印訊息 id）；`my-repo-impl-2` 由右而左解析成 repo `my-repo`、角色 `impl`、id `2`

#### Scenario: 位址文法（拒絕）
- **WHEN** `--to` 給 `myrepo-dev`（角色不在清單）、`MyRepo-PM`（角色字大小寫有別）、`-pm`（repo 是空的）、`foo-impl-`（id 段空）、`foo-impl-2!`（非法字元）
- **THEN** 都 exit 1（格式錯誤），什麼都沒送

#### Scenario: 沒給 body
- **WHEN** `angareion send --channel X --to <agent 名>`，沒有 `--body`，stdin 是開著的管線
- **THEN** exit 1，stderr 說明要用 `--body <文字>` 或 `--body -`；不讀 stdin，什麼都沒送

#### Scenario: 明寫才讀 stdin
- **WHEN** `echo 'hi' | angareion send --channel X --to <agent 名> --body -`
- **THEN** 送出的內文是 `hi`，stdout 印訊息 id

#### Scenario: 不存在的 channel
- **WHEN** 對 channel 設定檔裡沒有的 channel 做 `send`、`read`、`wait` 或 `channel delete`
- **THEN** exit 1，stderr 說明 channel 不存在；什麼都沒送、什麼都沒改

### Requirement: token 的位置與讀法
token 檔的位置 MUST 能由 `ANGAREION_TOKEN_FILE` 指定（優先於其他來源）；沒有設定時用設定檔的 `token_file` 或預設位置（實作細節，見 design）。檔案 MUST 只有擁有者有權限（0400 或 0600），否則 MUST 拒絕（exit 2）。token MUST NOT 出現在任何輸出、log、錯誤訊息或訊息內容裡。token 的權限範圍見 design §10（定案；複審 M2）：各 backend 的 repo 與 `MyAiStorage` 的 `Issues: Read and write`（backend repo＝讀 issue 與留言；MyAiStorage 只用來在註冊簿 issue 留言）；MyAiStorage 的 clone／fetch 走匿名 HTTPS、不用 token；workflow 推送 main 用的是 repo 的 `GITHUB_TOKEN`。檔案的位置與權限規則不變。

#### Scenario: 權限太寬
- **WHEN** token 檔的權限是 0644
- **THEN** 指令 exit 2，說明要改成只有擁有者能讀；輸出裡沒有 token

#### Scenario: 唯讀掛載
- **WHEN** token 檔是 0400（worker 上的唯讀掛載）
- **THEN** 正常讀取

### Requirement: 輸出與結束碼
stdout MUST 只放結果（`channel create`／`channel delete` 印 channel 名，`send` 印訊息 id，`read` 印訊息或 `--json`），說明與錯誤放 stderr。結束碼：0 成功；1 用法或輸入錯誤；2 後端、網路或設定錯誤；3 `wait` 逾時沒有新訊息。

#### Scenario: 設定缺少
- **WHEN** 沒有設定檔（或缺 channel／backend 設定檔）就執行 `angareion send`
- **THEN** exit 2，stderr 說明缺什麼

#### Scenario: 後端錯誤
- **WHEN** 後端回 HTTP 錯誤（例如 403 次級限制或網路中斷）
- **THEN** exit 2，stderr 說明狀態碼，不自動重試；輸出裡沒有 token

#### Scenario: HTTP 逾時
- **WHEN** 後端請求超過逾時（固定 30 秒；`wait` 另受剩餘時間限制）沒有回應
- **THEN** exit 2，stderr 說明逾時，不自動重試；`wait --timeout N` 不會因為一次卡住的請求而遠超過 N 秒

#### Scenario: 非 JSON 的回應
- **WHEN** 後端回 HTTP 200，但本文不是 JSON（例如 proxy 的登入頁）
- **THEN** exit 2（後端錯誤），不是 exit 1；輸出裡沒有 token

#### Scenario: 送註冊失敗
- **WHEN** `channel create X` 的第 2 步（送註冊）失敗後重試 2 次（共 3 次嘗試）都失敗
- **THEN** exit 2，stderr 說明 channel 沒有建立成功；後端可能留著沒有對應的實體，但介面不提它

### Requirement: 給 agent 的說明
repo MUST 附一份給 agent 讀的說明（`skills/angareion/SKILL.md`）：五個指令的用法（含 `channel create`／`channel delete` 與落地有時間差）、身分、閒著時用 `angareion wait` 阻塞地等、收到高 urgency 訊息的處理。說明 MUST NOT 含任何秘密或私人資訊。

#### Scenario: agent 照說明收信
- **WHEN** 一個讀了說明的 agent 手上沒事
- **THEN** 它執行 `angareion wait --timeout <秒>`，而不是反覆呼叫模型查信

### Requirement: 五個介面
Angareion 對外 MUST 只有五個介面：建 channel、刪 channel、送訊息、確認有沒有新訊息、讀訊息；指令分別是 `angareion channel create`、`angareion channel delete`、`angareion send`、`angareion wait`、`angareion read`。MUST NOT 有 ack、已讀回條或限流。channel 名 MUST 是英文名（非空的 ASCII 字母、數字、`-`、`_`）；介面（參數、stdout、stderr、錯誤訊息）MUST NOT 出現 GitHub repo／issue 的概念。第一期 channel 對所有隊員公開，沒有成員設定：`channel create` MUST NOT 收 `--member`。`send` 的 `--channel` 必填、`--to` 選填（不給＝發到整個 channel）。`channel create` 與 `channel delete` 的 stdout MUST 只印 channel 名。`channel create` MUST 在送出註冊留言前先確認後端實體（存在而且標題等於 channel 名；不符 MUST exit 2、什麼都沒註冊——複審 H2），留言後 MUST 輪詢 fetch 到對應出現為止（上限 2 分鐘；逾時 MUST 在 stderr 印提示、仍 exit 0）；`channel delete` MUST NOT 等落地（送出註銷留言成功即 exit 0）。（註冊簿與重建見 `angareion/channel-config` 與 design §9／§10。）

#### Scenario: 送出並讀到
- **WHEN** A 對 channel X 送一則 `to` 是 B 的訊息，B 執行 `angareion read`
- **THEN** B 讀到那則訊息；再執行一次 `angareion read` 讀不到同一則

#### Scenario: 不給 to 就是發到整個 channel
- **WHEN** A 對 channel X 送一則不帶 `to` 的訊息
- **THEN** 送出成功（stdout 印訊息 id）；X 的任何成員 `read --channel X` 都讀得到它

#### Scenario: 建 channel
- **WHEN** `angareion channel create X`（不帶 `--member`；後端與本體都可用）
- **THEN** 後端出現 X 的實體、註冊簿上出現 X 的 `register` 留言；stdout 只印 `X`，exit 0；沒有給名稱時 exit 1

#### Scenario: 刪 channel
- **WHEN** `angareion channel delete X`（X 在設定檔裡有對應）
- **THEN** 後端實體被關掉（不刪除）、註冊簿上出現 X 的 `unregister` 留言；stdout 只印 `X`，exit 0；X 不在設定檔裡時 exit 1

#### Scenario: 建 channel 不再收成員
- **WHEN** `angareion channel create X --member <位址>`
- **THEN** exit 1（未知參數）；什麼都沒建

#### Scenario: 建 channel 名已存在
- **WHEN** channel 設定檔已經有 `X`，執行 `angareion channel create X`
- **THEN** exit 1，stderr 說明 channel 已存在；什麼都沒建

#### Scenario: 建 channel 等落地
- **WHEN** `angareion channel create X` 的第 2 步送出成功，而本機複本還沒有 `X`
- **THEN** 指令輪詢 fetch 到 `X` 出現才回：stdout 只印 `X`、exit 0；2 分鐘還沒出現→stderr 印提示、仍 exit 0

#### Scenario: 建 channel 的後端開實體失敗
- **WHEN** 第 1 步（在 backend 的 repo 開實體）失敗（網路、403、5xx）
- **THEN** exit 2，什麼都沒註冊；介面不提後端細節

#### Scenario: 建 channel 的用戶端驗證（複審 H2）
- **WHEN** 第 1 步開好實體後，用戶端 GET 到它的標題不是 channel 名（或查不到那個 issue）
- **THEN** exit 2，不留註冊留言、什麼都沒註冊

#### Scenario: 刪 channel 的關實體失敗
- **WHEN** `angareion channel delete X` 的第 2 步（關實體）失敗（不是 404）
- **THEN** exit 2；沒有送註銷（對應仍在）

#### Scenario: 刪 channel 的 404 視同已關
- **WHEN** `channel delete X` 的實體已經被刪（後端回 404）
- **THEN** 視同已關、照送註銷；stdout 印 `X`、exit 0

#### Scenario: 刪 channel 的註銷送出失敗
- **WHEN** `channel delete X` 的第 3 步（送註銷）失敗
- **THEN** exit 2；實體已關、對應還在；重跑安全（再關一次是 404 視同已關）

#### Scenario: 多 backend 沒給 --backend
- **WHEN** `backends.json` 有兩個 backend，`angareion channel create X` 沒給 `--backend`
- **THEN** exit 1，什麼都沒建

#### Scenario: backend 名不存在
- **WHEN** `angareion channel create X --backend nosuch`
- **THEN** exit 1，什麼都沒建

#### Scenario: 送註冊失敗後重試成功
- **WHEN** 第 2 步的留言送出第一次失敗、重試（短間隔）成功
- **THEN** `channel create` 照常完成：stdout 印 `X`、exit 0

#### Scenario: channel 名不是英文名
- **WHEN** `angareion channel create 小明` 或 `angareion send --channel 小明 …`
- **THEN** exit 1；什麼都沒建、沒送

### Requirement: 後端可以替換（設定檔指位）
五個介面 MUST 經由一個後端介面實作，後端也是那幾個動作（建 channel＝開實體、刪 channel＝關實體、送、確認有沒有新訊息、讀）；對外的 `wait` 是在後端「確認有沒有新訊息」上輪詢（不阻塞＝確認一次）。這次的實作是 GitHub API＋Issues：channel 名經 channel 設定檔指到某個 backend 的某個實體（issue；訊息＝留言）。解析 MUST 只認設定檔，MUST NOT 用標題掃描挑實體——同名標題不影響操作。後端 MUST NOT 存成員（第一期 channel 全公開）。換後端時，指令與訊息格式 MUST NOT 改變。

#### Scenario: 用假後端
- **WHEN** 測試以記憶體裡的假後端執行五個指令
- **THEN** 行為和用 GitHub 後端時相同，不連網

#### Scenario: 多頁時第 1 頁不變
- **WHEN** 「寄給我的」的留言列表超過一頁，第 1 頁（都是寄給別人的）內容和上一次一樣，第 2 頁多了一則寄給我的；`wait` 先前已經查過一次
- **THEN** `angareion wait --timeout 0` exit 0（不因第 1 頁的 304 而回報沒有新訊息）

#### Scenario: 同名標題不影響對應
- **WHEN** 後端裡有兩個標題相同的實體（編號 3 與編號 7），channel 設定檔把名字指到編號 7
- **THEN** 訊息送到編號 7，不會因為「編號 3 比較舊」改送編號 3

