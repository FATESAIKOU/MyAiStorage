## Context

動機見 `proposal.md` 的 Why，要求見各 `specs/`，詞彙見 `CONTEXT.md`，已定案的架構決定見 `docs/adr/0001〜0008`。

**2026-09-26 重定期 1**：期 1 的目標改成驗證 Session 的分裂（1→n）、統合（n→1）、相互參照（n↔m），扮演 AI 的是 Mac 上的 opencode。秘書與員工都不在期 1，Atelier 只做需求設計，期 1 之後的事項收在 `docs/backlog.md`。本檔原本為手機 App、worker、Atelier 所做的設計，不再是期 1 的決定；仍有參考價值的部分寫在各決定的「之後」一段，並列進待辦清單。

**2026-09-27 技術驗證結果併入**：第 1 組（tasks 1.1〜1.8）的結果與使用者的決定已寫進本檔（報告 `docs/spike/report.md`，決定見 `docs/decision-log.md`「Spike 1.4f decision」「Spike 1.9 go decision」）。影響最大的一項：`drive.file` 的 client 能在任何知道 id 的資料夾建檔（1.4），所以「住民寫不進真本」不再靠 Drive 的權限做到，改成「偵測＋隔離＋釘選」保證真本的完整性（ADR 0008）；產生者章改靠每個 profile 的簽章金鑰（ADR 0006 修訂）。

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
  - `drive.file` scope 只看得到「這個 client 自己建的檔案」；但**只要知道資料夾 id，就能在別人的資料夾裡建檔**（技術驗證 1.4），讀、改、刪別人的檔則會被擋。同一個 GCP project 裡的 client 之間完全不隔離，可以互相讀、改、刪。
  - Drive 會為上傳的檔案提供 `sha256Checksum`（1 GB 的檔案也有，技術驗證 1.4f5），不必下載就能驗內容。
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
- 住民與同步程式在任何情況下都**沒有**能改寫或刪除 Agora / Foundry 真本的憑證，它們的寫入全部經過單一提交者。它們**仍然能在 repo 資料夾裡建檔**（技術驗證 1.4，使用者已接受的例外，ADR 0008），所以真本的完整性由提交流程的「偵測＋隔離＋釘選」保證：注入最多造成偵測得到的提交暫停，不會讓偽造的歷史被當成真本。產生者章靠每個 profile 的簽章金鑰（ADR 0006）。
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

**部署規則（技術驗證 1.2、1.5）：**
- special remote 用 `type=rclone`（git-annex 10.20260717 起的語法），`encryption=none`、不開 chunk。所有 rclone conf（提交流程、讀取身分、管理）都設 `root_folder_id` 指向 AiStorage 的根資料夾，`rcloneprefix` 用相對路徑。
- clone 一律用**完整 URL**（repo uuid 加上 `rcloneremotename`、`rcloneprefix`；push 時印出的 URL 少了這兩個參數，不能直接用），一律 `clone -b main`，clone 之後先 `git annex init`，再設 `annex.max-git-bundles`（本地設定，每個 clone 都要設）。每個 repo 的完整 URL 記在 `docs/resources.md` 與 6.4 的復原手冊。
- git-annex 每次 push 都把 GITMANIFEST 刪掉重建，所以它的 file id 每次都會變；manifest 裡以 `-` 開頭的行是「已被 consolidate 取代」的 bundle。rclone 讀唯讀掛載的 conf 時會因寫不回 token 而報錯，所以啟動時先複製一份可寫的暫存 conf。

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

- **提交流程是唯一能 push 的角色。** 它的憑證（專用帳號的完整 Drive 權限）只放在 GitHub Actions secrets。Drive 上的 repo 只有它 push，所以不會發生同時 push 互相覆蓋。
- **住民寫得進 repo 資料夾，所以提交流程每一輪都先確認真本沒被動過（ADR 0008）。** 住民的 `drive.file` client 只要知道資料夾 id，就能在 repo 資料夾裡建同名的 GITMANIFEST、GITBUNDLE（技術驗證 1.4f），讓 clone 失敗，甚至讓全新 clone 拿到偽造的歷史。資料夾與檔案 id **視為公開資訊**（讀取身分本來就列得到），任何安全性都不建立在 id 保密上。對策是：
  - **信任錨點＝釘選值**，存在一個**獨立的 repo**（`MyAiStorage-pin`，只放 `.pin/`，不放任何 workflow）。提交流程用一把只對這個 repo 有寫入權的 deploy key（放在 Actions secrets）寫入，所以整個提交流程維持**單一 job**（每個 job 各自進位到整分鐘，拆 job 會讓每次提交多 2 分鐘以上），MyAiStorage repo 的 `GITHUB_TOKEN` 維持 `contents: read`，rerun 舊版的提交流程也改不了 main repo 的程式碼。住民的 token 對 pin repo 沒有任何權限。釘選值記：全部 ref 的 hash（ref 集合要完全相同）、GITMANIFEST 的內容 sha256、manifest 的 active bundle 清單與已移除 bundle 清單、`git-annex` 分支裡這個 remote 記錄的 annex key 集合。可信集合的**唯一來源是釘選值**，不從列舉、也不從重放推導。
  - **兩階段釘選**：push 之前寫「待定」，push 並驗證成功之後轉「正式」。下一輪發現遠端等於「待定」就直接轉正，等於「正式」就丟棄待定，兩者都不是就中止（fail-closed）。
  - **釘選值只由提交流程的 job，依它自己觀測到的遠端狀態寫入**。不得有任何可以被觸發、又能接受 refs、雜湊或狀態作為輸入的寫入路徑（技術驗證 1.4f4：那種 workflow 會讓住民直接偽造釘選值）。抹除之後重建釘選值，由管理者在 Mac 上直接 push 到 pin repo，不經過 Actions。
  - **清掃**：以內容判定，而且只看 Drive 的 metadata（`sha256Checksum`），不下載（技術驗證 1.4f5：1.8 等級的 repo 在 runner 上約 3 秒）。同名的 GITMANIFEST 只保留內容雜湊等於正式或待定釘選值的那一個；`.bak` 只能等於目前或上一個正式 manifest 的內容雜湊，而且只保留一個（push 完成後它等於目前版本，替換過程中等於上一版）；bundle 只保留 active 清單裡、名稱雜湊與 `sha256Checksum` 相符的；annex 物件只保留釘選值 key 集合裡的。其餘的移到隔離資料夾。逐層檢查 repo 前綴每一層的同名資料夾，遞迴清掃子資料夾。**讀不到就中止、不做任何移動**：只有確實拿到 metadata、確實不符時才隔離。任何一次移動失敗也中止這一輪。`sha256Checksum` 缺少時，對新出現的檔下載驗證一次並記住結果。
  - **bundle 回收**：manifest 裡的「已移除」清單依 file id **永久刪除**（它們已被 consolidate 取代；刪除之後 git-remote-annex 的 clone、push、consolidate 是否仍正常，在 3.2 驗證）；不在 active、也不在已移除清單裡的，才當作疑似注入而隔離。這樣 repo 資料夾的檔案數與儲存量有上限，也跟抹除的後置條件一致。第一次回收時要處理大量舊 bundle（1.8 的 repo 約 508 個，約 3〜4 分鐘，一次性）。
  - **隔離資料夾**：保存 7 天供人工檢查，之後永久刪除；計入配額監控與抹除範圍。
