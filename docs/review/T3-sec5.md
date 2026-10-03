**沒有 High。R1～R4 都修好了，8 個 mutation 全部抓得到。** 但新加的 N5（另存成新的 Session）引入了一個交錯問題（V1，Medium）：另存的途中如果又 edit 了 X，第二次的修改會被 `_rescue_deleted` 的 `rmtree` 刪掉，既沒有存成 Y，也沒有留在 outbox。另外，Y 沒有進本機的索引和鏡像（V2，Medium）。HEAD 仍然是紅的（T3-sec4 的 S1 還沒處理）。

# Review：`5c804d8`（T3-sec3 R1～R4 的修正）

2026-10-03，review。在 `git archive 5c804d8` 的副本裡：`compileall` 通過；探測和 mutation 都用 pytest 跑（有 conftest 的隔離），子程序另外傳了 `/tmp` 底下的 `AGORA_CONFIG`／`AGORA_CACHE_DIR`／`AGORA_STATE_DIR`／`HOME`（照 T4-sec1 的附註，`AGORA_FOLDER_NAME`、`AGORA_RCLONE` 不傳進 pytest）。沒有跑整合測試，沒有碰 Drive，也沒有讀任何真實的 Session。

單元測試：**483 passed，1 failed**。失敗的是 `test_sync_leaves_a_queued_for_deletion_session_alone`，也就是 T3-sec4 的 S1：它依賴還沒 commit 的 `process_trash_queue`，和這次的修正無關，但 HEAD 還是紅的。

## R1～R4：修好了

| | 修了什麼 | 怎麼確認的 |
|---|---|---|
| R1 | `stage` 不再刪 `.done-<ULID>`；比對完如果 `outbox/<ULID>` 又出現了，只丟 `.done-`，這一筆算在「還在等」 | 重跑 T3-sec3 的 H1 重現（比對途中 stage 第二版）：第二版留在 outbox，下一輪之後 Drive 和本機都是「第二版」 |
| R1 | 一筆驗不了（`_entry_md5s(done)` 丟 `OSError`／`HeaderError`），只把那一筆改名回去、留到下一輪，不中斷整輪 | 新測試，以及下面的 mutation |
| R2（N5） | `.update` 而 Drive 上沒有 → 另存成新的 Session（新的 ULID，parents 指向 X），X 離開 outbox，Y 在同一輪傳上去 | 探測：Drive 上有 Y、X 沒有被傳回去，這一輪回傳 `left=[]` |
| R3 | M4 的測試只讓**原始檔**那一批失敗 | mutation |
| R4 | 只改標頭的 edit，比 session.md 的 md5，也算還在等 | mutation |

## Mutation（8 個，全部被抓到）

每一個都只改 `store.py` 的一處（先確認那段文字只出現一次，再確認改完能編譯），跑 upload_batch、background、cache、store 的測試。表裡沒有列 S1 那個本來就紅的測試。

| 拿掉的修正 | 抓到它的測試 |
|---|---|
| R1：`stage` 又刪 `.done-<ULID>` | `test_an_edit_that_lands_while_we_compare_is_not_lost` |
| R1：驗證時不接住例外 | `test_one_entry_that_cannot_be_verified_does_not_end_the_round` |
| R1：比對完 `outbox/X` 又出現，也照樣算完成 | `test_an_edit_that_lands_while_we_compare_is_not_lost` |
| N5：不另存（`_rescue_deleted` 直接回傳 None） | `test_an_update_someone_else_deleted_is_saved_as_a_new_session` |
| N5：另存之後不拿掉 X | 同上 |
| N5：Y 不在同一輪傳 | 同上 |
| M4：原始檔失敗，照樣傳 session.md | `test_a_failed_raw_upload_sends_no_session_md_at_all` |
| R4：不比 session.md 的 md5 | `test_an_edit_that_only_changes_the_header_is_not_reported_as_sent` |

## 新的問題

### V1（Medium）：另存的途中又 edit 了 X，第二次的修改會被刪掉

`_rescue_deleted` 的步驟是：讀 `outbox/X` → `stage(Y)` → `rmtree(outbox/X)`。它沒有先把 X 拿走（改名成 `.done-X`），所以在讀完之後、`rmtree` 之前，使用者 `edit` X 所 stage 的新版本，會被這個 `rmtree` 一起刪掉。

實測（pytest 裡的探測，在 `stage(Y)` 的時候插一次 `edit X`）：

```
PROBE-RACE outbox has X: False
Y on Drive holds: 第一次改
local mirror of X holds: 第二次改
after sync X is marked cloud-missing: True
```

所以第二次的修改沒有存成 Y，也沒有留在 outbox 等下一輪；它只留在 X 的本機鏡像裡，而 X 在下一次 sync 之後被標成雲端沒有。資料還在本機，但不會被傳上去，也不會有任何提醒；只能手動救回來。

這個時間窗口很小（使用者要剛好在背景另存的那一瞬間 edit），可是另存的情況本身就是「別台剛刪掉、這台還在改」，而且這種操作是同時進行的，所以會發生。

