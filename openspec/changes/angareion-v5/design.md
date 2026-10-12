## Context

v0（`openspec/specs/angareion/spec.md`）的介面帶著 GitHub 的形狀：`channel create --member`、位址 `{repo, role, name?}`、channel 名直接對應 issue 標題。本人 v5 決定：channel 是通訊軟體式的英文名，介面不再出現 repo／issue；收件人用共通 agent 命名。

本人 2026-10-12 對 v5 疑點的裁定（`v5-decisions.md`；本文件的唯一依據）把 channel 的自由建立／刪除定案成三步流程，把廣播算進「寄給我的」，並要求設計處理兩個 create 的競爭、workflow 的觸發權限、pull 週期與失敗。本人 2026-10-12 14:3x 再裁定 §10：改用註冊簿 issue＋整串留言重建（`issue_comment` 觸發、`concurrency`＋`cancel-in-progress`）；授權定案：只採計 `author_association` 為 `OWNER`／`MEMBER`／`COLLABORATOR` 的留言（MyAiStorage 是 public，見 §10）。§9／§10 照此改寫；`docs/review/v5-impact.md`（sidecar worktree）的疑點清單同步更新。

## Goals / Non-Goals

**Goals：**
- 介面（參數、stdout、stderr、訊息格式）去 GitHub 化；`--member` 拿掉；`send --to` 選填（不給＝廣播）。
- 五個介面：`channel create`、`channel delete`、`send`、`wait`、`read`；channel 的建立與刪除走裁定的三步流程，對應持久化在 repo。
- 兩種設定檔（`config/angareion/channels.json`、`config/angareion/backends.json`）的位置與格式；channel 由 workflow 直接更新 main、backend 走 PR；angareion 讀本機複本（lazy pull）。

**Non-Goals：**
- 實作（只出 planning artifacts）、workflow 的 yaml、token 的實際申請與佈署；本 change 只定契約、步驟與待確認點。
- 嚴格的身分認證（v5 明說不做）。
- 群組位址（新模型裡沒有群組；`group=` 形狀視為格式錯誤）。
- 後端實作的更換（仍是 GitHub Issues，只是經設定檔指位）。
- PR #41 的後續與 v5 PR 的實際開立與分支維運（已定案：v5 以 `angareion-v0` 為 base 堆疊——見另節）。

## Decisions

### 1. 位址＝agent 名字串
`from`、`to`、個人設定檔的 `identity` 一律是 `<repo 名>-<pm|arch|impl|test>[-<id>]`（例 `MyLinuxPool-impl-2`、`LLMGateway-pm`）。**文法（審查 M7；複審 M1 修正）**：驗證用的正規式是 `^[A-Za-z0-9][A-Za-z0-9._-]*-(pm|arch|impl|test)(-[A-Za-z0-9_.]+)?$`（repo 段含 `-`）；**驗證與解析是兩步**——regex 只驗整體形狀，「由右而左取出 id 與角色字」是另外寫的解析步驟：先取可省的 `-<id>`（`[A-Za-z0-9_.]+`）、再取最右邊符合角色字的位置（`pm|arch|impl|test`），左邊剩下的都是 repo 名（例 `my-repo-impl-2` → repo `my-repo`、角色 `impl`、id `2`）。角色字只有小寫的 `pm|arch|impl|test`（大小寫有別）。`messages.validate_address` 改用這個文法；`cli.parse_address` 不再解析 `repo=`／`×`／`:` 形狀（也不再有 JSON 位址）；游標檔名（`identity_slug`）與顯示格式（`<repo×role>` → `<agent 名>`）跟著換。替代方案（保留舊形狀、另加對應表）被否決：v5 要求介面不出現 repo／issue 概念。

### 2. 無 `to`＝廣播，算進「寄給我的」（本人已確認）
`send` 不給 `--to` 時訊息沒有 `to` 欄（front matter 省略）；`wait`／`read` 不給 `--channel` 時，「寄給我的」＝`to` 是自己 agent 名 **或沒有 `to`** 的訊息。本人 2026-10-12 裁定：廣播算進「寄給我的」。理由：第一期 channel 全公開，廣播就是給所有隊員的；等信的 agent 不該漏掉它。

