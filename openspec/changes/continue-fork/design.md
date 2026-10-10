## Context

`cmd_continue`（`src/agora/cli.py`）：取原始 raw（merge 出來的是各節要約＋`MERGE_NOTE`／`MERGE_READY`），跨 agent 時轉換，`agent.start_native()` 載入，寫 `pending/<ULID>.json` 並持有 flock，前景跑 agent，結束後 `_finish()` 把對話寫回 `record["agora_id"]`。接續途中那個 Session 被刪掉時，`_finish` 另存成新的（`relation: continue`、`parents` 指回去）。

這份 design 已經依動手前的審查（reviewer，F1～F18）改過。

## Goals / Non-Goals

**Goals：**
- 從任何 Session（普通、merge 出來的、fork 出來的；同 agent 或跨 agent）分岔出一個新 Session B 繼續；原 Session A 完全不動。

**Non-Goals：**
- 接續期間定期寫回（#28，change `continue-periodic-sync`，在這張之後實作）；
- agy（#26）；
- 互動模式的 fork 入口（另開工單）；
- 新增本人看得到的 header 欄位。

## Decisions

### 流程
1. 照 `cmd_continue` 現在的檢查：要 `--agent`；A 在雲端沒有就拒絕（`_refuse_if_gone`，和 merge 一樣：B 的 `parents` 不應該指向一個已經被刪的 Session，F14）。
2. **在 `start_native` 之前**先組好 B 的 header 中不需要 agent 資料的部分，並用 `_with_user` 驗證過（F16）：`--header` 有問題、或 A 的 `case`／`refs` 這版不認得時，在開 agent 之前就失敗，不留下沒人管的 agent session。
3. 組要載入的 raw，在最後接上 fork 的兩則（下面「fork 的說明」與「接在原生 raw 後面」）。
4. `start_native(raw, workdir)`；`launch.agent_session_id` 是 None 就拒絕。
5. **馬上 `agent.export(launch.agent_session_id)`**（claude 帶 `hint_dir=launch.cwd`），確認 `exported.message_count == launch.before_count`（F3）。
6. 用 `exported` 補完 B 的 header（`source = _source(agent, exported)`、`generated`、`sources`），`_save(paths, B, reading(exported.raw), exported.raw, update=False)`。B 等於「把這次啟動的 agent session 匯入」，資料形狀和 import、continue 一致；`raw.md5` 和 agent 端實際存的那份相同。
7. pending 記錄寫 B（下面「pending 記錄」）。
8. 跑 agent；結束後 `_finish` 寫回 B。

- opencode 會因此多一次 `opencode export`（`_import_verified` 已經讀回一次）。先不改 `Launch` 的介面，量起來太慢再說。
- B 的 `agora.source` 一定是這次啟動的 agent session，不能照抄 A 的：`search` 會把來源相同的列當成重複、只列一筆（T7 F2）。

### 還沒被 Drive 確認過的 Session，寫回時不算「更新既有 id」（F1）
- 現況的 bug：B 在步驟 6 存好、還在 outbox 等上傳時，`_finish` 寫回 B 會 `_save(update=True)` → `mark_update` 把 outbox 那一筆換成有 `.update` 的。背景上傳時 Drive 從來沒有 B，就當成「B 已被別台刪除」另存成 C，B 永遠上不了 Drive。離線 import 之後再 continue／edit 也會這樣（既有 bug），fork 讓它變成常態，#28 的定期寫回會更常踩到。
- 規則（store 層）：`_save(update=True)` 在 `stage` 之前判斷：同一個 ULID 在 outbox 裡有一筆**沒有 `.update`** 的項目，而且沒有上傳程序正在比對它（沒有 `.done-<ULID>`），就代表 Drive 從來沒確認過它，這次寫入**不標 `.update`**，新建的維持新建。
- 補 `local-first-writes` 的 MODIFIED delta，並補測試：
  - 離線（`FAKE_RCLONE_FAIL=copy`）fork、agent 說了話、恢復連線後上傳 → Drive 上是 B 本身，沒有另存，沒有「已被別台刪除」；
  - 既有的「離線 import → continue／edit」同樣不再另存。

