# #422 設計筆記：reaper 與 agent 的啟動順序（孤兒 agent 窗口）

- 單：#422（MAS，c；起因是 #415 量到的窗口，#229 的後續）。
- 作者：設計作者（隊員）；介面決定與核准：專案架構師。2026-11-07。
- 形式：不開 openspec change、沒有主 spec delta（見 §8）；決策見 `docs/architecture/decisions.md` 的 D-27。

## 1. 結論

**先起 reaper，再 spawn agent；agent 的 pid 由 agent 子行程自己（fork 之後、exec 之前的 `preexec_fn`）寫進 reaper 的 pipe，不靠 agora 在 `Popen` 返回後告知。**

- 「`Popen` 返回 → reaper 就位」的窗口整個消失，不是縮小：
  - reaper 在 agent 被 fork 之前就握著讀端；
  - agent 子行程存在，就在 exec 之前（`preexec_fn`）把自己的 pid 寫進 pipe；寫端不可繼承，在 preexec 之後關閉（agent 的 Popen 帶 `pass_fds`，所以在 close_fds 那一步，最遲在 exec 時）；
  - 所以 agora 不論死在哪個時點，reaper 讀到 EOF 時要嘛已知 agent 的 pid，要嘛 agent 根本沒出生。
- 不需要額外的「就位」信號：`Popen(reaper)` 返回時 reaper 已 exec、握著讀端；寫進 pipe 的位元組留在 kernel 緩衝，不必等 reaper 開機。
- 三行 stderr（`agora-session`、`agora-port`、`agora-agent-session`）的內容、順序、出現時機都不變。對外可見的只有兩點：
  - reaper 的 argv 不再帶 `--pid`；
  - 「agora 被 SIGKILL 時 agent 不變孤兒」從「reaper 起來之後才成立」變成「任何時點都成立」。限制同 #229：reaper 自己起不來、或和 agora 一起死。
- 已實測（CPython 3.12.3，Linux，暫存目錄探針）：
  - `preexec_fn` 執行時，不可繼承的寫端仍開著、寫得進去（`close_fds` 在 preexec 之後才關）；
  - 子行程在 preexec 睡 1 秒期間父行程關掉寫端，reaper 要等子行程跑完 preexec、寫端關掉之後才讀到 EOF，而且已收到 `P<pid>`（非作者審查的探針另外量到 EOF 早於 exec 後的第一個輸出，與 close_fds 的時點一致）。

## 2. 現況（已對照目前 main 驗證）

- `src/agora/cli.py:2321-2327`：headless 時印 `agora-session`、`agora-port`（opencode）、`agora-agent-session`，都在 agent spawn 之前。
- `src/agora/cli.py:2376-2379`：`subprocess.Popen(launch.argv, …, pass_fds=(lock.fileno(),), preexec_fn=<還原 SIGINT>)`（`preexec_fn` 在 2378），**之後**第 2379 行才 `guard = orphan_guard.start(proc.pid)`。
- `src/agora/orphan_guard.py:249-294`：`start()`：
  1. `os.pipe()`；
  2. `start_time(agent_pid)`（跑一次 `ps -o lstart=`）；
  3. `_spawn([python, -m, agora.orphan_guard, --pid, N, --fd, R, --start, …], start_new_session=True, pass_fds=(R,))`；
  4. 關讀端。

  窗口＝步驟 1 到 3，加上 `Popen(agent)` 返回之後的整段。
- `orphan_guard.main()`（`:195-217`）在 EOF 後用 `if NORMAL in data`（`:212`）判斷道別；`NORMAL = b"D"`（`:58`）。
- 互動與 headless 共用同一個 `Popen`（`cmd_continue` 只有這一處），修一處兩種都修到。
- `cmd_continue` 的 `finally` 呼叫 `guard.done()`（`cli.py:2480`）。
- 主 spec 沒有 #229 的 requirement（`grep -rn -i 'orphan\|reaper\|孤兒' openspec/specs` 只有無關的「孤兒段」）；三行 stderr 也不在主 spec，說明在 `docs/spike/opencode.md:1503-1509`。#229 的說明只在 `docs/design.md:384`。
- `store._fault(point)`（`src/agora/store.py:114`）在 `AGORA_TEST_FAULT` 等於該點時 `os._exit(137)`，不跑 finally、不寫 `D`，等同 SIGKILL。
- 依賴三行的測試：`tests/unit/test_continue_headless.py:187`、`:343`；`tests/unit/test_cli_sync.py:1849`；`tests/integration/test_opencode_v2_acceptance.py:153-164`；`tests/perf/measure_continue_start_rss.py:188`；`tests/unit/test_orphan_guard.py:432`（#415 加了等 agent 再等 reaper）。外部消費者是 MLP sidecar 的 headless driver；推論它讀 port 與 session id 後自己輪詢 HTTP，不把那一行當成「agent 已起」。行的時機不變，所以不影響本單。

