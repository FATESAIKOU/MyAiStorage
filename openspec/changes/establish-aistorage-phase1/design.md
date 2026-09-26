## Context

動機見 `proposal.md` 的 Why，要求見各 `specs/`，詞彙見 `CONTEXT.md`，已定案的架構決定見 `docs/adr/0001〜0007`。

**2026-09-26 重定期 1**：期 1 的目標改成驗證 Session 的分裂（1→n）、統合（n→1）、相互參照（n↔m），扮演 AI 的是 Mac 上的 opencode。秘書與員工都不在期 1，Atelier 只做需求設計，期 1 之後的事項收在 `docs/backlog.md`。本檔原本為手機 App、worker、Atelier 所做的設計，不再是期 1 的決定；仍有參考價值的部分寫在各決定的「之後」一段，並列進待辦清單。

影響做法的現況與限制如下：

- **使用者的選擇**
  - Agora 與 Foundry 要用 **git 形式操作**，看重的是版本與回滾、熟悉的指令、跟 MyBrain 一致。
  - 儲存實體**只放 Google Drive**（消費者 5TB 方案，已經付費），不另外保留第二份副本。AiStorage 用一個**專用的 Google 帳號**（Google One 家庭共用的成員，共用同一份 5TB 配額，已由使用者建立），所以提交流程的完整 Drive 權限碰不到使用者個人的 Drive。
  - GitHub 維持免費方案。
  - 讀寫分離：讀的手段只有一個，讀的時候指定新鮮度；寫入端的機制依成本選擇、慢慢追加，先給最便宜的做法（ADR 0007）。
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
  - Mac 上有你本人的各種憑證（GitHub、Google 帳號等）。在 Mac 上直接執行、能跑任意 shell 的 AI，看得到這些憑證。
  - Mac 上的 docker 是 colima；Apple Silicon 的容器預設是 arm64，而 Actions runner 與之後的 worker 多半是 amd64。
  - worker image 沒有安裝任何 agent；MyLinuxPool 的 profile 正在另一條線重新設計。
  - 手機 App 的 Session 以單筆記錄存在 IndexedDB（OpenAI chat 格式，圖片以 base64 內嵌），沒有匯出（手機整合在待辦清單）。

## Goals / Non-Goals

**Goals:**

- 驗證分裂、統合、相互參照三種關係在這套狀態模型上都成立，而且被接續、被參考的 Session 不受影響。
- 住民與同步程式在任何情況下都**沒有**能改寫 Agora / Foundry 真本的憑證。它們的寫入全部經過單一提交者，所以「刪不掉歷史」與「產生者章可信」是靠憑證本身做到的。
- 讀取端只有一個讀取介面，指定新鮮度，形狀不隨寫入機制改變。
- 期 1 不新增任何常駐服務：提交流程借 GitHub Actions 按需執行。

**Non-Goals:**

- 不保證寫入即時可見。期 1 只提供最便宜的寫入機制，新鮮度以分鐘到小時計；讀取端會看到快照時間與警告，自己決定怎麼用。
- 不防護「AiStorage 專用帳號」或「提交流程的憑證」被盜用。使用者選擇只放 Drive，這個風險明示接受；影響範圍限於 AiStorage（見 Risks）。
- 不做跨 Session 的即時通訊，也不做 LLMGateway 的隱私分級。
- 不做手機 App、worker、Atelier 的實作（見 `docs/backlog.md`）。

## Decisions

### D1. 儲存實體：Agora、Foundry 以 git-annex 存進 Drive

| 要素 | 真本放哪 | git 介面 |
|---|---|---|
| MyBrain | 維持現狀（GitHub private repo） | 一般 git |
| Agora | AiStorage 專用帳號的 Google Drive 上的一個 git-annex repo | `git-remote-annex` 加 rclone special remote：歷史以 git bundle 存、大檔以 annex 物件存，**全部在 Drive** |
| Foundry | 同一個專用帳號的 Google Drive 上另一個 git-annex repo | 同 Agora |
| Atelier | 期 1 不建（需求設計見 `docs/atelier/`） | — |

