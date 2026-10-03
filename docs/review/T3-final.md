> **審到 HEAD `8b8490f`。** 程式和測試與 `c312d9f` 完全相同；`c312d9f` 之後只多了 review 的文件。impl2 的精簡和 X1／W4／W5、impl1 的 T5 U1 與整合測試，都是在這之後的 commit，之後再補看。

**沒有 High。** spec 的每一個 Requirement 都有測試守著大部分的 MUST，16 個關鍵 MUST 的 mutation 抓到 12 個。

**沒被抓到的 4 個是真的缺口**：
- 前景寫入時放的 `.update` 記號（L7／N5 救回「別台刪掉的」靠的就是它），**沒有任何指令層級的測試**（F1，Medium）；
- push 等背景（F2，Medium），而且「每 10 秒說一次還在等」**根本不會發生**（T3-sec3 R7，還沒修）；
- sync 遇到正在跑的上傳時說「背景上傳中，N 筆」（F3）；
- 批次 import 開頭的同步要「受 5 分鐘節流」（F4）。

另外，N5 的 Scenario「別台在上傳前刪掉了」，還有兩個說法上的 THEN 沒做到：relation 和 parents（V3）、提醒使用者（V4）。

# T3 歸檔前的最後一關：spec 對測試

2026-10-03，review。
- 在 `git archive` 取出的 `c312d9f` 副本裡跑（和 HEAD 的程式相同）：`compileall` 通過，單元測試 **511 passed**，沒有失敗（T3-sec7 量的）。
- mutation 在副本裡做，用 pytest 跑（有 conftest 的隔離），子程序另外傳了 `/tmp` 底下的 `AGORA_CONFIG`／`AGORA_CACHE_DIR`／`AGORA_STATE_DIR`／`HOME`。
- 沒有跑整合測試，沒有碰 Drive，也沒有讀任何真實的 Session。

整合測試 `tests/integration/test_background_writes.py`（`a14e7cb`）：讀起來已經照 T3-it 的 I1、I2 改了（不再解包字串、用 `index.children`、拿住鎖、刪掉 `last-sync`、比對 md5 和標題、檢查 `.bad`）。但是這一份我**沒有審完、也沒有跑**，PM 說 impl1 正在跑，結果以 impl1 的為準。下表的「整合」欄只列出有對應的測試，不代表它通過了。

測試的檔名縮寫：
- `ub` = `test_upload_batch.py`
- `bg` = `test_background.py`
- `cli` = `test_cli.py`
- `clim` = `test_cli_more.py`
- `ca` = `test_cache.py`
- `st` = `test_store.py`
- `stm` = `test_store_more.py`
- `tui` = `test_tui.py`
- `it` = `tests/integration/test_background_writes.py`

## spec：local-first-writes

### Requirement：本機保留完整的一份

| MUST／Scenario | 測試 | |
|---|---|---|
| 指令結束前，`session.md` 和原始檔都在本機鏡像 | `ub::test_the_mirror_keeps_the_whole_session`；`it::test_import_several_…` | ✅ mutation 抓到 |
| 上傳之後原始檔留著；被取代的舊原始檔清掉 | `ub::test_the_mirror_keeps_the_whole_session`（新版本之後只剩一個 `raw-*`） | ✅ mutation 抓到 |
| Scenario「救回在這台寫的」 | `ub::test_a_session_written_here_can_be_rescued`；`it::test_a_session_deleted_elsewhere_…` | ✅ |
| Scenario「接續之後只留新的原始檔」 | `ub::test_the_mirror_keeps_the_whole_session` 用的是 `stage`＋`remember`，不是 continue 指令；`clim::test_continue_uses_the_local_raw_…` 看的是 **Drive** 上只剩一個 | ⚠️ 沒有用 `continue` 指令去看**鏡像**裡只剩一個。邏輯都在 `remember` 裡，風險低 |

### Requirement：背景上傳

