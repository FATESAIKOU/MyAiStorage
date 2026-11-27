# #491 agora TUI：啟動先顯示本機清單，Drive 同步放背景（F5）

- 單：#491（MAS，層級 c；架構師必經，作者模式）。#485 調查的後續（F5）。
- 作者：專案架構師（作者模式，myaistorage-arch-w1，Claude Opus 5.5），2026-11-27。基準：main `8941cf8`。
- 範圍：只設計。動到的模組：`store`（兩個小介面，不改同步演算法）、`tui`；新增 `sync_child`（子行程本體）、`tui_sync`（背景同步的行程與狀態，不碰 Textual）。測試替身 `tests/fakes/fake_rclone.py` 加一個閘門開關。
- 主 spec（`openspec/specs/interactive-mode/spec.md`）要補的條文草稿在 §9，由 PM 決定與撰寫。issue 的備註寫「排到時建 openspec change `openspec/changes/agora-tui-background-sync/`」；BRIEF 指定放在 design note。要不要另建 change，見 Q-5。
- 實測：只讀。沒有跑任何量測，數字都引自 `REPORT-tui-slow.md`（#485，Sonnet，假 rclone）與 `docs/perf.md`（2026-10-02，真 Drive）。沒有連真 Drive，也沒有碰真機。
- 名詞：
  - **背景同步**：TUI 啟動後，在子行程裡跑一次 `store.sync(throttle=True)`。
  - **可操作**：清單已顯示，而且按 `down` 游標會移動（BRIEF 的定義）。
  - **寫的動作**：會起一個指令模式子行程、或把終端交出去的動作。包括 import／merge／delete／pull／push（`act`），以及 continue／edit（`outside`）。
  - **套用**：同步完成後，用新的列重畫清單。

## 0. 結論

1. **背景同步跑在子行程，不跑在 TUI 的執行緒**。新增 `python -m agora.sync_child`，它跑一次 `store.sync(throttle=True)`，最後印一行結果。TUI 用 `tui_sync.BackgroundSync` 起這個子行程、讀它的輸出、在它結束時通知畫面。
   - 理由是三個讀碼得到的事實（§3.1）：
     - Textual 的 thread worker 跑在 asyncio 的預設 executor。`App.run` 用 `asyncio.run`，離開時要等 executor 的執行緒結束，上限 300 秒。所以在同步中按 `q`，畫面收起來了，行程卻還要等同步跑完才結束。
     - 執行緒裡的同步沒辦法中斷：rclone 是 `subprocess.run`。
     - 現在 `Busy` 的 docstring 寫著，只剩第一次同步與授權還在執行緒裡跑，其他動作都是子行程。
   - 子行程可以中斷（對它的行程群組送 SIGINT，與 `Run` 同一套），TUI 離開時也不必等它。`store` 本來就設計成多行程並行：mirror 原子寫入、index 每筆一個短交易、影子目錄有自己的鎖。
2. **啟動順序**改成 `setup → reload（本機清單，焦點在表格）→ 起背景同步`。同步多久，都不影響可操作的時間。可操作的時間只剩 `reload` 本身，也就是 F1／F3 之後的本機成本。驗收：假 rclone、遠端 3000 筆變動，可操作 < 3 秒（C-22）。
3. **同步中，寫的動作等同步結束；讀的操作照常**。讀的操作包括游標、換頁、勾選、篩選（標題或內文）、預覽、預覽搜尋。
   - 寫的動作一開始就先記下要處理的列（key），再進「等同步完成」視窗。視窗有三個選擇：等它完成（預設）、停止同步並馬上做（Q-1）、Esc 取消這個動作。
   - 等完之後先套用新的列，再用 key 找回剛才記下的列。有任何一列已經不在，就不做，並且說明原因。
   - 為什麼不讓動作與同步並行，理由在 §3.2：
     - 同步在開頭就取好快照（遠端列表、`known`、`trashing`），之後才寫入。同步中刪除的 Session，可能被同步寫回 index 與 mirror。
     - 動作子行程自己的節流同步，在背景同步寫下 `last-sync` 之前不會被節流，於是會再跑一次完整同步，與背景同步搶影子目錄的鎖。
4. **套用**：子行程結束後，在 worker 執行緒裡用自己的 `Index` 算新的列（import 頁用上次 `reload` 存下的 agent 清單，不再列一次），再交回主執行緒。
   - 新的列與現在的列相同：什麼都不重畫。
   - 不同：重畫一次，並且保留下面這些：
     - 游標停在同一個 key 上；那一列不在了，就停在原本的位置。
     - 游標在畫面上的高度不變。
     - 勾選只拿掉已經不在的列，拿掉幾個會說出來。
     - 篩選字不變；內文模式下，Agora 頁的命中會重算。
     - 預覽：游標那一列沒變、檔案也沒變時，預覽區完全不動，捲動位置與標亮都留著。
   - 有視窗開著，或焦點在篩選框、預覽搜尋框時，先不套用，等它們關掉才套用。打字中不會被一次 3000 列的重畫打斷。
5. **UI**：同步中，標題列有一個暗色的「⟳ 同步 Drive 中（N 秒）」。結束後依結果顯示：
   - 完成：不留字；有變化時，訊息列說「同步完成：新增 a、更新 b、移除 c」。
   - 離線：沿用現在的「離線：只有本機資料」（紅）。
   - 失敗：「同步失敗：只有本機資料」，訊息列放錯誤。
   - 停止：「同步已停止：只有本機資料」。
   - 被節流（距上次同步不到 300 秒）時，子行程照跑（`index_pending` 要跑，#222），但不顯示指示。
6. **切成 3 個 PR**，每個合上後行為都不會更糟：
   - PR-1：`store.sync_due`、`store.OFFLINE_NOTE`、`sync_child`、`tui_sync`、假 rclone 的閘門。沒有呼叫端，行為不變（只有一處例外，見 C-1：壞掉的 `last-sync` 從 ValueError 改成照常同步）。
   - PR-2：`tui` 的純函式與 `show` 的新參數。沒有新的呼叫路徑，行為不變。
   - PR-3：接上。啟動、標題列、套用、動作閘門、離開。
   - 依賴：PR-1 ∥ PR-2 → PR-3。
7. **與 F4／F6 的關係**：本單不依賴它們，建議先做本單。
   - 本單的 `build_rows`（在執行緒裡算列）就是 F4 要的那一塊，F4 可以直接拿來用在啟動與回清單。
   - F6（表格減量）會讓套用時的重畫從 0.3～1 秒降下來，但驗收不需要它。
   - F2（#488）已關，本單不改 `act()` 的重載路徑。

## 1. 現況（讀碼，main `8941cf8`）

- `AgoraApp.start`（`src/agora/tui.py:1168-1180`）：
  - `check_setup` 為真時，先 `setup()`。
  - 然後 `push_screen_wait(Busy("同步 Drive", lambda say: store.sync(self.paths, throttle=True, warn=say)))`，在 thread worker 裡同步。
  - 輸出含「連不上 Drive」時，`self.status = "離線：只有本機資料"`。
  - 之後才 `reload()`，清單為空時給提示，最後聚焦表格。
  - 測試都用 `check_setup=False`，所以這條路徑沒有 TUI 測試。
- `reload`（`tui.py:1232-1244`）：
  - 重開 `store.Index`。
  - 重算 `agora_rows`，以及會列出每個 agent 的 `import_rows`。
  - 依 `keep_marked` 處理勾選，`self.cache.clear()`，然後 `show()`。
  - `show()` 不帶 `keep`，所以重載之後游標回到第 0 列：`DataTable.clear` 會把游標與捲動歸零（textual 8.2.8 `_data_table.py:1582` 起）。
- `store.sync`（`store.py:3005-3153`）：
  - 開頭 `Index(paths)`，跑 `index_pending`（#222；在節流與離線的提早返回之前）。
  - 節流：`last-sync` 存在而且距今 < `SYNC_THROTTLE_S`（300），就直接回傳。
  - 然後 `kick_uploader`、`drive.list_sessions()`。失敗時 `say("連不上 Drive，改查本機索引：…")`，回傳本機 index（不 raise，除非 `strict`）。
  - 接著取 `trashing`、`known`、`unfinished` 的快照，算出 `changed`，批次（`download_many`：≥10 筆走影子目錄，持有 `download-staging.lock` 直到整個 `rclone copy` 結束）或逐一下載，逐筆 `index.put`（每筆一個 `BEGIN IMMEDIATE` 短交易）。
  - 最後是標記（`_really_gone`、`mark_missing`）、`_index_outbox`，`last-sync` 寫在最後。
  - sync **沒有**自己的行程間鎖；並行的安全靠每一步自己的原子寫入與短鎖。
- 各指令自己的同步（`cli.py`）：
  - search／import／merge／continue／edit／show 是節流同步（`_sync_for`：先節流同步，有 id 不認得就再完整同步一次）。
  - delete 是 `store.sync(paths, kick=False)`，不節流。
- `Index`：sqlite、WAL、autocommit、連線預設 `check_same_thread=True`。所以在 worker 執行緒裡建的 `Index` 不能拿到主執行緒用。現在 `Busy` 把 `store.sync` 回傳的 index 丟掉，`reload` 再開一個，所以沒有踩到。
- `Run`（`tui.py:824-918`）：
  - 子行程開在自己的行程群組（`start_new_session=True`），stdin 是 `/dev/null`。
  - 讀輸出的執行緒用 `call_from_thread` 回報（app 已經不在就吞掉）。
  - Esc 經 `stop_group` 依序送 SIGINT、SIGTERM、SIGKILL。
  - `stop_everything` 只對 `_groups` 裡的群組（也就是按過 Esc 的）送 SIGTERM。
