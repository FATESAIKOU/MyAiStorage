## Context

動機見 `proposal.md` 的 Why，要求見各 `specs/`，詞彙見 `CONTEXT.md`，已定案的架構決定見 `docs/adr/0001〜0006`。影響做法的現況與限制如下：

- **使用者的選擇**
  - Agora 與 Foundry 要用 **git 形式操作**，看重的是版本與回滾、熟悉的指令、跟 MyBrain / Atelier 一致。
  - 儲存實體**只放 Google Drive**（消費者 5TB 方案，已經付費），不另外保留第二份副本。
  - GitHub 維持免費方案。
- **Google Drive（研究：`docs/research/drive-and-github.md`）**
  - 沒有條件寫入，也沒有鎖；同一個資料夾允許同名檔案。
  - 以使用者身分行動的 token（不論 scope）可以永久刪除它看得到的檔案。
  - 舊版本最多保留 30 天或 100 版。
  - service account 不能在消費者 Drive 擁有檔案，但被分享成 reader 時，就是一把只能讀單一資料夾的憑證。
  - `drive.file` scope 只看得到「這個 client 自己建的檔案」。
- **git on Drive（研究：`docs/research/git-on-drive.md`）**
  - 唯一還在維護、能把整個 repo 存進 Drive 的是 git-annex 內建的 `git-remote-annex`，搭配 rclone special remote。git-annex 約每月發版，最新是 10.20260901。
  - 官方明寫：**同時 push 會讓其中一次悄悄被覆蓋**。
- **GitHub 免費方案**
  - private repo 沒有分支保護；有 contents 寫入權的 token 可以推 main、force push、merge。
  - fine-grained token 可以做到單一 repo 唯讀。
  - Actions 每月 2,000 分鐘（跟 MyLinuxPool 共用）。
- **現有系統（盤點結果）**
  - 手機 App 的 Session 以單筆記錄存在 IndexedDB（OpenAI chat 格式，圖片以 base64 內嵌），沒有匯出。
  - worker image 沒有安裝任何 agent，也沒有任何憑證。
  - MyLinuxPool 的 profile 有 `secrets` 注入機制（目前是空的）。

## Goals / Non-Goals

**Goals:**

- 住民與同步程式在任何情況下都**沒有**能改寫 Agora / Foundry / Atelier 真本的憑證。它們的寫入全部經過單一提交者，所以「刪不掉歷史」與「產生者章可信」是靠憑證本身做到的。
- 讀取端分兩種：能跑 git 的讀者可以用 git 讀歷史；不能跑 git 的讀者（手機）讀一般檔案。
- 期 1 不新增任何常駐服務：提交流程借 GitHub Actions 按需執行。

**Non-Goals:**

- 不保證寫入即時可見。寫入是非同步的，接續前的等待時間以分鐘計。
- 不防護「你本人的 Google 帳號」或「提交流程的憑證」被盜用。使用者選擇只放 Drive，這個風險明示接受（見 Risks）。
- 不做跨 Session 的即時通訊，也不做 LLMGateway 的隱私分級。

## Decisions

### D1. 儲存實體：Atelier 放 GitHub；Agora、Foundry 以 git-annex 存進 Drive

| 要素 | 真本放哪 | git 介面 |
|---|---|---|
| MyBrain | 維持現狀（GitHub private repo） | 一般 git |
| Atelier | GitHub private repo | 一般 git（純文字、小檔） |
| Agora | Google Drive 上的一個 git-annex repo | `git-remote-annex` 加 rclone special remote：歷史以 git bundle 存、大檔以 annex 物件存，**全部在 Drive** |
| Foundry | Google Drive 上另一個 git-annex repo | 同 Agora |

Agora 與 Foundry 分成兩個 repo，是依 ADR 0001「要素各自獨立」。兩者共用的只有提交流程（D2）這個實作，沒有共用介面。

**替代方案：**
- AWS S3：保證最直接，但不是 git 形式，而且每月多 1〜3 美元。使用者不採用。
- GitHub 存 git、Drive 存大檔的混合做法：使用者要求儲存實體只放 Drive，不採用。
- Linode：金鑰擋不住刪除舊版本。不採用。

