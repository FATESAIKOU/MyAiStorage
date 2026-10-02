**⚠ 有 1 個 High（S2-1）：目前 HEAD（`5132f8b`，已經推到 origin）上，`agora cache …`、`agora sync`，以及互動模式的 `r`／`s` 都會壞掉。原因是 `cmd_cache`／`cmd_sync` 還在呼叫已經被刪掉的 `cache.refresh_agora`／`refresh_local`／`sync_up`，而新的 `pull`／`push` 還沒有接到 cli 上（task 2.1 還沒做）。**

# Review：openspec change command-batch-actions 第 2 節（pull／push）

2026-10-03，review。對象：`287912f`（pull）、`e174a1b`（push）、`9172041`（K4）、`2d61c69`（單元測試），對照 `openspec/changes/command-batch-actions/specs/session-sync/spec.md` 的前兩個 Requirement（「pull 只處理給的 id」「push 只寫回該寫的檔案」），以及這個 change 的 `design.md`。只提意見，沒有改程式。依照指示，沒有跑整合測試、沒有碰 Drive、沒有讀任何真實的 Session，也沒有執行不帶參數的 `agora`。實測只用 repo 裡的假 rclone，所有目錄都在 scratchpad 裡。工作目錄裡有別人還沒 commit 的 `cli.py`／`test_cli.py`，這次**不看**，只看已經 commit 的部分。

## 結論

| # | 嚴重度 | 一句話 |
|---|---|---|
| S2-1 | **High（暫時性）** | `cache.py` 已經拿掉了 `refresh_agora`／`refresh_local`／`sync_up`，但 HEAD 的 `cli.py` 仍然是 `cmd_cache`／`cmd_sync` 在呼叫它們，結果是 `AttributeError`，被 catch-all 接住，exit 2，訊息是「非預期的錯誤」。互動模式的 `r`／`s` 也會一樣失敗。同時，`pull`／`push` 還沒有接到 cli 上，所以第 2 節的功能，使用者**一個都用不到**。這些都已經推到 origin 了。**建議**：2.1（cli 接上 pull／push，並且拿掉 cache／sync）要盡快 commit；在那之前，不要請任何人試用這個 branch |
| S2-2 | Medium（已實測） | `push`：outbox 裡的 id 上傳**失敗**時，仍然會被算成成功。`staged`（push 之前的 outbox）裡有的 id 會直接 `pass` 然後 `done += 1`，沒有檢查它是不是在 `push_outbox` 回傳的「失敗清單」裡。實測：所有的 copyto 都注入失敗之後，`push` 回傳 **done 1、failed 0**，但那個 id **還在 outbox 裡** |
| S2-3 | Medium | `pull` 一開始就會呼叫 `drive.list_sessions()`，即使給的**只有** `opencode:`／`claude:` 的 id（這些根本不需要 Drive）。所以只要離線，或者 rclone 有任何問題，`pull session claude:<uuid>` 就會整批失敗；而且這個例外是在逐筆的 `try` **之前**丟出的，K5 的「某一筆失敗照樣做下一筆」在這裡沒有作用 |
| S2-4 | Medium | `pull` 一個「雲端的 raw 還沒齊」的 Session 時，只會印出「下次再拉」，但仍然會 `index_mirror`，把那份 session.md **建進索引**。這和 S1／G3（還沒寫完的不建索引，鏡像裡的那份也刪掉）不一致：一個還沒寫完的 Session，就會被搜尋到，也可以被接續 |
| S2-5 | Low～Medium（已實測） | 打錯的 agora id（本機和雲端都沒有）：`pull` 印出「雲端沒有，本機的不動」，然後**算成成功**（實測回傳 `(1, 0)`）；`push` 也是印一行，然後略過。spec 的意思是「雲端沒有的只提醒、不動」，但「**本機也沒有**」的應該是「找不到」，算成失敗 |
| S2-6 | Low | 測試**沒有鎖住** spec 的「沒給 id → exit 1」這個 scenario（`cache.pull([])` 實測回傳 `(0, 0)`，判斷要放在 cli，而 cli 還沒接上）；push 的 outbox 上傳失敗（S2-2）也沒有測試 |
| S2-7 | Low | 重複的 id 沒有去重；`push_mirror` 上傳之後沒有用 md5 確認（`push_one` 有）；`pull` 會把**還在 outbox 裡**的 id 的鏡像，覆蓋成雲端上比較舊的 session.md |

**做得好的部分（spec 第 1、2 個 Requirement）**：id 的前綴規則；push 只傳兩個檔案、不會讓被刪掉的 Session 復活；K4；K5（逐筆的部分）。詳細見下面。

## 1. pull 的 id 前綴規則 ✅

`cache._split`：

| 輸入 | 結果 | spec |
|---|---|---|
| `01M…`（沒有前綴的 ULID） | agora | ✅「沒有前綴……是 agora 的 Session」 |
| `agora:01M…` | agora | ✅ |
| `opencode:ses_…`、`claude:<uuid>` | agent | ✅ |
| `ses_…`、沒有前綴的 uuid | **報錯**，訊息提示要加上 `opencode:` 或 `claude:` | ✅「不從形狀猜」 |
| 不認得的前綴（例如 `foo:x`） | 報錯 | ✅ |

測試：`test_pull_takes_a_bare_ulid_and_an_agora_prefixed_one`、`test_pull_of_a_bare_agent_id_says_which_prefix_to_write` 都有鎖住 ✅。

## 2. push 只傳兩個檔，而且不會讓被刪掉的 Session 復活 ✅（S2-2 除外）