- 量測（#485，假 rclone，3000 筆，只引用，沒有重量）：
  - `store.sync`：無變化 1.61 秒，3000 筆全部下載 23.0 秒。
  - 啟動到清單可用：L=0 而且被節流時 0.88 秒；全部下載時 21.9 秒。
  - `show()` 重建 3000 列，沒開 profile 時約 0.3～0.5 秒（推論）。
  - `docs/perf.md` 的真 Drive 舊量測：20 個 Session 冷啟動 85 秒（#364 之前）。
  - 目前真 Drive 的時間沒有量到。

## 2. 目標、驗收與不做什麼

### 2.1 目標與驗收

- **G-1**：啟動到可操作的時間與遠端變動量無關。
  - 驗收（C-22，`tests/perf` 探針，不進 CI）：假 rclone，遠端 3000 筆 `session.md` 與本機不同，本機 agent session 0 個。從 `run_test` 開始，到「表格有本機的列，而且按 `down` 之後 `cursor_row` 由 0 變 1」，3 次的中位數 < 3 秒。
  - 同一輪也記錄「同步完成並套用」的時間與套用時主執行緒的凍結時間，只報告，不設門檻。
  - L=100×3 一併記錄，不設門檻。
- **G-2**：同步中與同步後，資料正確性不比現在差。不會有被刪的 Session 復活，不會有動作送錯列，不會同時跑兩個同步（同一個 TUI 之內）。
- **G-3**：同步的狀態看得到：進行中、離線、失敗、停止、完成而且有變化。

### 2.2 不做什麼

- 不縮短同步本身，不改同步演算法：`store.sync` 的步驟、順序、`BULK_*`、節流秒數都不動。`store` 只加兩個小介面，見 §6.1。
- 不碰內容搜尋（`find_in_agents`、`cache`、FTS 的查法）；套用時只照現有的 `search()` 寫法重算 Agora 頁的命中。
- 不改 `act()` 動作後的重載（包括游標回到第 0 列的現況），那是 F2／F4 的範圍。
- 不新增手動再同步的按鍵（Q-2）。
- 不加跨行程的同步鎖（§3.3，風險 R-3）。
- 不處理 index 版本升級時，主執行緒上的 `rebuild_from_mirror`（F4 的範圍）。

## 3. 互斥與鎖

### 3.1 背景同步放在哪裡：子行程

| | 執行緒（`@work(thread=True)`） | 子行程（`python -m agora.sync_child`）＝採用 |
|---|---|---|
| 同步中離開 | `asyncio.run` 收尾時，`shutdown_default_executor` 等執行緒結束，上限 300 秒（`asyncio.constants.THREAD_JOIN_TIMEOUT`）。畫面收了，行程還在，殼不回來（讀碼推論，沒有實測） | 不等。子行程在自己的 session 裡把同步跑完，寫下 `last-sync`；下次啟動被節流，所以很快 |
| 中斷 | 做不到：`Drive._run` 是 `subprocess.run`，沒有取消的入口 | 對行程群組送 SIGINT（`stop_group`，與 `Run` 相同）；rclone 在同一個群組裡，一起停 |
| sqlite 的執行緒限制 | sync 回傳的 `Index` 不能拿到主執行緒用 | 沒有這個問題 |
| adapter 的模組層快取（claude 的 `_LIST_CACHE` 等） | 與內文搜尋的 worker 在同一個行程，有並行風險 | 無關 |
| 與現有架構 | `Busy` 的 docstring：只剩第一次同步與授權還這樣跑 | 與 `Run`、`background`、`round_child`、`start_child` 同一個做法：子行程本體不經 `cli.main` |
| 成本 | 0 | 多一次 Python 啟動加 `import agora`（背景裡，不在可操作的路徑上；PR-1 要量） |

子行程中斷之後是安全的，理由是讀碼得到的：mirror 用 `copy_atomic`／`_publish` 寫入；`index.put` 是單筆交易，中斷就回滾；`last-sync` 最後才寫，沒寫就代表下次會再同步；影子目錄的鎖隨 fd 釋放；rclone 被中斷時留下的是它自己的部分檔，`_publish` 只搬 md5 相符的檔。這與現在指令模式按 Esc 中斷的處境相同。

### 3.2 同步中有動作時：等

這是讀碼得到的兩個風險。兩者都不是新的，只是現在被 `Busy` 視窗擋住，而改成背景之後一定會遇到：

- **刪掉的 Session 復活**：
  1. 背景同步列完遠端、算好 `changed`（含 X）。
  2. 使用者按 `d` 刪 X。delete 子行程把 X 從 mirror 與 index 拿掉，放進垃圾桶佇列。
  3. 背景同步接著下載 X、`index.put(X)`。`trashing` 是它在第 1 步取的快照，不含 X。
  4. 背景上傳器把 X 移到 Drive 垃圾桶。下一次同步時，X 在本機 index 有、遠端沒有，就被標成「雲端已刪」（✗），看起來像是別台機器刪的。
- **動作的同步沒有被節流**：背景同步還沒寫下 `last-sync`，所以動作子行程的節流同步會跑一次完整同步。批次下載會在 `download-staging.lock` 上等背景同步的整個 `rclone copy`，結果動作反而比等背景同步跑完還慢。

所以規則是：

| 動作 | 同步中 | 理由 |
|---|---|---|
| 游標、換頁、Tab、勾選、全選、篩選（標題或內文）、預覽、預覽搜尋 | 照常 | 只讀 index（WAL：讀者不等寫者）、mirror（原子替換：讀到的是舊的或新的一整份）、agent 自己的資料與 `cache/reading` |
| import、merge、delete、pull、push（`act`） | 等 | 上面兩點；delete 的風險最大 |
| continue、edit（`outside`） | 等 | 會寫 index 與 mirror；把終端交出去的時候，背景子行程的輸出沒人讀（管線會塞滿），也不該在 agent 執行時套用 |
| 離開（`q`） | 照常，不等 | §3.1；子行程繼續把同步跑完（Q-3） |

**閘門放在哪裡**：在每個寫的動作的最前面，在確認視窗之前。順序是：

1. 照現在的規則算出要處理的列（`chosen_rows()`，或 `current()`）。沒有列就照現在的訊息結束（不進閘門）。
2. 記下這些列的 key。
3. `await self.wait_for_sync()`：
   - 沒在同步：立刻回傳真。有待套用的結果，就先套用。
   - 在同步：顯示 `WaitSync` 視窗。回傳假（Esc）就結束，什麼都不起，勾選不動。
4. 用 key 從 `shown()` 找回這些列（`rows_by_keys`），順序照畫面。少了任何一個，訊息列說「同步後清單變了：N 個已不在或被篩選掉，動作取消，請再確認」，結束。
5. 接著照現在的流程問問題、起子行程。

閘門放在問問題之前，有兩個原因：

- 確認視窗裡的標題與列數，必須是同步之後的。
- 不在同步時，第 3 步不顯示任何東西，第 4 步一定找得到。所以現有的測試與流程一個字都不變。

不放在「起子行程之前」，是因為那樣的話，使用者已經對同步之前的畫面回答過確認。

### 3.3 鎖與死鎖

| 鎖 | 誰拿 | 同步中，主執行緒會不會等它 |
|---|---|---|
| sqlite 寫鎖（`_tx`，`BEGIN IMMEDIATE`，`INDEX_BUSY_MS`＝30 秒） | sync 子行程每筆 `put`／`drop`／`mark_missing`；動作子行程 | 不會：TUI 主執行緒只讀，不寫 index |
| `index.lock`（flock） | `Index()` 在 index 還沒建好（新的、版本升級）時 | 不會：`start` 先 `reload()`（主執行緒建好 index）才起同步。同步中主執行緒的 `Index()` 只在 `_ready()` 為真時建，不拿鎖 |
| `download-staging.lock` | sync 的批次下載，從頭拿到 `rclone copy` 結束（真 Drive 上可能好幾分鐘） | 不會；動作已經被閘門擋住 |
| `upload.lock` | 背景上傳器、push | 不會；`kick_uploader` 在子行程裡 |
| `config.json.lock` | 第一次建 Drive 資料夾 | 不會 |

- **死鎖**：主執行緒只在 asyncio 上等子行程結束（`WaitSync`），子行程不等 TUI 的任何東西。唯一的反向依賴是 stdout 管線：TUI 不讀，子行程寫滿 64 KB 就會卡住。所以讀輸出的是一條獨立的執行緒，一直讀到 EOF，與畫面狀態無關（C-7）。TUI 離開時管線關掉，子行程的寫入失敗（EPIPE），由 `store.terminal_may_be_gone`／`say_out` 吞掉，同步照樣跑完（C-5）。
- **長時間占鎖**：`download-staging.lock` 本來就會被長時間持有（D-21）。本單不改；`WaitSync` 視窗讓使用者看得到在等，可以停止同步（Q-1）或取消動作。
- **`store.sync` 的重入**：同一個 TUI 內，`BackgroundSync.start()` 在執行中回傳 False，不起第二個（C-7）。跨行程（另一個終端的指令、離開後還在跑的同步子行程加上重新開的 TUI）照現在一樣可以並行。結果正確，但會重複下載（R-3）。本單不加跨行程鎖：那是同步演算法層級的改動，與 §2.2 衝突。
- **`throttle` 在背景下的語意不變**：子行程呼叫 `store.sync(paths, throttle=True)`，與現在的 `start` 相同。改變的只有「誰在等」。指示要不要顯示，由 TUI 在起子行程之前用 `store.sync_due(paths)` 判斷（C-1）；被節流時子行程照跑，因為 `index_pending` 要在節流的提早返回之前跑（#222）。
- 子行程的環境：`AGORA_CONFIG`／`AGORA_CACHE_DIR`／`AGORA_STATE_DIR` 明確設成 `self.paths` 的三個目錄，其餘沿用 `os.environ`（`AGORA_RCLONE`、`AGORA_UPLOAD`、`AGORA_FOLDER_NAME` 等）。這樣子行程同步的一定是畫面上這一份，不靠「app 的 paths 剛好等於 `from_env()`」（C-7）。

