## Context

`cmd_continue`：寫 pending 並持有 flock（agent 繼承）→ `subprocess.run(agent)` 在前景跑 → 結束後 `_finish()`：`agent.collect(launch)`（比 `before_count` 多才有內容）→ 組 header（途中 Session 被刪掉就另存成新的）→ `_save(update=same_id, kind="continue")`（stage 到 outbox、記到索引、啟動背景上傳）。`recover_pending` 在鎖沒人拿時補做 `_finish`。

「被刪就另存」現在有兩條路：前景的 `_finish`（`_lost_in_cloud`）與背景上傳的 L7（`_rescue_deleted`）。

這份 design 已經依動手前的審查（reviewer，P1～P17）改過；它依賴 change `continue-fork` 的 `store.never_confirmed`（寫回一個還沒被 Drive 確認過的 Session 不算更新既有 id），在 `continue-fork` 之後實作。

## Goals / Non-Goals

**Goals：**
- agent 執行期間每 5 分鐘把最新對話寫回並上傳；
- 不弄亂 agent 的畫面；不弄壞 Session；
- 和 pending、補存、outbox、途中被刪、背景的另存共存，同一段工作不會變成兩個 Session。

**Non-Goals：**
- 即時同步（秒級）；agy（#26）；互動模式顯示「正在接續」的進度；
- 跨機器同時接續同一個 Session 的衝突偵測（見 Risks）；
- 上傳端比對 header 裡的 raw md5（P17 後半，另開工單）。

## Decisions

### 等待迴圈放在主執行緒（P9）
- `subprocess.run` 換成 `Popen`（`pass_fds`、`preexec_fn`、`cwd`、`env` 的 `PWD` 照舊），主執行緒 `proc.wait(timeout=間隔)`；逾時就做一次定期寫回，再繼續等。`wait(timeout)` 在 POSIX 上每 50 ms 內輪詢一次，跑幾個小時可以接受。
- 間隔預設 300 秒。`AGORA_CONTINUE_SYNC_SECONDS` 給測試用，接受小數，下限 0.1（更小或讀不懂就用 0.1），寫進 design 的測試接縫清單。
- **agent 還活著時，迴圈不能離開**：定期寫回吞掉 `Exception`；迴圈本身遇到其他例外時，不殺 agent，先 `proc.wait()` 等它結束再把例外丟出去（`subprocess.run` 原本會 kill 子程序，換成 Popen 之後由我們決定）。
- SIGINT 照舊：agora 忽略、agent 用預設；定期寫回期間叫的子程序也繼承忽略，Ctrl-C 不會殺到它們。

### 寫回目標的解析：定期、最後一次、補存三處共用（P2、P3、P14）
`_write_target(paths, index, record, *, check_drive)` 回傳「這次要寫到哪個 id、是不是要另存」：
1. 先在索引找**已經另存過的**：`agora.source` 是這次的 (agent, agent_session_id)、`relation` 是 `continue`、`parents` 含 record 目前的目標 id、雲端有（`cloud_has`）的 Session，取最新的一個。找到就沿用它，不論原本那個還在不在。這包括背景上傳 L7 另存的 Y 和前景另存的 B。限定 relation 與 parents，是為了不認到使用者自己 import 同一個 agent session 產生的分岔。
2. 沒有的話，原本的目標還在就用它：
   - 定期寫回（`check_drive=False`）只看本機：索引裡有、沒有被標成雲端沒有。**不連 Drive**（P7）：別台的刪除交給背景上傳的 L7 發現，L7 另存之後，下一輪由第 1 步沿用。
   - 最後一次與補存（`check_drive=True`）照現在的 `_finish` 加上 `_lost_in_cloud`。這是最後的檢查點，**內容和上一次相同也要做**（P2）。
3. 原本的目標被刪了、也沒有另存過，才另存成新的（`relation: continue`，`parents` 指向目前的目標，所以 B 被刪後的 C 指向 B，和 `_rescue_deleted` 往下串的慣例一致）。

### 一次寫回
- 把 `_finish` 拆成「收」（`collect`）與「寫」（解析目標＋組 header＋`_save`），定期寫回、最後一次、補存都走同一條。
- **update 的判斷**（P1）：照現在的 `update = same_id`，再由 `continue-fork` 的 `store.never_confirmed` 規則修正：寫到的 id 在 outbox 裡還是新建（沒有 `.update`、沒有上傳程序在比對）時，仍然是新建。B 剛另存、還沒上過 Drive 時的下一輪寫回，不會被背景當成「被別台刪掉」。
- **沒有新內容就不寫**：記下上一次**成功**寫回（`_save` 正常回來之後才記，P8）的 raw md5。起點：一般接續是啟動時載入的 raw；fork 是 pending 的 `saved_md5`（`continue-fork` 的 pending 記錄）。`collect()` 回 None、或 md5 相同就不 stage。
- **便宜的預檢**（P6）：Claude 先看 jsonl 與 sidecar 目錄的大小與 mtime，和上一輪相同就連 export 都不做。opencode 照樣 `opencode export`（不碰 opencode 的資料庫）。
- `previous_sources` 去重（P5）。
- 每次寫回都會改 `agora.updated_at`；header_updates 用 overlay 重套，不會累加。

### 最後一次（P2、P8）
- agent 結束後輸出回到終端機。如果定期寫回剛好還在跑，agent 一結束就先印一行「[agora] 正在寫回…」。
- 照「寫回目標的解析」（`check_drive=True`）：
  - 有新內容：寫；
  - 沒有新內容、目標也還在：不 stage，但照樣 `background.start`（踢一下上傳），exit 依它的結果照 `_save` 的規則給 0 或 3，並印對應的訊息；
  - 沒有新內容、但目標被刪了而且沒有另存過：照樣另存（工作不能只留在被刪的 Session 裡）。