- **每一輪的固定順序**（任何一步失敗都中止這一輪，收件匣項目留到下一輪）：
  1. 檢查 `github.sha == main HEAD` 與 `github.ref == refs/heads/main`（防止用 rerun 舊 run 或其他分支執行舊版 workflow，技術驗證 1.6h4）。
  2. 掃描收件匣；收件匣是空的（只算形狀符合收件匣項目格式的檔）就直接結束，不 clone。
  3. **結算待定釘選值**（全程唯讀，不做任何移動）：有待定時，下載 manifest 引用的 active bundle 做內容重放；重放出的 refs 等於待定，就先轉正（同時記下新 manifest 的內容雜湊與 active、已移除清單）；等於正式就丟棄待定；兩者都不是就中止。待定是在 push 之前寫的，只有 refs、沒有新 manifest 的雜湊，所以必須先結算再清掃，否則上一輪 push 之後、轉正之前被中斷時，真的新 manifest 會被當成注入物隔離掉（技術驗證 1.4f3）。「主 manifest 不存在、`.bak` 內容與遠端 refs 都等於正式釘選值」視為上一輪在替換空檔被中斷：丟棄待定，照常往下走，push 會重建主 manifest（技術驗證 1.2m）。
  4. 逐層檢查上層同名資料夾，以內容判定、遞迴清掃 repo 資料夾與讀取視圖資料夾。
  5. `clone -b main`（完整 URL），核對全部 ref 與 manifest 內容雜湊等於正式釘選值。
  6. `git annex init`、設 `annex.max-git-bundles`。
  7. 處理收件匣：驗章（D3）、驗格式、防重放、依內容雜湊判斷重複；原始紀錄 → 閱讀版；交接單與認領、參考、改寫提案、產出登錄。
  8. 寫入「待定」釘選值（新的 refs）。
  9. 預檢：遠端 manifest 的內容雜湊（只查名稱符合 GITMANIFEST 的檔，出錯就中止）仍然等於正式釘選值裡的雜湊，然後 push `main` 與 `git-annex` 兩個 ref。直接跟釘選值比對，第 5 步之後的空窗也在保護範圍內。
  10. 以 `git ls-remote` 確認遠端全部 ref 等於本地，並重新讀 manifest，確認它的 active 清單恰好是「舊的可信集合＋這次新增的」。helper 輸出的 `unexpected status` 一律視為失敗。**push 會靜默失敗**（exit 0、`Everything up-to-date`，但內容沒上 Drive，技術驗證 1.2），所以「成功」只以這一步為準。
  11. 轉正釘選值（含新 manifest 的內容雜湊、當下 `git-annex` 分支的 key 集合），回收已移除的 bundle。
  12. 發佈讀取視圖與搜尋索引（D5；manifest 只用 API `files.update` 原地更新）。
  13. 刪除已處理的收件匣項目。
- **管理操作也要跟提交流程錯開。** 抹除、回滾、復原會直接改寫並重推 repo。住民的 token 能停用、啟用 workflow，也能取消 run（技術驗證 1.6），所以不能只靠開關：管理腳本先停用提交流程、確認沒有執行中的 run，並在 push 前重讀遠端 manifest，跟正式釘選值不同就中止（預檢）。**預檢只防受信任的寫入者互相覆蓋，不是完整性檢查**；防注入靠釘選。管理操作改寫歷史之後，由管理者重建正式釘選值，重建期間暫停清掃。健康檢查回報提交流程有沒有被停用。
- **收件匣項目的處理。**
  - 一個收件匣項目是不可分的單位：原始紀錄先寫，再寫 sidecar（記著原始紀錄的內容雜湊），最後寫分離式簽章檔（簽 sidecar 檔案的原始位元組，任何語言都能驗，不需要 JSON 正規化）；沒有簽章檔的項目留到下一輪。
  - **防重放**：同一個 client 底下的 worker 看得到彼此的收件匣項目，可以把別人簽過的舊項目重新放進來。提交流程拒收比真本現有版本舊或相同的更新（session 看快照時間與原始紀錄雜湊、參考 Link 看讀到的快照時間、其他看 `updated_at`），記下處理過的 item_key，並要求檔名的 item_key 等於 sidecar 裡的值；讀取視圖不發佈 sidecar 與簽章原文。
  - 第 10 步驗證成功之後才刪除已處理的收件匣項目；重跑時靠內容雜湊保持冪等。
  - 同一次執行裡先收原始紀錄、再收交接單與認領，所以接續點可以跟交接單同一批進來，一次接續只需要一次提交流程。
  - 被拒收的項目與逾時的孤兒項目（只有原始紀錄、沒有 sidecar），在拒絕原因發佈後 24 小時由提交流程刪除並永久刪除；抹除的範圍也涵蓋收件匣。
  - 拒絕原因發佈在讀取視圖（寫入者用讀取身分讀），不寫回收件匣，因為 `drive.file` 的寫入者看不到提交流程建立的檔案。