## 4. 重載時機與狀態保留（套用）

### 4.1 什麼時候算出新的列

子行程結束，`BackgroundSync` 經注入的 `post`（＝`app.call_from_thread`）在主執行緒呼叫 `on_done(outcome)`。`AgoraApp` 接著：

1. 依結果更新 `self.status`（§5），重畫標題列。
2. 不論結果，都起一個 thread worker（`group="sync-rows"`，`exclusive=True`）：`build_rows(self.paths, self._listed)`。
   - 它在這個執行緒裡開自己的 `store.Index`，用完就關。不碰 `self.index`、不碰任何 widget。
   - import 頁用 `self._listed`，也就是上次 `reload` 存下的各 agent `list_sessions()` 結果；不在這裡再列一次 agent。
   - 失敗、離線、停止也要算：`index_pending` 與部分完成的下載，可能已經改了本機。
3. worker 用 `call_from_thread(self._sync_rows_ready, rows)` 交回主執行緒。例外時交回 `None`，訊息列說「同步後讀不到本機清單，請重開」，清單不動。

### 4.2 要不要重畫（「有變化」怎麼判定）

- 判定用列本身：`rows == self.rows`（兩頁都比，`Row` 是 dataclass，逐欄比較）。
  - 這比用 md5 或計數判斷準：畫面上看得到的每一欄（標題、agent、更新時間、雲端欄）都在 `Row` 裡。
  - 「雲端已刪」的標記、outbox 的「未上傳」也都反映在 `cells`。
- 相同：不重畫，不呼叫 `show()`；`self._pending_rows` 清空。
- 不同：交給 §4.3 套用，訊息列說 `row_changes` 的摘要（§5）。

### 4.3 套用的條件與延後

- **可以套用**：`self.screen is self.default_screen`，而且焦點不在 `#filter`、`#search`。
- **不能套用**：把列存在 `self._pending_rows`，開一個 `set_interval(SYNC_APPLY_RETRY=0.5, self._try_apply_sync)`，套用後停掉。
- 下面這些地方也會立刻試一次，所以使用者不必等 0.5 秒：
  - `filter_done`；
  - `on_key` 的 Esc 收起篩選框；
  - `toggle_focus`；
  - `hide_preview_search`／`close_preview_search`；
  - `wait_for_sync` 回傳之前（§3.2 第 3 步）。
- `reload()` 被呼叫時（動作之後），丟掉 `_pending_rows`，停掉重試計時器：`reload` 讀的是同步之後的本機，已經包含它了。`reload` 也把 `_rows_gen` 加一；在那之前就開始算的列，算好時發現世代不同就丟掉，不會用舊的快照蓋掉 `reload` 的結果。
- 寫的動作的閘門看的是 `_sync_idle`（子行程結束**而且**列已算好），不是子行程還在不在（§6.3 `wait_for_sync`）。
- 延後期間打的字照常篩選，篩的是舊的列；套用時用同一個篩選字篩新的列。

### 4.4 套用時保留什麼

`_apply_rows(rows)` 只在主執行緒、只在「可以套用」時跑：

| 狀態 | 規則 |
|---|---|
| `self.index` | 換成主執行緒新開的 `store.Index(self.paths)`（index 已經建好時只是開連線，#485 量到 0.0002 秒） |
| 列 | `self.rows = rows` |
| 勾選 | `self.marked &= 兩頁所有列的 key`（與 `reload(keep_marked=True)` 相同）；拿掉的數目 > 0 時，併進訊息列：「…；N 個勾選的列已不在」 |
| 篩選字（`self.text`）、模式（`self.content`）、篩選框的內容與焦點 | 不動 |
| 內文模式的命中 | `self.text` 非空而且 `self.content`：照 `search()` 的寫法，重算 `self.matches["agora"]`（FTS）；`self.matches["import"]` 不動，不重起 agent 的掃描 |
| 分頁（`self.tab`） | 不動 |
| 游標 | 套用之前 `view = self.capture_view()`，也就是（游標 key、游標列號、游標列號減 `scroll_y`）。`show(view=view, preview=…)`：key 還在就停在它上面；不在就停在 `min(原列號, 列數-1)`；在 `call_after_refresh` 裡把 `scroll_y` 設成 `新列號 - 原高度`（夾在 0 與最大值之間） |
| 預覽快取（`self.cache`） | 拿掉 key 不在新列裡的，以及 `is_stale()` 的；其餘留著（預覽本來就靠 `is_stale()` 判斷檔案換過沒，W11） |
| 預覽區 | 游標的 key 與套用前相同、那一列的 `Row` 相等、而且 `self.shown_preview` 不是 `is_stale()`：`preview=False`，預覽區完全不動（文字、捲動、搜尋標亮、`#pinned`）。否則 `preview=True`，照現在的流程重讀；有搜尋字時 `pending_note = "內容已更新"`（與 `reload` 相同，R5） |
| 預覽搜尋（`self.query`、`search_key`） | 游標的 key 沒變就不動（`show` 裡的 `track_row` 會照現行規則處理 key 變了的情形） |

### 4.5 第一次（mirror 是空的）

第一次授權之後，或 cache 被刪掉時，本機清單是空的，同步一做就是很久：

- 同步中而且 Agora 頁沒有列：訊息列說「正在同步 Drive，清單稍後出現」，不說現在那句「Agora 還沒有 Session…」。
- 套用之後還是沒有列（或同步沒有結果）：才說現在那句。

## 5. UI

| 狀態 | 標題列（`paint_bar` 的最後） | 訊息列（`#msg`） |
|---|---|---|
| 同步中，而且起的時候 `sync_due` 為真 | ` ⟳ 同步 Drive 中（N 秒）`，暗色（`#8a8a8a`）。每秒由 `set_interval(1.0)` 重畫標題列，結束就停 | 不動（第一次見 §4.5） |
| 同步中，但被節流 | 不顯示 | 不動 |
| 完成（`done`），沒有變化 | 拿掉指示；`status = ""` | 不動 |
| 完成，有變化 | 同上 | `同步完成：新增 a、更新 b、移除 c`（數字是 0 的那一段省略；只有勾選被拿掉時另加「；N 個勾選的列已不在」）。用 `say`，不是 failed |
| 離線（`offline`） | `status = "離線：只有本機資料"`（紅，沿用現行的字） | 不動 |
| 失敗（`failed`） | `status = "同步失敗：只有本機資料"`（紅） | `say(outcome.detail, failed=True)`：錯誤那一行，最多 200 字 |
| 停止（`stopped`） | `status = "同步已停止：只有本機資料"`（紅） | 不動 |

- `row_changes` 只算 Agora 頁：新增＝新的有、舊的沒有的 key；移除＝相反；更新＝兩邊都有但 `Row` 不同。import 頁的變化不報（它只會因為別台機器的 continue 或 import 而有細微變化，報了反而讓人困惑），但仍然會套用。
- **可不可以取消**：
  - 沒有獨立的「取消同步」按鍵。
  - 寫的動作的 `WaitSync` 視窗提供「停止同步，馬上做」（Q-1，建議要）。
  - 離開不取消（Q-3）。
- `WaitSync` 視窗（`ModalScreen`）：
  - 標題「背景同步還沒完成」。
  - 選項：`等它完成`（預設）、`停止同步，馬上做`。說明文字是「Esc 取消這個動作」。
  - 下面一行是 spinner 加秒數，與 `Busy` 同一個樣子。
  - 同步結束時自己關掉，回傳真。
  - 選「停止」：呼叫 `BackgroundSync.stop()`，視窗留著，顯示「停止中…」，等子行程結束才回傳真。
  - Esc：回傳假，同步繼續。
  - Enter 走 `OptionList` 的選擇，與 `Choose` 同一套。
- `q`：同步中照常離開（§3.1、Q-3）。`action_quit` 不改；`Run` 的分支照舊。
- `Busy` 的 docstring 改成：只剩 rclone 的授權還這樣跑。

## 6. PR 切分、介面簽名與檔案

依賴：PR-1 ∥ PR-2 → PR-3。PR-1、PR-2 沒有正式呼叫端（PR-1 的 `sync_due` 會被 `store.sync` 用到，見 C-1），可以先 merge。

### 6.1 PR-1：`store` 的兩個小介面、`sync_child`、`tui_sync`、假 rclone 的閘門

- **契約**：C-1～C-7。
- **檔案**：
  - `src/agora/store.py`：加 `OFFLINE_NOTE`、`sync_due`；`sync` 的節流判斷與離線訊息改用它們。其他不動。
  - 新 `src/agora/sync_child.py`（約 60 行）。
  - 新 `src/agora/tui_sync.py`（約 150 行）。
  - `tests/fakes/fake_rclone.py`：加 `FAKE_RCLONE_HOLD`。
  - 測試新檔：`tests/unit/test_sync_child.py`、`tests/unit/test_tui_sync.py`（第一行照既有寫法 `import _guard`）。
- **介面**（`store.py`）：

  ```python
  #: The words sync's offline line starts with; the interactive mode tells "offline" by them (#491).
  OFFLINE_NOTE = "連不上 Drive"

  def sync_due(paths: Paths) -> bool:
      """Whether a throttled sync would talk to Drive now (#491 C-1).

      True when <state>/last-sync is missing, unreadable, not a number, or at least
      SYNC_THROTTLE_S old; False otherwise. Reads one file, never Drive.
      """
  ```

  - `sync` 裡的 `if throttle and stamp.exists() and now() - float(stamp.read_text()) < SYNC_THROTTLE_S:` 改成 `if throttle and not sync_due(paths):`。
  - `say(f"連不上 Drive，改查本機索引：{e}")` 改成 `say(f"{OFFLINE_NOTE}，改查本機索引：{e}")`；字串逐字不變。
