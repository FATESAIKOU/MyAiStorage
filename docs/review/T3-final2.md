> **審到 HEAD `5faac91`**（impl2 的精簡與修正），以及在它前面的 `c8effd1`（impl1 的 F1～F4 測試）。W5 是在更早的 `598981a`（impl1）做的。

**沒有 High。** 精簡沒有看到行為上的退步，修正大部分照 spec 做了：
- E1 讓 push 也有 H1 的保護（mutation 抓得到）；
- V4 的提醒會在下一個指令說出來；
- W4 是 exit 3；
- W5 有做（在 `598981a`），只是 impl2 的 commit 訊息沒提到；
- F1～F4 的測試都抓得到各自的 mutation。

但有一個 Medium（G1）：E1 改成讓 push 對「晚到的那一筆」跑一整輪 `upload_batch`，而這一輪是在**放開上傳鎖之後**跑的。探測確認，那一刻沒有人拿著鎖，所以它可能和背景同時對同一個 outbox 上傳，違反「同一時間只有一個上傳」。

另外 V3 只做對了一半（G2）：continue 之後另存出來的 Y，relation 還是 `import`。

**行數：3,759**（`c8effd1` 是 3,800，這次少了 41 行），在目標 3,800 以內。

# Review：T3 最後一輪（`c8effd1`、`5faac91`，以及 `598981a` 的 W5）

2026-10-03，review。
- 在 `git archive 5faac91` 的副本裡跑：`compileall` 通過，單元測試 **528 passed**。
- 探測和 mutation 都用 pytest 跑（有 conftest 的隔離），子程序另外傳了 `/tmp` 底下的 `AGORA_CONFIG`／`AGORA_CACHE_DIR`／`AGORA_STATE_DIR`／`HOME`。
- 沒有跑整合測試，沒有碰 Drive，也沒有讀任何真實的 Session。

## 行數（算法同 T1-size：不含空行、註解、docstring）

| 檔案 | `c8effd1` | HEAD `5faac91` | 差 |
|---|---:|---:|---:|
| store | 679 | 658 | −21 |
| background | 94 | 72 | −22 |
| cache | 203 | 199 | −4 |
| cli | 750 | 756 | +6（W4、V4 的提醒） |
| tui | 898 | 898 | 0 |
| **src 合計** | **3,800** | **3,759** | **−41** |

T3-size 估的是精簡可以省約 60 行。這次同時加了 V3、V4、W4、X1、R7 的程式，所以淨減 41 行，合理。E3（`_restore_done` 只在背景做）和 E8 沒有列在這次裡；E8 實際上選了 (b)：非阻塞、每 10 秒說一次。

## 逐項確認

