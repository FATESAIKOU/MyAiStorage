## Context

v0（`openspec/specs/angareion/spec.md`）的介面帶著 GitHub 的形狀：`channel create --member`、位址 `{repo, role, name?}`、channel 名直接對應 issue 標題。本人 v5 決定：channel 是通訊軟體式的英文名，介面不再出現 repo／issue；收件人用共通 agent 命名。

本人 2026-10-12 對 v5 疑點的裁定（`v5-decisions.md`；本文件的唯一依據）把 channel 的自由建立／刪除定案成三步流程，把廣播算進「寄給我的」，並要求設計處理兩個 create 的競爭、workflow 的觸發權限、pull 週期與失敗。本文件照裁定改寫；`docs/review/v5-impact.md`（sidecar worktree）的疑點清單同步更新。

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
`from`、`to`、個人設定檔的 `identity` 一律是 `<repo 名>-<pm|arch|impl|test>[-<id>]`（例 `MyLinuxPool-impl-2`、`LLMGateway-pm`）。**文法（審查 M7 定案）**：`^[A-Za-z0-9][A-Za-z0-9._]*-(pm|arch|impl|test)(-[A-Za-z0-9_.]+)?$`；解析由右而左——先取可省的 `-<id>`（`[A-Za-z0-9_.]+`）、再取角色字，左邊剩下的都是 repo 名（repo 名裡的 `-` 靠右側的角色字解析，例 `my-repo-impl-2` → repo `my-repo`、角色 `impl`、id `2`）；角色字只有小寫的 `pm|arch|impl|test`（大小寫有別）。`messages.validate_address` 改用這個文法；`cli.parse_address` 不再解析 `repo=`／`×`／`:` 形狀（也不再有 JSON 位址）；游標檔名（`identity_slug`）與顯示格式（`<repo×role>` → `<agent 名>`）跟著換。替代方案（保留舊形狀、另加對應表）被否決：v5 要求介面不出現 repo／issue 概念。

### 2. 無 `to`＝廣播，算進「寄給我的」（本人已確認）
`send` 不給 `--to` 時訊息沒有 `to` 欄（front matter 省略）；`wait`／`read` 不給 `--channel` 時，「寄給我的」＝`to` 是自己 agent 名 **或沒有 `to`** 的訊息。本人 2026-10-12 裁定：廣播算進「寄給我的」。理由：第一期 channel 全公開，廣播就是給所有隊員的；等信的 agent 不該漏掉它。

### 3. `--member` 整串拿掉
`channel create` 不再收 `--member`（未知參數 exit 1）；`ChannelBackend.create_channel` 簽名去掉 members；GitHub 後端不再寫／讀實體本文的 members（舊殘留的 members 欄讀取時忽略）；假後端同理。

### 4. channel 名只收英文名
`^[A-Za-z0-9_-]+$`（非空）；不合就 exit 1。`channel create`／`channel delete` 的 stdout 只印名（編號內部化）。workflow 收到註冊請求時 MUST 用同一個規則再驗一次（見 §9）。

### 5. 五個介面
對外五個介面：`channel create`、`channel delete`、`send`、`wait`、`read`（v0 的四個＋`channel delete`；本人裁定 channel 可自由建立）。介面（參數、stdout、stderr、錯誤訊息）MUST NOT 出現 GitHub repo／issue 的概念：create／delete 只印 channel 名；失敗訊息只說 channel 沒建立／沒刪掉成功，不吐後端細節。`channel delete`＝把後端實體**關掉（不刪，留紀錄）**＋註銷對應。MUST NOT 有 ack、已讀回條或限流。

### 6. `channel create` 的三步流程（本人裁定）
1. **後端開實體**（angareion）：在 backend 設定的 repo 開一個 issue（標題＝channel 名；本文不含成員）。要用的 backend：設定檔裡只有一個時就用它；多個時 MUST 用 `--backend <名>` 指定（沒給 exit 1；名不存在 exit 1）。開失敗（網路、403、5xx）→ exit 2，什麼都沒註冊。
2. **送註冊**（angareion）：把 `{action: register, channel, backend, issue}` 送去本體；傳遞方式見 §10（**待本人確認**；建議＝註冊 issue）。送出失敗時以短間隔重試 2 次（最多 3 次嘗試），仍失敗→ exit 2（第 1 步的實體留著，見 Risks）。
3. **落地**（MyAiStorage 的 `register-channel` workflow）：驗證請求（§9 的規則）、把對應寫進 main 的 `config/angareion/channels.json`、在請求上看得到結果。

