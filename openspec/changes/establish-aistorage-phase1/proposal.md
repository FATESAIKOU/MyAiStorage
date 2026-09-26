## Why

個人 AI 基礎建設裡，秘書（AiEntry 的住民）已經能在手機上工作，AiContainer 也有了 worker（但還不能跑 AI agent），而整個系統沒有「狀態」。

AiStorage 要做的是**外部化狀態**：先問這個 AI 系統應該記住什麼，才能跨 AI、跨裝置、跨時間運作，再把這些狀態設計成獨立的一層，放在所有執行體之外。手機 App、worker、Mac 都只是無狀態的工作場所。這不是把各應用既有的狀態備份出去：狀態的形狀由系統的需要決定，不由來源應用決定。

期 1 要回答的問題是：**這套工作體系撐不撐得住 Session 之間的三種關係**。它們分別對應 AI 產出產物時的三種工作場景：

| 關係 | 工作場景 | 詞彙（`CONTEXT.md`） |
|---|---|---|
| 分裂（1→n） | 切分工作 | 持有者為每一份工作各寫一張交接單，多個新 Session 各自認領（分岔） |
| 統合（n→1） | 聚合成果 | 每個分岔各寫一張交接單交出末端，一個新 Session 認領全部（收斂） |
| 相互參照（n↔m） | 互相支持推進 | 並行的 Session 彼此建立參考 Link，讀對方最新已提交的內容 |

## What Changes

期 1 只建立驗證這三種關係所需的最小集合，並留好擴張點。扮演 AI 的是 **Mac 上的 opencode**（在隔離的容器裡執行，見 design D3）。秘書與員工都不在期 1：worker 目前還不能跑 AI agent，手機 App 的整合排在待辦清單。期 1 之後不再分期規劃，其餘事項一律收在 `docs/backlog.md`。詞彙依 `CONTEXT.md`，架構決定依 `docs/adr/0001〜0007`。

- **共通約定**：
  - 每個項目都由 metadata 與本體組成。共通 metadata 有六個欄位：id（不變）、型態、產生者、時間、所屬案件、出處。
  - 身分以 profile 為單位；各要素依 profile 授權，產生者由介面依認證結果蓋章。期 1 的 profile 憑證由你本人手動安裝，「由 MyLinuxPool 證明 profile 歸屬」等 MLP 的 profile 重新設計後再接（工單見 `docs/tickets/mylinuxpool.md`）。
  - **讀寫分離**（ADR 0007）：每個要素的讀取手段只有一個讀取介面，讀的時候可以指定新鮮度；寫入端用來維持新鮮度的機制依成本選擇、之後再追加，期 1 只給最便宜的一種。讀取介面的形狀不隨寫入機制改變。
- **Agora**：
  - 儲存：原始紀錄是真本，閱讀版可以重建；Session 有狀態（運作中／停止中）。
  - 變更：改寫可回滾，而且不改變位置；另有抹除，只有你本人能執行。
  - 關係：Session Link（接續／參考）、接續點；接續經由交接單與認領建立（design D10）；分裂、統合與相互參照都要成立。
  - 同步：opencode 同步器（Mac），平時定期同步；寫交接單前、或想讓別的 Session 讀到進度時，同步並提交一次。停止中由明確宣告，不從閒置推測。
  - 讀取：搜尋（篩選＋全文）、讀取 Session（含兩個方向的 Link）、列出待認領的交接單，都經由同一個讀取介面，每筆結果附快照時間。
  - 其他：單一 Session 手動匯入（opencode 與 Claude Code）、永久保存。
- **Foundry（最小）**：
  - 一份產出目錄登錄所有產出，記錄產出它的 Session。
  - 收容產出的真本放在 Foundry；期 1 只收經提交流程轉手的檔案（上限見 design D7），數 GB 的路徑在待辦清單。
  - 原處產出由產生它的 AI 明確登錄出處。
- **MyBrain 銜接**：
  - 所屬案件以 MyBrain 案件的 id 表示，AiStorage 一律把它當成不透明的字串。MyBrain 那邊加 id 的 PR 在待辦清單。
