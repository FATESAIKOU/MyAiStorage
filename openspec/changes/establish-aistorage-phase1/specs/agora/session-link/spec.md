## Purpose

定義 Session 之間的關係：接續與參考、接續點、交接單與認領、由起點建出新 Session，以及它們組成的四種工作形態：1→1、1→n、n→1（分裂與統合）、n↔m（相互參照），並把 Session 歸到 MyBrain 的案件底下。

## ADDED Requirements

### Requirement: Session Link 的兩種類型
Session Link MUST 是從一個 Session 指向另一個 Session 的有向關係，類型為「接續」或「參考」。接續的目標 MUST 是較早的 Session；參考的目標 MAY 是任何其他 Session，包括仍在並行的 Session。一個 Session MAY 有多條 Session Link。Link 的類型集合 SHALL 可以擴充，不認得新類型的讀取者 MUST 忽略該 Link，而不是視為錯誤。

#### Scenario: 一個 Session 有多條接續 Link
- **WHEN** S4 由分別來自 S2 與 S3 的兩張交接單建出
- **THEN** S4 有兩條接續 Link，分別指向 S2 與 S3，各自記錄接續點

### Requirement: 四種關係都留下記錄
Session 之間的關係 MUST 只有四種：1→1、1→n、n→1、n↔m。每一種關係 MUST 在 Agora 留下可經由讀取介面查到的記錄：1→1、1→n、n→1 是接續 Link（由新 Session 指向被接續的 Session，各自記著接續點），n↔m 是參考 Link。分岔、收斂、統合、相互參照 MUST 只是這四種關係的組合，MUST NOT 引入新的關係類型。

#### Scenario: 1→1 不經過交接單
- **WHEN** 呼叫者以 `agora checkout S1` 建出新 Session S2，沒有任何交接單
- **THEN** S2 有一條指向 S1 的接續 Link，接續點就是那個起點；讀取介面在 `agora show S1` 與 `agora show S2` 兩個方向都看得到它

#### Scenario: 1→n 每個新 Session 各一條
- **WHEN** 同一個起點 S1 被 checkout n 次，產出 S2…S(n+1)
- **THEN** 每個新 Session 各自有一條指向 S1 的接續 Link，S1 有 n 條入向 Link，沒有任何一方因此被鎖住

#### Scenario: n→1 一個新 Session 多條
- **WHEN** 一次 checkout 以 S2、S3 為起點建出 S4
- **THEN** S4 有分別指向 S2 與 S3 的兩條接續 Link

### Requirement: 接續經由起點建出新 Session
新 Session MUST 由 Agora 從起點建出：`agora checkout` 產出起點包，內含起點之前的原始紀錄、要交代的任務與來源 Session 的 id，再由轉接器載入成 coding agent 的原生 session。起點 MUST 可以是一張交接單，也 MAY 是任何 Session 的任何一則已提交訊息（不指定訊息就是最新已提交的那一則）。**接續 MUST NOT 以交接單為前提**：交接單只是可選的便利。交接單若存在 MUST 由被接續 Session 的持有者發起：同步並建立一張交接單，一起提交。交接單 MUST 是 Agora 的一個項目，帶有 id，記錄被接續的 Session、接續點與要交代的任務。

每一次 checkout MUST 為**每一個**起點留下一筆接續記錄，無論起點是交接單還是某個位置：起點是交接單時沿用認領（認領本身就代表接續，MUST NOT 另記一條），起點是某個位置時建立一筆接續單項目。該記錄 MUST 由 checkout 簽章後放進收件匣，MUST 由提交流程收進 Agora 後建立從新 Session 指向被接續 Session 的接續 Link，並 MUST 自己帶著新 Session 的空紀錄（預留），所以記錄被拒時 Agora 裡 MUST NOT 留下任何東西。

**同一個新 Session 對同一個被接續 Session MUST 只有一條接續 Link**（不論是認領還是接續單建立的）。同一個起點重複送 MUST 冪等；同一個新 Session 對同一個被接續 Session 的**第二個**起點（不同接續點、或同一個來源的第二張交接單）MUST 被明確拒收，且 MUST NOT 寫入第二條 Link。checkout MUST 在本機就拒絕同一批起點裡同一個被接續 Session 出現兩次的組合（交接單起點的來源是該交接單的目標 Session，與直接起點混用也算同一個來源），所以留下哪一條 Link MUST NOT 取決於提交流程的套用順序。

被接續的目標 MUST 是**主 Session**（與交接單一致）：子 Session 是母 Session 內部的一段工作，接續它 MUST 被拒收。

每個 profile 能同時掛著的**未結預留**（`status` 還是 `reserved`、而且還沒有後續快照）數量 MUST 有上限（可設定，附預設值）。任何 profile 都能為任何一份既有快照送接續，沒有上限時讀取視圖要發佈的閱讀版數量沒有邊界。上限 MUST 是**跨輪累計**的：它 MUST 直接由真本算出該 profile 目前的未結預留數量，MUST NOT 是每輪歸零的計數（輪數由住民自己觸發，每輪歸零只是限速、總量沒有上限），也 MUST NOT 依賴任何寫在真本之外的帳本。設定值 MUST 只接受正整數：0 或負數 MUST 在載入時就被拒絕，MUST NOT 當成「不設上限」。超過上限的項目 MUST 被明確拒收並在讀取視圖發佈原因，**已經是冪等重送、或不會新增預留的那一筆 MUST NOT 佔用額度**。