### 3. `--member` 整串拿掉
`channel create` 不再收 `--member`（未知參數 exit 1）；`ChannelBackend.create_channel` 簽名去掉 members；GitHub 後端不再寫／讀實體本文的 members（舊殘留的 members 欄讀取時忽略）；假後端同理。

### 4. channel 名只收英文名
`^[A-Za-z0-9_-]+$`（非空）；不合就 exit 1。`channel create`／`channel delete` 的 stdout 只印名（編號內部化）。workflow 收到註冊／註銷留言時 MUST 用同一個規則再驗一次（見 §9）。

### 5. 五個介面
對外五個介面：`channel create`、`channel delete`、`send`、`wait`、`read`（v0 的四個＋`channel delete`；本人裁定 channel 可自由建立）。介面（參數、stdout、stderr、錯誤訊息）MUST NOT 出現 GitHub repo／issue 的概念：create／delete 只印 channel 名；失敗訊息只說 channel 沒建立／沒刪掉成功，不吐後端細節。`channel delete`＝把後端實體**關掉（不刪，留紀錄）**＋註銷對應。MUST NOT 有 ack、已讀回條或限流。

### 6. `channel create` 的三步流程（本人裁定）
1. **後端開實體**（angareion）：在 backend 設定的 repo 開一個 issue（標題＝channel 名；本文不含成員）。要用的 backend：設定檔裡只有一個時就用它；多個時 MUST 用 `--backend <名>` 指定（沒給 exit 1；名不存在 exit 1）。開失敗（網路、403、5xx）→ exit 2，什麼都沒註冊。
2. **送註冊**（angareion）：留言之前 MUST 先查一次實體（GET 該 issue；存在而且標題等於 channel 名，否則 exit 2、不留留言）；然後在 MyAiStorage 的註冊簿 issue 留一則 `{action: register, channel, backend, issue}` 的一行 JSON 留言（格式見 §9／§10）。留言送出失敗時以短間隔重試 2 次（最多 3 次嘗試），仍失敗→ exit 2（第 1 步的實體留著，見 Risks）。
3. **落地**（註冊簿重建 workflow）：讀整串留言、依 §9 的規則重建，把對應 commit 進 main 的 `config/angareion/channels.json`（§10）。

- **落地等待（審查 M4）**：第 2 步送出成功後，輪詢 fetch 到對應出現（上限 2 分鐘）；出現或逾時都印 channel 名、exit 0（逾時在 stderr 印提示「還沒看到對應」）。`channel delete` 不等待（送出註銷即 exit 0）。
- **同名**：本機複本已有這個名字→ exit 1「channel 已存在」（要換就用 `channel delete` 再建）；複本可能落後，這時的權威在重建結果（同名不覆蓋＝先到者為準，§9）。**同名競爭的輸家**（兩個 create 同時通過本機檢查）在 `channel create` 端看不出來（exit 0 照常）；只有註冊簿上的回報留言看得到結果——本期不加 `--wait`（審查 M5）。

### 7. `channel delete` 的流程（本人裁定）
1. 讀本機複本找對應；查無 → exit 1（channel 不存在），什麼都不做。
2. **後端關實體（不刪）**：對對應的 backend／issue 送 `state=closed`（留紀錄）；404（實體已被刪）視同已關；其他失敗 → exit 2，不送註銷。
3. **送註銷**：在註冊簿 issue 留一則 `{action: unregister, channel, backend, issue}` 的 JSON 留言；重建時只移除指向同一個實體的對應（指到別的實體或已經沒有＝見 §9）。
- 同樣不等待；stdout 印 channel 名。

