## Purpose

定義住民與同步程式如何向儲存要素證明自己是誰、儲存要素如何依此授權，讓權限的上限落在可以被證明的 profile 上，而不是 AI 自己的宣稱。

## ADDED Requirements

### Requirement: 身分即 profile
每個存取 AiStorage 的執行體（worker、手機 App、同步程式）MUST 以它所屬的 profile 作為身分。職務 MUST NOT 作為身分或授權依據。

#### Scenario: 員工宣稱職務
- **WHEN** 載入「FinDashboard 開發」職務的員工，以該職務名義要求寫入 MyBrain
- **THEN** 儲存要素只依該員工所屬的 profile 判斷，職務名稱不影響結果

### Requirement: MyLinuxPool 證明 profile 歸屬
執行體屬於哪個 profile MUST 由 MyLinuxPool 證明；儲存要素 MUST NOT 另外維護一份會與 MyLinuxPool 分歧的身分清單。撤銷的單位 SHALL 是 profile：要讓某個 profile 失去存取能力，就整組輪替該 profile 的憑證。

#### Scenario: worker 被刪除
- **WHEN** 某個 worker 在 MyLinuxPool 被刪除
- **THEN** 那台 worker 手上的憑證跟著消失；同一個 profile 的其他 worker 不受影響

#### Scenario: 撤銷一個 profile
- **WHEN** 你懷疑 worker/default 的憑證外洩，輪替了該 profile 的憑證
- **THEN** 舊憑證向任何儲存要素認證都會失敗，之後新建的 worker 拿到的是新憑證

### Requirement: 授權由各儲存要素持有
每個儲存要素 MUST 自行持有「哪個 profile 可以做哪些操作」的授權規則；執行體 MUST 只需要證明自己是誰，不需要知道自己被允許做什麼。

#### Scenario: 被拒絕的操作
- **WHEN** worker/default profile 的員工要求對 MyBrain 開 PR
- **THEN** MyBrain 依它自己的規則拒絕（期 1 員工對 MyBrain 只讀），並回報是授權不足

### Requirement: 禁止的能力不存在
儲存要素對某個 profile 不允許的操作，MUST 以「該身分根本沒有這個能力」的方式實現，不得只依賴 AI 遵守文字約定。

#### Scenario: 員工嘗試破壞歷史
- **WHEN** 員工用自己拿到的憑證，嘗試刪除 Agora 某個原始紀錄的舊版本
- **THEN** 操作失敗，因為該身分的憑證本身不具有刪除舊版本的能力

### Requirement: 期 1 的身分種類
期 1 MUST 至少支援三種 profile 身分：worker/default（員工）、手機 App（秘書與它的同步器）、Mac 同步程式。之後新增 profile MUST NOT 需要修改身分模型本身。

#### Scenario: 新增一種 profile
- **WHEN** 之後在 MyLinuxPool 新增一個 worker/browser profile
- **THEN** 只需要在各儲存要素的授權規則中加入這個 profile 的條目，身分模型與既有條目都不變