| 項目 | 結果 |
|---|---|
| **E1**：push 不再用沒有 H1 保護的 `push_one` | ✅ 晚到的那一筆改走 `upload_batch`，也就是 `.done-` 改名、再比對 md5，Drive 沒確認的就留在 outbox。mutation「不看 `upload_batch` 的結果」被 `test_a_late_session_drive_does_not_confirm_stays_in_the_outbox` 抓到。⚠️ 但是**沒拿鎖**，見 G1 |
| E2：鎖只有一套（`hold_upload_lock` 回傳打開的檔案，關掉就放開） | ✅ 行為不變。mutation「`uploader_is_running` 不放開鎖」「背景做完不放開鎖」：測試**卡住、300 秒逾時**，沒有任何斷言失敗。也就是說，只有在 CI 有逾時的情況下才算抓得到，見 G5 |
| E4：`--files-from` 只剩一份（`_with_files_from`） | ✅ mutation「清單不清掉」被 `test_mirror_holds_only_session_md_and_one_listing` 抓到 |
| E5：列檔失敗回傳 error，只剩一份 | ✅ mutation「改回傳 None」被 `test_pull_marks_every_agora_id_failed_when_drive_is_gone` 抓到 |
| E6：`_entry_md5s` 一起回傳原始檔的名字，拿掉 `_raw_of` | ✅ 讀不了的情況還在同一個 `try` 裡（R1 的「一筆驗不了不中斷整輪」照舊） |
| E7：`_rename_back`、`_restore_done` 合成 `_put_back` | ⚠️ 有一點小的行為改變，見 G6。mutation 沒被抓到（這一點本來就很難測） |
| E9：記錄檔超過 1 MB 就整個清掉 | ✅ mutation 被 `test_a_log_over_a_megabyte_starts_again` 抓到 |
| E10：拿掉只用一兩次的小函式 | ✅ |
| **X1**：delete 時不說「沒上傳成功」 | ✅ `sync(kick=False)` 時不說這一句。mutation 被 `test_a_sync_that_starts_the_uploader_itself_calls_nothing_a_failure` 抓到 |
| **W4**：背景啟動失敗時 delete exit 3 | ✅ `[agora] 背景上傳啟動失敗，移到 Drive 垃圾桶要等之後的指令`，exit 3。mutation 被 `test_delete_says_exit_3_when_the_uploader_could_not_start` 抓到。小地方見 G4 |
| **V3**：Y 的 parents 和 relation | parents ✅：X 原本的 parents 加上 X（mutation 抓得到）。relation ⚠️：見 G2 |
| **V4**：下一個指令說「X 已被別台刪除，這次的修改存成了 Y」 | ✅ 背景把這句寫進 `<state>/notices`，下一個指令開頭印一次，印完就清掉。兩個 mutation（不寫、不印）都被 `test_the_next_command_says_where_the_rescued_edit_went` 抓到。小地方見 G3 |
| **R7**：push 等鎖時每 10 秒說一次 | ✅ 改成非阻塞地試、每 0.2 秒一次，所以這一句真的會說出來了。⚠️ 但是沒有測試：mutation「改回阻塞地等」沒被抓到（G5） |
| **W5**：勾的全部是雲端沒有的，結果視窗不說「背景移到垃圾桶」 | ✅ **有做，在 `598981a`（impl1）**：只有子程序的輸出真的有「背景移到 Drive 垃圾桶」時才這樣說，不然只說「已從本機刪除」。mutation 被 `test_the_result_window_does_not_promise_a_background_delete_that_is_not_queued` 抓到。impl2 的 `5faac91` 訊息沒提到它，是因為不是他做的 |
| **F1～F4**（`c8effd1`） | ✅ 都抓得到：F1「前景不放 `.update`」（edit、continue、import 原地更新三個測試）、F2「push 不等鎖」、F3「兩種情況說同一句」、F4「import 的同步不節流」 |

## G1（Medium）：E1 讓 push 在沒拿鎖的情況下跑一整輪 `upload_batch`

`cache.push` 的鎖只包住開頭那一段：

```python
with held:
    staged = store.outbox_ulids(paths)
    left = store.push_outbox(drive, paths)
...
for k, agora_id in enumerate(wanted, 1):
    ...
    elif (paths.outbox / ulid).is_dir():
        if ulid in store.upload_batch(drive, paths):      # ← 鎖已經放開了
```

實測（pytest 裡的探測，用現有的 `_stage_after_push_looked`，讓一筆在 push 拍快照之後才 stage）：把 `upload_batch` 包起來，每次被呼叫時，試著用非阻塞的方式拿一下鎖：

```
push 回傳 (1, 0)；upload_batch 被呼叫時鎖是不是空著的：[False, True]
```

- 第一次（`push_outbox`，在 `with held` 裡面）：鎖被 push 自己拿著 ✅；
- 第二次（晚到的那一筆）：**鎖是空的**，誰都沒拿。

以前的 `push_one` 也是在鎖外面跑，但它只碰那一個資料夾。現在的 `upload_batch` 會處理**整個 outbox**：
- 改名成 `.done-`、另存 N5、刪掉舊的原始檔……
- 而「晚到的那一筆」通常就是另一個指令剛 stage 的，那個指令會同時啟動背景。

所以這條路正好就是「push 和背景同時處理同一個 outbox」。`.done-` 的改名讓兩邊不至於刪錯同一個版本，但：
- 兩邊會重複上傳；
- 兩邊的 `--files-from` 清單都放在 `paths.state/.files-from-<N>.txt`，筆數一樣時是**同一個檔名**，會互相覆蓋、互相刪掉；
- spec 的「同一時間 MUST 只有一個上傳，不論前景或背景」不成立。

