**沒有 High。** 第 1 節的骨架做對了：
- 背景程序**真的**不接呼叫端的 stdout（實測：讀 pipe 的那一端 0.1 秒就拿到 EOF，背景還要再傳 10 秒）；
- 不繼承 pending 鎖（用真的子程序測過）；
- env 原樣傳下去；
- inline 失敗時回報 FAILED；
- sync 不在前景上傳。

7 個 mutation 裡抓到 6 個。

有 3 個 Medium：
- **P1**：「放開鎖之後再檢查一次」沒有測試，拿掉了也照樣全過；
- **P2**：迴圈用「等著的 ULID 集合有沒有變」來判斷要不要停，等第 2 節的 H1 做好之後，「import 完馬上 edit」的新版本會被留到下一個指令；
- **P3**：`_save` 和 `push` 現在**還沒有**拿 `upload.lock`，而 sync 已經會啟動真的背景了，所以兩個上傳的程序可能同時傳同一筆。在第 2 節的 2.3、2.5 做完之前，**不要拿第 1 節去用真的資料**。

# Review：T3 local-first-writes 第 1 節（`6933935`，背景上傳的骨架）

2026-10-03，review。對照 `6046d62` 之後的 spec 和 design。在 `git archive 6933935` 取出的副本跑全部的單元測試：**459 passed**。mutation 和探測測試都只在副本裡做，用的是 fake rclone，測試用的目錄都在暫存資料夾裡。沒有跑整合測試，沒有碰 Drive，也沒有讀任何真實的 Session。

## 逐項確認

| 重點 | 實作 | 驗證 | 結果 |
|---|---|---|---|
| 不接呼叫端的 stdout | `Popen(..., stdin=DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True, env=None)` | **探測**：一個子程序呼叫 `background.start()` 之後就結束，外面讀它的 stdout pipe。原本的程式 **0.1 秒**就拿到 EOF（背景要到 10.6 秒才傳完）；把 stdout 改成繼承（`stdout=None`）的 mutant，要等到 **10.5 秒**、背景結束才拿到 EOF，而且背景的輸出也混進了 pipe。這就是 T2-archive X1 的那個問題。mutation：繼承 stdout → **被抓到**，但抓到它的是「記錄檔裡沒有『背景上傳開始』」那一個斷言，不是「沒寫到終端機」（見 P4） | ✅ |
| 另開 session、stdin 是 DEVNULL | 同上 | mutation 兩個都**被抓到**（`test_it_is_launched_the_way_the_lock_needs`） | ✅ |
| 不繼承 pending 鎖 | `close_fds=True` | `test_it_does_not_inherit_the_lock_a_continue_is_holding`：用真的子程序；我們這邊關掉 fd 之後，`continuing()` 是 False。（另外說明：Python 開的檔案預設本來就不會被繼承（PEP 446），所以 `close_fds=True` 是多一層保險；這個測試守的是真實的行為，這是對的） | ✅ |
| env 原樣 | `env=None` | `test_it_is_launched_the_way_the_lock_needs` 斷言 `env is None`。**我刻意沒有做「env 改成空的」這個 mutation**：子程序如果拿不到 `AGORA_*`，有可能退回去用正式的設定和正式的 Drive，這違反這次的規則 | ✅（靠參數的斷言） |
| 一把鎖 | `_take`：`flock(LOCK_EX \| LOCK_NB)`，拿不到就回 None | mutation「拿不到也照樣上傳」→ **被抓到**（`test_a_second_uploader_skips_while_the_lock_is_held`） | ✅ |
| 晚到的 | 迴圈裡每一輪都重新看 `_waiting`；放開鎖之後再看一次，不是空的就再拿一次鎖 | `test_a_session_staged_while_it_runs_still_goes_up` 通過；但拿掉「放開鎖之後再檢查」這一段，**照樣通過**（P1） | ⚠️ |
| inline 的 exit 3（N1） | `start()` 在 inline 時跑 `run()`，outbox 或佇列還有東西就回 `FAILED` | mutation「inline 失敗也說完成」→ **被抓到** | ✅（但呼叫端還沒接上，見 P3、P6） |
| sync 不在前景上傳（N3） | sync 呼叫 `kick_uploader`，不再呼叫 `push_outbox` | mutation 改回前景上傳 → **被抓到** | ✅ |
| 記錄檔 | 超過 1 MB 只留最後 256 KB | `test_a_log_over_a_megabyte_keeps_only_its_tail` | ✅（見 P7） |

## P1（Medium）：「放開鎖之後再檢查一次」沒有測試

把 `run()` 裡放開鎖之後的這一段：

```python
left = _waiting(paths)
if not left or left == seen:
    break
```

改成直接 `break`，`test_background.py` 照樣全部通過。原因是現有的測試，第二筆是在背景**還在第一輪**的時候進來的，所以裡面那個迴圈下一次就看到了，根本走不到「放開鎖之後」這一段。spec「背景上傳」那一句「不能留到『下一個指令』」，靠的就是這一段，卻沒有測試守著它。

**建議的測試**：monkeypatch `fcntl.flock`，在背景呼叫 `LOCK_UN` 的那一刻 stage 一筆新的進來（這就是 design 說的那個空檔），斷言不必再呼叫一次 `start()`，那一筆最後也會被傳上去。

## P2（Medium，第 2 節要一起改）：用「ULID 的集合有沒有變」判斷要不要停

