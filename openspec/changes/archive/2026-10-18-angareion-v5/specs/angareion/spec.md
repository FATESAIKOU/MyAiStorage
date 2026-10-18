## ADDED Requirements

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

## MODIFIED Requirements

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

### Requirement: 確認有沒有新訊息
`angareion wait` MUST 能不阻塞（`--timeout 0`：查一次就回）、限時阻塞（`--timeout N`）或一直阻塞（不給逾時）。有新訊息時 exit 0；逾時沒有新訊息時 exit 3。等待期間 MUST NOT 改動游標。

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

### Requirement: 目標與游標
`wait` 與 `read` 的目標是某個 channel（`--channel`）或「寄給我的」（不給 `--channel`：所有 channel 裡 `to` 是自己 agent 名、或沒有 `to` 的訊息）。判斷訊息新不新 MUST 只看後端（到達位置與後端記錄的時刻），MUST NOT 使用寄件者填的 `at` 或 `id`。`read` 不給 `--since` 時 MUST 從本機游標之後讀起並把游標往前推；給了 `--since`（後端時刻）就回傳不早於它的訊息（含邊界），MUST NOT 改動本機游標。`read` 的每則輸出 MUST 帶後端記錄的時刻 `received_at`。游標刪掉時 MUST 從頭讀起。

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

## REMOVED Requirements

### Requirement: 後端可以替換

## RENAMED Requirements

- FROM: `### Requirement: 四個介面`
- TO: `### Requirement: 五個介面`