## 3. 決定與理由

### 3.1 `orphan_guard`（依賴仍為「無」）

- `arm() -> Guard | None`：建 pipe，spawn reaper `[sys.executable, "-m", "agora.orphan_guard", "--fd", R]`；`start_new_session`、stdio 接 `/dev/null`、`pass_fds=(R,)`、cwd 規則同現行。之後關讀端，回 `Guard(proc, W)`；失敗時和現在一樣印 `_warn` 那一行、回 None。**不跑 ps、不需要 agent pid。**
- `Guard.child_hook() -> None`：**只在 agent 子行程的 `preexec_fn` 裡呼叫**。
  - 先把 SIGPIPE 設成 SIG_IGN，再 `os.write(W, b"P%d\n" % os.getpid())`，吞掉 `OSError`，最後把 SIGPIPE 還原成 SIG_DFL。
  - 只用 `os.getpid`、`os.write`、`signal.signal`，不 import、不配置大型物件。
  - 理由：`Popen` 的 `restore_signals` 在 preexec 之前就把 SIGPIPE 還原成預設。reaper 若已經死了，沒有 SIG_IGN 的話，這一寫會讓 agent 在 exec 前被 SIGPIPE 殺掉，`Popen` 卻照常返回。
- `Guard.started(pid: int) -> None`：agora 在 `Popen` 返回後呼叫。跑 `start_time(pid)`，有值就寫 `S<lstart>\n`，盡力而為、吞 `OSError`。這是現行 L2（在 agora 這邊讀 start time）的延續：agora 活著時 agent 的 zombie 由它持有，讀到的一定是 agent。這一筆不能省：reaper 開機較慢，讀到 `P` 時 agora 可能已經 `wait()` 回收了很快結束的 agent，pid 也可能已被重用；`S` 是在任何 `wait` 之前讀的，所以覆蓋 reaper 自己的讀值，陌生行程對不上就不殺。
- `Guard.done()`：語意不變，寫 `D\n` 後關閉並等 reaper（≤5 s）。
- `start(agent_pid)`：**移除**。cli 只用 `arm()`、`child_hook()`、`started()`、`done()`。
- reaper `main()`：
  - 參數只剩 `--fd`（必填）與 `--grace`（選用，語意不變）；`--pid`、`--start` **移除**。身分只來自 pipe 裡的 `P`／`S`。既有直接測 reaper 的單元測試（`test_a_sigkilled_agora_terms_the_agent`、`test_an_agent_that_ignores_term_is_killed`、`test_a_reused_pid_is_not_signalled`）改成往 pipe 寫 `P<pid>\n`（需要時加 `S<lstart>\n`）代替 `--pid`／`--start`，**行為斷言不變**。兩條 agora 端的測試要**改寫斷言**：
    - `test_the_reaper_is_told_the_agents_start_time`：原本斷言 argv 帶 `--start`，改成 `g = arm()`、`g.started(agent.pid)`、`g.done()` 之後，替身 reaper 的 `received() == b"S" + os.fsencode(recorded) + b"\nD\n"`，其中 `recorded = orphan_guard.start_time(agent.pid)`，在 `started()` 之前讀；argv 斷言改成只有 `--fd`（以及有給時的 `--grace`），**沒有** `--pid`／`--start`。
    - `test_a_normal_leave_says_goodbye_and_kills_nothing`：`orphan_guard.start(...)` 改成 `arm()`，加上 `Popen(..., preexec_fn=g.child_hook)` 與 `started()`，斷言不變（正常離開不殺）。
  - **逐筆處理**記錄，不是讀到 EOF 才解析：收到 `P<pid>` 立刻以自己的 `start_time(pid)` 記下身分；收到 `S<…>` 以它覆蓋；收到 `D` 記為正常離開。
  - EOF 時：有 `D` → exit 0，不殺；沒有 `P` → exit 0，不殺（agent 沒出生）；有 `P`、沒有 `D` → 身分是 None 時不殺，否則 `stop_orphan(pid, recorded, grace)`。

