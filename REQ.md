# REQ：AiStorage（MyAiStorage）

> 這份文件是交接用的。讀者是接手這個專案的 AI agent，目的是讓你釐清使用者的需求與背景，並沿用已經完成的需求設計與基本設計，接著完成實作與測試。結構沿用使用者原始需求文件的格式：背景、目的、步驟、注意/限制事項。

## 背景

使用者正在打造個人用的 AI 基礎建設，由以下要素組成：

- **AiEntry**（repo：`FATESAIKOU/MyAiEntry`）
    - 個人的 AI 入口，也是整套基礎建設裡最常用的對人介面，基本上就是手機 App。
    - 住在裡面的 AI 叫做**秘書**。
- **AiContainer**（repo：`FATESAIKOU/MyLinuxPool`，簡稱 MLP）
    - AI 的工作空間：Linode VPS（Gateway）、家裡一台常駐筆電（fh-proxy）、家裡的主力機（fh-l，可以用 wake-on-lan 開關），加上 docker，按需開關給 AI 用的 worker。
    - 住在 worker 裡的 AI 叫做**員工**，之後使用 opencode（worker 目前還不能跑 agent，員工不在期 1）。
- **AiStorage**（**本 repo**）：讓整個系統有「狀態」。它做的是**外部化狀態**：先問這個 AI 系統應該記住什麼，才能跨 AI、跨裝置、跨時間運作，再把這些狀態設計成放在所有執行體之外的一層；不是把各應用既有的狀態備份出去。狀態分成四個各自獨立的儲存要素。
    - **MyBrain**（repo：`FATESAIKOU/MyBrain`，已經在運作）：個人的第二大腦，存進去的東西只用於決策參考（我是誰、我在哪裡、我要去哪裡）。**案件**只在這裡定義。
    - **Atelier**：員工的 harness 資料，以**職務**為單位，每個職務由 know / do / judge / dont 加上能力需求組成，並以 MLP 的某個 profile 為前提。需要安裝建置的東西（MCP 等）放在 MLP 的 profile，不放這裡。
    - **Agora**：所有來源應用的原始 Session 集中存放處。它讓之後啟動的 AI 能**接續**或**參考**前面的 Session，並附有一個可以擴充的搜尋介面。
    - **Foundry**：所有產出的集中管理處，**管理集中、儲存分散**。每件產出都登錄在產出目錄裡，但只有收容產出（簡報、圖片、影片、一次性報告）的真本放在 Foundry。
- **LLMGateway**：另一個獨立的專案（參考 sub2api），本期沒有相依。之後的隱私分級 tag 會放在 metadata 的擴充欄位。

有幾條原則貫穿整套基礎建設：

- **AiEntry 與 AiContainer 絕對不能互為前提**。少了 AiContainer，秘書會少很多能力，但仍然能用。兩者交接的方式是：秘書把 Session 連同交接單與指定的職務，經由 Agora 交給員工（之後的目標；期 1 見「目的」）。
- AiStorage 是兩邊的**共同依賴**，所以 MUST NOT 寄居在 AiContainer 的機器上。

**目前的進度**

- 需求設計與基本設計都已經完成，並經過使用者 review。
- 2026-09-26 重定期 1（見 `docs/decision-log.md` 的 Step 3 re-scope）：期 1 改成驗證 Session 的分裂、統合、相互參照，扮演 AI 的是 Mac 上的 opencode；期 1 之後不再分期，收在 `docs/backlog.md`。
- 實作還沒開始，下一步是 tasks 第 1 組「技術驗證」。
- 以下是權威來源。它們彼此衝突時，以 `docs/adr/` 與 `openspec/` 為準。

| 文件 | 內容 |
|---|---|
| `CONTEXT.md` | 術語表。**所有討論與產出都用這裡的詞，而且只用這裡的詞** |
| `docs/decision-log.md` | 需求 grill 的 31 個決定、分期、擴張點、外部前提、使用者的限制（英文） |
| `docs/adr/0001〜0008` | 架構決定與理由 |
| `openspec/changes/establish-aistorage-phase1/` | 期 1 的 proposal、8 份 specs、design（D1〜D10）、tasks（9 組） |
| `docs/backlog.md` | 期 1 之外的待辦清單（手機秘書、worker 與員工、Atelier 實作、MyBrain PR、數 GB 大檔等） |
| `docs/atelier/` | Atelier 的需求設計（期 1 只做到這一層） |
| `docs/tickets/mylinuxpool.md` | 給 MyLinuxPool 的工單（profile 憑證發放、能力清單、worker 的 opencode 與同步） |
| `docs/research/` | 選型所依據的一手資料研究（object storage、Drive 與 GitHub 免費方案、git on Drive）。**不是權威來源**，用詞也可能跟 `CONTEXT.md` 不一致；design 有幾處是有意識地偏離研究的推薦（例如研究推薦 git 放 GitHub、大檔放 Drive，使用者則要求只放 Drive，見 ADR 0005） |
| `docs/resources.md` | 實體資源清單（Drive 資料夾、OAuth client、token 等，不記秘密的值）。技術驗證時開始填 |

