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