### 3.2 線上格式（pipe 協定 v2）

- 每筆一行，`\n` 結尾，每筆一次 `os.write`。最長的 `S` 行約 30 bytes，小於 PIPE_BUF（POSIX 下限 512），所以子行程與 agora 同時寫也不會交錯。
- 記錄：`P<十進位 pid>`、`S<ps lstart 原文，不透明位元組，不含 \n>`、`D`。reaper 只拿 `S` 和自己的 `start_time()` 做位元組比較，不解析。不認得的行忽略；沒有 `\n` 的殘行忽略。 編解碼：`start_time()` 回 `str`；agora 寫 `b"S" + os.fsencode(start_time(pid)) + b"\n"`，reaper 用 `os.fsdecode` 還原後，和自己的 `start_time()`（`str`）比較。兩邊在同一個 env 下跑 `ps`，locale 相同，`fsencode`／`fsdecode` 能來回還原（surrogateescape）。這裡說的「位元組比較」指的就是這個來回，不另外解析。
- **正常離開的判準改成「有一行恰為 `D`」**，不再是 `NORMAL in data`。理由：`S` 行的 lstart 可能含大寫 D（`Dec`），舊判準會把它誤當成道別（舊協定只有 `D`，舊判準沒問題；協定改成帶 `S` 行之後才必須改）。

### 3.3 cli（`cmd_continue`）

1. 印三行（不動）→ 裝 SIGTERM／SIGHUP handler（不動）。
2. 進 `try` 之後、`Popen` 之前：`guard = orphan_guard.arm()`。guard 必須在 `finally: guard.done()` 的保護範圍內，所以 arm 與 `Popen` 之間任何例外都會道別。
3. `Popen(..., preexec_fn=_agent_preexec)`，`_agent_preexec` 依序：還原 SIGINT → 測試注入點 `agent-child-before-pid`（§6）→ guard 不是 None 時呼叫 `guard.child_hook()`。
4. `Popen` 返回之後：`store._fault("after-agent-popen")` → `guard.started(proc.pid)` → 原本的 `term_pending` 轉送。
5. 其餘（等待、寫回、`finally` 的 `guard.done()`）不變。

依賴方向不變：`cli → orphan_guard, store`；`orphan_guard →（無）`。注入點放在 cli 的 preexec 包裝裡，不讓 `orphan_guard` 依賴 `store`。

## 4. 被否決的選項

| 方案 | 做法 | 窗口 | 判定 |
|---|---|---|---|
| **A'（選）** | 先 `arm()` 起 reaper（無 pid）；agent 的 `preexec_fn` 自寫 `P<pid>`；agora 在 `Popen` 後補 `S<lstart>`（盡力） | 無 | 採用 |
| A | 先起 reaper、等就位，`Popen` 後由 agora 寫 pid 給 reaper | 縮小成「`Popen` 返回 → `os.write(pid)`」，仍非零 | 否決：沒有關掉窗口。被 OOM 掃到的正是這種「已 fork、還沒做下一步」的行程 |
| B | agent 由 reaper（或一個小 launcher）spawn，agora 不是 agent 的父 | 無 | 否決：agora 會失去 `proc.wait()`／returncode（#54／#76 的 `agora-agent-failed rc=`）、SIGTERM／SIGHUP 轉送（#107）與 `_GentleStop.take_down`，還有 lock fd 的 `pass_fds`；這些都要改成代理協定，改動面是本單的好幾倍，而且 reaper 從「極小、只等 EOF」變成常駐中介 |
| C | 只把 `agora-agent-session` 延到 reaper 就位後才印 | 真窗口不變 | 否決：只修測試看得到的時序，不修產品；還會改變外部可見的行時機 |
| D1 | Linux `prctl(PR_SET_PDEATHSIG)` 在 agent 的 preexec 設 | Linux 上無窗口 | 否決（不作主修）：macOS 沒有對應；只對直接子行程，沒有 TERM→KILL 的寬限，也不收孫行程；「父執行緒」結束也會觸發（agora 若改在 thread 裡 `Popen` 會誤殺）；行為依平台分岔。可列為日後 Linux 加固，不在本單 |
| D2 | reaper 用 `ps` 輪詢 agora 的子行程來找 agent | 無 | 否決：要猜哪一個子行程是 agent（同時還有 round child、上傳器、rclone），會殺錯行程；違反 #229「認不出來的行程一律不殺」 |