**期 1 設計的骨幹**（完整內容見 design.md）

```
 Mac 容器裡的 opencode（之後：手機 App、worker）
   ──只能放進自己 profile 的收件匣（Drive，drive.file scope）──▶
   提交流程（GitHub Actions，同一時間只跑一個）：
     驗證 → 蓋產生者章 → 轉出閱讀版 → 處理 Session Link／交接單／產出登錄
     → git-annex commit/push 到 Drive 上的 repo → 發佈讀取視圖與搜尋索引
 讀取：每個要素一個讀取介面（背後讀讀取視圖），讀的時候指定新鮮度；讀取不觸發寫入（ADR 0007）
 身分：身分就是 profile；期 1 由使用者手動安裝憑證，之後由 MLP 發放
```

## 目的

完成 AiStorage **期 1** 的實作與測試，驗證這套工作體系撐得住 Session 之間的三種關係：

| 關係 | 工作場景 | 期 1 的驗收（tasks 第 9 組） |
|---|---|---|
| 分裂（1→n） | 切分工作 | S1 分裂成 S2、S3，各帶一張交接單；S1 不受影響 |
| 統合（n→1） | 聚合成果 | S4 同時接續 S2、S3 的末端，產出登錄進 Foundry |
| 相互參照（n↔m） | 互相支持推進 | 並行的 S2、S3 用讀取介面互相讀對方，指定新鮮度、看得到快照時間 |

扮演 AI 的是 Mac 上的 opencode（在只掛載白名單憑證的容器裡，每個 Session 各自一個容器；design D3）。秘書與員工都不在期 1。期 1 的完整範圍見 `proposal.md`；期 1 之後的事項不再分期，一律在 `docs/backlog.md`。

## 步驟

1. **catchup：理解使用者相關的過去判準**
    - 用 `/mybrain-read` 讀 MyBrain，重點是**這個專案的目的與背景**（不是使用者個人）：`個人 AiAgent 入口`、`MyLinuxPool`、`專案/下一步清單.md`，判準看 `技術取捨準則`、`統一的兩端稅`。
    - ⚠️ MyBrain 裡 2026-09-06「六要素作廢、LLMGateway 整條放棄」那一組判準已經被使用者**有意識地推翻**（見 `docs/decision-log.md` 的 Step 1 carry-over）。MyBrain 的更新 PR 還沒開，讀到時以本 repo 為準。
2. **理解需求與背景**
    - 讀完上面表格裡的權威來源，用自己的話向使用者複述需求、主場景與期 1 範圍，確認理解一致。
    - 有疑問就用提問工具問，不要猜。
3. **確認基本設計**
    - design.md 已經定案。你發現與事實或 spec 衝突的地方，先提出來由使用者決定，不要自行修改設計。
    - specs 寫的是「要做到什麼」，其中有幾項的可行性要靠技術驗證確認：`drive.file` 的隔離（產生者章的可信度靠它）、Drive 上的 git-annex 本身、`opencode export` 的位置是否穩定（接續點靠它）。驗證不過，就回到這一步跟使用者重新選型，不要為了遷就實作而偷偷降低 spec。
    - 要改就用 `/opsx:update`，改完用 `openspec validate --strict` 確認。
4. **實作**
    - 照 `tasks.md` 依序進行，用 `/opsx:apply`。
    - **第 1 組是閘門**：技術驗證報告（`docs/spike/`）經使用者判斷 go 之前，不開始第 2 組之後的任務。驗證不過，就回到步驟 3 跟使用者重新選型。
    - 實作語言與框架還沒定。第 2 組開始前，先提案並取得使用者同意。提案至少要回答四件事：
        - 能不能順暢呼叫 git-annex 與 rclone 的 CLI。
        - 能不能在 GitHub Actions 與 worker（Ubuntu 容器）上跑。
        - 有沒有 SQLite FTS5 的 trigram 分詞可用。
        - 能不能在 arm64（Mac 上的 colima 容器）與 amd64（Actions runner、之後的 worker）上都跑。
      另外，讀取介面之後要被手機 App（MyAiEntry，Capacitor／TypeScript）使用，這一點也要一併考慮。（judge 的執行環境屬於 Atelier，已移到 `docs/backlog.md`，期 1 不必回答。）
    - MyBrain 案件 id 的欄位名與格式，由待辦清單裡的 MyBrain PR 定案；AiStorage 這邊一律把 id 當成不透明的字串處理。
    - 第 8 組是需求設計與對外工單：Atelier 的需求設計，以及更新 `docs/tickets/mylinuxpool.md`。工單要交給其他 repo 之前，先取得使用者同意。
