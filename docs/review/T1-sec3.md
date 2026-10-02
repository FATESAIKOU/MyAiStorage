**第三次修正確認（2026-10-03，見最後一節）：G1、G3 修對了，沒有 High。** 但 G1 讓 `_finish` 裡看標記的那一行變成了**唯一**的保護，卻沒有測試（H1，Medium，實測拿掉它 X 會復活）；F6、F8 的測試還是測不到。
~~第二次修正確認（2026-10-03，見最後一節）：F1（High）已經在 7029b68 修好，沒有 High 了。 帶進了一個新的 Medium（G1：一筆收不了尾的 pending 會讓 delete 永遠被擋），另外 F3、F6 的測試還是測不到。~~（G1 已修）
~~第一次修正確認：有 1 個 High（F1：離線時 continue 一個已經標成雲端沒有的 Session，會把它寫回雲端）。~~（已修）

**沒有 High。** 有 3 個 Medium：寫回之前的「雲端沒有」判斷最舊可能是 5 分鐘前的（M1）、`pull --not-exist-delete` 對 agent 的 id 會**無條件**刪掉快取（M2）、索引版本重建只有在「索引是空的」時才會觸發（M3）。

# Review：openspec change command-batch-actions 第 3 節（雲端沒有的 Session）

2026-10-03，review。對象：`8561bbd`、`b772291`、`9359ffc`、`36430f6`、`b6f2fd3`、`7a85390`、`f19937e`，對照 `specs/session-sync/spec.md` 的後三個 Requirement（保留並標記、只在明確要求時刪除或復活、其他指令遇到雲端沒有的 Session）、這個 change 的 `design.md`，以及 `docs/review/T1.md` 的 Q1～Q4。只提意見，沒有改程式。依照指示，沒有跑整合測試、沒有碰 Drive、沒有讀任何真實的 Session，也沒有執行不帶參數的 `agora`。單元測試（用 `git archive HEAD` 取出的乾淨副本跑，不含工作目錄裡別人未 commit 的改動）：**361 passed**。

## 結論

Q1～Q4 的方向都落實了：標記放在獨立的 `cloud_missing` 表，只在列檔完整成功時更新；outbox 和接續中的 Session 不會被標記，也不會被刪除；continue／edit／merge 會拒絕；import 會建一個新的 Session；delete 只刪本機；`children()` 不把雲端沒有的算進去；索引有版本號。剩下的問題是**時間點**和**邊界**：

| # | 嚴重度 | 一句話 |
|---|---|---|
| M1 | Medium | continue／edit 判斷「雲端沒有」時，用的是 `_sync_for` 的**節流**同步（5 分鐘內同步過，就沿用舊的標記）；continue 真正寫回，是在 agent 結束的時候（可能是好幾個小時之後），而那時候不會再檢查一次。所以「別台機器在這段時間裡刪掉 → 這台機器寫回 → 復活」的窗口還在，只是變小了，Q1 並沒有完全關上 |
| M2 | Medium | `pull session claude:<id> --not-exist-delete`：程式**不檢查**agent 那邊是不是真的沒有這個 session，就直接刪掉快取（而且也不會重新 pull）。spec 寫的是「agent 那邊已經沒有這個 session 了，**就**刪掉它的全文快取」。從 search 接管線，一批 id 混了 agora 和 agent 的時候，agent 的快取會全部被刪掉 |
| M3 | Medium | `INDEX_VERSION` 不同時，`Index.__init__` 會把表都 drop 掉，但只有 `sync()` 和 `--no-sync` 的 search 會呼叫 `rebuild_from_mirror`，而它又只在「索引是空的」時才重建。如果版本升級後，第一個打開索引的是 `recover_pending` → `_finish` → `remember`（會先 put 一筆），之後的 `rebuild_from_mirror` 就會看到「不是空的」而**跳過**，索引就只剩那一筆。連得上 Drive 時，sync 會重新下載全部的 session.md 來補回（很慢）；**離線時就一直是缺的** |
| L1～L5 | Low | `mark_missing` 的競態、`delete_session` 把「`sessions/` 不存在」也當成已經刪掉、`--filter cloud=` 的值沒有驗證、幾個 scenario 沒有測試、push 之後的「雲端欄」 |

## 逐項確認

### 列檔失敗不動標記 ✅（Q4）

- `sync`：列檔丟出 StoreError（離線）→ 警告，然後直接 return，**不會**呼叫 `mark_missing`；`remote is None`（找不到 `sessions/`，N12）→ 警告「標記不動」，也不會呼叫；只有列檔**完整成功**時，才會 `mark_missing(本機有、雲端沒有、不在 outbox、沒有在接續中的)`，而且 `mark_missing` 會整個替換 `cloud_missing` 表，所以又出現的那些，標記也會被清掉 ✅。節流跳過的那一次同步，也不會動到標記 ✅。
- 測試：`test_missing_sessions_dir_keeps_mirror`（`sessions/` 不見了 → 沒有任何標記）、`test_deleted_remote_session_stays_and_is_marked`（被刪掉 → 有標記）、`test_not_exist_upload_sends_the_session_back`（傳回去之後再同步 → 標記被清掉）✅。**缺的**：「**原本就有**的標記，在列檔失敗或 `sessions/` 不見時保持不變」這個 scenario（spec 寫的是「原本的標記也不變」），現在的測試都是從「沒有標記」開始的；`test_failed_listing_keeps_mirror`（離線）完全沒有檢查標記（L4）。