- **落地等待（審查 M4）**：第 2 步送出成功後，輪詢 pull 直到對應出現（上限 2 分鐘）；出現或逾時都印 channel 名、exit 0（逾時在 stderr 印提示「還沒看到對應」）。`channel delete` 不等待（送出註銷即 exit 0）。
- **同名**：本機複本已有這個名字→ exit 1「channel 已存在」（要換就用 `channel delete` 再建）；複本可能落後，這時的權威在 workflow（同名不覆蓋，§9），衝突會留在註冊請求上。**同名競爭的輸家**（兩個 create 同時通過本機檢查）在 `channel create` 端看不出來（exit 0 照常）；只有註冊請求上的留言看得到結果——本期不加 `--wait`（審查 M5）。

### 7. `channel delete` 的流程（本人裁定）
1. 讀本機複本找對應；查無 → exit 1（channel 不存在），什麼都不做。
2. **後端關實體（不刪）**：對對應的 backend／issue 送 `state=closed`（留紀錄）；404（實體已被刪）視同已關；其他失敗 → exit 2，不送註銷。
3. **送註銷**：`{action: unregister, channel, backend, issue}` 給本體；`unregister-channel` workflow 只移除指向同一個實體的對應（指到別的實體或已經沒有＝見 §9）。
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

  由 `register-channel`／`unregister-channel` workflow 維護；是 channel 解析的唯一依據（查無＝channel 不存在，exit 1）；angareion MUST NOT 寫它。
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

### 9. workflow 的規則：授權、輸入、併發、冪等（審查 H2／M5／M8 定案）
兩層：workflow 之間、以及可能在不同機器的兩個 create。
- **workflow 序列化**：`concurrency: { group: angareion-channels, cancel-in-progress: false }`（register 與 unregister 同一組）。常見的「兩個同時」＝一 running、一 pending，依序處理。
- **pending 會被之後的新 run 取代**（GitHub 的行為）：三個以上同時到達時，中間那個 pending 會被取消——正確性不能只靠 concurrency，靠下面的 drain 補救。每個 run 的更新是「重讀 main 的最新版本（含該版本的 sha）→ 只改自己那一筆 → 寫回帶 sha；被拒就重讀重試（上限 5 次）」：就算兩個 run 真的同時跑，兩筆對應都不會遺失；並行的人工編輯、backend PR 的插入也擋得住。
- **drain（審查 M5）**：每次 run（issue 事件、`schedule`、手動）都從頭掃「尚未完成」的請求（開放中、帶 label、沒有完成記號），逐一處理——被 GitHub 取消或被漏掉的請求由下一次 run 帶走。另加 `on: schedule` 定時 sweep 當保險（頻率實作定，例如 15 分鐘）。
- **授權（審查 H2；定案）**：只處理「授權帳號開立」或「授權帳號加上 label」的請求——驗 `github.event.sender` 在授權名單（放 repo 變數，不寫死）；`author_association` 只當輔助，不單獨當充分條件。不在名單的請求 MUST NOT 動檔、MUST NOT 留完成記號。
- **輸入（審查 H2；定案）**：請求的內容（標題、本文 JSON）MUST 只經環境變數傳給腳本，MUST NOT 用 `${{ }}` 插進 shell；JSON MUST 嚴格解析。寫入前 MUST 先驗：channel 名（§4 的正規式）→ backend 名存在（在 `backends.json`）→ 實體存在（issue 在該 backend 的 repo）而且標題等於 channel 名。任何一項不符 MUST 在請求上回報、MUST NOT 動檔。
- **同名不覆蓋**：名字已有對應而且指到不同實體 → 不改檔，在註冊請求上回報「名字已被使用」；指到同一實體＝重複註冊 → 視為成功（冪等）。
- **完成記號與冪等（審查 M8；格式定案）**：workflow 在請求上留的留言就是記號——第一行固定 `angareion-wf: done`（成功，或依規則視為成功）或 `angareion-wf: rejected`（規則不符、不動檔），後面帶 action／channel／backend／issue 與結果。有記號的請求 MUST 跳過（drain 與重跑都安全）；沒有記號的開放請求＝還沒落地，看得出來。結果確定（done／rejected）的請求順手關閉；暫時性失敗（網路、5xx）不留記號、留著開放，sweep 會再試。
- 更新邏輯放 repo 內的小腳本（可單元測試；路徑實作時定），workflow 只負責觸發、權限與回報。

### 10. 觸發 workflow 的方式與權限（比較；待本人確認）
angareion 要讓 MyAiStorage 的 workflow 跑起來。信道 token 目前只有 Issues 權限；三個做法：