預留出來的新 Session 狀態 MUST 是 `reserved`（不是「運作中」）並帶一個期限。有人真的載入它（第一份真實快照）之後，它 MUST 回到一般的運作中／停止狀態且期限消失。**期 1 MUST NOT 自動刪除過期的預留**：那是改動真本，屬於管理操作；期限只是顯示與管理用的訊號。讀取介面 MUST 能把預留與已經開工的 Session 分開（`agora find --status reserved`），並在顯示時指出期限與是否已過期。

認領 MUST 只適用於以交接單為起點的情況（直接起點沒有交接單可認，也不需要認）。一張交接單 MUST 只能被認領一次；尚未被認領的交接單 MUST 能經由讀取介面找到。checkout MUST 先登記認領或接續記錄，並在讀取介面確認那條接續 Link 屬於自己之後，才產出起點包；被拒絕時 MUST NOT 產出任何東西，所以被拒的一方不會有 Session 開工。交接單起點的接手者 SHALL 能先讀任務，需要時再深入閱讀版。

#### Scenario: 分裂（切分工作）
- **WHEN** S1 同步，並為同一件工作的兩個部分各寫一張交接單、一起提交，之後依兩張交接單各跑一次 checkout，再由轉接器各自載入
- **THEN** 建出的新 Session S2、S3 各有一條指向 S1 的接續 Link；兩張交接單都能經由讀取介面讀到，S2、S3 知道對方負責什麼

#### Scenario: 統合（聚合成果）
- **WHEN** S2、S3 各自同步、各寫一張交接單交出自己的末端並提交，之後一次 checkout 以這兩張交接單為起點，由轉接器載入成 S4
- **THEN** S4 有分別指向 S2 與 S3 的兩條接續 Link，開頭就帶著兩者接續點之前的內容；S4 從頭到尾沒有要求 S2、S3 做任何事

#### Scenario: 直接從某則訊息開始
- **WHEN** 呼叫者以 `<session>@<訊息>` 為起點執行 checkout，沒有經過交接單
- **THEN** 不需要認領，checkout 直接產出起點包；載入後的新 Session 有一條指向來源 Session 的接續 Link，接續點就是那則訊息

#### Scenario: 直接起點不需要寫入端以外的準備
- **WHEN** 呼叫者只有讀取身分但沒有簽章金鑰，卻要以 `<session>[@<訊息>]` 為起點執行 checkout
- **THEN** checkout 明確拒絕並說明沒有接續記錄就不能產出起點包，因為沒有接續 Link 的新 Session 沒有人負責

#### Scenario: 重複送同一個起點
- **WHEN** 同一個新 Session 對同一個起點重複送出接續記錄（例如換了一個 item id）
- **THEN** 提交流程不寫入第二條接續 Link，重複的那筆被當成已完成；被接續的 Session 完全不受影響

#### Scenario: 同一個新 Session 接同一個來源兩次
- **WHEN** 同一個新 Session 先以直接起點接續 S1，之後又認領 S1 的另一張交接單（或反過來）
- **THEN** 第二筆被明確拒收，真本裡仍然只有一條接續 Link，留下哪一條不取決於兩筆的套用順序；被拒的認領不會把那張交接單標成已認領

#### Scenario: 一批起點裡同一個來源出現兩次
- **WHEN** 呼叫者一次 checkout 同時給 `handoff:H`（目標是 S1）與 `S1@某訊息`
- **THEN** checkout 在登記任何接續記錄之前就明確拒絕，說明同一個來源只能接一次，而且不產出起點包

#### Scenario: 接續的目標是子 Session
- **WHEN** 呼叫者以一個子 Session 的某個位置為起點執行 checkout
- **THEN** 提交流程明確拒收（與交接單只能由主 Session 接一致），且不留下那筆預留

#### Scenario: 超過未結預留的數量上限
- **WHEN** 某個 profile 目前的未結預留（`status=reserved` 且沒有後續快照）數量已達上限，又要再預留一個
- **THEN** 下一筆被明確拒收並發佈原因，不留下預留；被拒的、冪等重送的、或不會新增預留的那一筆不佔用額度

#### Scenario: 上限跨輪累計
- **WHEN** 某個 profile 這一輪把未結預留用滿，下一輪再送一個新的預留
- **THEN** 下一輪那筆一樣被拒收，因為上限數的是真本裡現存的未結預留、不是這一輪的筆數

#### Scenario: 預留開工之後就不再佔額度
- **WHEN** 某個 profile 的一筆預留後來有了第一份真實快照（狀態離開 `reserved`）
- **THEN** 它不再算未結預留，這個 profile 可以再預留一個

