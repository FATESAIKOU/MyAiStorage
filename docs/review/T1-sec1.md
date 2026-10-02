**沒有 High。** 2 個 Medium，都是 import 的 exit code 和 spec 不一致（S1-1、S1-2）；另外還有 2 個 Medium（S1-3、S1-4）。

# Review：openspec change command-batch-actions 第 1 節（批次指令）

2026-10-03，review。對象是 impl2 的 `8f570bd`（`src/agora/cli.py`、`tests/unit/test_cli.py`），對照 `openspec/changes/command-batch-actions/specs/batch-commands/spec.md`、這個 change 的 `design.md`，以及 `docs/review/T1.md` 的 Q6、Q8、Q11。只提意見，沒有改程式。依照指示，沒有跑整合測試、沒有碰 Drive，也沒有讀任何真實的 Session。工作目錄裡有別人還沒 commit 的 `cache.py`／`store.py`，這次不看。

## 結論

| # | 嚴重度 | 一句話 |
|---|---|---|
| S1-1 | Medium | import 多個時，exit code 是「**最後一個**非零的」（`worst = code or worst`），但 spec 寫的是「**第一個**非零的」。例如 `[3（存進 outbox）, 2（錯誤）]`，spec 要求回傳 3，程式回傳的是 2 |
| S1-2 | Medium | spec 寫明「只給一個 id 時，行為與 exit code 和以前相同」。但是 `_import_one` 丟出的 `InputError`（例如「沒有任何訊息，不匯入」）會被 `except Exception` 接住，變成 `EXIT_ERROR`（**2**）；以前它會一路傳到 `main`，exit 是 **1** |
| S1-3 | Medium | 「先同步一次」：`cmd_import` 開頭做了一次不節流的 `store.sync`，但 `_import_one` 裡**每一筆又各做一次**，所以 N 個 id 會同步 N+1 次（每次都要推 outbox、列遠端），和 spec 的意思（以及 U4）不一致，批次匯入也就沒有變快 |
| S1-4 | Medium | delete 重跑的強韌度：`delete_session` 對 Drive 上**已經不存在**的資料夾執行 `rclone purge`，會丟出 StoreError。如果在「purge 成功，但 `forget_local`／墓碑還沒寫」之間被中斷，重跑時，那個 id 還在索引裡，會被當成「還在」→ 再 purge 一次 → 失敗，exit 2，**這個 id 永遠刪不乾淨**。第 3 節的「雲端沒有的只刪本機」也會遇到同一條路徑 |
| S1-5～S1-9 | Low | 批次裡有打錯的 id 時，整批都不刪；delete 某一筆失敗時，後面的不會再做；merge 快取的鍵用的是提示詞的「版本號」，不是內容；Claude 的模型設定永遠是空的；merge 快取的暫存檔名是固定的 |

**做得好的部分**：stderr／stdout 的分工（Q11）、delete 只略過 `<state>/deleted` 裡的 id（Q8）、merge 沿用的鍵（agent、提示詞版本、模型設定、來源 id、實際送出的文字）以及沿用之前的 schema 驗證（Q6），都照 spec 做了，測試也大致都有鎖住。

## 1. import 多個：exit code 與失敗處理

- 多個 id：`--external-session-id` 改成 `action="append"`，每一個值再用逗號切開 ✅。
- 某一筆失敗，照樣做下一筆：`try: _import_one(...) except Exception` → 印出 `[agora] <id> 匯入失敗：…`（stderr），`code = EXIT_ERROR` ✅。`KeyboardInterrupt` **不是** `Exception`，所以會中斷整批，交給 `main` 回傳 130，這是對的（重跑時，已經做完的會因為「內容沒變」而略過）✅。
- **S1-1**：`worst = code or worst` 會讓**後面**的非零值蓋掉前面的。spec 要的是第一個，所以要改成 `worst = worst or code`。另外，docstring 寫的是「the worst of」，和 spec 的「第一個」也不一樣，要統一成同一種說法。
- **S1-2**：要讓只有一個 id 時的 exit code 和以前一樣，例外要依類型對應：`InputError` → 1、`HeaderError`／`StoreError`／`AgentError` → 2、其他 → 2；不要全部都變成 2。最簡單的做法是在 `except InputError` 裡設 `code = EXIT_INPUT`。
- **S1-3**：`_import_one` 改成接收 `index`，不要再自己呼叫 `store.sync`；每存一筆之後，`_save` 裡的 `store.remember` 本來就會更新本機的索引，所以後面的 `by_source` 照樣看得到前面剛匯入的那些。
- 重複的 id：沒有去重，但第二次會走「內容沒變」的路徑，只印出同一個 id，所以結果正確，只是多呼叫一次 export（Low）。

## 2. stderr／stdout 的分工 ✅（Q11）