## 5. SIGKILL 落在各時序點的行為

前提：reaper 能起來。agora 死於 SIGKILL、OOM 或 `os._exit`，都沒有 finally，也不寫 `D`。

| # | agora 死在 | 新行為 | 舊行為 |
|---|---|---|---|
| T0 | `arm()` 之前（含 `before-agent-launch`） | 沒有 reaper、沒有 agent；pending 照舊由下一條指令補 | 同 |
| T1 | `Popen(reaper)` 進行中（reaper 已 fork、未 exec） | reaper 的子行程在 close_fds／exec 時關掉繼承來的寫端 → 讀到 EOF、沒有 `P` → exit 0；沒有 agent | （舊順序不存在此點） |
| T2 | reaper 已就位、agent 未 fork（`after-guard-armed`） | reaper EOF、沒有 `P` → exit 0，不殺任何行程 | 舊順序：此時 agent 已在、reaper 未起 → **孤兒** |
| T3 | agent 已 fork、preexec 還沒寫 `P`（`agent-child-before-pid`） | 子行程照樣寫 `P`、exec；寫端在 preexec 之後關（close_fds，最遲 exec） → reaper 讀到 `P` 加 EOF → TERM，寬限後 KILL | **孤兒** |
| T4 | `P` 已寫、agent 已 exec、`Popen` 未返回 | 同 T3 | **孤兒** |
| T5 | `Popen` 返回、`started()` 之前（`after-agent-popen`） | reaper 已有 `P`（身分由 reaper 自己記）→ 收 | **孤兒**（即 #415 的窗口） |
| T6 | `started()` 之後、agent 執行中 | 收（身分以 `S` 為準） | 收 |
| T7 | agent 已結束、寫回中 | `start_time(pid)` 為 None 或不同 → "gone"，不殺 | 同 |
| T8 | `D` 之後 | reaper 已 exit 0 | 同 |
| T-f | `Popen(agent)` 的 fork 本身失敗（`OSError`） | 沒有子行程、沒有 `P`；Popen 拋例外 → finally 寫 `D`；若 agora 在 `D` 之前被殺，reaper 讀到 EOF、沒有 `P` → exit 0 | 舊：沒有 reaper（`start` 在 Popen 之後） |
| T-e | agent 已 fork、`P` 已寫，但 exec 失敗（執行檔不存在等） | 子行程 exit 255、被 Popen 回收；Popen 拋例外 → finally 寫 `D` → reaper exit 0。若 agora 在 `D` 之前被殺：reaper 有 `P`，但那個 pid 已不在（或已被重用，lstart 不同）→ 身分不符 → 不殺，exit 0 | 舊：沒有 reaper |
| — | reaper 起不來（`arm()` 回 None） | 一行警告，agent 不受保護（已知限制，同 #229） | 同 |
| — | reaper 先被殺、agent 才 spawn | `child_hook` 寫入得 EPIPE（SIGPIPE 已忽略）→ agent 照常起、不受保護 | （舊：起 reaper 失敗前已有 agent） |
| — | agora 與 reaper 一起死 | 補不到（已知限制，同 #229） | 同 |

T3／T4 的身分：子行程在 exec 前後是同一個 pid、同一個 lstart（lstart 是 fork 時刻，exec 不改），所以 reaper 在收到 `P` 時讀的 start time 就是 agent 的。pid 重用的殘餘風險只在「agora 已死，而且 agent 在 reaper 讀 `P` 前就結束並被 init 回收、pid 又被重用」，與現行 L2 註記同一類。失敗方向是不殺（身分不符）或不收，不會誤殺。

## 6. 契約（都可測）

注入點規則：

