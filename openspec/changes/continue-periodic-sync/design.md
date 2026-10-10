## Context

`cmd_continue`：寫 pending 並持有 flock（agent 繼承）→ `subprocess.run(agent)` 在前景跑 → 結束後 `_finish()`：`agent.collect(launch)`（比 `before_count` 多才有內容）→ 組 header（途中 Session 被刪掉就另存成新的）→ `_save(update=True, kind="continue")`（stage 到 outbox、記到索引、啟動背景上傳）。`recover_pending` 在鎖沒人拿時補做 `_finish`。

## Goals / Non-Goals

**Goals：** agent 執行期間每 5 分鐘把最新對話寫回並上傳；不弄亂 agent 的畫面；不弄壞 Session；和 pending、補存、outbox、途中被刪共存。

**Non-Goals：** 即時同步（秒級）；agy（#26）；互動模式顯示「正在接續」的進度。

## Decisions

### 等待迴圈放在主執行緒，不開執行緒
- `subprocess.run` 換成 `Popen`，主執行緒 `proc.wait(timeout=間隔)`；逾時就做一次定期寫回，再繼續等。agora 本身沒有其他工作，用逾時迴圈就夠，不需要執行緒與鎖。
- 間隔預設 300 秒；`AGORA_CONTINUE_SYNC_SECONDS` 給測試用（照 `AGORA_*` 測試接縫的慣例，寫進 design 3.x 的接縫清單）。
- SIGINT 照舊：agora 忽略、agent 用預設。`wait(timeout)` 不受影響。

### 一次寫回 ＝ 抽出 `_finish` 的寫回部分，可以重複呼叫
- 把 `_finish` 拆成「收」（collect）與「寫」（組 header＋`_save`），定期寫回與最後一次都走同一條。
- **沒有新內容就不寫**：記下上一次寫回的 raw 的 md5（一開始是啟動時載入的 raw）；`collect()` 回 None、或 raw 的 md5 和上一次相同，就不寫。
- 每次寫都是 `_save(update=True, kind="continue")`，和現在的最後一次相同。
- 最後一次（agent 結束後）：照現在的 `_finish`；和最後一次定期寫回相同就不再寫，但 exit 與印出的 id 照舊（印最後寫到的那個 id）。整段都沒有任何寫回才算「這次沒有新內容」，才記到 `unsaved-launches`。

### 讀到一半不能寫壞
- Claude：`_split_lines` 已經會丟掉寫到一半的最後一行（review S10）；其他行壞掉會丟 `AgentError`。
- opencode：`opencode export` 讀資料庫，是一致的快照；還在串流的那一則可能不完整，下一次會整則換掉。
- 定期寫回的任何例外（`AgentError`、讀檔錯、`opencode export` 失敗、上傳失敗）**都吞掉、記 log、這一輪跳過**，下一輪再試；agent 照樣跑。只有最後一次維持現在的行為。
- 寫回的內容永遠是「agent 自己的整份對話」，所以這一輪拿到不完整的，下一輪（或最後一次）會整份覆蓋，不會累積錯誤。

### 不弄亂 agent 的畫面
- 定期寫回期間，agora 的 stdout／stderr 要轉到 log 檔（`<state>/continue.log`，附時間與 Session id），包括：
  - Claude 的「最後一行不完整」與 `/clear` 警告；
  - `_finish` 的「途中被刪掉、另存成…」說明；
  - `_save` 的「上傳失敗，已存入 outbox」。
- 實作上在那一段用 `contextlib.redirect_stdout/redirect_stderr`（Python 層的 print）；呼叫外部程式（`opencode export`、背景上傳）時確認它們的輸出是 capture 或導到檔案，不會直接寫到終端機（fd 1／2）。
- 最後一次寫回（agent 已經結束）照舊印在終端機；途中曾經另存過的話，最後要補印一次「途中被刪、另存成 …」的說明，讓使用者看得到。

### 和 pending、補存、outbox、途中被刪共存
- **pending 鎖**：從頭持有到最後，定期寫回不碰鎖。`recover_pending` 只在鎖沒人拿時才跑，所以不會和定期寫回同時寫同一個 Session。
- **途中被刪（別台機器或這台）**：第一次發現時照 `_finish` 的規則另存成新的 Session（`relation: continue`、`parents` 指回去）。之後的定期寫回與最後一次都要寫到**這個新的 id**，不能每一輪再另存一個：
  - 更新記憶中的 record（`agora_id`、`parent`）；
  - 同時把 pending 檔的內容改寫成新的 record：用持有鎖的那個 fd `seek(0)`、`truncate`、寫入、`flush`，檔名不變。這樣 agora 或 agent 被殺掉時，`recover_pending` 也會寫到新的 id。
- **outbox**：每次寫回都是 stage 一個新版本；背景上傳只有在 Drive 的 md5 對上自己傳的那個版本才把 outbox 那筆拿掉，上傳途中又 stage 的新版本會留著（H1），下一輪或下一個指令再送。
- **sync 與 `continuing()`**：sync 已經不會把正在接續的 Session 當成雲端沒有（`store.continuing`）；定期寫回不改這個判斷。
- **同一台機器的互動模式**：寫回走 `stage`＋`remember`，本機鏡像與索引會更新，互動模式重讀時看得到最新的對話。

## Risks / Trade-offs

- [每 5 分鐘一次完整寫回：大 Session 每次都 stage 一份完整的 raw 與閱讀版，也上傳一次] → 沒有新內容就不寫；5 分鐘的間隔是本人定的。
- [Claude 寫 jsonl 的時機] → 只丟最後一行；如果中間某行壞掉（不該發生），這一輪丟 `AgentError` 被吞掉、跳過。
- [外部程式的輸出漏到終端機] → 測試要驗：定期寫回期間 fd 1／2 沒有被寫入（用子程序跑假 agent，抓 agora 的輸出）。