### outbox／pending 不被標記，也不被刪 ✅（Q2）

- 標記時排除了 `outbox_ulids` 和 `continuing(paths, ulid)`（`pending/<ulid>.json` 存在）。continue 現在是**寫回同一個 id**（`"agora_id": source_id`），所以 pending 的檔名就是被接續的那個 Session 的 ULID，這個檢查是對的 ✅。
- `pull --not-exist-delete`：在 outbox 裡的，印出「還沒上傳，不能刪」；接續中的，印出「正在接續，不能刪」；都不會刪 ✅（`test_not_exist_delete_keeps_what_has_not_been_uploaded`、`…_keeps_a_session_being_continued`）。
- delete 一個雲端沒有的：`forget_local` 加上刪掉 outbox 裡的那一份，只動本機 ✅。

### 寫回被拒絕的範圍 ✅（M1 除外）

| 路徑 | 處理 | 結果 |
|---|---|---|
| continue | 在打開 agent **之前**呼叫 `_need_in_cloud`，拒絕，exit 1，並提示兩個選擇 | ✅（`test_continue_refuses_a_session_the_cloud_lost`） |
| edit | 一開始就呼叫 `_need_in_cloud` | ✅（`test_edit_refuses…`） |
| merge 的來源 | 每一個來源都呼叫 `_need_in_cloud` | ✅（`test_merge_refuses…`） |
| import 的更新路徑 | `by_source` 會排除雲端沒有的，所以會走「新建」的路，不會寫回 | ✅（`test_importing_the_same_source_again_makes_a_new_session`），符合 spec「MUST 建一個新的 Session」 |
| 互動模式 | 呼叫的是同一套指令，自動得到同樣的行為 | ✅ |

**M1**：上面這些檢查，看的都是 `index.cloud_has()`，而它只有在**完整的同步**之後才會更新。continue／edit 用的是 `_sync_for`，也就是節流的同步（5 分鐘內同步過就不再列檔；只有在 id 不認得時才會完整同步）。另外，continue 的寫回發生在 agent 結束的時候（`_finish` → `_save` → push），中間可能隔了好幾個小時，而 `continuing()` 會讓它在這段期間**不會被標記**。所以會出現：(a) 別台機器在最近 5 分鐘內刪掉了 X，這台機器 `continue X` 照樣通過，最後把 X 寫回去；(b) 這台機器正在接續 X 的時候，別台機器刪掉了 X，這台結束時把 X 寫回去。**建議**：在**真正要寫回**之前（`_save` 寫回既有 id 的那條路徑上，也就是 continue 的 `_finish`、edit、import 的更新），用 `drive.list_one(ulid)` 檢查一次 Drive。如果已經沒有了：continue 的結果改存成一個**新的** Session（`parents` 指向 X），並且說明「agora:X 在你接續的時候被別台機器刪掉了，這次的對話另存成 agora:Y」，這樣使用者剛剛的工作不會丟掉；edit 和 import 的更新則直接拒絕。離線時（`list_one` 失敗），就照現在的做法（存進 outbox，下次 push）。

### search／show／merge／delete／children／import 的處理 ✅

- search：那一行最後加上 `(雲端沒有)`；`--filter cloud=no|yes` 用的是標記，不是標頭裡的欄位 ✅（`test_search_marks…`、`test_search_can_ask_for_it`）。L3：`cloud=` 的值沒有驗證，打錯成 `cloud=maybe` 會被當成 `cloud=yes`，而 `cloud~=no` 也會被當成 `=`。建議只接受 `no`／`yes`，其他的報錯。
- show：stderr 印出一行「雲端沒有……」以及兩個選擇 ✅。
- merge：拒絕雲端沒有的來源 ✅。
- delete：雲端沒有的就只刪本機（`forget_local` 加上 outbox），不會呼叫 Drive ✅（`test_delete_of_one_the_cloud_lost_removes_only_the_local_copy`）。
- `children()`：用 `self.cloud_has(u)` 排除，所以雲端沒有的子 Session 不會擋住刪除，也不會觸發 import 的分岔 ✅。
- import：`by_source` 排除雲端沒有的 ✅。

### S1-4b 的修正 ✅（L2 除外）