- **workflow 的輸入與 log。** 住民能任意觸發 workflow、rerun 舊的 run、用任何既有分支觸發，也讀得到 run 的 log（ADR 0006、技術驗證 1.6）。所以：
  - `workflow_dispatch` 不接受任何自由輸入。
  - MyAiStorage repo 除了 main 以外，不保留任何帶有 workflow 檔的分支或 tag。
  - workflow 只輸出 id、計數與耗時，不輸出標題、內容、commit 訊息；log 保留天數設到最短；抹除時一併刪除可能含有該內容的 run。
  - 修補 workflow 的安全問題之後，刪掉舊的 run、刪掉或改寫帶有舊 workflow 的分支與 tag，並輪替提交流程的憑證。
- **觸發方式有兩種**，都屬於寫入端的機制（D9）：
  - 定時執行，間隔可以設定，**預設每 6 小時**（使用者決定）。GitHub 的 schedule 會延遲（實測 3〜19 分鐘，樣本 8 個）、負載高時甚至跳過，所以這是「通常」而不是保證；健康檢查記錄實際間隔。
  - 手動或由寫入者主動觸發（workflow_dispatch）：「同步並提交」走這條，你也可以隨時手動觸發。
  - 同一時間只允許一個提交流程在跑（concurrency group）。GitHub 的 group 最多一個執行中、一個等待中，第三次觸發會取消原本等待中的那個（技術驗證 1.8 實測）；因為每次執行都會掃完整個收件匣，被取消不會漏收，但觸發者不能以「自己那個 run」判斷完成。
  - **「同步並提交」的完成**以讀取介面看得到這次放進收件匣的每一個項目、或它的拒絕原因為準（spec「寫入端維持新鮮度」）。
- **產生者章。** 同一個 client 底下的 worker 能讀、改、刪彼此的收件匣項目，別的 project 的 client 也能把項目放進任何收件匣（技術驗證 1.4），所以收件匣的位置**不能**證明產生者。每個 profile 有一把簽章金鑰，同步器對 sidecar（含原始紀錄的內容雜湊）簽章，提交流程以登錄的公開金鑰驗章，**沒有簽章或驗章失敗一律拒收**。產生者章依驗章通過的金鑰蓋。寫入者自己填的產生者一律忽略（ADR 0006）。
- **改寫與抹除。**
  - 改寫：寫入者把改寫提案放進收件匣，提交流程做成一個新 commit。舊版本留在 git 歷史裡，可以 revert。
  - 抹除：只有你在 Mac 上用管理憑證執行（不在 opencode 的容器裡），步驟由技術驗證 1.3 定下：
    1. 在本機 clone 裡改寫歷史（filter-repo），刪掉 `refs/annex/last-index` 等 `refs/annex/*`，清掉 `.git/annex/objects` 裡被抹除的物件，把被抹除的 key 標成 dead 並 `forget --drop-dead`，最後 `gc --prune=now`。
    2. 依 file id **永久刪除**遠端全部 GITBUNDLE、GITMANIFEST（含 `.bak`）與被抹除的 annex key（`files.delete`，不經過垃圾桶；`force push` 清不掉舊 bundle，丟進垃圾桶也不算刪除）。其他 annex 物件保留，不必重傳（技術驗證 1.3 的部分抹除）。
    3. 重推，重建正式釘選值。
    4. 後置條件：remote 的 bundle 集合等於 manifest 的 active 集合；垃圾桶裡沒有這個 repo 任何一代 uuid 的 GITBUNDLE（每一代 uuid 都要記錄）。
    5. 範圍一併涵蓋隔離資料夾、收件匣、Actions run log、垃圾桶，以及已知的 clone（管理用的 Mac、復原演練）。
    - 防呆：目標一律以 file id 指定，先 dry-run 列出清單；永久刪除前以 API 確認 `trashed=true`（清垃圾桶時）或 parents 不是任何 live repo 資料夾。`rclone --drive-trashed-only` 的遞迴清單會混進 live 資料夾，不能直接拿來刪。
    - AI 發現機敏內容時，只能提出改寫把它遮蔽掉，並提醒你抹除。
- **為什麼放 GitHub Actions：** 它在雲端，跟 AiContainer 與 Mac 都無關，符合「不寄居 AiContainer」與「互不為前提」；不用維運常駐機器；而且它是免費方案的一部分。

**替代方案：**
- 每個寫入者各自一個 repo：寫入即時，但 AI 能毀掉自己 repo 的歷史；之後的手機也仍需要有人代為 commit。不採用。
- 大家直接 push 同一個 repo：同時 push 時會悄悄丟資料。不採用。
- 在 Gateway VPS 跑提交程式：Gateway 設計成隨時可以整台丟棄，而且會碰到「VPS 不是拿來開服務」的立場。不採用。

