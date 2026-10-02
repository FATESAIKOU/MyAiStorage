**沒有 High。** 但歸檔前有 3 件事要先處理：
- **(1)** session-sync spec 對 import 自相矛盾：一處寫「import 的更新路徑 MUST 拒絕（exit 1）」，另一處寫「MUST 建一個新的 Session」。程式做的是後者。spec 一歸檔就成了正式規格，要先改掉。
- **(2)** 兩個 spec 的 MUST 沒有任何測試守著（mutation 拿掉之後，測試照樣全過）：「雲端沒有的不算子 Session」和「接續中的不被標記」。
- **(3)** 程式碼行數 **3,285**，超過 2,900，task 4.3 還沒打勾，要使用者決定。

# Review：change command-batch-actions 歸檔前的規格對照

2026-10-03，review。對象是 HEAD `7c19f85`，程式在 `35ab469` 之後就沒有再改過（工作目錄裡 `tui.py`／`test_tui.py` 有 T2 還沒 commit 的改動，不在這次的範圍）。

逐條對照 `specs/session-sync/spec.md`（5 個 Requirement、13 個 Scenario）、`specs/batch-commands/spec.md`（4 個 Requirement、7 個 Scenario）、這個 change 的 `design.md`、`proposal.md`、`tasks.md`、`docs/design.md`（5.4、5.6、5.10）和 README。

在 `git archive HEAD` 取出的副本跑單元測試：**394 passed**。對 spec 的條文做了 14 個 mutation，每次只拿掉一條的實作，跑 cli、cli_more、cache、store、store_more 的測試，看有沒有測試會失敗。再加上 T1-sec3 三輪確認裡已經做過的 mutation 結果。沒有跑整合測試（impl2 正在跑），沒有碰 Drive，也沒有讀任何真實的 Session。

標記：**✅** 有實作，有測試，而且 mutation 證實測試抓得到。**✅\*** 有實作，有測試，但沒有做 mutation 驗證（看過測試的內容，判斷是有效的）。**⚠️** 有實作，但沒有測試，或者測試抓不到。**❌** 沒有實作，或者和 spec 不符。

## batch-commands

| Requirement／Scenario | 實作 | 測試 | 狀態 |
|---|---|---|---|
| import：`--external-session-id` 可以給多次，也可以逗號分隔 | `cmd_import` | `test_import_several_ids_at_once`（兩種寫法都有） | ✅\* |
| import：先同步一次 | `store.sync(paths)` 一次 | `test_import_batch_syncs_once` | ✅\* |
| import：失敗照樣做下一個；exit code 是第一個非零 | | `test_import_keeps_going_after_one_fails`、`test_import_exit_code_is_the_first_non_zero` | ✅\* |
| import：只給一個 id 時和以前一樣 | | `test_import_one_id_keeps_the_old_exit_code` | ✅\* |
| Scenario 其中一個失敗 | | 同上 | ✅\* |
| Scenario 中斷後重跑（內容沒變就略過） | `_import_one` 的 unchanged 判斷 | `test_import_rerun_skips_what_is_done` | ✅\* |
| 進度：import、delete、merge、pull、push 在 stderr 印 `k/N` | `store.progress` | import／delete／merge（`來源 1/2`）／pull／push 各有斷言 | ✅\* |
| Scenario 管線不被進度弄髒（`delete A B --yes \| wc -l` = 2） | | `test_delete_progress_counts_down` 斷言 stdout 剛好兩行 | ✅\* |
| delete：自己刪過的略過，不算失敗 | `<state>/deleted` | `test_delete_rerun_skips_what_it_deleted` | ✅（mutation：拿掉 `_remember_deleted` 就失敗） |
| Scenario 打錯的 id → exit 1，找不到 | | `test_delete_reports_an_id_it_never_had` | ✅\* |
| merge：寫好的要約先存起來，重跑時沿用 | `_cache_section`／`_cached_section` | `test_merge_reuses_a_summary_it_already_paid_for` | ✅\* |
| merge：沿用前再驗一次 schema | | `test_merge_cache_refuses_a_broken_summary` | ✅（mutation 抓得到） |
| merge：鍵含 **agent** | `_section_key` | **沒有** | ⚠️（mutation：拿掉 agent 之後，測試全過） |
| merge：鍵含**提示詞版本** | 同上 | **沒有** | ⚠️（mutation：拿掉版本之後，測試全過） |
| merge：鍵含模型設定 | 同上 | `test_merge_cache_notices_a_different_model` | ✅\* |
| Scenario 來源改過 → 重寫 | 鍵含實際送出的文字 | `test_merge_rewrites_when_the_source_changed` | ✅\* |

