# REQ：AiStorage（MyAiStorage）

> 這份文件是交接用的。讀者是接手這個專案的 AI agent，目的是讓你釐清使用者的需求與背景，並沿用已經完成的需求設計與基本設計，接著完成實作與測試。結構沿用使用者原始需求文件的格式：背景、目的、步驟、注意/限制事項。

## 背景

使用者正在打造個人用的 AI 基礎建設，由以下要素組成：

- **AiEntry**（repo：`FATESAIKOU/MyAiEntry`）
    - 個人的 AI 入口，也是整套基礎建設裡最常用的對人介面，基本上就是手機 App。
    - 住在裡面的 AI 叫做**秘書**。
- **AiContainer**（repo：`FATESAIKOU/MyLinuxPool`，簡稱 MLP）
    - AI 的工作空間：Linode VPS（Gateway）、家裡一台常駐筆電（fh-proxy）、家裡的主力機（fh-l，可以用 wake-on-lan 開關），加上 docker，按需開關給 AI 用的 worker。
    - 住在 worker 裡的 AI 叫做**員工**。期 1 的員工使用 opencode。
- **AiStorage**（**本 repo**）：收著所有 AI 會用到的有狀態資料，分成四個各自獨立的儲存要素。
    - **MyBrain**（repo：`FATESAIKOU/MyBrain`，已經在運作）：個人的第二大腦，存進去的東西只用於決策參考（我是誰、我在哪裡、我要去哪裡）。**案件**只在這裡定義。
    - **Atelier**：員工的 harness 資料，以**職務**為單位，每個職務由 know / do / judge / dont 加上能力需求組成，並以 MLP 的某個 profile 為前提。需要安裝建置的東西（MCP 等）放在 MLP 的 profile，不放這裡。
    - **Agora**：所有來源應用的原始 Session 集中存放處。它讓之後啟動的 AI 能**接續**或**參考**前面的 Session，並附有一個可以擴充的搜尋介面。
    - **Foundry**：所有產出的集中管理處，**管理集中、儲存分散**。每件產出都登錄在產出目錄裡，但只有收容產出（簡報、圖片、影片、一次性報告）的真本放在 Foundry。
- **LLMGateway**：另一個獨立的專案（參考 sub2api），本期沒有相依。之後的隱私分級 tag 會放在 metadata 的擴充欄位。

有幾條原則貫穿整套基礎建設：

- **AiEntry 與 AiContainer 絕對不能互為前提**。少了 AiContainer，秘書會少很多能力，但仍然能用。兩者交接的方式是：秘書把 Session 連同交接單與指定的職務，經由 Agora 交給員工。
- AiStorage 是兩邊的**共同依賴**，所以 MUST NOT 寄居在 AiContainer 的機器上。

**目前的進度**

- 需求設計與基本設計都已經完成，並經過使用者 review。
- 實作還沒開始。
- 以下是權威來源。它們彼此衝突時，以 `docs/adr/` 與 `openspec/` 為準。

| 文件 | 內容 |
|---|---|
| `CONTEXT.md` | 術語表。**所有討論與產出都用這裡的詞，而且只用這裡的詞** |
| `docs/decision-log.md` | 需求 grill 的 31 個決定、分期、擴張點、外部前提、使用者的限制（英文） |
| `docs/adr/0001〜0006` | 架構決定與理由 |
| `openspec/changes/establish-aistorage-phase1/` | 期 1 的 proposal、10 份 specs、design（D1〜D8）、tasks（9 組） |
| `docs/research/` | 選型所依據的一手資料研究（object storage、Drive 與 GitHub 免費方案、git on Drive）。**不是權威來源**，用詞也可能跟 `CONTEXT.md` 不一致；design 有幾處是有意識地偏離研究的推薦（例如研究推薦 git 放 GitHub、大檔放 Drive，使用者則要求只放 Drive，見 ADR 0005） |
| `docs/resources.md` | 實體資源清單（Drive 資料夾、OAuth client、token 等，不記秘密的值）。技術驗證時開始填 |
| `docs/contracts/` | 跨 repo 的介面契約（tasks 2.5 產出；目前還沒建立）。第 8 組工單都以它為準 |

**期 1 設計的骨幹**（完整內容見 design.md）

```
 手機 App / worker / Mac ──只能放進自己 profile 的收件匣（Drive，drive.file scope）──▶
   提交流程（GitHub Actions，同一時間只跑一個）：
     驗證 → 蓋產生者章 → 轉出閱讀版 → git-annex commit/push 到 Drive 上的 repo → 發佈讀取視圖與搜尋索引
     Atelier 的修改提案：跑 judge，通過才推進 Atelier repo（GitHub private repo）
 讀取：能跑 git 的讀者用 service account 的唯讀權限 clone；手機讀讀取視圖
 身分：身分就是 profile，由 MLP 建 worker 時用 profile secrets 注入該 profile 的憑證
```

## 目的

完成 AiStorage **期 1** 的實作與測試，讓主場景「交接」端到端跑通：

1. 秘書在手機上把 Session S1 交給員工，交接時附上交接單與職務。
2. 員工以該職務啟動，讀 S1 的閱讀版接著做下去。
3. 員工的產出登錄進 Foundry。
4. 刪掉 worker 之後，Agora 與 Foundry 的內容都還在。

同時要讓「參考」情境可以用（用搜尋找到另一個 Session 來讀），並保留好期 2、期 3 的擴張點。期 1 的完整範圍與「不在本期」的清單，見 `proposal.md`。