### 8. 設定檔：位置、格式、讀寫分工
- **位置（定案）**：`config/angareion/channels.json`、`config/angareion/backends.json`。
- **channels.json**（channel 名 → backend 實體）：

  ```json
  {
    "version": 1,
    "channels": {
      "general": {"backend": "github-main", "issue": 123}
    }
  }
  ```

  由註冊簿重建 workflow 維護（§9／§10）；是 channel 解析的唯一依據（查無＝channel 不存在，exit 1）；angareion MUST NOT 寫它。**這檔是機器產物**（整串留言重建的輸出）：人工改會被下一次重建還原，MUST NOT 手改（複審 L5）。
- **backends.json**（backend 本身的設定）：

  ```json
  {
    "version": 1,
    "backends": {
      "github-main": {"type": "github", "repo": "owner/repo"}
    }
  }
  ```

  `type` 目前只有 `github`（未知 type＝設定錯誤，exit 2）；更新走 PR（AiStorage PM 審查＋merge，d 等級；頻率約每個 a/b 專案一次）。
- **格式細節**：UTF-8、2 格縮排、key 排序、檔尾換行（diff 整齊）；workflow 寫入時保留未知欄位，只動自己那一筆。

### 9. workflow 的規則：重建、輸入、回報（定案；複審 H1／H2 調整）
- **觸發與重建**：workflow 在註冊簿 issue 的 `issue_comment` 上觸發（觸發層 MUST 擋 Bot，§10），另加 `schedule`（每天一次）當保險；每次 run 用 `GET /repos/{owner}/{repo}/issues/{n}/comments?per_page=100` 讀整串留言（MUST 翻完所有頁）、依時間序套用（重建）成最終對應，寫回 main 的 `channels.json`；結果與 main 相同就不 commit。重建是冪等的——手動重跑（`workflow_dispatch`）安全；被取消的 run 不遺失（下一段）。
- **留言格式（定案）**：一行 JSON——`{"schema": 1, "action": "register"|"unregister", "channel": "<名>", "backend": "<backend 名>", "issue": <編號>}`。不是這個形狀的留言（含 workflow 自己的回報）一律忽略。
- **套用規則**：`register`——名字沒有對應就加上；已對應同一個實體＝重複註冊（成功、no-op）；已對應不同實體＝**不覆蓋**（no-op）＋在註冊簿上回報。`unregister`——只移除指向同一個實體的對應；已經沒有、或指到別的實體＝no-op＋回報。先到者為準（依留言時間序）。
- **併發**：`concurrency: { group: angareion-channels, cancel-in-progress: true }`——新的取消舊的；因為每次都從整串留言重建，被取消的 run **不遺失任何效果**（下一次 run 涵蓋全部留言）。
- **寫入**：以 main 的最新版本（sha）為底；被拒就重讀重試（上限 5 次）——並行的人工編輯、backend PR 的插入也擋得住。
- **授權（定案）**：只採計 `author_association` 為 `OWNER`／`MEMBER`／`COLLABORATOR` 的留言；其他留言 MUST 忽略（不採計、不影響重建，也**不回報**——複審 H1）。重建腳本 MUST 再驗一次（手動重跑與日後重建都會重讀整串）。不另外採計 token 帳號——接上 Angareion 的機器都用本人帳號的信道 token（留言的作者身分就是本人）。
- **輸入（複審 H2 調整）**：留言內容 MUST 只經環境變數交給腳本，MUST NOT 用 `${{ }}` 插進 shell；JSON MUST 嚴格解析。重建時每個留言 MUST **只驗留言本身**：channel 名（§4 的正規式）、backend 名存在（在 `backends.json`）、`author_association` 在採計清單；**MUST NOT 查 backend 的 issue**——標題被改、issue 被刪、API 暫時失敗都不會讓對應下架（issue 的存在與標題在 channel create 的用戶端驗，§6）。不符的留言 MUST 跳過並在註冊簿上回報（非授權作者不回報，見上），MUST NOT 影響其他留言的重建。（防注入與驗證不是授權，照做。）
- **回報**：無法套用或驗證失敗的留言（授權作者），workflow MUST 在註冊簿上用**純文字**回報（引用那則留言；內容 MUST NOT 是可被解析成 action 的一行 JSON——複審 L4）；回報留言不參與重建，而且它的作者是 Bot、被觸發層擋掉、不會觸發新 run（§10）。
- 更新邏輯放 repo 內的小腳本（可單元測試；路徑實作時定），workflow 只負責觸發、權限與回報。

