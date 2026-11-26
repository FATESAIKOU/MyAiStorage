# angareion/channel-config Specification

## Purpose
channel 名與 backend 實體的對應、以及 backend 本身的設定，放在 MyAiStorage repo 的兩個設定檔（`config/angareion/channels.json`、`config/angareion/backends.json`）；channel 的對應由**註冊簿重建 workflow**直接更新 main（本人裁定：channel 自由建立、不走 PR；2026-10-12 14:3x 改為註冊簿 issue＋整串留言重建），backend 設定走 PR；angareion 只讀本機複本（`git fetch` 讀 `origin/main`；查無先拉一次，失敗沿用）。
## Requirements
### Requirement: channel 設定檔
channel 名到 backend 實體的對應 MUST 寫在 MyAiStorage repo 的 `config/angareion/channels.json`（路徑已定案）：JSON、版本欄（`version`）、`channels` 對應表；每筆是 `{"backend": <backend 名>, "issue": <編號>}`（repo 不重寫，在 backend 設定裡）。換過頁的頻道多一個 `retired`：`[{"issue": <編號>, "until": <帶時區的 RFC 3339>, "side": true?}]`，依重建套用的順序（舊的在前）；`issue` 是現頁。沒換過頁的頻道 MUST NOT 有這個鍵（既有頻道的檔案逐位元組不變），`version` 不升。`retired` 形狀不合（不是 list、`issue` 不是正整數、`until` 讀不出、`side` 不是 `true`、與現頁或彼此重號）MUST 視為設定錯誤（exit 2）。舊版只讀 `backend`／`issue`。`send`／`read`／`wait` 與 `channel create`／`channel delete` MUST 經這個檔解析；名字不在檔裡 MUST 視為 channel 不存在（exit 1）。angareion MUST NOT 直接寫這個檔（只有 workflow 寫，見下面的 requirement）。**這檔是機器產物**（重建的輸出）：人工改會被下一次重建還原，MUST NOT 手改（複審 L5）。

#### Scenario: 設定檔指到 backend 實體
- **WHEN** 設定檔把 `general` 指到 backend `github-main` 的某個實體，執行 `angareion send --channel general --body hi`
- **THEN** 訊息進那個實體的留言；stdout 印訊息 id

#### Scenario: 名字不在設定檔
- **WHEN** `angareion send --channel nobody-knows --body hi`（或對它 `channel delete`）
- **THEN** exit 1，stderr 說明 channel 不存在；什麼都沒送、什麼都沒改

### Requirement: backend 設定檔
backend 本身的設定 MUST 寫在 MyAiStorage repo 的 `config/angareion/backends.json`（路徑已定案）：JSON、版本欄、`backends` 對應表；每筆 MUST 有 `type`（目前只有 `github`）與該 type 的設定（GitHub backend：用的 `repo`）。angareion MUST 只從這個檔知道 backend 的設定，不在個人設定檔裡放 backend 的位置；不認得的 `type` MUST exit 2（設定錯誤）。

#### Scenario: backend 的 repo 來自設定檔
- **WHEN** backend 設定檔把 `github-main` 指到某個 repo，channel 設定檔把 `general` 指到 `github-main` 的某個實體
- **THEN** `send --channel general` 的訊息進那個 repo 那個實體的留言

#### Scenario: 不認得的 backend type
- **WHEN** backend 條目的 `type` 不是 `github`
- **THEN** exit 2，stderr 說明不認得的 type；什麼都沒送

### Requirement: channel 對應的更新（註冊簿重建）
`channels.json` MUST 由 MyAiStorage 的**註冊簿 issue**（長期存在，label 見 design §10）上的留言重建：`channel create`／`channel delete` MUST 各在它下面留一則一行 JSON 留言——`{"schema": 1, "action": "register"|"unregister", "channel": "<名>", "backend": "<backend 名>", "issue": <編號>}`；angareion 送出留言後 MUST NOT 等重建完成（`channel create` 會輪詢 fetch 到對應出現，見 `angareion` 的六個介面）。換頁的留言是 `{"schema": 1, "action": "rollover", "channel": "<名>", "backend": "<backend 名>", "issue": <新頁>, "from": <舊頁>}`，恰好這六鍵，`from` 是正整數且不等於 `issue`；`register`／`unregister` 帶 `from` MUST 視為不合法（unknown fields）並回報。

