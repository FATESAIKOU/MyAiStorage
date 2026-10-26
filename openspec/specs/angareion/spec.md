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
stdout MUST 只放結果（`channel create`／`channel delete` 印 channel 名，`send` 印訊息 id，`read` 印訊息或 `--json`，`log` 印訊息或 `--json`（空結果也是成功）），說明與錯誤放 stderr。結束碼：0 成功；1 用法或輸入錯誤；2 後端、網路或設定錯誤；3 `wait` 逾時沒有新訊息。

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
repo MUST 附一份給 agent 讀的說明（`skills/angareion/SKILL.md`）：六個指令的用法（含 `channel create`／`channel delete` 與落地有時間差、`angareion log` 的查詢用途與「不動游標」）、身分、閒著時用 `angareion wait` 阻塞地等、收到高 urgency 訊息的處理。說明 MUST NOT 含任何秘密或私人資訊。

#### Scenario: agent 照說明收信
- **WHEN** 一個讀了說明的 agent 手上沒事
- **THEN** 它執行 `angareion wait --timeout <秒>`，而不是反覆呼叫模型查信

#### Scenario: agent 要回頭找信會用 log
- **WHEN** 一個讀了說明的 agent 要找「昨天某決策的串」或「某則訊息的前後文」
- **THEN** 它用 `angareion log` 的過濾與錨點查詢，而不是用 `read --since` 反覆撈；說明寫明 log 不動游標

### Requirement: 後端可以替換（設定檔指位）
六個介面 MUST 經由一個後端介面實作，後端的動作是：建 channel＝開實體、刪 channel＝關實體、送、確認有沒有新訊息、讀，以及一個唯讀的「列舉」（列出實體上的留言與其到達位置、後端時刻，並標出留言是否是 angareion 訊息——非訊息的留言也要列得出來；`log` 與它的本機快取使用這個動作）。列舉帶 `since` 時 MUST 按留言的**最後編輯時刻**過濾（與 GitHub 列表 API 的語意一致：被編輯過的舊留言 MUST 因編輯重新出現在結果裡；增量同步的「同 id 內容變就覆蓋」仰賴這條）——假後端 MUST 照守，MUST NOT 以「照 `created_at` 過濾或直接忽略 `since`」實作它。對外的 `wait` 是在後端「確認有沒有新訊息」上輪詢（不阻塞＝確認一次）。這次的實作是 GitHub API＋Issues：channel 名經 channel 設定檔指到某個 backend 的某個實體（issue；訊息＝留言）。解析 MUST 只認設定檔，MUST NOT 用標題掃描挑實體——同名標題不影響操作。後端 MUST NOT 存成員（第一期 channel 全公開）。換後端時，指令與訊息格式 MUST NOT 改變。

#### Scenario: 用假後端
- **WHEN** 測試以記憶體裡的假後端執行六個指令
- **THEN** 行為和用 GitHub 後端時相同，不連網

#### Scenario: 多頁時第 1 頁不變
- **WHEN** 「寄給我的」的留言列表超過一頁，第 1 頁（都是寄給別人的）內容和上一次一樣，第 2 頁多了一則寄給我的；`wait` 先前已經查過一次
- **THEN** `angareion wait --timeout 0` exit 0（不因第 1 頁的 304 而回報沒有新訊息）

#### Scenario: 同名標題不影響對應
- **WHEN** 後端裡有兩個標題相同的實體（編號 3 與編號 7），channel 設定檔把名字指到編號 7
- **THEN** 訊息送到編號 7，不會因為「編號 3 比較舊」改送編號 3

#### Scenario: 列舉的行為與讀共用同一個後端實作
- **WHEN** 假後端上對同一實體做「列舉」與「讀」
- **THEN** 同一批留言的到達位置與後端時刻一致；非 angareion 訊息的留言在「列舉」看得到（標示為非訊息），在「讀」（`read` 的輸出）則不會出現