| MUST／Scenario | 測試 | |
|---|---|---|
| 存進本機就結束，exit 0 | 單元測試用 inline；「不等」由 `bg::test_a_reader_of_our_stdout_gets_eof_before_the_background_is_done`（P4）和 `it` 守著 | ✅ |
| 只有背景啟動失敗才回 3 | `cli::test_upload_failure_exits_3_and_stays_searchable` 等 4 個 | ✅ mutation 抓到 |
| 背景：不接鍵盤、不寫終端機或 pipe、輸出寫進記錄檔 | `bg::test_it_is_launched_the_way_the_lock_needs`、`…_nothing_it_prints_reaches_our_terminal`、P4 | ✅ mutation 抓到（拿掉 `start_new_session`、拿掉 `stdin=DEVNULL`） |
| 沿用環境變數；不繼承開著的檔案 | `bg::test_it_is_launched_…`、`bg::test_it_does_not_inherit_the_lock_a_continue_is_holding` | ✅ |
| 同一時間只有一個上傳 | `bg::test_a_second_uploader_skips_while_the_lock_is_held` | ✅ |
| 同步遇到正在跑的上傳，跳過 outbox，說「背景上傳中，N 筆」 | `bg::test_a_sync_starts_the_uploader_instead_of_uploading` 守住了「不在前景送」；**說法沒有測試** | ❌ F3：把這一句改成「沒上傳成功」，沒有測試會紅 |
| `push` 等它給的 id 離開 outbox、md5 對了才結束；有上傳在跑就等（不設逾時，每 10 秒說一次，Ctrl-C 可以中斷） | `ca::test_push_sends_the_outbox_first`、`ca::test_push_checks_the_md5_drive_reports` 守住了「自己送、驗 md5」。**「有上傳在跑就等」沒有測試** | ❌ F2：mutation「拿不到鎖就不拿鎖直接送」沒被抓到。另外，**「每 10 秒說一次」不會發生**：`hold_upload_lock(paths, blocking=True)` 是阻塞的，回傳之前迴圈不會跑，訊息那一段是死碼（T3-sec3 R7，還沒修）。使用者看到的是 push 不出聲地停住 |
| 背景開始之後才進 outbox 的，在它結束前傳完，或由下一輪傳完 | `bg::test_a_session_staged_while_it_runs_still_goes_up`、`bg::test_it_looks_again_after_letting_the_lock_go`（P1） | ✅ |
| 背景失敗時留在 outbox；下一個連 Drive 的指令**啟動背景**再試，並且提醒 | `ub::test_the_next_command_after_a_failure_sends_it`、`st::test_failed_upload_stays_in_outbox_and_sync_pushes_it`、`ub::test_the_uploader_is_started_for_a_waiting_outbox` | ✅ mutation 抓到（「同步改成在前景上傳」）。⚠️ 提醒（`main` 的「outbox 有 N 筆未上傳」）沒有測試 |
| 只有 `push` 在前景送 | 同上 | ✅ |
| 寫回既有 id 前的雲端檢查仍在前景 | `cli::test_continue_and_edit_ask_drive_even_right_after_a_sync` | ✅ |
| Scenario「不用等」 | `it::test_import_several_…`（三個 id、exit 0、之後 Drive 上有） | ✅（整合） |
| Scenario「背景失敗之後補傳」 | `ub::test_the_next_command_after_a_failure_sends_it` | ✅ |
| Scenario「上傳中又改了同一個」 | `bg::test_an_edit_that_lands_mid_upload_is_still_sent_by_this_process`、`ub::test_a_version_staged_mid_upload_…`、`ub::test_an_edit_that_lands_while_we_compare_is_not_lost` | ✅ |
| Scenario「push 等背景」 | **沒有** | ❌ F2 |
| Scenario「互動模式等的不是背景上傳」 | P4（讀 stdout 的一方在背景還在跑時就拿到 EOF；等待視窗就是這樣讀子程序的）；背景另開 session，所以 Esc 打不到它（`bg::test_it_is_launched_…`） | ✅（靠機制，沒有用 TUI 按鍵直接驅動） |

### Requirement：只有自己驗過的版本離開 outbox

