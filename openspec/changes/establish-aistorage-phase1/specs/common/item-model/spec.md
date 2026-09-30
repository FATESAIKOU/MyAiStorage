## Purpose

定義四個儲存要素共用的「項目」形狀與共通 metadata，讓 MyBrain、Atelier、Agora、Foundry 的項目能互相參照，而不必共用儲存實體或介面。

## ADDED Requirements

### Requirement: 項目由 metadata 與本體組成
每個儲存要素裡的每個項目 MUST 由 metadata 與本體兩部分組成。項目只是指向別處的連結時，本體 SHALL 保存那個對外連結。

#### Scenario: 連結型項目
- **WHEN** opencode 在 Foundry 登錄一份留在 FinDashboard repo 的報告
- **THEN** 該項目的本體保存指向那份報告的連結，metadata 照常具備共通欄位

#### Scenario: 實體型項目
- **WHEN** 同步器把一個 Session 寫進 Agora
- **THEN** 該項目的本體是原始紀錄，metadata 另外存放

### Requirement: 共通 metadata 最小欄位
真本裡每個項目的 metadata MUST 至少包含六個欄位：id、型態、產生者、時間（建立與最後更新）、所屬案件、出處。所屬案件與出處在不適用時 MAY 為空值，其餘四個欄位 MUST 有值。寫入者提交的收件匣項目 MUST 帶有 id、型態與時間，但不帶產生者：產生者只由接受寫入的儲存要素填入（見「產生者由介面填入」）。

#### Scenario: 缺少必填欄位
- **WHEN** 任何收件匣項目缺少 id、型態或時間
- **THEN** 該儲存要素拒絕寫入，並回報缺少哪個欄位

#### Scenario: 所屬案件未知
- **WHEN** opencode 開啟一個還不確定屬於哪個案件的 Session
- **THEN** 該 Session 以空的所屬案件寫入，不被拒絕

### Requirement: id 不變
項目的 id MUST 在項目的整個生命週期內保持不變，不論項目被改寫、搬移或改名。id 由寫入者在提交之前依共通規則產生：Session 用 `<來源應用>:<來源應用自己的 Session id>`，其他項目（交接單、認領、參考 Link、改寫提案、產出登錄）用 `<型態>:<ULID>` 之類不會重複的形式。同一個 id、同一個產生者與型態的項目，視為同一個項目的更新（重新同步、重新匯入都對到同一個 id）；同一個 id 但產生者或型態不同，就是撞號，儲存要素 MUST 拒絕。其他項目 SHALL 只以 id 參照它，不以路徑或位置參照。

#### Scenario: 案件主題檔搬家
- **WHEN** MyBrain 的某個案件主題檔從 `技術/靈感/` 搬到 `技術/動手做/`
- **THEN** Agora 與 Foundry 中所屬案件指向它的項目，不需要任何修改就仍然解析得到它

### Requirement: 產生者由介面填入
metadata 的產生者 MUST 由接受寫入的儲存要素依認證結果填入，不得採用寫入者自己宣稱的值。

#### Scenario: 住民自報產生者
- **WHEN** Mac 上的 opencode 在寫入要求中把產生者填成「human:fatesaikou」
- **THEN** 儲存要素忽略該值，改以它認證後的身分（Mac opencode profile）作為產生者

### Requirement: metadata 可以擴充
各儲存要素 SHALL 允許在共通欄位之外加入自己的 metadata 欄位；讀取者 MUST 忽略自己不認得的欄位，而不是視為錯誤。

#### Scenario: 之後加入隱私 tag
- **WHEN** 之後的 LLMGateway 設計在 Agora 的 metadata 加入隱私 tag 欄位
- **THEN** 不認得這個欄位的既有讀取者照常運作