- `store.push_mirror`：只用 `copyto` 上傳 `session.md`，以及標頭 `agora.raw.file` 指到的那**一個** raw（先傳 raw，再傳 session.md，順序符合 S1）；本機沒有那個 raw 時，只傳 session.md 並警告（Q7 的 (a)）。拿掉了 `upload_tree`／`rclone copy`，所以舊的 raw、`*.partial`、`.DS_Store` 都**不可能**被傳上去 ✅（K1、K2）。
- 不會復活：`listing is None or ulid not in listing` 的時候，印出「雲端沒有，沒有傳」，然後 `continue` ✅（`test_push_does_not_revive_a_session_the_cloud_lost`）。
- 先送 outbox ✅（`test_push_sends_the_outbox_first`）。**但是**：outbox 裡如果有某個 Session，是在**別台機器刪掉之後**才在這台機器被寫入的（例如第 3 節的拒絕機制還沒做之前，有人對它做了 edit），`push_outbox` 照樣會把它傳上去。這就是 T1.md 的 Q1，要等第 3.3 節來擋，這裡先記下來。
- S2-2：`if ulid in staged: pass` 要改成 `if ulid in staged: if ulid in left: failed += 1; continue`（或者重新檢查 outbox 裡還有沒有它），這樣 exit code 和 `k/N` 的結果才會一致。

測試：`test_push_sends_session_md_and_the_raw_it_names_and_nothing_else`（舊的 raw、`*.partial`、`.DS_Store` 都留在本機）、`test_push_overwrites_the_copy_on_drive`、`test_push_sends_only_session_md_when_the_raw_is_not_here` 都有鎖住 spec 的兩個 scenario ✅。

## 3. K4 ✅

`local_reading` 改用 `tempfile.mkstemp(dir=…, prefix=".<name>.", suffix=".tmp")` 產生唯一的暫存檔，再 `os.replace`，`finally` 裡會刪掉殘留的檔案。`search_cached` 只會 glob `*.md`，所以那些 `.….tmp` 不會被誤認成快取。另外，`mkstemp` 建出來的檔案權限是 `0600`，`os.replace` 之後會保留這個權限，所以快取檔只有使用者本人能讀，也就順便處理了 K8 的一半（資料夾本身的權限還沒有設）。測試 `test_two_threads_caching_two_sessions_do_not_collide` ✅。

## 4. K5 ✅（S2-3 除外）

`pull` 和 `push` 的迴圈裡，每一筆都包了 `except Exception`，失敗就計數、印出訊息、繼續做下一筆 ✅（`test_pull_goes_on_past_a_failure…`、`test_push_goes_on_past_a_failure…`）。但是，迴圈**之前**的 `drive.list_sessions()`（pull 和 push 都有）不在保護之內，見 S2-3。**建議**：pull 只有在給的 id 裡有 agora 的時候才列檔；列檔失敗時，把所有 agora 的 id 都記成失敗（「連不上 Drive」），agent 的 id 照樣處理。push 只有 agora 的 id，列檔失敗就整批失敗，這樣可以接受，但訊息要說清楚是離線。

## 5. 測試有沒有真的鎖住 spec 的 scenario

| spec 的 scenario | 測試 | 結果 |
|---|---|---|
| 拿下一個 Session 的原始檔（本機只有 session.md → pull → raw 被下載，exit 0） | `test_pull_brings_one_session_down_with_its_raw_and_says_k_of_n` | ✅（exit 0 要等 cli 接上，見 S2-6） |
| 寫進 agent session 的全文快取，再執行一次時，因為沒有過時而略過 | `test_pull_of_an_agent_session_caches_its_full_text`、`…_skips_one_that_is_not_stale` | ✅ |
| 沒給 id → exit 1 | **沒有測試** | ❌ S2-6 |
| 覆蓋雲端的版本 | `test_push_overwrites_the_copy_on_drive` | ✅ |
| 不傳多餘的檔 | `test_push_sends_session_md_and_the_raw_it_names_and_nothing_else` | ✅ |
| （spec 正文）已經在本機、沒有過時的要略過 | `test_pull_of_one_that_is_already_fresh_asks_rclone_for_nothing` | ✅ |
| （spec 正文）雲端沒有的，push 時不傳 | `test_push_does_not_revive_a_session_the_cloud_lost` | ✅ |

建議補上的測試：outbox 上傳失敗時，push 要算成失敗（S2-2）；離線時，只有 agent id 的 pull 仍然會成功（S2-3）；pull 不會把還沒寫完的 Session 建進索引（S2-4）；打錯的 id 要算成失敗（S2-5）；以及 cli 接上之後，`pull`／`push` 不給 id 時 exit 1。

## 這次跑過的指令

| 指令 | 結果（只記形狀） |
|---|---|
| 讀 tasks.md、session-sync/spec.md、design.md；對 4 個 commit 跑 `git show`（src 與測試名稱） | 見上面 |
| `git show HEAD:src/agora/cli.py` 的 `cmd_cache`／`cmd_sync`，以及 `git show HEAD:src/agora/cache.py \| grep refresh_agora\|refresh_local\|sync_up` | cli 還在呼叫，但 cache.py 裡已經沒有這三個函式了（S2-1）；`git branch -r --contains 2d61c69` → origin/agora-lite |
| 在 scratchpad 用假 rclone：stage 一個 Session（不上傳），讓 copyto 全部注入失敗，然後 `cache.push(p, [ulid], {})`；`cache.pull(p, [], {})`；`cache.pull(p, ["01ZZ…"], {})` | push → `done 1 failed 0`，但那個 id 還在 outbox 裡（S2-2）；`pull([])` → `(0, 0)`；打錯的 id → `(1, 0)`（S2-5）；之後 scratchpad 已經刪掉 |

沒有跑整合測試，沒有碰 Drive，沒有讀任何真實的 Session，也沒有執行不帶參數的 `agora`。