**建議**：晚到的那一筆也在鎖裡面送。例如在 `with held:` 裡，`push_outbox` 之後再看一次，`wanted` 裡有沒有剛出現在 outbox 的，有的話在同一把鎖裡再跑一次 `upload_batch`；或者在那個分支重新拿一次鎖（照樣非阻塞地等）。

加一個測試：在 `upload_batch` 裡斷言 `store.uploader_is_running(paths)` 是 True（有人拿著鎖）。順便建議 `.files-from-*.txt` 的檔名加上 pid，就算以後又有兩個同時跑，也不會互相蓋掉。

## G2（Low～Medium，要 PM 決定）：V3 的 relation

impl2 的做法是「Y 沿用 X 標頭裡現在的 relation」，測試用的是一個 relation 被手動設成 `merge` 的 X。

實測（pytest 裡的探測，指令驅動）：
1. import X；
2. 讓 `copy` 失敗，然後分別做 **continue**、**edit**；
3. 刪掉 Drive 上的 X，跑一輪上傳。

| 寫入 | Y 的 relation |
|---|---|
| continue | **`import`** |
| edit | **`import`** |

原因：continue 寫回同一個 id 時，標頭的 relation 不會變（只有 merge 會被改成 continue），所以 X 的 relation 一直是 `import`。

- spec 寫的是「relation 照原本的寫入（**continue 或 edit**）」；
- `docs/design.md` 裡，同一類的另存（接續到一半被別台刪掉，session-sync）用的是 `relation: continue`；
- 但是 `edit` 不是一個合法的 relation（design 只有 `import | continue | merge`）。

**建議**由 PM 決定其中一種：
- (a) continue 另存的 Y 用 `continue`，edit 另存的沿用 X 的 relation，並且把 spec 的「continue 或 edit」改成這樣的說法；
- (b) 接受現在的「一律沿用 X 的」，改 spec 那一句。

不論哪一種，都補一個用 `continue` 指令驅動的測試。

## G3（Low）：V4 的提醒，在前景跑的時候會說兩次

`_rescue_deleted` 會寫 notice，**也會**用 `say` 印出來。在背景裡，`say` 寫進 `upload.log`，使用者只會在下一個指令看到一次 ✅。

但 `upload_batch` 也會在前景跑：`push`（包含 G1 那條路），以及 inline 模式。這時候使用者會**當下**看到一次，下一個指令又看到一次。探測也看到同一句出現兩次。

**建議**：前景（`say` 是終端機的時候）不要另外寫 notice；或者 `say` 那一句拿掉，一律靠 notice。

## G4（Low）：W4 的兩個小地方

- 互動模式：delete exit 3 的時候，結果視窗用的是共用的 exit 3 說法「已存進 outbox，之後的指令會自動再送」。對 delete 來說不對，應該說「已從本機刪除；移到 Drive 垃圾桶要等之後的指令」；
- 指令模式：這次 delete 沒有任何東西進佇列，只是 outbox 裡有其他東西在等，而背景啟動失敗的時候，訊息還是說「移到 Drive 垃圾桶要等之後的指令」。

## G5（Low）：兩種退步沒有斷言守著

- **R7**：把 push 改回阻塞地等（「每 10 秒說一次」又變成死碼），沒有測試會紅。F2 的測試刻意不看這一句。
  - 建議：把「10 秒」做成可以調的值（例如模組常數），測試裡調成 0.3 秒，拿著鎖等 1 秒，斷言 stderr 有「還在等」。
- **E2**：鎖沒放開時，測試不會失敗，而是**卡住**（push 一直在等）。
  - 建議：加 `pytest-timeout`（或在 conftest 給單元測試設一個全域逾時），讓「卡住」變成「失敗」；
  - 或者加一個斷言：`uploader_is_running` 之後，另一個 fd 拿得到鎖。

## G6（Low）：`_put_back` 合併之後，`_restore_done` 的行為變了一點

- 以前的 `_restore_done`：`.done-X` 改名回 `X` 失敗時，**留著** `.done-X`，等下一次；
- 現在統一用 `_put_back`：改名失敗，而且 `X` 不存在時，**刪掉** `.done-X`。

