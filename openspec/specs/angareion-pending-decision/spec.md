# angareion-pending-decision Specification

## Purpose
待決（pending decision）：提問者出題、作答者回答、提問者更新或撤回，全部都是在頻道裡 append 的一則 `datatype: decision` 訊息；Angareion 把整串紀錄匯總成每一題目前的狀態。匯總規則只有一份，放在 Angareion；App 與 PMO 只讀 `angareion decision list --json`。後端只用「append 一則」與「依序讀回」兩種能力（設計見 `openspec/changes/archive/2026-11-15-angareion-pending-decision/design.md`，C-編號對照見 §15）。

## Requirements

### Requirement: 待決紀錄的格式
待決紀錄 MUST 是 front matter `datatype: decision` 的訊息，內文是一個 YAML mapping，前兩個鍵固定是 `decision: 1`（payload 版本）與 `op`。`op` MUST 是 `ask`、`update`、`withdraw`、`submit`、`snapshot` 之一。寫入時 MUST 嚴格檢查：任何欄位不合就 exit 1、什麼都不送。ask 與 update 的題目欄位（`answerer`、`title`、`background`、`body`、`options`、`recommended`、`reason`）MUST 齊備，`options` 是 2～9 個 `{key, text}`，`key` 是單一大寫字母且不重複，`recommended` MUST 是其中一個 `key`；`title` 單行 1～120 字；`body` 不得為空。整則序列化文字（含 front matter）MUST NOT 超過 65536 字元（以字元計）。

#### Scenario: ask 的欄位違規
- **WHEN** `angareion decision ask --channel X --body` 的 options 只有 1 個、或 `recommended` 不在 options 裡、或 title 多行、或 body 是空的、或 answerer 不是 AI 名
- **THEN** exit 1，什麼都沒送（C-7）

#### Scenario: ask 的 body 含綁定或保留的鍵
- **WHEN** ask 的 body 含 `ask_channel`、`asker`、`op`、`decision`、`ref` 或其他未知的鍵
- **THEN** exit 1，什麼都沒送（C-6）

#### Scenario: 長度以字元計
- **WHEN** 整則序列化文字恰好 65536 字元（中文內文）
- **THEN** 送出成功；65537 字元則 exit 1（C-7）

#### Scenario: 讀取寬鬆
- **WHEN** 讀到內文不是 YAML、`decision` 不是整數 1、或 `op` 不認得的紀錄
- **THEN** 只有那則紀錄被略過並列在 `ignored`（`bad_payload` 或 `unsupported_version`），匯總照常完成，exit 0（C-15）

#### Scenario: 字串無法以 UTF-8 編碼
- **WHEN** 讀到的紀錄裡任何字串（包含未知鍵的值、巢狀的選項文字，或 submit 的 `choice`）含無法以 UTF-8 編碼的字元（孤立代理字元，例如 `json.dumps` 預設寫法產生的 `\ud800`，或 emoji 的代理字元對）
- **THEN** 整則紀錄判 `bad_payload` 並列在 `ignored`，即使那個字串只出現在未知鍵；題目不進視圖，匯總照常完成，exit 0
- **AND** 寫入端（`decision` 的每一個寫入 action，含 `withdraw` 的 `--reason`）對同樣的字串 exit 1、什麼都不送，不會寫出讀者必然略過的紀錄

### Requirement: 匯總與權限
匯總 MUST 是純函式：同一組依到達位置排好的紀錄 MUST 得到同一個結果，`at` 與 ULID 的順序 MUST NOT 影響結果（C-8）。每一題以題目 id（出題那則的 `id`）識別，歸屬於（頻道, 提問者）。只有出題者能更新或撤回（`not_owner`）；只有題目目前的作答者能送出答案（`not_answerer`）；`base` MUST 等於題目目前的版本（`stale_base`）；已結束的題目（answered 或 withdrawn）的任何引用都是 `closed_ref`。不符的紀錄 MUST 留在後端（append-only），匯總時略過並列在 `ignored`。這只防誤用，不防冒名。

#### Scenario: 只有出題者能改
- **WHEN** 作答者（或別人）對一題送出 update 或 withdraw
- **THEN** CLI 的預檢 exit 1、什麼都不送；若是直接寫入的紀錄，匯總判 `not_owner`，題目不變（C-9）

#### Scenario: 版本與作答者的競態
- **WHEN** 題目被 update 把作答者從 H 換成 H2 之後，H 以舊版本送出答案
- **THEN** 那一項判 `not_answerer`，題目仍 open（先查目前的作答者，C-10）

#### Scenario: 換成 H2 之後，H2 以舊版本送出
- **WHEN** 題目被 update 把作答者從 H 換成 H2 之後，H2 以 update 之前的版本（舊的 `base`）送出答案
- **THEN** 那一項判 `stale_base`，題目仍 open（C-10）