| 方式 | angareion 需要的權限 | 新 secret？ | 觸發與失敗回報 |
|---|---|---|---|
| A. `workflow_dispatch`（REST 直接呼叫） | 對 MyAiStorage 的 **Actions: write**（fine-grained） | 要：現有 token 加選 MyAiStorage 時，權限會套用到它全部已選的 repo（後端 repo 也拿到 Actions: write，過度授權）；不然就另開一把，多一個 secret（MyLinuxPool 的佈署與輪替要跟著改） | 呼叫當下回 204，失敗碼當場看得到；落地失敗另靠 run 的結果 |
| B. `repository_dispatch`（REST） | 對 MyAiStorage 的 **Contents: write** | 同上（另開一把；同一把就是所有已選 repo 都拿到 Contents: write＝可改 repo 內容，不建議） | 同 A；payload 是 JSON |
| C. **issue 事件（建議）** | 對 MyAiStorage 的 **Issues: write**（和現用權限同一類） | 不用：沿用現有 token，只要它的 repo 涵蓋 MyAiStorage（沒有就加選；不新增權限種類） | GitHub 自己觸發（通常數十秒）；成功與失敗都留在註冊 issue 上（workflow 留言、關閉） |

- **建議 C**：權限最小、不用第二把 token、稽核與失敗追蹤掛在註冊 issue 上。成本：每個 create／delete 在 MyAiStorage 多一個註冊 issue（內容＝channel 名、backend 名、編號，與設定檔同等級、不含秘密）。做法：angareion 在 registry repo 開一個帶 label 的 issue（標題 `[channel] register <名>`／`[channel] unregister <名>`，本文放 `{action, channel, backend, issue}` 的 JSON）；`register-channel`／`unregister-channel` workflow `on: issues`（`opened`／`labeled`）過濾那個 label 後處理。label 需先在 repo 建好（tasks 會列）。
- **C 的落點成本（審查 M6；待本人確認）**：註冊 issue 開在 MyAiStorage 會產生沒有 `層級:` label 的 issue——依 repo 的工單規則，MyPMO 每天的檢查會把它列為「樹外」並通知本人。替代：(1) 註冊 issue 開在專用的 registry repo（`channels.json` 在 MyAiStorage，workflow 需跨 repo 寫，權限要評估）；(2) 一個長期存在、帶正確 label 的「註冊簿」issue，每次請求是一則留言（`issue_comment` 觸發；授權改用留言作者判斷，其餘規則不變）。
- **待本人確認（決定後才開工）**：
  1. 選 A／B／C 哪一個（建議 C）。
  2. 選 C 時：現有 token 的 repo 清單是否含 MyAiStorage；沒有就加選（權限仍是 Issues）。
  3. MyAiStorage 的 main 是否允許 workflow 用 `GITHUB_TOKEN` 直推（分支保護／bypass 名單）——裁定是「直接更新 main」，被擋就要調整設定或另議。
  4. 選 A／B 時：新 token 由本人產出，MyLinuxPool 的 secret 佈署要跟著改。
  5. 選 C 時：註冊落點（M6）——MyAiStorage 的註冊 issue（樹外工單通知）／registry repo／單一註冊簿 issue＋`issue_comment`。
- 不採用：GitHub App（跨 repo 的安裝權限，第一期不需要）、把更新改成 PR（違反裁定）、angareion 自己 commit 檔案（同樣違反裁定且權限更大）。

### 11. 讀取與 pull（定案；審查 M4／L4）
- angareion 只讀本機複本（MyAiStorage 的 clone）。個人設定：`config_dir`（clone 路徑）、`registry_repo`（註冊 issue 的 `owner/repo`）、`config_pull_interval`（秒，預設 900；0＝不按週期拉——查無與 create 等待的事件驅動 fetch 仍會做，見下）。
- 更新用 `git fetch`、讀檔用 `git show origin/main:<路徑>`——不依賴工作樹（本機改動、detached HEAD 都無妨）；逾時 20 秒、flock 一個鎖檔避免同機並行。新鮮度＝clone 裡 `FETCH_HEAD` 的 mtime（沒有就視為很久以前）；第一次的 clone 由佈署做（複本不存在＝exit 2）。
- 每個要用設定檔的指令一開始，若上次 fetch 超過 `config_pull_interval` 就 fetch 一次；`wait` 的輪詢中 MUST NOT fetch。
- **查無 channel 時（審查 M4）**：忽略 `config_pull_interval`、先 fetch 一次再判斷（距上次 fetch 不到 30 秒就跳過，避免連續操作連發）；`channel create` 送出成功後輪詢 fetch 到對應出現（上限 2 分鐘），逾時印提示但 exit 0。
- fetch 失敗（離線、權限）→ stderr 警告一次、沿用目前的複本，指令照常（exit code 不因此改變）；複本不存在或 JSON 壞掉才 exit 2。也可以另外用 cron／systemd 拉（無害）。