- **介面**（`sync_child.py`）：

  ```python
  """The interactive mode's background sync, as a child process (#491).

  Started as `[sys.executable, "-m", "agora.sync_child"]` by `tui_sync`, never through
  `cli.main()`: it runs one throttled `store.sync` and says how it went on the last line.
  """

  RESULT = "AGORA-SYNC "          # last stdout line of a sync that returned: RESULT + JSON
  ERROR = "AGORA-SYNC-ERROR "     # last stdout line of a sync that raised: ERROR + one line

  def main(argv: list[str] | None = None) -> int
      # argv 不用（保留給以後）；paths = store.Paths.from_env()
      # with store.terminal_may_be_gone():
      #     store.sync(paths, throttle=True, warn=_line)     # kick 照預設 True
      # _line(msg) = store.say_out(f"[agora] {msg}", flush=True)
      # 回傳了：say_out(RESULT + json.dumps({"offline": offline}, ensure_ascii=False, sort_keys=True), flush=True)；return 0
      #   offline＝這一次有某個 warn 行以 store.OFFLINE_NOTE 開頭
      # Exception：say_out(ERROR + f"{type(e).__name__}: {e}"（換行換成空白，最多 300 字）, flush=True)；return 2
      # KeyboardInterrupt：不印結果行；return 130

  if __name__ == "__main__":
      sys.exit(main())
  ```

- **介面**（`tui_sync.py`，只用標準函式庫，不 import `tui`、`textual`、`cli`；不 import `store`）：

  ```python
  DONE, OFFLINE, FAILED, STOPPED = "done", "offline", "failed", "stopped"

  @dataclass(frozen=True)
  class SyncOutcome:
      state: str                 # DONE | OFFLINE | FAILED | STOPPED
      detail: str = ""           # FAILED：錯誤那一行（去掉 ERROR 前綴，最多 200 字）；其他是 ""
      lines: tuple[str, ...] = ()   # 子行程的全部輸出（去掉行尾），給測試與日後的檢視

  def parse_outcome(code: int, lines: list[str], *, stopped: bool = False) -> SyncOutcome
      # 規則依序：
      # stopped 或 code == 130 → STOPPED
      # code == 0 且最後一個以 RESULT 開頭的行可解析成 dict：offline 為 True → OFFLINE，否則 DONE
      # code == 2 且有以 ERROR 開頭的行 → FAILED，detail＝那一行去掉前綴
      # 其他（含 code == 0 卻沒有結果行、結果行不是 JSON）→ FAILED，detail＝f"同步子行程結束碼 {code}" 加上最後一行非空輸出（有的話，用「：」接）
      # RESULT／ERROR 這兩個前綴字串在這裡另寫一份（tui_sync 不 import sync_child 以外的 agora 模組；
      #   要 import sync_child 的兩個常數也可以，sync_child 頂層只 import store——實作者擇一，報告寫明）

  def child_argv() -> list[str]          # [sys.executable, "-m", "agora.sync_child"]

  def child_env(config: Path, cache: Path, state: Path) -> dict[str, str]
      # {**os.environ, "AGORA_CONFIG": str(config), "AGORA_CACHE_DIR": str(cache),
      #  "AGORA_STATE_DIR": str(state), "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}

  def spawn_child(argv: list[str], env: dict[str, str]) -> subprocess.Popen
      # 與 AgoraApp.spawn 相同的 Popen 參數：stdin=DEVNULL, stdout=PIPE, stderr=STDOUT, text=True,
      # encoding="utf-8", errors="replace", bufsize=1, start_new_session=True, env=env

  class BackgroundSync:
      """One background sync child at a time; tells `on_done` once, on the app's thread."""

      def __init__(self, *, spawn: Callable[[], Any], post: Callable[..., Any],
                   on_done: Callable[[SyncOutcome], None], stop_group: Callable[[int], None],
                   clock: Callable[[], float] = time.monotonic): ...
          # spawn()：回傳一個像 Popen 的物件（有 stdout 可迭代、wait()、poll()、pid）。
          #   正式是 lambda: spawn_child(child_argv(), child_env(...))；測試給假的（DripProc 類）
          # post(fn, *args)：在 app 的執行緒呼叫 fn；正式是 app.call_from_thread
          # stop_group(pgid)：正式是 app.stop_group（SIGINT→SIGTERM→SIGKILL）

      running: bool        # property：start 成功之後，到 on_done 被呼叫之前為 True
      started_at: float | None
      def elapsed(self) -> float      # running 時是 clock() - started_at，否則 0.0
      def start(self) -> bool
          # running → False，什麼都不做。否則 spawn()（OSError → 直接 post(on_done, SyncOutcome(FAILED, f"叫不起同步子行程：{e}"))，回傳 True）
          # 起一條 daemon 執行緒讀 stdout 到 EOF，收集 lines（去掉行尾），然後 code = proc.wait()，
          # 再 post(self._finish, code, lines)。讀或 wait 丟例外 → code=2，lines 加一行「讀不到輸出：<e>」（照 Run.read 的 L6）
          # post 丟例外（app 已經不在）→ 吞掉
      def stop(self) -> bool
          # 沒在跑 → False。已經停過 → False（不重送；照 Run 的 L3）。否則 self.stopped = True，
          # pid 是 int 就 stop_group(pid)，否則 proc.send_signal(SIGINT)（測試的假物件）；回傳 True
      def _finish(self, code: int, lines: list[str]) -> None
          # 在 app 的執行緒：running=False，outcome = parse_outcome(code, lines, stopped=self.stopped)，on_done(outcome)；只會一次
  ```

- **`FAKE_RCLONE_HOLD`**（`fake_rclone.py`，放在 `FAKE_RCLONE_DELAY` 那一段的旁邊、寫 calls.log 之前）：
  - 設了這個變數，而且這一次是 `lsjson` 加 `-R`（sync 的列表）時：
    1. 先 touch `<值>.waiting`；
    2. 每 0.02 秒看一次 `<值>` 這個檔在不在，在了才往下走；
    3. 30 秒還沒出現，就 `stderr` 寫 `FAKE_RCLONE_HOLD timed out`，exit 1。
  - 其他呼叫不受影響。
  - 這是測試替身的閘門：測試確定同步卡在列表那一步，而不是賭一個 sleep 夠長。
  - 模組 docstring 照其他開關的寫法補一段。
- **測試**：
  - `test_sync_child.py`：用假 rclone，同進程呼叫 `sync_child.main()`，用 `capsys` 讀 stdout。
    - 遠端 3 筆：exit 0，最後一行 `AGORA-SYNC {"offline": false}`，index 有那 3 筆，`last-sync` 寫了。
    - `FAKE_RCLONE_FAIL=lsjson`：exit 0，最後一行 `{"offline": true}`，前面有一行以 `[agora] 連不上 Drive` 開頭。
    - monkeypatch `store.sync` 丟 `RuntimeError("壞\n了")`：exit 2，最後一行 `AGORA-SYNC-ERROR RuntimeError: 壞 了`。
    - 丟 `KeyboardInterrupt`：exit 130，沒有結果行。
    - C-5：用 `subprocess.Popen([sys.executable, "-m", "agora.sync_child"], stdout=PIPE, env=…)`，立刻關掉讀端，`wait(timeout=60)`：exit 0，`last-sync` 寫了。遠端 12 筆，讓它走批次下載，而且一定會印「背景上傳中」以外的行：放一個 header 壞掉的，讓它印「讀不到，先跳過」。
  - `test_tui_sync.py`：不用 Textual。`post` 用 `lambda fn, *a: fn(*a)` 同步呼叫，假行程用 `threading.Event` 當閘門（照 `test_tui.py` 的 `DripProc`），測試等 `on_done` 用 `threading.Event`，加 `wait(timeout=10)`；不用 sleep。
    - `parse_outcome` 的每一列規則各一個。
    - `start` 兩次只 spawn 一次。
    - `on_done` 只被呼叫一次。
    - `stop` 對 int pid 呼叫 `stop_group`，第二次不呼叫。
    - stop 之後 code 是 0，也是 `STOPPED`。
    - `spawn` 丟 `OSError` → `FAILED`。
    - stdout 迭代丟例外 → `FAILED`，`detail` 含「讀不到輸出」。
    - `child_env` 三個目錄與 `PYTHONUNBUFFERED`。
  - `test_store*.py`（加在 `tests/unit/test_store_more.py`）：
    - `sync_due`：沒有檔、空檔、`abc`、`now()-299`（False）、`now()-300`（True）。
    - `store.sync(throttle=True)` 在 `last-sync` 是 `abc` 時照常同步（不丟 ValueError）。
    - 離線訊息逐字等於 `"連不上 Drive，改查本機索引：" + …`。
  - `fake_rclone` 的閘門：在 `test_sync_child.py` 裡一個。起 `sync_child` 子行程（`FAKE_RCLONE_HOLD`），等 `.waiting` 出現（`while not exists: time.sleep(0.02)`，最多 10 秒；這是等檔案出現，不是賭時間），確認 `poll() is None`，建出閘門檔，`wait(timeout=60)` 得到 exit 0。
- **合上後 main**：行為不變。唯一的差別是壞掉的 `last-sync`（不是數字）從 ValueError 變成照常同步。

### 6.2 PR-2：`tui` 的純函式與 `show` 的新參數