### 10. 註冊簿 issue、觸發與 token 權限（定案；複審 H1／M2／M3／L1／L2 調整）
- **註冊簿 issue**：MyAiStorage 上一張**長期存在**（不關閉）的 issue，label 定為 `層級:d`＋`狀態:進行中`（d 不佔 b／c 的同時進行額度；有 label 不會被列「樹外」——與「工單做完 completed 關閉」的慣例不同，見 Risks）。它是 channel 對應的請求入口：`channel create`／`channel delete` 各在上面留一則留言（格式見 §9），留言公開可稽核；置頂留言寫明格式與規則。
- **觸發**：workflow `on: issue_comment`（限這張 issue，`workflow_dispatch` 可手動重跑），觸發層 MUST 用 `if` 擋掉 Bot（`github.event.comment.user.type != 'Bot'`，或明確排除 `github-actions[bot]`）——workflow 自己的回報留言因此不會再觸發新的 run（不會空轉）。另加 `schedule`（每天一次）重建當保險（複審 M3）；drain 不需要（重建已涵蓋）。
- **workflow 的權限（複審 L1）**：`permissions:` MUST 至少 `contents: write`（commit 進 main）與 `issues: write`（在註冊簿回報留言）。
- **token 權限（定案；複審 M2）**：信道的細粒度 PAT——**Repositories** 勾「各 backend 的 repo」＋`MyAiStorage`；**Permissions**：`Issues: Read and write`（backend repo 的 Issues：讀 issue 與留言＝訊息；MyAiStorage 只用來在註冊簿 issue 留言）。不需要 Actions／Contents 等其他權限。MyAiStorage 的 clone／fetch 走**匿名 HTTPS**（public repo、不用這把 token，§11）；workflow 推送 main 用的是 repo 自己的 `GITHUB_TOKEN`，與信道 token 無關。
- **授權（定案）**：MyAiStorage 是 public（任何人都能留言），workflow MUST 只採計 `author_association` 為 `OWNER`／`MEMBER`／`COLLABORATOR` 的留言；其他留言 MUST 忽略（不採計、不影響重建，也**不回報**——不回報就不會產生新留言）。重建腳本 MUST 再驗一次（觸發層的 `if` 只擋 Bot，授權仍由腳本驗——手動重跑與日後重建都重讀整串）。
- **前置（環境）**：MyAiStorage 的 main 要允許 workflow 用 `GITHUB_TOKEN` 直推（分支保護／bypass 名單）；實作前確認一次。

### 11. 讀取與 pull（定案；審查 M4／L4）
- angareion 只讀本機複本（MyAiStorage 的 clone）。個人設定：`config_dir`（clone 路徑）、`registry_repo`（註冊簿 issue 的 `owner/repo`）、`registry_issue`（註冊簿 issue 的編號）、`config_pull_interval`（秒，預設 900；0＝不按週期拉——查無與 create 等待的事件驅動 fetch 仍會做，見下）。
- 更新用 `git fetch`、讀檔用 `git show origin/main:<路徑>`——不依賴工作樹（本機改動、detached HEAD 都無妨）；逾時 20 秒、flock 一個鎖檔避免同機並行。**clone 與 fetch 走匿名 HTTPS**（MyAiStorage 是 public、不用 token——複審 M2）。新鮮度＝clone 裡 `FETCH_HEAD` 的 mtime（沒有就視為很久以前）；第一次的 clone 由佈署做（複本不存在＝exit 2）。
- 每個要用設定檔的指令一開始，若上次 fetch 超過 `config_pull_interval` 就 fetch 一次；`wait` 的輪詢中 MUST NOT fetch。
- **查無 channel 時（審查 M4）**：忽略 `config_pull_interval`、先 fetch 一次再判斷（距上次 fetch 不到 30 秒就跳過，避免連續操作連發）；`channel create` 送出成功後輪詢 fetch 到對應出現（上限 2 分鐘），逾時印提示但 exit 0。
- fetch 失敗（離線、權限）→ stderr 警告一次、沿用目前的複本，指令照常（exit code 不因此改變）；複本不存在或 JSON 壞掉才 exit 2。也可以另外用 cron／systemd 拉（無害）。

