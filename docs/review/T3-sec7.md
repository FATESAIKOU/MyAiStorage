**沒有 High。W1～W3 都修好了，7 個 mutation 全部被抓到。** 只剩一個 Low 的說法問題（X1），以及 T3-sec6 當時就列為 Low、這次沒有處理的 W4、W5。

# Review：`c312d9f`（T3-sec6 的 W1～W3）

2026-10-03，review。
- 在 `git archive c312d9f` 的副本裡跑：`compileall` 通過，單元測試 **511 passed**，沒有失敗。T3-sec6 時那個已知會失敗的 pull 測試，已經由 `a3e0d67` 修好了。
- 探測和 mutation 都用 pytest 跑（有 conftest 的隔離），子程序另外傳了 `/tmp` 底下的 `AGORA_CONFIG`／`AGORA_CACHE_DIR`／`AGORA_STATE_DIR`／`HOME`。
- 沒有跑整合測試，沒有碰 Drive，也沒有讀任何真實的 Session。
- 這個副本也包含 `a14e7cb`（整合測試的修正），但那一份我**沒有審、也沒有跑**，PM 沒有指派。

## W1：delete 開頭的同步不再啟動上傳 ✅

- `cmd_delete` 改成 `store.sync(paths, kick=False)`；
- 結尾改成 `if queued or store.outbox_count(paths): background.start(paths)`，這樣原本就在 outbox 等的其他 Session，也會在這個時候一起啟動上傳。

我把 T3-sec6 的探測，原封不動在 `c312d9f` 上重跑一次（`AGORA_UPLOAD=inline`；X 已經在 Drive 上；讓 `copy` 失敗、edit X，新版本留在 outbox；恢復之後 delete X）。delete 期間的 rclone 呼叫：

| | T3-sec6（`5d99d0a`） | `c312d9f` |
|---|---|---|
| purge 之前 | `lsjson`、**`copy`（原始檔）、`copy`（session.md）**、`lsjson`×3 | `lsjson`、`copyto gdrive:… → 本機`、`lsjson` |
| 最後 | `purge` | `purge` |

`c312d9f` 那一次 `copyto` 的方向是 **Drive → 本機**（`Drive.download`，sync 把 Drive 上的 `session.md` 拉下來），不是上傳。所以 purge 之前**沒有任何上傳** ✅。

新的測試 `test_a_session_deleted_before_its_upload_never_goes_up` 改用 inline，不再換掉 `background.start`，並且斷言 delete 期間 `calls.log` 裡沒有 `copy`。把 `kick=False` 改回去、或讓 `sync` 不理 `kick`，這個測試都會紅。

## W2：「有 N 個等著移到 Drive 垃圾桶」✅

`main` 開頭，在「outbox 有 N 筆未上傳」的後面加上：佇列不是空的，而且沒有背景在跑，就提醒。

探測：讓 purge 失敗，下一個指令的 stderr 第一行就是 `[agora] 有 1 個等著移到 Drive 垃圾桶`。接著這個指令的同步啟動背景、再試一次；恢復連線之後，佇列空了，Drive 上也沒有了。

測試 `test_the_next_command_says_how_many_are_waiting_for_the_drive_trash` 同時斷言了兩件事：沒有背景時要說；背景在跑時不說。兩個 mutation 都會讓它紅。

附註：判斷「背景在不在跑」用的是 `uploader_is_running`，它是靠**拿一下鎖**來判斷的（T3-sec3 R6、T3-size E3）。這次在每個指令的開頭都會多做一次（只在佇列不是空的時候）。這是既有的 R6，不是這個 commit 新造成的。

## W3：sync 跳過佇列的兩個守護都有測試了 ✅

舊的測試拆成兩個：
- `test_sync_neither_lists_nor_marks_a_session_queued_for_deletion`：Drive 上**還留著**，`process_trash_queue` 換成什麼都不做，sync 之後斷言：沒有回到清單、沒有被標記、Drive 沒被動、佇列還在。這就是 T3-sec4 S2 的 (a)；
- `test_sync_does_not_mark_a_queued_session_that_drive_lost`：Drive 上已經沒有了，但佇列裡還有，sync 之後斷言沒有被標成雲端沒有。

T3-sec6 時沒被抓到的兩個 mutation（拿掉 `changed` 裡的 `u not in trashing`、拿掉 `mark_missing` 裡的 `u not in trashing`），現在分別被這兩個測試抓到。

## Mutation（7 個，全部被抓到）

| 拿掉的修正 | 抓到它的測試 |
|---|---|
| W1：delete 的 sync 照樣啟動上傳 | `test_a_session_deleted_before_its_upload_never_goes_up` |
| W1：`sync` 不理 `kick` | 同上 |
| W1：結尾只為佇列啟動上傳 | `test_delete_still_starts_the_uploader_for_another_session_waiting` |
| W2：不提醒 | `test_the_next_command_says_how_many_are_waiting_for_the_drive_trash` |
| W2：背景在跑也提醒 | 同上 |
| W3：sync 把佇列裡的拉回清單 | `test_sync_neither_lists_nor_marks_a_session_queued_for_deletion` |
| W3：sync 把佇列裡的標成雲端沒有 | `test_sync_does_not_mark_a_queued_session_that_drive_lost` |

## X1（Low）：delete 的時候說「outbox 還有 N 筆沒上傳成功」

`kick=False` 之後，delete 開頭的同步沒有啟動上傳，但 `sync` 還是會照舊說：

```python
say(f"背景上傳中，{len(waiting)} 筆" if uploader_is_running(paths)
    else f"outbox 還有 {len(waiting)} 筆沒上傳成功")
```

所以「改完馬上刪」的時候，使用者會先看到 `outbox 有 1 筆未上傳`（`main` 印的）、`outbox 還有 1 筆沒上傳成功`（sync 印的），接著那一筆就被刪掉了，結尾才有 `已從本機刪除 1 個，背景移到 Drive 垃圾桶`。這時候其實沒有任何東西「沒上傳成功」，根本還沒試。

**建議**：`kick=False` 的時候，sync 不說這一句（delete 結尾會自己啟動上傳）；或者在 kick=False 時改說「outbox 有 N 筆，指令結束時一起送」。

## 還沒處理的（T3-sec6 的 Low，這次不在範圍內）

- **W4**：背景啟動失敗時，delete 還是 exit 0，說「背景移到 Drive 垃圾桶」。我在 `c312d9f` 上重跑探測，結果一樣。
- **W5**：在互動模式裡，勾的全部都是雲端沒有的，結果視窗還是說「背景移到 Drive 垃圾桶」。

## 結論

W1～W3 都修好了，各自都有會失敗的測試守著。T3 的第 3、4 節，除了 X1、W4、W5 這三個 Low，沒有其他未解決的問題。要不要在歸檔前處理這三個 Low，由 PM 決定。