workflow MUST `on: issue_comment`（限註冊簿 issue；觸發層 MUST 用 `if` 擋掉 Bot——workflow 自己的回報留言不會再觸發新的 run，複審 H1）並有 `schedule`（每天一次）當保險（複審 M3）觸發；每次 run 用 `GET /repos/{owner}/{repo}/issues/{n}/comments?per_page=100` 讀整串留言（MUST 翻完所有頁——複審 M4），依時間序**重建** `channels.json` 並 commit 進 main（結果與 main 相同就不 commit）；MUST 用 `concurrency` 併發控制且 `cancel-in-progress`（新的取消舊的）——因為每次都從整串重建，被取消的 MUST NOT 遺失任何留言的效果。

**套用規則（先到者為準）**：`register`——名字沒有對應就加上；已對應同一個實體視為成功（冪等）；已對應不同實體 MUST NOT 覆蓋，MUST 在註冊簿上回報。`unregister`——只移除指向同一個實體的對應；已經沒有、或指到別的實體 MUST NOT 改動對應，MUST 回報。

`rollover`（依 `(created_at, id)` 的順序）：頻道不存在、或 `backend` 與現頁不同 → 不改、回報；`issue` 已是現頁或已在 `retired` → no-op、不回報；`from` 是現頁 → `retired` 加 `{from, until}`，現頁改成 `issue`；`from` 已在 `retired`（別人先換了）→ `retired` 加 `{issue, until, side: true}`，現頁不變；其他 `from` → 不改、回報。`until` 是那則 `rollover` 留言的 `created_at`，沒有就不改、回報。`register` 判斷「同一個實體」只比現頁；`unregister` 指向現頁時整筆移除（含 `retired`），指向退役頁 MUST NOT 改動、回報。

**輸入（防注入；複審 H2 調整）**：留言內容 MUST 只經環境變數交給腳本，MUST NOT 用 `${{ }}` 插進 shell；JSON MUST 嚴格解析。重建時每個留言 MUST **只驗留言本身**：channel 名（`^[A-Za-z0-9_-]+$`）、backend 名存在（在 `backends.json` 裡）、`author_association` 在採計清單；**MUST NOT 查 backend 的 issue**——標題被改、issue 被刪、API 暫時失敗都不會讓對應下架（issue 的存在與標題在 channel create 的用戶端驗，見 `angareion`）。不符的留言 MUST 跳過並在註冊簿上回報（非授權作者不回報，見下），MUST NOT 影響其他留言的重建。

**寫入**：以 main 的最新版本（sha）為底；被拒 MUST 重讀重試（上限 5 次）。

**workflow 權限（複審 L1）**：`permissions:` MUST 至少 `contents: write`（commit 進 main）與 `issues: write`（在註冊簿回報留言）。

**授權**：workflow MUST 只採計 `author_association` 為 `OWNER`／`MEMBER`／`COLLABORATOR` 的留言；其他留言 MUST 忽略（不採計、不影響重建、**也不回報**——不回報才不會產生新留言）。防注入與驗證不是授權，照上面執行；不是合法 action 形狀的留言（含 workflow 的回報）MUST 忽略。

**回報**：無法套用或驗證失敗的留言（授權作者）MUST 在註冊簿上用**純文字**回報（引用那則留言；MUST NOT 是可被解析成 action 的一行 JSON——複審 L4）。

引用某一則留言的回報，若註冊簿上已經有一模一樣的本文，MUST NOT 再貼；不引用留言的固定文字回報（例如寫檔衝突）每次發生都貼。

#### Scenario: 註冊新 channel
- **WHEN** 註冊簿上有一則 `register` 留言（`X`、backend `B`、issue `N`；B 在 `backends.json`、X 不在對應表）
- **THEN** 重建後 main 的 `channels.json` 出現 `{X: {backend: B, issue: N}}`