**建議**：和 H1 一樣，先 claim 再處理：
1. `outbox/X` 先改名成 `.done-X`（失敗就是有人在 stage，留到下一輪）；
2. 從 `.done-X` 建 Y，然後只刪 `.done-X`；
3. 如果這時候 `outbox/X` 又出現了，**不要動它**，下一輪會再碰到「`.update` 而 Drive 沒有」，再另存一次。

加一個測試：在 `stage(Y)` 時插一次 `edit X`，斷言第二次的修改最後在 Drive 上（存成另一個新的 Session，或者留在 outbox）。

### V2（Medium）：Y 沒有進本機的索引和鏡像

`_rescue_deleted` 只 `stage` 了 Y，沒有呼叫 `remember`。實測：Y 傳上去了，但本機的索引裡沒有 Y，鏡像裡也沒有 Y 的原始檔。結果：

- 這台的 search、list 找不到 Y，要等下一次 sync 把它從 Drive 拉下來；
- 這違反了「本機保留完整的一份」，等於讓 Y 又回到 P1（只有 Drive 上有）。

**建議**：stage Y 之後，用和 edit 一樣的方式 `remember` Y（鏡像加上索引），然後才把 X 移走。

### V3（Low）：Y 的標頭

- **parents 被整個換掉**，變成只有 `[X]`；X 原本的 parents 不見了。建議改成 `X 原本的 parents + [X]`，或者至少把 X 原本的 parents 留著；
- relation 還是 `import`。spec 寫的是「沿用原本那次寫入的 relation」，請確認 edit、merge、continue 另存出來的是不是都對；
- 探測裡，Y 的標頭沒有 `raw_md5`。`raw` 那一段由 `stage` 重算，沒有問題；但 X 的標頭裡如果還有其他和 X 綁在一起的欄位，請一起檢查。

### V4（Low）：「存成了 Y」的提醒，背景模式下只寫進 upload.log

`say` 在背景是寫到記錄檔，所以使用者永遠不會看到「X 已被別台刪除，這次的修改存成了 Y」。使用者會以為改的是 X，可是 X 已經不見了，新的 Y 他也不知道。

**建議**：把這個提醒存成一個檔案（例如 `<state>/notices/`），讓下一個指令（和 main 的「有 N 個等著上傳」放在一起）印出來一次。

### V5（Low，推論，沒有實測）：`stage` 的 exists／rename 交錯

```python
if folder.exists():
    folder.rename(old)
```

背景在 `exists()` 和 `rename()` 之間把 `outbox/X` 改名成 `.done-X` 的話，`rename` 會丟 `FileNotFoundError`，edit 指令就會失敗（使用者看到錯誤，但改的內容還沒存進 outbox）。**建議**：接住 `FileNotFoundError` 就繼續（照常把 tmp 放進來）。

### V6（Low，`235e68a` 就有了）：Drive 列不出來時，`.update` 照樣傳

```python
gone = [u for u in updates if remote is not None and u not in remote]
```

`remote is None`（列檔失敗）的時候，`gone` 是空的，所以所有 `.update` 都會照常傳上去，不做 L7 的檢查。如果列檔只是剛好失敗一次、傳檔又成功了，別台刪掉的 Session 就會被傳回去，這違反 T1 的 Q1。**建議**：列不出來時，這一輪**不傳** `.update` 的那幾筆，把它們算在「還在等」。

### V7（Low）：`tasks.md` 把 3.1～4.2 打勾了，但程式還沒 commit

`5c804d8` 把 tasks 的 3.1～3.4、4.1～4.2 都改成 `[x]`。可是在 `5c804d8`，`process_trash_queue` 仍然是空的（「Empty on purpose」），delete 和互動模式的結果視窗也還沒改。這些是 impl1 工作目錄裡還沒 commit 的東西。建議等 impl1 commit 之後再打勾，不然歸檔檢查會以為它們已經做完了。

另外有一個小地方：新的 Y 加進 `entries` 時呼叫的 `_entry_md5s(paths.outbox / new_id)` 沒有接例外。Y 是剛 stage 的，幾乎不可能讀不出來；可是萬一讀不出來，整輪就會中斷，和 R1 的「一筆不中斷整輪」不一致。

## T3-sec3 還沒處理的

R5（開頭列 outbox 時遇到 `OSError` 仍然會 quarantine），以及 R6～R12。這次的 commit 只處理 R1～R4，所以這些照舊。T3-sec4 的 S1～S3 也還在（S1 就是上面那個紅的測試）。

## 結論

R1～R4 都修好了，mutation 全部抓得到。N5 的另存功能可以用，但在下一次之前，**建議先修 V1**（另存的途中又 edit，第二次的修改會被刪掉）和 **V2**（Y 要進本機的索引和鏡像）。V3～V7 是 Low。另外，HEAD 仍然是紅的（T3-sec4 的 S1）。