### 沒有新內容時（打開就離開，F2）
- B 在步驟 6 已經存好。`_finish` 回 None 時：
  - stdout 印 B 的 id（每個寫入都先印 agora id 的約定）；
  - stderr 印「沒有新內容；fork 已存成 agora:B」；
  - exit code 依步驟 6 `_save` 的結果（0 或 3）；
  - **不寫 `unsaved-launches`**：B 的 `agora.source` 就是這個 launch，寫進去會讓未匯入頁永遠跳過它。
- 有新內容時：照現在印最後寫到的 id；exit code 取兩次存檔裡較差的那一個。

### header：照抄使用者欄位，系統欄位重新產生
- 照抄：`h.SYSTEM_KEYS`（`id`、`agora`）以外的欄位，扣掉 `FORK_DROP_KEYS`。
- `FORK_DROP_KEYS`（F6）：
  - `verified`：等本人決定（已經透過 PMO 詢問）。PM 建議不抄，依據是 design 3.4「`verified` 永遠不自動填」，照抄就等於替 B 自動填了。實作先列在這個常數裡，本人決定後只改這裡。
  - A 是 merge 出來的時候，`status`（merge 自動填的 `draft`）也拿掉，和「continue 一個 merge 時拿掉 `status`」（5.4）一致。
- `title`（F5、F8）：
  - `--header title=…` 有給就以它為準，不加 `-fork`（使用者的覆蓋優先於自動欄位，3.5）。
  - 沒給：`f"{A.title or A.description or A.id}-fork"`，先轉成字串（`title: 2026` 會被 YAML 讀成 int）。
  - 步驟 2 與 `_finish` 用同一份 `header_updates`，`_finish` 再套一次也不會把 title 改掉。
- `sources`（F7）：`[這次的 agent session, {id: A, title: A.title, last_modified: A 的 updated_at[:10]}]`，照 `_auto_header(parent_headers=[A])` 的寫法。A 自己的 sources 不帶過來：B 指向 A，A 的出處在 A 裡。
- 重新產生：`id`（新的 ULID）；`agora`：`relation: fork`，`parents: [{id: A, raw_md5: 這次實際取來的 A 的 raw 的 md5（A 是 merge 時為 None）}]`，`source`、`raw`、時間戳記。

### `relation: fork` 對其他地方的影響
- `search`：第三欄印 `source.agent`，B 有 source，不會印 relation。`_rescued_pair` 比的是同來源，B 與 A 的 source 不同，不會被比對到。
- `delete`：有子 Session（`parents` 指向它）時拒絕刪除，所以 A 在 B 還在時刪不掉。這和 merge／救回一樣，照舊；一次一起刪時 delete 會先刪 B。
- **import（F9）**：`_import_one` 的 S7 是「內容變了而且已經有子 Session，就另建新的」。fork 出來的子 Session **不算**：A 仍然是主線，本人會預期 A 原地更新；B 自己有 fork 當下的完整副本，A 原地更新不會讓任何東西消失。補測試：fork 之後再匯入 A 的來源，A 原地更新；有 continue／merge 子 Session 時照舊另建。
- 對 B 再普通 continue：照現在的寫回，`relation` 保持 `fork`。對 B 再 merge：`relation` 不是 merge，取 B 的原始 raw。
- 互動模式：`tui.py` 沒有依 relation 判斷的地方，種類欄顯示 B 的 `source.agent`。這張不加 fork 的入口（F12）。

### fork 的說明：一則 user、一則固定的 assistant
- user：`FORK_NOTE = "從 {id}（{header}）fork"`。
  - `{header}`（F15）只放 A 的 `title`、`description`、`case`、`tags`、`refs`（有值的才放），格式是 `key: value`，用「，」分隔；list 用 `, ` 接起來。例如 `從 agora:01ABC（title: 表格排版，case: mybrain:aistorage，tags: ui, tui）fork`。
  - 不含 `agora` 區塊、`generated`、`sources`。格式用測試固定下來。