| MUST／Scenario | 測試 | |
|---|---|---|
| md5 等於 Drive 上的、也等於這一輪送的，才移除；被取代的留下 | `ub::test_only_what_drive_confirmed_leaves_the_outbox`、`…_edit_that_only_changes_the_header…`、`…_version_staged_mid_upload…` | ✅ mutation 抓到 |
| 更新既有 id 前，用這一輪的列檔確認它還在（L7） | `ub::test_an_update_someone_else_deleted_is_saved_as_a_new_session` 等 3 個 | ✅ mutation 抓到 |
| 「更新既有 id」在**前景寫入的當下**決定：continue 寫回、edit、import 原地更新、中斷接續收尾是；新建的不是 | `ub::test_a_fresh_import_is_not_mistaken_for_an_update` 守住了「新建的不是」。**「是」的那四種，沒有任何測試**：所有 N5 的測試都是自己呼叫 `store.mark_update(folder)` | ❌ **F1**：mutation「`_save` 永遠不放 `.update`」**沒被抓到**。真的發生的話，edit 或 continue 之後，別台刪掉的 Session 會被**悄悄傳回去**（違反 T1 Q1），而且沒有任何單元測試會紅。整合測試的情境 4 會碰到這條路，但它要連 Drive，不在 CI 裡 |
| 不在 Drive 上的話，另存成新的 Session：新的 id、parents 指向原本的、**relation 照原本的寫入** | `ub::test_an_update_someone_else_deleted_…`：新的 id ✅，`parents == [X]` ✅；relation 沒有測 | ⚠️ V3（還沒修）：relation 沿用 X 原本的（例如 `import`），不是 `edit`／`continue`；X 原本的 parents 也被換掉了 |
| Scenario「別台在上傳前刪掉了」：THEN 有提醒說明 | 測試斷言 stderr 有「已被別台刪除」，但那是 inline 模式 | ⚠️ V4（還沒修）：真的背景程序只把這句寫進 `upload.log`，使用者看不到 |

### Requirement：delete 先在本機

| MUST／Scenario | 測試 | |
|---|---|---|
| 照舊的拒絕（沒加 `--yes`、有子 Session、正在接續） | `cli::test_delete_needs_yes_…`、`…_refuses_a_session_with_children`、`…_refuses_a_session_that_is_being_continued` | ✅ |
| 從本機清單與搜尋消失、記下墓碑 | `cli::test_delete_several_at_once_children_first`、`…_of_one_the_cloud_lost_…`、`…_rerun_skips_what_it_deleted` | ✅ mutation 抓到 |
| 加進刪除佇列、拿掉 outbox 裡還沒傳的 | `cli::test_a_deleted_session_is_queued_…`、`…_deleted_before_its_upload_never_goes_up` | ✅（T3-sec6／sec7 的 mutation） |
| 由背景移到垃圾桶；雲端沒有的只刪本機 | `bg::test_the_background_purges_…`、`cli::test_a_cloud_lost_session_is_not_queued_…` | ✅ |
| 佇列裡的：同步不加回、不標記 | `ub::test_sync_neither_lists_nor_marks_…`、`ub::test_sync_does_not_mark_…` | ✅ |
| 佇列裡的：pull、push（含 `--not-exist-upload`）都拒絕 | `cli::test_pull_refuses_…`、`ub::test_push_refuses_…` | ✅ mutation 抓到。push 的測試沒有加 `--not-exist-upload`，不過程式裡的拒絕在處理這個旗標**之前**，所以一樣會擋 |
| 背景刪除失敗的，下一個指令再試，並提醒 | `bg::test_a_trash_that_fails_stays_in_the_queue_…`、`cli::test_the_next_command_says_how_many_…` | ✅ |
| Scenario「刪了馬上消失」 | 上面幾個，加上 `it::test_delete_several_…` | ✅ |
| Scenario「刪除排隊時同步或 pull」 | 同步的兩個，加上 pull 的一個 | ✅ |
| Scenario「改完馬上刪」 | `cli::test_a_session_deleted_before_its_upload_never_goes_up`（inline，看 `calls.log` 沒有 `copy`） | ✅ |

### Requirement：一批只連固定幾次 Drive