- **契約**：C-8～C-11。
- **檔案**：`src/agora/tui.py`；測試新檔 `tests/unit/test_tui_rows.py`（`from test_tui import _hdr, _index, FakeAgent, FakeCli, _run, _app`；repo 裡有前例，例如 `test_cli_incremental.py`）。
- **介面**（`tui.py`，放在 `import_rows` 旁邊）：

  ```python
  def list_agent_sessions(agents: list) -> dict[str, list]
      # {agent.name: agent.list_sessions()}；某個 agent 丟例外 → 那一個是 []，
      # stderr 照現行的「[agora] <name> 的 session 清單讀不到：<e>」（review U1）

  def import_rows(index: store.Index, agents: list, paths: store.Paths | None = None,
                  listed: dict[str, list] | None = None) -> list[Row]
      # listed 給了就用 listed.get(agent.name, [])，不呼叫 list_sessions；沒給就照現在（內部改呼叫 list_agent_sessions）

  def build_rows(paths: store.Paths, listed: dict[str, list], agents: list) -> dict[str, list[Row]]
      # 在呼叫它的執行緒開自己的 store.Index(paths)，回傳
      # {"agora": agora_rows(index, [], paths), "import": import_rows(index, agents, paths, listed)}；
      # 結束前 index.db.close()（finally）

  @dataclass(frozen=True)
  class RowChanges:
      added: int
      changed: int
      removed: int

  def row_changes(old: list[Row], new: list[Row]) -> RowChanges
      # 依 key：added＝new 有 old 沒有；removed＝old 有 new 沒有；changed＝兩邊都有而 Row 不相等
  def change_note(changes: RowChanges, dropped_marks: int) -> str
      # "同步完成：新增 2、更新 1"；三個都是 0 而 dropped_marks 也是 0 → ""；
      # dropped_marks > 0 → 後面加 "；{n} 個勾選的列已不在"（三個都是 0 時是 "同步完成；…"）
  ```

  - `AgoraApp.reload` 改成：`self._listed = list_agent_sessions(self.agents)`，然後 `import_rows(self.index, self.agents, self.paths, self._listed)`。`__init__` 加 `self._listed: dict[str, list] = {}`。結果與現在相同。
  - `AgoraApp` 新方法：

  ```python
  View = tuple[str | None, int, int]       # (cursor key, cursor row, cursor row - scroll_y)

  def capture_view(self) -> View
  def show(self, keep: str | None = None, *, preview: bool = True, view: View | None = None) -> None
      # 現行行為不變（preview=True、view=None）。
      # view 給了就不看 keep：key 在 rows 裡 → 移到它；否則 → min(view[1], len(rows)-1)（沒有列就不移）；
      #   然後 call_after_refresh：table.scroll_to(y=max(0, 新列號 - view[2]), animate=False)
      # preview=False：不呼叫 self.preview()（不排預覽計時器）；track_row、paint_bar、paint_keys、gutter 照舊
  def rows_by_keys(self, keys: list[str]) -> list[Row] | None
      # 在 self.shown() 裡依畫面順序找回這些 key；少了任何一個 → None
  ```

- **測試**（`test_tui_rows.py`）：
  - `list_agent_sessions` 一個 agent 丟例外，另一個照常。
  - `import_rows(listed=…)` 與不給 `listed` 結果相同，而且給了 `listed` 時假 agent 的 `list_sessions` 被呼叫 0 次（FakeAgent 加計數，或包一層）。
  - `build_rows` 在另一條執行緒裡跑（`threading.Thread`），結果等於主執行緒的 `agora_rows`／`import_rows`。主執行緒的 `app.index` 在那之後還能查（沒有共用連線）。
  - `row_changes` 三種各一個，加一個全都相同。
  - `change_note` 的四種字串逐字比對。
  - `show(view=…)`：60 列，`run_test(size=(120, 30))`，游標移到第 40 列。
    - 記下 `cursor_row - scroll_y`；在最前面插一列後 `show(view=…)`，`await pilot.pause()`：游標在同一個 key（第 41 列），`cursor_row - scroll_y` 不變。
    - key 被拿掉：游標在第 40 列。
    - 列數變 10：游標在第 9 列。
  - `show(preview=False)`：monkeypatch `app.preview` 計數，0 次；`show()` 1 次。
  - `rows_by_keys` 依畫面順序；少一個 → None；被篩選掉的 → None。
- **合上後 main**：行為不變。

### 6.3 PR-3：接上

- **契約**：C-12～C-21（含 C-20′）；C-22（探針）與 C-23（整合）也在這個 PR。
- **檔案**：
  - `src/agora/tui.py`；
  - 測試新檔 `tests/unit/test_tui_background_sync.py`（`from test_tui import …`）、`tests/integration/test_tui_background_sync.py`；
  - 探針 `tests/perf/measure_tui_start.py`（不進 CI，照 `tests/perf/measure_back_to_list.py` 的隔離與 `NOT ISOLATED` 檢查）。
- **介面**（`tui.py`）：

  ```python
  SYNC_APPLY_RETRY = 0.5           # 延後的套用每隔多久再試一次（秒）
  SYNC_TICK = 1.0                  # 同步中標題列的秒數多久重畫一次

  class AgoraApp(App):
      def __init__(self, paths, cli, agents=None, check_setup=True, sync_on_start: bool | None = None): ...
          # sync_on_start 為 None 時等於 check_setup（正式路徑與現在一樣：check_setup 才同步）
          # self.sync = tui_sync.BackgroundSync(spawn=self.spawn_sync, post=self.call_from_thread,
          #                                     on_done=self.sync_done, stop_group=self.stop_group)
          # self._sync_shown = False         # 這一次同步要不要顯示指示（起的時候 sync_due）
          # self._pending_rows: dict[str, list[Row]] | None = None
          # self._sync_idle = asyncio.Event()；在 on_mount 建（要在 app 的 loop 上），沒在同步時是 set
          # self.sync_settled = asyncio.Event()   # 測試的鉤子：這一次同步的列已經算好、而且套用或判定不必套用
          # 計時器：self._sync_tick_timer、self._apply_timer

      def spawn_sync(self):
          # tui_sync.spawn_child(tui_sync.child_argv(), tui_sync.child_env(self.paths.config, self.paths.cache, self.paths.state))
          # 測試替換這一個（與 spawn 同一種接縫）

      @work
      async def start(self) -> None:
          # 1. check_setup 而且 setup() 失敗 → exit（不變）
          # 2. self.reload()；空清單提示（§4.5：要同步時說「正在同步 Drive，清單稍後出現」）；聚焦 #table
          # 3. sync_on_start → self.begin_sync()

      def begin_sync(self) -> bool:
          # self._sync_shown = store.sync_due(self.paths)；self.sync.start() 為 False → return False
          # self._sync_idle.clear()；self.sync_settled.clear()；_sync_shown 時開 _sync_tick_timer（paint_bar）；paint_bar()

      def sync_done(self, outcome: tui_sync.SyncOutcome) -> None:     # 主執行緒
          # 停 _sync_tick_timer；依 §5 設 self.status 與 say；paint_bar()；self.compute_sync_rows(self._rows_gen)

      @work(thread=True, exclusive=True, group="sync-rows")
      def compute_sync_rows(self, gen: int) -> None:
          # gen = self._rows_gen（在主執行緒起 worker 之前取，當參數傳進來；簽名是 compute_sync_rows(self, gen: int)）
          # rows = build_rows(self.paths, dict(self._listed), self.agents)（例外 → None）
          # self.call_from_thread(self._sync_rows_ready, rows, gen)（app 不在 → 吞掉）

      def _sync_rows_ready(self, rows: dict[str, list[Row]] | None, gen: int) -> None:
          # gen != self._rows_gen（算列期間有過 reload）→ 丟掉 rows，只做最後那一步（set 兩個事件）
          # None → say("同步後讀不到本機清單，請重開", failed=True)，結束
          # rows == self.rows → 不重畫
          # 否則 self._pending_rows = rows；self._try_apply_sync()；還沒套用就開 _apply_timer
          # 最後（不論哪一種）：self._sync_idle.set()；沒有 pending 時 self.sync_settled.set()

      def can_apply_sync(self) -> bool:
          # self.screen is self.default_screen and getattr(self.focused, "id", None) not in ("filter", "search")

      def _try_apply_sync(self) -> bool:
          # 沒有 pending → True；不能套用 → False；否則 _apply_rows(pending)、清掉 pending、停 _apply_timer、
          # self.sync_settled.set()，回傳 True

      def _apply_rows(self, rows: dict[str, list[Row]]) -> None:
          # §4.4 的表；最後 self.show(view=view, preview=need_preview)，再 say(change_note(...))（非空時）

      async def wait_for_sync(self) -> bool:
          # 判斷用 self._sync_idle，不用 self.sync.running：子行程結束到列算好之間（compute_sync_rows 還在跑）
          #   sync.running 已經是 False，但還不能讓動作開始，否則晚到的舊列會蓋掉動作之後 reload 的結果
          # _sync_idle 已 set：self._try_apply_sync()（在這裡一定可以套用：動作是在預設畫面上按的）；return True
          # 否則：ok = await self.push_screen_wait(WaitSync(self.sync, self._sync_idle))
          #        ok 為 False → return False
          #        await self._sync_idle.wait()（停止的情形，視窗已經等過；這裡只是保險）
          #        self._try_apply_sync()；return True
  ```

  ```python
  class WaitSync(ModalScreen):
      """An action waits here for the background sync (#491 §5). Dismisses True (go on) or False (Esc)."""
      BINDINGS = [Binding("escape", "dismiss(False)", "取消這個動作")]
      def __init__(self, sync: tui_sync.BackgroundSync, idle: asyncio.Event): ...
      # compose：標題「背景同步還沒完成」、OptionList("等它完成", "停止同步，馬上做")、
      #          Static(id="spin")、Static("Esc 取消這個動作", classes="hint")
      # on_mount：set_interval(0.12, tick)；self.run_worker(self._watch(), exclusive=True)，
      #   其中 _watch 是 `await idle.wait(); self.dismiss(True)`
      # 選 0：什麼都不做（繼續等）；選 1：self.sync.stop()，spin 那一行前面加「停止中… 」
      # CSS：與 Choose 等同一行置中（AgoraApp.CSS 的 `Choose, AskText, Busy, Run, Tell` 加上 WaitSync）
  ```

  - **閘門的接法**（§3.2）：
    - `action_primary`（兩頁）、`action_merge`、`action_edit`、`action_pull`、`action_push`、`action_delete` 各自在「算好列、判斷沒有列」之後，插進下面這一段：

      ```python
      keys = [r.key for r in rows]
      if not await self.wait_for_sync():
          return
      rows = self.rows_by_keys(keys)
      if rows is None:
          self.say("同步後清單變了：有列已不在或被篩選掉，動作取消，請再確認", failed=True)
          return
      ```

      然後才進原本的確認視窗。
    - `action_primary` 的 Agora 頁與 `action_edit` 用 `row = rows[0]`（與現在的 `self.current()` 是同一列）。
    - `act()` 本身不加閘門；閘門在呼叫它之前已經過了。這樣 `import` 一個 agent 一個指令的迴圈不會在每一輪都問一次。
  - `reload()`：開頭 `self._rows_gen += 1`，丟掉 `_pending_rows`、停掉 `_apply_timer`（§4.3）。`__init__` 加 `self._rows_gen = 0`。
  - 立刻試一次套用的地方：`filter_done`、`on_key` 收起篩選框的分支、`toggle_focus` 最後、`hide_preview_search` 與 `close_preview_search` 最後，各加一行 `self._try_apply_sync()`。
  - `paint_bar`：在 `status` 之前，`self.sync.running and self._sync_shown` 時加 ` ⟳ 同步 Drive 中（{int(self.sync.elapsed())} 秒）`，樣式 `#8a8a8a`。
  - `on_unmount`：停掉 `_sync_tick_timer`、`_apply_timer`。**不**停子行程。
  - `Busy` 的 docstring：改成只剩授權（§5）。
  - 現有的 `Busy("同步 Drive", …)` 那兩行與 `"連不上 Drive" in out` 的判斷刪掉（`OFFLINE_NOTE` 的判斷在 `sync_child`）。
