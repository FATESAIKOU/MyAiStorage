**全做的話，兩個 adapter 合計淨省約 85 行**：opencode 約 −63（其中 19 行是刪掉「export 找不到時回到專案目錄重試」，要先決定）、claude 約 −32，base.py 會多約 10 行。不刪那 19 行的話，淨省約 65 行。加上 T1-size 的 cli／store／cache 約 75 行，整體大約是 3,121 → **約 2,960**，**還是超過 2,900 約 60 行**。剩下的只能從 tui.py（563 行，還沒 review）找，或者由使用者調整額度。

# Review：opencode.py 與 claude.py 的精簡

2026-10-03，review。對象是 HEAD `f2284e7` 的 `src/agora/agents/{opencode,claude,base}.py`（用 `git archive` 取出來看）。只提意見，沒有改程式；沒有跑整合測試，沒有碰 Drive，也沒有讀任何真實的 Session。行數的算法同 T1-size（不含空行、註解、docstring）：opencode **530**、claude **442**、base **63**。

「省」是估計可以少掉的程式碼行。**低** = 純粹搬動，行為不變；**中** = 行為會有一點改變，或者碰到要小心的路徑；**決定** = 要刪掉一個刻意留下的保險，需要 PM 或使用者點頭。測試直接用到的名字在「測試」欄標出來，改了就要跟著改測試。

## 1. 搬到 base.py 共用

| # | 現況 | 建議 | 省（淨） | 風險 | 測試 |
|---|---|---|---:|---|---|
| B1 | `AGORA_SUMMARIZE_TIMEOUT` 有兩套讀法：opencode 用 `_seconds` 加上 `_summarize_timeout`（看不懂的值會退回預設），claude 用 `_summarize_timeout` 加上 `SUMMARIZE_TIMEOUT_S`，而 claude 的 `float("")` 或 `float("abc")` 會丟出 **ValueError**。這個例外不在 `summarize` 的 except 裡，會變成 cli 的「非預期的錯誤」，算是一個 Low bug | 在 base 放 `env_seconds(name, default)` 和 `summarize_timeout()`，兩邊共用 opencode 那一版 | ~3 | 低（順便修好 claude 那一個 bug） | `C._summarize_timeout`、`oc._summarize_timeout`、`oc.DEFAULT_SUMMARIZE_TIMEOUT` 都要改名 |
| B2 | `"%Y-%m-%dT%H:%M:%SZ"` 寫了三次：`opencode._iso`、`claude.list_sessions`，還有 `cli._now_iso` | 在 base 放 `iso_utc(seconds)` | ~2 | 低 | — |
| B3 | opencode 的 `_folded`（NFKC + casefold），claude 用 `store.normalize`（NFKC + lower） | opencode 也改用 `store.normalize`，這樣兩個 agent 和索引的比對方式就一致了 | 2 | 低～中（ß 這類字元的結果會不一樣；統一反而是好事） | — |
| B4 | 兩個 `collect` 都是「沒有 id 就丟錯 → 讀回來 → 訊息數 ≤ before_count 就回傳 None」 | claude 的 `collect` 改成 `_warn_cleared(…)` 之後直接用 `self.export(id, launch.cwd)` 再比對 `message_count`，和 opencode 的寫法一樣 | ~4 | 低（訊息數沒有增加時，會多打包一次 aux，可以忽略） | — |
| B5 | `print(f"[agora] …", file=sys.stderr)` 在 adapter 裡出現 4 次，store 和 cache 也各有一份 | 在 base 放一個 `warn`（和 T1-size 的 C3 一起做） | ~2 | 低 | — |

另外有一件**只是不一致、行數不變**的事：`last_message` 的上限，claude 取的是**最後** 2,000 字（`text[-PREVIEW_MAX:]`，測試有鎖住），opencode 取的是**前面** 2,000 字。預覽要看哪一段，應該統一一個規則，常數也放到 base。

## 2. opencode.py

| # | 位置 | 建議 | 省 | 風險 | 測試 |
|---|---|---|---:|---|---|
| O1 | `_session_directory`（14 行）加上 `export` 的重試（約 6 行） | 這是為了「**假如**以後的 opencode 版本只能從自己的專案目錄 export」而留的保險；docstring 也說 1.18 從哪裡都能 export，而且是量過的。刪掉之後，`export` 只剩一條路 | **~19** | **決定**：這是刻意留下的保險。刪了就要一起刪 3 個測試（`test_export_retries_in_the_projects_own_directory` 等）和 fake 的 `FAKE_OPENCODE_EXPORT_SCOPE` | 3 個測試加上 fake |
| O2 | 打開 db 時的 `_open_readonly` → `try … except sqlite3.Error: _warn_schema() … finally: close()`，寫了 4 次（`_session_directory`、`_read_sessions`、`search_text`、`last_message`） | 抽成 `@contextmanager def _db()`：yield 連線或 None，自己 catch `sqlite3.Error` 並警告，最後關掉。generator（`search_text`）在 with 裡面一樣能用 | ~10 | 低～中：每個地方失敗時的回傳值（`[]`／`None`／什麼都不 yield）還是由呼叫端決定 | 不受影響 |
| O3 | `search_text` 和 `last_message` 都用 3 次 `_field` 來判斷「是 text part、不是 synthetic、有文字」 | 抽成 `_said(blob) -> str \| None` | ~4 | 低 | — |
| O4 | `_event_session_id` 和 `_last_reply` 各自解析一次 event 的 JSON 行 | 抽成一個 `_event(line) -> dict \| None`，兩邊共用 | ~5 | 低 | `_last_reply` 的介面不變 |
| O5 | `_discard` 和 `_drop_summary_session` 都是 `session delete <id>` 再處理失敗 | 抽成 `_delete(id, cwd) -> str \| None`（失敗時回傳細節） | ~4 | 低：一律只按 id 刪的規則不變 | — |
| O6 | `proc.stderr.decode("utf-8", "replace").strip()[-N:]` 出現 5 次 | 抽成 `_err(proc, n=300)` | ~3 | 低 | — |
| O7 | `_warn_schema` 用一個 global 旗標保證只印一次 | 改用 `@functools.cache` | ~3 | 低 | — |
| O8 | 只用一次的 helper：`_data_home`、`_salt_for`、`_like_pattern`、`_sweep_pending` | 直接寫在呼叫的地方 | ~6 | 低 | — |
| O9 | `_model_of` 裡 `if _last_model(payload): return _last_model(payload)` 算了兩次 | 改成 `return _last_model(payload) or _str(...)` | ~2 | 低 | — |
| O10 | `_timeout` 與 `DEFAULT_CLI_TIMEOUT` | 和 B1 一起改用 `env_seconds` | ~1 | 低 | — |