Agora 與 Foundry 分成兩個 repo，是依 ADR 0001「要素各自獨立」。兩者共用的只有提交流程（D2）這個實作，沒有共用介面。

**替代方案：**
- AWS S3：保證最直接，但不是 git 形式，而且每月多 1〜3 美元。使用者不採用。
- GitHub 存 git、Drive 存大檔的混合做法：使用者要求儲存實體只放 Drive，不採用。
- Linode：金鑰擋不住刪除舊版本。不採用。

**之後**：Atelier 原本的方向是 GitHub private repo（純文字、小檔、一般 git），留在待辦清單，等 MLP 的 profile 重新設計後一起重新考慮。

### D2. 單一提交者：所有寫入先進收件匣，再由唯一的提交流程收進真本

```
 Mac 容器裡的 opencode ─┐                ┌──────────── 提交流程（GitHub Actions）───────────┐
 （之後：手機 App、worker）┼─ 放進 Drive ──▶│ 1. 驗證收件匣項目（格式、必填 metadata、授權）     │
                        ┘  上自己的收件匣 │ 2. 依來源收件匣蓋產生者章                          │
                                          │ 3. 原始紀錄 → 閱讀版（每個來源應用一個轉換器）       │
                                          │ 4. 處理交接單與認領、參考、改寫提案、產出登錄         │
                                          │ 5. git-annex add / commit，push 到 Drive 上的 repo  │
                                          │ 6. 發佈讀取視圖與搜尋索引（D5）                      │
                                          └───────────────────────────────────────────────┘
```

- **提交流程是唯一能 push 的角色。** 它的憑證（專用帳號的完整 Drive 權限）只放在 GitHub Actions secrets。Drive 上的 repo 永遠只有一個 push 的人，所以不會發生同時 push 互相覆蓋。
- **管理操作也要跟提交流程錯開。** 抹除、回滾、復原會直接改寫並重推 repo，所以管理腳本先停用提交流程的 workflow、確認沒有執行中的 run，做完再恢復。否則兩邊同時 push，被覆蓋的可能正是抹除本身。但住民的 token 有 Actions 寫入權，可能也能重新啟用 workflow（技術驗證 1.6 確認），所以不能只靠開關：**提交流程與管理腳本在 push 前都重讀遠端的 git-remote-annex manifest，跟 clone 時不同就中止**。Drive 沒有條件寫入，這不是原子的，但能把競爭窗口壓到秒級，而且不依賴 workflow 的開關。健康檢查回報提交流程有沒有被停用。
- **收件匣項目的處理。**
  - 一個收件匣項目是不可分的單位：原始紀錄先寫，sidecar 最後寫並記著原始紀錄的內容雜湊；只看到一半的項目留到下一輪。
  - push 成功之後才刪除已處理的收件匣項目；重跑時靠內容雜湊保持冪等。
  - 同一次執行裡先收原始紀錄、再收交接單與認領，所以接續點可以跟交接單同一批進來，一次接續只需要一次提交流程。
  - 被拒收的項目與逾時的孤兒項目（只有原始紀錄、沒有 sidecar），在拒絕原因發佈後 24 小時由提交流程刪除並永久刪除；抹除的範圍也涵蓋收件匣。
  - 拒絕原因發佈在讀取視圖（寫入者用自己的讀取身分讀），不寫回收件匣，因為 `drive.file` 的寫入者看不到提交流程建立的檔案。
  - 收件匣是空的就直接結束，不 clone repo，省 Actions 分鐘。
