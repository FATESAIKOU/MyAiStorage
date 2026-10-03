> **審到 HEAD `3915952`**（impl2 的 G1～G3），以及 `16054c5`（G2 的 spec 改寫，PM 選 a）。整合測試第三輪（20 過、11 失敗）impl1 在查，我沒有跑、也沒有看那份記錄。

**沒有 High。G1～G3 都修好了，8 個 mutation 有 7 個被抓到**（沒抓到的那一個是「檔名帶 pid」，屬於防禦性的，不影響結論）。

但 G1 的修法帶出一個 **Medium 的退步（H1）**：
- 計算「晚到的那一筆」時，在鎖裡、在逐筆的 `try` **外面**，對每一個 id 呼叫了 `_split`；
- 所以 `agora push session` 只要有一個 id 寫錯（裸的 `ses_…`、看不懂的前綴），**整個指令**就會以「非預期的錯誤：ValueError」結束；
- 同一批裡寫對的那幾個也**不會送**。以前是只有那一筆算失敗，其他照送。

**修好 H1 就可以歸檔。** 行數 **3,771**，在 3,800 以內。

# Review：T3 歸檔前的最後一關（`3915952`）

2026-10-03，review。
- 在 `git archive 3915952` 的副本裡跑：`compileall` 通過，單元測試 **532 passed**。
- 探測和 mutation 都用 pytest 跑（有 conftest 的隔離），子程序另外傳了 `/tmp` 底下的 `AGORA_CONFIG`／`AGORA_CACHE_DIR`／`AGORA_STATE_DIR`／`HOME`。
- 沒有跑整合測試，沒有碰 Drive，也沒有讀任何真實的 Session。

## G1～G3：修好了

| 項目 | 怎麼修的 | 確認 |
|---|---|---|
| **G1**：晚到的那一筆在鎖裡送 | 在 `with held:` 裡面，`push_outbox` 之後，看 `wanted` 裡有沒有快照之後才出現在 outbox 的；有的話，在**同一把鎖**裡再跑一次 `upload_batch`。迴圈裡那個分支只剩「還在 outbox 就是沒傳成功」。`--files-from` 的清單檔名加上 pid | ✅ 新的測試 `test_push_sends_a_late_entry_while_holding_the_upload_lock`，在每一次 `upload_batch` 被呼叫時檢查有沒有人拿著鎖，兩次都是有。mutation「在放開鎖之後送」「這一輪不送」都被抓到 |
| **G2**：continue 另存的 Y 是 `continue` | `.update` 記號的內容，記下這次寫入的種類：`_finish`（continue 結束和 `recover_pending` 都走這裡）寫 `continue`，edit 和 import 原地更新留空。另存時，種類是 `continue` 就把 relation 設成 `continue`，其他沿用 X 的。spec 在 `16054c5` 改成這個說法 | ✅ 指令驅動的測試 `test_a_continued_session_deleted_elsewhere_is_saved_as_a_continue`（真的跑 `continue`，然後刪掉 Drive 上的 X）。mutation「另存時不改」「`_finish` 不說是 continue」都被抓到 |
| **G3**：提醒不重複 | 只有脫離終端機的背景（`python -m agora.background`，`run(notices=True)`）才把提醒寫進檔案；前景（push、inline）只當下說一次 | ✅ `test_a_rescue_in_the_foreground_is_not_said_twice`，以及 `test_the_next_command_says_where_the_rescued_edit_went`（照指令啟動背景的方式跑）。三個 mutation（前景也寫、背景也不寫、入口沒開 notices）都被抓到 |

## H1（Medium，新的退步）：push 遇到一個寫錯的 id，整個指令就失敗

`cache.push` 裡新加的這一段：

```python
with held:
    staged = store.outbox_ulids(paths)
    left = store.push_outbox(drive, paths)
    if late := [i for i in wanted if _split(i, agents)[1] not in staged
                and (paths.outbox / _split(i, agents)[1]).is_dir()]:
```

`_split` 對兩種 id 會丟 `ValueError`：
- 裸的 `ses_…`／uuid（「是 agent 的 session id，請寫前綴」）；
- 看不懂的前綴（「看不懂的 id」）。

原本這兩種，是在後面逐筆的 `try` 裡被接住，算成那一筆失敗。現在它在 `try` 之外先被呼叫，例外一路丟到 `main`。

