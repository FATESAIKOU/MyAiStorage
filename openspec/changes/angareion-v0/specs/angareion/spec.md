## ADDED Requirements

### Requirement: 四個介面
Angareion 對外 MUST 只有四個介面：建 channel、送訊息、確認有沒有新訊息、讀訊息；指令分別是 `angareion channel create`、`angareion send`、`angareion wait`、`angareion read`。MUST NOT 有 ack、已讀回條或限流。

#### Scenario: 送出並讀到
- **WHEN** A 對 channel X 送一則 `to` 是 B 的訊息，B 執行 `angareion read`
- **THEN** B 讀到那則訊息；再執行一次 `angareion read` 讀不到同一則

#### Scenario: 建 channel
- **WHEN** `angareion channel create X --member <位址>`
- **THEN** stdout 印 channel 名與編號，exit 0；之後對 channel X 送訊息成功；沒有給名稱時 exit 1

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
`wait` 與 `read` 的目標是某個 channel（`--channel`）或「寄給我的」（不給 `--channel`：所有 channel 裡 `to` 是自己身分的訊息）。`read` 不給 `--since` 時 MUST 從本機游標之後讀起並把游標往前推；給了 `--since` 就照給的讀，MUST NOT 改動本機游標。游標刪掉時 MUST 從頭讀起。

#### Scenario: 游標刪掉
- **WHEN** 刪掉本機游標後執行 `angareion read`
- **THEN** 讀到目標裡所有的訊息

#### Scenario: 寄給我的
- **WHEN** 兩個 channel 裡各有一則 `to` 是我的訊息，另外有一則 `to` 是別人的；不給 `--channel` 執行 `angareion read`
- **THEN** 讀到 `to` 是我的那兩則，不含別人的

#### Scenario: 同一秒的兩則
- **WHEN** 兩則訊息的 `at` 在同一秒，B 讀過第一則之後第二則才到，再執行 `angareion read`
- **THEN** 讀到第二則，而且第一則不會重複

#### Scenario: 被編輯的留言
- **WHEN** 已讀過的一則留言在 GitHub 上被編輯（`updated_at` 變新）
- **THEN** 下一次 `read` 不會把它當新訊息再回傳（依訊息 `id` 去重）

### Requirement: 訊息格式
一則訊息 MUST 有 `from`、`to`、`channel`、`urgency`（0～9 的整數，預設 5）、`content`、`attachments`，加上寄件者產生的 `id`（ULID）與 `at`（含時區）。位址 MUST 是 `{repo, role, name?}`；群組位址在 v0 MUST 被拒絕。

#### Scenario: urgency 超出範圍
- **WHEN** `angareion send --urgency 12`
- **THEN** exit 1，什麼都沒送

#### Scenario: 群組位址
- **WHEN** `angareion send --to` 給的是群組位址
- **THEN** exit 1，什麼都沒送

### Requirement: 後端可以替換
四個介面 MUST 經由一個後端介面實作，後端也是那四個動作（建 channel、送、確認有沒有新訊息、讀）；對外的 `wait` 是在後端「確認有沒有新訊息」上輪詢（不阻塞＝確認一次）；這次的實作是 GitHub API＋Issues（channel＝issue、訊息＝留言）。換後端時，指令與訊息格式 MUST NOT 改變。

#### Scenario: 用假後端
- **WHEN** 測試以記憶體裡的假後端執行四個指令
- **THEN** 行為和用 GitHub 後端時相同，不連網

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
- **WHEN** 沒有設定 `channel_repo` 就執行 `angareion send`
- **THEN** exit 2，stderr 說明缺什麼

#### Scenario: 後端錯誤
- **WHEN** 後端回 HTTP 錯誤（例如 403 次級限制或網路中斷）
- **THEN** exit 2，stderr 說明狀態碼，不自動重試；輸出裡沒有 token

### Requirement: 給 agent 的說明
repo MUST 附一份給 agent 讀的說明（`skills/angareion/SKILL.md`）：四個指令的用法、身分、閒著時用 `angareion wait` 阻塞地等、收到高 urgency 訊息的處理。說明 MUST NOT 含任何秘密或私人資訊。

#### Scenario: agent 照說明收信
- **WHEN** 一個讀了說明的 agent 手上沒事
- **THEN** 它執行 `angareion wait --timeout <秒>`，而不是反覆呼叫模型查信