### D3. 身分的實現：worker 共用限權的 Drive 憑證，profile 以簽章金鑰區分；期 1 由你手動安裝

**Drive 憑證的形狀（技術驗證 1.4 之後，使用者決定）：** 每個 profile 各開一個 Google 帳號不可行，而同一個 GCP project 裡的 client 之間完全不隔離（1.4a）。所以：

| 角色 | GCP project | 寫：Drive | 讀：讀取視圖／git 歷史 | GitHub |
|---|---|---|---|---|
| 所有 worker（期 1 是 Mac opencode `mac-opencode` 與測試用 profile） | 共用一個 worker project | **共用一個** OAuth client，`drive.file` scope | **共用一個** service account，被分享成 reader | fine-grained token：MyAiStorage repo 只有 Actions 寫入權（只用來觸發提交流程），沒有 contents 權限 |
| 提交流程 | **獨立的** project | 專用帳號的完整 Drive 權限 | — | workflow 的 `GITHUB_TOKEN`（MyAiStorage repo 只有 contents 讀取）；pin repo 的 deploy key（只對 pin repo 有寫入權） |
| 你（管理） | — | 專用帳號 | — | 你的帳號 |

- **committer 一定要在獨立的 project**：跟 worker 同一個 project 的話，worker 能改、刪 committer 建的每一個檔，甚至把 repo 分享給外人（1.4a）。不同 project 之間，讀、改、刪都會被擋（1.4b、1.4c）。
- **Drive 憑證的粒度是「worker 群組」**：在 Drive 層級撤銷，所有 worker 一起失效。refresh token 由所有 worker 共用同一份（每個 worker 容器不各自授權）：Google 對同一帳號、同一 client 保留的 refresh token 有數量上限（PM 的已知資訊，未實測），共用一份就不會碰到。
- **profile 的粒度靠簽章金鑰**：每個 profile 一把簽章金鑰，產生者章與「以 profile 為單位的撤銷」都靠它（D2「產生者章」）。要讓某個 profile 失去寫入能力，就讓提交流程不再接受它的金鑰。
- **讀取身分共用一個 service account**（使用者決定）：撤銷讀取權＝輪替這把共用金鑰，影響所有 worker。service account 沒有任何儲存配額（1.5），所以就算金鑰外洩，也建不了任何檔案；但它讀得到注入物，所以所有讀者與工具一律以 id 定位（D5）。期 1 的讀取授權以要素為單位（全有或全無）；之後要做內容層級的讀取授權（例如 LLMGateway 的隱私 tag），讀取視圖就依可見範圍分資料夾（擴張點）。
- **收件匣的建立**：由 worker 的 client 建立收件匣資料夾，放在專用帳號「我的雲端硬碟」的根目錄，不放在 AiStorage 的根資料夾底下。這是選擇，不是限制（技術驗證 1.4e 證實它建得進去）：放在根資料夾底下會繼承讀取身分的分享，讓所有 worker 讀得到收件匣裡還沒驗證的內容，也會跟清掃的範圍混在一起。建好之後把「資料夾 id ↔ profile」登記進設定（tasks 2.3）。收件匣一律以資料夾 id 辨識；位置不證明產生者，產生者靠簽章。
- **憑證的證明與發放。** 拿到哪組憑證，就等於屬於哪個 profile。期 1 由你本人把 worker 的憑證與 Mac opencode 的簽章金鑰放在 Mac 上的固定位置（repo 之外，權限 600），並記進 `docs/resources.md`（只記名稱與位置）。之後改由 MyLinuxPool 發放（工單見 `docs/tickets/mylinuxpool.md`）。
- **opencode 在容器裡執行。** Mac 上直接執行的 AI 能跑任意 shell，看得到你本人在 Mac 上的所有憑證；那樣「住民沒有能改寫真本的憑證」就不成立。所以期 1 的 opencode 與同步器在 Mac 上的 Ubuntu 容器（colima）裡執行。容器看得到自己的憑證（擋不住），但那組憑證的上限是：在知道 id 的資料夾裡建檔（包括 repo 資料夾與讀取視圖資料夾，由 D2 的清掃處理）、讀取、讀改刪同一個 client 底下其他 worker 的收件匣項目（由 D4 的補傳處理）、觸發提交流程。它改不了、也刪不了真本，所以毀不掉歷史。這個容器的形狀跟之後的 worker 相同，可以直接沿用。
- **容器裡允許的憑證是一份白名單**：worker 的 Drive 憑證、讀取身分、Mac opencode 的簽章金鑰（全部以唯讀方式掛在 `/secrets/`，conf 裡一律寫容器內的路徑）；opencode 要用的 LLM provider 金鑰（沒有它 opencode 不能工作，外洩的影響是額度，不是 AiStorage；以 opencode 設定檔的檔案引用讀取，不要讓 opencode 把它複製成 `/work` 裡的明文 `auth.json`，技術驗證 1.7a）；需要時一把限定單一測試 repo 的 fine-grained token。白名單以外的都不掛載。
- **每個分岔各自一個容器。** 驗收分裂、統合、相互參照時，每個 Session 在各自的容器裡執行，工作目錄與 opencode 的本機資料都不共用（可以共用同一組 profile 憑證）。接手的一方唯一的資訊來源是讀取介面，這樣才驗證得到「狀態在執行體之外」。
- **撤銷**：profile 的寫入以簽章金鑰撤銷；Drive 寫入憑證與讀取身分是 worker 群組共用，輪替時影響所有 worker。
- 期 1 用長期憑證。改成短效憑證是之後的擴張點。

