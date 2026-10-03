**沒有 High。** 有 5 個 Medium：
- **M1**：Esc 的升級只看 agora 這個子程序本身，它一結束就停了，留在同一個 process group 裡的 agent 還會繼續跑（已在 scratchpad 實測重現）。
- **M2**：未匯入頁同時勾了 opencode 和 claude 時，Esc 只會停掉第一段，第二段還是會自動開始。
- **M3**：在等待視窗裡按 `ctrl+q`，或者關掉終端機，子程序都不會收到任何訊號，會在背景繼續跑（已實測）。
- **M4**：Esc 和它的升級（1.5）完全沒有測試，連假物件的版本都沒有。
- **M5**：opencode summarize 被 SIGINT 打斷時不會殺掉 opencode，而 `material-*.md`（裡面是整份要約材料）在 SIGTERM／SIGKILL 之後會一直留著。

# Review：openspec change tui-batch-actions 第 1 節（impl1）

2026-10-03，review。對象：`5f4af72`、`18be495`、`2b5eef3`、`2a16903`、`eb181d4`，對照 `specs/interactive-mode/spec.md` 的「進度與中斷」、這個 change 的 `design.md`，以及 `docs/review/T2.md` 的 V1、V2、V5、V7、V8。只提意見，沒有改程式。單元測試：在 `git archive HEAD` 取出的副本跑 `tests/unit/test_tui.py` 和 `tests/unit/test_agent_opencode.py`，**114 passed**；3 次裡有 1 次出現 `PytestUnhandledThreadExceptionWarning`，原因見 M3。沒有跑整合測試，沒有碰 Drive，沒有讀任何真實的 Session，也沒有執行不帶參數的 `agora`。

## 逐項確認

| 重點 | 結果 |
|---|---|
| 子程序與 process group | ✅ `[sys.executable, "-m", "agora.cli", …]` 加上 `start_new_session=True`，`killpg(proc.pid, sig)` 送的就是整個 group；opencode、claude 的 summarize 和 rclone 都**沒有**另開 session，所以會在同一個 group 裡被送到。⚠️ M1、M3 |
| stdin（V1） | ✅ `stdin=DEVNULL`，孫程序也會繼承；`PYTHONUNBUFFERED=1`、`bufsize=1`、stderr 併入 stdout。`test_a_child_process_cannot_take_the_users_keystrokes` 斷言了這些參數 |
| SIGINT → SIGTERM → SIGKILL（V2） | ✅ 順序對，間隔 5 秒，都是 `killpg`，`ProcessLookupError` 會被接住（它是 OSError，會改走 `send_signal`）。⚠️ M1（停止條件）、L3（連按 Esc），而且沒有測試（M4） |
| 進度只讀 `[agora] k/N`（V8） | ⚠️ 部分做到：只有 `[agora]` 開頭的行會被讀，但 `k/N` 可以出現在那一行的**任何位置**，也沒有檢查 1 ≤ k ≤ N（L1） |
| opencode summarize 提早寫 pending（V5） | ✅ 讀事件的執行緒一看到 `sessionID` 就寫 `pending-<id>`；`FAKE_OPENCODE_SUMMARIZE_HOLD` 的測試是真的子程序，而且如果還是等 run 結束才寫就會失敗，這是有效的測試。⚠️ M5、L4 |
| 測試是否用到真的子程序（V7） | ❌ TUI 這一邊全部用 `FakeProc`；4.1b（真的子程序測 Esc）還沒做。這可以留到第 4 節，但 1.5 已經打勾了，卻連一個 Esc 的假物件測試都沒有（M4）。opencode 那一邊是透過 fake_opencode 跑真的子程序 ✅ |
| K3（1.3） | ✅ `Busy` 不再 `redirect_stdout`，sync 和授權改用 `say`。L5：sync 裡面呼叫到的 `push_outbox`／`quarantine` 還是用 `store._warn` 寫 stderr，那幾行在畫面上看不到 |
| 一個 agent 一個指令（1.2） | ✅ 一個指令帶多個 `--external-session-id`，cli 用 `append` 收；`test_import_runs_one_command_per_agent` 有蓋到。⚠️ M2 |

## Medium

### M1：升級只看 agora 本身，不看整個 group

`action_stop` 和 `_again` 都用 `self.proc.poll() is not None` 來判斷「已經停了」，而 `Run.read` 一讀到 stdout 的 EOF 就 `dismiss`，計時器也跟著這個畫面一起消失。可是 agora 自己可能**先**結束：cli 接住 KeyboardInterrupt 之後 return 130，或者 agora 在 5 秒時被 SIGTERM 殺掉。這時 group 裡的 agent（例如不理 SIGINT，或者收尾很慢的 opencode）就不會再收到 SIGTERM／SIGKILL。spec 寫的是「都對整個 process group」，也「包含它啟動的 agent」。

我在 scratchpad 實測過：leader 收到 SIGINT 之後 exit 130，孫程序（`signal.SIG_IGN` 掉 SIGINT）還活著，`os.killpg(pgid, 0)` 也還成功，要再送一次 `SIGKILL` 才會消失。