### Requirement: 六個介面
Angareion 對外 MUST 只有六個介面：建 channel、刪 channel、送訊息、確認有沒有新訊息、讀訊息、查詢訊息（唯讀）；指令分別是 `angareion channel create`、`angareion channel delete`、`angareion send`、`angareion wait`、`angareion read`、`angareion log`。MUST NOT 有 ack、已讀回條或限流。channel 名 MUST 是英文名（非空的 ASCII 字母、數字、`-`、`_`）；介面（參數、stdout、stderr、錯誤訊息）MUST NOT 出現 GitHub repo／issue 的概念。第一期 channel 對所有隊員公開，沒有成員設定：`channel create` MUST NOT 收 `--member`。`send` 的 `--channel` 必填、`--to` 選填（不給＝發到整個 channel）。`channel create` 與 `channel delete` 的 stdout MUST 只印 channel 名。`channel create` MUST 在送出註冊留言前先確認後端實體（存在而且標題等於 channel 名；不符 MUST exit 2、什麼都沒註冊——複審 H2），留言後 MUST 輪詢 fetch 到對應出現為止（上限 2 分鐘；逾時 MUST 在 stderr 印提示、仍 exit 0）；`channel delete` MUST NOT 等落地（送出註銷留言成功即 exit 0）。（註冊簿與重建見 `angareion/channel-config` 與 design §9／§10。）

`log` 是唯讀的查詢介面（語意見「唯讀查詢 log」）：它 MUST NOT 讀取、改動或推進游標，MUST NOT 參與「同一個身分只留一個讀者」規則；它的旗標與錯誤訊息同樣 MUST NOT 出現 GitHub repo／issue 的概念（只用 channel 名、AI 名、訊息 id（ULID）、RFC 3339 時刻與字面字串表達）。

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

#### Scenario: log 是第六個介面
- **WHEN** `angareion --help` 印出的子指令清單、`angareion channel --help` 印出的子子指令清單，與 `angareion log --help` 印出的旗標清單（低-14：三份清單都由 WHEN 的輸出觀察）
- **THEN** 頂層子指令恰好是 `channel`、`send`、`wait`、`read`、`log`（由 `angareion --help` 斷言）；`channel` 下的子子指令恰好是 `create`、`delete`（由 `angareion channel --help` 斷言）；`log` 的旗標是查詢類（`--channel`、`--from`、`--to`、`--mentioned`、`--since`、`--until`、`--grep`、`--around`、`--thread`、`--before`、`--after`、`--limit`、`--json`），沒有 `--body` 等寫入旗標（log 不寫信；`--to` 在 log 是收件人的「過濾」條件，不是 `send` 的寫入）

#### Scenario: log 的介面不出現後端概念
- **WHEN** 檢查 `angareion log` 的參數清單、stdout、stderr 與說明（`--help`）
- **THEN** 沒有 repo、issue、comment 等後端概念的字樣；只有 channel 名、AI 名、訊息 id、時刻與字面字串

### Requirement: 唯讀查詢 log
`angareion log` MUST 是一次查到全部相關信件的唯讀查詢，MUST NOT 讀取、改動或推進游標（任何情況、任何旗標組合），MUST NOT 有「印完再 commit」的步驟。