- **測試**（`tests/unit/test_tui_background_sync.py`）：
  - 共通的 app：`tui.AgoraApp(paths, FakeCli(), agents=[…], check_setup=False, sync_on_start=True)`。
    - `app.spawn_sync` 換成回傳 `SyncProc` 的函式。`SyncProc` 照 `DripProc`：`release(lines, code)` 之前 `stdout` 會擋住；它記錄收到的訊號。
    - `app.spawn` 照 `_spawn()` 記錄動作。
  - 要讓同步「改了本機」，測試在 `release` 之前，自己在 `paths` 的 mirror／index 寫入新的 session（`index.put`，與 `_index` 同一個寫法）。這等於子行程做的事，不必跑真的 sync。
  - 等待一律用事件：`await asyncio.wait_for(app.sync_settled.wait(), 5)`，之後 `await pilot.pause()`；不用 `pilot.pause(<秒數>)`。
  - 要測「套用被延後」時，斷言 `app._pending_rows is not None`，然後做那個會觸發套用的動作（Esc、Enter），再等 `sync_settled`。
  - `sync_due` 用 monkeypatch `store.sync_due` 決定真或假。
- **整合測試**（`tests/integration/test_tui_background_sync.py`，C-23）：
  - 真的 `spawn_sync`（子行程）、假 rclone、`FAKE_RCLONE_HOLD`、`AGORA_UPLOAD=inline`。
  - 本機 2 筆，遠端 12 筆，其中 2 筆的 md5 與本機不同。
  - `run_test` 之後等 `<hold>.waiting` 出現：在 `async` 裡用 `await asyncio.sleep(0.02)` 迴圈，最多 20 秒。這是等外部檔案，不是賭時間；這是整個設計裡唯一的輪詢，而且只在整合測試裡。
  - 斷言：表格 2 列；`down` 讓游標到 1；標題列有「同步 Drive 中」；按 `d` 出現 `WaitSync`。
  - Esc：`app._last_spawned == []`。
  - 建出閘門檔，等 `sync_settled`。斷言：表格 12 列，訊息列是「同步完成：新增 10、更新 2」，`last-sync` 寫了。
- **探針**（`tests/perf/measure_tui_start.py`，C-22）：
  - 照 `measure_back_to_list.py` 的隔離做；假資料照 `REPORT-tui-slow.md` §2 的形狀：3000 筆，body 約 3 KB。
  - 本機 mirror／index 是舊的 3000 筆，遠端是 md5 全都不同的 3000 筆。
  - 正式的 `spawn_sync`，`sync_on_start=True`，`check_setup=False`。
  - 量三件事：
    - 從 `run_test` 進入，到「表格有 3000 列而且 `down` 之後 `cursor_row == 1`」；
    - 到 `sync_settled`；
    - 套用那一刻主執行緒的凍結時間：用 `set_interval(0.05)` 的 ticker，看最大的間隔。
  - 各 3 次取中位數，輸出 JSON。L=0 與 L=100×3 各一組。
  - 門檻只有第一項 < 3 秒（L=0）；不符就 exit 1。
- **合上後 main**：啟動不再等同步；同步中寫的動作會等；其餘行為不變。

### 6.4 給實作者的共通事項

- 依賴方向照 §8.2：
  - `tui_sync` 不 import `tui`、`textual`、`cli`、`store`。
  - `sync_child` 只 import `store`（與標準函式庫）。
  - `tui` 不 import `sync_child`（行程邊界，走 `tui_sync.child_argv()`）。
- 不改 `store.sync` 的步驟、順序與常數；PR-1 在 `store` 只動 §6.1 寫的兩處。
- 既有測試的斷言不改。現有的 TUI 測試都用 `check_setup=False`，`sync_on_start` 預設跟著它，所以不會起同步。有既有測試轉紅，先回報架構師，不要改斷言。
- UI 一律在主執行緒更新；執行緒只經 `call_from_thread`（`BackgroundSync` 用注入的 `post`）。執行緒裡不碰 `self.index`、不碰 widget。
- 不連真 Drive；隔離照 AGENTS.md（`isolated_home`、`AGORA_RCLONE` 指到假 rclone 的包裝腳本、`AGORA_UPLOAD=inline`）。
- 只停自己開的行程：測試起的 `sync_child` 子行程要在 `finally` 裡 `wait` 或 kill 自己的 pid，不要用 `pkill`。
- 實作報告逐條把 C-x 對到測試名稱，測試名稱帶契約編號（例如 `test_c3_result_line_is_last`）。

## 7. 契約（每條都有測試對應；實作報告逐條對到測試名稱）

### PR-1

- **C-1** `store.sync_due(paths)`：`<state>/last-sync` 不存在、讀不出、內容不是數字、或 `now() - 值 >= SYNC_THROTTLE_S` → True；否則 False。不連 Drive。`store.sync(throttle=True)` 在 `not sync_due` 時提早返回（`index_pending` 照樣在它之前跑），否則照常同步；`last-sync` 不是數字時照常同步（現在是 ValueError）。
- **C-2** `store.OFFLINE_NOTE == "連不上 Drive"`；`sync` 的離線那一行逐字是 `f"連不上 Drive，改查本機索引：{e}"`。
- **C-3** `sync_child.main()` 恰好跑一次 `store.sync(paths, throttle=True, warn=…)`，`paths == Paths.from_env()`。每個 warn 行以 `[agora] ` 開頭寫到 stdout 並 flush。回傳了：最後一行是 `AGORA-SYNC ` 加 JSON 物件，`offline` 只在這一次有 warn 行以 `OFFLINE_NOTE` 開頭時為 true；exit 0。
- **C-4** `store.sync` 丟 `Exception`：最後一行是 `AGORA-SYNC-ERROR <型別名>: <訊息>`（一行、最多 300 字），exit 2。丟 `KeyboardInterrupt`：沒有結果行，exit 130。
- **C-5** stdout 的讀端在同步中被關掉（EPIPE）：同步照樣跑完（`last-sync` 寫了），exit 0。
- **C-6** `tui_sync.parse_outcome` 照 §6.1 的規則表。`stopped=True` 一律是 `STOPPED`；`code == 0` 但沒有可解析的結果行是 `FAILED`；`detail` 最多 200 字。
- **C-7** `BackgroundSync`：
  - 執行中再 `start()` 回傳 False，而且不 spawn。
  - `on_done` 對每一次 `start` 恰好被呼叫一次，而且是經 `post` 呼叫。
  - 讀輸出不依賴 `on_done` 的呼叫端：輸出超過 64 KB 也不會卡住，測試用假行程給 2000 行。
  - `stop()` 第一次對 int pid 呼叫 `stop_group` 恰好一次，第二次回傳 False、不再呼叫。
  - `spawn` 丟 `OSError` → `FAILED`。
  - `child_env` 的三個 `AGORA_*` 目錄等於給的參數。

### PR-2

- **C-8** `import_rows(..., listed=L)` 與不給 `listed` 時，同一份 agent 資料得到的列相同，而且給了 `listed` 時不呼叫任何 agent 的 `list_sessions`。`list_agent_sessions` 一個 agent 失敗只影響它自己（U1）。
- **C-9** `build_rows` 在任何執行緒都能呼叫，用自己的 `Index`，結束時關掉連線，結果等於同一份資料上的 `agora_rows`＋`import_rows(listed=…)`。
- **C-10** `row_changes`／`change_note` 照 §6.2。
- **C-11** `show()` 不帶新參數時行為不變。`show(view=…)` 的游標規則照 §6.2（key 在就跟著 key，不在就停在 `min(原列號, 列數-1)`），游標在畫面上的高度（`cursor_row - scroll_y`）不變（能捲到的範圍內）。`show(preview=False)` 不排預覽。`rows_by_keys` 照畫面順序，少一個就是 None。