- `store._fault(<點>)` 用 `os._exit(137)`，不跑 finally、不寫 `D`，等同 SIGKILL。新增 `after-guard-armed`、`after-agent-popen` 兩點。
- 另加 `agent-child-before-pid`：環境變數同樣是 `AGORA_TEST_FAULT`，但**不經 `store._fault`**（那是 `os._exit(137)`，在子行程裡會殺到子行程自己）。cli 在 `Popen` 之前、在 agora 行程裡判斷 `os.environ.get("AGORA_TEST_FAULT") == "agent-child-before-pid"`，結果存成一個 bool 交給 `_agent_preexec`。子行程裡只在它為真時做 `os.kill(os.getppid(), signal.SIGKILL)`，然後照常往下走（寫 `P`、exec）。子行程裡不讀 `os.environ`、不 import。這樣能確定地重現「agora 死在 fork 與寫 `P` 之間」。

除非另註，測試以 `python -m agora.cli continue … --headless`（fake claude agent，`wait-file:` 永不釋放，`AGORA_ORPHAN_GRACE=0.3`）跑。

- **C-1（T5 窗口關閉）**：`AGORA_TEST_FAULT=after-agent-popen` → agora 以 137 結束；30 秒內 fake agent（以 session id 找）不存在。**在現行碼上這條是紅的**：把同一個注入點放在現行 `Popen` 與 `orphan_guard.start` 之間就能證明，是 #415 的窗口。
- **C-2（T3 窗口關閉）**：`AGORA_TEST_FAULT=agent-child-before-pid` → agora 以 -SIGKILL 結束；30 秒內 fake agent 不存在。
- **C-3（child_hook 由子行程寫 pid）**：真的 `subprocess.Popen(["sleep", "5"], preexec_fn=guard.child_hook)` 之後，替身 reaper（沿用 `reaper_log` 的 `_RecordingReaper`）收到的第一筆是 `P<proc.pid>\n`。這是在 agora 呼叫 `started()` 之前；測法是不呼叫 `started()`，直接 `os.close(guard.fd)`。這條同時驗 preexec 時寫端仍開著。
- **C-4（T2：沒有 agent 不殺任何行程）**：
  - 行程層：`AGORA_TEST_FAULT=after-guard-armed` → agora 以 137 結束；之後 3 秒內沒有 fake agent（以 marker／session id 找）出生。**不在行程層找 reaper**：agora 已死，reaper 被收養，argv 也沒有可辨認的標記。
  - 單元層（真 reaper）：測試行程自己 `g = orphan_guard.arm()`（`g.proc` 就是真 reaper，pid 已知），不寫任何東西，直接 `os.close(g.fd)` → `g.proc.wait(timeout=10) == 0`。
  - 單元層（直接呼叫）：`main(["--fd", R])` 只收到 EOF → 回 0，`stop_orphan` 沒被呼叫（monkeypatch 記錄）。
- **C-5（協定 v2 判準）**：reaper `main` 收到下列輸入時的結果：
  - `P<pid>\nS<含 "Dec" 的字串>\n` 後 EOF → 呼叫 `stop_orphan`，不能把它當道別；
  - `P<pid>\nD\n` → 回 0、不殺；
  - 只有 `D\n` → 回 0；
  - `P<pid>\n` 加上不認得的行 → 照常處理 P；
  - 沒有 `\n` 的殘行 `D` → 不算道別。
- **C-6（身分：S 覆蓋、無 S 時自記）**：只有 `P` 時，reaper 用收到 `P` 當下的 `start_time` 作身分；有 `S` 時以 `S` 為準；`start_time` 回 None（行程已不在）時不呼叫 `os.kill`。以 monkeypatch `start_time`／`os.kill` 驗。
  - 先收到 `P`（reaper 自讀為 X）、後收到 `S`（值為 Y≠X）、EOF 沒有 `D` → 用 Y 判斷身分：pid 現在的 start time 等於 X 時**不呼叫** `os.kill`。這條驗「`S` 覆蓋」能擋住 pid 重用；拿掉覆蓋就會轉紅。
- **C-7（reaper 已死時 agent 照常起）**：
  - 先 `arm()`，把替身 reaper 殺掉並等它結束，再以 `preexec_fn=guard.child_hook` `Popen` 一個寫檔後結束的小程式：程式有跑（檔案出現），returncode 為 0，不是 -SIGPIPE；
  - `child_hook` 不拋例外；
  - 呼叫後子行程的 SIGPIPE 處置是 SIG_DFL（由程式自報）。
