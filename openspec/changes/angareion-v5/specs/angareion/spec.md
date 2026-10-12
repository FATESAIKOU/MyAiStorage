## MODIFIED Requirements

### Requirement: 四個介面
Angareion 對外 MUST 只有四個介面：建 channel、送訊息、確認有沒有新訊息、讀訊息；指令分別是 `angareion channel create`、`angareion send`、`angareion wait`、`angareion read`（`channel create` 是否保留待本人確認，在那之前維持現狀；見 design 的 Open Questions）。MUST NOT 有 ack、已讀回條或限流。channel 名 MUST 是英文名（非空的 ASCII 字母、數字、`-`、`_`）；介面（參數、stdout、stderr、錯誤訊息）MUST NOT 出現 GitHub repo／issue 的概念。第一期 channel 對所有隊員公開，沒有成員設定：`channel create` MUST NOT 收 `--member`。`send` 的 `--channel` 必填、`--to` 選填（不給＝發到整個 channel）。

#### Scenario: 送出並讀到
- **WHEN** A 對 channel X 送一則 `to` 是 B 的訊息，B 執行 `angareion read`
- **THEN** B 讀到那則訊息；再執行一次 `angareion read` 讀不到同一則

#### Scenario: 不給 to 就是發到整個 channel
- **WHEN** A 對 channel X 送一則不帶 `to` 的訊息
- **THEN** 送出成功（stdout 印訊息 id）；X 的任何成員 `read --channel X` 都讀得到它

#### Scenario: 建 channel
- **WHEN** `angareion channel create X`（不帶 `--member`）
- **THEN** stdout 只印 channel 名，exit 0；之後對 channel X 送訊息成功；沒有給名稱時 exit 1

#### Scenario: 建 channel 不再收成員
- **WHEN** `angareion channel create X --member <位址>`
- **THEN** exit 1（未知參數）；什麼都沒建

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
一則訊息 MUST 有 `from`、`to`、`channel`、`urgency`（0～9 的整數，預設 5）、`content`、`attachments`，加上寄件者產生的 `id`（ULID）與 `at`（含時區）；`id` 與 `at` 只用來辨識與顯示，MUST NOT 用來判斷新舊。`from`、`to` 與自己的身分 MUST 是 agent 名（`<repo 名>-<pm|arch|impl|test>[-<id>]`，例 `MyLinuxPool-impl-2`、`LLMGateway-pm`；同角色只有一個時省略 id）；`to` 選填（不給＝發到整個 channel）。`--json` 輸出的 `from`／`to` 也是這個字串。不做嚴格的身分認證（任何人可以用任何名字送）。送出時內文 MUST 來自 `--body <文字>` 或 `--body -`（stdin），沒給 `--body` MUST exit 1。

#### Scenario: urgency 超出範圍
- **WHEN** `angareion send --channel X --urgency 12 --body hi`
- **THEN** exit 1，什麼都沒送

#### Scenario: 群組位址
- **WHEN** `angareion send --channel X --to 'group=全員' --body hi`
- **THEN** exit 1（新模型裡沒有群組位址，`group=` 不是合法的 agent 名），什麼都沒送

#### Scenario: 位址不是 agent 名
- **WHEN** `angareion send --channel X --to 'repo=owner/team,role=pm' --body hi`
- **THEN** exit 1（位址格式錯誤），什麼都沒送

#### Scenario: 沒給 body
- **WHEN** `angareion send --channel X --to <agent 名>`，沒有 `--body`，stdin 是開著的管線
- **THEN** exit 1，stderr 說明要用 `--body <文字>` 或 `--body -`；不讀 stdin，什麼都沒送

#### Scenario: 明寫才讀 stdin
- **WHEN** `echo 'hi' | angareion send --channel X --to <agent 名> --body -`
- **THEN** 送出的內文是 `hi`，stdout 印訊息 id

#### Scenario: 不存在的 channel
- **WHEN** 對 channel 設定檔裡沒有的 channel 做 `send`、`read` 或 `wait`
- **THEN** exit 1，stderr 說明 channel 不存在；什麼都沒送

### Requirement: 後端可以替換
四個介面 MUST 經由一個後端介面實作，後端也是那四個動作（建 channel、送、確認有沒有新訊息、讀）；對外的 `wait` 是在後端「確認有沒有新訊息」上輪詢（不阻塞＝確認一次）；這次的實作是 GitHub API＋Issues（channel 名經 channel 設定檔對應到 backend 實體：哪個 backend 的哪個 issue；訊息＝留言）。後端 MUST NOT 存成員（第一期 channel 全公開）。換後端時，指令與訊息格式 MUST NOT 改變。

#### Scenario: 用假後端
- **WHEN** 測試以記憶體裡的假後端執行四個指令
- **THEN** 行為和用 GitHub 後端時相同，不連網

#### Scenario: 同名 channel 以最舊的為準
- **WHEN** 信道裡有兩個標題相同的 issue（較舊的編號 3、較新的編號 7），`angareion send --channel <名稱>`
- **THEN** 訊息送到編號 3 的那個

#### Scenario: 建立時發現更舊的同名
- **WHEN** `angareion channel create X` 在建立之後再查，發現出現了比自己剛建的更舊的同名 issue
- **THEN** 回傳（並印出）更舊的那個的編號，之後的訊息都送到那個

#### Scenario: 多頁時第 1 頁不變
- **WHEN** 「寄給我的」的留言列表超過一頁，第 1 頁（都是寄給別人的）內容和上一次一樣，第 2 頁多了一則寄給我的；`wait` 先前已經查過一次
- **THEN** `angareion wait --timeout 0` exit 0（不因第 1 頁的 304 而回報沒有新訊息）

### Requirement: token 的位置與讀法
token 檔的位置 MUST 能由 `ANGAREION_TOKEN_FILE` 指定（優先於其他來源）；沒有設定時用設定檔的 `token_file` 或預設位置（實作細節，見 design D5）。檔案 MUST 只有擁有者有權限（0400 或 0600），否則 MUST 拒絕（exit 2）。token MUST NOT 出現在任何輸出、log、錯誤訊息或訊息內容裡。

#### Scenario: 權限太寬
- **WHEN** token 檔的權限是 0644
- **THEN** 指令 exit 2，說明要改成只有擁有者能讀；輸出裡沒有 token

#### Scenario: 唯讀掛載
- **WHEN** token 檔是 0400（worker 上的唯讀掛載）
- **THEN** 正常讀取

### Requirement: 輸出與結束碼
stdout MUST 只放結果（`send` 印訊息 id，`read` 印訊息或 `--json`），說明與錯誤放 stderr。結束碼：0 成功；1 用法或輸入錯誤；2 後端、網路或設定錯誤；3 `wait` 逾時沒有新訊息。

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

### Requirement: 給 agent 的說明
repo MUST 附一份給 agent 讀的說明（`skills/angareion/SKILL.md`）：四個指令的用法、身分、閒著時用 `angareion wait` 阻塞地等、收到高 urgency 訊息的處理。說明 MUST NOT 含任何秘密或私人資訊。

#### Scenario: agent 照說明收信
- **WHEN** 一個讀了說明的 agent 手上沒事
- **THEN** 它執行 `angareion wait --timeout <秒>`，而不是反覆呼叫模型查信