### PR-3

- **C-12** `sync_on_start=True` 時，在背景同步還被擋住的情況下：表格已經有本機的列，焦點在表格，`down` 讓 `cursor_row` 由 0 變 1。`spawn_sync` 在 `reload` 之後才被呼叫（測試讓 `spawn_sync` 斷言 `app.rows["agora"]` 已經有列）。
- **C-13** 標題列：同步中而且起的時候 `sync_due` 為真，含 `同步 Drive 中`；`sync_due` 為假時不含。結束後照 §5 的表：`done` 時拿掉，`offline`／`failed`／`stopped` 時 `status` 各是那三個字串，`failed` 時訊息列是 `detail` 而且是 failed 樣式。
- **C-14** 同步結束後：
  - 新的列與現在相同：`show` 被呼叫 0 次，訊息列不變。
  - 不同：`show` 恰好 1 次，游標在同一個 key，被拿掉的列的勾選消失、其他勾選留著，訊息列等於 `change_note(...)`。
- **C-15** 結束時有 `Confirm`／`Choose` 視窗開著，或焦點在 `#filter`、`#search`：不套用（`_pending_rows` 不是 None，表格的列沒變）。在篩選框繼續打字也不套用。之後下面任何一個都會套用：Enter 收起篩選框、Esc 收起篩選框、Tab 切出去、關掉預覽搜尋框、關掉視窗之後的下一次重試。套用時篩選字不變。`reload()` 之後 `_pending_rows` 是 None。
- **C-16** 套用時，游標那一列的 key 與 `Row` 沒變，而且預覽檔沒換：`put_preview` 被呼叫 0 次，`shown_preview` 是同一個物件，預覽搜尋的 `query` 與 `which` 不變。檔案換了（測試改寫 mirror 的 `session.md`）：重讀一次；有搜尋字時，`#count` 那一行帶「內容已更新」。
- **C-17** 內文模式下有篩選字時，套用之後 `matches["agora"]` 含新列裡命中的 key，`find_in_agents` 不被重起（monkeypatch 計數 0）。
- **C-18** 同步中，下面七個動作都會出現 `WaitSync`，而且在同步結束之前 `app.spawn` 不被呼叫、`cli.calls`（`outside`）是空的：Agora 頁的 `d`、`m`、`e`、`p`、`P`、Enter，以及未匯入頁的 Enter。
  - 在 `WaitSync` 按 Esc：什麼都不起，勾選不變，同步繼續。
  - 選「停止同步，馬上做」：`SyncProc` 收到 SIGINT（經 `stop_group`，測試用 `group_calls` fixture 的寫法記錄），子行程結束後動作照常進確認視窗，`status` 是「同步已停止：只有本機資料」。
  - 等到同步結束：視窗自己關掉，先套用，再用 key 找回列：
    - 都在：確認視窗顯示的是新列的標題；
    - 少一個：不起子行程，訊息列是 §6.3 那一句。
- **C-19** 沒在同步時，七個動作都不出現 `WaitSync`，起的指令與現在相同（既有測試全過就是證據，另加一個 `sync_on_start=True`、同步已結束之後按 `d` 的測試）。
- **C-20** 同步中按 `q`：app 結束；`SyncProc` 沒有收到任何訊號；`app._groups` 不含它的 pid。（測試在 `finally` 裡放行 `SyncProc`，不留卡住的執行緒。）
- **C-20′** 子行程已結束、列還在算（`compute_sync_rows` 被測試用 `threading.Event` 擋住）時按 `d`：出現 `WaitSync`，`app.spawn` 不被呼叫；放行之後才進確認視窗。`reload` 之後才到的列（世代不同）不被套用：表格的列等於 `reload` 的結果。
- **C-21** 第一次（mirror 是空的）：同步中訊息列是「正在同步 Drive，清單稍後出現」；套用之後還是空的，才是現在那句「Agora 還沒有 Session…」。
- **C-22**（探針，不進 CI）：§2.1 G-1 的量法，L=0 的中位數 < 3 秒。報告附三項數字與 L=100×3 的數字。
- **C-23**（整合）：§6.3 的整合測試。真的子行程被假 rclone 擋在列表時，清單可操作、`d` 等候、Esc 不起東西；放行之後列數與訊息列正確，`last-sync` 寫了。

## 8. 結構（模組、介面、依賴方向）

### 8.1 模組切割

| 模組 | 職責 | 不做什麼 |
|---|---|---|
| `store`（改兩處） | `sync_due`：節流的唯一判斷；`OFFLINE_NOTE`：離線那一行的開頭 | 不知道 TUI，不知道子行程 |
| `sync_child`（新） | 子行程本體：跑一次節流同步，用最後一行說結果 | 不畫畫面，不解讀結果，不經 `cli.main`（不跑 `recover_pending`、不印提醒，與 `background` 同理） |
| `tui_sync`（新） | 起子行程、讀輸出、只通知一次、停止、把結束碼與輸出解讀成 `SyncOutcome` | 不 import Textual，不碰列、不碰畫面、不決定何時套用 |
| `tui`（改） | 啟動順序、標題列、`WaitSync`、閘門、算列（`build_rows`）、套用與延後、保留狀態 | 不跑同步本身，不解讀子行程的輸出格式 |

### 8.2 依賴方向（新增的邊以 ＋ 標示；沒有環）

```
store       → header；lazy: background                         （不變）
sync_child  → store                                            ＋（新模組）
tui_sync    → （標準函式庫）                                   ＋（新模組；可選擇 import sync_child 的兩個常數，見 §6.1）
tui         → cache, header, store, agents.agy, tui_sync＋；lazy: agents.base, cli
```

- 行程邊界：`tui_sync` 以 `python -m agora.sync_child` 起子行程，不 import 它的程式（除了上面那兩個常數）。與 `background`、`round_child` 的做法相同。
- `tui_sync` 不 import `store`：它需要的三個目錄由 `tui` 用參數傳進來，所以它的測試不必建 `Paths`。

### 8.3 SOLID

- **S**：同步本身（`store`）、子行程協議（`sync_child`／`parse_outcome`）、行程生命週期（`BackgroundSync`）、畫面（`tui`）各有一個變更理由。結果行的格式只在 `sync_child` 與 `parse_outcome` 兩處，由 C-3／C-6 一起鎖住。
- **O**：以後加手動再同步（Q-2），只是再呼叫 `begin_sync()`；加別的結果種類，只是在 `parse_outcome` 與 §5 的表各加一列，不必改 `BackgroundSync`。
- **L**：`spawn` 回傳的是「像 Popen 的物件」，正式的 `Popen` 與測試的 `SyncProc` 都滿足 `stdout` 可迭代、`wait()`、`poll()`、`pid`。這與 `Run` 對 `FakeProc` 的要求相同。
- **I**：`BackgroundSync` 只要求四個可呼叫物件（`spawn`、`post`、`on_done`、`stop_group`），不要求整個 App。
- **D**：`BackgroundSync` 依賴注入的 `post`／`stop_group`，不依賴 Textual；`tui` 依賴 `tui_sync` 的抽象（`SyncOutcome`），不依賴子行程的輸出格式。
- **不符合的地方**：
  - `tui.py` 會從 2281 行長到約 2500 行。理由：閘門、套用與 `WaitSync` 都要碰 `AgoraApp` 的狀態（`rows`、`marked`、`cache`、焦點），拆出去只會多一層傳參。
  - 與 P-7、P-11 同類，記一筆待決 P-17（§11）：日後把「列的資料面」（`Row`、`agora_rows`、`import_rows`、`build_rows`、`row_changes`、`filtered`）切成 `tui_rows.py`。

### 8.4 被捨棄的方案

- **背景執行緒（`@work(thread=True)`）**：§3.1。最致命的是同步中離開時要等最多 300 秒，而且沒辦法中斷。
- **動作與同步並行**：§3.2。刪除的 Session 會復活，而且動作的同步反而變慢。
- **動作直接取消背景同步再做**：按 `d` 就悄悄停掉同步，使用者不知道同步沒做完，下一次啟動前清單都是舊的。改成讓人選（Q-1）。
- **閘門放在起子行程之前（問完問題之後）**：使用者是對同步前的畫面回答確認的。
- **用 md5 集合或 index 的計數判斷有沒有變化**：漏掉雲端欄與「未上傳」這類不在 md5 裡的變化，還要多一個查詢。直接比 `Row` 最準，而且 3000 列的比較是毫秒級（推論，沒有量）。
- **套用時重算 `import_rows`（重新列 agent）**：會在執行緒裡碰 adapter 的模組層快取，與內文搜尋的 worker 並行（§3.1）。sync 不改 agent 的資料，用上次的清單就夠。
- **加跨行程的同步鎖**：會讓另一個終端的指令在 TUI 背景同步時卡住，而且是同步演算法層級的改動（§2.2）。
- **新的公開指令 `agora sync`**：要進 spec 的指令清單、要說明、要處理參數；這裡只需要一個內部入口，`background` 與 `round_child` 已經有前例。

## 9. 主 spec 要補的地方（PM 撰寫；架構師只看介面能否一致表達）

`openspec/specs/interactive-mode/spec.md` 建議新增一個 Requirement「啟動與背景同步」。草稿：

