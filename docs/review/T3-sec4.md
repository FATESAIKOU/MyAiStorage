> **審到 `943d44d`（HEAD 是 `d4f4422`，只多了 review 的文件）。** impl1 的第 3 節還**沒有 commit 完**：`cmd_delete` 的前景、`process_trash_queue`、「雲端沒有的只刪本機、不進佇列」、佇列的提醒，都還在 impl1 的工作目錄裡（`git status` 看得到 `background.py`、`cli.py`、`store.py` 有改動），所以這一份只審已經 commit 的部分；其餘的留一份檢查清單在最後，等 commit 之後再審。

**沒有 High。** 但 **HEAD 現在是紅的**（S1，Medium）：`943d44d` 改了一個測試，讓它依賴還沒 commit 的 `process_trash_queue`。在乾淨的 HEAD 副本上，那個測試會失敗。

# Review：T3 local-first-writes 第 3 節（背景刪除），已 commit 的部分

2026-10-03，review。對照 `specs/local-first-writes/spec.md` 的「delete 先在本機」，以及 design 的「背景刪除」。在 `git archive HEAD` 的副本裡：`compileall` 通過，測試和探測都用 pytest 跑（有 conftest 隔離），子程序另外傳了指到 `/tmp` 的 `AGORA_CONFIG`／`CACHE_DIR`／`STATE_DIR`／`HOME`。沒有跑整合測試，沒有碰 Drive，也沒有讀任何真實的 Session。

## S1（Medium）：`943d44d` 讓 HEAD 的測試變紅

`943d44d` 把 `test_sync_leaves_a_queued_for_deletion_session_alone` 的最後兩行改成：

```python
assert ulid not in store.queued_for_trash(paths)          # 背景已經把它移到垃圾桶了
assert not (env / "agora" / "sessions" / ulid).exists()
```

可是 HEAD 的 `background.process_trash_queue` **還是空的**（「Empty on purpose: … section 3 fills it in」）。所以在乾淨的 HEAD 副本上：

```
FAILED tests/unit/test_upload_batch.py::test_sync_leaves_a_queued_for_deletion_session_alone
E  assert '01M3…' not in {'01M3…'}
1 failed, 48 passed
```

這個測試要通過，需要 impl1 工作目錄裡還沒 commit 的程式，這和 PM 剛定的規則（commit 前先確認能編譯、相關的測試能跑，只 add 確認過的檔案）不符。

**建議**：把這個測試的修改，和 `process_trash_queue` 的實作放在**同一個** commit；或者先把 `943d44d` 改回去。

## S2（Low～Medium）：這個測試本身守不住它要守的兩件事

測試在 sync **之前**就自己 `shutil.rmtree(env / "agora" / "sessions" / ulid)`，把 Drive 上的資料夾刪掉了。所以：

1. 「sync 不把刪除佇列裡的拉回來」（spec「delete 先在本機」、tasks 2.4）：Drive 上已經沒有了，sync 本來就拉不回來。這就是 T3-sec3 R11 那個 mutation 沒被抓到的原因。
2. 新加的「Drive 上也沒有」：測試自己刪的，所以一定成立，證明不了背景真的 purge 了。

**建議**拆成兩個測試：
- **(a) sync 的那一半**：Drive 上的資料夾**留著**（還沒 purge），把它放進佇列、從本機忘掉，sync 之後斷言索引裡沒有它、也沒有被標成雲端沒有；
- **(b) 背景的那一半**：Drive 上的資料夾留著，呼叫 `process_trash_queue`（或 `background.run`），斷言 Drive 上沒有了（fake rclone 的 purge 會把它刪掉），而且佇列空了。

## S3（Low）：pull 拒絕佇列裡的，卻算成「拉下」

`ce7bfb6` 的 pull 拒絕，我實測（pytest 裡的探測，佇列裡放一個已經上傳、本機已經忘掉的 Session）：

| | 回傳 (done, failed) | 訊息 | 加回索引 |
|---|---|---|---|
| `cache.pull(paths, [X], {})` | **(1, 0)** | 「X 正在刪除，不能 pull」 | 沒有 ✅ |
| `cache.push(paths, [X], {})` | (0, 1) | 「X 傳不上去：X 正在刪除，不能 push」 | — |

spec 說 pull 和 push 都 MUST 拒絕。push 算成失敗，exit 會是 2；pull 卻算成拉下一個，結尾說「拉下 1 個」，exit 0。這和 T3-sec3 R10 是同一個問題。**建議**：`_pull_agora` 裡也丟出 `StoreError("正在刪除，不能 pull")`，讓它算成失敗。

## 還沒 commit、之後要審的（給 impl1 的檢查清單）

照 spec「delete 先在本機」和 design「背景刪除」，commit 之後我會一項一項確認：

| 項目 | 依據 |
|---|---|
| 前景的檢查照舊：沒加 `--yes`、有子 Session、正在接續（拿著鎖的），都要拒絕 | spec 第 1 點 |
| `forget_local` 加上墓碑（`<state>/deleted`），並且在指令結束之前，`search` 就已經找不到了 | spec 第 2 點，Scenario「刪了馬上消失」 |
| 拿掉 `outbox/<ULID>` 還沒傳的版本（L3），也要處理背景正在比對的 `.done-<ULID>`（不要和 T3-sec3 R1 一樣互相刪錯） | spec 第 3 點，Scenario「改完馬上刪」 |
| 加進 `<state>/trash-queue/<ULID>`，然後透過 1.2 的 helper 啟動背景；啟動失敗時的 exit code 和說法 | design |
| **雲端沒有的**（有 `cloud_missing` 標記）只刪本機，**不**進佇列（L6） | spec、design |
| `process_trash_queue`：每個 ULID 一次 `purge`，沿用 S1-4／S1-4b（purge 失敗時，用列檔確認；列檔也失敗就不算刪掉）；成功的才從佇列移除。建議**所有**失敗的 purge 共用**一次**列檔，不要每一筆列一次 | design |
| 背景刪除失敗的，下一個會連 Drive 的指令會再試（`kick_uploader` 已經會看佇列），並且提醒「有 N 個等著移到 Drive 垃圾桶」（背景在跑的時候不提醒） | spec、tasks 3.3 |
| 刪除佇列和背景迴圈的互動：`_waiting` 已經把佇列算進去了（`(ulid, "trash")`）；一直 purge 失敗的那一筆，要讓迴圈在「沒有進展」的時候停下來，不要一直重試 | design、T3-sec2 P2 |
| 互動模式（tasks 4.1）：delete 的結果視窗說「已從本機刪除，背景移到 Drive 垃圾桶」 | spec「互動模式的說明」 |
| 測試：S2 拆開的那兩個，加上「刪除排隊時 pull 拒絕」、「改完馬上刪不會先傳上去」、「雲端沒有的不進佇列」、「背景 purge 失敗之後，下一個指令再試」 | tasks 3.4 |

## 結論

已 commit 的部分只有兩個：pull／push 的拒絕（`ce7bfb6`），以及一個測試的修改（`943d44d`）。
- pull 和 push 都會拒絕佇列裡的，pull 也不會把它加回索引；但 pull 算成了「拉下」（S3）。
- `943d44d` 讓 HEAD 的測試變紅（S1），而且這個測試守不住它要守的東西（S2）。

建議 impl1 把 `process_trash_queue` 和這個測試的修改放在同一個 commit，並且照 S2 把測試拆成兩個。第 3 節的其餘部分，等 commit 之後照上面的清單再審。
