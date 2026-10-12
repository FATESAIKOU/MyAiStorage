## Purpose

channel 名與 backend 實體的對應、以及 backend 本身的設定，放在 MyAiStorage repo 的兩個設定檔（`config/angareion/channels.json`、`config/angareion/backends.json`）；channel 的對應由**註冊簿重建 workflow**直接更新 main（本人裁定：channel 自由建立、不走 PR；2026-10-12 14:3x 改為註冊簿 issue＋整串留言重建），backend 設定走 PR；angareion 只讀本機複本（`git fetch` 讀 `origin/main`；查無先拉一次，失敗沿用）。

## ADDED Requirements

### Requirement: channel 設定檔
channel 名到 backend 實體的對應 MUST 寫在 MyAiStorage repo 的 `config/angareion/channels.json`（路徑已定案）：JSON、版本欄（`version`）、`channels` 對應表；每筆是 `{"backend": <backend 名>, "issue": <編號>}`（repo 不重寫，在 backend 設定裡）。`send`／`read`／`wait` 與 `channel create`／`channel delete` MUST 經這個檔解析；名字不在檔裡 MUST 視為 channel 不存在（exit 1）。angareion MUST NOT 直接寫這個檔（只有 workflow 寫，見下面的 requirement）。

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
`channels.json` MUST 由 MyAiStorage 的**註冊簿 issue**（長期存在，label 見 design §10）上的留言重建：`channel create`／`channel delete` MUST 各在它下面留一則一行 JSON 留言——`{"schema": 1, "action": "register"|"unregister", "channel": "<名>", "backend": "<backend 名>", "issue": <編號>}`；angareion 送出留言後 MUST NOT 等重建完成（`channel create` 會輪詢 fetch 到對應出現，見 `angareion` 的五個介面）。

workflow MUST `on: issue_comment`（限註冊簿 issue）觸發，每次 run 從整串留言依時間序**重建** `channels.json` 並 commit 進 main（結果與 main 相同就不 commit）；MUST 用 `concurrency` 併發控制且 `cancel-in-progress`（新的取消舊的）——因為每次都從整串重建，被取消的 MUST NOT 遺失任何留言的效果。

**套用規則（先到者為準）**：`register`——名字沒有對應就加上；已對應同一個實體視為成功（冪等）；已對應不同實體 MUST NOT 覆蓋，MUST 在註冊簿上回報。`unregister`——只移除指向同一個實體的對應；已經沒有、或指到別的實體 MUST NOT 改動對應，MUST 回報。

**輸入（防注入）**：留言內容 MUST 只經環境變數交給腳本，MUST NOT 用 `${{ }}` 插進 shell；JSON MUST 嚴格解析。每個留言 MUST 驗證：channel 名（`^[A-Za-z0-9_-]+$`）、backend 名存在（在 `backends.json` 裡）、實體存在（issue 在該 backend 的 repo）而且標題等於 channel 名；不符的留言 MUST 跳過並在註冊簿上回報，MUST NOT 影響其他留言的重建。

**寫入**：以 main 的最新版本（sha）為底；被拒 MUST 重讀重試（上限 5 次）。

**授權**：workflow MUST 只採計 `author_association` 為 `OWNER`／`MEMBER`／`COLLABORATOR` 的留言；其他留言 MUST 忽略（不採計、不影響重建）。防注入與驗證不是授權，照上面執行；不是合法 action 形狀的留言（含 workflow 的回報）MUST 忽略。

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

#### Scenario: issue 不存在或標題不符
- **WHEN** 一則 `register` 留言指向的 backend repo 裡沒有那個 issue 編號，或它的標題不等於 channel 名
- **THEN** 該留言被跳過（不進 `channels.json`）並在註冊簿上回報；其他留言照常重建

#### Scenario: 非 OWNER／MEMBER／COLLABORATOR 的留言被忽略
- **WHEN** 一則格式合法的留言，作者 `author_association` 不是 `OWNER`／`MEMBER`／`COLLABORATOR`
- **THEN** 該留言被忽略（不進重建、不影響其他留言）

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
