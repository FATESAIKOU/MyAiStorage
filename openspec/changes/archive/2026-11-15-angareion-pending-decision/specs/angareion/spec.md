## MODIFIED Requirements

### Requirement: 六個介面
Angareion 對外 MUST 只有七個介面家族：建 channel、刪 channel、送訊息、確認有沒有新訊息、讀訊息、查詢訊息（唯讀）、待決（`decision`）；指令分別是 `angareion channel create`、`angareion channel delete`、`angareion send`、`angareion wait`、`angareion read`、`angareion log`、`angareion decision`。`decision` 的六個動作（`ask`、`update`、`withdraw`、`submit`、`list`、`snapshot`）的語意見 `angareion-pending-decision`；它的寫入也只是 append 一則訊息，MUST NOT 編輯或刪除任何訊息。MUST NOT 有 ack、已讀回條或限流。channel 名 MUST 是英文名（非空的 ASCII 字母、數字、`-`、`_`）；介面（參數、stdout、stderr、錯誤訊息）MUST NOT 出現 GitHub repo／issue 的概念。第一期 channel 對所有隊員公開，沒有成員設定：`channel create` MUST NOT 收 `--member`。`send` 的 `--channel` 必填、`--to` 選填（不給＝發到整個 channel）。`channel create` 與 `channel delete` 的 stdout MUST 只印 channel 名。`channel create` MUST 在送出註冊留言前先確認後端實體（存在而且標題等於 channel 名；不符 MUST exit 2、什麼都沒註冊——複審 H2），留言後 MUST 輪詢 fetch 到對應出現為止（上限 2 分鐘；逾時 MUST 在 stderr 印提示、仍 exit 0）；`channel delete` MUST NOT 等落地（送出註銷留言成功即 exit 0）。（註冊簿與重建見 `angareion/channel-config` 與 design §9／§10。）

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
- **THEN** 頂層子指令恰好是 `channel`、`send`、`wait`、`read`、`log`、`decision`（由 `angareion --help` 斷言）；`decision` 下的 action 恰好是 `ask`、`update`、`withdraw`、`submit`、`list`、`snapshot`（由 `angareion decision --help` 斷言）；`channel` 下的子子指令恰好是 `create`、`delete`（由 `angareion channel --help` 斷言）；`log` 的旗標是查詢類（`--channel`、`--from`、`--to`、`--mentioned`、`--since`、`--until`、`--grep`、`--around`、`--thread`、`--before`、`--after`、`--limit`、`--json`），沒有 `--body` 等寫入旗標（log 不寫信；`--to` 在 log 是收件人的「過濾」條件，不是 `send` 的寫入）

#### Scenario: log 的介面不出現後端概念
- **WHEN** 檢查 `angareion log` 的參數清單、stdout、stderr 與說明（`--help`）
- **THEN** 沒有 repo、issue、comment 等後端概念的字樣；只有 channel 名、AI 名、訊息 id、時刻與字面字串

### Requirement: 訊息格式
一則訊息 MUST 有 `from`、`to`、`channel`、`urgency`（0～9 的整數，預設 5）、`content`、`attachments`，加上寄件者產生的 `id`（ULID）與 `at`（含時區）；`id` 與 `at` 只用來辨識與顯示，MUST NOT 用來判斷新舊。`from`、`to` 與自己的身分 MUST 是 agent 名：驗證用的正規式是 `^[A-Za-z0-9][A-Za-z0-9._-]*-(pm|arch|impl|test)(-[A-Za-z0-9_.]+)?$`（repo 段含 `-`；複審 M1）；**驗證與解析是兩步**——regex 只驗整體形狀，另做由右而左的解析（先取可省的 `-<id>`、再取最右邊的角色字，左邊剩下的都是 repo 名；例 `MyLinuxPool-impl-2`、`LLMGateway-pm`、`my-repo-impl-2` → repo `my-repo`、角色 `impl`、id `2`；同角色只有一個時省略 id；角色字只有小寫的 `pm|arch|impl|test`）；`to` 選填（不給＝發到整個 channel）。`--json` 輸出的 `from`／`to` 也是這個字串。不做嚴格的身分認證（任何人可以用任何名字送）。送出時內文 MUST 來自 `--body <文字>` 或 `--body -`（stdin），沒給 `--body` MUST exit 1。

`datatype` 是 front matter 的一個欄位（單行純量，文法 `^[a-z][a-z0-9-]{0,31}$`；內文不放結構）：`send` 寫 `message`，`angareion decision …` 寫 `decision`，寫入的 front matter 鍵順序是 `angareion`、`datatype`、`id`、…。沒有這個欄位的舊訊息讀成 `message`；值不是字串或不合文法的也讀成 `message`（整則照收）；值合法但不認得的照原字串保留，只照位址遞送，不參與 `decision` 的匯總。`angareion: 1` 不升版（舊讀者會略過未知的欄位）。`read --json` 與 `log --json` 的每則都帶 `datatype`。

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

#### Scenario: 寫入帶 datatype
- **WHEN** `angareion send --channel X --body hi`
- **THEN** 留言的 front matter 第二個鍵是 `datatype: message`；`read --json` 的那則帶 `"datatype": "message"`

#### Scenario: 舊訊息沒有 datatype
- **WHEN** 讀到一則 front matter 沒有 `datatype` 的留言（舊版寫的）
- **THEN** 讀成一般訊息，`datatype` 是 `message`，照常遞送與顯示，不丟掉

#### Scenario: 不認得的 datatype
- **WHEN** 讀到 `datatype: poll`（合法但這個讀者不認得）
- **THEN** 照位址遞送（`read`／`wait` 的收件範圍不變），`log` 照顯示並標出原字串，`decision` 的匯總不理它