## session-sync

| Requirement／Scenario | 實作 | 測試 | 狀態 |
|---|---|---|---|
| pull 只處理給的 id；不給 id → exit 1 | `_ids_of` | `test_pull_and_push_need_ids` | ✅\*（見文件 D6：Scenario 寫的是 `agora pull`） |
| 沒有前綴或 `agora:`：拿下 session.md 與原始檔 | `_pull_agora` | `test_pull_brings_one_session_down…`、`test_pull_takes_a_bare_ulid…` | ✅\* |
| `opencode:`／`claude:`：寫全文快取 | `local_reading` | `test_pull_of_an_agent_session_caches_its_full_text` | ✅\* |
| `ses_…`／uuid 沒有前綴就報錯 | `_split` | `test_pull_of_a_bare_agent_id_says_which_prefix_to_write` | ✅\* |
| 沒有過時的略過 | | agent：`…skips_one_that_is_not_stale`；agora：`…already_fresh_asks_rclone_for_nothing` | ✅\* |
| push：先送 outbox | `cache.push` | `test_push_sends_the_outbox_first` | ✅（mutation 抓得到） |
| push：session.md 與 `agora.raw.file`，同名覆蓋 | `_upload_checked` | `test_push_overwrites_the_copy_on_drive` | ✅\* |
| push：本機沒有原始檔就只傳 session.md | | `test_push_sends_only_session_md_when_the_raw_is_not_here` | ✅\* |
| push：不傳舊的原始檔、`*.partial`、`.` 開頭的檔 | 只傳兩個檔 | `test_push_sends_session_md_and_the_raw_it_names_and_nothing_else` | ✅\* |
| push：Drive 上多的不刪 | mirror 那條路徑不刪 | — | ⚠️ 見 D5：outbox 那條路徑**會**刪舊的 `raw-*` |
| 標記：只有列檔完整成功時才更新 | `sync` | `test_deleted_remote_session_stays_and_is_marked` | ✅\* |
| 標記：不在 outbox 的才標 | `u not in staged` | 兩個測試都在，但這是**等價的 mutant**（`_index_outbox` 會再把標記清掉，T1-sec3 H2） | ✅（對外的行為守住了） |
| 標記：**不在接續中的**才標 | `not continuing(paths, u)` | **沒有** | ⚠️（mutation：拿掉之後，測試全過） |
| 列檔失敗、離線、沒有 `sessions/` → 不新增也不清除 | | `test_a_mark_that_is_already_there_survives_a_broken_listing`、`test_missing_sessions_dir_keeps_mirror` | ✅\* |
| Scenario 又出現了 → 清除標記 | `mark_missing` 整個替換 | `test_not_exist_upload_sends_the_session_back`（傳回去之後同步，標記就清掉） | ✅\* |
| 還在 outbox 的顯示「未上傳」 | `cmd_search` | `test_upload_failure_exits_3_and_stays_searchable` | ✅\* |
| 索引版本不同就重建（design） | `Index.__init__` | `test_a_bumped_index_is_filled_from_the_mirror…` | ✅（mutation 抓得到） |
| `--not-exist-delete` 刪掉本機副本 | `forget_local` | `test_not_exist_delete_drops_the_local_copy` | ✅\* |
| outbox、接續中的不刪，並印出原因 | | `…keeps_what_has_not_been_uploaded`、`…keeps_a_session_being_continued` | ✅（G1 的 mutation 抓得到） |
| `--not-exist-upload` 傳回去；本機沒有原始檔就拒絕 | `need_raw=True` | `test_not_exist_upload_sends_the_session_back`、`…refuses_when_the_raw_is_not_here` | ✅（mutation 抓得到） |
| 沒加 flag 時只印一行、不動 | | pull：`…the_cloud_does_not_have_changes_nothing`；push：`test_push_does_not_revive…` | ✅\* |
| agent 的 id：`--not-exist-delete` 是刪全文快取 | M2、F5 | 三個測試（真的沒了、還在、清單讀不到） | ✅（F5 的 mutation 抓得到） |
| continue、edit 拒絕（exit 1），提示兩個選擇，agent 不會被打開 | `_refuse_if_gone`（標記加上當下的 Drive） | `test_continue_refuses…`、`test_edit_refuses…`、`…even_offline` | ✅（F1 的 mutation 抓得到） |
| import 的更新路徑 → **spec 寫拒絕（exit 1）** | 程式是**改建一個新的 Session**（exit 0） | `test_importing_the_same_source_again_makes_a_new_session` | ❌ **spec 自相矛盾**，見 D1 |
| 互動模式裡的對應動作 | T2：互動模式用子程序跑同樣的指令 | 屬於 T2 | —（T2 的範圍） |
| search、show 標「雲端沒有」 | | `test_search_marks…`、`test_show_says…` | ✅（mutation 抓得到） |
| `--filter cloud=no`／`cloud=yes` | `Index.search` | `test_search_can_ask_for_it`（兩個都有） | ✅\*（值沒有驗證：`cloud=maybe` 會被當成 yes，T1-sec3 L3，還沒修） |
| merge 不接受雲端沒有的來源 | `_need_in_cloud` | `test_merge_refuses_a_source_the_cloud_lost` | ✅（mutation 抓得到） |
| delete 雲端沒有的只刪本機（exit 0） | | `test_delete_of_one_the_cloud_lost_removes_only_the_local_copy` | ✅（mutation 抓得到） |
| **不算成別人的子 Session**（不擋刪除、不觸發 import 的分岔） | `children()` 裡的 `cloud_has(u)` | **沒有** | ⚠️（mutation：拿掉之後，測試全過） |
| import 的來源對到雲端沒有的 → 建一個新的 | `by_source` 排除 | 同上 | ✅（mutation 抓得到） |
| Scenario 接續被別台刪掉的 | | `test_continue_refuses_a_session_the_cloud_lost` | ✅ |
| Scenario 接續到一半被別台刪掉的 → 另存 Y | `_finish` | `test_a_continue_whose_session_vanished…` | ✅\*；**離線收尾只靠標記**的那條路還沒有測試（T1-sec3 H1，實測拿掉那一行 X 就會復活） |
| Scenario 用管線清掉 | | `test_search_can_ask_for_it` | ✅\* |