**不建議動**：`reidentify`、`_id`、`_remap`、`native`、`_session_info`、`_import_verified`、`_summarizing`。這些是 OC1、OC2、OC6、V5、Y6 這幾個量過的陷阱，每一行都有它的理由，省下來的那幾行換不到它們的風險。`list_sessions` 和 WAL key 的 memo 也一樣（T15）。

## 3. claude.py

| # | 位置 | 建議 | 省 | 風險 | 測試 |
|---|---|---|---:|---|---|
| C1 | `_peek_session`、`last_message`、`search_text` 三個迴圈都寫了一次：`_TYPE_RE.search` → 檢查種類 → `json.loads` → `_is_noise` → `_line_text` | 抽成 `_said(line, kinds) -> (kind, obj, text) \| None`，三個地方共用 | ~10 | 低～中：`_peek_session` 另外還要 cwd 和 summary，所以它要多拿 `obj` | `_peek_session` 被 mock 計次，名字要留著 |
| C2 | 標題有兩套規則：`_session_title`（export 用）是「只要有 summary 就用 summary，否則用第一個 user」，`_peek_session`（清單用）是「前 40 行裡哪個先出現就用哪個」，所以同一個 session，匯入分頁顯示的標題和匯入之後的標題可能不一樣 | 統一成一套規則，由 `_peek_session` 的那段和 `_session_title` 共用 | ~5 | 低～中（標題的結果可能會變；統一是好事） | 要補一個標題一致的測試 |
| C3 | `_user_text`（用 `""` 接起來）和 `_line_text`（用換行接起來，再 strip）幾乎一樣 | `_is_noise` 和 `_session_title` 改用 `_line_text(o) or ""` | ~3 | 低（`_is_noise` 只看前綴，不受影響；標題原本就截到 60 字） | — |
| C4 | 只用一次的 helper：`_json_str`、`_forget_other_keys`、`_rewrite_aux_jsonl` | 直接寫在呼叫的地方 | ~5 | 低 | — |
| C5 | B1、B2、B4、B5 在 claude 這一邊省下的 | （在第 1 節已經列過） | ~9 | 低 | 見第 1 節 |

**不建議動**：`_split_lines`（S10）、`_unpack_aux` 的路徑檢查（CL14）、`encode_project_dir`（CL1）、`_warn_cleared`（CL5）、`summarize` 的旗標（Y3）、`find_jsonl` 的 `hint_dir`（CL12，整合測試靠它才不會掃到真實的目錄名稱）。

## 4. 合計

| 檔案 | 低／中 | 加上「決定」那一項 |
|---|---:|---:|
| opencode.py（O2–O10，加上 B1–B3、B5 在 opencode 這一邊的部分） | ~44 | ~63（加 O1） |
| claude.py（C1–C5） | ~32 | ~32 |
| base.py（多出來的共用函式） | +~10 | +~10 |
| **淨省** | **~66** | **~85** |

可以排優先順序：**O1（需要決定）、O2、C1、C2** 這四項就占了大約 44 行；其他都是每項 2～5 行的小整理。

整體來看，3,121 − 75（T1-size）− 85 ≈ **2,961**，全部都做還是超過 2,900 約 60 行。要真的回到 2,900 以內，有三條路：(a) tui.py 再做一輪精簡（563 行，還沒看過），(b) 使用者把額度放寬到大約 3,000，(c) 兩條都做。這需要使用者決定。

## 這次做了什麼

讀了 HEAD 的 `base.py`、`opencode.py`、`claude.py` 全文；用 `grep -w` 數了每個 helper 在 `src/` 和 `tests/` 被用到的次數，找出只用一次的、被測試直接引用或 mock 的、以及沒有呼叫者的。沒有找到完全沒有呼叫者的函式；最接近的是 claude `export` 的 `hint_dir`，`src` 裡沒有人傳它，但整合測試要用，所以保留。用 T1-size 裡那支腳本量了每個函式的行數。沒有改程式，沒有跑整合測試，沒有碰 Drive，也沒有讀任何真實的 Session。
