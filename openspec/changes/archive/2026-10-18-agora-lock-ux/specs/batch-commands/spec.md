## ADDED Requirements

### Requirement: delete 遇到正在接續的就跳過
`agora delete session <id>… --yes` 中，正在接續中的 Session（本機 `pending/<ULID>.json` 的鎖有人拿著，見 `session-continue`）MUST 被跳過，其他照刪；MUST NOT 因為其中一個被鎖就整批不刪。只是留下來的記錄（鎖沒人拿）MUST NOT 算正在接續，照現行規則刪除（含原本的警告）。

跳過的每一個 MUST 在 stderr 寫明**是哪一個**與**為什麼**：pending 記錄的 `agent`、`dir`、`started_at`（拿不到的欄位略過，MUST NOT 印這些以外的私人資訊）。結尾的摘要 MUST 說明跳過了幾個，以及等它結束再重跑同一個指令（要連它一起刪就加 `--force`，見下一條）。

exit code：有任何一個被鎖而跳過時 MUST 是 1；**全部都被鎖住**時 MUST 什麼都不刪，stderr 明確說明「沒有刪除任何東西」，exit 1。MUST NOT 用 4（`continue` 被鎖的專用訊號，固定行 `agora-locked <agora id>`）或 5（agent 子行程死掉）；**背景上傳器起不來時仍是 3**（`EXIT_IN_OUTBOX` 的既有優先序：佇列那半沒人做比「有跳過」更需要說），MUST NOT 被當成 1。自己刪過的 id 重跑時略過仍不算失敗（0）；找不到的 id（打錯）仍然整批不刪（1）；有子 Session 的規則不變。

#### Scenario: 批次裡混著被鎖與沒鎖的
- **WHEN** `agora delete session agora:A agora:B --yes`，A 正在接續中、B 沒有
- **THEN** B 被刪掉並印出它的 id；A 留著，stderr 說 A 正在接續中（agent claude、目錄 ~/proj、從 2026-10-18T05:01:00Z 開始）並在摘要說跳過了幾個；exit 1

#### Scenario: 全部都被鎖住
- **WHEN** 給的三個都在接續中
- **THEN** 三個都沒有被刪，stderr 列出三個與原因，並說這次沒有刪除任何東西；exit 1，不是 4 或 5

#### Scenario: 鎖放掉之後重跑
- **WHEN** 上一個例子之後那些 agent 結束（`pending` 的鎖沒人拿），再執行同一個指令
- **THEN** 那三個被刪掉，exit 0

#### Scenario: 收不了尾的記錄不算被鎖
- **WHEN** 一個 Session 有一筆 `pending/<ULID>.json`，但沒有任何人拿它的鎖
- **THEN** 照現行規則刪掉，並印原本的「有一筆中斷的接續沒補存成功」警告

### Requirement: delete --force 刪使用中的
`agora delete session <id>… --force --yes` MUST 連正在接續中的 Session 一起刪，刪法與一般刪除相同（本機鏡像與索引、outbox 那一筆、`trash-queue`、`deleted` 記錄）。`--force` MUST NOT 取代 `--yes`：沒有 `--yes` 時什麼都不刪（exit 1），預覽 MUST 把使用中的那幾個標出來，並說明 `--force` 也要 `--yes`。沒加 `--force` 時行為如上一條。

刪掉每一個使用中的 Session **之前**，stderr MUST 先寫明後果：那個 agent 之後的寫回會另存成一個新的 Session（`parents` 指向被刪的這個），這個 id 不會再有它的更新；訊息 MUST 含哪個 agent、哪個目錄、從什麼時候開始。pending 記錄與它的鎖是另一個行程的東西，MUST NOT 被動（刪了鎖會讓那個 run 寫到錯的地方）；那段工作 MUST NOT 消失——之後的寫回照 `session-continue` 的另存規則存成新的 Session。

#### Scenario: 確定要刪使用中的
- **WHEN** A 正在接續中，`agora delete session agora:A --force --yes`
- **THEN** A 被刪掉（本機與垃圾桶佇列與一般刪除相同），stderr 在刪除之前說明 A 正在接續中（誰、哪個目錄、從什麼時候）以及那個 agent 之後的寫回會另存成新的 Session；exit 0

#### Scenario: --force 沒有 --yes
- **WHEN** `agora delete session agora:A --force`（A 正在接續中）
- **THEN** 什麼都沒有刪，清單把 A 標成使用中並說明 `--force` 也要 `--yes`；exit 1

#### Scenario: 被 force 刪掉的那段工作還在
- **WHEN** A 被 `--force --yes` 刪掉之後，接續 A 的那個 agent 結束
- **THEN** 那次的對話另存成一個新的 Session（`parents` 指向 A），A 維持被刪的狀態

#### Scenario: 被 force 刪掉的那個還在雲端
- **WHEN** 在 A 的機器上 `--force --yes` 刪掉正在接續的 A，A 在 Drive 上也有
- **THEN** A 進 `trash-queue`（背景移到 Drive 垃圾桶），和一般刪除一樣