同一個資料夾裡的改名幾乎不會失敗。會失敗的那一種（剛好有 stage 在換，ENOTEMPTY），刪掉也沒關係，因為有比較新的版本了。只有權限之類的怪情況，會把一個可能還沒傳上去的版本刪掉。風險很低，記下來就好。

如果要保守一點：`except OSError:` 裡面，只在 `folder.exists()` 的時候才刪 `.done-`。

## 還沒處理、之前就記過的 Low

- **V6**（T3-sec5）：列檔失敗時，`.update` 照樣傳。這次的註解寫明是刻意的（「看不到不等於不見了」）。但反過來說，這樣也可能把別台剛刪掉的傳回去。由 PM 決定，維持現狀的話建議在 design 寫一句；
- **R6**／E3：`uploader_is_running` 靠拿鎖來判斷，可能讓剛啟動的背景直接結束。

## Mutation（18 個）

| # | 改了什麼 | 結果 |
|---|---|---|
| 1 | E1：晚到的那一筆不看 `upload_batch` 的結果 | ✅ `test_a_late_session_drive_does_not_confirm_stays_in_the_outbox` |
| 2 | R7：改回阻塞地等 | ❌ 沒被抓到（G5） |
| 3 | F2：push 不等鎖 | ✅ `test_push_waits_for_the_uploader_instead_of_sending_itself` |
| 4 | V4：下一個指令不說 | ✅ `test_the_next_command_says_where_the_rescued_edit_went` |
| 5 | V4：不寫 notice | ✅ 同上 |
| 6 | V3：parents 只有 X | ✅ `test_the_rescued_session_keeps_this_write_s_relation_and_continues_the_line` |
| 7 | W4：啟動失敗照樣 exit 0 | ✅ `test_delete_says_exit_3_when_the_uploader_could_not_start` |
| 8 | X1：delete 時照樣說「沒上傳成功」 | ✅ `test_a_sync_that_starts_the_uploader_itself_calls_nothing_a_failure` |
| 9 | E9：記錄檔永遠不清 | ✅ `test_a_log_over_a_megabyte_starts_again` |
| 10 | E5：列不出來時回 None | ✅ `test_pull_marks_every_agora_id_failed_when_drive_is_gone` |
| 11 | E2：`uploader_is_running` 不放開鎖 | ⏱ 測試卡住，300 秒逾時（G5） |
| 12 | E2：背景做完不放開鎖 | ⏱ 同上 |
| 13 | E7：`_put_back` 改名失敗時留著 `.done-` | ❌ 沒被抓到（G6：舊的行為反而比較保守） |
| 14 | E4：`--files-from` 清單不清掉 | ✅ `test_mirror_holds_only_session_md_and_one_listing` |
| 15 | F1：前景不放 `.update` | ✅ `test_edit_marks_…`、`test_continue_writing_back_marks_…`、`test_an_import_that_updates_…` |
| 16 | F4：import 的同步不節流 | ✅ `test_two_imports_inside_five_minutes_list_drive_once` |
| 17 | F3：兩種情況說同一句 | ✅ `test_sync_says_nobody_is_sending_when_nobody_is` 等兩個 |
| 18 | W5：雲端沒有的也說「背景移到垃圾桶」 | ✅ `test_the_result_window_does_not_promise_a_background_delete_that_is_not_queued` |

（第一次跑的時候，runner 停在第 11 個：那兩個鎖沒放開的 mutant 讓測試永遠在等。所以 11～18 個是加了 300 秒逾時之後重跑的。）

## 結論

精簡沒有改變行為，src 是 3,759 行，在 3,800 以內。修正大致照 spec 做了，T3-final 的 F1～F4 也都有測試守著了。

**歸檔前建議修 G1**：晚到的那一筆要在鎖裡面送，並加一個「`upload_batch` 執行時一定有人拿著鎖」的測試。

**G2 要 PM 決定**：continue／edit 另存的 Y 用哪一種 relation，並讓 spec 和程式一致。

G3～G6 是 Low，可以和 V6、R6 一起放到之後。