### D2. 單一提交者：所有寫入先進收件匣，再由唯一的提交流程收進真本

```
 手機 App ──┐                 ┌──────────── 提交流程（GitHub Actions）───────────┐
 worker ────┼─ 放進 Drive 上 ──▶│ 1. 驗證收件匣項目（格式、必填 metadata）          │
 Mac 同步 ──┘   自己的收件匣    │ 2. 依來源收件匣蓋產生者章                          │
                               │ 3. 原始紀錄 → 閱讀版（每個來源應用一個轉換器）       │
                               │ 4. git-annex add / commit，push 到 Drive 上的 repo  │
                               │ 5. 更新搜尋索引與讀取視圖（D5）                      │
                               │ 6. Atelier 提案：跑 judge，通過才推進 Atelier repo   │
                               └───────────────────────────────────────────────┘
```

- **提交流程是唯一能 push 的角色。** 它的憑證（完整 Drive 權限、Atelier repo 寫入權）只放在 GitHub Actions secrets。Drive 上的 repo 永遠只有一個 push 的人，所以不會發生同時 push 互相覆蓋。
- **觸發方式有兩種。**
  - 定時執行，預設每 3 小時一次，可以調整。
  - 寫入者主動觸發（workflow_dispatch）。接續前同步、Atelier 提案都走這條，觸發者會等它完成。
  - 同一時間只允許一個提交流程在跑（concurrency group），第二個排隊等。
- **產生者章。** 每個 profile 的收件匣只有它自己寫得進去（D3），所以「項目來自哪個收件匣」就是可信的產生者。寫入者自己填的產生者一律忽略。
- **改寫與抹除。**
  - 改寫：寫入者把改寫提案放進收件匣，提交流程做成一個新 commit。舊版本留在 git 歷史裡，可以 revert。
  - 抹除：只有你在 Mac 上用管理憑證執行。步驟是 `git annex drop --force`、`git annex forget`、改寫歷史，然後重新推送 bundle。
- **為什麼放 GitHub Actions：** 它在雲端，跟 AiContainer 無關，符合「不寄居 AiContainer」與「互不為前提」；不用維運常駐機器；而且它是免費方案的一部分。

**替代方案：**
- 每個寫入者各自一個 repo：寫入即時，但手機仍需要有人代為 commit，AI 也能毀掉自己 repo 的歷史。不採用。
- 大家直接 push 同一個 repo：同時 push 時會悄悄丟資料。不採用。
- 在 Gateway VPS 跑提交程式：Gateway 設計成隨時可以整台丟棄，而且會碰到「VPS 不是拿來開服務」的立場。不採用。

### D3. 身分的實現：每個 profile 一組限權憑證，由 MyLinuxPool 注入

| profile | 寫：Drive 收件匣 | 讀：讀取視圖／git 歷史 | GitHub |
|---|---|---|---|
| worker/default（員工） | 自己專屬的 OAuth client，`drive.file` scope，只碰得到自己建的檔案 | service account，被分享成 reader | fine-grained token：MyBrain 與 Atelier 唯讀，MyAiStorage repo 只有 Actions 寫入權（只用來觸發提交流程） |
| 手機 App（秘書） | 自己的 OAuth client，`drive.file`，在裝置上用原生 AuthorizationClient 登入（WebView 內禁止 OAuth） | 同上 | 同上 |
| Mac 同步程式 | 自己的 OAuth client，`drive.file` | 同上 | — |
| 提交流程 | 完整的 Drive 權限 | — | Atelier repo 寫入權 |
| 你（管理） | 你的帳號 | — | 你的帳號 |

- 「MyLinuxPool 證明 profile 歸屬」，實際做法是 **worker 建立時，MyLinuxPool 用 profile 的 `secrets` 注入該 profile 的憑證**。拿到哪組憑證，就等於屬於哪個 profile。刪掉 worker，憑證就跟著消失。profile 本身的憑證可以整組輪替。
- 員工看得到自己的憑證（能跑任意 shell，所以擋不住）。但那組憑證的上限是：在自己的收件匣裡建檔、讀取、觸發提交流程。它碰不到真本，所以毀不掉歷史。
- 期 1 用長期憑證。改成短效憑證是之後的擴張點。