**合計**：spec 的條文全部都有實作，沒有「沒實作」的。「有實作但沒測試，或測試抓不到」的有 5 條：子 Session 的排除、接續中的不標記、merge 的鍵含 agent、merge 的鍵含提示詞版本，以及 H1 的離線收尾。另外有 1 條是 spec 自己矛盾（D1）。

## 文件和程式不一致的地方

| # | 嚴重度 | 位置 | 不一致 | 建議 |
|---|---|---|---|---|
| D1 | **Medium（擋歸檔）** | `specs/session-sync/spec.md`「其他指令遇到雲端沒有的 Session」 | 第一句：「會寫回既有 id 的指令（continue、edit、**import 的更新路徑**…）MUST 拒絕（exit 1）」；同一段後面：「import 的來源對到雲端沒有的那一筆時 MUST 建一個新的 Session」。程式和 design 5.10（7029b68 已經修過）都是後者 | 把第一句的「import 的更新路徑」拿掉，改成「import 不會更新它，改建一個新的」。spec 一歸檔就進了 `openspec/specs/`，成為以後的依據 |
| D2 | Medium | README | 完全沒有提到「雲端沒有」、`--not-exist-delete`、`--not-exist-upload`、`--filter cloud=…`（grep 沒有任何結果）；可是 tasks 4.1 寫著「README 更新」已經完成 | 在 pull／push 那一段補上兩個 flag 和 `search --filter cloud=no \| … pull --not-exist-delete` 的用法 |
| D3 | Low～Medium | change 的 `design.md` Decisions「寫回既有 id 前檢查：continue、edit、import 的更新路徑在寫之前**看標記**」，以及 Risks「判斷雲端沒有**只用這次的列檔結果，不額外呼叫**」 | 後來的決定（M1、F1）是：標記**加上**當下的 Drive（continue／edit 開始前、continue 結束時、edit 存檔前，各做一次完整列檔）；import 改成建新的；中途被刪就另存 Y | 照 3.7～3.9 的決定改寫這兩處；design.md 會跟著一起歸檔 |
| D4 | Low | `docs/design.md` 5.10「接續中（pending）的也不算」、5.6 delete | G1 之後，「接續中」指的是**有人拿著鎖**的；留下來的記錄不算接續中，不擋標記，也不擋刪除。5.6 沒有寫「正在接續的拒絕刪除；留下來的記錄照刪並提示」，也沒有寫 S1-4／S1-4b（Drive 上已經沒有的當成刪掉；列檔也失敗就不算）。5.10 也沒有寫 G3（沒有 `sessions/` 時，`--not-exist-delete` 拒絕）和 F5（agent 的清單讀不到時不刪快取） | 補上 |
| D5 | Low | spec「push：Drive 上多的不刪」和 outbox 那條路徑 | `push session X` 時，如果 X 在 outbox 裡，走的是 `push_one`，它**會刪掉** Drive 上不是現在那個的 `raw-*`（N8，換原始檔的時候需要這樣做）。spec 這句話只對 mirror 那條路徑成立 | spec 改成「同名覆蓋；送 outbox 時會清掉被取代的舊原始檔，其他多的不刪」 |
| D6 | Low | spec Scenario「沒給 id：執行 `agora pull`」 | 照字面執行 `agora pull` 的話，得到的是「pull 的型態要寫 session」（也是 exit 1），而不是「要給 session id」；測試用的是 `pull session` | Scenario 改成 `agora pull session` |
| D7 | Low | spec「進度」的例子 `[agora] 來源 1/3：…` | 實際的格式是 `[agora] 來源 1/3  agora:…`（兩個空白，沒有冒號）。T2 的進度解析就是依實際的格式寫的 | 例子照實際的格式改 |
| D8 | Low | tasks 4.2b「S1（`store.mirror_one`，**sync 與 pull 的 G3 只有一份**）」 | `sync` 還是用它自己那一份，沒有呼叫 `mirror_one`（只有 pull 用），所以 G3 的邏輯還是有兩份 | 改正 tasks 的敘述，或者讓 sync 也改用 `mirror_one` |
| D9 | Low（T2） | `docs/design.md` 5.10 最後一行「互動模式：`r`…、`s`…」 | T2 已經拿掉 `r`、`s`（5.9 在 4262e30 已經對齊了） | 交給 T2 的 4.x 一起改 |
| D10 | **要使用者決定** | proposal「程式碼行數目標 2,900 行」、tasks 4.3 | HEAD 是 **3,285**（cache 180、cli 735、store 476、tui 692、opencode 538、claude 442、header 169、base 63，算法同 T1-size） | 歸檔之前，請使用者決定要放寬額度，還是接受目前的行數（見 T1-size、adapters-size） |