#### Scenario: 留言格式與重建
- **WHEN** 註冊簿上有兩則合法留言（依時間序）：register `X`→B/N，之後 unregister `X`→B/N
- **THEN** 重建後 `channels.json` 沒有 `X`；兩則都被採計（每次從整串重建）

#### Scenario: 重建以先到者為準
- **WHEN** 兩則 `register` 留言都註冊 `X`、指向不同實體（先 B1/N1、後 B2/N2）
- **THEN** 重建後 `X` 指到 B1 的 N1（不覆蓋）；後到的留言被跳過，並在註冊簿上回報

#### Scenario: 兩則註冊都採計
- **WHEN** 兩則不同名字的 `register` 留言幾乎同時留下
- **THEN** 兩筆對應都在 `channels.json`（沒有因為併發遺失）

#### Scenario: 註銷
- **WHEN** 一則 `unregister` 留言指到 `X` 目前的同一個實體
- **THEN** 重建後 `X` 從 `channels.json` 移除

#### Scenario: 註銷已經沒有或指到別人
- **WHEN** 一則 `unregister` 留言的 `X` 已經不在，或（依先前留言）指到別的實體
- **THEN** 對應不變；留言在註冊簿上被回報（no-op）

#### Scenario: 留言超過一頁仍全部重建
- **WHEN** 註冊簿的留言超過 100 則（要翻頁）
- **THEN** 重建翻完所有頁；早期的 register 仍在（沒有因為只讀第 1 頁被下架）

#### Scenario: 非 OWNER／MEMBER／COLLABORATOR 的留言被忽略且不回報
- **WHEN** 一則格式合法的留言，作者 `author_association` 不是 `OWNER`／`MEMBER`／`COLLABORATOR`
- **THEN** 該留言被忽略（不進重建、不影響其他留言、也不回報——沒有新留言產生）

#### Scenario: 回報留言不會再觸發 run
- **WHEN** workflow 對一則不合格的留言回了純文字回報（作者是 `github-actions[bot]`）
- **THEN** 那則回報不會觸發新的 run（觸發層的 `if` 擋 Bot）；不會無限空轉

#### Scenario: 取消舊 run 不遺失
- **WHEN** 一次重建 run 進行中，新留言觸發的 run 把它取消（`cancel-in-progress`）
- **THEN** 新 run 從整串留言完整重建；沒有任何留言的效果遺失

#### Scenario: 被取消後下一次 run 仍正確
- **WHEN** 一次 run 被取消（還沒寫回 main），之後又一次 run（下一則留言或手動重跑）
- **THEN** 重建結果仍正確包含全部合法留言（`channels.json` 完整）

#### Scenario: 手動重跑安全
- **WHEN** 對同一串留言 `workflow_dispatch` 重跑
- **THEN** 重建結果相同（相同就不 commit）；檔案不變

#### Scenario: 樂觀鎖重試用盡
- **WHEN** 寫回 main 連續被拒 5 次
- **THEN** 不動 main；在註冊簿上回報；下一次 run 重建再試

#### Scenario: 換頁
- **WHEN** X 指到 B/N1，之後註冊簿上有一則 `rollover`：X、B、issue N2、from N1
- **THEN** 重建後 X 是 `{backend: B, issue: N2, retired: [{issue: N1, until: <那則留言的時刻>}]}`

#### Scenario: 兩則換頁搶同一頁
- **WHEN** 兩則 `rollover` 都是 from N1，先 issue N2、後 issue N3
- **THEN** 現頁是 N2；N3 記在 `retired`，帶 `side: true`；都不回報

#### Scenario: 換頁的 from 不對
- **WHEN** `rollover` 的 from 既不是現頁、也不在 `retired`；或頻道不存在；或 backend 不同
- **THEN** 對應不變，留言在註冊簿上被回報

#### Scenario: 重複的換頁
- **WHEN** 同一則 `rollover` 貼了兩次，或 issue 已經在 `retired`
- **THEN** 第二則是 no-op，不回報

