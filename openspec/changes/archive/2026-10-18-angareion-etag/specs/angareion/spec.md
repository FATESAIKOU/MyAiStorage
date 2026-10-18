## MODIFIED Requirements

### Requirement: 確認有沒有新訊息
`angareion wait` MUST 能不阻塞（`--timeout 0`：查一次就回）、限時阻塞（`--timeout N`）或一直阻塞（不給逾時）。有新訊息時 exit 0；逾時沒有新訊息時 exit 3。等待期間 MUST NOT 改動游標。**不給 `--timeout` 的預設仍然是一直播阻塞**（MyAiStorage#96 重新評估後維持：保留 ETag 之後閒置重啟的成本接近 0，限時是呼叫方／harness 的事）。

`wait` 的後端輪詢 MUST 在 `wait` 之間記住每個實體上一次「完整查過而且沒有新訊息」的條件式查詢狀態：當時實際查詢的 URL（含 `since`）、當時的游標、回應的 ETag，存在本機狀態檔（與游標檔分開；見「目標與游標」）。重新開始 `wait` 時，只要游標與收件模式（整個 channel／寄給我的）相同就 MUST 送條件式查詢（`If-None-Match`）；記錄的 URL 只是「那次查的是哪個視窗」，MUST NOT 要求與這次的 URL 相同——304 代表後端說「現在這份表示與記錄的那份相同」，而那份已經完整掃過且對同一個游標沒有新訊息。若記錄的視窗就是游標的視窗，`wait` MUST 優先查那個視窗（不採用更窄的掃描視窗），避免掃描視窗漂移讓每次重啟都多一次完整查詢。只有 304 才算「沒有新訊息」（304 不計 GitHub 額度）。

下列情況 MUST 用完整查詢（不送 `If-None-Match`）：狀態檔不存在、不是合法 JSON、版本不認得、記錄缺欄位或與現在的 URL／游標／收件模式不符、上一次回應有下一頁、上一次回應的第 1 頁已滿（回傳筆數等於 `per_page`，可能還有下一頁）、或該實體沒有可用的 ETag。查到新訊息時 MUST 清掉該筆記錄（`wait` exit 0 之後還沒 `read` 的訊息，下一次 `wait` 仍要叫得到）。304 以外、可以記錄的回應 MUST 用新的 ETag 覆寫該筆（ETag 失效時照常查到）。狀態檔壞掉或寫不進去 MUST NOT 讓 `wait` 失敗或漏訊息——最壞情況是下一次用完整查詢；寫不進去時 MUST 在 stderr 印一次提示（同一行程最多一次），stdout 與 exit code MUST NOT 因此改變。

#### Scenario: 不阻塞
- **WHEN** 沒有新訊息時執行 `angareion wait --timeout 0`
- **THEN** 立刻 exit 3

#### Scenario: 阻塞到有新訊息
- **WHEN** B 執行 `angareion wait --timeout 600`，之後 A 送一則給 B
- **THEN** B 的 `wait` 在那則送出後不久 exit 0

#### Scenario: 不給逾時就一直等
- **WHEN** B 執行 `angareion wait`（不給 `--timeout`），沒有新訊息
- **THEN** 一直阻塞，直到 A 送了一則給 B 才 exit 0

#### Scenario: wait 不動游標
- **WHEN** B 的 `wait` 因為有新訊息而 exit 0，B 隨後執行 `angareion read`
- **THEN** `read` 讀到那則訊息（`wait` 沒有把它標成已讀）

#### Scenario: 重啟 wait 不吃額度
- **WHEN** B 的 `wait --timeout 60` 逾時後，再 `wait --timeout 60`（其間沒有任何新訊息），連續多次
- **THEN** 第一次對每個 channel 的查詢是完整請求（建立記錄）；之後每次重啟的第一次查詢都帶 `If-None-Match`、後端回 304，計額度的完整請求接近 0

#### Scenario: 掃描視窗漂移後重啟
- **WHEN** 一則亂序的訊息（較小的 id、較晚的後端時刻）把行程內的掃描視窗推到游標視窗之後，B 的 `wait` 重啟 6 次（每次 run 兩次查詢）
- **THEN** 只有第一次 run 的第一次查詢是完整請求；其餘都是 304（修前每次 run 是 2 次完整請求）

#### Scenario: ETag 失效時照常查到
- **WHEN** 記錄的查詢被後端回 200（回應變了），而且裡面有寄給 B 的新訊息
- **THEN** `wait` exit 0；該筆記錄被清掉（不留下會把同一則訊息蓋成「沒有新訊息」的 ETag）

#### Scenario: 沒讀走的新訊息不會被 304 蓋掉
- **WHEN** `wait` 曾因有新訊息 exit 0，B 還沒 `read`，B 重新 `wait`
- **THEN** 那則訊息仍然叫得到（沒有可用的記錄時用完整查詢），不會被 304 當成沒有新訊息

#### Scenario: 狀態檔壞掉或不存在
- **WHEN** 狀態檔不存在、不是 JSON、版本不認得，或記錄與現在的游標／收件模式不符
- **THEN** 用完整查詢（不送 `If-None-Match`）；新訊息不會因此漏掉；壞掉的檔案在下次寫入時被改寫成合法內容