- **workflow 的輸入。** 住民能任意觸發 workflow（ADR 0006），所以 `workflow_dispatch` 不接受會被帶進 shell 的自由字串輸入。
- **觸發方式有兩種**，都屬於寫入端的機制（D9）：
  - 定時執行，預設每 3 小時一次，可以調整。GitHub 的 schedule 會延遲、負載高時甚至跳過，所以這是「通常」而不是保證；健康檢查記錄實際間隔。
  - 寫入者主動觸發（workflow_dispatch）：「同步並提交」走這條。
  - 同一時間只允許一個提交流程在跑（concurrency group）。GitHub 的 group 最多一個執行中、一個等待中，第三次觸發會取消原本等待中的那個；因為每次執行都會掃完整個收件匣，被取消不會漏收，但觸發者不能以「自己那個 run」判斷完成。
  - **「同步並提交」的完成**以讀取介面看得到這次放進收件匣的每一個項目、或它的拒絕原因為準（spec「寫入端維持新鮮度」）。
- **產生者章。** 每個 profile 的收件匣只有它自己寫得進去（D3），所以「項目來自哪個收件匣」就是可信的產生者。收件匣以資料夾 id 辨識，不以名稱辨識（Drive 允許同名）。寫入者自己填的產生者一律忽略。
- **改寫與抹除。**
  - 改寫：寫入者把改寫提案放進收件匣，提交流程做成一個新 commit。舊版本留在 git 歷史裡，可以 revert。
  - 抹除：只有你在 Mac 上用管理憑證執行（不在 opencode 的容器裡）。步驟是 `git annex drop --force`、`git annex forget`、改寫歷史，然後重新推送 bundle。AI 發現機敏內容時，只能提出改寫把它遮蔽掉，並提醒你抹除。
- **為什麼放 GitHub Actions：** 它在雲端，跟 AiContainer 與 Mac 都無關，符合「不寄居 AiContainer」與「互不為前提」；不用維運常駐機器；而且它是免費方案的一部分。

**替代方案：**
- 每個寫入者各自一個 repo：寫入即時，但 AI 能毀掉自己 repo 的歷史；之後的手機也仍需要有人代為 commit。不採用。
- 大家直接 push 同一個 repo：同時 push 時會悄悄丟資料。不採用。
- 在 Gateway VPS 跑提交程式：Gateway 設計成隨時可以整台丟棄，而且會碰到「VPS 不是拿來開服務」的立場。不採用。

### D3. 身分的實現：每個 profile 一組限權憑證；期 1 由你手動安裝

**期 1 的 profile：**

| profile | 寫：Drive 收件匣 | 讀：讀取視圖／git 歷史 | GitHub |
|---|---|---|---|
| Mac opencode（`mac-opencode`：Mac 容器裡的 opencode 與同步器） | 自己專屬的 OAuth client，`drive.file` scope，只碰得到自己建的檔案 | 自己專屬的 service account，被分享成 reader | fine-grained token：MyAiStorage repo 只有 Actions 寫入權（只用來觸發提交流程），沒有 contents 權限 |
| 測試用 profile | 另一個 OAuth client，`drive.file` | 另一個 service account | — |
| 提交流程 | 專用帳號的完整 Drive 權限 | — | — |
| 你（管理） | 專用帳號 | — | 你的帳號 |

- **每個 profile 各自一個讀取身分**（service account）。這樣讀取的撤銷也以 profile 為單位，讀取介面也分得出呼叫者。期 1 的讀取授權以要素為單位（全有或全無）；之後要做內容層級的讀取授權（例如 LLMGateway 的隱私 tag），讀取視圖就依可見範圍分資料夾（擴張點）。
- **收件匣的建立**：由該 profile 自己的 client 建立收件匣資料夾，建好之後才把「資料夾 id ↔ profile」登記進授權規則（tasks 2.3）。收件匣一律以資料夾 id 辨識。