**替代方案：**
- 住民直接持有 Drive 真本的寫入權：任何能寫的 token 都能刪檔。不採用。
- 所有 profile 共用一組憑證：產生者章就失去意義。不採用。

### D4. 同步器：寫入端只負責上傳，格式轉換集中在提交流程

- 每個來源應用各有一個上傳器，只做三件事：
  1. 把原始紀錄（來源應用自己的匯出單位）上傳到收件匣。
  2. 附上 metadata sidecar，內容包括 Session id、來源應用、狀態、所屬案件、職務版本。
  3. 需要時觸發提交流程。
- 閱讀版的轉換器放在提交流程裡，每個來源應用一個。「閱讀版可以從原始紀錄重建」就等於在提交流程上重跑一次轉換器。之後加一個來源應用，就是加一個上傳器加一個轉換器（spec「每個來源應用一個同步器」）。
- **期 1 的上傳器**
  - opencode：在 worker 內定期對有變化的 Session 執行 `opencode export`。worker 刪除前，由 MyLinuxPool 的刪除流程再跑最後一次，並等提交流程完成。
  - 手機 App：由 MyAiEntry 實作，定期上傳變動過的 Session 記錄。上傳時要排除 App 另存的 SSH 私鑰、LLM key、PAT。
- 上傳重複的內容時，要以內容雜湊判斷為同一份，不產生新 commit（spec「定期同步」）。

### D5. 讀取：git 讀者用 clone，手機讀「讀取視圖」

- **能跑 git 的讀者**（員工、你）：用 service account 的 reader 權限 `git clone annex::…`，就能用 `log / diff / show` 看歷史；需要時再 `git annex get` 取大檔。
- **不能跑 git 的讀者**（手機）：每次提交後，提交流程把「讀取視圖」以一般檔案發佈到 Drive 上的另一個資料夾。內容是每個 Session 的 metadata 與閱讀版、產出目錄、搜尋索引。手機用 service account 的 reader 權限讀取。
- **搜尋**
  - 提交流程每次提交後重建一個 SQLite FTS5 索引（trigram 分詞，能處理中日文），放進讀取視圖。
  - 讀者下載索引到本地查詢：篩選用 metadata 欄位，全文用閱讀版。期 1 資料量小，下載成本低。
  - 之後可以把後端換成語意搜尋或雲端查詢，查詢的形狀不變（spec「搜尋後端可以替換或疊加」）。

### D6. Atelier：GitHub private repo＋提案流程

- 目錄結構：`roles/<職務>/{know,do,judge,dont}/`，加上一份能力需求宣告；外部副本放在 `vendor/<來源>/`，並記錄出處與版本。
- **職務版本就是 Atelier repo 的 commit。** 員工啟動時載入某個 commit 的職務內容，並把 commit id 寫進自己 Session 的 metadata。
- **修改流程**
  1. 員工把改動（patch）放進自己的收件匣，並觸發提交流程。
  2. 提交流程在隔離的工作目錄套用改動，跑該職務 judge 裡的驗證腳本。
  3. 通過才推進 Atelier repo 的 main；失敗就記下原因，main 不動（spec「員工可改職務，但要過 judge」）。
  4. 回滾是由提交流程或你做 revert commit。
- **外部副本比對**：期 1 由人或 AI 手動觸發一個比對 workflow，比的是內容，不是版本。結果分成一致、有落差、上游已消失三類，有落差的產生更新提案。
- 員工的 GitHub token 對 Atelier 只有唯讀，沒有能力直接推 main。

### D7. Foundry：產出目錄在 git，收容產出的大檔放 annex

- Foundry repo 內容：產出目錄是每件產出一個 metadata 檔；收容產出的本體以 annex 物件存在 Drive。
- 原處產出只登錄出處，也就是連結型項目。員工產出後把登錄項目放進收件匣。
- 數 GB 的影片如果經過 Actions runner 轉手，會受到 runner 磁碟與執行時間的限制。所以大檔改走「寫入者直接上傳以內容雜湊命名的物件，提交流程只登錄這個雜湊」。這條路要在技術驗證時確認（見 Risks）。

### D8. MyBrain 銜接

