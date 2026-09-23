## Purpose

定義 Agora 如何保存 Session：原始紀錄是真本、閱讀版可以重建，並規定改寫、抹除與保存期限的規則，讓接續點與交接單永遠有效。

## ADDED Requirements

### Requirement: 原始紀錄是真本
Agora MUST 以來源應用自己的匯出單位保存每個 Session 的原始紀錄，作為該 Session 在 Agora 的真本；Agora MUST NOT 保存來源應用的憑證或與 Session 無關的資料。

#### Scenario: opencode 的 Session
- **WHEN** opencode 同步器同步一個 Session
- **THEN** Agora 保存該 Session 的 opencode 匯出內容，不包含 opencode 資料庫裡的帳號與憑證資料表

#### Scenario: 手機 App 的 Session
- **WHEN** 手機 App 同步器同步一個 Session
- **THEN** Agora 保存該 Session 的記錄（含內嵌圖片），不包含 App 另存的 SSH 私鑰、LLM key 或 PAT

### Requirement: 閱讀版可以從原始紀錄重建
Agora MUST 為每個 Session 提供共通格式的閱讀版，閱讀版 MUST 能完全由原始紀錄重新產生。閱讀版與原始紀錄不一致時，以原始紀錄為準。

#### Scenario: 閱讀版格式升級
- **WHEN** 閱讀版的共通格式改版
- **THEN** 所有 Session 的閱讀版可以從原始紀錄重新產生，不需要回到來源應用

#### Scenario: 跨應用讀取
- **WHEN** 員工（opencode）要讀一個手機 App 產生的 Session
- **THEN** 它讀閱讀版就能理解內容，不需要懂手機 App 的原始格式

### Requirement: Session 狀態
每個 Session MUST 帶有「運作中」或「停止中」其中一種狀態，由同步器依來源應用的情況回報。

#### Scenario: 來源應用結束 Session
- **WHEN** 來源應用回報某個 Session 不會再追加內容
- **THEN** 下一次同步後，該 Session 在 Agora 的狀態是停止中

### Requirement: 改寫可回滾且不改變位置
對原始紀錄的改寫 MUST 保留改寫前的版本與一筆變更紀錄（誰、何時、為什麼），並且 MUST 能回滾到任何舊版本。改寫 MUST NOT 改變內容的位置，任何已記錄的接續點都必須仍然指向同一段內容。你本人與 AI 都 MAY 改寫。

#### Scenario: AI 遮蔽一段內容
- **WHEN** 員工把某個 Session 第 12 則訊息裡的一段文字換成遮蔽標記
- **THEN** 第 12 則訊息仍在原位，改寫前的版本仍可取回，變更紀錄記下是哪個身分、何時、為什麼

#### Scenario: 回滾
- **WHEN** 你要求把某個 Session 回到上週的版本
- **THEN** 原始紀錄回到那個版本，閱讀版隨之重建

### Requirement: 抹除
Agora MUST 提供抹除：真正刪除原始紀錄的某一段或整個 Session，連同它的舊版本與衍生物。抹除後 MUST 只留下「誰、何時、為什麼、抹了哪一段」的紀錄，不留任何被抹除的內容。抹除 MUST 只能由你本人以管理身分執行；AI 發現不該留存的內容時，只能以改寫遮蔽它（舊版本仍在），並提醒你抹除。

#### Scenario: AI 發現機敏內容
- **WHEN** 員工在某個 Session 裡發現一把印出來的 token
- **THEN** 它只能提出改寫把那段遮蔽掉，並提醒你抹除；它沒有能力讓舊版本消失

#### Scenario: 憑證外洩
- **WHEN** 你發現某個 Session 的工具輸出裡印出了一把 token，並要求抹除那一段
- **THEN** 該段內容從目前版本、所有舊版本與閱讀版中消失，Agora 只留下抹除紀錄

### Requirement: 永久保存
Agora MUST 預設永久保存所有 Session；抹除是唯一的移除方式，不得有自動過期。

#### Scenario: 來源應用刪除了自己的紀錄
- **WHEN** Claude Code 或手機 App 在本機刪除了一個已同步過的 Session
- **THEN** Agora 裡的該 Session 不受影響

### Requirement: 單一 Session 手動匯入
Agora MUST 允許把單一個現存的 Session 手動匯入，匯入結果與同步器寫入的 Session 形式相同。期 1 的手動匯入 MUST 支援手機 App、opencode 與 Claude Code 三種來源（Claude Code 的自動同步仍屬期 2）。

#### Scenario: 匯入一個舊的 Claude Code Session
- **WHEN** 你請 AI 把上個月某個 Claude Code Session 匯入 Agora
- **THEN** 該 Session 出現在 Agora，具備原始紀錄、閱讀版與共通 metadata