`delete_session`：purge 失敗之後，再呼叫一次 `list_sessions()`：列檔也失敗 → 原本的錯誤照樣丟出；列檔成功而且裡面**還有**這個 ULID → 原本的錯誤照樣丟出；其他情況 → 當作已經刪掉。測試鎖住了「還找得到 → 不忘掉」和「列檔也失敗 → 不忘掉」✅。**L2**：`remote is None`（找不到 `sessions/`）也會落到「當作已經刪掉」。N12 的說法是「找不到 `sessions/` 時，可能是 folder ID 或 token 有問題」，所以 sync 遇到這種情況「標記不動」，但 delete 在同樣的情況下卻會把 Session 忘掉，兩邊不一致。建議：`remote is None` 時，也照原本的錯誤丟出。

### 索引版本重建 ⚠️（M3）

- `INDEX_VERSION = 2`；`PRAGMA user_version` 不同時，`DROP TABLE` 掉三張表，再設定新的版本號；`sync` 裡會呼叫 `index.rebuild_from_mirror(paths)` ✅。
- **M3**：`rebuild_from_mirror` 只在 `not self.known()` 時才做。如果升級之後，**第一個**建立 `Index` 的是 `main` 一開始的 `recover_pending` → `_finish` → `_save` → `remember`（先 put 了一筆），那麼之後的 sync 會看到索引不是空的而跳過重建，就只能靠「重新下載全部的 session.md」慢慢補回來；離線時，就會一直缺。**建議**：`Index.__init__` 在 drop 表之後設一個旗標（`self.rebuilt = True`），或者在 drop 之後**馬上**從鏡像重建（`Index` 知道 `paths`）。不要用「是不是空的」來判斷要不要重建。另外，要加一個測試：把 `user_version` 設成 1，在鏡像裡放三份 session.md，先 `remember` 一筆，再 `Index(paths)`，斷言三筆都在。
- 同時開著互動模式和一個 CLI 的時候，其中一個 drop 表，另一個的連線就會遇到「no such table」。這只會在**升級的那一刻**發生，可以接受，在 design 寫一句就好。

## 其他（Low）

| # | 問題 | 建議 |
|---|---|---|
| L1 | `mark_missing` 的參數是 `set(known) \| set(index.known())`，其中 `index.known()` 是在**列檔之後**才讀的。如果在這段時間裡，另一個程序（例如互動模式和 CLI 同時在跑）剛存好並推上去一個**新的** Session，它不在這次的 `remote` 裡，也已經不在 outbox 裡了，就會被**誤標**成雲端沒有，一直到下一次完整同步才會清掉（這段期間 continue／edit 都會被拒絕） | 只標記「**列檔之前**就已經在索引裡」的那些，也就是只用列檔前拿到的 `known` |
| L2 | 見上面的 S1-4b | `remote is None` 也要丟出錯誤 |
| L3 | `--filter cloud=` 的值沒有驗證 | 只接受 `no`／`yes` |
| L4 | 沒有測試的 scenario：「原本就有的標記，在列檔失敗或 `sessions/` 不見時保持不變」；「outbox 裡的不會被**標記**」（現在只測了「不會被刪」）；索引版本重建（M3）；`pull --not-exist-delete` 一個 agent 那邊**還有**的 session（M2） | 各補一個 |
| L5 | push 之後（尤其是 `--not-exist-upload`），標記要等下一次**完整**同步才會清掉。這段期間 search 和互動模式的「雲端」欄會一直顯示「雲端沒有」，continue 也會被拒絕 | push 成功的那些 id，直接從 `cloud_missing` 刪掉（push 自己就知道它們已經在雲端上了） |

## M2 的細節

`cache.pull` 的迴圈是：

```python
if kind == "agora":
    _pull_agora(...)
elif not_exist_delete:
    _drop_reading(paths, kind, bare)      # 不管 agent 那邊還有沒有
else:
    local_reading(...)
```

**建議**：

```python
elif not_exist_delete and bare not in listed.setdefault(kind, _listed(agents, kind)):
    _drop_reading(paths, kind, bare)      # agent 那邊真的沒有了
else:
    local_reading(...)                    # 還在：照常 pull（更新快取）
```

測試 `test_not_exist_delete_for_an_agent_id_drops_its_cached_text` 用的是 agent 那邊**沒有**的情況，所以沒有抓到這個問題。要補一個「agent 那邊還有 → 快取保留（並且會更新）」的測試。

## 這次讀過、跑過的東西

`git show` 7 個 commit 的 `src`（`store.py` 的 `cloud_missing`／`mark_missing`／`cloud_has`／`continuing`／`forget_local`／`delete_session`、`cache.py` 的 `--not-exist-delete`／`--not-exist-upload`、`cli.py` 的 `_need_in_cloud` 和它被呼叫的位置），以及測試的名稱；`tests/unit/test_store_more.py`、`test_cache.py`、`test_store.py` 裡和標記有關的那幾個測試；tasks.md 的第 3 節。`git archive HEAD` 取出副本，`PYTHONPATH=<副本>/src .venv/bin/python -m pytest -q tests/unit` → 361 passed。沒有跑整合測試，沒有碰 Drive，沒有讀任何真實的 Session，也沒有執行不帶參數的 `agora`。

## 修正確認（2026-10-03）

