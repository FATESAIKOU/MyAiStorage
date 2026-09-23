## Purpose

定義 Atelier 如何收納不是你寫、或屬於別的系統的 skill：複製成外部副本並記下出處與版本，讓員工只依賴 Atelier，同時用內容比對找出副本與上游的落差。

## ADDED Requirements

### Requirement: 外部 skill 以副本收納
職務用到的第三方 skill，或屬於別的系統的 skill（例如 MyBrain 的 `mybrain-*`），MUST 以外部副本的形式存在 Atelier 中；員工載入職務時 MUST NOT 在執行期去外部來源取得 skill。

#### Scenario: 上游暫時連不到
- **WHEN** 員工啟動時，第三方 skill 的上游 repo 無法連線
- **THEN** 員工照常從 Atelier 載入該 skill 的副本

### Requirement: 副本記錄出處與版本
每份外部副本的 metadata MUST 記錄它的出處（上游位置）與複製時的上游版本。

#### Scenario: 查副本來源
- **WHEN** 你想知道 Atelier 裡的 `tdd` skill 是從哪個版本複製來的
- **THEN** 從該副本的 metadata 可以找到上游位置與版本

### Requirement: 與上游比對
Atelier MUST 提供與上游的內容比對，比的是內容而不是版本戳記。期 1 由人或 AI 手動觸發。比對結果 MUST 把副本分成三類：一致、有落差、上游已消失。

#### Scenario: 上游改了規則
- **WHEN** MyBrain 的 `mybrain-write` 在上游改了規則後，觸發比對
- **THEN** 該副本被標為有落差，並提出一筆更新

#### Scenario: 上游消失
- **WHEN** 某個第三方 skill 的上游 repo 已被刪除，觸發比對
- **THEN** 該副本被標為上游已消失，並提出汰換

### Requirement: 更新要過 judge
由比對提出的副本更新，MUST 通過使用該副本的職務的 judge 驗證後才套用，並 MUST 產生新的職務版本。

#### Scenario: 更新造成驗證失敗
- **WHEN** 套用某個副本更新後，某個職務的 judge 驗證失敗
- **THEN** 該更新不套用到那個職務，職務維持原版本
