# local-first-writes Specification

## Purpose
寫入先在本機存完整的一份，再交給背景上傳；delete 先在本機消失，再由背景移到 Drive 垃圾桶。一批動作只連固定幾次 Drive，使用者不必站在那裡等，被別台刪掉的 Session 也救得回來。
## Requirements
### Requirement: 本機保留完整的一份
import、continue、merge、edit 寫出的每個 Session（包含中斷的接續在下一個指令收尾時寫出的），MUST 在指令結束前把 `session.md` 與標頭 `agora.raw.file` 指到的原始檔都放進本機鏡像。上傳成功之後，鏡像裡的原始檔 MUST 留著；同一個 Session 被取代的舊原始檔 MUST 從鏡像清掉。標頭沒有原始檔的 Session 照它實際有的檔案存。

#### Scenario: 救回在這台寫的
- **WHEN** 在這台 import 一個 Session X，背景上傳完成之後別台機器刪掉了 X，這台同步後執行 `agora push session X --not-exist-upload`
- **THEN** X 回到 Drive，不會因為「本機沒有原始檔」而拒絕

#### Scenario: 接續之後只留新的原始檔
- **WHEN** 對 X 執行 continue 並且有新的對話
- **THEN** X 在本機鏡像裡只有一個原始檔，就是新標頭指到的那一個

### Requirement: 背景上傳
import、continue、merge、edit MUST 在把 Session 存進本機（鏡像與 outbox）之後就結束，不等上傳；交給背景的算成功（exit 0），結果說明「已經存在本機，背景上傳中」。只有背景程序啟動失敗時才回 3（已存進 outbox，之後的指令會再送）。

上傳 MUST 由一個脫離終端機的背景程序做：
- 它不接使用者的鍵盤輸入，也不寫到呼叫它的終端機或 pipe，輸出寫到狀態目錄裡的記錄檔；
- 它沿用呼叫端的環境變數，所以測試用的資料夾設定不會跑到正式的資料夾；
- 它不繼承呼叫端開著的檔案（例如接續的鎖）。

**同一時間 MUST 只有一個上傳在跑，不論前景或背景**：
- 指令開頭的同步遇到正在跑的上傳時，MUST 跳過 outbox，交給正在跑的那一個，提醒的說法是「背景上傳中，N 筆」，不是「沒上傳成功」；
- `push session <id>…` MUST 等到它給的那幾個 Session 都離開 outbox、Drive 上的 md5 也對了才結束：有上傳在跑時就等（不設逾時，每 10 秒在 stderr 說一次還在等，Ctrl-C 可以中斷），沒有的話自己送。

背景程序開始之後才存進 outbox 的 Session，MUST 在它結束前一併傳完，或由它結束時接著啟動的下一輪傳完，不能留到「下一個指令」。背景上傳失敗時，Session MUST 留在 outbox。下一個會連 Drive 的指令 MUST **啟動背景**再試，而不是在前景上傳，並提醒還有幾個沒上傳。只有 `push` 在前景送。

寫回既有 id 之前的雲端檢查（continue 開始前與結束時、edit 存檔前）照 `session-sync` 的規定，仍然在前景做。

#### Scenario: 不用等
- **WHEN** 執行 `agora import session --external-session-id a,b,c --agent opencode`
- **THEN** 三個 id 印出來、指令就結束，exit 0；不久之後這三個出現在 Drive 上

#### Scenario: 背景失敗之後補傳
- **WHEN** 背景上傳時離線，之後恢復連線並執行任何會連 Drive 的指令
- **THEN** 留在 outbox 的 Session 被傳上去

#### Scenario: 上傳中又改了同一個
- **WHEN** import X 之後、背景還在上傳 X 時執行 `agora edit session X --header title=新的`
- **THEN** 新的版本沒有遺失：背景傳完第一版之後，新版本仍在 outbox，最後 Drive 與本機都是新的版本

#### Scenario: push 等背景
- **WHEN** 背景正在上傳時執行 `agora push session X`
- **THEN** push 等到 X 離開 outbox 才結束，結束時 X 已經在 Drive 上

#### Scenario: 互動模式等的不是背景上傳
- **WHEN** 在互動模式裡匯入，背景上傳還沒結束
- **THEN** 等待視窗在匯入完成時就結束，不等背景上傳；Esc 不會停掉背景上傳

### Requirement: 只有自己驗過的版本離開 outbox