對象：`5eab055`（M1～M3、L4）、`738008d`（design 5.10）。在 `git archive 5eab055` 取出的副本跑單元測試：**373 passed**。repro 只在 scratchpad 的副本裡加探測測試，用的是 fake rclone 和 fake agent，repo 沒有動。沒有跑整合測試，沒有碰 Drive，也沒有讀任何真實的 Session。

> 另外：現在的 HEAD `9e44286`（T2 1.6）在 `git archive HEAD` 的副本上有 **10 個單元測試失敗**。原因是它把 `store.index_mirror` 拿掉了、`_put_file` 改名成 `index_file`，可是 `cache.py` 還在呼叫 `index_mirror`，`rebuild_from_mirror` 也還在呼叫 `_put_file`，所以 M3 的重建在 HEAD 上是壞的。工作目錄裡的 `cache.py`／`store.py` 有別人還沒 commit 的改動，看起來正在修；我沒有動。

| 項目 | 結果 |
|---|---|
| M1 寫回前直接問 Drive | ⚠️ 方向對（continue 和 edit 開始前、continue 結束後都會問；中途被刪就另存成 Y，parents 指向 X），但有 **F1（High）**、F2～F4，見下 |
| M2 agent id 先確認真的沒了 | ✅ 還在就照常 pull，測試也有；F5（Low～Medium）：清單讀不到的時候會被當成「沒了」 |
| M3 版本不同一律從鏡像重建 | ✅ 在 `Index.__init__` 裡偵測到版本不同就重建，「db 被刪掉」也會走到這條路（新檔的 user_version 是 0）；測試寫的就是 `recover_pending` 會先寫一筆的那種情況 ✅（HEAD 上壞了，見上面的註記） |
| L4 「原本的標記不變」 | ✅ 從已經有標記開始，離線和 `sessions/` 不見都測到了 |
| L4 「outbox 不被標記」 | ❌ 這個測試**沒有測到**：sync 第一次標記的時候，那一筆還沒進索引，本來就不可能被標；我把 `u not in staged` 拿掉做 mutation，store／cache／cli 的測試全部照樣通過（F6） |
| 738008d design 5.10 和 spec | ✅ 三個 Requirement 都一致，只有 F7 那兩處 |

### F1（High）：離線時 continue 一個**已經標成雲端沒有**的 Session，會把它寫回雲端

`5eab055` 把 continue 和 edit 原本的 `_need_in_cloud(index, id)`（看標記）**換成了** `_refuse_if_gone`（直接問 Drive），而 `_lost_in_cloud` 遇到離線會回傳 False（「離線不算刪掉」）。所以只要標記已經在了（完整同步過，知道 X 被別台刪掉了）、現在離線、而且 X 的原始檔已經在本機（例如之前 `show --raw` 過，或接續過）：

```
continue X → 沒有被拒絕，agent 打開 → 存進 outbox（exit 3）→ 恢復連線後隨便一個指令 → X 回到 Drive
```

我實測過（探測測試 `PROBE-OFFLINE-CONTINUE 3 launched: True outbox has X: True` → `PROBE-RESURRECTED True`）。這正是 spec「接續被別台刪掉的：exit 1、agent 沒有被打開」要擋的事，也正是 Q1／K1 那種「沒有人決定，別台的刪除就被撤銷了」；而且在這個修正**之前**是會被擋下來的，所以是一個回歸。edit 在同樣的條件下，如果 Session 沒有原始檔，也一樣會寫回去。

**修法**：兩個都檢查——`_need_in_cloud(index, id)`（標記說沒了就拒絕）**加上** `_refuse_if_gone`（Drive 說沒了就拒絕）。`_finish` 也一樣：標記說沒了，或 Drive 說沒了，就另存成 Y。補一個測試：先標記，再 `FAKE_RCLONE_FAIL=lsjson`，然後 continue → exit 1，agent 沒有被打開。

### F2（Medium）：Drive 上沒有 `sessions/` 時是 TypeError

`_lost_in_cloud` 寫的是 `ulid not in store.Drive(paths).list_sessions()`，可是 `list_sessions()` 在找不到 `sessions/` 時回傳的是 **None**（N12），所以會丟出 `TypeError`；而它只接 `StoreError`。實測：edit 得到 exit 2，「非預期的錯誤：TypeError: argument of type 'NoneType' is not iterable」。如果發生在 `_finish` 裡，pending 會留著，每一個指令的 `recover_pending` 都會再失敗一次，一直到 `sessions/` 回來為止。**修法**：`remote is None` 時當成「不知道」，回傳 False，和 sync 遇到這種情況「標記不動」是同一個判斷；再配合 F1 的「也看標記」。

### F3（Medium）：還在 outbox 的 Session 會被當成「別台刪掉了」