- **憑證的證明與發放。** 拿到哪組憑證，就等於屬於哪個 profile。期 1 由你本人把 Mac opencode 的憑證放在 Mac 上的固定位置（repo 之外，權限 600），並記進 `docs/resources.md`（只記名稱與位置）。之後改由 MyLinuxPool 發放（工單見 `docs/tickets/mylinuxpool.md`）。
- **opencode 在容器裡執行。** Mac 上直接執行的 AI 能跑任意 shell，看得到你本人在 Mac 上的所有憑證；那樣「住民沒有能改寫真本的憑證」就不成立。所以期 1 的 opencode 與同步器在 Mac 上的 Ubuntu 容器（colima）裡執行。容器看得到自己的憑證（擋不住），但那組憑證的上限是：在自己的收件匣裡建檔、讀取、觸發提交流程。它碰不到真本，所以毀不掉歷史。這個容器的形狀跟之後的 worker 相同，可以直接沿用。
- **容器裡允許的憑證是一份白名單**：Mac opencode profile 的憑證；opencode 要用的 LLM provider 金鑰（沒有它 opencode 不能工作，外洩的影響是額度，不是 AiStorage）；需要時一把限定單一測試 repo 的 fine-grained token。白名單以外的都不掛載。
- **每個分岔各自一個容器。** 驗收分裂、統合、相互參照時，每個 Session 在各自的容器裡執行，工作目錄與 opencode 的本機資料都不共用（可以共用同一組 profile 憑證）。接手的一方唯一的資訊來源是讀取介面，這樣才驗證得到「狀態在執行體之外」。
- **撤銷的單位是 profile**：要讓某個 profile 失去存取能力，就整組輪替該 profile 的憑證。
- 期 1 用長期憑證。改成短效憑證是之後的擴張點。

**替代方案：**
- opencode 直接在 Mac 上執行：最省事，但它看得到你的 GitHub 與 Google 憑證，能力邊界只剩文字約定。不採用。
- 住民直接持有 Drive 真本的寫入權：任何能寫的 token 都能刪檔。不採用。
- 所有 profile 共用一組憑證：產生者章就失去意義。不採用。

**之後**：worker/default（員工）與手機 App（秘書）的 profile 沿用同一個形狀：各自一個 `drive.file` 的 OAuth client、service account reader、fine-grained token（員工另加 MyBrain 唯讀）。手機在裝置上用原生 AuthorizationClient 登入（WebView 內禁止 OAuth）。見 `docs/backlog.md`。

### D4. 同步器：寫入端只負責上傳，格式轉換集中在提交流程

- 每個來源應用各有一個同步器，只做三件事：
  1. 把原始紀錄（來源應用自己的匯出單位）上傳到收件匣。
  2. 附上 metadata sidecar，內容包括項目 id、來源應用、狀態（與停止時間）、所屬案件、快照時間、原始紀錄的內容雜湊。
  3. 需要時觸發提交流程（同步並提交）。
- 閱讀版的轉換器放在提交流程裡，每個來源應用一個。「閱讀版可以從原始紀錄重建」就等於在提交流程上重跑一次轉換器。之後加一個來源應用，就是加一個同步器加一個轉換器（spec「每個來源應用一個同步器」）。
- **快照時間**由同步器在擷取原始紀錄時記下；提交流程以收件匣檔案在 Drive 上的建立時間當上限檢查它，晚於上限的一律以上限為準。
- **期 1 的同步器**：opencode，在 Mac 的容器內定期對有變化的 Session 執行 `opencode export`（可行性、位置是否穩定、壓縮對話之後的行為、停止訊號、Session id 的取得，由技術驗證 1.7 確認）。
- **項目 id**：`<來源應用>:<來源應用自己的 Session id>`（細節在 tasks 2.1 定案）。重新同步或手動匯入同一個 Session，都對到同一個 id。
- **停止中**：opencode 沒有可信的結束訊號（1.7 確認），所以期 1 的停止中由你或 AI 明確宣告（skill 提供指令），sidecar 記下停止時間。同步器偵測到宣告停止後又有新內容，就立刻同步並提交一次讓它回到運作中；在那之前約一次提交流程耗時的窗口內，讀者可能讀到停止時的內容而沒有警告。
- 上傳重複的內容時，要以內容雜湊判斷為同一份，不產生新 commit（spec「定期同步」）。
- **期 1 的轉換器**：opencode；Claude Code（只用在單一 Session 手動匯入，Claude Code 的 Session 大約 30 天就會被本機自動清除，需要時要能及時救進來）。
- 閱讀版的共通格式要在寫任何轉換器之前先定下來（tasks 2.4），所有轉換器都照同一份格式輸出。