- 新增 `_progress(word, k, total, item)`，一律印到 **stderr**：`匯入 k/N <id>`、`來源 k/N <agora id>`、`刪除 k/N`。
- stdout 只放結果：import 的 `_emit` 印出 agora id；delete 每刪掉一個，就 `print(agora_id)`；merge 最後印出新的 id。失敗與提示（「已經不在了」「找不到」「有子 Session」「要約沿用上次寫好的」）都在 stderr ✅。
- spec 的「管線不被進度弄髒」（`delete … | wc -l` 是 2）：`test_delete_progress_counts_down` 斷言 `len(out.split()) == 2`，而且「刪除 1/2」「刪除 2/2」都在 err 裡 ✅。

## 3. delete 只略過 `<state>/deleted` 裡的 ✅（Q8）

- 刪除成功之後，`_remember_deleted` 會把 ULID 追加到 `<state>/deleted`。重跑時，「索引裡沒有，但墓碑裡有」的會被歸到 `missing` → 印出「已經不在了，略過」（stderr），不算失敗；「索引裡沒有，墓碑裡也沒有」的歸到 `unknown` → `InputError`「找不到」，exit 1 ✅。測試：`test_delete_rerun_skips_what_it_deleted`、`test_delete_reports_an_id_it_never_had` ✅。
- 同一批裡有父也有子：迴圈每一輪先刪「沒有子 Session 的」，子刪掉之後（`forget_local` 會把它從索引拿掉），父在下一輪就變成可以刪的了 ✅（T1.md 的 Q9）。
- **S1-4**：建議 `delete_session` 遇到 rclone 回報「directory not found」時，當作「Drive 上已經沒有了」，照樣執行 `forget_local`，並且寫入墓碑。這樣中斷之後的重跑，和第 3 節「雲端沒有的只刪本機」這兩種情況就都能收尾。
- S1-5（Low）：批次裡只要有一個 `unknown`，**整批都不刪**（在 `--yes` 之前就丟出 InputError）。這比較安全，也可以接受，但 spec 沒有寫，要在 design 或 README 補一句：「有找不到的 id 時，什麼都不刪」。
- S1-6（Low）：`delete_session` 丟出例外時（例如網路斷了），這一筆之後的就都不會再做（例外直接傳到 `main`）。重跑時，墓碑會讓已經做完的被略過，所以**可以**接著做，算是可以接受；不過訊息裡最好說明「已經刪了 k 個，重跑會接著做」。
- 墓碑檔只會一直追加、從來不清理（Low，檔案很小）。

## 4. merge 沿用的鍵與 schema 驗證 ✅（Q6）

- 鍵：`md5("\n".join([agent, SUMMARY_PROMPT_VERSION, AGORA_<AGENT>_MODEL, agora_id, 實際送出的文字（截斷之後）]))` ✅，符合 spec 的四個條件（同一個 agent、同一份提示詞、同一個模型設定、同一段實際送出的文字）。來源改過（continue 會寫回同一個 id）→ 文字變了 → 鍵也變了，就會重寫 ✅（`test_merge_rewrites_when_the_source_changed`）。
- 沿用之前，會用 `SECTION_SCHEMA` 再驗證一次；不合格就當作沒有快取（`test_merge_cache_refuses_a_broken_summary`）✅。快取裡也存了當時的 `model`，沿用時會加進 `models`，所以 `generated.by` 是正確的 ✅。換了模型設定就重寫（`test_merge_cache_notices_a_different_model`）✅。
- S1-7（Low）：鍵用的是提示詞的**版本號**，不是提示詞的內容。如果有人改了 `SUMMARY_PROMPT`，卻忘了把版本號加 1，就會沿用舊提示詞寫的要約。建議鍵改用 `SUMMARY_PROMPT` 本身（或者它的雜湊）。
- S1-8（Low）：`_model_setting("claude")` 讀的是 `AGORA_CLAUDE_MODEL`，但目前的程式裡**沒有**這個設定（v6 Z3），所以它永遠是空字串。使用者 Claude 的預設模型換了，也會沿用舊的要約。在 Claude 的模型可以指定之前，可以接受。
- S1-9（Low）：`_cache_section` 的暫存檔名是固定的 `<key>.json.tmp`。兩個 merge 同時處理同一個來源時，會遇到 K4 那樣的競態。建議用 `tempfile.mkstemp`（和 `9172041` 的做法一樣）。

## 5. 測試有沒有鎖住每個 scenario