標記那一邊會排除 outbox（Q2），可是 `_lost_in_cloud` 沒有排除。如果 X 的上傳一直沒成功（離線時匯入，或者上傳不穩），而 `_sync_for` 因為 5 分鐘內同步過被節流、沒有先推 outbox，那麼 `continue X`／`edit X` 就會被拒絕，訊息是「雲端沒有（別台機器刪掉了）」。實測：`PROBE-OUTBOX 1 in outbox: True | … 雲端沒有（別台機器刪掉了）`。在 `_finish` 裡，同樣的情況會讓 X 被另存成一個 Y。**修法**：`ulid in store.outbox_ulids(paths)` 就回傳 False。

### F4（Medium）：在**這台機器上**接續的途中刪掉 X，結束時會用同一個 id 把 X 寫回去

`_finish` 裡 `lost = bool(hdr) and …`：只有 hdr 還在的時候才會問 Drive。`cmd_delete` 不檢查 `continuing`，所以在另一個終端機（或互動模式）`delete X --yes` 之後，索引裡已經沒有 X，`_finish` 就會走 `elif not hdr`，把 `hdr["id"] = X` 當成 import 存回去，X 就這樣回到 Drive 了。實測：`on drive after delete: False` → `on drive after finish: True`。spec 的新 scenario 說的是「另一台機器刪掉」，但「X 維持被刪的狀態」這個承諾，這條路一樣要守住。**修法**：(a) `cmd_delete` 遇到 `store.continuing` 的就拒絕（「正在接續，等它結束再刪」），和 `pull --not-exist-delete` 的作法一樣；(b) `elif not hdr` 也改成另存一個新的 Session（parents 用 pending 記錄裡的 `parent`），不要沿用 X 的 id。

### 其他

| # | 嚴重度 | 問題 | 建議 |
|---|---|---|---|
| F5 | Low～Medium | M2：opencode 的 `list_sessions` 在 db 不在、認不出結構、或 sqlite 出錯時回傳 `[]`，claude 在 `projects/` 不在時也回傳 `[]`，所以「讀不到 agent」被當成「agent 那邊什麼都沒有」，`--not-exist-delete` 就會把那個 agent 的快取全部刪掉 | 清單是空的、但快取裡有東西時，就當成「不知道」，不要刪（印一行說明） |
| F6 | Low | 「outbox 不被標記」的測試沒有測到（見上面的 mutation） | 先 `store.remember(paths, folder)`（或連續 sync 兩次，而且上傳都失敗），再斷言 `missing_in_cloud() == []` |
| F7 | Low | 738008d 的 design 5.10 寫在 5eab055 **之前**，所以沒有 PM 的 M1 決定；design 5.4 第 6 步還是寫「寫回原本那個 agora Session（同一個 id）」，沒有例外。另外 spec 和 design 都寫「import 的更新路徑……拒絕」，同時又寫「import……建一個新的 Session」，實作是後者 | 5.4 第 6 步加上「接續途中原本的被刪掉了（不論是別台還是這台），就另存一個新的 Session，parents 指向它」；把 import 那句統一成「不更新它，改建一個新的」 |
| F8 | Low | edit 只在開始前檢查一次，`$EDITOR` 開著的這段時間沒有再檢查（tasks 3.7 寫的是 continue 和 edit 都檢查兩次） | 存檔前再呼叫一次 `_refuse_if_gone` |
| F9 | Low | 一次 continue 要做兩次完整的 `list_sessions()`（會遞迴列出所有 session，要好幾秒）；docstring 說「只問這一個資料夾」，這不正確。用 `list_one` 不行，因為「找不到」不能信（S1-4b），所以完整列檔是對的選擇，但說明要改 | 修正 docstring；可以把結果交給接下來的 sync 重用 |
| F10 | Low | 另存成 Y 之後，如果在 `_save` 和 `pending.unlink()` 之間當掉，下一次 `recover_pending` 會再另存一個 Y′（每次都是新的 ULID），結果有兩份 | 把 Y 的 id 在第一次決定的時候就寫進 pending 記錄，重跑時沿用 |
| F11 | Low | `_finish` 遇到離線時照常寫回 X（因為不知道 X 被刪了），之後推 outbox 的時候也沒有再檢查，所以「接續途中被刪、結束的時候剛好離線」這種情況還是會讓 X 復活 | 在 outbox 那一筆加上「這是更新既有的 X」的註記，`push_outbox` 推之前用列檔確認 X 還在，不在就改成另存（可以和 F1 一起排進 T1 的收尾，或者在 design 裡寫明這是已知的窗口） |

**結論**：M2、M3 修對了，L4 的第一個測試也對。M1 修對了 PM 決定的那一半（中途被別台刪掉就另存 Y，測試也有），但把「看標記」換成了「只問 Drive」，造成了 F1 這個回歸（High）；F2～F4 是這個新寫法的三個邊界。建議先修 F1～F4 再驗收。

## 第二次修正確認（2026-10-03）

對象：`7029b68`（F1～F7，順便修了 F8、F9；同一個 commit 也做了 T1-size 的幾項精簡：`_listing`、`_absent`、`_split_ids`、`_actor`、pull 改用 `store.mirror_one`）。在 `git archive 7029b68` 取出的副本跑單元測試：**387 passed**。在副本裡重跑上一次的 7 個探測測試，再加 2 個新的，並做了 9 個 mutation。repo 沒有動。沒有跑整合測試，沒有碰 Drive，也沒有讀任何真實的 Session。