- **Atelier**：
  - 期 1 只做需求設計：需要哪些部門（profile）、需要哪些職能（職務），寫成 `docs/atelier/`。基本設計與實作等 MyLinuxPool 的 profile 重新設計之後再一起考慮。
- **不在期 1**：一律收在 `docs/backlog.md`，包括手機秘書的整合、worker 與員工、Atelier 的實作、MyBrain 的 id PR、數 GB 的收容產出、Mac 本機既有 agent 的自動同步、摘要、收斂 AI、語意搜尋、Foundry 自動收錄與死連結檢查、更高新鮮度的寫入機制。更早的歷史 Session 不整批回補。

**期 1 留好的擴張點**：

1. metadata 可以加欄位（之後 LLMGateway 的隱私 tag 放這裡）。
2. Session Link 的類型可以擴充。
3. 讀取介面的搜尋後端可以替換或疊加（全文 → 語意）。
4. 衍生層可以疊加（閱讀版 → 摘要 → …），全部都能從原始紀錄重建。
5. 每個來源應用一個同步器。
6. 身分粒度從第一天起就是 profile。
7. 寫入機制可以依成本追加，讀取介面不變。
8. 交接單、接續 Link 與 Session metadata 預留 `role`、`role_version` 欄位（之後的 Atelier）。
9. 讀取授權期 1 以要素為單位；之後讀取視圖可以依可見範圍分資料夾，做內容層級的讀取授權。

## Capabilities

### New Capabilities

- `common/item-model`：項目＝metadata＋本體、共通 metadata 六欄位、不變的 id、項目之間互相參照的方式，以及 metadata 可以加欄位的擴張點。
- `common/identity`：身分＝profile、profile 憑證證明歸屬、各要素依 profile 授權、產生者蓋章，以及期 1 的身分種類。
- `agora/session-record`：原始紀錄與閱讀版、Session 狀態、改寫與抹除、永久保存、單一 Session 手動匯入。
- `agora/session-sync`：來源應用的同步器、定期同步與接續前同步、寫入端維持新鮮度的機制。
- `agora/session-link`：接續與參考、接續點、交接單與認領、分裂、統合與相互參照、所屬案件。
- `agora/search`：Agora 的讀取介面：以所屬案件、來源應用、時間、標題、狀態篩選，對閱讀版全文搜尋，讀取 Session（含兩個方向的 Link），列出待認領的交接單；指定新鮮度與快照時間；搜尋後端可以替換或疊加。
- `foundry/catalog`：產出目錄、原處產出的登錄、收容產出的儲存、依新鮮度規則查詢。
- `mybrain/case-reference`：以不透明的案件 id 參照 MyBrain 案件、MyBrain 既有寫入流程不變。

Atelier 的兩份需求（職務、外部副本）移到 `docs/atelier/`，作為需求設計的一部分，不在本 change 實作。

### Modified Capabilities

（無。這是新 repo，`openspec/specs/` 目前是空的。）

## Impact

- **新系統**：本 repo（`FATESAIKOU/MyAiStorage`）。儲存實體是 Google Drive 5TB（已付費），放在 AiStorage 專用的 Google 帳號（家庭共用成員，共用配額），提交流程的憑證碰不到你個人的 Drive；GitHub 維持免費方案。
- **Mac**：期 1 的 opencode 在 Mac 上的容器（colima）裡執行，每個 Session 各自一個容器，只掛載白名單上的憑證（Mac opencode profile 的憑證、LLM 金鑰），碰不到你本人在 Mac 上的其他憑證。
- **MyLinuxPool**：期 1 沒有相依。profile 歸屬的證明、憑證注入、能力清單格式、worker 的 opencode 與刪除前同步，整理成工單 `docs/tickets/mylinuxpool.md`，等 profile 重新設計時一起考慮。
- **MyAiEntry**：期 1 沒有相依，整合項目在待辦清單。
- **MyBrain**：期 1 沒有相依。加不變 id 的 PR，以及更新 2026-09-06 那組已被推翻的判準筆記，都在待辦清單。
- **LLMGateway**：本期沒有相依。之後的隱私 tag 放在 metadata 的擴張欄位。
