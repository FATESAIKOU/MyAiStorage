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
- 間隔由 `--sync-period <分鐘>` 決定（本人 10-10 決定）：
  - 沒給就是 5；給 0 就不自動同步（等待迴圈退回成單純等 agent 結束，只有最後一次寫回）；
  - 接受整數或小數（例如 0.5 是 30 秒），負數或不是數字就 `InputError`（exit 1），在開 agent 之前就擋下；
  - 大於 0 時換算成秒，下限 0.1 秒（避免連續不停地寫回）；
  - 測試直接用很小的小數（例如 `--sync-period 0.005`），不另設環境變數。
  - `--fork` 也適用；其他動作給了也被忽略（和 `--dir` 一樣）。
- **agent 還活著時，迴圈不能離開**：定期寫回吞掉 `Exception`；迴圈本身遇到其他例外時，不殺 agent，先 `proc.wait()` 等它結束再把例外丟出去（`subprocess.run` 原本會 kill 子程序，換成 Popen 之後由我們決定）。
- SIGINT 照舊：agora 忽略、agent 用預設；定期寫回期間叫的子程序也繼承忽略，Ctrl-C 不會殺到它們。

### 寫回目標的解析：定期、最後一次、補存三處共用（P2、P3、P14）
`_write_target(paths, index, record, *, check_drive)` 回傳「這次要寫到哪個 id、是不是要另存」：
1. 先找**已經另存過的**，兩處都找：
   - **索引**：`agora.source` 是這次的 (agent, agent_session_id)、`relation` 是 `continue`、`parents` 含 record 目前的目標 id、雲端有（`cloud_has`）的 Session，取最新的一個。這包括背景上傳 L7 另存的 Y 和前景另存的 B。
   - **outbox**（含上傳器正在比對的 `.done-`）：同一組三個記號（`relation: continue`、`source` 是這次的 agent session、`parents` 含目前的目標），找**只 staged、還沒被 `store.remember` 索引**的另存——uploader 是 stage 完才 remember，那個窗口裡（或 crash 在裡面）rescue 只在 outbox。`_staged_as` 把 header 一起讀回來，之後不再讀它一次（免得和上傳器搶檔案）。
   - 兩邊都有時取 ULID 較新的那一個。找到就沿用它，不論原本那個還在不在。限定 relation 與 parents，是為了不認到使用者自己 import 同一個 agent session 產生的分岔。
2. 沒有的話，原本的目標還在就用它：
   - 定期寫回（`check_drive=False`）只看本機：索引裡有、沒有被標成雲端沒有。**不連 Drive**（P7）：別台的刪除交給背景上傳的 L7 發現，L7 另存之後，下一輪由第 1 步沿用。
   - 最後一次與補存（`check_drive=True`）照現在的 `_finish` 加上 `_lost_in_cloud`。這是最後的檢查點，**內容和上一次相同也要做**（P2）。
3. 原本的目標被刪了、也沒有另存過，才另存成新的（`relation: continue`，`parents` 指向目前的目標，所以 B 被刪後的 C 指向 B，和 `_rescue_deleted` 往下串的慣例一致）。另存的 Session **帶上原本的使用者 header**（`SYSTEM_KEYS` 以外，含 case、tags、refs、description、verified；原本的 header 還在本機時），再套 `header_updates`。一般 continue 與 fork 都一樣；背景上傳的另存（`_rescue_deleted`）本來就保留 header，兩條路因此一致（程式審查 M9，PM 10-10 決定）。

### 一次寫回
- 把 `_finish` 拆成「收」（`collect`）與「寫」（解析目標＋組 header＋`_save`），定期寫回、最後一次、補存都走同一條。
- **update 的判斷**（P1）：照現在的 `update = same_id`，再由 `continue-fork` 的 `store.never_confirmed` 規則修正：寫到的 id 在 outbox 裡還是新建（沒有 `.update`、沒有上傳程序在比對）時，仍然是新建。B 剛另存、還沒上過 Drive 時的下一輪寫回，不會被背景當成「被別台刪掉」。
- **沒有新內容就不寫**：記下上一次**成功**寫回（`_save` 正常回來之後才記，P8）的 raw md5。起點：一般接續是啟動時載入的 raw；fork 是 pending 的 `saved_md5`（`continue-fork` 的 pending 記錄）。`collect()` 回 None、或 md5 相同就不 stage。
- **便宜的預檢**（P6）：Claude 先看 jsonl 與 sidecar 目錄的大小與 mtime，和上一輪相同就連 export 都不做。opencode 照樣 `opencode export`（不碰 opencode 的資料庫）。
- `previous_sources` 去重（P5）。
- 每次寫回都會改 `agora.updated_at`；header_updates 用 overlay 重套，不會累加。

