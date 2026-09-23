## Why

個人 AI 基礎建設裡，秘書（AiEntry 的住民）已經能在手機上工作，AiContainer 也有了可用的 worker，但兩邊之間沒有「有狀態的資料層」。

- 手機 App 的 Session 只存在裝置本身，沒有任何備份。
- Mac 上 Claude Code 的 Session 約 30 天就會被自動清除。
- worker 用完就丟，裡面的工作一刪就沒了。
- AI 的產出散在各個 repo，還有沒有 remote 的本機目錄裡。

所以「秘書把工作交給員工接續」這個主場景現在做不到，任何 AI 也無法參考另一個 AI 做過的事。

## What Changes

本期（期 1）只建立讓主場景「交接」跑通的最小集合，並留好之後各期的擴張點。詞彙依 `CONTEXT.md`，架構決定依 `docs/adr/0001〜0004`。

- **共通約定**：
  - 每個項目都由 metadata 與本體組成。共通 metadata 有六個欄位：id（不變）、型態、產生者、時間、所屬案件、出處。
  - 身分以 profile 為單位，由 MyLinuxPool 證明；各要素依 profile 授權，產生者由介面依認證結果蓋章。
- **Agora**：
  - 儲存：原始紀錄是真本，閱讀版可以重建；Session 有狀態（運作中／停止中）。
  - 變更：改寫可回滾，而且不改變位置；另有抹除。
  - 關係：Session Link（接續／參考）、接續點、交接單。
  - 同步：手機 App 與 opencode 兩種同步器，平時定期同步，發起接續前再同步一次。
  - 其他：搜尋介面（篩選＋全文）、單一 Session 手動匯入（支援手機 App、opencode、Claude Code）、永久保存。抹除只有你本人能執行。
- **Atelier**：
  - 職務由 know / do / judge / dont 加能力需求組成，並檢查能力需求與 profile 的能力清單。
  - 職務版本：員工可以改，改動要過 judge 才生效，可以回滾；每個 Session 記下它所用的職務版本。
  - 外部副本記下出處與版本，與上游的比對用手動觸發。
- **Foundry**：
  - 一份產出目錄登錄所有產出。
  - 收容產出的真本放在 Foundry；原處產出由員工明確登錄出處。
- **MyBrain 銜接**：
  - 案件主題檔加上不變的 id，這項變更以另一個 MyBrain PR 進行。
  - 員工對 MyBrain 只讀。
- **不在本期**：
  - 期 2：Mac 本機的 Claude Code / opencode / agy / codex 同步；摘要、收斂 AI、語意搜尋；外部副本自動比對；Foundry 自動收錄與死連結檢查；員工寫入 MyBrain。
  - 期 3：秘書 harness 移入 Atelier；Mac 本機與 LearnGhAgent 成為 Atelier 消費者；重要產出快照。
  - 更早的歷史 Session 不整批回補。

## Capabilities

### New Capabilities

- `common/item-model`：項目＝metadata＋本體、共通 metadata 六欄位、不變的 id、項目之間互相參照的方式，以及 metadata 可以加欄位的擴張點。
- `common/identity`：身分＝profile、MyLinuxPool 證明歸屬、各要素依 profile 授權、產生者蓋章，以及期 1 的三種身分。
- `agora/session-record`：原始紀錄與閱讀版、Session 狀態、改寫與抹除、永久保存、單一 Session 手動匯入。
- `agora/session-sync`：來源應用的同步器、定期同步與接續前同步、worker 刪除前同步完成。
- `agora/session-link`：接續與參考、接續點、交接單、分岔與收斂、所屬案件。
- `agora/search`：以所屬案件、來源應用、時間、標題、狀態篩選，對閱讀版全文搜尋；搜尋後端可以替換或疊加。
- `atelier/role`：職務的組成、能力需求與 profile 能力清單的比對、職務版本與 judge 驗證、回滾、Session 記錄職務版本。
- `atelier/external-copy`：外部副本的出處與版本、與上游的內容比對（手動觸發）、更新與汰換的提出。
- `foundry/catalog`：產出目錄、原處產出的登錄、收容產出的儲存。
- `mybrain/case-reference`：以案件 id 參照 MyBrain 案件、員工對 MyBrain 只讀。

### Modified Capabilities

（無。這是新 repo，`openspec/specs/` 目前是空的。）

## Impact

- **新系統**：本 repo（`FATESAIKOU/MyAiStorage`）。儲存實體從現有訂閱中選：Google Drive 5TB、AWS／Linode object storage；GitHub 維持免費方案。
- **MyAiEntry（AiEntry）**：
  - 要加 Session 同步器。
  - 要加交接動作：先同步、寫交接單、指定職務、建立接續的 Session Link。
  - 要能向 AiStorage 證明身分。
  - 秘書本身的 harness 不變（ADR 0006 維持）。
- **MyLinuxPool（AiContainer）**：
  - worker image 要裝 opencode。
  - worker 刪除前要把員工的 Session 同步完。
  - profile 要能宣告能力清單與 AiStorage 用的身分；手機 App 與同步程式也要登記成 profile。
- **MyBrain**：
  - OKF 格式與 validator 要加上不變的 id 欄位。
  - 2026-09-06 那組判準已被有意識地推翻，相關筆記要更新（另開 PR）。
- **LLMGateway**：本期沒有相依。之後的隱私 tag 放在 metadata 的擴張欄位。