**之後**：手機 App 的同步器由 MyAiEntry 實作，上傳時要排除 App 另存的 SSH 私鑰、LLM key、PAT；worker 的 opencode 同步器在刪除 worker 前由 MyLinuxPool 跑最後一次。見 `docs/backlog.md`。

### D5. 讀取：每個要素一個讀取介面，背後讀「讀取視圖」

- **讀取介面**是讀者取得內容的唯一手段，形式是函式庫加 CLI，要能在 arm64 與 amd64 上跑。Agora 的讀取介面負責找 Session（篩選、全文）、讀 Session（metadata、閱讀版、兩個方向的 Session Link、交接單）、列出尚未被認領的交接單；Foundry 的讀取介面負責查產出目錄與取得收容產出。兩者各自獨立（ADR 0001），但遵守同一套新鮮度規則（D9）。
- **背後讀的是讀取視圖**：每次提交後，提交流程把讀取視圖以一般檔案發佈到 Drive 上的另一個資料夾，內容是每個 Session 的 metadata、閱讀版、Session Link（含反向索引）與交接單、各項目的快照時間、拒絕原因、產出目錄、搜尋索引。讀取介面用該 profile 自己的 service account 讀取。讀者不需要知道背後是讀取視圖還是 git。
- **讀取視圖增量發佈**：只重寫有變動的檔案，另外發佈一份帶世代號的 manifest；讀取介面依世代號快取，沒變就不重新下載。Drive 大約每秒只能處理 2 個檔案，整份重建會讓提交時間隨 Session 數線性成長。
- **讀取介面以檔案 id 定位**：讀取視圖的 manifest 是一個**固定檔案 id、原地更新**的檔案，它的 id 跟著 profile 的讀取設定一起發放；其他檔案以 manifest 裡記的檔案 id 取得。整條讀取路徑都不以名稱搜尋，避免同名檔案被混進讀取路徑（Drive 允許同名；所有 client 都以同一個專用帳號授權，owner 欄位分不出是誰建的）。
- **收容產出的本體**：讀取介面用 service account 依 annex key 直接從 rclone special remote 取物件（不開 chunk，key 對應到 Drive 路徑的方式由技術驗證 1.5 確認），不另存一份可讀副本。
- **門檻**：搜尋索引超過 50 MB、或讀取視圖超過 5,000 個檔案時，「期 1 資料量小、整份下載索引」的前提失效，要改成分片或雲端查詢（健康檢查回報這兩個數字）。
- **git 歷史**：你（管理）與復原作業可以用 service account 的 reader 權限 `git clone annex::…`，用 `log / diff / show` 看歷史。這不是住民的讀取手段。
- **搜尋**
  - 提交流程每次提交後重建一個 SQLite FTS5 索引（trigram 分詞，能處理中日文），放進讀取視圖。
  - 讀取介面下載索引到本地查詢：篩選用 metadata 欄位，全文用閱讀版。期 1 資料量小，下載成本低。
  - 之後可以把後端換成語意搜尋或雲端查詢，查詢的形狀不變（spec「搜尋後端可以替換或疊加」）。

**之後**：手機 App（MyAiEntry，Capacitor／TypeScript）也要用同一個讀取介面，所以實作語言的提案要考慮它能不能有 TypeScript 版的用戶端，或共用同一份查詢規格。