### 最後一次（P2、P8）
- agent 結束後輸出回到終端機。如果定期寫回剛好還在跑，agent 一結束就先印一行「[agora] 正在寫回…」。
- **等待目標 settle（M1）**：若 `run.target` 在 outbox（含 `.done-`）裡有正在上傳的項目，先等待該項目 settle（最多 15 秒）：一旦目標離開 `waiting_ulids` 或發現另存（outbox 的 staged rescue 或索引裡的 rescue）即立刻返回；背景上傳器若未啟動則踢一次並最多等 2 秒，未啟動或結束即返回。這大幅縮小「最後一次寫回時背景正在 rescue、前景誤讀舊目標」的時間差。
- 照「寫回目標的解析」（`check_drive=True`）：
  - 有新內容：寫；
  - 沒有新內容、目標也還在：不 stage，但照樣 `background.start`（踢一下上傳），exit 依它的結果照 `_save` 的規則給 0 或 3，並印對應的訊息；
  - 沒有新內容、但目標被刪了而且沒有另存過：照樣另存（工作不能只留在被刪的 Session 裡）。「已經另存過」包含還只在 outbox 的：沿用時直接拿讀回來的那個 header 寫到它。
- 最後印出：
  - stdout：最後寫到的 id（沿用背景另存的 Y、或還只在 outbox 的 staged rescue 時，就是沿用後的那個 id）；
  - stderr：途中另存過的說明（含沿用背景另存的 Y 的情況），以及「定期寫回失敗 N 次，見 `<state>/continue.log`」（N > 0 時）。
- 整段都沒有任何寫回才記 `unsaved-launches`；fork 不記（`continue-fork`）。

### 讀到一半不能寫壞（P10）
- Claude：`_split_lines` 會丟掉寫到一半的最後一行（review S10）；其他行壞掉會丟 `AgentError`。
- 量測（tasks 1.1，PM 10-10 用真的 agent 跑 `tests/integration/test_sync_measure.py`，493b2ea）：
  - opencode 閒置時兩次 `opencode export` byte 相同；另一輪對話進行中並行 export 6 次，0 次失敗或讀不出來（每次 0.5～1 秒）；
  - Claude 閒置 20 秒，jsonl 的大小、行數、mtime 都沒變，沒有 append 非訊息的行；
  - 所以「raw 的 md5 相同就是沒有新內容」成立，Claude 的大小／mtime 預檢也成立。
  - 測試用的短 Session：opencode 匯出 4.7～7.6 KB，Claude jsonl 約 106 KB（20 行，大部分是系統行）。
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

### 互動模式：接續前確認同步間隔（本人 10-10 決定）
- Agora 頁按 Enter 接續時，選完 agent 與工作目錄之後，多一個輸入框（沿用 `AskText`）：標題「自動同步間隔（分鐘，0 ＝ 不自動同步）」，預設值 5。
- Enter 確認：是不小於 0 的數字就把 `--sync-period <值>` 加進 continue 的參數；不是的話留在輸入框、說明要填數字。Esc 取消整個接續。
- 輸入框照第 1 節的按鍵規則：視窗裡 Tab／shift+tab 不動底下的畫面。

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
- [存在檢查與 copy 之間還剩一個呼叫的窗口] → `.update` 的 entry 在 copy 前一刻再列一次檔、不在就地另存，窗口從「整個 raws 批次」縮到「這一次列檔＋幾次 copy」。落在這個窗口裡的刪除會讓 copy 把資料夾重新建立，所以確認階段會比對：這一輪自己什麼都沒刪，而列檔時還在的檔案不見了，那就是資料夾被刪掉又寫回去——**把剛寫回去的資料夾清掉（維持別台的刪除）並另存成新的 Session**。殘留：entry 指名的 raw 與 Drive 上那一份同名時（例如只改標頭的 `edit`，raw 一個 byte 沒變）重建前後的檔案集合一樣，看不出來；這種寫回不常見，而且內容還在 outbox 裡，下一輪由 L7 的既有規則處理。
- [最終寫回可能把「copy 復活中的 A」當成還活著] → 別台的刪除正好落在背景 copy 的窗口時，copy 會把 A 的資料夾重建；那一刻若做最後一次寫回，它會看到 A、判它還活著、印出 A，背景隨後才 purge A 並另存成 B——工作不會丟（B 會做出來，之後的寫回都到 B），但 stdout 指的 id 之後就沒了，spec「途中被刪」的「結束時印出另存的說明」在這個交錯下不會發生。現在最後一次寫回有 15 秒上限的等待（`_wait_target_settled`），只要 A 的上傳在 15 秒內有結果就會採納另存，但這只是「縮小窗口」而非完全關閉；若背景上傳超時（如超過 15 秒），超時後仍可能走回舊目標並印出 A。機率低：要刪除正好落在 copy 窗口、agent 又正好在那瞬間結束。**PM 10-11 決定接受、不對調 purge／另存順序**：先另存再 purge 只把窗口縮掉一次 purge、關不掉，還會改變 crash 的語意（先另存再 purge：crash 會同時留下 B 和沒被維持刪除的 A）。
- [never_confirmed 在 import 的 entry 還沒送出就 continue 時會自我延續] → 鄰近風險，這次不修：只要 A 的 outbox entry 在某個 `_save` 當下是沒記號的（例如 import 自己的 entry 還沒上傳完、continue 就開始），之後每輪都會寫出沒記號的新版，上傳器就一直把它當新建直接傳。目前的整合測試在 continue 之前等 entry 排空，所以走不到這條；**不要**用「在 index 裡就不算 never_confirmed」去堵——那會讓 F1（離線 import 的新 Session 寫回時被誤判成被別台刪掉）回來。