### 上一次的探測，現在的結果

| 探測 | 上一次（5eab055） | 現在（7029b68） |
|---|---|---|
| 離線時 continue 一個已經標記的 Session（F1） | agent 打開、exit 3、X 回到 Drive | **exit 1，agent 沒有打開，outbox 沒有 X，沒有復活** ✅ |
| 離線時 edit 一個已經標記的 Session（F1） | 不會被擋 | exit 1 ✅ |
| Drive 上沒有 `sessions/`（F2） | TypeError | 不再是 TypeError；edit 會因為原始檔拿不到而失敗，這是預期的 ✅ |
| 還在 outbox 的 Session（F3） | 被說成「別台機器刪掉了」 | 不再出現「雲端沒有」 ✅ |
| 在這台機器上接續的途中 delete（F4） | 結束時用同一個 id 寫回去，X 復活 | **delete 被拒絕（exit 1，「正在接續」），X 留著，結束時正常寫回** ✅ |

### Mutation（每次只拿掉一個修正，跑 cli、cli_more、cache、store、store_more 的測試）

| 拿掉的修正 | 結果 |
|---|---|
| F1：`_refuse_if_gone` 裡看標記的那一行 | **被抓到**（`test_continue_of_a_marked_session_is_refused_even_offline`） |
| F1：`_finish` 裡看標記的那一行 | 沒被抓到。接續中的 Session 不會被標記，接續一個已經標記的 Session 一開始就會被拒絕，所以這一行實際上走不到，算是防禦性的寫法，可以接受 |
| F2：`remote is not None` | 沒被抓到：沒有任何測試在「沒有 `sessions/`」的情況下跑 continue 或 edit |
| F3：`_lost_in_cloud` 的 outbox 判斷 | **沒被抓到**：新的測試 `test_a_session_in_the_outbox_is_not_taken_for_deleted` 是 edit 一個**已經在 Drive 上**的 Session，所以 Drive 的清單裡本來就有它，outbox 的判斷有沒有都一樣。要用「第一次上傳就沒成功」的 Session 才測得到（上一次的探測就是這樣做的） |
| F4：delete 的 `continuing` 判斷 | **被抓到**（`test_delete_refuses_a_session_that_is_being_continued`） |
| F4b：索引裡沒有的也另存一個新的 | **被抓到**（`test_lock_survives_agora_death_while_agent_lives`） |
| F5：清單是空的就不刪 | **被抓到**（`test_not_exist_delete_keeps_the_cache_when_the_agent_lists_nothing`） |
| F6：`mark_missing` 排除 outbox | **還是沒被抓到**：那個測試裡從來沒有東西成功上傳到 Drive，所以 Drive 上**根本沒有 `sessions/`**，sync 走的是「標記不動」那條路（我實際印出來看了：「Drive 上找不到 sessions/……標記不動」），標記本來就不會動 |
| F8：edit 存檔前再查一次 | 沒被抓到：沒有測試 |

### 新的問題

| # | 嚴重度 | 問題 | 建議 |
|---|---|---|---|
| G1 | **Medium**（F4 帶進來的回歸） | `cmd_delete` 只要看到 `pending/<ulid>.json` **存在**就拒絕，訊息是「正在接續，等它結束再刪」。可是一筆**收不了尾**的 pending（例如 agent 的 session 已經不見了，或者 `_finish` 每次都失敗）會一直留著，**沒有任何程序拿著它的鎖**，結果 X 就**永遠刪不掉**，訊息還說它正在接續。實測：`補存 … 失敗，下次再試：'ses_gone'` → `正在接續，等它結束再刪`，再跑一次也一樣，pending 還在 | 用 `flock(LOCK_EX \| LOCK_NB)` 判斷真的有人在跑：拿不到鎖 → 拒絕（真的在接續）；拿得到鎖 → 讓 delete 照做，並提示「有一筆中斷的接續沒補存成功：pending/<ulid>.json」。之後如果 `_finish` 成功，也會依 F4b 另存一個新的，不會讓 X 復活 |
| G2 | Low | 一批 delete 裡只要有一個是接續中的，就會丟出 InputError，**整批**都不刪；有子 Session 的那種情況只是略過那一個，其他的照刪 | 和 children 一樣，略過那一個、印出原因，exit 1 |
| G3 | Low～Medium（**不是回歸**，8561bbd 就這樣了） | `pull X --not-exist-delete` 在 Drive 上**沒有 `sessions/`**時（N12：可能是 folder ID 或 token 有問題）會把 X 當成雲端沒有，然後**刪掉本機副本**。實測：exit 0，本機和索引裡的 X 都不見了。sync 和 `_lost_in_cloud` 在同樣的情況下都當成「不知道」，只有這裡不是 | `_pull_agora` 收到 `remote is None` 時，拒絕這一個（「Drive 上找不到 sessions/，不能確定它是被刪掉的」） |
| G4 | Low | F3、F6、F2、F8 的測試補強（見上面 mutation 的結果）。F6 的修法：先 `_save` 另一個 Session 讓 Drive 上有 `sessions/`，再做現在的步驟 | 各補一個；F3 用「stage 但從來沒上傳成功」的 Session |
| G5 | Low | `_pull_agora` 先用 `mirror_one` 把 session.md 放進索引，最後又 `index_file` 一次。在 `fetch_raw` 依 N8 換了 session.md 的時候，第二次是需要的，所以不是錯，只是要加一句註解說明 | 加註解 |

