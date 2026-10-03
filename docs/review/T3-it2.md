**沒有 High。** T3-it 的 I1～I3 都照建議修了：
- 四個情境走得到要驗的地方，也驗了內容（Drive 上的 md5 和本機一樣、原始檔在、救回來的是那次修改）；
- 機器 2 用完會切回來；
- 節流用 `forget_last_sync` 處理；
- 另存的 Y 也會被清掉。

**斷言沒有被放寬**：三個和背景無關的檔案改用 `AGORA_UPLOAD=inline`，是讓「指令回來 = Drive 上有了」重新成立，不是測得比較少。隔離和清理也都還正確。

但還有兩處在和背景比快慢（J1、J2，Low～Medium）：
- 測試先拿住上傳鎖，再放開；
- 放開之後**沒有人去啟動上傳**，只靠「指令剛啟動的背景程序，剛好在放開之後才去拿鎖」。

通常會過，但這正是 PM 要避免的「和背景競爭」。改法很小：放開之後明確呼叫一次 `store.kick_uploader(paths)`。

# Review：整合測試的修改（`a14e7cb`、`91682c7`、`f2e85f7`、`c8fe438`）

2026-10-03，review。**只讀**：impl1 正在跑整合測試，所以我沒有跑任何整合測試，也沒有碰 Drive。下面的判斷都是讀 `c8fe438` 的測試和 `src/agora/background.py`、`store.py` 推出來的。

## 有沒有把斷言放寬？沒有

| commit | 改了什麼 | 判斷 |
|---|---|---|
| `a14e7cb` | 照 T3-it I1：`make_session` 不再解包；`use_machine` 切過去也切回來；`forget_last_sync`；`hold_our_lock`；`index.children`。照 I2 加了 `same_as_local`（Drive 的 `session.md` md5 = 本機的、原始檔在、md5 對、可以比標題）、`bad_count == 0`、刪除之後搜尋不到、救回來的標題是「改過」。照 I3：清理前兩台都等一下，連另存的 Y 一起清 | ✅ 比原本**更嚴** |
| `91682c7` | `test_e2e_cli`、`test_e2e_opencode`、`test_pull_push` 設 `AGORA_UPLOAD=inline` | ✅ 這三個檔案驗的是 CLI、pull／push、跨 agent，所有斷言的前提都是「指令回來 = Drive 上有了」。inline 跑的是同一段上傳程式，只是在前景，所以斷言的意思沒有變。真正的背景程序，由 `test_background_writes` 和單元測試守著 |
| `91682c7` | `purge_on_drive` 拿掉 `check=True` | ✅ 可以接受。它是用來造出「Drive 上沒有了」的情況，已經沒有也算達到目的。如果 purge 因為別的原因失敗（例如斷線），後面的 `resync` 之後 `cloud_has` 還是 True，斷言照樣會紅，只是訊息會比較不直接。建議 purge 之後加一句 `assert ulid not in (drive.list_sessions() or {})`（Low） |
| `91682c7` | `wait_uploaded` 要連續兩次都是「安靜」的才回來；救回之後再同步一次才看標記 | ✅ 標記本來就是在同步時才變（T2 S4），不是在 push 時。這是改對測試，不是放寬 |
| `f2e85f7` | 情境 4 先等 import 傳完，**再**拿鎖 | ✅ 原本拿著自己的鎖去 `wait_uploaded`，一定會等到 300 秒逾時，修對了 |
| `c8fe438` | 多了一個 `}`，修掉 | ✅ |

## 隔離、清理：仍然正確

- 三個 `AGORA_*` 都指到 `tmp_path`；`AGORA_FOLDER_NAME=agora-test`；`config.json` 在暫存的 config 目錄裡；機器 2 用 `tmp_path/other`，共用同一個 config 目錄（也就是同一個 `agora-test` 的 folder ID）✅
- 清理：
  - 先對兩台各做一次 `wait_uploaded(…, timeout=60)`（失敗也繼續），避免背景在 purge 之後才把東西傳上去 ✅
  - purge 的只有這次印出的 ULID，加上 `index.children(…)` 找到的另存 ✅
  - opencode 的 session 依 id 一個一個刪 ✅
- 小地方（J4）：測試在 `hold_our_lock(...)` 和 `held.__exit__()` 之間失敗的話，鎖不會被放開，清理的 `wait_uploaded(here)` 會白等 60 秒，outbox 裡的也不會傳上去。不會留下垃圾（沒傳上去的就沒有要清的），只是慢。建議用 `try/finally` 或 `with`。

## `wait_uploaded()`：不會太早回來，有逾時，但可能被「沒人啟動上傳」拖到逾時