- 案件主題檔的 frontmatter 加上不變的 `id`，並更新 OKF 規範與 `validate.py`。這項變更以 MyBrain PR 進行，走它自己的 CI。
- 員工拿 MyBrain 的唯讀 fine-grained token。MyBrain 的寫入仍然只走既有的 `/mybrain-write`。
- 期 2 可以把 D2 的提案流程延伸到 MyBrain：員工提案，由提交流程開 PR。這樣在免費方案下，「AI 不能 merge」也守得住。

## Risks / Trade-offs

- [git-annex＋rclone＋Drive 的組合沒有在你的 Drive 實測過] → 期 1 的第一個任務就是技術驗證：clone、push、revert、抹除、大檔、同時觸發。不過就退回重新選型。
- [只放 Drive：你的 Google 帳號或提交流程的憑證外洩，就能永久刪除全部資料；Drive 的垃圾桶與舊版本只保留 30 天] → 使用者明示接受。緩解做法：提交流程的憑證只放 Actions secrets；住民只有 `drive.file` 與 reader 權限；每個 git clone 本身都是一份完整歷史，可以當非正式的備份。之後要加第二份副本時，git-annex 的 numcopies 就是現成的擴張點。
- [`drive.file` 的可見範圍到底是以 OAuth client 還是以 GCP project 為單位，文件沒有講清楚] → 技術驗證時確認手機的 client 看不到 worker 建的檔案。不成立的話，每個 profile 各開一個 GCP project。
- [OAuth client 停在 Testing 狀態時，refresh token 7 天就過期；refresh token 6 個月沒用也會失效；rclone 內建的共用 client 2026 年內會停用] → 自己建 OAuth client，並設成已發佈（以個人使用的例外處理，不送驗證）；再加一個定期使用的健康檢查。
- [Actions 分鐘數（每月 2,000 分鐘，跟 MyLinuxPool 共用）] → 定時頻率壓低，預設每 3 小時；有需要的寫入者才主動觸發；每次執行都記錄耗時，定期看用量。不夠時改用自架 runner（擴張點）。
- [寫入是非同步的：接續前要等提交流程跑完，預估 1〜2 分鐘] → 使用者已經接受。觸發者要等待並顯示進度，逾時要明確告知。
- [Actions runner 處理數 GB 的影片會碰到磁碟與執行時間上限] → 大檔走「寫入者直接上傳雜湊命名物件，提交流程只登錄」（D7），在技術驗證時確認可行。
- [Drive API 大約每秒只能處理 2 個檔案] → 用 git bundle 與 annex 分塊，把檔案數量壓低；轉換與索引一次批次處理。
- [抹除要改寫 git 歷史並重推 bundle，步驟複雜] → 寫成管理腳本，並用驗證測試保證抹除後的內容在目前版本、舊版本和讀取視圖裡都找不到。
- [員工的 token 有 Actions 寫入權，可以觸發這個 repo 的任何 workflow，也能刪 workflow 的執行紀錄] → MyAiStorage repo 裡每個 workflow 都必須設計成被任意觸發也安全；稽核紀錄另外寫在提交流程自己的 commit 裡，不依賴 Actions 的執行紀錄。

## Migration Plan

- 這是新系統，沒有既有資料要遷移。歷史 Session 不整批回補，需要時再用單一 Session 手動匯入。
- 上線順序：
  1. 技術驗證。
  2. 共通 metadata schema 與收件匣格式。
  3. 提交流程加 Agora 的 opencode 路徑。
  4. 讀取視圖與搜尋。
  5. Atelier 與提案流程。
  6. Foundry。
  7. 外部專案：MyLinuxPool 的 worker 裝 opencode、注入憑證、刪除前同步；MyAiEntry 的上傳器與交接；MyBrain PR。
- **回退**：任何一步失敗都只影響 AiStorage 自己。來源應用原本的本機紀錄不受影響，所以停用 AiStorage 不會讓任何既有功能壞掉。

## Open Questions

- 定時提交的頻率，預設每 3 小時。要看實際的 Actions 用量再調，不影響 spec 與任務拆分。
- 閱讀版的共通格式細節（欄位命名、圖片怎麼表示），在實作轉換器時定，不影響介面。