- **C-8（正常路徑不變）**：既有的 `test_a_real_continue_says_goodbye_on_its_normal_path` 與 `…_when_the_write_back_blows_up` 照常綠。替身 reaper 收到的記錄**依序恰為三行**：`P<agent pid>`、`S<orphan_guard.start_time(agent pid) 的 fsencode>`、`D`。`S` 的值由測試在 continue 結束前（agent 還活著時）自己讀一次來比對；fake agent 用 `wait-file:` 讓測試控制它何時結束。這條驗證 cli 真的在 `Popen` 之後呼叫 `started(proc.pid)`，而且寫在 `D` 之前。`test_killing_agora_continue_takes_the_agent_with_it`：保留「SIGKILL 後 agent 消失」的斷言；「等 reaper 就位」那段要嘛刪掉，要嘛改成斷言「看到 agent 時 reaper 已在（ppid 是 agora）」；註解改寫成 #422。
- **C-9（arm 與 Popen 之間的例外也道別）**：讓 `subprocess.Popen(launch.argv …)` 拋 OSError（monkeypatch cli 端的 `Popen`，不影響 `orphan_guard._spawn`）→ `cmd_continue` 照原本的錯誤路徑失敗，替身 reaper 收到 `D`，沒有 `P`。
- **C-10（arm 失敗不影響 continue）**：`_spawn` 拋例外 → 一行 `_warn`、continue 照常、exit code 不變；agent 的 preexec 不寫任何東西（guard 為 None）。這是既有的 `test_a_reaper_that_cannot_start_does_not_fail_continue`，改成以 `arm` 為準。
- **C-11（stderr 行不變）**：headless 時 stderr 的前幾行依序為 `agora-session`、（opencode）`agora-port`、`agora-agent-session`，內容與現在一致。既有的 `test_continue_headless.py:187/:343`、`test_cli_sync.py:1849` 不改也綠。
- **C-12（互動版同一條路）**：不帶 `--headless` 的 continue（stdin 是 `/dev/null`），`AGORA_TEST_FAULT=after-agent-popen` → fake agent 在 30 秒內消失。至少一條，證明互動與 headless 共用同一處修正。
- **C-13（依賴方向）**：`orphan_guard` 仍不 import agora 的任何模組。可用既有的 import 守衛或一條 `ast` 檢查驗。
- **C-14（exec 失敗時 reaper 不卡，T-e）**：在 `AGORA_ORPHAN_GRACE=30` 下 `g = orphan_guard.arm()`（真 reaper，從測試行程繼承這個 env）；`subprocess.Popen(["/nonexistent/agora-422"], preexec_fn=g.child_hook)` 拋 `FileNotFoundError`；不寫 `D`，直接 `os.close(g.fd)` → `g.proc.wait(timeout=5) == 0`。在遠小於 grace 的時間內結束，表示 reaper 走的是「身分不符／已不在 → 不送訊號」，沒有進入 TERM 之後的 30 秒等待。「不殺同 pid 的陌生行程」由 C-6 與 `test_a_reused_pid_is_not_signalled` 把關，不在這條驗。

改壞驗證建議（給審查）：

- 把 `child_hook` 改成 no-op → C-1、C-2、C-3 轉紅；
- 判準改回 `NORMAL in data` → C-5 轉紅；
- 拿掉 SIGPIPE 忽略 → C-7 轉紅；
- 把 `arm()` 移回 `Popen` 之後 → C-2 轉紅；
- 拿掉 `S` 對自讀值的覆蓋 → C-6 的新點轉紅；
- `child_hook` 不吞 `OSError` → C-7 轉紅（Popen 會報 preexec 例外）。
- 拿掉 cli 的 `guard.started(proc.pid)` 呼叫 → C-8 轉紅（少了 `S` 行）。

測試策略：單元（C-3～C-7、C-9、C-10、C-13、C-14）用替身 reaper 或直接呼叫 `main`；行程層（C-1、C-2、C-4、C-8、C-11、C-12）用真的 `agora.cli` 子行程加 fake agent，只殺自己記下的 pid，不 `pkill`；逾時沿用 #415 的 30／60 秒上限，不加負載重現。驗收門檻建議全 unit 綠，加上 `-k "orphan or continue"` 在同一台機器連跑 20 次 0 紅（門檻由 PM 定）。