#### Scenario: 同一題只接受一個答案
- **WHEN** 同一題有兩則合法的 submit，或 withdraw 與 submit 先後到達
- **THEN** 只有先到的被接受，後到的判 `closed_ref`；重複送出不產生第二個答案（C-11）

#### Scenario: 同一則裡同一題兩次
- **WHEN** `angareion decision submit` 的 items 裡同一個 `ref` 出現兩次
- **THEN** exit 1，什麼都不送；直接寫入的紀錄則第一項照常判，第二項判 `duplicate_ref`（C-11）

### Requirement: 指令 `angareion decision`
`angareion decision <action>` MUST 有六個 action：`ask`、`update`、`withdraw`、`submit`、`list`、`snapshot`。stdout 只印結果（新紀錄的 id，或 `list` 的結果），stderr 只印說明與警告。`decision` 的寫入與讀取 MUST NOT 讀取或推進游標，也 MUST NOT 讀寫 `wait` 的狀態檔（C-18）。`decision` 的用法與輸出 MUST NOT 出現 GitHub repo、issue 或 comment 的概念（C-22）。

#### Scenario: 六個 action
- **WHEN** `angareion decision --help`
- **THEN** 子指令恰好是 `ask`、`update`、`withdraw`、`submit`、`list`、`snapshot`（C-22）

#### Scenario: 寫入只 append
- **WHEN** 任何一個寫入的 action 成功
- **THEN** 頻道裡只多了 N 則新留言（`submit` 是分塊後的則數，`snapshot` 是 0 或 1，其他是 1；關掉自動 snapshot 時，自動 snapshot 另見 C-19 的 +1）；既有的留言內容與數目都不變（C-4）

#### Scenario: 寫入經序列化
- **WHEN** 寫入的一則在後端會被 `serialize_message` 的讀回比對拒絕
- **THEN** exit 1，什麼都不送（C-4 的經序列化一案）

#### Scenario: 只用後端的送與列舉
- **WHEN** 執行 `decision` 的任一 action
- **THEN** 後端被呼叫的方法只有 `send` 與經由 log 快取同步的列舉（C-20(e)）

### Requirement: 列出待決（list）
`list` 預設只列 open 的題目；`--all` 列出本機鏡像裡頻道的全部歷史（open、answered、withdrawn），MUST 從頭完整匯總，MUST NOT 使用 snapshot。`--json` 的輸出 MUST 是 `{"decisions": [...], "ignored": [...]}`，欄位與設計（`openspec/changes/archive/2026-11-15-angareion-pending-decision/design.md`）§5 相同，沒有 `draft` 欄位。排序 MUST 是 `(asked_at, channel, id)`。`--channel` 指定未登記的頻道 MUST exit 1；同步失敗 MUST exit 2、stdout 是空的。

#### Scenario: 預設與 --all
- **WHEN** 頻道有一題 answered、一題 withdrawn、一題 open
- **THEN** 預設 `list` 只有那題 open；`list --all` 三題都有，狀態分別是 answered、withdrawn、open（C-23）

#### Scenario: snapshot 不影響 --all
- **WHEN** 一則有效 snapshot 的 items 被竄改成假的 open 題目
- **THEN** 預設 `list` 受影響，`list --all` 不受影響（C-23）

#### Scenario: 同步失敗
- **WHEN** 列舉失敗
- **THEN** exit 2，stdout 是空的（C-23）

### Requirement: 提交答案（submit）
答案 MUST 只在使用者按下送出時寫出。`submit` 的 items 每一項 MUST 有 `ref` 與 `base`（`base` 必給，CLI 不替作答者填目前版本）；`choice` 與 `note` 至少要有一個；`choice` MUST 是 `base` 那一版 `options` 裡的 `key`；`note` 是 1～4000 字元。預檢不過的項 MUST 一次全部列在 stderr，exit 1，什麼都不送（C-12）。`submit` 的規劃 MUST 用完整匯總，不用 snapshot。

- `title`、`asker` 與頻道 MUST 由匯總補上；body 裡只能有 `items`，其他的鍵 MUST exit 1，每一項只能有 `ref`、`base`、`choice`、`note`。
- 同一個 `ref` 出現兩次 MUST 判 `duplicate_ref`，先驗這條，再做重跑略過。
- 已經被同一個作答者以相同的 `base`、`choice`、`note` 送出並接受的項 MUST 略過（stderr 印「已送出，略過」），不算錯；任何一個值不同就不略過，照其餘的前置條件判（C-29）。

#### Scenario: 預檢失敗
- **WHEN** 三項中有兩項的 choice 不在 options 裡、或 note 是空字串
- **THEN** stderr 恰好列出那兩項的 `ref` 與 `bad_item`，exit 1，什麼都不送（C-12）

#### Scenario: 答案的邊界
- **WHEN** 只給 note（4000 字元）、或 note 4001 字元
- **THEN** 4000 字元成功；4001 字元 exit 1（C-12）

