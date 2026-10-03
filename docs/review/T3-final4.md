> **審到 HEAD `4ac6115`**（impl3 的 E3／R6）。前面的 `9af64dc`（H1）和 `91682c7`（整合測試）不在這次的範圍內。

**沒有 High，但有一個 Medium（K1），建議歸檔前修。** R6 修好了：
- `outbox_ulids` 不再碰上傳鎖；
- 互動模式重讀時只呼叫 `outbox_ulids`，所以也不再短暫地拿鎖；
- 兩個新測試都抓得到各自的 mutation。

**但 PM 特別問的「當掉後留下的 `.done-` 還會被還原嗎」：答案是不會。**
- 只有在背景**因為別的原因**被啟動時，才會還原；
- 只剩下 `.done-X` 的時候，`kick_uploader`、`outbox_count` 都看不到它，所以**沒有人會啟動背景**；
- 下一次同步會把鏡像蓋回 Drive 上的舊版本；
- 使用者再 edit 一次，那一次被改名到一邊的修改就**永久不見了**，而且沒有任何提醒。

在 `9af64dc` 上，同一個情境下一個指令就會還原並傳上去。

# Review：`4ac6115`（T3-size E3／T3-sec3 R6）

2026-10-03，review。
- 在 `git archive 4ac6115` 的副本裡跑：`compileall` 通過，單元測試 **534 passed**。
- 探測和 mutation 都用 pytest 跑（有 conftest 的隔離），子程序另外傳了 `/tmp` 底下的 `AGORA_CONFIG`／`AGORA_CACHE_DIR`／`AGORA_STATE_DIR`／`HOME`。
- 沒有跑整合測試，沒有碰 Drive，也沒有讀任何真實的 Session。

## 改了什麼

- `outbox_ulids` 拿掉了「沒人拿鎖就 `_restore_done`」：列 outbox 不再碰鎖；
- `restore_done` 改成只在 `background.run` 拿到鎖之後做一次。

## R6：修好了 ✅

- 互動模式重讀（`tui.py` 第 89 行）只呼叫 `outbox_ulids`，現在不會拿鎖了；
- 所以「剛啟動的背景，剛好撞上某個指令或互動模式在列 outbox，以為已經有人在跑，就直接結束」的情況，不會再因為列 outbox 而發生；
- 新的測試 `test_a_background_starting_while_a_command_lists_the_outbox_still_sends_it` 守著。

還會短暫拿一下鎖的地方，只剩下：
- sync 在 outbox 有東西時，判斷要說「背景上傳中」還是「沒上傳成功」；
- `main` 在刪除佇列有東西時的提醒；
- `background.start` 決定要不要修剪記錄檔。

都是「一個指令一次」，不是每按一個鍵一次。

**交錯**：
- 能建立 `.done-` 的只有拿著鎖的人（`upload_batch`、N5 另存，push 也在鎖裡了）。所以一個**剛拿到鎖**的背景看到的 `.done-X`，一定是之前當掉留下的，還原是安全的 ✅；
- 還原時如果 `outbox/X` 已經有比較新的（`stage` 剛好在換），`_put_back` 會丟掉 `.done-X`，讓新的贏 ✅（和以前一樣）。

## K1（Medium）：只剩當掉留下的 `.done-X` 時，沒有人會去還原它

`kick_uploader` 只在 `outbox_ulids(paths) or queued_for_trash(paths)` 時啟動背景，而 `outbox_ulids` 會跳過 `.` 開頭的資料夾。`main` 的「outbox 有 N 筆未上傳」、sync 的「沒上傳成功」也一樣看不到它。

實測（pytest 裡的探測，指令驅動；`9af64dc` 和 `4ac6115` 的副本跑同一支）：
1. import X，已經在 Drive 上；
2. 讓 `copy` 失敗，`edit X --header title=第二版`，新版本留在 outbox；
3. 把 `outbox/X` 改名成 `.done-X`（模擬背景在改名之後、比對之前當掉）；
4. 刪掉 `last-sync`，跑兩次 `search session`，然後再 `edit X --header description=第三次`。