## 7. 跨平台

- **start time**：仍用 `ps -o lstart= -p`，Linux 與 macOS 同格式，秒級精度；不新增 ps 用法。`_children` 的 `ps -eo pid=,ppid=` 不變。
- **`preexec_fn` 與 `close_fds` 的順序**：CPython `_posixsubprocess` 的子行程依序做 restore_signals → preexec_fn → close_fds → exec，Linux／macOS 同一份 C 碼。Linux 已實測；macOS 是推論，由 C-3 在 CI／Mac 上驗。現行碼已在 macOS 用 `preexec_fn`，所以 `Popen` 本來就走 fork_exec，不是 posix_spawn。
- **process group**：不變。reaper 用 `start_new_session`；agent 留在 agora 的 group；不用 killpg（#229）。
- **`PR_SET_PDEATHSIG`**：不用（§4 D1）。
- **`preexec_fn` 與執行緒**：preexec 在多執行緒行程裡有 fork 後鎖死的風險。`child_hook` 只呼叫 C 層的 `os.write`、`os.getpid`、`signal.signal`，不 import、不碰 logging，風險與現行的 SIGINT 還原同級。

## 8. 對現有文件與 spec 的影響

- `openspec/specs/`：**無**。沒有 requirement 寫到 reaper、啟動順序或三行 stderr（§2 的 grep）。是否把 #229 寫成主 spec 的 requirement：本單不做，需要時另開 d（C-1／C-2 可直接當 scenario）。
- 實作 PR 一起改：
  - `docs/design.md:384` 的 #229 段：「起 agent 之後……另起一個極小的回收者」「agent 在 pipe 建立之前就起好」改成新順序與 pipe 協定 v2，並把「`Popen` 到 reaper 之間」從限制中移除；
  - `src/agora/orphan_guard.py` 開頭的 docstring 第 11-15 行（「right after the agent」「the pipe is made after the agent starts」）。
- `docs/architecture/overview.md`：依賴方向不變，只有 `orphan_guard.py` 行數會變（下次總圖更新時順手）。
- `docs/architecture/decisions.md`：新增 D-27，與本 note 放在同一個 PR。

## 9. PR 切分

- **設計 PR**：本 note 加 `docs/architecture/decisions.md` 的 D-27（同一個 PR）。
- **實作 PR（一張）**：`src/agora/orphan_guard.py`、`src/agora/cli.py`（`cmd_continue` 的啟動段與 preexec 包裝）、`src/agora/store.py`（只在需要時加注入點的說明，`_fault` 本身不改）、`tests/unit/test_orphan_guard.py`（C-1～C-14）、`docs/design.md:384`。不拆：協定兩端（reaper 的判準與 cli 的寫入）必須一起上，分開會有半套狀態，例如新 reaper 配舊 cli 時會收到已移除的 `--pid`／`--start`。

## 10. 風險與未決

- R-1：依賴 CPython 子行程「preexec 在 close_fds 之前」的實作順序（3.12 已實測）。將來版本若改變，C-3 會轉紅，不會靜靜失效。`preexec_fn` 在 CPython 文件中被標為多執行緒下不安全，但沒有棄用；現行碼已在用。
- R-2：多執行緒 fork 後的 preexec 鎖死。`child_hook` 只用 C 層呼叫；實作 PR 要註明 `cmd_continue` 在 `Popen` 當下有哪些執行緒（本 note 沒查），審查時對一下。
- R-3：殘餘的 pid 重用窗口（§5 末段），失敗方向安全。`S` 讓 agora 活著時的正常路徑沒有這個窗口；殘餘只在 agora 於 `started()` 之前就死（T3～T5）時。
- R-4：reaper 現在一定先於 agent 存在，每次 continue 多一個「沒有 agent 也短暫存在」的情況（T2、`Popen` 失敗）。成本是一個 python 行程，與現在相同。
- R-5：外部程式若用 `ps` 依 `--pid` 找 reaper，會找不到。本 repo 只有 `tests/unit/test_orphan_guard.py` 的 `_reaper_pids`（#415）這樣找；外部是否依賴**沒驗到**。實作 PR 的說明寫一句知會下游。
- 已決（PM）：設計文件用本檔、不開 openspec change；本單不把 #229 加進主 spec；接受測試注入點 `agent-child-before-pid`。