### 12. v0→v5 的歷史訊息
讀寫都只認新形狀；舊 `{repo,role}` 形狀的留言視為非訊息忽略（與未知版本同理）。切換日前已讀過的不受影響（游標是後端位置）；要回頭看很久以前的人用 `--since` 會看到舊留言被跳過。切換日由各專案第一次設定檔 PR 決定。v0 的「同名標題取最舊」不再適用：解析只認設定檔，沒有對應就是 channel 不存在。

### 13. 文件改寫點（本 change 不動這兩個檔）
- `docs/angareion.md`：指令表四個→五個（加 `channel delete` 與 `--backend`；`--member` 字樣全清）；create／delete 的三步與落地時間差（create 送出留言後輪詢 fetch 到出現或用罄 2 分鐘、逾時提示；delete 不等待；查無 channel 先 fetch 一次）；位址寫法整節（agent 名字串、文法、`group=` 拒絕、`to` 選填）；訊息格式範例（`from`／`to` 字串、`to` 可省）；游標檔名規則；設定節（`identity` 字串；`channel_repo` 換成 `config_dir`／`registry_repo`／`registry_issue`／`config_pull_interval`；channels／backends 兩檔、註冊簿 issue 與重建 workflow）；token 節（權限範圍照 §10：各 backend repo＋MyAiStorage 的 `Issues: Read and write`）；smoke test。
- `skills/angareion/SKILL.md`：五個指令表；位址寫法（agent 名）；「不給 `--channel`」的定義加廣播；create／delete 的用法與落地時間差；回覆時 `channel`＋`--to` 的寫法。

## PR #41 與 v5 的 PR（定案 2026-10-12）

現況：PR #41（#30「Angareion v0」）已用 **merge commit `95a7641`** 合併；head branch `angareion-v0` 保留。

**定案：v5 的 PR 以 `angareion-v0` 為 base 堆疊。**
- `angareion-v5` 是從 `angareion-v0` 的 head 分出來的（v5 規劃文件，目前只有本機 commit、尚未 push）；PR 的 diff 只有 v5 的規劃 commit，#41 的 review 與驗收討論串不被牽動。
- 因為 #41 是 merge commit（不是 squash），v0 的原 commit 已在 main 上——**不需要** `git rebase --onto main`（那是 squash 的情形）。
- 前置：把 `angareion-v5` push 上去；誰 push 由 PM 指派。分支的後續（`angareion-v0` 何時刪、PR base 要不要改指 main）由 PM 決定。

## 本人未指示、作者提議（保留；向本人說明時要提）

以下不是本人的指示，是設計時作者提的（審查列出來的）；都保留，但說明時要讓本人知道：

- `channel create --backend <名>`：多個 backend 時必給（§6）。
- `config_pull_interval` 預設 900 秒（§11）。
- 註冊送出失敗重試 2 次、最多 3 次嘗試（§6）。
- workflow 的 sha 樂觀鎖重試上限 5 次（§9）。
- `config_dir`／`registry_repo`／`registry_issue` 個人設定（§11）。
- 同名 `channel create` exit 1（§6）。
- `workflow_dispatch` 手動重跑與每天一次的 `schedule` 重建（§9／§10）。
- 「issue 存在且標題等於 channel 名」的檢查放在 channel create 用戶端（§6；複審 H2 從 workflow 移過來）。
- create 的 2 分鐘等待上限、查無時 30 秒最短 fetch 間隔（§6／§11）。
- 註冊簿的置頂留言，以及 `issue_comment` 只限單一註冊簿 issue 的觸發條件（§10）。

## Risks / Trade-offs