### D6. Atelier：期 1 只做需求設計

- 期 1 的產出是 `docs/atelier/` 裡的需求設計：需要哪些部門（profile）、需要哪些職能（職務），以及原本 spec 的職務與外部副本需求（已移到 `docs/atelier/role.md`、`docs/atelier/external-copy.md`）。
- 不做基本設計，也不建 repo。部門的需求會交給 MyLinuxPool 的 profile 重新設計那條線參考。
- ADR 0003（授權依 profile，職務不參與授權）與 ADR 0004（Atelier 複製外部 skill）仍然有效，作為之後設計的前提。

### D7. Foundry（最小）：產出目錄在 git，收容產出經提交流程轉手

- Foundry repo 內容：產出目錄是每件產出一個 metadata 檔；收容產出的本體以 annex 物件存在 Drive。
- 原處產出只登錄出處，也就是連結型項目，記錄產出它的 Session。AI 產出後把登錄項目放進收件匣。
- 期 1 的收容產出一律經提交流程轉手，**單檔上限先訂 100 MB**（簡報、圖片、報告都夠用，也遠低於 Actions runner 的磁碟與時間限制）。超過上限的明確拒收，原因發佈在讀取視圖。
- 數 GB 的檔案要改走「寫入者直接上傳以內容雜湊命名的物件，提交流程只登錄這個雜湊」的路徑，連同它的技術驗證（原 tasks 1.7）都在待辦清單。

### D8. MyBrain 銜接

- 所屬案件是 MyBrain 案件的 id，AiStorage 一律當成不透明的字串，不驗證格式、不要求找得到。
- MyBrain 那邊為案件主題檔加上不變 `id`、更新 OKF 規範與 `validate.py` 的 PR 在待辦清單，由那個 PR 定欄位名與格式。
- MyBrain 的寫入仍然只走既有的 `/mybrain-write`。

**之後**：員工拿 MyBrain 的唯讀 fine-grained token；也可以把 D2 的提案流程延伸到 MyBrain：員工提案，由提交流程開 PR，在免費方案下守住「AI 不能 merge」。

### D9. 新鮮度：讀取指定，寫入端依成本維持（ADR 0007）

- **讀取端**：每次讀取 MAY 帶一個新鮮度要求（最多落後多久）。每筆結果都附快照時間。最新已提交的內容達不到要求時，照樣回傳，附上「未達新鮮度」的警告與實際快照時間。讀取 MUST NOT 觸發同步或提交流程。停止中的 Session（依可信訊號或明確宣告，D4），只要最新快照在它停止之後，就視為符合任何要求。
- **寫入端**：一個 Session 能維持多新，由寫入端的機制決定。期 1 只提供最便宜的一組：
  1. 同步器定期同步有變化的 Session，預設每 10 分鐘（只花 Drive 上傳，不花 Actions 分鐘）。
  2. 提交流程定時執行，預設每 3 小時。
  3. 寫入者主動「同步並提交」：同步後觸發提交流程，等到讀取介面看得到自己這份快照。寫交接單前一定要做；AI 想讓並行的 Session 讀到自己的進度時也可以做。
- 所以期 1 的新鮮度大致是：沒人主動提交時，通常最多落後約 3 小時（schedule 會延遲）；主動提交之後，約等於一次提交流程的耗時（由技術驗證 1.8 量測）。
- **之後追加的寫入機制**（依成本由低到高）：縮短定時間隔、上傳後自動觸發提交、合併多次觸發、自架 runner。追加任何一種都不改變讀取介面。

### D10. 接續經由交接單與認領：分裂與統合

