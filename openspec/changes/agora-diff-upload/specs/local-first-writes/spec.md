## MODIFIED Requirements

### Requirement: 只有自己驗過的版本離開 outbox

一筆 outbox 項目 MUST 只在「outbox 裡**現在**的 `session.md`，md5 等於 Drive 上的、也等於這一輪上傳的那一個」時才被移除。**分段儲存的版本，還要「`session.md` 列的每一個段都在 Drive 上、md5 與清單相符」**。**更新既有 id 的版本，還要「`sessions/<ULID>` 的資料夾 ID 與送出前相同」**——複製期間被別台刪掉、又被我們自己的 `copy` 建回來的不算成功（要 purge 加另存，見下）。上傳期間被新版本取代的，MUST 留在 outbox，等下一輪再傳。這是**確認當下**的判斷；一個已經確認、離開 outbox 的版本之後因為別台的清理變成不完整，是允許的（`session-sync`「raw 分段儲存」），不算這一條被違反。

上傳一筆**更新既有 id** 的項目之前，MUST 用這一輪列檔的結果確認那個 id 還在 Drive 上（**而且那個資料夾裡有 `session.md`**；只有孤兒段的資料夾不算還在）。「更新既有 id」在前景寫入的當下決定：continue 寫回、edit、import 的原地更新、中斷接續的收尾寫回是；新建的 import、merge、fork、另存的 Session 不是。

**寫回一個還沒被 Drive 確認過的 Session 不算更新既有 id**：同一個 id 在 outbox 裡還有一筆新建的項目（沒有標成更新、也沒有上傳程序正在比對它）時，這次寫入 MUST 維持「新建」，上傳時 MUST NOT 因為 Drive 上沒有它而另存。

不在 Drive 上的話（別台刪掉了），MUST NOT 把它傳回去，而是**另存成一個新的 Session**：新的 id，標頭的 parents 指向原本的 id。relation：continue 寫回的另存成 `continue`（和 `session-sync` 的「接續到一半被別台刪掉的」相同）；edit 與其他寫回的沿用原本的 relation（`edit` 不是 relation 的值）。原本的 id 維持被刪的狀態。這和 `session-sync`「接續到一半被別台刪掉的」是同一個規則（使用者 10-03 決定）。

**「被別台刪掉」只看得到兩種證據**：送出前那個 ULID 不在完整的 listing 裡（`store.py:812`、`:848`；**那個 ULID 的資料夾裡沒有 `session.md` 也算不在**——purge 後第一個大段 copy 中途失敗只留下孤兒段時，下一輪 MUST 當成不在了），或**送出前後的資料夾 ID 不同**（`sessions/<ULID>` 在送出前的 listing 是 `I1`、確認時是 `I2`，`I1 ≠ I2`——那代表資料夾在複製期間被 purge，又被我們自己的 `copy` 建回來）。資料夾 ID 這一條依賴 Drive 對同名重建的資料夾給新 ID（**假設**，K17 的探針驗；探針 MUST 在 R1b 之前通過，L14，因為 R1b 會把單檔路徑的名字差異偵測換掉；不成立時只剩前一條證據）。段名或 `raw-*` 的名字變動（tail 換名、`gen+1` 換整組、清舊世代）MUST NOT 被當成刪除：另一台每一輪的正常清理都會讓舊名字消失，那和「資料夾被 purge」在 listing 上長得一樣；資料夾 ID 不因換名而變，所以只有它算證據。**只看「送出前那個 ULID 已經不在」是不夠的**：那兩次檢查都在複製之前（`store.py:812`、`:848`），而分段項目的複製要好幾分鐘，正好是刪除最容易發生的窗口。分段的新 id 另存時（Y），Y 是本機的一份單檔、沒有基準段。

#### Scenario: 別台在上傳前刪掉了
- **WHEN** X 的新版本在 outbox，背景上傳之前另一台機器刪掉了 X
- **THEN** X 沒有被傳回 Drive；這次的修改另存成新的 Session Y（parents 指向 X），提醒說明 X 已被別台刪除、修改存成了 Y

#### Scenario: 還沒上傳就寫回
- **WHEN** 離線時 import X（X 還在 outbox），接著 continue 或 edit X，之後恢復連線
- **THEN** Drive 上出現的是 X 本身（最新的內容），沒有另存，也沒有「X 已被別台刪除」的提醒

#### Scenario: 分段版本少一個段
- **WHEN** 分段版本上傳後，Drive 的 `session.md` md5 對了，但清單裡有一段不在
- **THEN** 這一筆留在 outbox；不當成成功；下一輪補送或整份重來