- 最後印出：
  - stdout：最後寫到的 id；
  - stderr：途中另存過的說明（含沿用背景另存的 Y 的情況），以及「定期寫回失敗 N 次，見 `<state>/continue.log`」（N > 0 時）。
- 整段都沒有任何寫回才記 `unsaved-launches`；fork 不記（`continue-fork`）。

### 讀到一半不能寫壞（P10）
- Claude：`_split_lines` 會丟掉寫到一半的最後一行（review S10）；其他行壞掉會丟 `AgentError`。
- opencode：`opencode export` 的一致性、閒置時兩次 export 是否 byte 相同、和正在跑的 TUI 同時 export 的副作用、Claude 閒置時會不會 append 非訊息的行，**都還沒量過**。tasks 1.1 先用真的 agent 量，結果寫進這份 design 之後才承諾「沒有新內容就不寫」。
- 中途快照：定期寫回可能停在工具呼叫中間（Claude 有 tool_use 沒 tool_result；opencode 有 running 的 tool part）。這個版本會上 Drive，別台拿去 continue／merge／fork 時看到的是停在中間的對話。接受，寫進 Risks；最後一次寫回會整份覆蓋。fork 的 `append_native` 已經處理「結尾是沒有回答的 tool_use」。
- 定期寫回的任何例外都吞掉、記 log、計數，下一輪再試；agent 照樣跑。

### 不弄亂 agent 的畫面（P9、P15）
- 定期寫回那一段，Python 的 stdout／stderr 用 `contextlib.redirect_stdout/redirect_stderr` 轉到一個包裝過的 writer：每行前面加時間與 Session id，寫到 `<state>/continue.log`，大小上限比照 upload.log 的 `LOG_MAX`。
- 那一段叫的外部程式：
  - 輸出：rclone 用 `capture_output`；`opencode export` 的 stdout 寫暫存檔、stderr 是 PIPE；背景上傳是脫離終端機的新 session，寫自己的 log。都不會寫到終端機。
  - 輸入：`Drive._run` 與 `opencode export` 加 `stdin=subprocess.DEVNULL`，rclone 加 `--ask-password=false`，不和 agent 搶鍵盤。
- 從互動模式接續時（`tui` 的 `outside()` 在同一個 process 裡呼叫 `cli.main`），redirect 影響整個 process；那時 TUI 已經交出終端機、沒有別的 thread 在印東西。測試要涵蓋。

### pending（P4）
- 不改寫 pending 的內容（不必：每次都重新解析目標）。
- 目標換到另存的 B 時，`os.rename(pending/A.json, pending/B.json)`：rename 是原子的，flock 跟著 inode 走（agent 繼承的 fd 也是同一個）。之後：
  - B 受「正在接續」保護：`store.continuing(B)` 為真，同機 `delete`、`pull --not-exist-delete` 會拒絕，sync 不把 B 標成雲端沒有；
  - A 不再算接續中，sync 會把 A 標成雲端沒有（它本來就是）；
  - session-sync 的「正在接續中＝`pending/<ULID>.json` 的鎖有人拿著」字面仍然成立（ULID 是目前的目標），不需要改 spec。
- `cmd_continue` 結束時 unlink 的是目前的路徑。`recover_pending` 讀到的 record 的 `agora_id` 仍是 A，靠「寫回目標的解析」找到 B。

### 同一台機器上不能同時接續同一個 Session（P5）
- `cmd_continue` 一開頭，目標 `store.continuing(target)` 為真就拒絕（exit 1，「正在接續中，等它結束」）。現在第二個接續會用 rename 蓋掉第一個的 pending，第一個結束時刪到第二個的、第二個結束時 exit 2，這是既有的 bug。
- fork 的目標是新的 B，不受影響；但被 fork 的 A 正在接續中時仍然可以 fork（A 不會被寫）。

### store 的小修（P13、P17）
- `stage` 接受 `update`／`kind`，在 tmp 資料夾裡先寫好 `.update` 再整個換進去，背景上傳不會在 stage 與 `mark_update` 之間看到「沒有 `.update`」的版本。
- 定期寫回的 stage（檔案與資料夾）與 pending 的寫入做 fsync。

## Risks / Trade-offs

- [每次有變化就上傳一份完整的 raw；被取代的 raw 進 Drive 垃圾桶留 30 天、佔配額。例如 raw 20 MB 的 Session 接續 8 小時、每 5 分鐘都有新對話，約 2 GB 在垃圾桶] → 只在有變化時寫，Claude 先做便宜的預檢；PM 10-10 決定先維持進垃圾桶當安全網（已回報 PMO）。tasks 1.1 量典型的 raw 大小。
- [跨機器同時接續同一個 Session：Drive 上的版本會每 5 分鐘換成另一段對話，後寫的贏] → 很少見；寫進文件。同機的情況直接拒絕。
- [中途快照停在工具呼叫中間] → 接受；最後一次會整份覆蓋。
- [接續中不做 sync，別台在接續期間改的 header（title、tags）會每 5 分鐘被本機的舊版蓋回去（原本只在結束時蓋一次）] → 接受，寫進文件。
- [斷電後被截斷的 raw 可能被當成完成版上傳] → stage 加 fsync；上傳端比對 header 的 raw md5 另開工單。