- **交接單是 Agora 的項目**，有自己的 id，記著被接續的 Session、接續點與交接內容。接續 Link 在新 Session **認領**交接單時才建立，方向是「新 Session → 被接續的 Session」。理由：分裂的當下，接手的 Session 還不存在、也沒有 id；而且接續只能由持有者發起，才不會出現「讀者觸發別人寫入」（ADR 0007）。
- **分裂（1→n）**：持有者 S1 同步，為每一份工作各寫一張交接單，一起提交（接續點可以跟交接單同一批進來，只需要一次提交流程）。之後開新的 opencode Session（各自的容器），各自認領一張。
- **統合（n→1）**：每個分岔的持有者（S2、S3）各自同步、寫一張交接單交出自己的末端並提交；新 Session S4 認領全部。S4 從頭到尾不要求 S2、S3 做任何事，所以同一套語意之後可以延伸到跨裝置（手機的 S2 加 worker 的 S3）。
- **一張交接單只能被認領一次**；重複的認領由提交流程拒絕，並把原因發佈在讀取視圖。尚未被認領的交接單可以經由讀取介面列出。因為判定是非同步的，認領之後要立刻同步並提交，等讀取介面確認這條 Link 屬於自己才開工；看到拒絕就停下。
- **認領需要知道自己的 Session id**：opencode 的 skill 能不能取得目前的 Session id、能不能用指定內容啟動新 Session，由技術驗證 1.7 確認。不成立的話，回到步驟 3 跟使用者重新設計。
- **預留欄位**：交接單、接續 Link 與 Session metadata 先保留選填的 `role`、`role_version` 擴充欄位（之後 Atelier 要求接續指定職務、Session 記錄職務版本），期 1 不驗證。

## Risks / Trade-offs