### 其他確認

- design 5.4 第 6 步已經寫進「接續途中被刪掉了（不論是別台還是這台）就另存成新的」✅。5.10 寫明了「判斷同時看標記與當下的 Drive，離線時也不會放行」✅，import 那句也改成「不更新它，改建一個新的」✅；不過同一段的最後還保留著舊的那句「import 的來源對到雲端沒有的那一筆時建一個新的 Session」，兩句意思一樣，重複了，刪掉一句就好。
- 精簡的部分（`_listing` 用回傳 StoreError 物件來表示離線、pull 在遇到第一個 agora id 時才列 Drive、`_absent`、`_split_ids`）：行為沒有變；`_split_ids` 讓 pull 和 push 也會 strip 了，順便修好了 T1-size L4 提到的那個 `"a, b"` 的小 bug ✅。

**結論**：F1（High）和 F4 都修對了，探測和 mutation 都證實。F2、F3、F5、F7 也修對了。F4 帶進了 **G1（Medium）**：一筆收不了尾的 pending 會讓 delete 永遠被擋，建議在驗收之前修。F3 和 F6 的測試還是測不到它們要保護的東西（G4）。G3 是之前就有的問題，可以和 G1 一起修。

## 第三次修正確認（2026-10-03）

對象：`9e56116`（G1、G3、G4）。在 `git archive 9e56116` 取出的副本跑單元測試：**393 passed**。做了 9 個 mutation，並在副本裡加了探測測試，repo 沒有動（工作目錄裡的 `store.py` 有別人還沒 commit 的改動，我沒有碰，量測也不包含它）。沒有跑整合測試，沒有碰 Drive，也沒有讀任何真實的 Session。

### G1：用 flock 判斷「真的有人在接續」✅

`store.continuing()` 改成：檔案不在 → False；用 `LOCK_EX | LOCK_NB` 拿得到鎖 → False（是留下來的記錄）；拿不到 → True。我直接測了五種情況，都對：

| 情況 | `continuing()` |
|---|---|
| 有記錄，沒有人拿著鎖 | False |
| 同一個程序用另一個 fd 拿著鎖（flock 是以 open file description 為單位，所以一樣會衝突） | True |
| agora 關掉了自己的 fd，但繼承了鎖的 agent 子程序還活著（C1） | True |
| 子程序也結束了 | False |
| 記錄不在 | False |

delete 遇到真的在接續的 → 拒絕；遇到留下來的記錄 → 印出「有一筆中斷的接續沒補存成功（路徑），照樣刪」，然後照常刪除。上一次的探測（`ses_gone` 那一筆讓 delete 永遠被擋）現在可以刪了，新的測試 `test_delete_goes_through_when_the_pending_record_is_a_leftover` 也測到了這一點。

### G3：沒有 `sessions/` 時，`--not-exist-delete` 不刪 ✅

`_pull_agora` 依序檢查 outbox、接續中，最後是 `remote is None`，遇到時丟出「不能確定它是被刪掉的」，本機的副本留著。測試有。

### Mutation（每次只拿掉一個修正）

| 拿掉的修正 | 結果 |
|---|---|
| G1：`continuing()` 改回「檔案存在就算」 | **被抓到**（`test_delete_goes_through_when_the_pending_record_is_a_leftover`） |
| G1：`continuing()` 不看鎖，一律回傳 False | **被抓到**（`test_delete_refuses_a_session_that_is_being_continued`） |
| G3：沒有 `sessions/` 的那個判斷 | **被抓到** |
| F2：`remote is not None` | **被抓到**（新的 `test_continue_and_edit_survive_a_drive_without_a_sessions_folder`） |
| F3：`_lost_in_cloud` 的 outbox 判斷 | **被抓到**（新的測試改用「從來沒上傳成功」的 Session）✅ G4 這一半做到了 |
| F4：delete 的接續中判斷 | **被抓到** |
| F6：`mark_missing` 排除 outbox | **還是沒被抓到**，原因見 H2 |
| F8：edit 存檔前再查一次 | **還是沒被抓到**，原因見 H3 |
| F1：`_finish` 裡看標記的那一行 | **沒被抓到**，但現在這一行很重要，見 H1 |

### H1（Medium）：G1 讓 `_finish` 的標記判斷變成唯一的保護，卻沒有測試