| spec 的 scenario | 測試 | 結果 |
|---|---|---|
| import：其中一個失敗（另外兩個被匯入，stderr 說明失敗的那個，exit 非零） | `test_import_keeps_going_after_one_fails` | ✅（但只斷言 `code != 0`，「第一個非零」的**順序**沒有被鎖住，見 S1-1） |
| import：中斷後重跑 | `test_import_rerun_skips_what_is_done` | ⚠️ 測的是「全部跑完之後再跑一次」，不是「在第二個被中斷」。語意上算是涵蓋到了（第一個因為內容沒變而略過），可以接受；要更貼近 scenario 的話，可以在第二個的 export 丟出 `KeyboardInterrupt`，再重跑 |
| 進度：管線不被進度弄髒 | `test_delete_progress_counts_down`、`test_import_progress_stays_off_stdout` | ✅ |
| delete：中斷後重跑（已刪的被略過，第三個被刪，exit 0） | `test_delete_rerun_skips_what_it_deleted` | ✅（兩個 id 的版本；語意相同） |
| delete：打錯的 id → exit 1，找不到 | `test_delete_reports_an_id_it_never_had` | ✅ |
| merge：在第二個來源被中斷，重跑只為第二個叫一次 AI | `test_merge_reuses_a_summary_it_already_paid_for` | ⚠️ 測的是「兩個都寫完之後，再跑一次，呼叫次數沒有增加」。沒有測「第一個寫完、第二個中斷」這個情況，也沒有斷言「只為第二個叫一次」。建議：讓假的 agent 在第二個來源時丟出 `KeyboardInterrupt`，重跑之後，斷言 prompt 只多了一個 |
| merge：來源改過 → 第一個的要約重寫 | `test_merge_rewrites_when_the_source_changed` | ✅ |
| （spec 正文）只給一個 id 時，exit code 和以前相同 | **沒有測試** | ❌ S1-2 |

## 這次讀過、跑過的東西

`git show 8f570bd -- src/agora/cli.py`（全文）、`git show 8f570bd -- tests/unit/test_cli.py` 的測試名稱與四個重點測試的內容、batch-commands/spec.md、這個 change 的 design.md、`store.delete_session`。沒有跑任何測試（工作目錄裡有別人還沒 commit 的修改），沒有碰 Drive，沒有叫任何 agent，也沒有讀任何真實的 Session。

---

## 修正確認（`7b77f01`、`a5d31ac`）

**沒有新的 High。** S1-1～S1-3 都修對了，S1-9 也順便修好了；S1-4 修了，但比對錯誤訊息的條件**太寬**，帶進了一個新的 Medium（S1-4b）。單元測試 **351 passed**。

| # | 狀態 | 確認的內容 |
|---|---|---|
| S1-1 | ✅ | `first_bad = first_bad or code`，所以第一個非零的會被保留下來（`test_import_exit_code_is_the_first_non_zero`） |
| S1-2 | ✅ | `except InputError` → `EXIT_INPUT`（1），其他例外仍然是 2，所以只給一個 id 時，exit code 和以前一樣（`test_import_one_id_keeps_the_old_exit_code`）。`KeyboardInterrupt` 仍然會中斷整批，交給 `main` 回傳 130 ✅ |
| S1-3 | ✅ | `cmd_import` 只同步一次，把 index 傳進 `_import_one`，`_import_one` 不再自己同步（`test_import_batch_syncs_once`）。`_save` → `remember` 用另一個連線寫進同一個 sqlite，所以後面的 `by_source` 照樣看得到前面剛匯入的 ✅ |
| S1-4 | ⚠️ 修了，但有 S1-4b | `delete_session` 遇到 purge 失敗、而且訊息裡有 `not found` 時，就當作「Drive 上已經沒有了」，照樣 `forget_local` 並寫入墓碑；假 rclone 也改成回報和 rclone 一樣的訊息（`test_deleting_a_session_drive_already_lost_still_finishes`）。中斷之後的重跑可以收尾了 ✅ |
| S1-6 | ✅ | 有子 Session 而刪不掉的時候，訊息會說「重跑會接著做剩下的 N 個」 |
| S1-9 | ✅ | merge 快取改用 `tempfile.mkstemp` 產生唯一的暫存檔名，失敗時會刪掉暫存檔 |

### S1-4b（Medium）：「not found」這個條件太寬

`"not found" in str(e).lower()` 不只會比對到 rclone 的 `directory not found`（資料夾真的不在了），**也會**比對到 Google Drive API 的 404。例如，`config.json` 裡記的 folder ID 是錯的（被刪掉了、換了 OAuth client，或者寫成了別的資料夾），這時 rclone 回報的是 `couldn't find root directory ID: … Error 404: File not found …, notFound`。結果：**每一個** delete 都會被當成「Drive 上已經沒有了」，本機就把 Session 忘掉、寫進墓碑、回報成功，但 Drive 上的資料夾根本沒有被移到垃圾桶。之後只要列檔恢復正常，它就又回來了（R6 會保留它，並標成雲端有），使用者會以為刪除失效了。

**建議**：(a) 只比對 `directory not found`（rclone 對「這個資料夾不存在」的固定說法），不要比對一般的 `not found`；(b) 更穩的做法是：purge 失敗時，再呼叫一次 `drive.list_sessions()`，**列檔成功、而且裡面沒有這個 ULID**，才當成已經刪掉；列檔也失敗，就照原本的錯誤處理（exit 2，什麼都不忘掉）。單元測試：讓假 rclone 對 purge 回報 `Error 404: File not found`（模擬 root ID 錯誤），斷言 delete 失敗，本機的索引和鏡像都還在，也沒有寫墓碑。

跑過的指令：`git show 7b77f01 a5d31ac -- src tests`（src 的 diff 與新測試的名稱）；`.venv/bin/python -m pytest -q tests/unit` → 351 passed。沒有跑整合測試，沒有碰 Drive，沒有讀任何真實的 Session，也沒有執行不帶參數的 `agora`。