一筆 outbox 項目 MUST 只在「outbox 裡**現在**的 `session.md`，md5 等於 Drive 上的、也等於這一輪上傳的那一個」時才被移除。**分段儲存的版本，還要「`session.md` 的 manifest 列的每一個段都在 Drive 上、md5 與 manifest 相符」**。**更新既有 id 的版本，還要「`sessions/<ULID>` 的資料夾 ID 與送出前相同」**——複製期間被別台刪掉、又被我們自己的 `copy` 建回來的不算成功（要 purge 加另存，見下）。上傳期間被新版本取代的，MUST 留在 outbox，等下一輪再傳。這是**確認當下**的判斷；一個已經確認、離開 outbox 的版本之後因為別台的清理變成不完整，是允許的（`session-sync`「raw 一律分段儲存」），不算這一條被違反。

上傳一筆**更新既有 id** 的項目之前，MUST 用這一輪列檔的結果確認那個 id 還在 Drive 上（**而且那個資料夾裡有 `session.md`**；只有孤兒段的資料夾不算還在）。**列檔說那個資料夾不見了時，MUST 先對它做一次定向查詢（見下）；只有定向查詢明確回「資料夾不存在」才算那個資料夾不在。列檔說資料夾還在、只是沒有 `session.md` 時，同樣 MUST 先定向查詢確認真的沒有 `session.md`，再走孤兒段規則。**「更新既有 id」在前景寫入的當下決定：continue 寫回、edit、import 的原地更新、中斷接續的收尾寫回是；新建的 import、merge、fork、另存的 Session 不是。

**寫回一個還沒被 Drive 確認過的 Session 不算更新既有 id**：同一個 id 在 outbox 裡還有一筆新建的項目（沒有標成更新、也沒有上傳程序正在比對它）時，這次寫入 MUST 維持「新建」，上傳時 MUST NOT 因為 Drive 上沒有它而另存。

不在 Drive 上的話（別台刪掉了），MUST NOT 把它傳回去，而是**另存成一個新的 Session**：新的 id，標頭的 parents 指向原本的 id。relation：continue 寫回的另存成 `continue`（和 `session-sync` 的「接續到一半被別台刪掉的」相同）；edit 與其他寫回的沿用原本的 relation（`edit` 不是 relation 的值）。原本的 id 維持被刪的狀態。另存的 Session 也是分段（走同一個 `stage`，所有段都是新的）。這和 `session-sync`「接續到一半被別台刪掉的」是同一個規則（使用者 10-03 決定）。

**「被別台刪掉」只看得到兩種證據**：送出前那個 ULID 不在完整的 listing 裡（`store.py:812`、`:848`；**那個 ULID 的資料夾裡沒有 `session.md` 也算不在**——purge 後第一個大段 copy 中途失敗只留下孤兒段時，下一輪 MUST 當成不在了），或**送出前後的資料夾 ID 不同**（`sessions/<ULID>` 在送出前的 listing 是 `I1`、確認時是 `I2`，`I1 ≠ I2`——那代表資料夾在複製期間被 purge，又被我們自己的 `copy` 建回來）。資料夾 ID 這一條依賴 Drive 對同名重建的資料夾給新 ID（**假設**，探針驗；探針 MUST 在 R2／R3 之前通過，因為 R2 會把單檔路徑的名字差異偵測換掉；不成立時只剩前一條證據）。段名或舊 `raw-*` 的名字變動（tail 換名、`gen+1` 換整組、清舊世代）MUST NOT 被當成刪除：另一台每一輪的正常清理都會讓舊名字消失，那和「資料夾被 purge」在 listing 上長得一樣；資料夾 ID 不因換名而變，所以只有它算證據。**只看「送出前那個 ULID 已經不在」是不夠的**：那兩次檢查都在複製之前（`store.py:812`、`:848`），而分段項目的複製要好幾分鐘，正好是刪除最容易發生的窗口。

**在把任何一種證據當成「被別台刪掉」之前，MUST 先對那個 ULID 做一次定向查詢**（`lsjson` 該資料夾；送出前的存在檢查與確認時的資料夾 ID 比較兩處都一樣）。只有定向查詢**明確**回「資料夾不存在」（rclone 的 directory-not-found）才算被刪；**回「還在且有 `session.md`」**（與列表矛盾）、**逾時／其他錯誤／權限／空結果／退出碼非 0** MUST 當成「不確定」：那一筆留在 outbox、MUST NOT `purge`、MUST NOT 另存，警告後下一輪再試。定向查詢回「還在、非空、但沒有 `session.md`（只剩孤兒段）」時，走原本的孤兒段規則（下一輪另存、不復活 X），不是「不確定」，也不算「被刪」。**listing 本身的退出碼非 0 或重試過的，MUST 整批不做刪除判定**（全部留在 outbox）。**確認階段的 `purge` 判準與此不同**（原因：確認時資料夾必然存在，它剛被我們自己的 copy 重建）：資料夾 ID 變了 → 定向查詢，MUST 回「還在且有 `session.md`」且資料夾裡**只有**這一輪送出的檔，才 `purge`＋另存；空、查不動、「資料夾不在」（與剛列出的結果矛盾）、只剩孤兒、多出這一輪沒送的檔，MUST 當不確定，留在 outbox。這和 MyAiStorage#152 是同一條規則；現行 `store.py` 與這個 change 的實作 MUST 共用同一個函式（一次定向查詢＋四種結果的回傳）。