```python
seen = None
while (waiting := _waiting(paths)) and waiting != seen:
    seen = waiting      # nothing new since the last round: it is failing, stop
```

`_waiting` 回傳的是 **ULID 的集合**。等第 2 節的 H1 做好之後（上傳期間被新版本取代的，留在 outbox），spec 的 Scenario「上傳中又改了同一個」會變成這樣：
1. 第一輪上傳 X 的 v1；
2. 這中間 edit 了 X，outbox 裡換成了 v2；
3. 驗完之後 X 留下來（H1 的規則），`waiting = {X}`，和 `seen = {X}` 一樣；
4. 迴圈判斷成「失敗了，停」，放開鎖之後的 `left == seen`，也一樣 `break`。

所以 v2 會被留到**下一個指令**，違反 spec 的「MUST 在它結束前一併傳完」。

**建議**：「有沒有新的東西」要比對**版本**，不是只比 ULID。例如用 `{(ulid, outbox 裡 session.md 的 md5)}` 這個集合（刪除佇列就只看 ULID）。這一段第 2 節的 2.3 會改到，最好在那時一起改，並把 Scenario「上傳中又改了同一個」寫成真的測試。

## P3（Medium，過渡期）：其他上傳的地方還沒有拿鎖

在 `6933935`：
- `cli._save` 還是直接 `store.push_one(...)`（第 141 行）；
- `cache.push` 還是直接 `store.push_outbox(...)`（第 246 行）；
- 這兩個都**沒有**拿 `upload.lock`。

而 sync 現在已經會在 outbox 不是空的時候，啟動**真的**背景（非 inline 的時候）。所以，例如上一次失敗留在 outbox 裡的 X，被背景拿去傳；同時使用者 edit X，`_save` 在前景也去傳 X。兩個程序同時對同一個 `outbox/X` 做上傳和 `rmtree`，這正是 T3 M1 和 H1 要避免的事。

tasks 2.3、2.5 會處理（push 阻塞等鎖、`_save` 改成交給背景），在那之前：
- 第 1 節**不要單獨拿去用真的資料**；
- 整合測試（非 inline）也先不要只跑到第 1 節就跑。

建議在 tasks 的 1.x 加一行註記，說明這個過渡期。

## 其他（Low）

| # | 問題 | 建議 |
|---|---|---|
| P4 | `test_nothing_it_prints_reaches_our_terminal` 用的是 `capsys`，它只抓得到 Python 層的 `sys.stdout`；子程序繼承 fd 1 時寫的東西，它看不到。上面那個「繼承 stdout」的 mutant 被抓到，是因為後面那一句「記錄檔裡有『背景上傳開始』」失敗了，不是因為「沒寫到我們的終端機」 | 把我的探測測試加成正式的測試：子程序呼叫 `start()` 之後就結束，外面讀它的 pipe，斷言 EOF 在背景結束**之前**就到了（幾百毫秒之內） |
| P5 | sync 的提醒：只要 outbox 不是空的，就說「背景上傳中，N 筆」。可是：(a) `kick_uploader` 沒有看 `start()` 的回傳值，啟動失敗（`FAILED`）的時候也這樣說；(b) 背景如果已經失敗結束了（鎖沒人拿，outbox 還有東西），也這樣說。L5 的意思是：鎖有人拿 → 「背景上傳中」；鎖沒人拿、outbox 卻不是空的 → 「沒上傳成功」 | 由 `kick_uploader` 回傳 `start()` 的結果，sync 依照結果和鎖的狀態選擇說法。另外，`cli.main` 開頭的「outbox 有 N 筆未上傳」還在，所以一個指令會出現兩句提醒（N10 說要在 2.3 一起改） |
| P6 | inline 時的 `FAILED`，是看「**整個** outbox 和佇列還有沒有東西」。如果 outbox 裡本來就有一筆一直傳不上去的 Y，那麼這次 import 的 X 就算傳成功了，也會回 `FAILED`，X 就會被說成 exit 3 | 2.5 接上 `_emit` 的時候，改成只看**這次寫的那幾個** ULID 還在不在 outbox |
| P7 | `_trim_log` 在 `start()` 裡執行。如果這時已經有一個背景在跑、正在附加寫入，`write_bytes` 會把檔案截斷重寫，那一個背景這段時間寫的幾行就不見了 | 只在拿得到鎖的時候（沒有背景在跑）才修剪；或者交給背景自己，在拿到鎖之後修剪 |
| P8 | 拿不到鎖的那個背景，也會在記錄檔寫「背景上傳開始」「背景上傳結束，剩下 N 筆」，看起來像是跑了一次 | 拿不到鎖就安靜地結束，或者寫一句「已經有一個在跑」 |
| P9 | 被節流的 sync（之後 import 開頭的那一次，2.5）不會 `kick_uploader`。所以上一次背景失敗之後，接下來 5 分鐘內的 import 不會重試舊的那幾筆。但 import 自己存完之後會啟動背景，背景會連同舊的那幾筆一起處理，所以實際上沒有影響 | 不用改；在 design 記一句就好 |

## 結論

骨架的三個關鍵（不接 pipe、不繼承鎖、env 原樣）都做對了，而且有測試；inline 和「sync 不在前景上傳」也都是對的。

- **P1**：補一個測試就好；
- **P2**：在第 2 節改 2.3 的時候一起改；
- **P3**：是過渡期的風險，2.3、2.5 完成之前，第 1 節不要拿去用真的資料；
- **P4～P9**：可以在第 2 節一起處理。