**太早回來？不會。**
- 條件是「鎖沒人拿、outbox 空、佇列空」，連續兩次、間隔 0.5 秒；
- outbox 只有在**拿著鎖**、驗完 Drive 的 md5 之後才會變空。比對中的 `.done-`、N5 另存，也都在鎖裡；
- 所以「安靜」的時候，該傳的都已經驗過了；
- 「已經啟動、但還沒拿到鎖」的背景，這時候也已經沒有事可做。

**永遠等？不會**，有 300 秒的上限，訊息會說是鎖、outbox、還是佇列卡住。

**但會被拖到逾時**的情況，見 J1、J3。

## J1（Low～Medium）：情境 1～3 放開鎖之後，沒有人去啟動上傳

情境 1（情境 2、3 一樣）：

```python
held = hold_our_lock(paths)
ids = import_sessions(env, 2)        # 每一個 import 都會 background.start(paths)
assert store.outbox_ulids(paths)
held.__exit__()
wait_uploaded(paths)                 # 只看，不啟動
```

`background.start` 不管鎖，照樣 spawn 一個 `python -m agora.background`。這個子程序**只試一次**鎖，拿不到就印「已經有一個上傳在跑，這一輪不重來」然後結束。它的設計前提是：拿著鎖的那一個，放開之前會再看一次 outbox。可是這裡拿著鎖的是**測試**，不是上傳程式，放開時不會再看。

所以這三個情境能不能過，看的是：**最後一個指令 spawn 的子程序**，是不是剛好在 `held.__exit__()` **之後**才去拿鎖。

| 情況 | 結果 |
|---|---|
| 子程序的 Python 啟動（import `agora.store` 等，大約一兩百毫秒）比測試走到 `held.__exit__()`（幾毫秒）慢 | 它拿得到鎖，照常上傳。**通常是這樣**，所以現在看起來會過 |
| 子程序比較快（測試的程序剛好被排程延後、或機器很忙） | 它拿不到鎖就結束；之後沒有人再啟動上傳，`wait_uploaded` 等滿 300 秒，訊息是「outbox=[…]」。看起來像上傳卡住，其實是測試自己的問題 |

情境 4 已經是對的寫法：放開之後跑了一個會同步的 `search`，由它去啟動上傳。

**建議**：情境 1～3 在 `held.__exit__()` 之後，加一行 `store.kick_uploader(paths)`（只看檔案、不拿鎖，outbox 或佇列有東西才啟動）。這樣就不靠時序了。

## J2（Low～Medium）：情境 2 放開鎖之後才看刪除佇列

```python
held = hold_our_lock(paths)
code, out, err = run_cli(env, "delete", "session", *ids, "--yes")
held.__exit__()
... # header 是 None、search 找不到
assert sorted(store.queued_for_trash(paths)) == sorted(ulid_of(i) for i in ids)
```

放開之後，delete 剛 spawn 的子程序（如果它晚一點才去拿鎖）會開始 purge。下面的 `search` 有節流，所以不會列 Drive，很快。佇列那一句大概在放開後一百毫秒內就執行，通常比 purge 早。但這又是在比快慢；反方向一輸，就變成 J1。

**建議**：
1. 把 `held.__exit__()` 移到**佇列那一句之後**（鎖拿著的時候，佇列一定還在）；
2. 放開之後 `store.kick_uploader(paths)`；
3. 再 `wait_uploaded`。

## J3（Low）：`wait_uploaded` 自己的探測，可能讓剛啟動的背景直接結束

`wait_uploaded` 每 0.5 秒呼叫一次 `store.uploader_is_running(paths)`，它是靠**真的拿一下鎖**來判斷的（T3-sec3 R6 的形狀；T3-it I4 提過）。背景剛好在那幾微秒去拿鎖的話，會以為有人在跑，直接結束；之後沒有人再啟動，又是等到逾時。機率非常小。

順便的修法，可以同時解決 J1：在 `wait_uploaded` 裡，「鎖沒人拿，但 outbox 或佇列還有東西」連續兩次時，呼叫一次 `store.kick_uploader(paths)` 推一下。這和真的使用者「下一個指令會再啟動」是同一個行為，不算放寬。

## J4（Low）：鎖在失敗時沒有放開

見上面的「隔離、清理」。

## 結論

這一輪的修改方向是對的：
- 修的是測試的前提和等待方式，不是把斷言放寬；內容的斷言反而比較嚴；
- 隔離和清理都正確；
- `wait_uploaded` 不會太早回來，也有上限。

剩下的 J1、J2 是「放開鎖之後靠子程序剛好比較慢」這種時序依賴。各加一行 `kick_uploader`、把放開鎖的那一行往後移，就可以讓結果不靠運氣。impl1 這一輪跑出來的結果如果是全過，也不代表 J1、J2 不存在，只是這次時序站在對的那一邊。