- 互動模式啟動時 MUST 先顯示本機清單，而且清單 MUST 在不等 Drive 的情況下可以操作（移動游標、換頁、勾選、篩選、預覽）；距上次同步超過節流時間時，MUST 在背景同步一次。
- 背景同步中，標題列 MUST 顯示「同步 Drive 中」；結束時 MUST 依結果顯示：完成時不顯示；離線時「離線：只有本機資料」；失敗時「同步失敗：只有本機資料」，並且說明原因；停止時「同步已停止：只有本機資料」。清單有變化時 MUST 說新增、更新、移除各幾個。
- 同步完成後清單有變化時，MUST 保留游標所在的 Session、篩選字與模式、仍在清單裡的勾選；預覽的 Session 沒有變時，MUST NOT 重讀預覽。有視窗開著、或正在篩選框或預覽搜尋框輸入時，MUST 等它們結束才更新清單。
- 背景同步中，會啟動指令或交出終端的動作（import、merge、delete、pull、push、continue、edit）MUST 等同步結束才執行，等的時候 MUST 讓使用者選擇等待、停止同步後執行、或取消動作。等完之後，若動作要處理的 Session 有任何一個已不在清單，MUST NOT 執行，並且說明。
- 背景同步中離開，MUST NOT 等同步結束。
- Scenario 建議三個：
  - 遠端大量變動時啟動，清單立刻可操作；
  - 同步中按 `d`，先等候，Esc 不刪；
  - 同步完成時游標與勾選保留。

## 10. 改壞驗證清單（mutation；各 PR 做自己的那幾項，報告逐項附「改了什麼、哪個測試轉紅」）

| PR | 改壞 | 應該轉紅的契約 |
|---|---|---|
| 1 | `sync_due` 把 `>=` 改成 `>` | C-1（剛好 300 秒那一個） |
| 1 | `sync_due` 遇到不是數字時回 False | C-1 |
| 1 | `sync_child` 的結果行在 warn 行之前印 | C-3 |
| 1 | `offline` 改成「任何 warn 行」 | C-3 |
| 1 | `sync_child` 不包 `terminal_may_be_gone` | C-5 |
| 1 | `parse_outcome` 不看 `stopped` | C-6 |
| 1 | `BackgroundSync.start` 不檢查 `running` | C-7 |
| 1 | 讀輸出改成在 `_finish` 裡才讀 | C-7（2000 行） |
| 1 | `stop` 每次都呼叫 `stop_group` | C-7 |
| 2 | `import_rows` 給了 `listed` 仍呼叫 `list_sessions` | C-8 |
| 2 | `build_rows` 用主執行緒的 `Index` | C-9 |
| 2 | `show(view=…)` 不做 `scroll_to` | C-11 |
| 2 | `show(view=…)` key 不在時移到 0 | C-11 |
| 2 | `rows_by_keys` 少一個時回傳找得到的部分 | C-11 |
| 3 | `start` 先 `begin_sync` 再 `reload` | C-12 |
| 3 | 被節流時也顯示指示 | C-13 |
| 3 | `_sync_rows_ready` 不比較就套用 | C-14（`show` 1 次） |
| 3 | `can_apply_sync` 不看焦點 | C-15 |
| 3 | 套用時一律 `preview=True` | C-16 |
| 3 | 套用時呼叫 `search(self.text)` | C-17（`find_in_agents` 被重起） |
| 3 | 拿掉 `action_delete` 的閘門 | C-18 |
| 3 | `wait_for_sync` 改看 `self.sync.running` | C-20′ |
| 3 | `_sync_rows_ready` 不比世代 | C-20′ |
| 3 | 閘門之後不做 `rows_by_keys` | C-18（少一個那一個） |
| 3 | `on_unmount` 停掉同步子行程 | C-20 |

## 11. 風險、沒驗到與待決

### 11.1 風險與沒驗到

- **R-1 真 Drive 的延遲沒量到**：
  - 驗收是用假 rclone 定的（G-1）；真 Drive 上同步更久，但這只影響「多久之後清單更新」，不影響可操作的時間。這是設計推論，真機上沒驗。
  - 真 Drive 上寫的動作等得更久：`WaitSync` 可以停止同步或取消動作。
  - 本人在 Mac 上的體感要合上之後才知道（Q-6）。
- **R-2 Textual 的執行緒限制**：
  - Textual 不是 thread-safe，所以本設計所有 UI 更新都在主執行緒，執行緒只經 `call_from_thread`。
  - `call_from_thread` 在 app 結束後會丟 `RuntimeError`，一律吞掉（`Run.read` 的前例）。
  - `asyncio.Event` 在 `on_mount` 建，只在主執行緒 set（`_sync_rows_ready` 是經 `call_from_thread` 呼叫的）。
  - 上面「離開時等 300 秒」是讀 textual 8.2.8（`app.py:2350` 的 `asyncio.run`、`worker.py` 的 `run_in_executor`）與 CPython 3.12 的 `THREAD_JOIN_TIMEOUT` 得到的，沒有實測。
- **R-3 離開後子行程還在跑**：
  - 這時馬上再開 TUI：`last-sync` 還沒寫，新的 TUI 會再起一個同步，兩個同步並行。結果正確（store 本來就是多行程設計），但會重複列表，批次下載會在影子目錄的鎖上排隊。
  - 這種重疊不改變任何資料的正確性。這是讀碼推論，沒有測試。
  - 要消除就要跨行程的同步鎖，本單不做（§2.2）。
- **R-4 套用時的主執行緒凍結**：
  - 3000 列的 `show()` 約 0.3～0.5 秒（推論，引自 #485 的 profile）。這段時間鍵盤會排隊。
  - 只在有變化時發生，而且不在打字時發生（§4.3）。
  - F6 會改善。C-22 的探針會量出實際值。
- **R-5 macOS 與 Linux 的差異**：
  - `start_new_session`、`killpg`、EPIPE、`flock` 在兩邊的語意相同。
  - macOS 上 Python 子行程的啟動較慢，但在背景，不影響可操作的時間。
  - Apple Terminal 不送 2026 查詢（#485 第 4 節），與本單無關。
  - 都沒有在 macOS 上驗；探針只在 worker（Linux）上跑。
- **R-6 其他行程的並行**：另一個終端在 TUI 背景同步時刪除 Session，仍有 §3.2 的復活風險，與現在相同。本單只消除同一個 TUI 之內的那一種。
- **R-7 第一次同步很久時**，清單是空的，使用者可能以為壞了；靠 §4.5 的訊息緩解。

### 11.2 待決（附建議）

| # | 問題 | 建議 | 誰決定 |
|---|---|---|---|
| Q-1 | `WaitSync` 要不要有「停止同步，馬上做」 | 要。停掉是安全的（§3.1），使用者在真 Drive 上可能要等好幾分鐘；停掉之後標題列會說「同步已停止」，不會悄悄地少同步 | PM |
| Q-2 | 要不要手動再同步的按鍵（例如 `ctrl+r`） | 這一單不做。介面已經留好（`begin_sync()`），之後要做就開一張 d；按鍵與按鍵列屬於行為，是 PM 的範圍 | PM |
| Q-3 | 同步中離開時，子行程要讓它跑完還是停掉 | 讓它跑完（與背景上傳器同一個做法，下一次啟動因此被節流而變快）；代價是 R-3 | PM |
| Q-4 | 同步完成但沒有變化時，要不要說「同步完成」 | 不說。啟動後每一次都出現一句沒有資訊的話，會讓真正有變化的那一句被忽略 | PM |
| Q-5 | 要不要照 issue 的備註另建 `openspec/changes/agora-tui-background-sync/`（proposal、specs、design.md） | 要進主 spec 的條文有（§9），所以建議建 change。這份設計搬進 change 的 `design.md`，或在 `design.md` 裡連到這裡。由 PM 決定並建檔（`proposal.md` 與 `specs/` 不在架構師可以改的路徑） | PM |
| Q-6 | 合上之後要不要請本人在 Mac 上用真 Drive 試一次（體感，以及 C-22 的探針換成真的 rclone 量一次） | 要，只看，不改設定。真機操作要本人授權，由 PMO 安排；不是合併的條件 | PMO／本人 |
| P-17 | `tui.py`（PR-3 之後約 2500 行）的資料面要不要切成 `tui_rows.py` | 之後再看；與 P-7、P-11 同一類 | 架構師設計、PM 排程 |

## 12. 與 F4／F2／F6 的關係與順序

| 項目 | 內容 | 與本單的相依 | 建議 |
|---|---|---|---|
| F1（#486）、F3（#487） | `import_rows` 的一次查詢；codex 清單的快取 | 已 merge；本單的可操作時間就是它們做完之後的 `reload` 時間 | — |
| F2（#488，已關） | 一次動作只重建一次表格 | 本單不改 `act()` 的重載；`show` 只加參數，預設行為不變 | — |
| **F5（本單）** | 背景同步 | — | **先做** |
| F4（未開單） | 重活（`reload` 的 `agora_rows`／`import_rows`）移出 event loop，先顯示「載入中」 | 不依賴本單；本單的 `build_rows`（可以在任何執行緒呼叫，C-9）與 `list_agent_sessions` 正是 F4 要的那一塊。F4 會把 `list_agent_sessions` 也移進執行緒，那時要處理 adapter 的快取與內文搜尋 worker 的並行（§8.4） | 本單之後 |
| F6（未開單） | 表格減量（gutter 不逐格更新、少重建） | 不依賴本單；會降低本單的 R-4 | F4 之後，或與 F4 並行 |

- 理由：
  - 本單驗收需要的只有 F1／F3（已完成）。
  - 先做 F4 的話，F4 自己就要碰 `start()`，與本單改的是同一段，兩者只能排隊。
  - 先做本單，F4 可以直接沿用 `build_rows` 與「執行緒只算列、主執行緒只畫」的規則（§6.4）。
- F4 與 F6 要開單的時候，各自另起設計，可能是 d 或 c，由 PM 判定。