旗標語意（組合規則見 design D1）：
- `--channel <ch>` 可重複（多頻道聯集）；不給＝所有已登記 channel（含寄給別人的訊息）；未登記 exit 1。`log` 用了 `--channel` MUST NOT 印 `read`／`wait` 那行「讀到寄給別人的訊息」警告（log 的目的就是查整個頻道；不警告 MUST NOT 影響 stdout 或 exit code）。
- `--from <AI 名|me>`：寄件人精確相等；`me` 是本指令解析出的身分。
- `--to <AI 名|me>`：「寄給 `<名>`」＝`to` 是該名、或沒有 `to`（廣播）、或 `mentions` 有該名（與 `read` 的「寄給我的」同一條件）。
- `--mentioned`：「@ 我」＝`to` 是我或 `mentions` 有我、廣播不算（語意同 `read --mentioned`）；在 `log` 裡可以與 `--channel` 組合（交集）。
- `--since`／`--until`：時間上下界，都是後端記錄的時刻（`received_at`，RFC 3339），**皆含邊界**；格式錯 exit 1（附格式提示，同 `read --since`），`--since > --until` exit 1。
- `--grep <字串>`：內文的字面（非 regex）搜尋、不分大小寫、只比對 `content`。
- `--around <訊息 id>`（＋`--before N`／`--after N`）：以訊息 id（26 字 ULID）錨定，視窗＝同 channel 內以到達位置計的前 N＋後 N 則＋錨點；`--before`／`--after` 只在 `--around` 下合法（否則 exit 1）、非負整數、各自預設 0。
- `--thread <訊息 id>`：沿 `in_reply_to` 取整串（錨點往上到根，根＋全部子孫，按到達順序）；與 `--around` 互斥（同給 exit 1）；錨點自己的 `in_reply_to` 指不到訊息時，錨點視為根。**已知限制（唯一規則）：串只在錨點所在 channel 內組裝**——`in_reply_to` 指到別的 channel 的訊息時，鏈在跨 channel 前停下，該訊息視為根；跨 channel 的串不追。
- `--limit N`：過濾完之後**依輸出順序排序、取排序結果的最後 N 則**（「最新」由排序鍵定義：單 channel 以到達位置；跨 channel 以 `(received_at, channel, id)`——是全域最新的 N 則，**不是每個 channel 各取 N 則**），仍由舊到新印；N ≥ 0，0 是合法的空結果；負數 exit 1。**預設不限**。與錨點旗標同用時照「先錨點後過濾再排序再截」的順序截——錨點沒有保留地位（被過濾或被截掉就不在輸出裡）。
- `--json`：與 `read --json` 同欄位（`message_to_dict`＋`received_at`）；空結果印 `[]`。

套用順序 MUST 固定：**錨點候選集（`--around` 視窗／`--thread` 串；錨點所在的 channel MUST 在本查詢的選取集合（`--channel` 聯集或全部）內，選取集合內同步後沒有那個訊息 id → exit 1；錨點不符合其他過濾條件（`--from`／`--to`／`--mentioned`／`--since`／`--until`／`--grep`）MUST NOT 被當成錯誤——照常定位，錨點本身被過濾則不出現在輸出，結果照常）→ 過濾交集 → 排序（單 channel 以到達位置；跨 channel 以 `(received_at, channel, id)`）→ `--limit` 取排序後的最後 N 則 → 依同序印出**。輸出 MUST 固定由舊到新（同一排序鍵；單 channel 以到達位置；跨 channel 以 `(received_at, channel, id)`），內文 MUST 完整印出不截斷（多行逐行完整、`--json` 的 `content` 完整保留）。錨點旗標與 `--since`／`--until`／`--grep` 等過濾旗標 CAN 同時使用（先錨點、後過濾）。非 angareion 訊息格式的留言 MUST NOT 出現在結果裡，MUST NOT 計入 `--around` 的視窗計數。

#### Scenario: log 不動游標
- **WHEN** B 有未讀訊息，B 執行 `angareion log`（無旗標、或任何旗標組合，含 `--channel`、`--around`、`--thread`）
- **THEN** 那些訊息仍是未讀：log 前後本機游標檔逐位元相同，隨後的 `angareion read` 讀得到它們

#### Scenario: 預設查全部 channel 的全部訊息
- **WHEN** 兩個 channel 各有「寄給別人」的訊息與廣播，B 執行無旗標的 `angareion log`
- **THEN** 兩個 channel 的所有訊息都回傳（含寄給別人的），由舊到新；exit 0；stderr 沒有警告

#### Scenario: log --channel 不印 read 的警告
- **WHEN** B 執行 `angareion log --channel general`
- **THEN** stdout 是查得的訊息、exit 同 `read` 的成功情況；stderr 沒有「包含寄給別人的訊息」那行警告

#### Scenario: 過濾旗標取交集
- **WHEN** channel X 裡有：`from` 是 A 的信（內文含 blocker）、`from` 是 B 的信（也含 blocker）、其他信；B 執行 `angareion log --channel X --from <A 的 AI 名> --grep blocker`
- **THEN** 只回傳 A 那則；`--mentioned` 與 `--channel` 同給也成立（交集）

#### Scenario: 時間上下界皆含邊界
- **WHEN** 兩則訊息的後端時刻分別是 2026-10-25T03:00:00Z 與 2026-10-25T22:00:00Z，`angareion log --channel X --since 2026-10-25T03:00:00Z --until 2026-10-25T22:00:00Z`
- **THEN** 兩則都回傳；執行前後的游標檔相同；每則帶 `received_at`

