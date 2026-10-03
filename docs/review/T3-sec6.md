**沒有 High。** 第 3、4 節大致照 spec 做了，T3-sec4 的 S1 也解決了（`process_trash_queue` 補上了，那個測試變綠）。有兩個 Medium：
- **W1**：「改完馬上刪」時，新版本還是會先被傳上去，因為 delete 自己開頭的同步就先啟動了上傳；
- **W2**：spec 要求的提醒「有 N 個等著移到 Drive 垃圾桶」沒有做。

另外，T3-sec4 的 S2 沒有處理：「sync 不把佇列裡的拉回來」的兩個 mutation，都只被那個已知會失敗的 pull 測試「抓到」，實際上沒有測試守著（W3）。

# Review：`5d99d0a`（T3 第 3、4 節：背景刪除、互動模式的說法）

2026-10-03，review。對照 `specs/local-first-writes/spec.md` 的「delete 先在本機」「互動模式的說明」，以及 `T3-sec4.md` 最後的檢查清單。

- 程式審的是 `5d99d0a` 的 diff；測試、探測和 mutation 都在 `git archive` 取出的副本裡跑，副本是 `5d99d0a`，以及 HEAD `60c1c76`（impl2 的 V1、V2 修正）。
- 兩份都通過 `compileall`。
- 探測和 mutation 都用 pytest 跑（有 conftest 的隔離），子程序另外傳了 `/tmp` 底下的 `AGORA_CONFIG`／`AGORA_CACHE_DIR`／`AGORA_STATE_DIR`／`HOME`。
- 沒有跑整合測試，沒有碰 Drive，也沒有讀任何真實的 Session。

## 單元測試

| | 結果 |
|---|---|
| `5d99d0a` | 494 passed，**1 failed** |
| HEAD `60c1c76` | 496 passed，**1 failed** |

兩邊失敗的都是 `test_pull_refuses_a_session_queued_for_deletion`，也就是 PM 說的那個已知失敗：它要等 impl2 commit T3-sec4 的 S3（pull 把拒絕算成失敗）。HEAD 的 `_pull_agora` 還是印一行之後 `return`，所以算成「拉下」。

附註：這個測試是在 `5d99d0a` 裡**加進來的**，但它要通過的程式不在這個 commit 裡。commit 訊息寫「496 unit tests pass」，量的應該是含有 impl2 還沒 commit 的改動的工作目錄。這和 T3-sec4 的 S1 是同一種情形：commit 前要在乾淨的副本上跑一次。

## 對照 T3-sec4 的檢查清單

| 項目 | 結果 |
|---|---|
| 前景的檢查照舊：沒加 `--yes`、有子 Session、正在接續 | ✅ 沒有改動，既有的測試照常通過 |
| `forget_local`、墓碑，指令結束前 `search` 就找不到 | ✅ |
| 拿掉 `outbox/<ULID>` 還沒傳的版本（L3） | ⚠️ 有拿掉，但 delete 開頭的同步已經先把它傳上去了，見 W1 |
| 背景正在比對的 `.done-<ULID>` | ✅ 探測：留下一個 `.done-X`（假裝背景當掉）再 delete X，然後跑兩次 search。X 沒有被傳回 Drive，也沒有回到清單；墓碑還在。原因是 delete 開頭的同步會先把 `.done-X` 改名回去、傳完，然後才刪 |
| 加進 `trash-queue`，然後啟動背景 | ✅；只有真的有東西進佇列時才啟動 |
| 啟動失敗時的 exit code 和說法 | ⚠️ 沒有看 `background.start` 的回傳值，見 W4 |
| 雲端沒有的只刪本機、不進佇列（L6） | ✅ 修好了 impl3 留下的 bug：`cloud_has` 改到 `forget_local` 之前問。mutation 確認過 |
| `process_trash_queue`：每個 ULID 一次 purge，沿用 S1-4／S1-4b | ✅ 直接呼叫 `store.delete_session`，所以規則只有一份。失敗的 purge 是每一筆各自列一次檔，不是共用一次列檔；這只影響失敗時的次數，可以接受 |
| 成功的才離開佇列；一筆失敗不擋後面的 | ✅（mutation 確認過） |
| 背景失敗的，下一個連 Drive 的指令會再試 | ✅ 探測：purge 失敗之後，佇列還在；下一個指令啟動背景，再試一次就成功了，Drive 上沒有了 |
| 提醒「有 N 個等著移到 Drive 垃圾桶」（背景在跑的時候不提醒） | ❌ **沒有做**，見 W2 |
| 一直失敗的那一筆，迴圈要停下來 | ✅ `_waiting` 把佇列算成 `(ulid, "trash")`，版本沒變就停 |
| pull 拒絕佇列裡的 | ⚠️ 會拒絕，但算成「拉下」（S3，等 impl2） |
| 互動模式（4.1）：delete 說「已從本機刪除，背景移到 Drive 垃圾桶」，import／merge 說「已經存在本機，背景上傳中」，只有 exit 3 說 outbox | ✅（W5 是一個小地方） |
| 4.2：等待視窗在子程序結束時就結束 | ✅ 沒有改動，T2 的程序群組本來就是這樣 |
| 測試：S2 拆開的那兩個 | ❌ 沒有拆，見 W3 |