## 步驟

1. **catchup：理解使用者相關的過去判準**
    - 用 `/mybrain-read` 讀 MyBrain，特別是 `骨幹` tag 的檔、`技術取捨準則`、`統一的兩端稅`、`個人 AiAgent 入口`、`MyLinuxPool`。
    - ⚠️ MyBrain 裡 2026-09-06「六要素作廢、LLMGateway 整條放棄」那一組判準已經被使用者**有意識地推翻**（見 `docs/decision-log.md` 的 Step 1 carry-over）。MyBrain 的更新 PR 還沒開，讀到時以本 repo 為準。
2. **理解需求與背景**
    - 讀完上面表格裡的權威來源，用自己的話向使用者複述需求、主場景與期 1 範圍，確認理解一致。
    - 有疑問就用提問工具問，不要猜。
3. **確認基本設計**
    - design.md 已經定案。你發現與事實或 spec 衝突的地方，先提出來由使用者決定，不要自行修改設計。
    - specs 寫的是「要做到什麼」，其中有幾項的可行性要靠技術驗證確認：數 GB 大檔的路徑、`drive.file` 的隔離（產生者章的可信度靠它）、Drive 上的 git-annex 本身。驗證不過，就回到這一步跟使用者重新選型，不要為了遷就實作而偷偷降低 spec。
    - 要改就用 `/opsx:update`，改完用 `openspec validate --strict` 確認。
4. **實作**
    - 照 `tasks.md` 依序進行，用 `/opsx:apply`。
    - **第 1 組是閘門**：技術驗證報告（`docs/spike/`）經使用者判斷 go 之前，不開始第 2 組之後的任務。驗證不過，就回到步驟 3 跟使用者重新選型。
    - 實作語言與框架還沒定。第 2 組開始前，先提案並取得使用者同意。提案至少要回答四件事：
        - 能不能順暢呼叫 git-annex 與 rclone 的 CLI。
        - 能不能在 GitHub Actions 與 worker（Ubuntu 容器）上跑。
        - 有沒有 SQLite FTS5 的 trigram 分詞可用。
        - judge 驗證腳本在哪種執行環境跑、隔離到什麼程度、逾時多久、失敗原因寫在哪裡。
      另外，讀取視圖的搜尋用戶端之後要被手機 App（MyAiEntry，Capacitor／TypeScript）使用，這一點也要一併考慮。
    - MyBrain 案件 id 的欄位名與格式，由第 8 組的 MyBrain PR 定案；AiStorage 這邊一律把 id 當成不透明的字串處理。
    - 第 8 組是其他 repo 的工單（MyLinuxPool、MyAiEntry、MyBrain）。每個工單照它自己 repo 的流程進行，也要先取得使用者同意。
5. **測試**
    - 每個任務都有對應的測試。整合測試只用測試專用的 Drive 資料夾與 repo。
    - 最後跑 `tasks.md` 第 9 組的端到端驗收（交接、參考、反向測試、家裡離線），把結果交使用者 review。

## 注意/限制事項

※ 每一步都要經過使用者 review，沒有得到明確的 OK 不准進下一步。

※ 不得同時進行多步。

※ 你必須擔任 PM，負責理解需求、基本設計、拆任務與驗收，不得自己一行一行看或寫程式碼。隊員三位：一位實作、一位寫測試、一位做 QA 初步審查。建議用 herdr 在隔壁 tab 開 opencode 當隊員。模型建議用 `opencode/muse-spark-1.3-contributor-free`（xhigh）。

※ 問使用者事情，一律使用提問工具，一次可以問多題，每題附上你建議的答案。

※ **隱私**：`muse-spark-1.3-contributor-free` 屬於 Contributor 方案，prompt 會被拿去訓練。使用者已經同意隊員讀本 repo 的設計文件；但 MyBrain 的內容、真實的 Session 內容、任何憑證，不要交給這類免費模型處理。

※ **能力邊界**：住民（秘書、員工）不得持有任何能改寫 Agora / Foundry / Atelier 真本的憑證。禁止的能力要做成「根本不存在」，不能靠 AI 遵守文字約定。dont 只是行為約定，不是安全邊界。

※ **抹除只有使用者本人能執行**（使用管理憑證）。AI 發現不該留存的內容時，只能以改寫遮蔽（舊版本仍在），並提醒使用者去抹除。

※ **MyBrain 的硬禁止**：AI 不得推 main，也不得 merge。期 1 員工對 MyBrain 只讀；寫入只走既有的 `/mybrain-write`（開 PR，由本人 review）。

※ **使用者的限制**：
- GitHub 只用免費方案，不升級。
- 儲存實體只用已經付費的 Google Drive（消費者 5TB 方案），不另外保留第二份副本；這個風險使用者已經明示接受。
- 盡量不新增常駐服務。
- 使用者每週大約只有 10〜20 小時，所以一切都分期，每一期都要留下擴張點。

※ **已知風險**（詳見 design.md 的 Risks）：
- 寫入是非同步的，接續前大約要等 1〜2 分鐘。
- GitHub Actions 每月 2,000 分鐘，跟 MLP 共用。
- OAuth client 必須設成「已發佈」，否則 refresh token 7 天就會過期。
- `drive.file` 的隔離要實測。
- 數 GB 的大檔要走雜湊命名物件的路徑。
