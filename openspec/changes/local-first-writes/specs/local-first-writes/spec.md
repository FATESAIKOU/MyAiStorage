## Purpose

寫入先在本機存完整的一份，再交給背景上傳；delete 先在本機消失，再由背景移到 Drive 垃圾桶。一批動作只連固定幾次 Drive，使用者不必站在那裡等，被別台刪掉的 Session 也救得回來。

## ADDED Requirements

### Requirement: 本機保留完整的一份
import、continue、merge 寫出的每個 Session，MUST 在指令結束前把 `session.md` 與標頭 `agora.raw.file` 指到的原始檔都放進本機鏡像。上傳成功之後，鏡像裡的原始檔 MUST 留著。同一個 Session 被取代的舊原始檔 MUST 從鏡像清掉。標頭沒有原始檔的 Session（例如 merge 的 sections）照它實際有的檔案存。

#### Scenario: 救回在這台寫的
- **WHEN** 在這台 import 一個 Session X，背景上傳完成之後，別台機器刪掉了 X，這台同步後執行 `agora push session X --not-exist-upload`
- **THEN** X 回到 Drive（不會因為「本機沒有原始檔」而拒絕）

#### Scenario: 接續之後只留新的原始檔
- **WHEN** 對 X 執行 continue 並且有新的對話
- **THEN** X 在本機鏡像裡只有一個原始檔，就是新標頭指到的那一個

### Requirement: 背景上傳
import、continue、merge MUST 在把 Session 存進本機之後就結束，不等上傳。上傳 MUST 由一個脫離終端機的背景程序做：它不接使用者的鍵盤輸入，也不寫到呼叫它的終端機或 pipe，輸出寫到狀態目錄裡的記錄檔。同一時間 MUST 只有一個背景上傳在跑；它開始之後才存進 outbox 的 Session，MUST 在它結束前一併傳完，或由下一個背景上傳傳完。背景上傳失敗時，Session MUST 留在 outbox，下一個會連 Drive 的指令 MUST 再試，並且照現在的規則提醒還有幾個沒上傳。`push session <id>…` MUST 維持前景，等到傳完才結束。

#### Scenario: 不用等
- **WHEN** 執行 `agora import session --external-session-id a,b,c --agent opencode`
- **THEN** 三個 id 印出來、指令就結束；不久之後這三個出現在 Drive 上

#### Scenario: 背景失敗之後補傳
- **WHEN** 背景上傳時離線，之後恢復連線並執行任何會連 Drive 的指令
- **THEN** 留在 outbox 的 Session 被傳上去

#### Scenario: 互動模式等的不是背景上傳
- **WHEN** 在互動模式裡匯入，背景上傳還沒結束
- **THEN** 等待視窗在匯入完成時就結束（不會等背景上傳），Esc 不會停掉背景上傳

### Requirement: delete 先在本機
`delete session <id>… --yes` MUST 先讓給的 Session 從本機的清單與搜尋消失、記下墓碑，指令就結束；移到 Drive 垃圾桶的動作 MUST 由背景程序做（和背景上傳是同一個）。還沒移到垃圾桶之前，同步 MUST NOT 把它加回清單，也 MUST NOT 把它標成雲端沒有。背景刪除失敗的，下一個會連 Drive 的指令 MUST 再試。既有的拒絕條件（沒加 `--yes`、有子 Session、正在接續）照舊在前景判斷。

#### Scenario: 刪了馬上消失
- **WHEN** 執行 `agora delete session X Y --yes`
- **THEN** 指令結束時 `agora search session` 已經沒有 X、Y；不久之後 Drive 上也沒有

#### Scenario: 刪除排隊時同步
- **WHEN** X 已經在本機刪掉、背景還沒移到垃圾桶，這時執行一次完整同步
- **THEN** X 不會回到清單，也沒有被標成雲端沒有

### Requirement: 一批只連固定幾次 Drive
背景上傳一批 Session（不論幾個）時，MUST 只用固定幾次 rclone：
1. 一次上傳全部的原始檔；
2. 原始檔都到了之後，一次上傳全部的 `session.md`；
3. 一次列檔，驗證 md5；
4. 有被取代的舊原始檔時，再一次刪除。

先原始檔、後 `session.md` 的順序 MUST 保留，因為讀的一方靠 `session.md` 判斷一個版本寫完了。md5 不符的那幾個 MUST 留在 outbox，其他的照常完成。import 開頭的同步 MUST 受 5 分鐘節流，不在每次 import 都完整列檔。

#### Scenario: 匯入三個
- **WHEN** 匯入三個 Session，背景上傳完成
- **THEN** 背景上傳用了不超過 4 次 rclone

#### Scenario: 其中一個沒傳好
- **WHEN** 一批三個裡，有一個的 `session.md` 在 Drive 上的 md5 不符
- **THEN** 那一個留在 outbox，另外兩個離開 outbox

### Requirement: 互動模式的說明
互動模式裡，背景上傳完成前，雲端欄 MUST 顯示「未上傳」。import、merge 的結果視窗 MUST 說明「已經存在本機，背景上傳中」。delete 的結果視窗 MUST 說明「已從本機刪除，背景移到 Drive 垃圾桶」。

#### Scenario: 剛匯入的
- **WHEN** 在未匯入頁匯入一列，回到 Agora 頁
- **THEN** 那一列的雲端欄是「未上傳」；背景上傳完成、下一次重讀之後變成 ✓