#### Scenario: 時間旗標的輸入錯
- **WHEN** `--since` 給了非 RFC 3339 的字串；或 `--since` 晚於 `--until`
- **THEN** 都 exit 1（stderr 有格式／範圍說明），stdout 沒有內容

#### Scenario: --grep 的語意
- **WHEN** 某則信的內文是 `Blocker: disk full`，另有一則內文不含此字但 `from` 是 `Blocker-xxx` 樣式的名字
- **THEN** `angareion log --grep blocker` 命中前者（不分大小寫）、不命中後者（欄位不在比對範圍）

#### Scenario: --around 的視窗
- **WHEN** channel 內到達順序是 m1..m20，`angareion log --channel X --around <m10 的 id> --before 3 --after 4`
- **THEN** 回傳 m7 到 m14（錨點 m10 前 3 與後 4），由舊到新、m10 在中間
- **WHEN** `angareion log --channel X --around <m10 的 id>`（沒給 --before/--after）
- **THEN** 只回傳 m10

#### Scenario: --around 的輸入錯
- **WHEN** `--around` 給的不是 26 字 ULID；`--thread` 與 `--around` 同給；`--before 2` 沒有 `--around`；`--before -1`
- **THEN** 都 exit 1，什麼都不印在 stdout

#### Scenario: --thread 的整串
- **WHEN** m5 回 m3、m8 回 m3、m9 回 m5（無其他 in_reply_to），`angareion log --channel X --thread <m5 的 id>`
- **THEN** 回傳 m3、m5、m8、m9（m5 的根是 m3），由舊到新

#### Scenario: --thread 的錨點視為根
- **WHEN** m5 的 `in_reply_to` 指向一則不存在（或不是 angareion 訊息）的 id，`--thread <m5 的 id>`
- **THEN** m5 與它的子孫照回傳（m5 視為根），不因為指不到而 exit 1

#### Scenario: --thread 的根在別的 channel（已知限制）
- **WHEN** channel X 的 m5 的 `in_reply_to` 指向 channel Y 的 m1（Y 那則是 angareion 訊息、在另一個 channel），`--thread <m5 的 id>`
- **THEN** m5 視為根：回傳 X 內 m5 與它的子孫；Y 的 m1 不追、不回傳；exit 0（不是錯誤）

#### Scenario: 錨點找不到與空結果
- **WHEN** `--around`／`--thread` 給了同步後確認不存在的 ULID
- **THEN** exit 1（stderr 說明找不到）；**WHEN** 過濾交集後沒有任何訊息（如 `--grep` 不中）**THEN** exit 0、stdout 空（或 `--json` 為 `[]`）

#### Scenario: 錨點被過濾條件濾掉不是錯誤
- **WHEN** channel X 內按到達順序有 m1（A 寄）、m2（B 寄）、m3（A 寄），執行 `angareion log --channel X --from <A 的 AI 名> --around <m2 的 id> --before 1 --after 1`
- **THEN** exit 0：窗口以 m2 數（m1..m3）、`--from A` 把 m2 濾掉，輸出 m1、m3 由舊到新——錨點自己不在輸出裡（與「找不到」不同：定位成功、結果不空）

#### Scenario: --limit 保留排序尾端的 N 則
- **WHEN** 目標有 m1..m30，`angareion log --channel X --limit 5`
- **THEN** 回傳 m26..m30，以由舊到新的順序印出

#### Scenario: --limit 跨 channel 取全域最新的
- **WHEN** channel X 與 Y 各有多則（X 內最新的收到時刻比 Y 內所有訊息都早），`angareion log --channel X --channel Y --limit 1`
- **THEN** 只回傳 **Y 內最新的那一則**（全域排序 `(received_at, channel, id)` 下最新的一則恰是 Y 內最新那則；不是 X、Y 各一則，也不是 Y 的其他訊息）；由舊到新印出

#### Scenario: --limit 可以把錨點截掉
- **WHEN** `--around` 的窗口經過濾後留有 7 則，再一起給 `--limit 2`
- **THEN** 輸出是該窗口排序後最後 2 則；錨點不在這 2 則內時照常不出現，exit 0（不是錯誤）