| | `9af64dc` | `4ac6115` |
|---|---|---|
| 第一次 search 之後 | `.done-X` 被還原、傳上去了；stderr 有 `outbox 有 1 筆未上傳`、`背景上傳開始`。Drive、鏡像、索引都是「第二版」 | **`.done-X` 還在**；沒有啟動背景，也沒有任何提醒。Drive 是舊的標題；**鏡像和索引也被同步蓋回了舊的標題** |
| 第二次 search 之後 | 一樣 | 一樣（沒有東西會改變它） |
| 再 edit 一次之後 | Drive 的標題還是「第二版」 | Drive 的標題是**舊的**，`.done-X` **不見了**。「第二版」那次的修改永久遺失 |

最後一步的原因：
- 第三次的 edit 是從（被蓋回舊版的）鏡像改的，stage 出一個新的 `outbox/X`；
- 背景拿到鎖之後的 `restore_done` 看到 `outbox/X` 已經有了，照「新的贏」的規則把 `.done-X` 丟掉；
- 可是這個「新的」並不包含「第二版」的修改。

觸發的條件：背景剛好在「改名成 `.done-` 之後、比對完之前」被殺掉（強制結束、斷電、睡眠時被系統收掉）。那一段包含一次列檔，大約一秒。機率低，但結果是**悄悄地丟掉使用者的修改**，而這正是 H1、R1 一路在防的事。

測試沒有抓到：`test_the_background_puts_back_what_a_crashed_uploader_renamed_aside` 是**直接呼叫** `background.run`，跳過了「誰會去啟動它」這一步。

**建議**（不需要碰鎖，只是看一下有沒有 `.done-*`）：
1. `kick_uploader` 在 `any(paths.outbox.glob(".done-*"))` 時也啟動背景；
2. `outbox_count`（「outbox 有 N 筆未上傳」）和 sync 的提醒，把留下來的 `.done-X` 也算進去；
3. sync 把有 `.done-X` 的 ULID 當成「還在 outbox」：不要用 Drive 的舊版蓋掉鏡像，也不要標成雲端沒有（和 `staged` 一樣處理）。這一點最重要：只做 1 的話，背景還原之前，同步就已經把鏡像蓋回舊的了，使用者在這之間 edit，一樣會掉；
4. 加一個**指令驅動**的測試：照上面的 1～4 步，只跑 `search`，不手動呼叫 `background.run`，斷言最後 Drive 和鏡像都是「第二版」；再 edit 一次之後，兩次的修改都在。

## Mutation（3 個，全部被抓到）

| 改了什麼 | 抓到它的測試 |
|---|---|
| 背景拿到鎖之後不還原 `.done-` | `test_the_background_puts_back_what_a_crashed_uploader_renamed_aside` |
| `outbox_ulids` 又去試拿鎖（R6） | `test_a_background_starting_while_a_command_lists_the_outbox_still_sends_it` |
| `outbox_ulids` 改回「沒人跑就還原」 | 上面兩個都抓到 |

K1 的情境沒有對應的 mutation 可做：缺的是「啟動」那一步，現在的程式本來就沒有。

## 結論

R6 修好了，互動模式重讀不再碰鎖，背景拿到鎖之後還原 `.done-` 也是安全的。但是「當掉之後留下的 `.done-`」，現在只有在別的東西剛好啟動背景時才會被救回來；在那之前，同步會把鏡像蓋回舊版，下一次 edit 就會讓那次修改永久消失。

**建議歸檔前修 K1**：`kick_uploader`、提醒、sync 都把 `.done-*` 算成還在等，再加一個指令驅動的測試。改動不大，而且都不需要拿鎖，不會把 R6 帶回來。
