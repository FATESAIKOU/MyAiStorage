## Purpose

定義住民與同步程式如何向儲存要素證明自己是誰、儲存要素如何依此授權，讓權限的上限落在可以被證明的 profile 上，而不是 AI 自己的宣稱。

## ADDED Requirements

### Requirement: 身分即 profile
每個存取 AiStorage 的執行體（Mac 容器裡的 opencode 與同步器，之後的 worker、手機 App）MUST 以它所屬的 profile 作為身分。職務與 AI 自己的宣稱 MUST NOT 作為身分或授權依據。

#### Scenario: AI 宣稱別的身分
- **WHEN** Mac 上的 opencode 在寫入要求中自稱屬於 worker/default
- **THEN** 儲存要素只依它實際持有的憑證所屬的 profile（Mac opencode）判斷，自稱的值不影響結果

### Requirement: 以 profile 憑證證明歸屬
執行體屬於哪個 profile MUST 由它持有的該 profile 憑證證明，而憑證 MUST 只有一個發放來源。存取儲存實體的憑證（Drive 寫入與讀取）MAY 由多個 profile 共用；這時 profile 的歸屬 MUST 由每個 profile 各自一把的簽章金鑰證明：寫入者對它放進收件匣的項目簽章，提交流程驗章，沒有簽章或驗章失敗的項目 MUST 被拒收。目標的發放來源是 MyLinuxPool（它的 profile 重新設計完成後接上）；期 1 由你本人手動安裝，並記在 `docs/resources.md`（不記秘密的值）。儲存要素 MUST NOT 另外維護一份會與發放來源分歧的身分清單。撤銷的單位 SHALL 是 profile：要讓某個 profile 失去寫入能力，就撤銷它的簽章金鑰；共用的存取憑證輪替時，影響所有共用它的 profile。

#### Scenario: 撤銷一個 profile
- **WHEN** 你懷疑 Mac opencode 的簽章金鑰外洩，撤銷了它
- **THEN** 之後以舊金鑰簽章的項目一律被拒收，其他 profile 不受影響；換上新金鑰的執行體照常運作

#### Scenario: 別的 profile 冒充產生者
- **WHEN** 共用同一組 Drive 憑證的另一個 worker，把一個項目放進 Mac opencode 的收件匣，但沒有 Mac opencode 的簽章金鑰
- **THEN** 提交流程驗章失敗，拒收該項目；收件匣的位置不構成產生者的證明

### Requirement: 授權由各儲存要素持有
每個儲存要素 MUST 自行持有「哪個 profile 可以做哪些操作」的授權規則；執行體 MUST 只需要證明自己是誰，不需要知道自己被允許做什麼。

#### Scenario: 被拒絕的操作
- **WHEN** 一個不在 Agora 授權規則裡的 profile，把項目放進它自己的收件匣
- **THEN** 提交流程拒收該項目、記下原因是授權不足，真本不變

### Requirement: 禁止的能力不存在
儲存要素對某個 profile 不允許的操作，MUST 以「該身分根本沒有這個能力」的方式實現，不得只依賴 AI 遵守文字約定。唯一的有意識例外（ADR 0008）：住民的憑證仍然能在真本所在的資料夾「建立」檔案；對這一點，儲存要素 MUST 保證被建立的檔案不會被當成真本的一部分（偵測、隔離，真本的完整性由住民碰不到的釘選值保證），改寫與刪除真本的能力仍然 MUST 不存在。

#### Scenario: AI 嘗試破壞歷史
- **WHEN** Mac 上的 opencode 用它拿到的憑證，嘗試刪除 Agora 某個原始紀錄的舊版本
- **THEN** 操作失敗，因為該身分的憑證本身不具有刪除舊版本的能力

#### Scenario: AI 在 repo 資料夾放入偽造的歷史
- **WHEN** Mac 上的 opencode 用它的憑證，在 Agora 的 repo 資料夾裡放入同名的偽造 manifest 與 bundle
- **THEN** 提交流程偵測到不符、把它們隔離並中止這一輪；偽造的內容沒有成為真本，讀取介面讀到的仍是上一次提交的內容；清掃之後提交恢復正常

### Requirement: 期 1 的身分種類
期 1 MUST 支援 Mac opencode profile，並 MUST 另有至少一個測試用 profile，用來驗證簽章與產生者章。之後新增 profile（worker/default、手機 App 等）MUST NOT 需要修改身分模型本身。

#### Scenario: 新增一種 profile
- **WHEN** 之後新增手機 App profile
- **THEN** 只需要發放該 profile 的憑證，並在各儲存要素的授權規則中加入它的條目；身分模型與既有條目都不變
