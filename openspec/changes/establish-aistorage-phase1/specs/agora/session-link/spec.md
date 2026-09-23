## Purpose

定義 Session 之間的關係：接續與參考、接續點、交接單，以及分岔與收斂，讓一個新 Session 能承接或查閱多個較早的 Session，並把 Session 歸到 MyBrain 的案件底下。

## ADDED Requirements

### Requirement: Session Link 的兩種類型
Session Link MUST 是從新 Session 指向較早 Session 的有向關係，類型為「接續」或「參考」。一個 Session MAY 有多條 Session Link。Link 的類型集合 SHALL 可以擴充，不認得新類型的讀取者 MUST 忽略該 Link，而不是視為錯誤。

#### Scenario: 接續多個 Session
- **WHEN** 一個新 Session 同時接續兩個分岔的末端
- **THEN** 它有兩條接續 Link，分別指向那兩個 Session

### Requirement: 接續點
每條接續 Link MUST 記錄它的接續點。接續點之後被接續 Session 新增的內容，MUST NOT 被視為新 Session 承接的內容。

#### Scenario: 被接續後繼續聊
- **WHEN** S2 接續 S1 之後，你又在手機上對 S1 多講了幾句
- **THEN** S1 照常繼續，S2 的接續點不變，那幾句不屬於 S2 承接的範圍

### Requirement: 接續不影響被接續的 Session
接續 MUST NOT 改變被接續 Session 的狀態、內容或它在來源應用中的可用性。

#### Scenario: 分岔
- **WHEN** S1 被 S2、S3 兩個 Session 分別接續
- **THEN** S1、S2、S3 各自前進，任何一個都不因為另外兩個而被鎖住

### Requirement: 交接單
每條接續 Link MUST 附帶一張交接單，由發起接續的 AI 撰寫。接手者 SHALL 能先讀交接單，需要時再深入閱讀版。

#### Scenario: 員工接手
- **WHEN** 員工從一個接續 Link 開始工作
- **THEN** 它能讀到那張交接單，以及交接單所附接續點之前的閱讀版

### Requirement: 參考 Link
參考 Link MUST 記錄新 Session 查閱了哪個較早 Session。參考 MUST NOT 喚醒任何 AI，也 MUST NOT 承接工作。

#### Scenario: 查另一個 Session 的決定
- **WHEN** 員工為了知道 Agora 格式的決定而讀了另一個 Session
- **THEN** 它的 Session 多一條參考 Link，被參考的 Session 沒有任何變化

### Requirement: 所屬案件
Session 的所屬案件 MUST 以 MyBrain 案件的 id 表示，MAY 為空值。Agora MUST NOT 另外維護案件清單。

#### Scenario: 依案件列出 Session
- **WHEN** 你想看 AiStorage 這個案件底下的所有 Session
- **THEN** Agora 依所屬案件 id 列出它們，案件的名稱與狀態取自 MyBrain
