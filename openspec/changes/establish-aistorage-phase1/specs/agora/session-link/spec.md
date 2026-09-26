## Purpose

定義 Session 之間的關係：接續與參考、接續點、交接單與認領，以及它們組成的三種工作形態：分裂（1→n）、統合（n→1）、相互參照（n↔m），並把 Session 歸到 MyBrain 的案件底下。

## ADDED Requirements

### Requirement: Session Link 的兩種類型
Session Link MUST 是從一個 Session 指向另一個 Session 的有向關係，類型為「接續」或「參考」。接續的目標 MUST 是較早的 Session；參考的目標 MAY 是任何其他 Session，包括仍在並行的 Session。一個 Session MAY 有多條 Session Link。Link 的類型集合 SHALL 可以擴充，不認得新類型的讀取者 MUST 忽略該 Link，而不是視為錯誤。

#### Scenario: 一個 Session 有多條接續 Link
- **WHEN** S4 認領了分別來自 S2 與 S3 的兩張交接單
- **THEN** S4 有兩條接續 Link，分別指向 S2 與 S3，各自記錄接續點

### Requirement: 接續經由交接單與認領建立
接續 MUST 由被接續 Session 的持有者發起：同步並建立一張交接單，一起提交。交接單 MUST 是 Agora 的一個項目，帶有 id，記錄被接續的 Session、接續點與交接內容。新 Session 認領交接單時，才 SHALL 形成從新 Session 指向被接續 Session 的接續 Link。一張交接單 MUST 只能被認領一次；尚未被認領的交接單 MUST 能經由讀取介面找到。認領者 MUST 在讀取介面確認這條接續 Link 屬於自己之後才開始承接工作；看到拒絕就停下。接手者 SHALL 能先讀交接單，需要時再深入閱讀版。

#### Scenario: 分裂（切分工作）
- **WHEN** S1 同步，並為同一件工作的兩個部分各寫一張交接單、一起提交，兩個新 Session 各自認領其中一張
- **THEN** 新 Session S2、S3 各有一條指向 S1 的接續 Link；兩張交接單都能經由讀取介面讀到，S2、S3 知道對方負責什麼

#### Scenario: 統合（聚合成果）
- **WHEN** S2、S3 各自同步、各寫一張交接單交出自己的末端並提交，新 Session S4 認領這兩張
- **THEN** S4 有分別指向 S2 與 S3 的兩條接續 Link，讀得到兩者接續點之前的內容；S4 從頭到尾沒有要求 S2、S3 做任何事

#### Scenario: 重複認領
- **WHEN** 兩個新 Session 幾乎同時認領同一張交接單
- **THEN** 提交流程只接受先處理到的那一個，拒絕另一個並在讀取視圖發佈原因；被拒絕的一方在讀取介面看到拒絕、還沒開始承接工作就停下

### Requirement: 接續點
每條接續 Link MUST 記錄它的接續點，也就是交接單所記的位置。接續點之後被接續 Session 新增的內容，MUST NOT 被視為新 Session 承接的內容。

#### Scenario: 被接續後繼續聊
- **WHEN** S2 接續 S1 之後，你又對 S1 多講了幾句
- **THEN** S1 照常繼續，S2 的接續點不變，那幾句不屬於 S2 承接的範圍

### Requirement: 接續不影響被接續的 Session
接續 MUST NOT 改變被接續 Session 的狀態、內容或它在來源應用中的可用性。

#### Scenario: 分裂之後各自前進
- **WHEN** S1 被 S2、S3 兩個 Session 分別接續
- **THEN** S1、S2、S3 各自前進，任何一個都不因為另外兩個而被鎖住

### Requirement: 參考 Link
參考 Link MUST 記錄一個 Session 讀了哪個 Session，以及讀到的快照時間。同一對 Session 之間的參考 Link MUST 只保留一條，再次參考時更新為最新讀到的快照時間（歷史留在版本紀錄裡）。參考 MUST NOT 喚醒任何 AI，也 MUST NOT 承接工作。

#### Scenario: 查另一個 Session 的決定
- **WHEN** opencode 為了知道 Agora 格式的決定而讀了另一個 Session
- **THEN** 它的 Session 多一條參考 Link，記下讀到的快照時間；被參考的 Session 沒有任何變化

#### Scenario: 相互參照
- **WHEN** 並行中的 S2 與 S3 為了互相支持推進，反覆讀對方的最新快照
- **THEN** S2 有一條指向 S3 的參考 Link、S3 有一條指向 S2 的參考 Link，各自記著最新讀到的快照時間，不因反覆讀取而增加；兩者都不被對方鎖住或改變

### Requirement: 所屬案件
Session 的所屬案件 MUST 以 MyBrain 案件的 id 表示，MAY 為空值。Agora MUST NOT 另外維護案件清單。

#### Scenario: 依案件列出 Session
- **WHEN** 你想看某個案件底下的所有 Session，包括分裂與統合出來的
- **THEN** Agora 依所屬案件 id 列出它們；案件的名稱與狀態由讀者自己向 MyBrain 解析