**建議**：判斷「停了」改用 group 還在不在：`os.killpg(pgid, 0)` 丟出 `ProcessLookupError` 才算停了。Esc 之後，就算 agora 先結束，也要讓升級繼續跑到 group 消失為止（`read` 結束時，如果 `stopping` 為真，就先不要 dismiss，或者把升級交給 App 層級的計時器）。另外，`opencode._summarizing` 收到 KeyboardInterrupt 時也應該直接 `proc.kill()`（見 M5），這樣大部分情況光靠 SIGINT 就夠了。

### M2：中斷第一段之後，第二段還是會開始

`2a16903` 把 `act` 改成一次只跑一個 argv，「中斷了就不開下一個」的那個 `break` 也一起拿掉了；但 `action_primary` 在未匯入頁有自己的 `for agent in …: await self.act(…)` 迴圈，而 `act` 不會告訴呼叫端它被中斷了。結果是：勾了 opencode 和 claude 的列 → 開始匯入 → 在 opencode 那一段按 Esc → 結果視窗顯示「已中斷」→ 關掉之後，**claude 那一段會自動開始**。使用者按 Esc 的意思是停下這個動作，不是只停一半。

**建議**：`act` 回傳 `stopped`（或者 code），迴圈一看到中斷就 `break`；補一個測試：第一段的假程序回傳 130，斷言 `started` 只有一筆。

### M3：在等待視窗裡 `ctrl+q`，或關掉終端機，子程序會在背景繼續跑

Textual 8.2.8 的 `App.BINDINGS` 有 `ctrl+q → quit`，而且 `priority=True`，所以在 `Run` 視窗上面也有效。實測（只在 scratchpad 的副本裡加了一個探測測試，repo 沒有動）：`ctrl+q` 之後 `app.is_running == False`，假程序沒有收到任何訊號，`poll()` 仍然是 None；讀輸出的執行緒之後呼叫 `call_from_thread` 時丟出 `NoActiveAppError`，這也就是那個時有時無的 `PytestUnhandledThreadExceptionWarning`。用真的 `Popen` 時，因為 `start_new_session=True`，子程序脫離了終端機，關掉終端機視窗時的 SIGHUP 也送不到它。所以 merge、delete 會在使用者以為已經離開了的時候，繼續在背景寫要約、刪 Drive。

**建議**：`Run` 自己綁一個 `ctrl+q`（priority），把它當成 Esc，等 group 停下來之後再離開；`tui.main` 在 `App.run()` 結束時（`finally`），以及收到 SIGHUP 時，對還活著的子程序 group 送 SIGTERM。`read` 裡的 `call_from_thread` 也要用 `contextlib.suppress(Exception)` 包起來。

### M4：Esc 和升級沒有測試

`test_tui.py` 裡完全沒有 `escape`，也沒有 SIGINT、SIGTERM 或 130。`FakeProc.send_signal` 和 `Run.signals`、`Run.STOP_AFTER` 都準備好了，卻沒有任何測試用到。spec 裡有 MUST 的這一條，目前只有程式在支撐。**建議**（用假物件就可以做到，不必等 4.1b）：
1. 按 Esc → `signals == [SIGINT]`，結果視窗的標題有「已中斷」，狀態列寫著「重跑同一個動作會接著做」；
2. `STOP_AFTER=0.1`，假程序不理訊號 → `signals == [SIGINT, SIGTERM, SIGKILL]`；
3. 程序在 SIGINT 之後就結束 → 不會再送 SIGTERM；
4. M2 和 M3 的測試。

4.1b 用真的子程序，就照 T2.md V7 的寫法，外加一個「leader 先結束、孫程序不理 SIGINT」的版本（M1）。

### M5：opencode summarize 在中斷時留下的東西

- `_summarizing` 只在 `TimeoutExpired` 時才 `proc.kill()`。收到 KeyboardInterrupt 時，`finally` 會先 `join` 兩個讀取執行緒（各等 5 秒），這時 opencode 還活著，stdout 不會 EOF，所以 agora 至少要卡 5 秒，接著就被 TUI 的 SIGTERM 殺掉，cli 那句「中斷了；…會自動補存」也就印不出來。**建議**：`except BaseException: proc.kill(); proc.wait(); raise`。pending 記錄已經提早寫好了，殺掉 opencode 是安全的。
- `material-<id>.md` 是在 `summarize` 的 `finally` 裡刪的。被 SIGTERM 或 SIGKILL 時，`finally` 不會執行，那份檔案（**內容是被要約的 Session 全文**）就會留在 `<state>/summarize/`；`_sweep_pending` 只會清 `pending-*`，所以它永遠不會被清掉。**建議**：`_sweep_pending` 順便刪掉 `material-*`（那個目錄是 agora 專用的，裡面不會有別人的檔案）。

## Low

