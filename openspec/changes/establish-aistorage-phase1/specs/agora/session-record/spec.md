## Purpose

定義 Agora 如何保存 Session：原始紀錄是真本、閱讀版可以重建，並規定版本保留、回滾、抹除與保存期限的規則，讓接續點與交接單永遠有效。

## ADDED Requirements

### Requirement: 原始紀錄是真本
Agora MUST 以來源應用自己的匯出單位保存每個 Session 的原始紀錄，作為該 Session 在 Agora 的真本；Agora MUST NOT 保存來源應用的憑證或與 Session 無關的資料。

#### Scenario: opencode 的 Session
- **WHEN** opencode 同步器同步一個 Session
- **THEN** Agora 保存該 Session 的 opencode 匯出內容，不包含 opencode 資料庫裡的帳號與憑證資料表

#### Scenario: 手動匯入的 Claude Code Session
- **WHEN** 你手動匯入一個 Claude Code Session
- **THEN** Agora 保存該 Session 的 jsonl 內容，不包含 Claude Code 的設定檔與憑證

### Requirement: 閱讀版可以從原始紀錄重建
Agora MUST 為每個 Session 提供共通格式的閱讀版，閱讀版 MUST 能完全由原始紀錄重新產生。閱讀版與原始紀錄不一致時，以原始紀錄為準。

#### Scenario: 閱讀版格式升級
- **WHEN** 閱讀版的共通格式改版
- **THEN** 所有 Session 的閱讀版可以從原始紀錄重新產生，不需要回到來源應用

#### Scenario: 跨應用讀取
- **WHEN** opencode 要讀一個手動匯入的 Claude Code Session
- **THEN** 它讀閱讀版就能理解內容，不需要懂 Claude Code 的原始格式

### Requirement: Session 狀態
每個 Session MUST 帶有「運作中」或「停止中」其中一種狀態，並在 metadata 記下停止的時間。停止中 MUST 只依來源應用的可信訊號判定；來源應用沒有可信訊號時，停止中 MUST 由你或 AI 明確宣告，MUST NOT 從閒置時間推測。停止中的 Session 又被追加內容時，同步器 MUST 立刻同步並提交一次，讓它回到運作中。

#### Scenario: 明確宣告停止
- **WHEN** opencode 在工作做完時宣告 Session S2 停止，並同步一次
- **THEN** S2 在 Agora 的狀態是停止中，記有停止時間

#### Scenario: 停止後又被恢復
- **WHEN** 已宣告停止的 S2 又被你在 opencode 裡接著對話，同步器再同步一次
- **THEN** 同步器偵測到宣告停止後又有新內容，立刻同步並提交；S2 回到運作中，之後讀取它的讀者照常看到新鮮度警告。從恢復對話到這次提交完成之間，讀者仍可能讀到停止時的內容而沒有警告（窗口約一次提交流程的耗時）

### Requirement: 版本保留與回滾
Session 的每一個新版本都 MUST 保留舊版本，並且 MUST 能回滾到任何舊版本。來源應用自己對 Session 的編輯（例如 /rewind、/undo 之後重新輸入、刪掉後面的訊息），對 Agora 而言就是原始紀錄的新版本，MUST 照常收下，舊版本留在歷史裡，已記錄的接續點不受影響（接續點依據的是當時的快照）。期 1 不提供「在 Agora 裡修改 Session 內容」的改寫功能（2026-09-27 使用者決定，列在 `docs/backlog.md`）；要移除內容一律用抹除。

#### Scenario: 來源端的編輯
- **WHEN** 你在 opencode 裡 /undo 了一個 Session 的最後幾則訊息，同步器同步了新版本
- **THEN** Agora 收下這個新版本，沒有把它當成違反位置規則的改寫而拒收；舊版本仍然可以取回

#### Scenario: 回滾
- **WHEN** 你要求把某個 Session 回到上週的版本
- **THEN** 原始紀錄回到那個版本，閱讀版隨之重建

### Requirement: 抹除
Agora MUST 提供抹除：真正刪除原始紀錄的某一段或整個 Session，連同它的舊版本與衍生物。「舊版本與衍生物」MUST 涵蓋 git 歷史、儲存實體自己保留的舊版本與垃圾桶、讀取視圖與搜尋索引、收件匣裡尚未刪除的項目；已知的 clone 與快取 MUST 一併處理或列出來提醒你。抹除後 MUST 只留下「誰、何時、為什麼、抹了哪一段」的紀錄，不留任何被抹除的內容。抹除 MUST 只能由你本人以管理身分執行；AI 發現不該留存的內容時，只能提醒你抹除。

#### Scenario: AI 發現機敏內容
- **WHEN** opencode 在某個 Session 裡發現一把印出來的 token
- **THEN** 它只能提醒你抹除；它沒有能力讓內容或舊版本消失

#### Scenario: 憑證外洩
- **WHEN** 你發現某個 Session 的工具輸出裡印出了一把 token，並要求抹除那一段
- **THEN** 該段內容從目前版本、所有舊版本與閱讀版中消失，Agora 只留下抹除紀錄

### Requirement: 永久保存
Agora MUST 預設永久保存所有 Session；抹除是唯一的移除方式，不得有自動過期。

#### Scenario: 來源應用刪除了自己的紀錄
- **WHEN** opencode 或 Claude Code 在本機刪除了一個已同步過（或已匯入）的 Session
- **THEN** Agora 裡的該 Session 不受影響

### Requirement: 單一 Session 手動匯入
Agora MUST 允許把單一個現存的 Session 手動匯入，匯入結果與同步器寫入的 Session 形式相同。期 1 的手動匯入 MUST 支援 opencode 與 Claude Code 兩種來源；手機 App 的匯入與 Claude Code 的自動同步在待辦清單。

#### Scenario: 匯入一個舊的 Claude Code Session
- **WHEN** 你請 AI 把上個月某個 Claude Code Session 匯入 Agora
- **THEN** 該 Session 出現在 Agora，具備原始紀錄、閱讀版與共通 metadata