### 12. v0→v5 的歷史訊息
讀寫都只認新形狀；舊 `{repo,role}` 形狀的留言視為非訊息忽略（與未知版本同理）。切換日前已讀過的不受影響（游標是後端位置）；要回頭看很久以前的人用 `--since` 會看到舊留言被跳過。切換日由各專案第一次設定檔 PR 決定。v0 的「同名標題取最舊」不再適用：解析只認設定檔，沒有對應就是 channel 不存在。

### 13. 文件改寫點（本 change 不動這兩個檔）
- `docs/angareion.md`：指令表四個→五個（加 `channel delete` 與 `--backend`；`--member` 字樣全清）；create／delete 的三步與落地時間差（create 送出後輪詢到出現或用罄 2 分鐘、逾時提示；delete 不等待；查無 channel 先 fetch 一次）；位址寫法整節（agent 名字串、文法、`group=` 拒絕、`to` 選填）；訊息格式範例（`from`／`to` 字串、`to` 可省）；游標檔名規則；設定節（`identity` 字串；`channel_repo` 換成 `config_dir`／`registry_repo`／`config_pull_interval`；channels／backends 兩檔與 workflow）；token 節（權限範圍依 §10 的裁定更新）；smoke test。
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
- `config_dir`／`registry_repo` 個人設定（§11）。
- 註冊 issue＋label 的傳遞方式（A／B／C 是作者提的，已標待本人確認；§10）。
- 同名 `channel create` exit 1（§6）。

## Risks / Trade-offs

- [workflow 直接寫 main（不經 PR）] → 沒有審查關卡：緩解＝每次變更都有 workflow run、commit 與註冊 issue 的留言；同名不覆蓋；權限最小；backend 設定仍走 PR。
- [註冊依賴 GitHub Actions] → workflow 掛掉時對應不會落地（create 已 exit 0）：緩解＝drain（每次 run 掃未完成的請求）＋`schedule` sweep，重跑冪等；本機複本落後時 create 的同名檢查可能誤放行：權威在 workflow 的同名不覆蓋。
- [create 不是原子操作] → 第 1 步成功、第 2 步失敗會留一個沒有對應的實體（孤兒）：緩解＝重試 2 次；孤兒不影響操作（解析只認設定檔）、介面不提它，之後可人工清理。
- [併發的 pending 取消] → 三個以上同時註冊時，中間的 pending run 會被 GitHub 取消：緩解＝drain（下一次 run 掃未完成的請求、冪等）＋`schedule` sweep；已執行 run 的更新不會被另一個 run 蓋掉（sha 樂觀鎖，§9）。
- [同名競爭的輸家] → 輸家 `channel create` exit 0、看不到衝突，只有註冊請求上的留言看得到結果；不加 `--wait`（審查 M5）。
- [「寄給我的」掃所有 channel] → 不給 `--channel` 的 `wait` 每次輪詢對每個 channel 各查一次，API 用量隨 channel 數線性增加（ETag 只在單頁有效）：第一期接受（審查 L5）。
- [delete 之後的過期複本] → 別人還沒拉到新對應前，仍能對已關閉的實體送留言（GitHub 允許），訊息靜靜進死 channel；註銷重跑安全（關已關的實體是冪等，審查 L6）。
- [公開 repo 的設定檔與註冊 issue] → channel 名／backend／編號在 MyAiStorage 是公開的：內容與設定檔同等級、不含秘密（本人已選這個落點）；私人資訊 MUST NOT 進設定檔或註冊請求。
- [pull 失敗] → 沿用舊複本：新 channel 短時間看不到；指令不受影響。
- [設定檔是單點] → 複本缺失或壞 JSON 會讓用到它的指令 exit 2：緩解＝backend 設定走 PR、channel 由 workflow 驗證後才寫、更新用 fetch 讀 origin/main（不動工作樹）。
- [舊同名 issue] → v0 的「同名標題取最舊」不再適用：舊 issue 不會被自動採用（沒有對應＝channel 不存在）。
- [channel 改名要動後端實體] → 改名＝delete＋create（舊實體留紀錄）；不做 rename 指令。
- [游標檔名改變] → 舊游標檔（`{repo}__{role}` 形）自然廢棄，新身分從頭讀一次；緩解：切換日時清掉或留著都無害。

## Open Questions

1. **觸發方式與權限（本人確認）**：A／B／C 三選一（建議 C＝issue 事件）；含 token 的 repo 涵蓋、main 的分支保護與 C 的落點（M6），見 §10。

（PR #41 已定案：已用 merge commit `95a7641` 合併、v5 以 `angareion-v0` 為 base 堆疊，見上節。）