#### Scenario: 註銷退役頁
- **WHEN** `unregister` 指向 X 的一張退役頁
- **THEN** 對應不變，留言被回報；`unregister` 指向現頁時整筆（含 `retired`）移除

#### Scenario: 沒換過頁的設定檔不變
- **WHEN** 註冊簿上沒有任何 `rollover`
- **THEN** 重建出的 `channels.json` 與換頁功能之前逐位元組相同

#### Scenario: 回報只貼一次
- **WHEN** 同一個註冊簿連續重建兩次，其中有一則會被回報的留言
- **THEN** 那則回報只貼一次；寫檔衝突這種固定文字的回報，兩次都發生就貼兩次

### Requirement: 讀取與 pull
angareion MUST 只讀本機複本（MyAiStorage 的 clone；路徑由個人設定 `config_dir` 給）——更新用 `git fetch`、讀檔用 `git show origin/main:<路徑>`，MUST NOT 依賴工作樹的狀態（本機改動、detached HEAD 都無妨）。新鮮度以 clone 裡 `FETCH_HEAD` 的 mtime 判斷（沒有就視為很久以前）。每個要用設定檔的指令一開始，若上次 fetch 超過 `config_pull_interval`（預設 900 秒；0＝不按週期拉）MUST 試一次 fetch：同機並行 MUST 以鎖檔避免、請求 MUST 有逾時（20 秒）；`wait` 的輪詢中 MUST NOT fetch。查無 channel 時 MUST 忽略間隔先 fetch 一次再判斷（距上次 fetch 少於 30 秒則跳過）；`channel create` 送出後 MUST 輪詢 fetch 到對應出現（上限 2 分鐘，逾時印提示、exit 0）——查無與 create 的事件驅動 fetch 在 `config_pull_interval=0` 時仍會做。fetch 失敗時 MUST 沿用目前的複本（stderr 警告；exit code 不因此改變）；複本缺失或不是合法 JSON MUST exit 2 並說明缺什麼。

#### Scenario: workflow 更新後新 channel 可用
- **WHEN** `channels.json` 在 main 更新（workflow 或 PR），讀取端下一次操作觸發 fetch
- **THEN** 對新 channel `send` 成功（stdout 印訊息 id）

#### Scenario: 複本落後時先 pull 再判斷
- **WHEN** `send --channel X` 時本機複本沒有 `X`，但 main 上有（另一個隊友剛建好）
- **THEN** angareion 忽略 `config_pull_interval` 先 fetch 一次、讀 origin/main 後找到 `X`（照常送）；fetch 後仍沒有才 exit 1「channel 不存在」

#### Scenario: 同機並行
- **WHEN** 同一台機器的兩個 angareion 指令同時觸發 fetch
- **THEN** fetch 以鎖檔序列化（第二個等第一個結束，或直接用它的結果）；兩個指令都讀到一致的最新複本

#### Scenario: fetch 逾時
- **WHEN** fetch 超過 20 秒沒有回應
- **THEN** 視同失敗：沿用目前的複本＋stderr 警告；指令照常（exit code 不變）

#### Scenario: interval 是 0
- **WHEN** `config_pull_interval=0`
- **THEN** 不按週期自動 fetch；查無 channel 與 `channel create` 的等待仍會 fetch

#### Scenario: wait 輪詢中不 fetch
- **WHEN** `wait --timeout 600` 的輪詢進行中，`channels.json` 在 main 更新
- **THEN** 輪詢期間不 fetch；下一次一般操作才拉

#### Scenario: pull 失敗
- **WHEN** 離線或 fetch 失敗，複本還在
- **THEN** 指令照常用目前的複本（stderr 有警告）；exit code 不變

#### Scenario: 本地複本缺失或格式錯
- **WHEN** 本地沒有 `channels.json`，或它是壞掉的 JSON，執行 `angareion send --channel general --body hi`
- **THEN** exit 2，stderr 說明缺什麼或格式錯；什麼都沒送