#### Scenario: 狀態檔寫不進去
- **WHEN** 狀態目錄是唯讀的（或磁碟寫入失敗），B 執行 `angareion wait`
- **THEN** stderr 印一次提示（同一行程不重複），`wait` 照常回 exit 0／3（不是 2），訊息不漏

#### Scenario: 多頁或第 1 頁已滿時不用 ETag
- **WHEN** 回應有下一頁，或第 1 頁回傳的筆數已滿（`per_page`）
- **THEN** 該實體不留 ETag；下一次用完整查詢（第 2 頁以後的新訊息不會因為第 1 頁不變的 304 被跳過）

#### Scenario: 游標前進之後
- **WHEN** B 用 `read` 把游標往前推，之後再 `wait`
- **THEN** 記錄因游標不同而不被採用：該 channel 用完整查詢，查過沒有新訊息後才重新記住；`wait` 仍不改動游標

#### Scenario: 兩個行程同時 wait
- **WHEN** 同一個身分、同一個狀態目錄的兩個 `wait` 同時在跑
- **THEN** 狀態檔不會被寫壞（整檔原子替換）；最壞情況是少記一筆、下一次用完整查詢，不會出現壞檔被當成 304 或漏訊息

### Requirement: 目標與游標
`wait` 與 `read` 的目標是某個 channel（`--channel`）或「寄給我的」（不給 `--channel`：所有 channel 裡 `to` 是自己 agent 名、或沒有 `to` 的訊息）。判斷訊息新不新 MUST 只看後端（到達位置與後端記錄的時刻），MUST NOT 使用寄件者填的 `at` 或 `id`。`read` 不給 `--since` 時 MUST 從本機游標之後讀起並把游標往前推；給了 `--since`（後端時刻）就回傳不早於它的訊息（含邊界），MUST NOT 改動本機游標。`read` 的每則輸出 MUST 帶後端記錄的時刻 `received_at`。游標刪掉時 MUST 從頭讀起。

`read`／`wait` 給了 `--channel` 時 MUST 在 stderr 印一行警告，說明這會讀到 channel 裡所有訊息（包含寄給別人的訊息）；不給 `--channel`（「寄給我的」，含廣播）時 MUST NOT 印。警告 MUST NOT 改變 exit code 或 stdout。

`wait` 的條件式查詢狀態（見「確認有沒有新訊息」）MUST NOT 寫進游標檔；`read` MUST NOT 讀它或受它影響。它是本機快取：刪掉 MUST 只造成下一次完整查詢，MUST NOT 讓訊息漏掉。

#### Scenario: 游標刪掉
- **WHEN** 刪掉本機游標後執行 `angareion read`
- **THEN** 讀到目標裡所有的訊息

#### Scenario: 寄給我的
- **WHEN** 兩個 channel 裡各有一則 `to` 是我的訊息、一則沒有 `to` 的訊息，另外有一則 `to` 是別人的；不給 `--channel` 執行 `angareion read`
- **THEN** 讀到 `to` 是我的那兩則與沒有 `to` 的那一則，不含別人的

#### Scenario: 同一秒、後到但 ULID 較小
- **WHEN** B 讀過 `at` 是 03:00:00、ULID 較大的一則之後，另一個寄件者的 `at` 同樣是 03:00:00、ULID 較小的一則才到，B 再執行 `angareion read`
- **THEN** 讀到後到的那一則，先前讀過的那則不會重複；`angareion wait --timeout 0` 在讀之前是 exit 0

#### Scenario: 寄件者的時鐘慢
- **WHEN** B 的游標在後端時刻 03:00:10 的那一則，之後到達的一則 `at` 填的是 03:00:08（寄件者的時鐘慢）
- **THEN** B 讀到它

#### Scenario: at 的時區不同
- **WHEN** B 已讀過 `at` 為 `…T17:00:00+09:00` 的一則，之後到達的一則 `at` 為 `…T09:00:00+00:00`（實際上晚一小時、字串比較較小）
- **THEN** B 讀到它

#### Scenario: 被編輯的留言
- **WHEN** 已讀過的一則留言在後端被編輯（後端的更新時間變新）
- **THEN** 下一次 `read` 不會把它當新訊息再回傳

#### Scenario: since 含邊界
- **WHEN** 兩則訊息的後端時刻都是 03:00:00，`angareion read --since <03:00:00 的時刻>`
- **THEN** 兩則都回傳（含邊界）；本機游標沒有被改動；每則都帶 `received_at` 與 `id`，呼叫方用 `id` 去重

#### Scenario: read 輸出後端時刻
- **WHEN** `angareion read --json`
- **THEN** 每則訊息都有 `received_at`，它是後端記錄的時刻，不是寄件者的 `at`

#### Scenario: 收訊加了 --channel
- **WHEN** B 執行 `angareion read --channel general`（或 `angareion wait --channel general`）
- **THEN** stderr 有一行警告說明這會包含寄給別人的訊息；stdout 與 exit code 與不加 `--channel` 時相同

#### Scenario: 寄給我的不加 --channel
- **WHEN** B 執行 `angareion read`（或 `angareion wait`），不給 `--channel`
- **THEN** 沒有那行警告；讀到的是寄給 B 的訊息（含廣播）

#### Scenario: 刪掉游標後 wait 仍完整查詢
- **WHEN** 刪掉游標檔之後 B 執行 `angareion wait`
- **THEN** 不採用先前的 ETag 記錄（游標不同），用完整查詢；有新訊息時 exit 0
