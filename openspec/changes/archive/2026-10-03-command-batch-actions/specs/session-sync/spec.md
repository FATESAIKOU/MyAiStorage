## Purpose

本機與 Google Drive 之間明確地搬 Session：pull 把給的 Session 拿到本機，push 把給的 Session 寫回 Drive；別台機器刪掉的 Session 不會被悄悄傳回去。

## ADDED Requirements

### Requirement: pull 只處理給的 id
`agora pull session <id>…` MUST 只處理給的 id，不給 id 時 MUST 報錯（exit 1）。沒有前綴或 `agora:` 前綴的 id 是 agora 的 Session：拿下它的 `session.md` 與標頭指到的原始檔。`opencode:<id>`、`claude:<id>` 是這台機器上的 agent session：把它的全文（閱讀版）寫進本機快取。`ses_…` 或 uuid 沒有寫 agent 前綴時 MUST 報錯，不從形狀猜。已經在本機、沒有過時的 MUST 略過。

#### Scenario: 拿下一個 Session 的原始檔
- **WHEN** 本機只有某個 Session 的 `session.md`，執行 `agora pull session agora:X`
- **THEN** 它的原始檔被下載到本機，exit 0

#### Scenario: 寫進 agent session 的全文快取
- **WHEN** 執行 `agora pull session claude:<uuid>`
- **THEN** 那個 session 的閱讀版寫進本機快取；再執行一次時，因為沒有過時而略過

#### Scenario: 沒給 id
- **WHEN** 執行 `agora pull session`（型態有寫，但沒給任何 id）
- **THEN** exit 1，提示要給 session id

### Requirement: push 只寫回該寫的檔案
`agora push session <agora id>…` MUST 先送出 outbox，再把每個給的、雲端還在的 Session 的 `session.md` 與它標頭 `agora.raw.file` 指到的那一個原始檔寫回 Drive，同名覆蓋。**從鏡像送出時**，Drive 上多出來的檔不刪；**從 outbox 送出時**（`push_one`），被取代的舊 `raw-*` 會被刪掉（換過原始檔的 Session 需要這樣，N8），其餘多出來的檔仍然不刪。本機沒有那個原始檔時，只傳 `session.md`。舊的原始檔、下載到一半的檔、以 `.` 開頭的檔 MUST NOT 被上傳。

#### Scenario: 覆蓋雲端的版本
- **WHEN** 本機改過某個 Session 的 `session.md`，執行 `agora push session agora:X`
- **THEN** Drive 上的 `session.md` 變成本機的版本

#### Scenario: 不傳多餘的檔
- **WHEN** 本機的鏡像裡有舊的原始檔、`*.partial`、`.DS_Store`
- **THEN** push 之後 Drive 上沒有這些檔

### Requirement: 雲端沒有的 Session 保留在本機並標記
同步時，**只有在這次列檔完整成功**時，本機有、雲端沒有、而且不在 outbox、也不是**正在接續中**（`pending/<ULID>.json` 的鎖有人拿著；只是留下來、沒有人拿鎖的記錄不算）的 Session MUST 被標成「雲端沒有」，鏡像與索引 MUST 保留。列檔失敗、離線或 Drive 上沒有 `sessions/` 時，MUST NOT 新增或清除任何標記。雲端又出現時，下一次同步 MUST 清除標記。還在 outbox 的 MUST 顯示為「未上傳」，不算雲端沒有。

#### Scenario: 別台機器刪掉了
- **WHEN** 另一台機器刪掉 X，這台機器同步
- **THEN** X 還在這台的索引與鏡像裡，標成雲端沒有

#### Scenario: 列檔失敗
- **WHEN** 同步時列檔失敗或離線
- **THEN** 沒有任何 Session 被標成雲端沒有，原本的標記也不變

#### Scenario: 又出現了
- **WHEN** X 被另一台機器傳回 Drive，這台再同步
- **THEN** X 的「雲端沒有」標記被清掉

### Requirement: 雲端沒有的 Session 只在明確要求時刪除或復活
`pull --not-exist-delete` MUST 刪掉給的 id 中雲端沒有的本機副本；在 outbox 或接續中的 MUST NOT 被刪，並印出原因。`push --not-exist-upload` MUST 把給的 id 中雲端沒有的傳回 Drive（傳 `session.md` 與它指到的原始檔；本機沒有那個原始檔時拒絕這一個）。都沒加 flag 時，遇到雲端沒有的 MUST 只印一行提醒、不動。flag 只對 agora 的 id 有作用；對 agent 的 id，`--not-exist-delete` 的意思是「agent 那邊已經沒有這個 session 了，就刪掉它的全文快取」。

#### Scenario: 刪掉本機副本
- **WHEN** X 雲端沒有，執行 `agora pull session X --not-exist-delete`
- **THEN** X 從這台機器的鏡像與索引消失

#### Scenario: 不刪還沒上傳的
- **WHEN** Y 還在 outbox，執行 `agora pull session Y --not-exist-delete`
- **THEN** Y 沒有被刪，訊息說它還沒上傳

#### Scenario: 復活
- **WHEN** X 雲端沒有，執行 `agora push session X --not-exist-upload`
- **THEN** X 回到 Drive，下一次同步後標記被清掉

### Requirement: 其他指令遇到雲端沒有的 Session
會寫回既有 id 的指令（continue、edit，以及互動模式裡的對應動作）遇到雲端沒有的 Session MUST 拒絕（exit 1），並提示 `agora push session X --not-exist-upload` 或 `agora pull session X --not-exist-delete`。search 與 show MUST 標出「雲端沒有」，search MUST 支援 `--filter cloud=no` 與 `cloud=yes`。merge MUST NOT 接受雲端沒有的 Session 當來源。delete 一個雲端沒有的 Session MUST 只刪本機副本（exit 0）。雲端沒有的 Session MUST NOT 算成別人的子 Session（不擋刪除、不觸發 import 的分岔）。import 的來源對到雲端沒有的那一筆時 MUST NOT 寫回它，而是 MUST 建一個新的 Session（使用者接受 review T1 Q3 的建議；review T1-final 指出原本和上一句矛盾）。

#### Scenario: 接續被別台刪掉的
- **WHEN** X 雲端沒有，執行 `agora continue session X --agent opencode`
- **THEN** exit 1，提示兩個選擇，agent 沒有被打開

#### Scenario: 接續到一半被別台刪掉的
- **WHEN** 執行 `agora continue session X` 之前 X 還在雲端，agent 工作的這段時間裡另一台機器刪掉了 X
- **THEN** 這次的工作不丟掉：另存成一個新的 Session Y（relation continue、parents 指向 X），並說明 X 已被刪；X 維持被刪的狀態，沒有被寫回去

#### Scenario: 用管線清掉
- **WHEN** 執行 `agora search session --filter cloud=no`
- **THEN** 只列出雲端沒有的 Session，每行最後標示 `(雲端沒有)`