**替代方案：**
- opencode 直接在 Mac 上執行：最省事，但它看得到你的 GitHub 與 Google 憑證，能力邊界只剩文字約定。不採用。
- 住民直接持有 Drive 真本的寫入權：任何能寫的 token 都能刪檔。不採用。
- 每個 profile 各自一個 Google 帳號（profile 的 token 不再是擁有者，建不進沒被分享的資料夾）：從根源解決注入，但帳號管理的成本太高，使用者不採用。
- 每個 profile 各自一個 OAuth client 或 project：擋不住跨 project 的建檔（1.4d、1.4e），對注入沒有幫助，只多了管理成本。不採用。

**之後**：worker/default（員工）與手機 App（秘書）的 profile 沿用同一個形狀：共用 worker 的 Drive 憑證與讀取身分，各自一把簽章金鑰與 fine-grained token（員工另加 MyBrain 唯讀）。手機在裝置上用原生 AuthorizationClient 登入（WebView 內禁止 OAuth）。見 `docs/backlog.md`。

### D4. 同步器：寫入端只負責上傳，格式轉換集中在提交流程

- 每個來源應用各有一個同步器，只做三件事：
  1. 把原始紀錄（來源應用自己的匯出單位）上傳到收件匣。
  2. 附上 metadata sidecar，內容包括項目 id、來源應用、狀態（與停止時間）、所屬案件、快照時間、原始紀錄的內容雜湊、是否仍在生成中（`in_progress`），並以 profile 的簽章金鑰簽章。
  3. 需要時觸發提交流程（同步並提交）。
- 閱讀版的轉換器放在提交流程裡，每個來源應用一個。「閱讀版可以從原始紀錄重建」就等於在提交流程上重跑一次轉換器。之後加一個來源應用，就是加一個同步器加一個轉換器（spec「每個來源應用一個同步器」）。
- **快照時間**由同步器在擷取原始紀錄時記下；提交流程以收件匣檔案在 Drive 上的建立時間當上限檢查它，晚於上限的一律以上限為準。
- **期 1 的同步器**：opencode，在 Mac 的容器內定期對有變化的 Session 執行 `opencode export <id>`（技術驗證 1.7 確認可行：單一 JSON、內容完整；沒有變化時位元組完全相同；自動壓縮只在尾端追加；prune 只加 `state.time.compacted` 標記、不清除已匯出的內容，這個標記會被視為一次新版本，屬於正常）。
  - **列舉**：用 API 的全域清單 `GET /session`（包含子 Session）或 `/session/{id}/children` 遞迴列舉。CLI 的 `session list` 不含子代理產生的子 Session，只用它會漏掉子 Session 的完整對話（1.7h）。子 Session 也是 Agora 的 Session，metadata 記 `parent_id`。
  - **生成中**：Session 正在產生回覆時匯出，快照會含半截訊息。判斷依據是最後一則 assistant 訊息有沒有 `time.completed`，sidecar 記 `in_progress`（1.7b）。生成中的快照照常算作新鮮度的快照。
  - **要不要上傳，以 Agora 比對**（使用者決定）：同步器拿 Session 的最新內容雜湊跟讀取視圖裡的版本比對，Agora 沒有或較舊就上傳。已上傳、還沒提交的項目視為等待中，不重傳；下一次提交之後 Agora 裡仍然沒有，才補傳。這樣同一個 client 底下的其他 worker 就算刪掉收件匣裡的項目，內容也不會遺失，而且不增加 Actions 分鐘。
  - **opencode 的版本相依**：prune 目前只加標記、不清除匯出裡的 `state.output`；升級 opencode 時要重驗（6.3 的檢查清單）。
- **項目 id**：`<來源應用>:<來源應用自己的 Session id>`（細節在 tasks 2.1 定案）。重新同步或手動匯入同一個 Session，都對到同一個 id。
- **停止中**：opencode 沒有自然的結束訊號（1.7e），所以期 1 的停止中由你或 AI 明確宣告，sidecar 記下停止時間。宣告的管道用 opencode 自己的封存：你在 TUI 按封存，或 AI 用 skill 呼叫 API（`PATCH /session/{id}` 設 `time.archived`）。`archived > 0` 而且封存之後沒有新訊息，才算停止中；停止時間用同步器觀測到的時間，不直接用來源端填的值。封存的 Session 仍然出現在清單裡，所以同步器看得到封存後的追加。同步器偵測到宣告停止後又有新內容，就立刻同步並提交一次讓它回到運作中；在那之前約一次提交流程耗時的窗口內，讀者可能讀到停止時的內容而沒有警告。
- 上傳重複的內容時，要以內容雜湊判斷為同一份，不產生新 commit（spec「定期同步」）。
- **期 1 的轉換器**：opencode；Claude Code（只用在單一 Session 手動匯入，Claude Code 的 Session 大約 30 天就會被本機自動清除，需要時要能及時救進來）。
- 閱讀版的共通格式要在寫任何轉換器之前先定下來（tasks 2.4），所有轉換器都照同一份格式輸出。

**之後**：手機 App 的同步器由 MyAiEntry 實作，上傳時要排除 App 另存的 SSH 私鑰、LLM key、PAT；worker 的 opencode 同步器在刪除 worker 前由 MyLinuxPool 跑最後一次。見 `docs/backlog.md`。

### D5. 讀取：每個要素一個讀取介面，背後讀「讀取視圖」