## W1（Medium）：「改完馬上刪」，新版本還是會先被傳上去

spec 的 Scenario「改完馬上刪」：「edit X 之後、背景還沒傳上去，就 delete X → **X 的新版本不會被傳上去**」。

`cmd_delete` 的第一步是 `store.sync(paths)`，而 `sync` 一開始就會呼叫 `kick_uploader`：只要 outbox 不是空的，就啟動上傳。這發生在 `shutil.rmtree(paths.outbox / ulid)` **之前**。所以 X 的新版本會先被送出去，然後才被 purge。

實測（pytest 裡的探測，`AGORA_UPLOAD=inline`）：
1. X 已經在 Drive 上；
2. 讓 `copy` 失敗，然後 edit X，新的版本留在 outbox；
3. 恢復正常，`delete X --yes`。

delete 期間的 rclone 呼叫：

```
lsjson gdrive:sessions -R
copy …   ← 原始檔
copy …   ← session.md：新版本傳上去了
lsjson gdrive:sessions -R
lsjson gdrive:sessions -R
lsjson gdrive:sessions -R
purge gdrive:sessions/<X>
```

最後 Drive 上的 X 還是進了垃圾桶，所以**不會掉資料，也不會復活**。但是：
- spec 的這個 Scenario 不成立；
- 新版本在 purge 之前，會在 Drive 上待一下，別台的同步可能剛好拉到；
- 多了好幾次 rclone，這正是 T3 要省掉的。

用真的背景程序時，這是一個競賽：是背景先送出，還是前景先 `rmtree`。不過背景是 delete 自己啟動的，所以通常是背景比較快。

現在的測試 `test_a_session_deleted_before_its_upload_never_goes_up` 抓不到這個問題：它把 `background.start` 換成什麼都不做，所以 delete 的同步也啟動不了上傳。

**建議**：
- delete 的同步**不要**啟動上傳（例如給 `sync` 一個 `kick=False`）。等 outbox 和佇列都處理完，結尾再啟動一次；現在只在 `queued` 時啟動，要改成 outbox 不是空的也啟動；
- 測試不要換掉 `background.start`，用 inline，斷言 delete 期間 `calls.log` 裡沒有 `copy`。

## W2（Medium）：沒有「有 N 個等著移到 Drive 垃圾桶」的提醒

spec：「背景刪除失敗的，下一個會連 Drive 的指令 MUST 再試，**並提醒**『有 N 個等著移到 Drive 垃圾桶』」；design L4 也寫了「每個指令開頭提醒…背景正在跑時不提醒」。

在 HEAD 的 `src/` 裡找不到這句話。`main` 開頭只有 `outbox 有 N 筆未上傳` 和壞檔的提醒。

實測：讓 purge 失敗，再執行下一個 search。stderr 只有 inline 模式下背景自己的那幾行（`背景上傳開始`、`… 移到 Drive 垃圾桶失敗…`、`背景上傳結束，剩下 1 筆`）。用真的背景程序時，這些都寫進 `upload.log`，**終端機上什麼都沒有**。所以使用者不會知道 Drive 上還留著一份。

**建議**：在 `main` 開頭，和「outbox 有 N 筆未上傳」放在一起：`queued_for_trash(paths)` 不是空的，而且 `not uploader_is_running(paths)`，就印出 `[agora] 有 N 個等著移到 Drive 垃圾桶`。加一個測試：purge 失敗之後，下一個指令的 stderr 有這一句。

## W3（Low～Medium）：sync 跳過佇列的那一半，還是沒有測試守著（T3-sec4 S2）

`test_sync_leaves_a_queued_for_deletion_session_alone` 還是在 sync **之前**就自己刪掉了 Drive 上的資料夾。

| mutation | 在已知會失敗的那個之外，有沒有測試失敗 |
|---|---|
| sync 把佇列裡的拉回清單（拿掉 `changed` 的 `u not in trashing`） | **沒有** |
| sync 把佇列裡的標成雲端沒有（拿掉 `mark_missing` 的 `u not in trashing`） | **沒有** |

