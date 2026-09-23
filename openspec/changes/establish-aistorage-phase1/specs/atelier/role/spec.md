## Purpose

定義 Atelier 的職務：員工啟動時載入的 know / do / judge / dont 與能力需求，以及職務如何被派到合適的 profile、如何在可驗證、可回滾的前提下被員工修改。

## ADDED Requirements

### Requirement: 職務的組成
每個職務 MUST 由 know、do、judge、dont 四部分與一份能力需求組成。dont 是行為約定，MUST NOT 被當成安全邊界使用。

#### Scenario: 載入職務
- **WHEN** 員工以某個職務啟動
- **THEN** 它取得該職務的 know、do、judge、dont，以及職務要啟用的能力清單

### Requirement: 能力需求與 profile 比對
職務 MUST 只能派到能力清單涵蓋它全部能力需求的 profile 上；員工啟動時 MUST 只啟用職務宣告的能力。

#### Scenario: 能力不足
- **WHEN** 秘書要把需要 chrome-devtools 能力的職務派到一個沒有該能力的 profile
- **THEN** 指派被拒絕，並說明缺少哪個能力

#### Scenario: 只啟用需要的能力
- **WHEN** 一個 profile 裝有三個 MCP，而職務只宣告需要其中一個
- **THEN** 員工只啟用那一個

### Requirement: 秘書在交接時指定職務
每次接續交接 MUST 指定一個職務；員工 MUST 以該職務啟動。

#### Scenario: 交接時未指定職務
- **WHEN** 秘書建立接續卻沒有指定職務
- **THEN** 交接不成立，並提示要指定職務

### Requirement: 職務版本
職務的每一次變更 MUST 產生一個新版本，所有舊版本 MUST 保留並可退回。每個員工的 Session MUST 在 metadata 記下它載入的職務與版本。

#### Scenario: 追查當時的 harness
- **WHEN** 你想知道某個員工上週做錯事時拿的是哪版 harness
- **THEN** 從該 Session 的 metadata 可以找到職務與版本，並取回那個版本的內容

### Requirement: 員工可改職務，但要過 judge
員工 MAY 修改職務。修改 MUST 通過該職務 judge 所定義的驗證才生效；驗證失敗時，職務維持在原版本。

#### Scenario: 修改沒通過驗證
- **WHEN** 員工修改了職務的 know，但 judge 的驗證腳本失敗
- **THEN** 修改不生效，職務版本不變，失敗原因被記錄

#### Scenario: 回滾
- **WHEN** 一個已生效的職務修改被發現有問題
- **THEN** 可以把職務退回上一個版本，之後啟動的員工載入的是退回後的版本