#### Scenario: --json 與多行內文
- **WHEN** 某則信的內文有三行，`angareion log --channel X --json`／文字模式各執行一次
- **THEN** `--json` 的 `content` 完整保留三行（換行在 JSON 字串內）；文字模式逐行縮排完整印，無截斷；兩者都帶 `received_at`

#### Scenario: 非訊息留言不列入
- **WHEN** channel X 的實體上混著一則不是 angareion 訊息格式的留言（例如維運註記），`--around` 的視窗跨過它的位置
- **THEN** log 的結果不含它，視窗的前後 N 計數也不算它

#### Scenario: 錯誤的結束碼
- **WHEN** `angareion log --channel nosuch`；或 token／設定缺、後端 HTTP 錯
- **THEN** 前者 exit 1（channel 不存在）、後者 exit 2；stderr 有說明、stdout 沒有內容、輸出沒有 token

### Requirement: log 的本機快取
`log` 的查詢 MUST 以本機快取服務：對查詢涉及的每個 channel 先做**增量同步**，同步成功（304 的條件式請求也算成功）之後才在本機過濾回答。

- 同步用後端的「列舉」動作（見「後端可以替換」），帶 `since = 上一次同步到的後端時刻 − 60 秒`（與游標同源的 margin；快取為空時不帶 `since`）；合併 MUST 是冪等的 upsert：到達位置比已記錄的新、**或從未出現過**（接住亂序晚到的較小位置）就進鏡射，同位置內容不同（後端被編輯）就覆蓋；已記錄的最大位置與時刻 MUST NOT 後退。
- 回應含新訊息時 MUST NOT 記錄 ETag（不得成為下次「沒有新訊息」的驗證器）；回應完整掃過且乾淨、單頁未滿才記錄（沿用 `wait` 的條件式查詢規則）；下次同步同一視窗 MUST 送 `If-None-Match`，304 MUST 視為「沒有增量」且不計後端額度。
- 任一涉入 channel 的同步失敗（HTTP 錯、逾時）MUST 以 exit 2 結束且 MUST NOT 輸出任何查詢結果——不用「部分同步＋舊鏡射」當完整答案。
- 快取 MUST 每身分一份（檔名跟身分的 slug，同游標檔的分位），放在 state 目錄；channel 的檔名 MUST 以 channel 名的**可逆轉義**成檔（屬 `[a-z0-9_-]` 的字元原樣，**大寫 ASCII 字母與其餘字元一律以 `%XX` 編碼其 UTF-8 位元組**——大寫也轉義是低-16 定案：大小寫不敏感的檔案系統上，只差大小寫的兩個 channel 名 MUST NOT 落到同一個檔，`A` 與 `a` MUST 各有自己的快取檔；轉義 MUST 無條件可逆且單射），MUST NOT 依賴 channel 名的字元限制成立——組態檔裡出不合法的名字（`a/b`、`../x` 這類）時 MUST 仍不出快取目錄（轉義後不成路徑分隔；design D3.2 的 `channel_slug`），檔內記原始 channel 名並在載入時比對（不符＝整檔視為空）；與游標檔、`wait` 的 ETag 狀態檔 MUST NOT 共用檔案，也 MUST NOT 被對方讀寫。快取 MUST NOT 寫進或讀出任何游標。
- 快取檔 MUST 以「同目錄暫存＋原子替換」寫出，檔案權限 0600、目錄 0700（快取含頻道訊息內文，只能本身分讀）。
- 快取檔不存在、不是合法 JSON、版本不認得、缺欄位、或檔內記的 channel↔entity 對應與設定檔不符時，MUST 整檔視為空並全量重建；MUST NOT 部分信任。刪掉快取 MUST 只造成下一次全量同步，MUST NOT 讓訊息漏掉或查詢結果變異。
- 快取寫不進去（唯讀目錄、磁碟滿）MUST 只在 stderr 警告一次（同一行程最多一次），查詢照常以本輪記憶體內的鏡射回答；exit code 與 stdout MUST NOT 因此改變。
- 兩個 `log` 行程並行同一身分時，快取檔 MUST NOT 半寫（原子替換）；最壞情況是其中一輪的增量被下一輪重補（upsert 冪等），結果不漏。
- **已知限制**：合併只有 upsert，MUST NOT 解後端的刪除——後端刪掉的留言會留在快取與結果裡、`--since` 增量 MUST NOT 回報刪除；它在全量重建之後才不再出現。skill 的說明 MUST 寫明 log 的結果不是「這則現在還在」的證據。

