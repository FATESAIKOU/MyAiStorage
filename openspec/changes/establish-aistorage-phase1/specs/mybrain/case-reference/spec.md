## Purpose

定義 AiStorage 與 MyBrain 的銜接：案件以 MyBrain 主題檔裡不變的 id 被參照，AiStorage 把它當成不透明的字串；MyBrain 既有的寫入流程不變。

## ADDED Requirements

### Requirement: 案件以不變的 id 被參照
Agora 與 Foundry MUST 只以 MyBrain 案件的 id 參照案件，並 MUST 把 id 當成不透明的字串：不解析它的格式，也不假設它在 MyBrain 裡一定找得到。MyBrain 那邊為案件主題檔加上不變 id 的變更，以 MyBrain 自己的 PR 進行（在待辦清單），AiStorage 不以它為前提。

#### Scenario: 解析所屬案件（期 1 不驗收，依賴待辦清單裡的 MyBrain PR）
- **WHEN** MyBrain 已經為案件加上 id，讀者讀到一個 Session 的所屬案件 id
- **THEN** 讀者能在 MyBrain 找到對應的主題檔，取得案件的名稱與現況

#### Scenario: id 指不到案件
- **WHEN** 某個所屬案件 id 在 MyBrain 找不到對應的主題檔
- **THEN** 該 Session 照常可讀，案件以「找不到」標示，而不是讓讀取失敗

### Requirement: MyBrain 既有寫入流程不變
AiStorage MUST NOT 改變 MyBrain 既有的寫入規則，也 MUST NOT 提供另一條寫入 MyBrain 的路徑：AI 產出一律開 PR、預設為 draft，由本人 review 後才合併。

#### Scenario: AI 要記錄一個結論
- **WHEN** opencode 要把統合後得到的結論寫進 MyBrain
- **THEN** 它照既有的 mybrain-write 流程開 PR，AiStorage 不提供另一條寫入路徑