#### Scenario: 重跑略過只看四個值
- **WHEN** 已接受的答案是 `choice: A` 沒有 note，重跑的輸入是 `choice: A, note: null`
- **THEN** 略過（缺省與 null 相等）；輸入的 `note: y` 則 `closed_ref`（C-29）

#### Scenario: 作答後被換版本
- **WHEN** 題目從 V1 被更新成 U2，H 以 U2 送出 A 被接受，之後以 V1 再送 A
- **THEN** 不略過，`closed_ref`，exit 1，什麼都不送（C-29）

#### Scenario: 同一則裡重複且已送出
- **WHEN** 輸入裡同一題出現兩次，而且答案已經送出
- **THEN** exit 1，`duplicate_ref`，什麼都不送，不印「已送出，略過」（C-29）

### Requirement: 分組、分塊與部分失敗
`submit` 的已答項 MUST 依（頻道, 提問者）分組，每組 MUST 至少一則；每一則的 `to` 與 `mentions` 都是提問者，`channel` 是提問頻道。一則超過 65536 字元時 MUST 依 (`asked_at`, `ref`) 順序分塊，每一塊各自完整。整批 MUST 在開始送之前全部組好並通過讀回檢查；任何一則組不出來 exit 1，什麼都不送。送的順序是組依 (`channel`, `asker`)，組內依分塊的順序。第 k 則送出失敗 MUST 立刻停，不送後面的，stdout 是前 k−1 則的 id，exit 2。原樣重跑 MUST 只送剩下的（C-13、C-14、C-25）。

#### Scenario: 兩個頻道
- **WHEN** 已答的題在兩個頻道
- **THEN** 每個頻道各一則，stdout 兩行 id，順序是頻道名由小到大（C-13）

#### Scenario: 超長分塊
- **WHEN** 同一組 100 題，每題 note 1000 字元
- **THEN** 寫出至少兩則，每則都 ≤ 65536 字元，所有則的 items 合起來恰好是那 100 題；`list --all` 100 題都 answered（C-25）

#### Scenario: 中途失敗後重跑
- **WHEN** 第 2 則送出失敗（exit 2）
- **THEN** stdout 只有第 1 則的 id；原樣重跑時第 1 則的題被略過，只送剩下的（C-14、C-25）

#### Scenario: 已送出但回應失敗
- **WHEN** 第 2 則已經進到後端、但回應失敗
- **THEN** 重跑時若同步看得到它，那幾題被略過；若同步看不到，重送的那一則的項全部判 `closed_ref`，每題只有一個答案（C-14）

### Requirement: snapshot
每個（頻道, 提問者）各自有 snapshot，只有該提問者能寫。出題者的 ask、update、withdraw 寫入成功之後，若該提問者在有效 snapshot 的 `through` 之後的相關紀錄已達 `SNAPSHOT_EVERY`（200），MUST 再寫一則 snapshot；自動寫失敗只在 stderr 警告，結束碼與 stdout 不變。手動的 `angareion decision snapshot --channel <ch>` 不看門檻；沒有可涵蓋的（後端時刻不足以判斷穩定）MUST exit 0、stdout 空；寫不出來（超過長度）MUST exit 1。snapshot 只是加速：壞掉或過期的 snapshot MUST 被略過並退回前一則或從頭匯總，結果不因此改變（C-16、C-17、C-19）。

#### Scenario: 自動寫 snapshot
- **WHEN** `SNAPSHOT_EVERY` 是 3，出題者連寫三則相關紀錄之後，穩定窗外又有一則一般訊息，然後再 ask 一題
- **THEN** 多寫一則 snapshot，`through` 是第三則 ask；stdout 只有那題的 id（C-19）

#### Scenario: 自動寫失敗
- **WHEN** 自動寫的 snapshot 寫不出來
- **THEN** stderr 警告一次，ask 仍 exit 0，stdout 只有 ask 的 id（C-19）

#### Scenario: 手動寫沒有可涵蓋的
- **WHEN** 頻道裡的紀錄都還在穩定窗內
- **THEN** exit 0，stdout 空（C-19）

### Requirement: 遞送與叫醒
`decision` 的紀錄都有 `to`，不是廣播，只遞送給 `to`、`mentions` 指到的人；寫的人自己 MUST NOT 被遞送到（C-3）。`decision` 寫入 MUST NOT 叫醒（poke）任何人（Q-4）；但已經阻塞在 `wait` 的讀者，符合它的目標的紀錄會讓它醒來。`read`、`wait` 的游標規則與位址規則不變。

#### Scenario: 自己寫的待決不遞送給自己
- **WHEN** 提問者寫的 snapshot 在頻道裡
- **THEN** 提問者的「寄給我的」與「@ 我」讀不到它（C-3）

#### Scenario: 寄給作答者的題目
- **WHEN** 出題者寫了一題給作答者
- **THEN** 作答者的「寄給我的」讀得到它；其他人讀不到（C-3）