#### Scenario: 上限設定不是正整數
- **WHEN** 設定檔把未結預留上限設成 0 或負數
- **THEN** 提交流程在載入設定時就明確報錯，不把它當成「不設上限」默默跑

#### Scenario: 預留一直沒有人開工
- **WHEN** `agora checkout` 預留了新 Session，但之後一直沒有它的真實快照
- **THEN** 讀取介面把它顯示為預留（附期限），與已經開工的 Session 分開；期限過了只標示為已過期，提交流程不會自動刪除它

#### Scenario: 重複認領
- **WHEN** 兩次 checkout 幾乎同時以同一張交接單為起點
- **THEN** 提交流程只接受先處理到的那一次認領，拒絕另一次並在讀取視圖發佈原因；被拒絕的那次 checkout 看到拒絕後不產出起點包，不會有 Session 從它開工

### Requirement: 建出的 Session 保留原始開頭
同一個 coding agent 之間接續時，新 Session 在起點之前的內容 MUST 與被釘住的原始紀錄快照位元組相同，MUST NOT 經過閱讀版轉換。跨 coding agent 接續或需要壓縮時 MAY 改寫開頭，但 MUST 在新 Session 的 metadata 標記「開頭已改寫」。多個起點合成一個 Session（n→1）時，最長的一段 SHALL 原樣放最前面；總長度超過目標模型的 context 上限時 MUST 明確拒絕，MUST NOT 默默截斷。

轉接器 MUST 沿用起點包預留的新 Session id。呼叫端要求換一個 id 時，MUST 明確拒絕並說明：起點包預留的那個 id 是提交流程認得出這筆預留與接續 Link 的唯一線索，換 id 匯入會留下一筆沒有人負責的空 Session。要換 id MUST 在 checkout 階段就指定，讓它預留呼叫端要的那一個。

#### Scenario: 1→n 建出的開頭相同
- **WHEN** 同一個起點各跑一次 checkout 與轉接器，建出兩個 opencode Session
- **THEN** 兩個 Session 送給模型的開頭位元組相同，也與原 Session 在起續點之前的內容相同，只在接續點之後分岔

#### Scenario: n→1 超過 context 上限
- **WHEN** checkout 以兩張交接單為起點，兩段合起來超過目標模型的 context 上限
- **THEN** checkout 明確拒絕並說明原因，不產出起點包，也不截斷任何一段

#### Scenario: 轉接器被要求換 id
- **WHEN** 呼叫端對起點包傳入一個與預留不同的 `--session-id`
- **THEN** 轉接器明確拒絕、不匯入任何東西，並說明預留的 id 是哪一個

### Requirement: Agora 不依賴特定 coding agent
Agora 的 session 操作（找、看、讀、寫交接單、產出起點包）MUST NOT 依賴任何特定的 coding agent。把起點包載入成原生 session 的工作 MUST 由各 coding agent 的轉接器負責，轉接器命名為 `agora-<名稱>`。誰開 agent、在哪開，由呼叫者決定，Agora MUST NOT 限制呼叫者是人還是 AI。

#### Scenario: 換一個 coding agent
- **WHEN** 之後要讓 Claude Code 接續 Agora 裡的 Session
- **THEN** 只需要新增轉接器 `agora-claude-code`，`agora` 的指令與起點包的格式都不變

### Requirement: 接續點
每條接續 Link MUST 記錄它的接續點，也就是接續所依據的位置。接續點 MUST 同時指明它所依據的原始紀錄快照與快照裡的一則訊息；那份快照因此被釘住，MUST 被發佈到讀取視圖。新 Session 承接的內容 MUST 從這份被指明的快照讀取，不從被接續 Session 的最新版本讀取。接續點之後被接續 Session 新增的內容，以及來源端之後對既有訊息的編輯或刪除，都 MUST NOT 改變新 Session 承接的內容。

接續點指到的訊息 MUST 在該快照裡存在、已完成且未被撤銷。**兩種起點的驗證規則不同**：交接單的接續點 MUST 是該快照**最後一則已完成**的訊息（交接單是交出手上做到哪）；直接起點 MAY 是該快照裡的**任何**一則已完成、未撤銷的訊息（呼叫端要從某個位置繼續）。

#### Scenario: 被接續後繼續聊
- **WHEN** S2 接續 S1 之後，你又對 S1 多講了幾句
- **THEN** S1 照常繼續，S2 的接續點不變，那幾句不屬於 S2 承接的範圍

#### Scenario: 來源端之後刪掉了訊息
- **WHEN** S2 接續 S1 之後，你在 opencode 裡對 S1 做了 /undo 再重新輸入，接續點之前的幾則訊息在來源端被刪掉
- **THEN** S2 承接的內容不變，仍然是被指明的那份快照裡、接續點之前的內容

#### Scenario: 從中間某一則開始接續
- **WHEN** 呼叫者以 `<session>@<訊息>` 指定該快照裡倒數第二則已完成訊息為接續點
- **THEN** 該接續被接受並記下那個接續點；讀取介面看得到那條 Link 與它的接續點，而且那份快照有被發佈

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
