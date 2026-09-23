## Purpose

定義 AiStorage 與 MyBrain 的銜接：案件以 MyBrain 主題檔裡不變的 id 被參照，員工在期 1 對 MyBrain 只讀，MyBrain 既有的寫入流程不變。

## ADDED Requirements

### Requirement: 案件以不變的 id 被參照
MyBrain 中代表案件的主題檔 MUST 在 metadata 帶有不變的 id；Agora 與 Foundry MUST 只以這個 id 參照案件。

#### Scenario: 解析所屬案件
- **WHEN** 員工讀到一個 Session 的所屬案件 id
- **THEN** 它能在 MyBrain 找到對應的主題檔，取得案件的名稱與現況

#### Scenario: id 指不到案件
- **WHEN** 某個所屬案件 id 在 MyBrain 找不到對應的主題檔
- **THEN** 該 Session 照常可讀，案件以「找不到」標示，而不是讓讀取失敗

### Requirement: 員工對 MyBrain 只讀
期 1 的員工身分 MUST 只能讀取 MyBrain，MUST NOT 能建立分支、開 PR、推送或合併。

#### Scenario: 員工嘗試寫入 MyBrain
- **WHEN** 員工嘗試對 MyBrain 推送一個分支
- **THEN** 操作失敗，因為員工的身分沒有寫入 MyBrain 的能力

### Requirement: MyBrain 既有寫入流程不變
AiStorage MUST NOT 改變 MyBrain 既有的寫入規則：AI 產出一律開 PR、預設為 draft，由本人 review 後才合併。

#### Scenario: 秘書要記錄一個結論
- **WHEN** 秘書要把交接後得到的結論寫進 MyBrain
- **THEN** 它照既有的 mybrain-write 流程開 PR，AiStorage 不提供另一條寫入路徑