以前，接續中的記錄（只要檔案存在）會讓那個 Session **永遠不被標記**，所以 `_finish` 裡看標記的那一行實際上走不到（第二次確認時我說「可以接受」）。G1 之後，**留下來的記錄不再擋標記**，於是有了這條路：接續被中斷（agora 和 agent 都死了）→ 別台機器刪掉 X → 這台機器做一次完整同步，**X 被標記** → 之後在**離線**的時候 `recover_pending` 把那筆接續收尾。這時 `_lost_in_cloud` 因為離線而回傳 False，**只有標記**能讓 `_finish` 改成另存 Y。

我實測了這條路（探測測試，fake rclone 和 fake agent）：

| | 有沒有標記 | 恢復連線之後 X 有沒有回到 Drive |
|---|---|---|
| `9e56116` | True | **False** ✅（另存成 Y） |
| 拿掉 `_finish` 裡的 `not index.cloud_has(ulid)` | True | **True** ❌（X 復活了） |

程式現在是對的，但這是 Q1 那一類的保證，卻沒有測試在守著它。**建議**：把這個探測測試（步驟就是上面那幾步）正式加進 `test_cli.py`。

### H2（Low）：F6 的 mutation 抓不到，是因為它本來就是「等價的 mutant」

這一次補了兩個測試，但兩個都抓不到，原因不一樣：

1. `test_a_session_in_the_outbox_is_not_marked`：就算拿掉排除，sync 在 `mark_missing` **之後**還會跑 `_index_outbox` → `remember` → `Index.put` → `drop`，而 **`drop` 會把那一筆的標記一起刪掉**，所以 sync 回傳的時候，標記已經沒了。也就是說，對一個讀得到的 outbox Session 來說，那個排除是**多出來的一層保險**，拿掉它，對外看得到的結果也一樣。
2. `test_a_staged_session_the_index_cannot_read_is_not_marked_either`：它的 docstring 說「沒有這個排除就會被標記」，**這不正確**。壞掉的 session.md 在標記之前，就已經被 `push_outbox` 的 `read_entry` 擋下、移到 `outbox/.bad` 了（實測：sync 之後 `staged: False`、`quarantined: [ulid]`、`in index: False`）。它不在 outbox，也不在索引裡，本來就不可能被標記。

**建議**：二選一。(a) 接受它是保險，在 `sync` 的註解寫明「`_index_outbox` 也會清掉標記，這個排除是為了不讓標記短暫出現」，並刪掉第二個測試，或者至少改掉它不正確的 docstring；(b) 想測就直接測 `mark_missing` 收到的參數（spy 一下 `Index.mark_missing`），斷言 staged 的 ulid 不在裡面，這樣拿掉排除就會失敗。

### H3（Low）：F8 的測試在 edit 開始**之前**就刪了

`test_editing_one_the_editor_kept_open_while_it_vanished` 是在呼叫 `edit` **之前**就 `rmtree`，所以第一個 `_refuse_if_gone` 就已經拒絕了，存檔前的那一次檢查根本沒有走到。註解寫的是「the delete happens while it is open」，但 fake 的 `editor()` 裡什麼都沒做。**建議**：把 `rmtree` 搬進 fake 的 `editor()` 裡面。

### 其他

| # | 嚴重度 | 問題 | 建議 |
|---|---|---|---|
| H4 | Low | `continuing()` 在 `exists()` 和 `open()` 之間，如果檔案剛好被刪掉（接續剛好結束），`FileNotFoundError` 是 OSError，會被當成「有人拿著鎖」而回傳 True，那一次就會誤判成「正在接續」 | `except FileNotFoundError: return False` 放在 `except OSError` 前面 |
| H5 | Low | 刪掉一個有留下記錄的 Session 之後，那筆記錄還在；之後如果 `_finish` 有一天成功了（例如 agent 的 session 又讀得到了），就會依 F4b 另存成一個新的 Y（「在這台機器上被刪掉了」）。使用者明明刪掉了，結果冒出一個新的 Session，可能會嚇一跳；而如果它一直收不了尾，每個指令都會再試一次、再印一次失敗 | delete 時把那筆記錄移到 `pending/.bad`（`quarantine`，保留內容，但不再重試），訊息裡說清楚；或者在 design 5.6 寫明這個行為 |
| H6 | Low | G2（整批 delete 只要有一個在接續中，就整批都不刪）還沒修 | 同第二次確認的建議 |
| — | — | T1-size 的 D1（每一筆 outbox 上傳兩次）在 `9e56116` 還在；工作目錄裡的 `store.py` 有改動，看起來正在修 | 修好之後，順便看 `_fault` 有沒有一起搬過去 |

**結論**：G1、G3 都修對了，flock 的判斷五種情況都對，mutation 也抓得到。G4 修好了 F2、F3 兩個測試；F6 是等價的 mutant（H2），F8 的測試步驟順序錯了（H3）。G1 帶來的副作用是 **H1（Medium）**：`_finish` 裡看標記的那一行從「走不到」變成了「離線收尾時唯一的保護」，程式是對的，但要補測試。