#### Scenario: 別台的正常清理不算刪除
- **WHEN** 分段 Session X 在 outbox，另一台在同一段時間換掉自己的 tail、或 `gen+1` 讓整組段名消失
- **THEN** 本機的 `upload_batch` MUST NOT `purge` X 的資料夾、MUST NOT 另存（送出前後的資料夾 ID 相同）；X 照常上傳（缺的段由整份 raw 補回）

#### Scenario: 分段版被真的刪掉了（送出前）
- **WHEN** outbox 裡有一個分段版本的 X（entry 內有整份 raw），送出前的 listing 裡沒有 `sessions/X`（那個 ULID 不在）
- **THEN** 另存成單檔的 Y（parents 指向 X）；Y 的段全是新的，沒有基準段可補

#### Scenario: 分段版被真的刪掉了（複製期間）
- **WHEN** 分段版本 X 正在送（基準段已在 Drive），送出前 listing 的 `sessions/X` 資料夾 ID 是 `I1`；段與 `session.md` 送完、確認的 listing 裡同一個路徑的資料夾 ID 是 `I2`，而 `I1 ≠ I2`（另一台在複製期間 purge 了 X，我們的 `copy` 把資料夾建回來）
- **THEN** MUST 把剛建回來的那個資料夾 `purge` 掉、另存成單檔的 Y（parents 指向 X）；Drive 上沒有 X，**MUST NOT** 因為「確認時 `session.md` 與段都在」就當成成功——那會把別台的刪除悄悄撤銷

#### Scenario: 只剩孤兒段（複製中途失敗）
- **WHEN** 另一台 purge 了 X，本機正在送 X 的第一個大段 copy，中途失敗，`sessions/X` 只剩幾個孤兒段（沒有 `session.md`）
- **THEN** 下一輪的存在檢查 MUST 當成「X 不在了」→ 另存成單檔的 Y、X 維持被刪；MUST NOT 把 X 補完而復活它

### Requirement: 一批只連固定幾次 Drive

背景上傳一批 Session 時，不論幾個，MUST 只用固定幾次 rclone：

1. 一次上傳這一輪要送的原始檔或分段：沒有分段的 Session 送整份原始檔；分段的只送 Drive 還沒有的段（這一版新產生的、或從本機整份原始檔補回的基準段），並在送出前確認清單裡的段都在 Drive 上，缺的補回；送出前 MUST 把這些檔案的 mtime 設成現在（寬限期看的是上傳時間，不是切出時間）；
2. 原始檔那一次成功之後，一次上傳全部的 `session.md`。原始檔那一次沒有成功時，這一輪 MUST NOT 傳任何 `session.md`，整批留到下一輪；
3. 一次列檔，驗 md5；分段的一併驗清單裡的每一個段；確認用的 listing 也要帶「這個 ULID 的資料夾 ID」，與送出前相同才算沒被刪（「被別台刪掉」的兩種證據）；**送出前取 ID 與確認用的 listing 若都沒有目錄 ID，MUST 各補一次 `lsjson gdrive:sessions --dirs-only`；送出前補不到 MUST NOT 送那一筆更新；確認時補不到 MUST NOT 當成成功（留在 outbox）**；
4. 有被取代的舊原始檔、或「不在目前與前一版清單、且超過寬限期」的舊段時，再一次刪除（`session-sync`「舊段只刪被取代與過期的」）。單檔版本的確認也 MUST 跑同一條舊段清理。

這一批裡有更新既有 id 的項目時，前面 MAY 再多一次列檔（確認那些 id 還在 Drive 上，並取得它們的資料夾 ID；資料夾 ID 併進那幾次列檔，不算新的呼叫；只有 listing 沒帶目錄項時，送出前與確認各多一次 `--dirs-only`——更新路徑的 rclone 次數上限因此從 7 變 9，只有一邊缺是 8）。先原始檔、後 `session.md` 的順序 MUST 保留，因為讀的一方靠 `session.md` 判斷一個版本寫完了。md5 不符的那幾個留在 outbox，其他的照常完成。

#### Scenario: 匯入三個
- **WHEN** 匯入三個 Session，背景上傳完成
- **THEN** 背景上傳用了不超過 4 次 rclone

#### Scenario: 其中一個沒傳好
- **WHEN** 一批三個裡，有一個的 `session.md` 在 Drive 上的 md5 不符
- **THEN** 那一個留在 outbox，另外兩個離開 outbox

#### Scenario: 原始檔那一次失敗
- **WHEN** 上傳原始檔的那一次 rclone 中途失敗
- **THEN** 這一輪沒有任何 `session.md` 被傳上去

#### Scenario: 分段的一批
- **WHEN** 一批裡有一個 814 MB 的分段 Session 只多了 1 MB，和兩個新的小 Session
- **THEN** 這一輪只送那 1 MB 的新段＋tail、兩個小 Session 的整份原始檔，再一次送三個 `session.md`；已封存的 814 MB 沒有重送