| # | 問題 | 建議 |
|---|---|---|
| L1 | `PROGRESS = ^\[agora\].*?\b(\d+)/(\d+)\b`：只要是 `[agora]` 開頭的行，`k/N` 在哪裡都算。實際會遇到的例子：pull／push 失敗的訊息裡帶著 rclone stderr 的尾巴，`[agora] … 拉不到：rclone copyto 失敗（rc=1）：2026/10/03 12:00:00 ERROR …`，這會讓進度條變成 2026／10。V8 的測試只測了「不是 `[agora]` 開頭的日期」 | 照 V8 的寫法錨定成 `^\[agora\] \S+ (\d+)/(\d+)(\s\|$)`（也就是 cli 和 cache `_progress` 實際的格式），並且要求 `1 ≤ k ≤ N`；補一個測試：`[agora] x 拉不到：… 2026/10/03` |
| L2 | `stopped = code in (130, -SIGINT, -SIGTERM, -SIGKILL)`：一個自己被 OOM 殺掉（-9）的子程序，也會顯示成「已中斷」 | 用 `Run.stopping` 判斷（一起 dismiss 回來） |
| L3 | 連按 Esc：每按一次都會再送一次 SIGINT，而且會把 `armed` 重設回 `[TERM, KILL]`。第二次 SIGINT 可能打斷 agora 正在做的收尾（KeyboardInterrupt 發生在 `finally` 裡）；舊的計時器又會讓 SIGKILL 比預定的還早送出。`_arm` 的 lambda 在**觸發的時候**才去讀 `self.armed[0]`，清單空了就會 IndexError | 已經在 `stopping` 的時候，再按 Esc 就忽略（或者明確定義成「直接跳到下一步」）；把 `sig` 在 arm 的時候就綁進 lambda |
| L4 | `test_the_record_is_written_even_when_the_run_is_interrupted` 其實沒有中斷任何東西：舊的實作在 `_drop_summary_session` 裡也會呼叫 `_remember_summary_session`，所以這個測試在 1.4 之前一樣會過。真正有效的是 `…_while_the_run_is_still_going` 那一個 | 改成真的中斷：fake 用 HOLD 卡住，等 `pending-*` 出現之後就 kill 跑 summarize 的子程序，斷言記錄還在，下一次 summarize 會把它刪掉 |
| L5 | `sync(warn=say)` 只蓋到 sync 自己的訊息；它呼叫的 `push_outbox`（上傳失敗）和 `quarantine`（壞檔）還是用 `store._warn` 寫 stderr。Textual 會吃掉 stderr，所以等待視窗裡看不到「outbox 的 X 上傳失敗」 | 把 `say` 傳進 `push_outbox`，或者讓 `_warn` 讀一個 contextvar |
| L6 | `text=True` 沒有指定 encoding。`read()` 如果遇到解碼錯誤（或任何例外），執行緒就會結束，而且**永遠不會 dismiss**，等待視窗就會一直卡著 | `encoding="utf-8", errors="replace"`，env 加上 `PYTHONIOENCODING=utf-8`；`read` 用 `try/finally` 保證一定會 dismiss |
| L7 | spec 寫「**結果視窗** MUST 依 exit code 說明」，但「部分失敗」「已存進 outbox」只寫在關掉之後的狀態列，結果視窗只有標題的「（已中斷）」和顏色 | 結果視窗的第一行也寫上同一句說明 |

## 沒有問題的地方

- 等待視窗是 modal 的：我實測在 `Run` 上按 Enter，**不會**觸發 App 的 `action_primary`（畫面堆疊還是 `[Screen, Run]`，也只啟動了一個子程序）。所以 App 層級那些 priority 的按鍵不會在執行途中再開一個動作，只有 Textual 內建的 `ctrl+q` 例外（M3）。
- 逐行讀加上 `PYTHONUNBUFFERED`，agora id 和進度會即時出現；stderr 併入 stdout，所以順序和實際的輸出順序一樣。
- 中斷之後會 `reload()`，狀態列寫著「重跑同一個動作會接著做」✅；delete 的續跑依賴 S1-4／S1-4b，在 T1 已經修好了 ✅（V3）。
- design 對 V5 的 Risk 已經改成正確的說法 ✅。

## 這次讀過、跑過的東西

5 個 commit 的 `src` 和測試 diff、HEAD 的 `tui.py`（`Run`、`Busy`、`spawn`、`act`、`action_primary`）、`opencode.py` 的 `summarize`／`_summarizing`／`_sweep_pending`、`tests/unit/test_tui.py` 的 `FakeProc` 和相關的測試、`fake_opencode.py` 的 HOLD、這個 change 的 design 和 tasks、T2.md 的 V1～V8。在 scratchpad 做了兩件事：(1) 一支 process group 的 repro，用的是 `python -c` 的假 leader 和假孫程序，不是 agora，也不是任何 agent；(2) 在 HEAD 副本裡加了兩個探測測試（Enter、`ctrl+q`），repo 沒有動。單元測試 114 passed。沒有跑整合測試，沒有碰 Drive，沒有讀任何真實的 Session，也沒有執行不帶參數的 `agora`。