| MUST／Scenario | 測試 | |
|---|---|---|
| Scenario「匯入三個」：不超過 4 次 rclone，原始檔先、`session.md` 後 | `ub::test_three_sessions_go_up_in_a_few_rclone_calls` | ✅ |
| Scenario「原始檔那一次失敗」：不傳任何 `session.md` | `ub::test_a_failed_raw_upload_sends_no_session_md_at_all` | ✅（T3-sec5 的 mutation） |
| Scenario「其中一個沒傳好」 | `ub::test_only_what_drive_confirmed_leaves_the_outbox` | ✅ mutation 抓到 |

### Requirement：互動模式的說明

| MUST／Scenario | 測試 | |
|---|---|---|
| 背景上傳完成前，雲端欄顯示「未上傳」 | `tui::test_the_cloud_column_says_where_each_session_is` | ✅ mutation 抓到 |
| import、merge 說「已經存在本機，背景上傳中」；delete 說「已從本機刪除，背景移到 Drive 垃圾桶」；只有 exit 3 說 outbox | `tui::test_the_result_window_says_an_import_is_safe_here`、`…_the_drive_half_is_on_its_way`、`test_only_a_failed_start_still_says_the_outbox` | ✅（T3-sec6 的 mutation） |
| Scenario「剛匯入的」：先是「未上傳」，背景完成、重讀之後變成 ✓ | 雲端欄的測試是**靜態的**：一個在 outbox、一個在 Drive，各看一次 | ⚠️ 沒有測「同一列，從未上傳變成 ✓」。顯示的規則已經守住了，風險低 |

## spec：batch-commands（MODIFIED「import 一次多個」）

| MUST／Scenario | 測試 | |
|---|---|---|
| 多個 id（逗號、多次） | `cli::test_import_several_ids_at_once`；`it`／`test_import_batch.py` | ✅ |
| 先同步一次，**受 5 分鐘節流** | `cli::test_import_batch_syncs_once` 只數了次數 | ❌ F4：把 `throttle=True` 拿掉，沒有測試會紅 |
| 某一個失敗照樣做下一個；exit 是第一個非零的 | `cli::test_import_keeps_going_after_one_fails`、`…_exit_code_is_the_first_non_zero` | ✅ |
| 只給一個 id 時，行為和給多個一樣 | `cli::test_import_one_id_keeps_the_old_exit_code` | ✅ |
| Scenario「中斷後重跑」 | `cli::test_import_rerun_skips_what_is_done` | ✅ |

## Mutation（16 個關鍵 MUST；12 個被抓到、4 個沒有）

| # | 拿掉的 MUST | 結果 | 抓到它的測試 |
|---|---|---|---|
| 1 | 鏡像不放原始檔 | ✅ | `ub::test_the_mirror_keeps_the_whole_session` 等 5 個 |
| 2 | 鏡像不清舊的原始檔 | ✅ | `ub::test_the_mirror_keeps_the_whole_session` |
| 3 | 背景啟動失敗也算成功（不回 3） | ✅ | `cli::test_upload_failure_exits_3_…` 等 4 個 |
| 4 | 背景不另開 session | ✅ | `bg::test_it_is_launched_the_way_the_lock_needs` |
| 5 | 背景接到呼叫端的 stdin | ✅ | 同上 |
| 6 | 同步改成在前景上傳 | ✅ | `ub::test_the_uploader_is_started_…`、`bg::test_a_sync_starts_the_uploader_…` |
| 7 | 同步遇到正在跑的上傳，不說「背景上傳中」 | ❌ **沒被抓到** | —（F3） |
| 8 | push 不等背景（拿不到鎖就不拿鎖直接送） | ❌ **沒被抓到** | —（F2） |
| 9 | 前景不放 `.update` 記號 | ❌ **沒被抓到** | —（F1） |
| 10 | 不比對 Drive 的 md5 就移出 outbox | ✅ | `ub::test_only_what_drive_confirmed_leaves_the_outbox` |
| 11 | 不檢查 id 還在不在 Drive（L7） | ✅ | `ub::test_an_update_someone_else_deleted_…` 等 3 個 |
| 12 | delete 不從本機忘掉 | ✅ | `cli::test_delete_several_at_once_children_first`、`…_of_one_the_cloud_lost_…` |
| 13 | pull 不拒絕刪除佇列裡的 | ✅ | `cli::test_pull_refuses_a_session_queued_for_deletion` |
| 14 | push 不拒絕刪除佇列裡的 | ✅ | `ub::test_push_refuses_a_session_queued_for_deletion` |
| 15 | 雲端欄不顯示「未上傳」 | ✅ | `tui::test_the_cloud_column_says_where_each_session_is` |
| 16 | 批次 import 開頭的同步不節流 | ❌ **沒被抓到** | —（F4） |