- **讀取介面**是讀者取得內容的唯一手段，形式是函式庫加 CLI，要能在 arm64 與 amd64 上跑。Agora 的讀取介面負責找 Session（篩選、全文）、讀 Session（metadata、閱讀版、兩個方向的 Session Link、交接單）、列出尚未被認領的交接單；Foundry 的讀取介面負責查產出目錄與取得收容產出。兩者各自獨立（ADR 0001），但遵守同一套新鮮度規則（D9）。
- **背後讀的是讀取視圖**：每次提交後，提交流程把讀取視圖以一般檔案發佈到 Drive 上的另一個資料夾，內容是每個 Session 的 metadata、閱讀版、Session Link（含反向索引）與交接單、各項目的快照時間、拒絕原因、產出目錄、搜尋索引。讀取介面用讀取身分（期 1 的 worker 共用一個 service account）讀取。讀者不需要知道背後是讀取視圖還是 git。
- **讀取視圖增量發佈**：只重寫有變動的檔案，另外發佈一份帶世代號的 manifest；讀取介面依世代號快取，沒變就不重新下載。Drive 大約每秒只能處理 2 個檔案，整份重建會讓提交時間隨 Session 數線性成長。
- **讀取介面以檔案 id 定位**，而且**所有讀者與工具**（包括管理腳本、復原作業、之後的手機用戶端）都遵守：讀取視圖的 manifest 是一個**固定檔案 id、原地更新**的檔案（只用 API 的 `files.update` 寫入，id 不變，技術驗證 1.4i；讀者看到更新的延遲約 2〜5 秒），它的 id 跟著 profile 的讀取設定一起發放；其他檔案以 manifest 裡記的檔案 id 取得。整條讀取路徑都不以名稱搜尋，因為住民能在讀取視圖資料夾裡注入同名檔（Drive 允許同名；所有 client 都以同一個專用帳號授權，owner 欄位分不出是誰建的）。讀取視圖資料夾也在每一輪清掃（D2）。
- **收容產出的本體**：讀取介面用 service account 依 annex key 直接從 rclone special remote 取物件，路徑規則是 `<rcloneprefix>/<完整 key 檔名>`（不開 chunk，技術驗證 1.5 確認），不另存一份可讀副本；取得後以 key 驗內容雜湊。
- **門檻**：搜尋索引超過 50 MB、或讀取視圖超過 5,000 個檔案時，「期 1 資料量小、整份下載索引」的前提失效，要改成分片或雲端查詢（健康檢查回報這兩個數字）。
- **git 歷史**：你（管理）與復原作業可以用 service account 的 reader 權限 `git clone annex::…`，用 `log / diff / show` 看歷史。這不是住民的讀取手段。
- **搜尋**
  - 提交流程每次提交後重建一個 SQLite FTS5 索引（trigram 分詞，能處理中日文），放進讀取視圖。
  - 讀取介面下載索引到本地查詢：篩選用 metadata 欄位，全文用閱讀版。期 1 資料量小，下載成本低。
  - 之後可以把後端換成語意搜尋或雲端查詢，查詢的形狀不變（spec「搜尋後端可以替換或疊加」）。

**實作語言**（使用者 2026-09-27 決定）：Python 3.12（提交流程、同步器、讀取介面的函式庫與 CLI、管理腳本）；opencode 的 plugin 用 TypeScript（opencode 的限制）。

**之後**：手機 App（MyAiEntry，Capacitor／TypeScript）也要用同一個讀取介面，所以索引格式與查詢規格要寫成可以獨立實作的規格，之後另寫 TypeScript 版的用戶端（手機的原生 SQLite 版本不一定支援 trigram，要在那時確認）。

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
  2. 提交流程定時執行，間隔可以設定，預設每 6 小時；你也可以隨時手動觸發。
  3. 寫入者主動「同步並提交」：同步後觸發提交流程，等到讀取介面看得到自己這份快照。寫交接單前一定要做；AI 想讓並行的 Session 讀到自己的進度時也可以做。
- 所以期 1 的新鮮度大致是：沒人主動提交時，最多落後「排程間隔＋約 20 分鐘的排程延遲」，預設約 6 小時 20 分；主動提交之後，約等於一次提交流程的耗時（技術驗證 1.8：合成流程約 1 分鐘，真流程估計 2〜3 分鐘）。
- **Actions 分鐘數**（使用者 GitHub 帳號的免費額度，每月 2,000 分鐘，與 MyLinuxPool 共用；AiStorage ≤300 為目標）：空收件匣 1 分鐘，非空提交估計 2〜3 分鐘。每 6 小時排程加上平均每天約 2 次主動觸發，估計 276〜372 分鐘／月（每天 5 次會到 456〜642），可能超過 300，所以 6.3 監控實際用量，連續兩週超過 300 就回頭調整間隔。主動觸發只用在寫交接單、認領確認、明確要發佈進度這些情況；concurrency group 會合併短時間內的連續觸發。
- **之後追加的寫入機制**（依成本由低到高）：縮短定時間隔、上傳後自動觸發提交、合併多次觸發、自架 runner。追加任何一種都不改變讀取介面。

### D10. 接續經由交接單與認領：分裂與統合