- [workflow 直接寫 main（不經 PR）] → 沒有審查關卡：緩解＝每次變更都有 workflow run、commit 與註冊簿的留言；同名不覆蓋（先到者為準）；權限最小；backend 設定仍走 PR。
- [註冊依賴 GitHub Actions] → workflow 掛掉時對應不會落地（create 已 exit 0）：緩解＝每次 run 都從整串留言重建（下一次觸發補上；另有每天一次的 `schedule` 與手動重跑）。**run 硬失敗（腳本錯誤、讀留言 API 失敗、被手動取消）而且之後沒有新留言時，那則留言要等到下一次 `schedule` 才會套用**（複審 M3）；本機複本落後時 create 的同名檢查可能誤放行：權威在重建（先到者為準）。
- [授權降級] → 舊留言的作者若從 `OWNER`／`MEMBER`／`COLLABORATOR` 被降級，下次重建不再採計、對應會消失（複審 H2）：第一期所有留言都是本人帳號，不另外處理。
- [channels.json 被手改] → 它是重建的輸出（機器產物）：人工改會被下一次重建還原；MUST NOT 手改（§8；複審 L5）。
- [註冊簿不關閉] → 一張長期 `層級:d`＋`狀態:進行中` 的 issue（d 不佔 b／c 額度；與「工單做完 completed 關閉」的慣例不同、不會被自動關）（§10；複審 L2）。
- [create 不是原子操作] → 第 1 步成功、第 2 步失敗會留一個沒有對應的實體（孤兒）：緩解＝重試 2 次；孤兒不影響操作（解析只認設定檔）、介面不提它，之後可人工清理。
- [併發的 run 取消] → 新的 run 會取消舊的（`cancel-in-progress`）：因為每次都從整串留言重建，被取消的不遺失任何效果；寫回用 sha 樂觀鎖（§9）。
- [同名競爭的輸家] → 輸家 `channel create` exit 0、看不到衝突，只有註冊簿上的回報留言看得到結果（重建先到者為準）；不加 `--wait`（審查 M5）。
- [「寄給我的」掃所有 channel] → 不給 `--channel` 的 `wait` 每次輪詢對每個 channel 各查一次，API 用量隨 channel 數線性增加（ETag 只在單頁有效）：第一期接受（審查 L5）。
- [delete 之後的過期複本] → 別人還沒拉到新對應前，仍能對已關閉的實體送留言（GitHub 允許），訊息靜靜進死 channel；註銷重跑安全（關已關的實體是冪等，審查 L6）。
- [公開的註冊簿] → channel 名／backend／編號與留言在 MyAiStorage 是公開的、任何人可留言（授權＝只採計 `OWNER`／`MEMBER`／`COLLABORATOR` 的留言，§10）：內容與設定檔同等級、不含秘密；私人資訊 MUST NOT 進設定檔或留言。
- [pull 失敗] → 沿用舊複本：新 channel 短時間看不到；指令不受影響。
- [設定檔是單點] → 複本缺失或壞 JSON 會讓用到它的指令 exit 2：緩解＝backend 設定走 PR、channel 由 workflow 驗證後才寫、更新用 fetch 讀 origin/main（不動工作樹）。
- [舊同名 issue] → v0 的「同名標題取最舊」不再適用：舊 issue 不會被自動採用（沒有對應＝channel 不存在）。
- [channel 改名要動後端實體] → 改名＝delete＋create（舊實體留紀錄）；不做 rename 指令。
- [游標檔名改變] → 舊游標檔（`{repo}__{role}` 形）自然廢棄，新身分從頭讀一次；緩解：切換日時清掉或留著都無害。

## Open Questions

1. **註冊簿 issue 的遷移（複審 L6；第一期可接受）**：`registry_issue` 是每台機器各自填的編號；註冊簿若被關閉、換號或重建，各機器要怎麼被告知、怎麼遷移，沒定。

（§10 的授權已由本人裁定：只採計 `author_association` 為 `OWNER`／`MEMBER`／`COLLABORATOR` 的留言；PR #41 已用 merge commit `95a7641` 合併、v5 以 `angareion-v0` 為 base 堆疊——見上節。）