- [git-annex＋rclone＋Drive 的組合沒有在你的 Drive 實測過] → 期 1 的第一個任務就是技術驗證：clone、push、revert、抹除、同時觸發。不過就退回重新選型。
- [git-remote-annex 每次 push 多一個增量 bundle，提交流程每次都在全新的 runner 上 clone，耗時會隨 push 次數與資料量成長] → 技術驗證 1.8 用合成資料（500 次 push、1 GB）量測，並估算每月 Actions 分鐘數；收件匣是空的就不 clone。結果不理想時，把 Actions cache 或定期重整 bundle 列入選型，並注意它們會多出抹除要處理的副本。
- [管理操作（抹除、回滾、復原）與提交流程同時 push] → 管理腳本先停用提交流程的 workflow、確認沒有執行中的 run，做完再恢復（D2）。
- [只放 Drive：專用帳號或提交流程的憑證外洩，就能永久刪除 AiStorage 的全部資料；Drive 的垃圾桶與舊版本只保留 30 天] → 使用者明示接受。緩解做法：AiStorage 放在專用帳號，影響範圍不含你個人的 Drive；提交流程的憑證只放 Actions secrets；住民只有 `drive.file` 與 reader 權限；每個 git clone 本身都是一份完整歷史，可以當非正式的備份（但它也是抹除碰不到的地方，抹除時要一併處理）。之後要加第二份副本時，git-annex 的 numcopies 就是現成的擴張點。
- [抹除之後，內容仍留在 Drive 自己的舊 revision 與垃圾桶，或既有的 clone 與快取裡] → 技術驗證 1.3 確認 Drive 的 revision 與垃圾桶都找不到（必要時刪檔重建而不是覆寫，再永久刪除）；抹除腳本列出已知的 clone 與快取提醒你處理。
- [`drive.file` 的可見範圍到底是以 OAuth client 還是以 GCP project 為單位，文件沒有講清楚；也沒講清楚能不能在別人建立的資料夾（別的收件匣、repo 資料夾、讀取視圖資料夾）裡建檔] → 技術驗證 1.4 逐一確認。讀取視圖那一側一律以固定 id 的 manifest 定位（D5）。**如果 `drive.file` 的 client 能在 repo 資料夾建檔，而技術驗證找不到 rclone／git-remote-annex 那一側的對策，就是 no-go**（每個 profile 各開 GCP project 只解決 client 之間的可見性，解決不了這個）。
- [opencode 沒有可信的「停止」訊號] → 停止中只能明確宣告，不從閒置時間推測（D4）；宣告後又有新內容就回到運作中，讀者會重新看到新鮮度警告。
- [Apple Silicon（arm64）上驗證過的東西，不一定能直接搬到 amd64 的 worker 與 runner] → 實作語言的提案要回答兩種架構都能跑；技術驗證記下每個工具在兩種架構上的安裝方式與版本。
- [Mac 上的 AI 看得到你本人的憑證] → opencode 與同步器在容器裡執行，只掛載白名單上的憑證（D3）；抹除等管理操作不在容器裡做。
- [`opencode export` 的位置可能不穩定：重新匯出或壓縮對話之後，訊息位置可能改變，接續點就會指錯] → 技術驗證 1.7 確認。不穩定的話，接續點改以訊息 id 表示，或由同步器保存每次匯出的完整快照；結論回到步驟 3 跟使用者決定。
- [OAuth client 停在 Testing 狀態時，refresh token 7 天就過期；refresh token 6 個月沒用也會失效；rclone 內建的共用 client 2026 年內會停用] → 自己建 OAuth client，並設成已發佈（以個人使用的例外處理，不送驗證）；再加一個定期使用的健康檢查。
- [Actions 分鐘數（每月 2,000 分鐘，跟 MyLinuxPool 共用）] → 定時頻率壓低，預設每 3 小時；需要時寫入者才主動觸發；每次執行都記錄耗時，定期看用量。不夠時改用自架 runner（擴張點）。
- [新鮮度受限於最便宜的寫入機制：相互參照時讀到的可能是幾小時前的內容] → 讀取端一定看得到快照時間與警告，不會誤以為是最新；需要時由寫入的一方主動同步並提交。真的不夠用時，再依成本追加寫入機制（D9），讀取介面不變。
- [Drive API 大約每秒只能處理 2 個檔案] → 用 git bundle 與 annex 分塊，把檔案數量壓低；轉換與索引一次批次處理。
- [抹除要改寫 git 歷史並重推 bundle，步驟複雜] → 寫成管理腳本，並用驗證測試保證抹除後的內容在目前版本、舊版本和讀取視圖裡都找不到。
- [Mac opencode 的 token 有 Actions 寫入權，可以觸發這個 repo 的任何 workflow，也能刪 workflow 的執行紀錄] → MyAiStorage repo 裡每個 workflow 都必須設計成被任意觸發也安全；稽核紀錄另外寫在提交流程自己的 commit 裡，不依賴 Actions 的執行紀錄。

## Migration Plan

- 這是新系統，沒有既有資料要遷移。歷史 Session 不整批回補，需要時再用單一 Session 手動匯入。
- 上線順序：
  1. 技術驗證。
  2. 共通 metadata schema、收件匣格式、閱讀版格式。
  3. 提交流程加 Agora 路徑（opencode、Claude Code 轉換器）。
  4. 讀取視圖與讀取介面（含新鮮度）。
  5. Mac 容器裡的 opencode：同步器、同步並提交、分裂／統合／參照用的 skill。
  6. Foundry（最小）。
  7. Atelier 需求設計、MyLinuxPool 工單。
  8. 端到端驗收：分裂、統合、相互參照。
- **回退**：任何一步失敗都只影響 AiStorage 自己。來源應用原本的本機紀錄不受影響，所以停用 AiStorage 不會讓任何既有功能壞掉。

## Open Questions

- 定時提交的頻率（預設每 3 小時）與同步器的同步間隔（預設每 10 分鐘），要看實際的 Actions 用量再調，不影響 spec 與任務拆分。
- 閱讀版共通格式的細節（欄位命名、圖片怎麼表示）在 tasks 2.4 定案，時間點在寫任何轉換器之前，不影響介面。
- 接續點的表示方式（訊息序號或訊息 id），依技術驗證 1.7 的結果決定。