- **交接單是 Agora 的項目**，有自己的 id，記著被接續的 Session、接續點與交接內容。
- **接續點＝（快照識別，message id）**（技術驗證 1.7c）：opencode 的 message id 在追加、重開、壓縮後都不變，但使用者 /undo 之後重新輸入（很常見）會刪掉後面的訊息。所以接續點釘在交接單所依據的那一份原始紀錄快照（內容雜湊）上，讀取介面「只取接續點之前的內容」一律從**被釘住的快照**讀，不從最新版本讀；來源端之後怎麼編輯，都不影響已經成立的接續。接續點指向**最後一則已完成的訊息**：寫交接單的那一次回覆本身一定還在生成中，不算在內。來源端刪掉訊息，對 Agora 來說是原始紀錄的新版本（舊版本留在歷史裡），不是 spec 所說的「改寫」，不拒收。閱讀版要照 `info.revert` 指標處理：指標之後的訊息在來源端已經被撤銷。接續 Link 在新 Session **認領**交接單時才建立，方向是「新 Session → 被接續的 Session」。理由：分裂的當下，接手的 Session 還不存在、也沒有 id；而且接續只能由持有者發起，才不會出現「讀者觸發別人寫入」（ADR 0007）。
- **分裂（1→n）**：持有者 S1 同步，為每一份工作各寫一張交接單，一起提交（接續點可以跟交接單同一批進來，只需要一次提交流程）。之後開新的 opencode Session（各自的容器），各自認領一張。
- **統合（n→1）**：每個分岔的持有者（S2、S3）各自同步、寫一張交接單交出自己的末端並提交；新 Session S4 認領全部。S4 從頭到尾不要求 S2、S3 做任何事，所以同一套語意之後可以延伸到跨裝置（手機的 S2 加 worker 的 S3）。
- **一張交接單只能被認領一次**；重複的認領由提交流程拒絕，並把原因發佈在讀取視圖。尚未被認領的交接單可以經由讀取介面列出。因為判定是非同步的，認領之後要立刻同步並提交，等讀取介面確認這條 Link 屬於自己才開工；看到拒絕就停下。
- **認領需要知道自己的 Session id**：plugin tool 的 `context.sessionID`（或 shell.env 注入）可以確定地取得目前的 Session id，`opencode run "<內容>" --format json` 可以用指定內容開新 Session（技術驗證 1.7f、1.7g）。plugin 裝在 image 裡的唯讀路徑，不放 `/work`。**認領與宣告停止只能在主 Session 裡執行**：子代理的子 Session 呼叫時，plugin 看到帶 `parentID` 的 Session 就拒絕。
- **fork**：opencode 的 fork 沒有 parent 欄位、訊息 id 全部重新產生，Agora 會把它看成一個不相關的新 Session（內容與母 Session 重複）。期 1 的驗收不用 fork；偵測 fork 列在待辦。
- **預留欄位**：交接單、接續 Link 與 Session metadata 先保留選填的 `role`、`role_version` 擴充欄位（之後 Atelier 要求接續指定職務、Session 記錄職務版本），期 1 不驗證。

## Risks / Trade-offs