也就是說，這兩個 mutation 實際上都**沒被抓到**。

程式本身是對的。探測：放進佇列、從本機忘掉，Drive 上**還留著**，在背景處理之前做一次完整的 sync → 沒有回到清單，也沒有被標成雲端沒有。

**建議**：照 T3-sec4 S2 的 (a)：Drive 上的資料夾留著，換掉 `process_trash_queue`（背景還沒處理到），sync 之後斷言索引裡沒有它、也沒有被標記。(b) 那一半，`test_the_background_purges_each_queued_session_and_forgets_it` 已經做了。

## W4（Low）：背景啟動失敗時，delete 仍然 exit 0，說「背景移到 Drive 垃圾桶」

`cmd_delete` 呼叫了 `background.start(paths)`，但沒有看它的回傳值。實測：把 `start` 換成回傳 `FAILED`，結果是 exit `0`，stderr 是 `[agora] 已從本機刪除 1 個，背景移到 Drive 垃圾桶`。

design 的「exit code」寫的是「背景程序啟動失敗：exit 3」；`_save` 也是這樣做的。資料不會有事：佇列還在，下一個指令會再啟動。但說法不對，互動模式也會說「背景移到 Drive 垃圾桶」，而不是 exit 3 的那一句。

**建議**：`FAILED` 時 exit 3，說「已從本機刪除；移到 Drive 垃圾桶要等之後的指令」。

## W5（Low）：互動模式裡，全部都是「雲端沒有」的 delete 也說「背景移到 Drive 垃圾桶」

結果視窗只看 exit 0 和 `argv[0] == "delete"`。勾的列全部都是 ✗（雲端沒有）的話，什麼都沒有進佇列，指令本身也沒有說「背景移到…」，但結果視窗還是這樣說。

**建議**：可以接受，在 spec 寫一句就好；或者在這種情況下說「已從本機刪除」（例如看 stderr 裡有沒有「背景移到」）。

## Mutation（9 個）

每一個都只改一處（先確認那段文字只出現一次，再確認改完能編譯），跑 cli、cli_more、background、upload_batch、tui、cache 的測試。表裡**不算**那個已知會失敗的 pull 測試。

| 拿掉的東西 | 抓到它的測試 |
|---|---|
| delete 不拿掉 outbox 的那一筆（L3） | `test_a_session_deleted_before_its_upload_never_goes_up` |
| delete 在 `forget_local` 之後才問雲端 | `test_delete_of_one_the_cloud_lost_removes_only_the_local_copy`、`test_a_cloud_lost_session_is_not_queued_and_starts_nothing` |
| purge 失敗也移出佇列 | `test_a_trash_that_fails_stays_in_the_queue_for_the_next_command`、`test_one_session_that_cannot_be_deleted_does_not_hold_up_the_next` |
| 一筆 purge 失敗就停下 | `test_one_session_that_cannot_be_deleted_does_not_hold_up_the_next` |
| delete 不啟動背景 | `test_delete_needs_yes_and_moves_to_trash`、`test_a_deleted_session_is_queued_for_the_trash_and_the_command_returns` |
| 結果視窗 delete 說「完成」 | `test_the_result_window_says_the_drive_half_is_on_its_way` 等三個 |
| 結果視窗 import 說「完成」 | `test_the_result_window_says_an_import_is_safe_here` |
| sync 把佇列裡的拉回清單 | **沒有**（W3） |
| sync 把佇列裡的標成雲端沒有 | **沒有**（W3） |

## 行數

HEAD `60c1c76` 的 `src` 是 **3,741**（算法同 T1-size），目標是 3,800。

| 檔案 | 行數 |
|---|---:|
| store | 678 |
| cli | 747 |
| tui | 842 |
| cache | 204 |
| background | 94 |

## 結論

第 3、4 節的主要部分都做了：
- 前景的刪除和佇列，以及背景的 purge；
- 失敗留在佇列、下一個指令再試；
- 雲端沒有的只刪本機；
- 結果視窗的說法。

S1 也解決了。

**建議先修 W1、W2**，兩個都是 spec 的 MUST 沒有達到：
- W1：delete 開頭的同步不要啟動上傳；
- W2：每個指令開頭提醒還有幾個等著移到垃圾桶。

**W3** 是 T3-sec4 留下的 S2，補一個測試就好。W4、W5 是 Low。

pull 的那個失敗，等 impl2 commit S3 之後應該就會變綠，到時候我再確認一次。