- assistant：`FORK_READY = "（知道了：這是從上面那個 Session 分出來的新 Session，原本那個不會被改動。等你的指示。）"`。
- 只要在 `start_native` **之前**接好，`before_count` 就會把這兩則算進去（claude 算 `_count_messages(rewritten)`，opencode 用讀回的 `landed`），打開就離開時 `_finish` 不會再存一次。

### 接在原生 raw 後面（F4）
- 跨 agent 與 merge 來源：在 turns 最後加 fork 的兩輪再 `native()`。不需要新方法。
- 同 agent：原 raw 不經轉換（保留工具呼叫與前綴，快取能命中，5.4 第 2 點），所以 `Agent` Protocol 新增 `append_native(raw, turns) -> bytes`。兩個轉接器與所有測試裡的 FakeAgent 都要補。契約：
  - 回傳的 raw 經過 `turns()` 等於 `turns(raw) + turns`；原本的每一行、每一則一個 byte 都不改；`aux`（claude 的 sidecar）不動。
  - A 以 user 結尾時，先補一則 assistant `NO_REPLY`，再接 fork 的兩則，讓 A 最後的問題不會和 fork 說明黏在一起（同跨 agent 的 W1）。A 以「沒有 tool_result 的 tool_use」結尾（中途被殺）時，同樣先補，並用真的 claude 實測 `--resume` 會不會被 API 拒絕，結果寫進 tasks。
- **claude**：第一則的 `parentUuid` 接在檔案裡**最後一個有 `uuid`、`isSidechain` 不是 true 的行**之後，不是最後一行。檔尾常有 `summary`、`queue-operation`、`last-prompt`、`cost-state`、`mode` 這些沒有 uuid 的行。`claude --resume` 從最新的葉子沿 `parentUuid` 往回接，接錯會讓 agent 完全看不到 A 的前文，而且不會報錯。新行的 `sessionId`、`cwd` 照 `start_native` 的重寫規則處理。
- **opencode**：
  - `info.revert`（本人按過 undo，OC6）：在 fork 的這一份上**先套用 revert**，丟掉被 undo 的訊息、清掉指標，再接。否則接上去的 fork 說明會被藏起來，並在本人第一次發言後被刪掉。A 本身不動。
  - model：新加的 user／assistant 從 A 最後一則 user 與 assistant 抄 `model`、`agent`、`providerID`、`modelID`。否則 `_last_model()` 會停在這則空的 assistant，B 的 `generated.by` 就沒有模型；opencode 恢復時也可能換掉本人原本用的模型。用真的 opencode 實測。

### pending 記錄（F11，#28 照這些欄位實作）
- `agora_id`：B；`parent`：`{id: B, raw_md5: B 存檔時的 raw md5}`，`_finish` 的「途中被刪就另存」因此照樣適用。另存時 `parents` 含 B；背景上傳時才發現的 `_rescue_deleted` 是 `[A, B]`。
- `title`：B 的 title；`header_updates`：同步驟 2；`fork: true`；`saved_md5`：B 在步驟 6 存的 raw md5（#28 判斷「上一次寫回」從這裡開始，並把 B 視為「已經寫過」）。
- A 從頭到尾不寫、不改 `updated_at`。

### 其他
- `--fork` 只有 continue 用；其他動作給了也被忽略，和現在的 `--dir` 一樣（F18）。
- 文件（F13）：design 3.4 的 `relation` 加 `fork`；5.4 第 2 點（轉接器多了 `append_native`）、第 6 點（「不再能從同一點分岔」改成「要分岔用 `--fork`」）；5.6 的子 Session 說明提 fork。

## Risks / Trade-offs

- [打開就離開也會留下一個 fork] → 這是 fork 的本意；不要可以 `agora delete`。
- [A 在 B 還在時刪不掉] → 和 merge 一樣；要刪就一起刪（delete 先刪子 Session）。
- [同 agent fork 的 `source.created_at` 是 A 第一則的時間，search 與互動模式會把 B 排在 A 的日期；「打開就離開」時，未匯入頁可能誤標「↻」（F17）] → 列入 PM 實跑時觀察。
- [`verified` 照不照抄] → 等本人決定，實作只改一個常數。
- [F1 的 store 規則改到既有的 import→continue／edit 行為] → 這是修 bug；local-first-writes 的 spec 一起改，並補測試。
