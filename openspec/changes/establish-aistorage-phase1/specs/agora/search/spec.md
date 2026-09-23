## Purpose

定義 Agora 的搜尋介面：讓 AI 在不知道 Session id 時，用條件篩選與全文搜尋找到要接續或參考的 Session，之後可以再疊加語意搜尋。

## ADDED Requirements

### Requirement: 條件篩選
搜尋介面 MUST 支援以所屬案件、來源應用、時間範圍、標題、狀態篩選 Session，條件之間可以組合。

#### Scenario: 找某個案件最近停止的 Session
- **WHEN** 員工以「所屬案件＝AiStorage、狀態＝停止中、最近 7 天」搜尋
- **THEN** 回傳符合的 Session 清單，每筆帶有 id、標題、來源應用、時間與狀態

### Requirement: 全文搜尋
搜尋介面 MUST 支援對閱讀版內容的全文搜尋，並能與條件篩選組合。

#### Scenario: 用關鍵字找決定
- **WHEN** 員工搜尋「接續點」並限定所屬案件為 AiStorage
- **THEN** 回傳閱讀版中含有該詞的 Session，並標出命中的位置

### Requirement: 搜尋結果尊重授權
搜尋介面 MUST 只回傳呼叫者身分被允許讀取的 Session。

#### Scenario: 未授權的身分
- **WHEN** 一個沒有 Agora 讀取權的身分呼叫搜尋介面
- **THEN** 搜尋被拒絕，而不是回傳空結果

### Requirement: 搜尋後端可以替換或疊加
搜尋介面對呼叫者的形狀 MUST 不因搜尋後端改變而改變；之後加入語意搜尋時，既有的篩選與全文搜尋呼叫方式 MUST 照常可用。

#### Scenario: 期 2 加入語意搜尋
- **WHEN** 期 2 為搜尋介面加入語意搜尋
- **THEN** 期 1 寫好的篩選與全文搜尋呼叫不需要修改