5. **測試**
    - 每個任務都有對應的測試。整合測試只用測試專用的 Drive 資料夾與 repo。
    - 最後跑 `tasks.md` 第 9 組的端到端驗收（分裂、統合、相互參照、反向測試、持久性），把結果交使用者 review。

## 注意/限制事項

※ 每一步都要經過使用者 review，沒有得到明確的 OK 不准進下一步。

※ 不得同時進行多步。

※ 你必須擔任 PM，負責理解使用者的背景與意圖、針對技術架構給出取捨、拆任務與驗收。**極力避免自己讀程式碼、自己調查環境**，這些一律交給隊員，你只看他們的結論與證據。用 herdr 在同一個 workspace 開 tab 或 pane 給隊員：

| 角色 | 職責 | 工具與模型 |
|---|---|---|
| impl | 實際的實踐者與建構者：寫程式碼、讀程式碼、調整 infra 與實際環境。視狀況可以開多位 | opencode，`ollama-cloud/deepseek-v4.1-flash`，variant `max` |
| test | 針對將要撰寫的程式碼先給出測試案例；或在環境調整之後確認 infra 的內容，**但不動環境**。視狀況可以開多位 | 同上 |
| review | 架構師：意圖與架構接不接得上（需要時補足基本設計）、未來的擴張性、技術債。技術面的最高領導，但主要職責是給意見，**不寫程式碼，也不實際操作** | Claude Code，`claude-opus-5-5`，effort high |

※ 問使用者事情，一律使用提問工具，一次可以問多題，每題附上你建議的答案。commit／push 要等使用者說可以。

※ **隱私與憑證**：使用者已經同意隊員讀本 repo 的設計文件；但 MyBrain 的內容、真實的 Session 內容，不要交給隊員的外部模型處理。秘密的值不得出現在 prompt、log、repo 或報告裡，隊員只能透過環境變數或檔案路徑引用秘密，也不得把秘密檔案的內容印出來。要在 Google Cloud Console 或 GitHub 設定頁建立的東西，由使用者操作，PM 負責告訴他步驟。整合測試只能用測試專用的 Drive 資料夾與 repo。

※ **能力邊界**：住民（秘書、員工，以及期 1 在 Mac 容器裡的 opencode）不得持有任何能改寫 Agora / Foundry / Atelier 真本的憑證。禁止的能力要做成「根本不存在」，不能靠 AI 遵守文字約定。dont 只是行為約定，不是安全邊界。**有意識的例外（2026-09-26 使用者接受）**：住民仍然能在 repo 資料夾裡「建檔」（Drive `drive.file` 的限制，技術驗證 1.4），真本的完整性改由提交流程的偵測、隔離、釘選保證（ADR 0008）；改寫與刪除真本的能力仍然不存在。

※ **抹除只有使用者本人能執行**（使用管理憑證）。AI 發現不該留存的內容時，只能以改寫遮蔽（舊版本仍在），並提醒使用者去抹除。

※ **MyBrain 的硬禁止**：AI 不得推 main，也不得 merge。寫入只走既有的 `/mybrain-write`（開 PR，由本人 review）。

※ **使用者的限制**：
- GitHub 只用免費方案，不升級。
- 儲存實體只用已經付費的 Google Drive（消費者 5TB 方案），不另外保留第二份副本；這個風險使用者已經明示接受。
- 盡量不新增常駐服務。
- 使用者每週大約只有 10〜20 小時。期 1 之後不再分期規劃，壓成待辦清單；但期 1 要留下擴張點。

※ **Mac 的環境**：docker 是用 colima 跑的。斷網重連之後 colima 會有 DNS 問題，不用排查，直接重開 colima（`colima restart`）即可。

※ **已知風險**（詳見 design.md 的 Risks）：
- 寫入是非同步的，接續與認領至少要等一次提交流程的耗時（技術驗證 1.8 量測後更新）；相互參照時，沒人主動提交的話可能讀到幾小時前的內容（讀取端看得到快照時間與警告）。
- GitHub Actions 每月 2,000 分鐘，跟 MLP 共用。
- OAuth client 必須設成「已發佈」，否則 refresh token 7 天就會過期。
- `drive.file` 的隔離要實測。
- `opencode export` 的位置穩定性要實測。
- Mac 上的 AI 看得到使用者本人的憑證，所以期 1 的 opencode 在只掛載白名單憑證的容器裡執行（design D3）。