#### Scenario: 增量同步接住新訊息
- **WHEN** B 先 `log` 過一次（快取建好），之後 A 又送一則信，B 再 `angareion log --channel X`
- **THEN** 第一次查詢是全量，第二次是增量（只列到新訊息），查得到 A 的新信

#### Scenario: 亂序晚到的留言
- **WHEN** 鏡射已記錄到到達位置 100，一則位置 98 的訊息晚一秒才出現
- **THEN** 下一次同步把它合併進鏡射（「從未出現過的位置」也納入），`log` 查得到它；已記錄的最大位置不後退

#### Scenario: 後端被編輯的留言
- **WHEN** 已進鏡射的一則留言在後端被編輯（同一位置的內容變了）
- **THEN** 下一次同步以同位置覆蓋內容；沒建立新位置、不在結果裡重複（機理：列舉的 `since` 按最後編輯時刻過濾，編輯過的舊留言重新出現在列表上）

#### Scenario: 後端刪掉的留言是已知限制
- **WHEN** 一則留言已在快取裡，隨後在後端被刪掉；之後只做增量的 `log`
- **THEN** 它仍然出現在結果裡（已知限制：合併不解刪除）；刪掉快取目錄、全量重建之後它就不出現了

#### Scenario: 快取檔名不依賴 channel 名的字元限制（轉義）
- **WHEN** `channels.json` 被手改出一個名字含 `/` 的 channel（例如 `a/b`），執行觸及它的 `angareion log`
- **THEN** 它的快取檔落在 `log-<身分>/` 內（檔名用轉義後的名字），MUST NOT 寫到或讀出快取目錄外面的路徑；`log` 的查詢照常回答

#### Scenario: 安靜 channel 重複查詢近零額度
- **WHEN** 對同一沒有新訊息的 channel 連續多次 `angareion log`
- **THEN** 第一次全量（或增量）之後記下 ETag；後續每次同步送 `If-None-Match`、後端回 304、照常以鏡射回答；中間若有新訊息，該筆記錄清掉、同步用完整請求接住它
- **WHEN** 回應有下一頁或第 1 頁已滿
- **THEN** 不留 ETag（同 `wait` 的規則），下一次用完整請求

#### Scenario: 同步失敗不出結果
- **WHEN** 查詢涉及的某一個 channel 在同步時後端回 HTTP 500（或逾時）
- **THEN** exit 2，stdout 沒有內容；不會用其餘 channel 的舊鏡射交出「看似完整」的答案

#### Scenario: 壞檔與對應失配都重建
- **WHEN** 快取檔被截斷／不是 JSON／版本不認得／缺欄位；或 channel X 對應的 issue 換了（檔內記的是舊實體）
- **THEN** 該 channel 整檔視為空、全量重建，`log` 的結果照常正確（exit 0）；不部分信任舊內容

#### Scenario: 權限
- **WHEN** 首次執行 `angareion log`（快取目錄與檔案初次建立）
- **THEN** 目錄是 0700，快取檔是 0600；同一身分以外的座位不共用這份快取（檔名含身分 slug）

#### Scenario: 快取寫不進去
- **WHEN** state 目錄唯讀，B 執行 `angareion log`
- **THEN** stderr 警告一次，`log` 照常以本輪記憶體內的鏡射回答，exit 0 與 stdout 正常；下一輪仍可用

#### Scenario: 兩個 log 並行
- **WHEN** 同一身分的兩個 `angareion log` 同時跑
- **THEN** 快取檔不會半寫；兩邊的查詢答案都正確、不漏訊息（最壞情況：其中一輪的增量下一輪重補）

#### Scenario: 快取與游標各行其是
- **WHEN** B 刪掉 `log-<身分>/` 後執行 `log`；或 B 執行 `log` 跟 `read` 交替
- **THEN** `log` 全量重建後照常回答；`read` 的游標與已讀狀態不受 `log`／快取影響，`log` 也不受游標影響