這次之前的 review 已經做過、而且都被抓到的 mutation（這次沒有重做）：
- H1、R1～R4、M4（T3-sec5）；
- 刪除佇列的各種（T3-sec6、T3-sec7）；
- P1～P3（T3-sec2）；
- V1、V2（impl2 自己做的，T3-sec5 之後）。

## 要補的（依重要性）

| # | 內容 | 建議 |
|---|---|---|
| **F1**（Medium） | `.update` 記號沒有指令層級的測試 | 讓 `copy` 失敗（`FAKE_RCLONE_FAIL=copy`），然後分別做：`edit --header`、`continue`、import 原地更新、`recover_pending` 收尾，斷言 `outbox/<ULID>/.update` 都存在。新建的 import 和 merge 斷言**沒有**。這是 Q1（別台刪掉的不能被悄悄傳回去）唯一的守門 |
| **F2**（Medium） | push 等背景沒有測試；「每 10 秒說一次」是死碼（R7） | 決定 T3-size 的 E8：(a) 接受「阻塞地等」，spec 拿掉「每 10 秒說一次」；或 (b) 改成非阻塞、每 0.2 秒試一次、每 10 秒說一次。不論哪一種，都補一個測試：測試自己拿住 `upload.lock`，push 在另一個 thread 裡跑，斷言放開之前 push 沒有結束、放開之後 Drive 上有了 |
| F3（Low） | 「背景上傳中，N 筆」的說法 | 拿住鎖、在 outbox 放一筆，跑 `sync`，斷言 stderr 有這一句（反過來，沒人拿鎖時是「沒上傳成功」）。順便把 T3-sec7 X1（delete 時說「沒上傳成功」）一起處理 |
| F4（Low） | import 開頭同步的節流 | 在 5 分鐘內跑兩次 import，斷言第二次沒有列 Drive（`calls.log` 沒有 `lsjson`） |
| V3、V4（Low，spec 的 THEN） | N5 另存的 relation／parents；背景模式下的提醒 | 照 T3-sec5：relation 用這次寫入的種類，parents 是 X 原本的加上 X；提醒存成檔案，下一個指令開頭印出來一次。或者在 spec 裡寫明這兩點**不做**，由 PM 決定 |
| 其他 Low | 「outbox 有 N 筆未上傳」的提醒、「接續之後只留新的原始檔」用指令驅動、「剛匯入的」從未上傳變成 ✓ | 都有規則守著，可以之後再補 |

還沒解決的其他 Low（之前的 review，這次不在範圍內，impl2 正在做其中幾個）：
- T3-sec7 的 X1；
- T3-sec6 的 W4、W5；
- T3-sec3 的 R6（`uploader_is_running` 靠拿鎖來判斷，可能讓剛啟動的背景直接結束）。

## 結論

T3 的主要行為（本機完整、背景上傳、只刪驗過的版本、刪除先在本機、一批固定幾次 rclone、互動模式的說法）都有用指令或函式驅動的測試守著，16 個關鍵 MUST 裡有 12 個拿掉就會紅。

歸檔前**建議至少處理 F1 和 F2**：
- F1 是 Q1 的守門，卻沒有任何測試；
- F2 的 spec 寫的行為（每 10 秒說一次）根本不會發生，要嘛改程式、要嘛改 spec。

F3、F4、V3、V4 可以由 PM 決定，是補上，還是在 spec 寫明不做。