#### Scenario: 別台在上傳前刪掉了
- **WHEN** X 的新版本在 outbox，背景上傳之前另一台機器刪掉了 X
- **THEN** X 沒有被傳回 Drive；這次的修改另存成新的 Session Y（parents 指向 X），提醒說明 X 已被別台刪除、修改存成了 Y

#### Scenario: 列表少了資料夾但定向查詢說它還在
- **WHEN** 送出前或確認時的 listing 少了 `sessions/X`（例如 Drive 回 500 之後的那次列表不完整），而對 X 的定向查詢明確回資料夾還在
- **THEN** MUST NOT 當成「被別台刪掉」：不 `purge`、不另存；這一筆留在 outbox，警告後下一輪再試（定向查詢失敗或結果矛盾時同樣是不確定，處理相同）

#### Scenario: 還沒上傳就寫回
- **WHEN** 離線時 import X（X 還在 outbox），接著 continue 或 edit X，之後恢復連線
- **THEN** Drive 上出現的是 X 本身（最新的內容），沒有另存，也沒有「X 已被別台刪除」的提醒

#### Scenario: 分段版本少一個段
- **WHEN** 分段版本上傳後，Drive 的 `session.md` md5 對了，但 manifest 裡有一段不在
- **THEN** 這一筆留在 outbox；不當成成功；下一輪補送（只補缺的段）或整份重來

#### Scenario: 別台的正常清理不算刪除
- **WHEN** 分段 Session X 在 outbox，另一台在同一段時間換掉自己的 tail、或 `gen+1` 讓整組段名消失
- **THEN** 本機的 `upload_batch` MUST NOT `purge` X 的資料夾、MUST NOT 另存（送出前後的資料夾 ID 相同）；X 照常上傳（缺的段由整份 raw 補回）

#### Scenario: 分段版被真的刪掉了（送出前）
- **WHEN** outbox 裡有一個分段版本的 X（entry 內有整份 raw），送出前的 listing 裡沒有 `sessions/X`（那個 ULID 不在）
- **THEN** 另存成新的 Session Y（parents 指向 X），Y 也是分段；Y 的段全是新的，沒有基準段可補

#### Scenario: 分段版被真的刪掉了（複製期間）
- **WHEN** 分段版本 X 正在送（基準段已在 Drive），送出前 listing 的 `sessions/X` 資料夾 ID 是 `I1`；段與 `session.md` 送完、確認的 listing 裡同一個路徑的資料夾 ID 是 `I2`，而 `I1 ≠ I2`（另一台在複製期間 purge 了 X，我們的 `copy` 把資料夾建回來）
- **THEN** MUST 把剛建回來的那個資料夾 `purge` 掉、另存成新的 Session Y（parents 指向 X）；Drive 上沒有 X，**MUST NOT** 因為「確認時 `session.md` 與段都在」就當成成功——那會把別台的刪除悄悄撤銷

#### Scenario: 只剩孤兒段（複製中途失敗）
- **WHEN** 另一台 purge 了 X，本機正在送 X 的第一個大段 copy，中途失敗，`sessions/X` 只剩幾個孤兒段（沒有 `session.md`）
- **THEN** 下一輪的存在檢查 MUST 當成「X 不在了」→ 另存成 Y、X 維持被刪；MUST NOT 把 X 補完而復活它

### Requirement: delete 先在本機
`delete session <id>… --yes` MUST 在前景做三件事，然後指令就結束：
1. 照舊判斷拒絕條件（沒加 `--yes`、有子 Session、正在接續）；
2. 讓給的 Session 從本機的清單與搜尋消失，記下墓碑；
3. 把它加進刪除佇列，並拿掉 outbox 裡同一個 Session 還沒傳的版本。

移到 Drive 垃圾桶的動作，MUST 由背景程序做（和背景上傳是同一個）。雲端沒有的 Session 被 delete 時，只刪本機，不進佇列。

在刪除佇列裡的 Session：
- 同步 MUST NOT 把它加回清單，也 MUST NOT 把它標成雲端沒有；
- `pull` MUST 拒絕它，說明「正在刪除」；
- `push`（包含 `--not-exist-upload`）也 MUST 拒絕它，說明「正在刪除」。

背景刪除失敗的，下一個會連 Drive 的指令 MUST 再試，並提醒「有 N 個等著移到 Drive 垃圾桶」。

#### Scenario: 刪了馬上消失
- **WHEN** 執行 `agora delete session X Y --yes`
- **THEN** 指令結束時 `agora search session` 已經沒有 X、Y；不久之後 Drive 上也沒有