實測（pytest 裡的探測，`5faac91` 和 `3915952` 的副本跑同一支）：

| 指令 | `5faac91` | `3915952` |
|---|---|---|
| `push session ses_abcdef123` | stdout `[agora] 寫回 0 個，1 個失敗`；stderr `… 傳不上去：ses_abcdef123 是 agent 的 session id，請寫前綴（opencode: 或 claude:）`；exit 2 | stdout **空的**；stderr `[agora] 非預期的錯誤：ValueError: ses_abcdef123 是 agent 的 session id…（AGORA_DEBUG=1 看細節）`；exit 2 |
| `push session foo:bar` | `寫回 0 個，1 個失敗`，`看不懂的 id：foo:bar` | `非預期的錯誤：ValueError: 看不懂的 id：foo:bar` |
| `push session <好的 agora id> ses_abcdef123` | **`寫回 1 個，1 個失敗`**（好的那一個照送） | **好的那一個也沒送**，`非預期的錯誤：…` |

exit code 剛好都是 2，所以只看 exit code 的測試抓不到。但是：
- 「一批裡某一個失敗，照樣做下一個」是 T1 的行為；
- 訊息變成了「非預期的錯誤」；
- 同一批裡正確的 id 也不送了。

**建議**：計算 `late` 時不要讓 `_split` 的例外跑出來。例如只看寫成 agora id 的那幾個：

```python
def _agora_ulid(i):
    try:
        kind, ulid = _split(i, agents)
    except ValueError:
        return None
    return ulid if kind == "agora" else None
```

然後 `late = [u for u in map(_agora_ulid, wanted) if u and u not in staged and (paths.outbox / u).is_dir()]`。

再加一個測試：`push` 同時給一個好的 id 和一個裸的 `ses_…`，斷言回傳 `(1, 1)`，而且好的那一個在 Drive 上。

## Mutation（8 個，7 個被抓到）

| 改了什麼 | 結果 |
|---|---|
| G1：晚到的那一筆這一輪不送 | ✅ `test_a_session_staged_after_push_looked_goes_up_through_the_batch`、`test_push_sends_a_late_entry_while_holding_the_upload_lock` |
| G1：晚到的那一筆在放開鎖之後送 | ✅ `test_push_sends_a_late_entry_while_holding_the_upload_lock` |
| G1：清單檔名不帶 pid | ❌ 沒被抓到。在鎖都拿對了的前提下，兩輪本來就不會同時跑，所以這只是多一層保險，可以接受 |
| G2：另存時不把 relation 改成 continue | ✅ `test_a_continued_session_deleted_elsewhere_is_saved_as_a_continue` |
| G2：`_finish` 不說是 continue | ✅ 同上 |
| G3：前景也寫提醒檔（說兩次） | ✅ `test_a_rescue_in_the_foreground_is_not_said_twice` |
| G3：背景也不寫提醒檔 | ✅ `test_the_next_command_says_where_the_rescued_edit_went` |
| G3：脫離終端機的入口沒開 notices | ✅ 同上 |

（第一個 mutant 第一次的寫法括號沒對上，編譯不過；改寫之後重跑，被抓到。）

## 行數（算法同 T1-size）

| | `5faac91` | `3915952` |
|---|---:|---:|
| store | 658 | 668 |
| cache | 199 | 201 |
| cli | 756 | 756 |
| background | 72 | 72 |
| tui | 898 | 898 |
| **src 合計** | **3,759** | **3,771**（+12） |

目標 3,800，還有 29 行的空間。

## 還開著的 Low（T3-final2 記過，這次不在範圍內）

- G4：互動模式 delete exit 3 時的說法；
- G5：R7 的「每 10 秒說一次」沒有測試、鎖沒放開時測試會卡住而不是失敗；
- G6：`_put_back` 改名失敗時刪掉 `.done-`；
- V6：列不出 Drive 時，`.update` 照樣傳；
- R6：`uploader_is_running` 靠拿鎖來判斷。

要不要在歸檔時列進 backlog，由 PM 決定。

## 結論

G1～G3 都修好了，各自都有會紅的測試。**歸檔前請修 H1**（push 的批次遇到一個寫錯的 id 就整批失敗）：一個小函式加一個測試就好，修好之後 T3 就可以歸檔。