## 之前的 review 還沒處理的（不擋歸檔，但要記下來）

| 來源 | 項目 |
|---|---|
| T1-sec3 H1 | 離線收尾時只靠 `_finish` 裡的標記判斷，沒有測試（實測拿掉它 X 會復活）。**建議歸檔前補上**，和上面那 2 條 ⚠️ 的 MUST 一起 |
| T1-sec3 H2、H3 | F6 的第二個測試 docstring 寫錯了；F8 的測試在 edit 開始**之前**就刪了 |
| T1-sec3 H4、H5、H6（G2） | `continuing()` 的 FileNotFoundError 競態；刪掉之後留下的記錄還會一直重試；一筆在接續中，整批 delete 都不刪 |
| T1-sec3 F9～F11 | F10（另存 Y 時當掉，會多出一份）、F11（結束時剛好離線，推 outbox 時沒有再檢查） |
| T1-sec3 L3 | `--filter cloud=` 的值沒有驗證 |

## 建議的歸檔條件

1. 修 D1（spec 的矛盾），順便改 D3、D5、D6、D7 這幾處文字。
2. 補 3 個測試：子 Session 的排除、接續中（拿著鎖）的不標記、H1 的離線收尾；可以的話，merge 的鍵含 agent 和提示詞版本也各補一個。
3. 補 README（D2）。
4. 行數由使用者決定（D10），然後才勾 4.3。

## 這次讀過、跑過的東西

兩份 spec 的全文、change 的 proposal／design／tasks、`docs/design.md` 5.6 和 5.10、README（用 grep 查）；HEAD 的 `tests/unit` 裡 6 個檔案、每一個測試的名稱和 docstring（213 個），並抽看了和 spec 對應的那些測試的內容。14 個 mutation 的結果：抓到 10 個，沒抓到 4 個（子 Session、接續中的標記、merge 的鍵含 agent、merge 的鍵含提示詞版本）。單元測試 394 passed。沒有跑整合測試，沒有碰 Drive，也沒有讀任何真實的 Session。