#### Scenario: 刪除排隊時同步或 pull
- **WHEN** X 已經在本機刪掉、背景還沒移到垃圾桶，這時執行一次完整同步，再執行 `agora pull session X`
- **THEN** X 不會回到清單，也沒有被標成雲端沒有；pull 說 X 正在刪除

#### Scenario: 改完馬上刪
- **WHEN** edit X 之後、背景還沒傳上去，就 delete X
- **THEN** X 的新版本不會被傳上去，Drive 上的 X 被移到垃圾桶

### Requirement: 一批只連固定幾次 Drive

背景上傳一批 Session 時，不論幾個，MUST 只用固定幾次 rclone：

1. 一次上傳這一輪要送的段：對每一個分段 Session，比對它的 manifest 與 listing，只把 Drive 還沒有的段（這一版新產生的、或要補回的基準段）**從本機的整份原始檔按 manifest 的 `size` 切進本輪的 batch dir**，一次 `copy --files-from` 送出；送出前 MUST 把這些檔案的 mtime 設成現在（寬限期看的是上傳時間，不是切出時間）。整份 raw MUST NOT 上傳；
2. 段那一次成功之後，一次上傳全部的 `session.md`。段那一次沒有成功時，這一輪 MUST NOT 傳任何 `session.md`，整批留到下一輪；
3. 一次列檔，驗 md5；分段的一併驗 manifest 裡的每一個段，並帶「這個 ULID 的資料夾 ID」，與送出前相同才算沒被刪（「被別台刪掉」的兩種證據）；**送出前取 ID 與確認用的 listing 若都沒有目錄 ID，MUST 各補一次 `lsjson gdrive:sessions --dirs-only`；送出前補不到 MUST NOT 送那一筆更新；確認時補不到 MUST NOT 當成成功（留在 outbox）**；判定「被別台刪掉」之前 MUST 先對那個 ULID 做一次定向查詢（只有明確的 directory-not-found 才算被刪；失敗或與 listing 矛盾 → 不確定，留在 outbox、不 purge、不另存，見上）；
4. 有「不在目前與前一版 manifest、且超過寬限期」的舊段時，再一次刪除（`session-sync`「舊段只刪被取代與過期的」）。Drive 上不再有新的整份 `raw-*`，所以沒有「刪掉被取代的舊 raw」這一步；舊 Session 的 `raw-*.json` 也不逐檔刪，它隨 `agora delete` 的整個資料夾 purge 進垃圾桶。

這一批裡有更新既有 id 的項目時，前面 MAY 再多一次列檔（確認那些 id 還在 Drive 上，並取得它們的資料夾 ID；資料夾 ID 併進那幾次列檔，不算新的呼叫；只有 listing 沒帶目錄項時，送出前與確認各多一次 `--dirs-only`——更新路徑的 rclone 次數上限因此從 7 變 9，只有一邊缺是 8）。先段、後 `session.md` 的順序 MUST 保留，因為讀的一方靠 `session.md` 判斷一個版本寫完了。md5 不符的那幾個留在 outbox，其他的照常完成。

#### Scenario: 匯入三個
- **WHEN** 匯入三個 Session，背景上傳完成
- **THEN** 背景上傳用了不超過 4 次 rclone

#### Scenario: 其中一個沒傳好
- **WHEN** 一批三個裡，有一個的 `session.md` 在 Drive 上的 md5 不符
- **THEN** 那一個留在 outbox，另外兩個離開 outbox

#### Scenario: 原始檔那一次失敗
- **WHEN** 上傳段（分段 Session 的原始檔）的那一次 rclone 中途失敗
- **THEN** 這一輪沒有任何 `session.md` 被傳上去

#### Scenario: 分段的一批
- **WHEN** 一批裡有一個 814 MB 的分段 Session 只多了 1 MB，和兩個新的小 Session
- **THEN** 這一輪只送那 1 MB 的新段＋tail、兩個小 Session 的整份段（各自 1 個 tail 或幾個段），再一次送三個 `session.md`；已封存的 814 MB 沒有重送，整份 raw 沒有被上傳

### Requirement: 互動模式的說明
互動模式裡，背景上傳完成前，雲端欄 MUST 顯示「未上傳」。
- import、merge 的結果視窗 MUST 說明「已經存在本機，背景上傳中」；
- delete 的結果視窗 MUST 說明「已從本機刪除，背景移到 Drive 垃圾桶」；
- 只有 exit 3（背景程序啟動失敗）才說「已存進 outbox，之後的指令會再送」。

#### Scenario: 剛匯入的
- **WHEN** 在未匯入頁匯入一列，回到 Agora 頁
- **THEN** 那一列的雲端欄是「未上傳」；背景上傳完成、下一次重讀之後變成 ✓