- [住民能在 repo 資料夾與讀取視圖資料夾注入檔案（技術驗證 1.4，使用者已接受的例外，ADR 0008）] → 殘餘風險有四項，都由使用者明示接受：(1) 住民能持續注入、取消 run 或停用 workflow，讓提交暫停（偵測得到，不會遺失或竄改內容）；(2)「寫進 repo 資料夾」的能力依然存在；(3) 注入可以消耗家庭共用的 5TB 配額；(4) 共用 client 的 worker 能刪除彼此的收件匣項目（同步器會補傳）。緩解：釘選＋清掃＋隔離（D2）、簽章（D3）、Agora 比對的補傳（D4），以及 6.3 監控「連續中止的輪數」「隔離資料夾的增長」「workflow 是否被停用」「距離上一次成功提交的時間」「家庭共用的整體配額（`limit - usage`，家人的用量也會縮小 AiStorage 的空間）」，連續 N 輪中止就通知你。
- [清掃與釘選的實作細節容易出錯（技術驗證 1.4f3 的查核找到的問題）] → 3.2 必須做到：讀不到就中止、不做移動；manifest 依角色判定（非 `.bak` 只能等於目前的釘選值）；可信的 annex key 來自釘選值；push 後比對 active 清單；解析 manifest 時分出 active 與已移除（不然第一次 consolidate 之後每一輪都會中止）；不保留任何「信任任何能重放的 manifest」的模式。9.4 的反向測試逐條覆蓋。
- [git-remote-annex 每次 push 都要下載全部 active bundle，而且舊 bundle 永遠不會被刪] → `annex.max-git-bundles` 讓 active bundle 維持在少數幾個（1.8：500 次 push 後只有 5 個）；bundle 回收讓總檔案數有上限（D2）。**consolidate 的成本沒有量到**：合成資料的歷史只有幾 KB，真實的 Session 原始紀錄放在 git 裡時，歷史會穩定成長，每次 consolidate 要重傳一次完整歷史。原始紀錄放 git（有 delta 壓縮）還是放 annex（bundle 小、但每個版本一份完整物件）要在 2.x 用真實大小的匯出模擬一個月後決定。
- [管理操作（抹除、回滾、復原）與提交流程同時 push] → 管理腳本先停用提交流程、確認沒有執行中的 run，push 前做預檢；refs 已經分歧時的撞車是看得見的 non-fast-forward 失敗（1.8 實測），manifest 層級的競速靠預檢把窗口壓到秒級（D2）。
- [只放 Drive：專用帳號或提交流程的憑證外洩，就能永久刪除 AiStorage 的全部資料；Drive 的垃圾桶與舊版本只保留 30 天] → 使用者明示接受。緩解做法：AiStorage 放在專用帳號，影響範圍不含你個人的 Drive；提交流程的憑證只放 Actions secrets；住民只有 `drive.file` 與 reader 權限；每個 git clone 本身都是一份完整歷史，可以當非正式的備份（但它也是抹除碰不到的地方，抹除時要一併處理）。之後要加第二份副本時，git-annex 的 numcopies 就是現成的擴張點。
- [抹除之後，內容仍留在 Drive 自己的舊 revision 與垃圾桶，或既有的 clone 與快取裡] → 技術驗證 1.3 實證：`force push` 清不掉舊 bundle，丟進垃圾桶也能完整還原，所以抹除一律依 file id 永久刪除 bundle、manifest 與目標 key 再重推（D2）；抹除腳本列出已知的 clone 與快取提醒你處理。`git-annex` 分支會留下被抹除物件的 key 名稱（內含內容的 SHA256，只允許「猜內容、驗證是否相符」的確認攻擊），所以把 key 標成 dead 並 `forget --drop-dead`。永久刪除是無法復原的操作，打錯前綴就等於永久丟失一個 repo（1.3 期間實際發生過一次），所以目標一律以 file id 指定、先 dry-run。
- [`drive.file` 的隔離單位是 GCP project，而且擋不住建檔（技術驗證 1.4）] → committer 放在獨立的 project；worker 共用一個 project 視為同一個信任範圍（D3）；注入以釘選＋清掃處理（D2、ADR 0008）。
- [opencode 沒有自然的「停止」訊號] → 停止中只能明確宣告（用 `time.archived`），不從閒置時間推測（D4）；宣告後又有新內容就回到運作中，讀者會重新看到新鮮度警告。
- [Apple Silicon（arm64）上驗證過的東西，不一定能直接搬到 amd64 的 worker 與 runner] → 實作語言的提案要回答兩種架構都能跑；技術驗證記下每個工具在兩種架構上的安裝方式與版本。
- [Mac 上的 AI 看得到你本人的憑證] → opencode 與同步器在容器裡執行，只掛載白名單上的憑證（D3）；抹除等管理操作不在容器裡做。
- [來源端的編輯會刪掉接續點之後的訊息（技術驗證 1.7c）] → 接續點釘在快照上（D10），來源端之後的編輯不影響已經成立的接續。
- [OAuth client 停在 Testing 狀態時，refresh token 7 天就過期；refresh token 6 個月沒用也會失效；rclone 內建的共用 client 2026 年內會停用] → 自己建 OAuth client，並設成已發佈（以個人使用的例外處理，不送驗證）；再加一個定期使用的健康檢查。
- [Actions 分鐘數（每月 2,000 分鐘，跟 MyLinuxPool 共用）] → 預設每 6 小時，估計 276〜372 分鐘／月（D9）；主動觸發限縮在必要的情況；每次執行都記錄耗時，6.3 監控實際用量。住民注入垃圾檔讓每一輪都變成非空提交，也會灌分鐘（殘餘風險 1），所以空收件匣只算形狀符合格式的檔。不夠時改用自架 runner（擴張點）。
- [新鮮度受限於最便宜的寫入機制：相互參照時讀到的可能是幾小時前的內容] → 讀取端一定看得到快照時間與警告，不會誤以為是最新；需要時由寫入的一方主動同步並提交。真的不夠用時，再依成本追加寫入機制（D9），讀取介面不變。
- [Drive API 大約每秒只能處理 2 個檔案] → 用 git bundle 與 annex 分塊，把檔案數量壓低；轉換與索引一次批次處理。
- [抹除要改寫 git 歷史並重推 bundle，步驟複雜] → 寫成管理腳本，並用驗證測試保證抹除後的內容在目前版本、舊版本和讀取視圖裡都找不到。
- [Mac opencode 的 token 有 Actions 寫入權：能觸發任何 workflow、用任何既有分支觸發、rerun 舊的 run（用舊程式碼配上現在的秘密）、停用或啟用 workflow、取消與刪除 run、讀 log（技術驗證 1.6）] → 每個 workflow 都必須設計成被任意觸發也安全；開頭檢查 `github.sha` 與 `github.ref`；main 以外不留帶 workflow 的分支；修補後刪舊 run 並輪替秘密；log 最小化；稽核紀錄寫在提交流程自己的 commit 裡，不依賴 Actions 的執行紀錄（D2）。
- [OAuth 的 7 天 refresh token 過期，在技術驗證期間驗不到] → 首次授權後第 8 天複查（2026-10-04 前後）；committer 搬到獨立 project 之後，對新的 client 重新起算。
- [容器時鐘在睡眠喚醒後的漂移沒有量到（1.7j）] → 非睡眠情況的漂移不到 1 秒，遠小於新鮮度的分鐘級尺度；睡眠喚醒的補測之後與你一起做。

## Migration Plan

- 這是新系統，沒有既有資料要遷移。歷史 Session 不整批回補，需要時再用單一 Session 手動匯入。
- 上線順序：
  1. 技術驗證（2026-09-27 完成，go-with-design-changes）。
  2. 共通 metadata schema、收件匣格式、閱讀版格式。
  3. 提交流程加 Agora 路徑（opencode、Claude Code 轉換器）。
  4. 讀取視圖與讀取介面（含新鮮度）。
  5. Mac 容器裡的 opencode：同步器、同步並提交、分裂／統合／參照用的 skill。
  6. Foundry（最小）。
  7. Atelier 需求設計、MyLinuxPool 工單。
  8. 端到端驗收：分裂、統合、相互參照。
- **回退**：任何一步失敗都只影響 AiStorage 自己。來源應用原本的本機紀錄不受影響，所以停用 AiStorage 不會讓任何既有功能壞掉。

## Open Questions

- 定時提交的間隔（預設每 6 小時，可設定）與同步器的同步間隔（預設每 10 分鐘），要看實際的 Actions 用量再調，不影響 spec 與任務拆分。
- 原始紀錄放 git 還是 annex，以及 `annex.max-git-bundles` 的值，在 2.x 用真實大小的匯出模擬一個月後決定（Risks）。
- 閱讀版共通格式的細節（欄位命名、圖片怎麼表示）在 tasks 2.4 定案，時間點在寫任何轉換器之前，不影響介面。
