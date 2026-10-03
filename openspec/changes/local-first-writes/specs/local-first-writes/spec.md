## Purpose

寫入先在本機存完整的一份，再交給背景上傳；delete 先在本機消失，再由背景移到 Drive 垃圾桶。一批動作只連固定幾次 Drive，使用者不必站在那裡等，被別台刪掉的 Session 也救得回來。

## ADDED Requirements

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
一筆 outbox 項目 MUST 只在「outbox 裡**現在**的 `session.md`，md5 等於 Drive 上的、也等於這一輪上傳的那一個」時才被移除。上傳期間被新版本取代的，MUST 留在 outbox，等下一輪再傳。

上傳一筆**更新既有 id** 的項目之前，MUST 用這一輪列檔的結果確認那個 id 還在 Drive 上。「更新既有 id」在前景寫入的當下決定：continue 寫回、edit、import 的原地更新、中斷接續的收尾寫回是；新建的 import、merge、另存的 Session 不是。

不在 Drive 上的話（別台刪掉了），MUST NOT 把它傳回去，而是**另存成一個新的 Session**：新的 id，標頭的 parents 指向原本的 id。relation：continue 寫回的另存成 `continue`（和 `session-sync` 的「接續到一半被別台刪掉的」相同）；edit 與其他寫回的沿用原本的 relation（`edit` 不是 relation 的值）。原本的 id 維持被刪的狀態。這和 `session-sync`「接續到一半被別台刪掉的」是同一個規則（使用者 10-03 決定）。

#### Scenario: 別台在上傳前刪掉了
- **WHEN** X 的新版本在 outbox，背景上傳之前另一台機器刪掉了 X
- **THEN** X 沒有被傳回 Drive；這次的修改另存成新的 Session Y（parents 指向 X），提醒說明 X 已被別台刪除、修改存成了 Y

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
1. 一次上傳全部的原始檔；
2. 原始檔那一次成功之後，一次上傳全部的 `session.md`。原始檔那一次沒有成功時，這一輪 MUST NOT 傳任何 `session.md`，整批留到下一輪；
3. 一次列檔，驗 md5；
4. 有被取代的舊原始檔時，再一次刪除。

這一批裡有更新既有 id 的項目時，前面 MAY 再多一次列檔（確認那些 id 還在 Drive 上）。先原始檔、後 `session.md` 的順序 MUST 保留，因為讀的一方靠 `session.md` 判斷一個版本寫完了。md5 不符的那幾個留在 outbox，其他的照常完成。

#### Scenario: 匯入三個
- **WHEN** 匯入三個 Session，背景上傳完成
- **THEN** 背景上傳用了不超過 4 次 rclone

#### Scenario: 其中一個沒傳好
- **WHEN** 一批三個裡，有一個的 `session.md` 在 Drive 上的 md5 不符
- **THEN** 那一個留在 outbox，另外兩個離開 outbox

#### Scenario: 原始檔那一次失敗
- **WHEN** 上傳原始檔的那一次 rclone 中途失敗
- **THEN** 這一輪沒有任何 `session.md` 被傳上去

### Requirement: 互動模式的說明
互動模式裡，背景上傳完成前，雲端欄 MUST 顯示「未上傳」。
- import、merge 的結果視窗 MUST 說明「已經存在本機，背景上傳中」；
- delete 的結果視窗 MUST 說明「已從本機刪除，背景移到 Drive 垃圾桶」；
- 只有 exit 3（背景程序啟動失敗）才說「已存進 outbox，之後的指令會再送」。

#### Scenario: 剛匯入的
- **WHEN** 在未匯入頁匯入一列，回到 Agora 頁
- **THEN** 那一列的雲端欄是「未上傳」；背景上傳完成、下一次重讀之後變成 ✓
