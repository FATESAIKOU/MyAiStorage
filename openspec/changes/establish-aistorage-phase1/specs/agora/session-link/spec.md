## Purpose

定義 Session 之間的關係：接續與參考、接續點、交接單與認領、由起點建出新 Session，以及它們組成的三種工作形態：分裂（1→n）、統合（n→1）、相互參照（n↔m），並把 Session 歸到 MyBrain 的案件底下。

## ADDED Requirements

### Requirement: Session Link 的兩種類型
Session Link MUST 是從一個 Session 指向另一個 Session 的有向關係，類型為「接續」或「參考」。接續的目標 MUST 是較早的 Session；參考的目標 MAY 是任何其他 Session，包括仍在並行的 Session。一個 Session MAY 有多條 Session Link。Link 的類型集合 SHALL 可以擴充，不認得新類型的讀取者 MUST 忽略該 Link，而不是視為錯誤。

#### Scenario: 一個 Session 有多條接續 Link
- **WHEN** S4 由分別來自 S2 與 S3 的兩張交接單建出
- **THEN** S4 有兩條接續 Link，分別指向 S2 與 S3，各自記錄接續點

### Requirement: 接續經由起點建出新 Session
新 Session MUST 由 Agora 從起點建出：`agora checkout` 產出起點包，內含起點之前的原始紀錄、要交代的任務與來源 Session 的 id，再由轉接器載入成 coding agent 的原生 session。起點 MUST 可以是一張交接單，也 MAY 是任何 Session 的任何一則已提交訊息（不指定訊息就是最新已提交的那一則）。交接單 MUST 由被接續 Session 的持有者發起：同步並建立一張交接單，一起提交。交接單 MUST 是 Agora 的一個項目，帶有 id，記錄被接續的 Session、接續點與要交代的任務。新 Session 收進 Agora 時，SHALL 形成從新 Session 指向被接續 Session 的接續 Link。

認領 MUST 只適用於以交接單為起點的情況。一張交接單 MUST 只能被認領一次；尚未被認領的交接單 MUST 能經由讀取介面找到。checkout 以交接單為起點時 MUST 先登記認領，並在讀取介面確認這張交接單由自己接手之後，才產出起點包；被拒絕時 MUST NOT 產出任何東西，所以被拒的一方不會有 Session 開工。接手的 Session SHALL 能先讀任務，需要時再深入閱讀版。

#### Scenario: 分裂（切分工作）
- **WHEN** S1 同步，並為同一件工作的兩個部分各寫一張交接單、一起提交，之後依兩張交接單各跑一次 checkout，再由轉接器各自載入
- **THEN** 建出的新 Session S2、S3 各有一條指向 S1 的接續 Link；兩張交接單都能經由讀取介面讀到，S2、S3 知道對方負責什麼

#### Scenario: 統合（聚合成果）
- **WHEN** S2、S3 各自同步、各寫一張交接單交出自己的末端並提交，之後一次 checkout 以這兩張交接單為起點，由轉接器載入成 S4
- **THEN** S4 有分別指向 S2 與 S3 的兩條接續 Link，開頭就帶著兩者接續點之前的內容；S4 從頭到尾沒有要求 S2、S3 做任何事

#### Scenario: 直接從某則訊息開始
- **WHEN** 呼叫者以 `<session>@<訊息>` 為起點執行 checkout，沒有經過交接單
- **THEN** 不需要認領，checkout 直接產出起點包；載入後的新 Session 有一條指向來源 Session 的接續 Link，接續點就是那則訊息

#### Scenario: 重複認領
- **WHEN** 兩次 checkout 幾乎同時以同一張交接單為起點
- **THEN** 提交流程只接受先處理到的那一次認領，拒絕另一次並在讀取視圖發佈原因；被拒絕的那次 checkout 看到拒絕後不產出起點包，不會有 Session 從它開工

### Requirement: 建出的 Session 保留原始開頭
同一個 coding agent 之間接續時，新 Session 在起點之前的內容 MUST 與被釘住的原始紀錄快照位元組相同，MUST NOT 經過閱讀版轉換。跨 coding agent 接續或需要壓縮時 MAY 改寫開頭，但 MUST 在新 Session 的 metadata 標記「開頭已改寫」。多個起點合成一個 Session（n→1）時，最長的一段 SHALL 原樣放最前面；總長度超過目標模型的 context 上限時 MUST 明確拒絕，MUST NOT 默默截斷。

#### Scenario: 1→n 建出的開頭相同
- **WHEN** 同一個起點各跑一次 checkout 與轉接器，建出兩個 opencode Session
- **THEN** 兩個 Session 送給模型的開頭位元組相同，也與原 Session 在起點之前的內容相同，只在起點之後分岔

#### Scenario: n→1 超過 context 上限
- **WHEN** checkout 以兩張交接單為起點，兩段合起來超過目標模型的 context 上限
- **THEN** checkout 明確拒絕並說明原因，不產出起點包，也不截斷任何一段

### Requirement: Agora 不依賴特定 coding agent
Agora 的 session 操作（找、看、讀、寫交接單、產出起點包）MUST NOT 依賴任何特定的 coding agent。把起點包載入成原生 session 的工作 MUST 由各 coding agent 的轉接器負責，轉接器命名為 `agora-<名稱>`。誰開 agent、在哪開，由呼叫者決定，Agora MUST NOT 限制呼叫者是人還是 AI。

#### Scenario: 換一個 coding agent
- **WHEN** 之後要讓 Claude Code 接續 Agora 裡的 Session
- **THEN** 只需要新增轉接器 `agora-claude-code`，`agora` 的指令與起點包的格式都不變

### Requirement: 接續點
每條接續 Link MUST 記錄它的接續點，也就是交接單所記的位置。接續點 MUST 同時指明它所依據的原始紀錄快照與快照裡的一則訊息（最後一則已完成的訊息；寫交接單時仍在生成中的那一則不算）；新 Session 承接的內容 MUST 從這份被指明的快照讀取，不從被接續 Session 的最新版本讀取。接續點之後被接續 Session 新增的內容，以及來源端之後對既有訊息的編輯或刪除，都 MUST NOT 改變新 Session 承接的內容。

#### Scenario: 被接續後繼續聊
- **WHEN** S2 接續 S1 之後，你又對 S1 多講了幾句
- **THEN** S1 照常繼續，S2 的接續點不變，那幾句不屬於 S2 承接的範圍

#### Scenario: 來源端之後刪掉了訊息
- **WHEN** S2 接續 S1 之後，你在 opencode 裡對 S1 做了 /undo 再重新輸入，接續點之前的幾則訊息在來源端被刪掉
- **THEN** S2 承接的內容不變，仍然是交接單所依據的那份快照裡、接續點之前的內容

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
