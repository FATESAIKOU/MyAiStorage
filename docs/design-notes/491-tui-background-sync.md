# #491 agora TUI：啟動先顯示本機清單，Drive 同步放背景（F5）

- 單：#491（MAS，層級 c；架構師必經，作者模式）。#485 調查的後續（F5）。
- 作者：專案架構師（作者模式，myaistorage-arch-w1，Claude Opus 5.5），2026-11-27。基準：main `8941cf8`。
- 第 2 版（2026-11-27）：依 Sonnet 審查（head db72a46，`REPORT-491-review.md`）修正。高-1、中-1～中-3、低-1～低-6 都已處理；PM 對 Q-1～Q-5 的決定寫在 §11.2。重點：拿掉「停止同步，馬上做」（中-2，Q-1），連帶拿掉 `stop()` 與 STOPPED；spawn 失敗改由 `begin_sync` 在主執行緒收尾（中-1）；Agora 頁 Enter 與 edit 的列改成游標那一列（高-1）；訊息列依結果決定字頭（中-3）；拿掉世代號（低-2）。
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
   - 理由是兩個讀碼得到的事實（§3.1）：
     - Textual 的 thread worker 跑在 asyncio 的預設 executor。`App.run` 用 `asyncio.run`，離開時要等 executor 的執行緒結束，上限 300 秒。所以在同步中按 `q`，畫面收起來了，行程卻還要等同步跑完才結束。
     - 現在 `Busy` 的 docstring 寫著，只剩第一次同步與授權還在執行緒裡跑；import 等 `act` 的動作都是子行程（continue／edit 例外：它們在 TUI 行程裡跑 `cli.main`，見 §3.2）。
   - TUI 離開時不必等子行程。`store` 本來就設計成多行程並行：mirror 原子寫入、index 每筆一個短交易、影子目錄有自己的鎖。
2. **啟動順序**改成 `setup → reload（本機清單，焦點在表格）→ 起背景同步`。同步多久，都不影響可操作的時間。可操作的時間只剩 `reload` 本身，也就是 F1／F3 之後的本機成本。驗收：假 rclone、遠端 3000 筆變動，可操作 < 3 秒（C-22）。
3. **同步中，寫的動作等同步結束；讀的操作照常**。讀的操作包括游標、換頁、勾選、篩選（標題或內文）、預覽、預覽搜尋。
   - 寫的動作一開始就先記下要處理的列（key），再進「等同步完成」視窗。視窗只有兩條路：等它完成（同步結束自己關掉），或 Esc 取消這個動作。沒有「停止同步，馬上做」（PM 決定，Q-1）：停掉的同步沒寫 `last-sync`，動作自己的節流同步會再做一次完整同步，並不會「馬上」（§3.2）。
   - 等完之後先套用新的列，再用 key 找回剛才記下的列。有任何一列已經不在，就不做，並且說明原因。
   - 為什麼不讓動作與同步並行，理由在 §3.2：
     - 同步在開頭就取好快照（遠端列表、`known`、`trashing`），之後才寫入。同步中刪除的 Session，若在這次同步要下載的清單裡，會被同步寫回 index 與 mirror（審查實測重現了短暫復活）。
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
   - 離線：沿用現在的「離線：只有本機資料」（紅）；本機有變化時說「本機清單有更新：…」，不說「同步完成」。
   - 失敗：「同步失敗：只有本機資料」，訊息列放錯誤；本機有變化時接在錯誤後面，不蓋掉它。
   - 被節流（距上次同步不到 300 秒）時，子行程照跑（`index_pending` 要跑，#222），但不顯示指示；這幾秒內按寫的動作，等候視窗的標題是「正在整理本機索引」。
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
  - 同一輪也記錄「同步完成並套用」的時間、套用時主執行緒的凍結時間、同步進行中的按鍵延遲，以及不同步時的基準，只報告，不設門檻（§6.3 探針）。
  - L=100×3 一併記錄，不設門檻。
- **G-2**：同步中與同步後，資料正確性不比現在差。不會有被刪的 Session 復活，不會有動作送錯列，不會同時跑兩個同步（同一個 TUI 之內）。
- **G-3**：同步的狀態看得到：進行中、離線、失敗、完成而且有變化。

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
| 中斷 | 做不到：`Drive._run` 是 `subprocess.run`，沒有取消的入口 | 做得到（對行程群組送 SIGINT，與 `Run` 相同；rclone 在同一個群組裡）。本單不提供停止（Q-1），只是留著這個可能 |
| sqlite 的執行緒限制 | sync 回傳的 `Index` 不能拿到主執行緒用 | 沒有這個問題 |
| adapter 的模組層快取（claude 的 `_LIST_CACHE` 等） | 與內文搜尋的 worker 在同一個行程，有並行風險 | 無關 |
| 與現有架構 | `Busy` 的 docstring：只剩第一次同步與授權還這樣跑 | 與 `Run`、`background`、`round_child`、`start_child` 同一個做法：子行程本體不經 `cli.main` |
| 成本 | 0 | 多一次 Python 啟動加 `import agora`（背景裡，不在可操作的路徑上；PR-1 要量） |

子行程被中斷（本單沒有按鍵會這樣做；例如別人對它的群組送了訊號）之後是安全的，理由是讀碼得到的：mirror 用 `copy_atomic`／`_publish` 寫入；`index.put` 是單筆交易，中斷就回滾；`last-sync` 最後才寫，沒寫就代表下次會再同步；影子目錄的鎖隨 fd 釋放；rclone 被中斷時留下的是它自己的部分檔，`_publish` 只搬 md5 相符的檔。這與現在指令模式按 Esc 中斷的處境相同。

### 3.2 同步中有動作時：等

這是讀碼得到的兩個風險。兩者都不是新的，只是現在被 `Busy` 視窗擋住，而改成背景之後一定會遇到：

- **刪掉的 Session 復活**（審查用假 rclone 重現了前三步；第 4 步的永久誤標是推論，沒有重現）：
  1. 背景同步列完遠端、算好 `changed`。只有 X 在 `changed` 裡（遠端比本機新）才有這個風險：沒有變的已知 Session 不會被 `put`。
  2. 使用者按 `d` 刪 X。delete 子行程把 X 從 mirror 與 index 拿掉，放進垃圾桶佇列。
  3. 背景同步接著下載 X、`index.put(X)`。`trashing` 是它在第 1 步取的快照，不含 X。於是 X 回到 index 與 mirror，TUI 會把已刪的 Session 列回清單，使用者可以對它動作。審查的場景裡，上傳器完成垃圾桶搬移後呼叫 `forget_local`（`store.py:3180`），X 約 8 秒後又消失，之後也沒有被標「雲端已刪」。
  4. 推論：若 `put` 發生在上傳器的 `forget_local` **之後**（大批次、X 排在 `changed` 的後段），X 會留在本機；下一次同步時 X 在本機 index 有、遠端沒有，就被標成「雲端已刪」（✗），看起來像是別台機器刪的。
- **動作的同步沒有被節流**：背景同步還沒寫下 `last-sync`，所以動作子行程的節流同步會跑一次完整同步。批次下載會在 `download-staging.lock` 上等背景同步的整個 `rclone copy`，結果動作反而比等背景同步跑完還慢（審查實測：400 筆變動，單獨同步 5.1 秒；兩個同時跑各約 7.8 秒）。
  - 同一個理由，也是拿掉「停止同步，馬上做」的理由（Q-1）：停掉的同步沒寫 `last-sync`，所以動作的節流同步一樣不會被節流，照樣做一次完整同步（3000 筆變動約 23 秒，#485），只是挪進 `Run` 視窗；continue／edit 甚至是在交出終端之後，連進度視窗都沒有。停止並不會「馬上做」，標題列卻會說同步已停止。要真的馬上做，得讓動作的子行程能跳過同步（例如 `--no-sync`），那是指令模式的介面改動，超出 §2.2，要另開單。

所以規則是：

| 動作 | 同步中 | 理由 |
|---|---|---|
| 游標、換頁、Tab、勾選、全選、篩選（標題或內文）、預覽、預覽搜尋 | 照常 | 只讀 index（WAL：讀者不等寫者）、mirror（原子替換：讀到的是舊的或新的一整份）、agent 自己的資料與 `cache/reading` |
| import、merge、delete、pull、push（`act`） | 等 | 上面兩點；delete 的風險最大 |
| continue、edit（`outside`） | 等 | 它們在 TUI 行程裡同步執行 `cli.main`（`self.outside()`，`tui.py:2054-2060`），一樣有自己的節流同步，也寫 index 與 mirror，風險與上面相同。另外，這段時間 event loop 停著：讀輸出的執行緒最後的 `call_from_thread` 會擋到終端交回來（`future.result()`），套用也只能等到那時 |
| 離開（`q`） | 照常，不等 | §3.1；子行程繼續把同步跑完（Q-3） |

**閘門放在哪裡**：在每個寫的動作的最前面，在確認視窗之前。順序是：

1. 照現在的規則算出要處理的列：用 `chosen_rows()` 的動作就用它的結果；只處理游標那一列的（Agora 頁的 Enter、`e`）就用 `current()`，**不用** `chosen_rows()`（它在有勾選時回傳勾選的列，不是游標那一列）。沒有列就照現在的訊息結束（不進閘門）。
2. 記下這些列的 key。
3. `await self.wait_for_sync()`：
   - 沒在同步：立刻回傳真。有待套用的結果，就先套用。
   - 在同步（或子行程已結束、列還在算）：顯示 `WaitSync` 視窗。回傳假（Esc）就結束，什麼都不起，勾選不動。
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
- **長時間占鎖**：`download-staging.lock` 本來就會被長時間持有（D-21）。本單不改；`WaitSync` 視窗讓使用者看得到在等，可以用 Esc 取消動作（不能停止同步，Q-1）。
- **`store.sync` 的重入**：同一個 TUI 內，`BackgroundSync.start()` 在執行中回傳 False，不起第二個（C-7）。跨行程（另一個終端的指令、離開後還在跑的同步子行程加上重新開的 TUI）照現在一樣可以並行。結果正確，但會重複下載（R-3）。本單不加跨行程鎖：那是同步演算法層級的改動，與 §2.2 衝突。
- **`throttle` 在背景下的語意不變**：子行程呼叫 `store.sync(paths, throttle=True)`，與現在的 `start` 相同。改變的只有「誰在等」。指示要不要顯示，由 TUI 在起子行程之前用 `store.sync_due(paths)` 判斷（C-1）；被節流時子行程照跑，因為 `index_pending` 要在節流的提早返回之前跑（#222）。
- 子行程的環境：`AGORA_CONFIG`／`AGORA_CACHE_DIR`／`AGORA_STATE_DIR` 明確設成 `self.paths` 的三個目錄，其餘沿用 `os.environ`（`AGORA_RCLONE`、`AGORA_UPLOAD`、`AGORA_FOLDER_NAME` 等）。這樣子行程同步的一定是畫面上這一份，不靠「app 的 paths 剛好等於 `from_env()`」（C-7）。

## 4. 重載時機與狀態保留（套用）

### 4.1 什麼時候算出新的列

子行程結束，`BackgroundSync` 的讀輸出執行緒經注入的 `post`（＝`app.call_from_thread`）在主執行緒呼叫 `on_done(outcome)`。子行程根本沒起來（spawn 丟 OSError）時，`start()` 把例外丟回 `begin_sync`，由 `begin_sync` 在主執行緒直接呼叫 `sync_done`（§6.3；不經 `post`：`call_from_thread` 在 app 自己的執行緒呼叫會丟 RuntimeError）。`AgoraApp` 接著：

1. 依結果更新 `self.status`（§5），重畫標題列；記下 `self._sync_outcome = outcome`，套用時的訊息依它決定（§5）。
2. 不論結果，都起一個 thread worker（`group="sync-rows"`，`exclusive=True`）：`build_rows(self.paths, self._listed, self.agents)`。
   - 它在這個執行緒裡開自己的 `store.Index`，用完就關。不碰 `self.index`、不碰任何 widget。
   - import 頁用 `self._listed`，也就是上次 `reload` 存下的各 agent `list_sessions()` 結果；不在這裡再列一次 agent。
   - 失敗、離線也要算：`index_pending` 與部分完成的下載，可能已經改了本機。
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
- `reload()` 被呼叫時（動作之後），丟掉 `_pending_rows`，停掉重試計時器：`reload` 讀的是同步之後的本機，已經包含它了。
- **不變式：`reload()` 只在 `_sync_idle` 已 set 時被呼叫**。讀碼：`reload` 的呼叫端只有 `start()`（`begin_sync` 之前，`tui.py:1177`）、`act()`（`:2019`）、continue（`:2115`）、edit（`:2200`），後三者都在閘門之後，閘門要等 `_sync_idle`（＝列已算好）。所以「算列期間發生 reload、晚到的舊列蓋掉 reload 的結果」走不到，本單不加世代號（第 1 版的 `_rows_gen` 拿掉，審查低-2）。日後加手動再同步（Q-2）或別的會在同步中 `reload` 的路徑時，要在那一張單把世代號補回來（§8.3 O）。
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
| 預覽區 | 游標的 key 與套用前相同、`self.shown_preview` 不是 `is_stale()`，而且（Agora 頁，或未匯入頁那一列的 `Row` 相等）：`preview=False`，預覽區完全不動（文字、捲動、搜尋標亮、`#pinned`）。否則 `preview=True`，照現在的流程重讀；有搜尋字時 `pending_note = "內容已更新"`（與 `reload` 相同，R5）。Agora 頁不比 `Row`：預覽的內容與 `#pinned`（dir、tags）都來自 mirror 的 `session.md`（`agora_preview`，`tui.py:534-542`），檔案換了 `is_stale()` 就是真；只有雲端欄變了（未上傳→已上傳）時預覽不必重讀（審查低-5）。未匯入頁的預覽是 agent 的最後一則訊息，沒有檔案可比（`path` 是 None，`is_stale()` 恆假），所以看 `Row`（`updated` 變了就重讀） |
| 預覽搜尋（`self.query`、`search_key`） | 游標的 key 沒變就不動（`show` 裡的 `track_row` 會照現行規則處理 key 變了的情形） |

### 4.5 第一次（mirror 是空的）

第一次授權之後，或 cache 被刪掉時，本機清單是空的，同步一做就是很久：

- 同步中而且 Agora 頁沒有列：訊息列說「正在同步 Drive，清單稍後出現」，不說現在那句「Agora 還沒有 Session…」。
- 套用之後還是沒有列（或同步沒有結果）：才說現在那句。

## 5. UI

| 狀態 | 標題列（`paint_bar` 的最後） | 訊息列（`#msg`） |
|---|---|---|
| 同步中，而且起的時候 `sync_due` 為真 | ` ⟳ 同步 Drive 中（N 秒）`，暗色（`#8a8a8a`）。每秒由 `set_interval(1.0)` 重畫標題列，結束就停 | 不動（第一次見 §4.5） |
| 同步中，但被節流 | 不顯示（子行程只做 `index_pending`，通常幾秒內結束） | 不動 |
| 完成（`done`），沒有變化 | 拿掉指示；`status = ""` | 不動（PM 決定，Q-4） |
| 完成，有變化 | 同上 | `sync_note(...)`：`同步完成：新增 a、更新 b、移除 c`（數字是 0 的那一段省略；勾選被拿掉時另加「；N 個勾選的列已不在」）。用 `say`，不是 failed |
| 離線（`offline`） | `status = "離線：只有本機資料"`（紅，沿用現行的字） | 沒有變化：不動。有變化：`本機清單有更新：新增 a…`（字頭不是「同步完成」） |
| 失敗（`failed`） | `status = "同步失敗：只有本機資料"`（紅） | 子行程一結束就 `say(outcome.detail, failed=True)`：錯誤那一行，最多 200 字。之後套用時有變化：`say(f"{outcome.detail}（{note}）", failed=True)`，`note` 是 `本機清單有更新：…`；錯誤不被蓋掉 |

- 套用時的訊息由 `sync_note(changes, dropped_marks, done=…, error=…)`（§6.2）一個函式決定：`done=outcome.state == DONE`，`error` 在 `failed` 時是 `outcome.detail`、其他是 `""`。只有 `done` 說「同步完成」，其他說「本機清單有更新」；有 `error` 時把錯誤放在前面、用 failed 樣式。沒有變化而且沒有被拿掉的勾選 → `""`，不說話（failed 的錯誤留在訊息列，§4.2 的「相同」分支本來就不說話）。
- 子行程被中斷（結束碼 130，或被訊號殺掉）不是一個獨立的狀態：本單沒有停止同步的按鍵（Q-1），這只會是外力造成的，歸 `failed`，`detail` 是「同步子行程被中斷（結束碼 …）」。
- `row_changes` 只算 Agora 頁：新增＝新的有、舊的沒有的 key；移除＝相反；更新＝兩邊都有但 `Row` 不同。import 頁的變化不報（它只會因為別台機器的 continue 或 import 而有細微變化，報了反而讓人困惑），但仍然會套用。
- **可不可以取消**：
  - 沒有獨立的「取消同步」按鍵，`WaitSync` 裡也沒有「停止同步」（PM 決定，Q-1；理由 §3.2）。
  - 寫的動作在 `WaitSync` 裡可以用 Esc 取消這個動作。
  - 離開不取消同步（PM 決定，Q-3）。
- `WaitSync` 視窗（`ModalScreen`，樣子與 `Busy` 相同，沒有選項）：
  - 標題：這一次同步顯示指示（`_sync_shown`）時是「背景同步還沒完成」；被節流時是「正在整理本機索引」（審查低-4：這時標題列沒有指示，視窗要自己說在等什麼）。
  - 下面一行是 spinner 加秒數，與 `Busy` 同一個樣子；再下一行是說明「同步結束就會繼續；Esc 取消這個動作」。
  - `_sync_idle` 被 set（列已算好）時自己關掉，回傳真。
  - Esc：回傳假，同步繼續。
  - Enter 沒有作用（沒有選項可選）。
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

- **介面**（`tui_sync.py`，只用標準函式庫，不 import `tui`、`textual`、`cli`、`store`、`sync_child`）：

  ```python
  DONE, OFFLINE, FAILED = "done", "offline", "failed"

  # 與 sync_child 的兩個常數逐字相同；另寫一份，不 import sync_child（它頂層 import store）。
  # 兩邊一致由 C-6 的測試鎖住（測試可以 import 兩個模組來比）。
  RESULT = "AGORA-SYNC "
  ERROR = "AGORA-SYNC-ERROR "

  @dataclass(frozen=True)
  class SyncOutcome:
      state: str                 # DONE | OFFLINE | FAILED
      detail: str = ""           # FAILED：說明的那一行（最多 200 字）；其他是 ""
      lines: tuple[str, ...] = ()   # 子行程的全部輸出（去掉行尾），給測試與日後的檢視

  def parse_outcome(code: int, lines: list[str]) -> SyncOutcome
      # 規則依序：
      # code == 0 且最後一個以 RESULT 開頭的行可解析成 dict：offline 為 True → OFFLINE，否則 DONE
      # code == 2 且有以 ERROR 開頭的行 → FAILED，detail＝最後那一行去掉前綴
      # code == 130 或 code < 0（被訊號結束）→ FAILED，detail＝f"同步子行程被中斷（結束碼 {code}）"
      # 其他（含 code == 0 卻沒有結果行、結果行不是 JSON）→ FAILED，detail＝f"同步子行程結束碼 {code}" 加上最後一行非空輸出（有的話，用「：」接）
      # detail 一律截到 200 字

  def child_argv() -> list[str]          # [sys.executable, "-m", "agora.sync_child"]

  def child_env(config: Path, cache: Path, state: Path) -> dict[str, str]
      # {**os.environ, "AGORA_CONFIG": str(config), "AGORA_CACHE_DIR": str(cache),
      #  "AGORA_STATE_DIR": str(state), "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}

  def spawn_child(argv: list[str], env: dict[str, str]) -> subprocess.Popen
      # 與 AgoraApp.spawn 相同的 Popen 參數：stdin=DEVNULL, stdout=PIPE, stderr=STDOUT, text=True,
      # encoding="utf-8", errors="replace", bufsize=1, start_new_session=True, env=env
      # start_new_session：子行程不在 TUI 的行程群組裡，終端的 ctrl+c 不會打到它（Q-3：離開後讓它跑完）

  class BackgroundSync:
      """One background sync child at a time; tells `on_done` once, from its reader thread through `post`."""

      def __init__(self, *, spawn: Callable[[], Any], post: Callable[..., Any],
                   on_done: Callable[[SyncOutcome], None],
                   clock: Callable[[], float] = time.monotonic): ...
          # spawn()：回傳一個像 Popen 的物件（有 stdout 可迭代、wait()、pid）。
          #   正式是 lambda: spawn_child(child_argv(), child_env(...))；測試給假的（DripProc 類）
          # post(fn, *args)：在 app 的執行緒呼叫 fn；正式是 app.call_from_thread。
          #   只從讀輸出的執行緒呼叫，絕不從呼叫 start() 的執行緒呼叫（call_from_thread 在 app 的執行緒會丟 RuntimeError）

      running: bool        # property：start 成功之後，到 _finish 跑完之前為 True
      started_at: float | None
      def elapsed(self) -> float      # running 時是 clock() - started_at，否則 0.0
      def start(self) -> bool
          # running → False，什麼都不做。
          # 否則 proc = spawn()；spawn 丟例外（OSError 等）→ 原樣往外丟，running 仍是 False，不起執行緒、不呼叫 post、不呼叫 on_done
          #   （收尾是呼叫端的事：begin_sync 在主執行緒直接呼叫 sync_done，§6.3）
          # 成功：running=True，started_at=clock()，起一條 daemon 執行緒讀 stdout 到 EOF，收集 lines（去掉行尾），然後 code = proc.wait()，
          #   再 post(self._finish, code, lines)；回傳 True。讀或 wait 丟例外 → code=2，lines 加一行「讀不到輸出：<e>」（照 Run.read 的 L6）
          # post 丟例外（app 已經不在）→ 吞掉
      def _finish(self, code: int, lines: list[str]) -> None
          # 在 app 的執行緒：running=False，outcome = parse_outcome(code, lines)，on_done(outcome)；只會一次
  ```

  - 第 1 版有 `stop()` 與 STOPPED，第 2 版拿掉（Q-1 不做停止，審查中-2）。日後要加停止時，照 `Run.action_stop`（`tui.py:904-918`）先看 `proc.poll()`：子行程已經結束（`_finish` 還在排隊）就不送訊號、不標停止（審查低-3）。
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
  - `test_tui_sync.py`：不用 Textual。`post` 預設用 `lambda fn, *a: fn(*a)` 同步呼叫（另有同執行緒替身，見下），假行程用 `threading.Event` 當閘門（照 `test_tui.py` 的 `DripProc`），測試等 `on_done` 用 `threading.Event`，加 `wait(timeout=10)`；不用 sleep。
    - `parse_outcome` 的每一列規則各一個（含 130 與負的結束碼）。
    - `tui_sync.RESULT == sync_child.RESULT`、`tui_sync.ERROR == sync_child.ERROR`（測試 import 兩邊來比）。
    - `start` 兩次只 spawn 一次。
    - `on_done` 只被呼叫一次。
    - `spawn` 丟 `OSError`：`start()` 把它丟出來，`running` 是 False，`post` 與 `on_done` 都沒被呼叫；之後再 `start()` 可以起（spawn 被呼叫第二次）。這個案例的 `post` 用「同執行緒替身」：記下建立它的執行緒，在同一條執行緒被呼叫就丟 `RuntimeError`（與 Textual 的 `call_from_thread` 相同），否則呼叫 fn。正常路徑的案例也換成這個替身跑一次，證明 `post` 只從讀輸出的執行緒呼叫。
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
  def sync_note(changes: RowChanges, dropped_marks: int, *, done: bool, error: str = "") -> str
      # 三個數字都是 0 而 dropped_marks 也是 0 → ""（不論 done、error）
      # 否則 head＝"同步完成" if done else "本機清單有更新"；
      #   body＝head + "：新增 2、更新 1"（0 的那一段省略；三個都是 0 時沒有「：…」）
      #   dropped_marks > 0 → body 後面加 "；{n} 個勾選的列已不在"
      # error 非空 → f"{error}（{body}）"；否則 body
      # 不 import tui_sync（PR-2 與 PR-1 並行）：done／error 由 PR-3 的呼叫端從 SyncOutcome 換算
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
  - `sync_note` 逐字比對：done 有變化、done 只有勾選被拿掉、全 0 回 `""`、`done=False` 的字頭是「本機清單有更新」、有 `error` 時是 `"<error>（本機清單有更新：新增 1）"`、有 `error` 而全 0 時是 `""`。
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
  SYNC_APPLY_RETRY = 0.5           # 延後的套用每隔多久再試一次（秒）；測試 monkeypatch 成 0.01
  SYNC_TICK = 1.0                  # 同步中標題列的秒數多久重畫一次

  class AgoraApp(App):
      def __init__(self, paths, cli, agents=None, check_setup=True, sync_on_start: bool | None = None): ...
          # sync_on_start 為 None 時等於 check_setup（正式路徑與現在一樣：check_setup 才同步）
          # self.sync = tui_sync.BackgroundSync(spawn=self.spawn_sync, post=self.call_from_thread,
          #                                     on_done=self.sync_done)
          # self._sync_shown = False         # 這一次同步要不要顯示指示（起的時候 sync_due）
          # self._sync_outcome: tui_sync.SyncOutcome | None = None   # 最近一次的結果；套用時的訊息用它（§5）
          # self._pending_rows: dict[str, list[Row]] | None = None
          # self._sync_idle = asyncio.Event()；在 on_mount 建（要在 app 的 loop 上）並 set（沒在同步）
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
          # 主執行緒。一次只一個：not self._sync_idle.is_set()（同步中，或列還在算）→ return False，什麼都不動
          # self._sync_shown = store.sync_due(self.paths)
          # 先 self._sync_idle.clear()、self.sync_settled.clear()，再 self.sync.start()：
          #   這個順序保證 _sync_idle 被 clear 之後，一定有一條路會走到 _sync_rows_ready（它在每個分支都 set）
          # start() 丟例外（spawn 失敗）→ self._sync_shown = False；
          #   self.sync_done(tui_sync.SyncOutcome(tui_sync.FAILED, f"叫不起同步子行程：{e}"[:200]))（直接呼叫，不經 post）；return True
          # start() 回 False（照上面的檢查不會發生；保險）→ self._sync_idle.set()；self.sync_settled.set()；return False
          # 成功：_sync_shown 時開 _sync_tick_timer（paint_bar）；paint_bar()；return True

      def sync_done(self, outcome: tui_sync.SyncOutcome) -> None:     # 主執行緒
          # self._sync_outcome = outcome；停 _sync_tick_timer；依 §5 設 self.status 與 say（failed 時 say(detail, failed=True)）；
          # paint_bar()；self.compute_sync_rows()

      @work(thread=True, exclusive=True, group="sync-rows")
      def compute_sync_rows(self) -> None:
          # rows = build_rows(self.paths, dict(self._listed), self.agents)（例外 → None）
          # self.call_from_thread(self._sync_rows_ready, rows)（app 不在 → 吞掉）
          # dict(self._listed) 在這個執行緒裡複製：_listed 只在 reload 換掉（整個換，不就地改），
          #   而 reload 在 _sync_idle set 之前不會跑（§4.3 的不變式），所以讀到的是同一份

      def _sync_rows_ready(self, rows: dict[str, list[Row]] | None) -> None:
          # try:
          #   None → say("同步後讀不到本機清單，請重開", failed=True)
          #   rows == self.rows → 不重畫
          #   否則 self._pending_rows = rows；self._try_apply_sync()；還沒套用就開 _apply_timer
          # finally（不論哪一個分支，含例外）：self._sync_idle.set()；沒有 pending 時 self.sync_settled.set()

      def can_apply_sync(self) -> bool:
          # self.screen is self.default_screen and getattr(self.focused, "id", None) not in ("filter", "search")

      def _try_apply_sync(self) -> bool:
          # 沒有 pending → True；不能套用 → False；否則 _apply_rows(pending)、清掉 pending、停 _apply_timer、
          # self.sync_settled.set()，回傳 True

      def _apply_rows(self, rows: dict[str, list[Row]]) -> None:
          # §4.4 的表；最後 self.show(view=view, preview=need_preview)，
          # 再 note = sync_note(row_changes(舊 agora, 新 agora), dropped,
          #                     done=o.state == tui_sync.DONE, error=o.detail if o.state == tui_sync.FAILED else "")
          #   （o = self._sync_outcome）；note 非空時 say(note, failed=bool(error))

      async def wait_for_sync(self) -> bool:
          # 判斷用 self._sync_idle，不用 self.sync.running：子行程結束到列算好之間（compute_sync_rows 還在跑）
          #   sync.running 已經是 False，但還不能讓動作開始，否則晚到的列會蓋掉動作之後 reload 的結果
          # _sync_idle 已 set：self._try_apply_sync()（在這裡一定可以套用：動作是在預設畫面上按的）；return True
          # 否則：ok = await self.push_screen_wait(WaitSync(self.sync, self._sync_idle, shown=self._sync_shown))
          #        ok 為 False → return False
          #        self._try_apply_sync()；return True
  ```

  ```python
  class WaitSync(ModalScreen):
      """An action waits here for the background sync (#491 §5). Dismisses True (go on) or False (Esc)."""
      BINDINGS = [Binding("escape", "dismiss(False)", "取消這個動作")]

      def __init__(self, sync: tui_sync.BackgroundSync, idle: asyncio.Event, *, shown: bool): ...
      # compose：標題（shown → 「背景同步還沒完成」；否則「正在整理本機索引」）、
      #          Static(id="spin")、Static("同步結束就會繼續；Esc 取消這個動作", classes="hint")
      # on_mount：set_interval(0.12, tick)（spinner 加 int(sync.elapsed()) 秒）；
      #   self.run_worker(self._watch(), exclusive=True)，其中 _watch 是 `await idle.wait(); self.dismiss(True)`
      # 沒有 OptionList，Enter 沒有綁定
      # CSS：與 Choose 等同一行置中（AgoraApp.CSS 的 `Choose, AskText, Busy, Run, Tell` 加上 WaitSync）
  ```

  - **閘門的接法**（§3.2）：下面八個入口各自在「算好列、判斷沒有列」之後插進閘門，然後才進原本的確認視窗或問題。
    - 用 `chosen_rows()` 的：`action_primary` 的未匯入頁、`action_merge`、`action_pull`（兩頁）、`action_push`、`action_delete`：

      ```python
      keys = [r.key for r in rows]          # rows = self.chosen_rows()（現有的那一行）
      if not await self.wait_for_sync():
          return
      rows = self.rows_by_keys(keys)
      if rows is None:
          self.say("同步後清單變了：有列已不在或被篩選掉，動作取消，請再確認", failed=True)
          return
      ```

    - 只處理游標那一列的：`action_primary` 的 Agora 頁（現在的 `row = self.current()` 那一行）與 `action_edit`。**不用** `chosen_rows()` 的結果：有勾選時它是勾選的列，不是游標那一列（審查高-1）：

      ```python
      row = self.current()
      if row is None:                       # action_edit 現有的檢查；action_primary 這裡補上（保險）
          return
      keys = [row.key]
      if not await self.wait_for_sync():
          return
      found = self.rows_by_keys(keys)
      if found is None:
          self.say("同步後清單變了：有列已不在或被篩選掉，動作取消，請再確認", failed=True)
          return
      row = found[0]
      ```

      `action_primary` 的 Agora 頁：開頭 `rows = self.chosen_rows()` 與「沒有列」的判斷照舊（現在就是這樣），之後的 `row = self.current()` 換成上面這段。
    - `action_pull` 在未匯入頁也走閘門：它不寫 index（拉進全文快取），但一個動作一條規則比較簡單；被節流時等的只是幾秒。
    - `act()` 本身不加閘門；閘門在呼叫它之前已經過了。這樣 `import` 一個 agent 一個指令的迴圈不會在每一輪都問一次。
  - `reload()`：開頭丟掉 `_pending_rows`、停掉 `_apply_timer`（§4.3）。不加世代號（§4.3 的不變式）。
  - 立刻試一次套用的地方：`filter_done`、`on_key` 收起篩選框的分支、`toggle_focus` 最後、`hide_preview_search` 與 `close_preview_search` 最後，各加一行 `self._try_apply_sync()`。
  - `paint_bar`：在 `status` 之前，`self.sync.running and self._sync_shown` 時加 ` ⟳ 同步 Drive 中（{int(self.sync.elapsed())} 秒）`，樣式 `#8a8a8a`。
  - `on_unmount`：停掉 `_sync_tick_timer`、`_apply_timer`。**不**停子行程。
  - `Busy` 的 docstring：改成只剩授權（§5）。
  - 現有的 `Busy("同步 Drive", …)` 那兩行與 `"連不上 Drive" in out` 的判斷刪掉（`OFFLINE_NOTE` 的判斷在 `sync_child`）。
- **測試**（`tests/unit/test_tui_background_sync.py`）：
  - 共通的 app：`tui.AgoraApp(paths, FakeCli(), agents=[…], check_setup=False, sync_on_start=True)`。
    - `app.spawn_sync` 換成回傳 `SyncProc` 的函式。`SyncProc` 照 `DripProc`：`release(lines, code)` 之前 `stdout` 會擋住；它記錄收到的訊號（C-20 要證明沒有任何訊號）。
    - `app.spawn` 照 `_spawn()` 記錄動作。
  - 要讓同步「改了本機」，測試在 `release` 之前，自己在 `paths` 的 mirror／index 寫入新的 session（`index.put`，與 `_index` 同一個寫法）。這等於子行程做的事，不必跑真的 sync。
  - 等待一律用事件：`await asyncio.wait_for(app.sync_settled.wait(), 5)`，之後 `await pilot.pause()`；不用 `pilot.pause(<秒數>)`。
  - 要測「套用被延後」時，斷言 `app._pending_rows is not None`，然後做那個會觸發套用的動作（Esc、Enter），再等 `sync_settled`。
  - `sync_due` 用 monkeypatch `store.sync_due` 決定真或假。
  - `tui.SYNC_APPLY_RETRY` 一律 monkeypatch 成 0.01：「關掉視窗之後的下一次重試」靠這個計時器（C-15），不改就每個案例多等 0.5 秒。
  - spawn 失敗（C-12′）：`app.spawn_sync` 換成丟 `OSError` 的函式。這個案例走的是 Textual 真的 `call_from_thread`（`run_test` 裡），所以若實作把失敗經 `post` 送出，就會撞到同執行緒的 RuntimeError、`_sync_idle` 永遠不 set，測試會逾時轉紅。
- **整合測試**（`tests/integration/test_tui_background_sync.py`，C-23）：
  - 真的 `spawn_sync`（子行程）、假 rclone、`FAKE_RCLONE_HOLD`、`AGORA_UPLOAD=inline`。
  - 本機 2 筆，遠端 12 筆，其中 2 筆的 md5 與本機不同。
  - `run_test` 之後等 `<hold>.waiting` 出現：在 `async` 裡用 `await asyncio.sleep(0.02)` 迴圈，最多 20 秒。這是等外部檔案，不是賭時間。這與 §6.1 閘門測試（`time.sleep(0.02)` 等 `.waiting`）是設計裡僅有的兩處輪詢，都只在跨行程的測試裡；假 rclone 自己等閘門檔也是每 0.02 秒看一次，那是替身的內部。其餘測試一律等事件。
  - 斷言：表格 2 列；`down` 讓游標到 1；標題列有「同步 Drive 中」；按 `d` 出現 `WaitSync`。
  - Esc：`app._last_spawned == []`。
  - 建出閘門檔，等 `sync_settled`。斷言：表格 12 列，訊息列是「同步完成：新增 10、更新 2」，`last-sync` 寫了。
- **探針**（`tests/perf/measure_tui_start.py`，C-22）：
  - 照 `measure_back_to_list.py` 的隔離做；假資料照 `REPORT-tui-slow.md` §2 的形狀：3000 筆，body 約 3 KB。
  - 本機 mirror／index 是舊的 3000 筆，遠端是 md5 全都不同的 3000 筆。
  - 正式的 `spawn_sync`，`sync_on_start=True`，`check_setup=False`。
  - 量四件事：
    - 從 `run_test` 進入，到「表格有 3000 列而且 `down` 之後 `cursor_row == 1`」；
    - 到 `sync_settled`；
    - 套用那一刻主執行緒的凍結時間：用 `set_interval(0.05)` 的 ticker，看最大的間隔；
    - 同步進行中（套用之前）的按鍵延遲：每 0.2 秒送一次 `down`／`up`，記「送出到 `cursor_row` 改變」的最大值（子行程與 rclone 在吃 CPU 時，畫面是不是還跟得上；審查低-5）。
  - 另外在同一份本機資料上用 `sync_on_start=False` 量第一項，當基準一起報告（審查在 main 上量到約 1.8～1.9 秒）。
  - 各 3 次取中位數，輸出 JSON。L=0 與 L=100×3 各一組。
  - 門檻只有第一項 < 3 秒（L=0）；不符就 exit 1。3 秒是為了分辨現況的 22 秒，不是 `reload` 本身的效能門檻（它比基準只寬約 1.6 倍，擋不住 `reload` 慢到 2.5 秒；那是 F4／F6 的事）。其餘三項只報告。
- **合上後 main**：啟動不再等同步；同步中寫的動作會等；其餘行為不變。

### 6.4 給實作者的共通事項

- 依賴方向照 §8.2：
  - `tui_sync` 不 import `tui`、`textual`、`cli`、`store`、`sync_child`。
  - `sync_child` 只 import `store`（與標準函式庫）。
  - `tui` 不 import `sync_child`（行程邊界，走 `tui_sync.child_argv()`）。
- 不改 `store.sync` 的步驟、順序與常數；PR-1 在 `store` 只動 §6.1 寫的兩處。
- 既有測試的斷言不改。現有的 TUI 測試都用 `check_setup=False`，`sync_on_start` 預設跟著它，所以不會起同步。有既有測試轉紅，先回報架構師，不要改斷言。
- UI 一律在主執行緒更新；執行緒只經 `call_from_thread`（`BackgroundSync` 用注入的 `post`）。執行緒裡不碰 `self.index`、不碰 widget。反過來，**在主執行緒上不呼叫 `call_from_thread`**（Textual 會丟 RuntimeError）：主執行緒上發生的失敗（例如 spawn 失敗）直接呼叫處理函式。
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
- **C-6** `tui_sync.parse_outcome` 照 §6.1 的規則表。`code == 0` 但沒有可解析的結果行是 `FAILED`；130 與負的結束碼是 `FAILED`，`detail` 說「被中斷」；`detail` 最多 200 字。`tui_sync.RESULT`／`ERROR` 與 `sync_child` 的逐字相同。
- **C-7** `BackgroundSync`：
  - 執行中再 `start()` 回傳 False，而且不 spawn。
  - `on_done` 對每一次 `start` 恰好被呼叫一次，而且是經 `post` 呼叫。
  - 讀輸出不依賴 `on_done` 的呼叫端：輸出超過 64 KB 也不會卡住，測試用假行程給 2000 行。
  - `spawn` 丟 `OSError`：`start()` 把它丟出來；`post` 與 `on_done` 都沒被呼叫，`running` 是 False，之後可以再 `start()`。
  - `post` 只從讀輸出的執行緒呼叫（同執行緒替身：在呼叫 `start()` 的執行緒被呼叫就丟 RuntimeError；正常路徑照樣得到 `on_done`）。
  - `child_env` 的三個 `AGORA_*` 目錄等於給的參數。

### PR-2

- **C-8** `import_rows(..., listed=L)` 與不給 `listed` 時，同一份 agent 資料得到的列相同，而且給了 `listed` 時不呼叫任何 agent 的 `list_sessions`。`list_agent_sessions` 一個 agent 失敗只影響它自己（U1）。
- **C-9** `build_rows` 在任何執行緒都能呼叫，用自己的 `Index`，結束時關掉連線，結果等於同一份資料上的 `agora_rows`＋`import_rows(listed=…)`。
- **C-10** `row_changes`／`sync_note` 照 §6.2。`sync_note` 只有 `done=True` 時字頭是「同步完成」；`error` 非空時錯誤在前、變化放在括號裡；全 0 而且沒有被拿掉的勾選時是 `""`。
- **C-11** `show()` 不帶新參數時行為不變。`show(view=…)` 的游標規則照 §6.2（key 在就跟著 key，不在就停在 `min(原列號, 列數-1)`），游標在畫面上的高度（`cursor_row - scroll_y`）不變（能捲到的範圍內）。`show(preview=False)` 不排預覽。`rows_by_keys` 照畫面順序，少一個就是 None。

### PR-3

- **C-12** `sync_on_start=True` 時，在背景同步還被擋住的情況下：表格已經有本機的列，焦點在表格，`down` 讓 `cursor_row` 由 0 變 1。`spawn_sync` 在 `reload` 之後才被呼叫（測試讓 `spawn_sync` 斷言 `app.rows["agora"]` 已經有列）。
- **C-12′** `spawn_sync` 丟 `OSError`：`status` 是「同步失敗：只有本機資料」，訊息列含「叫不起同步子行程」而且是 failed 樣式；`_sync_idle` 是 set（`sync_settled` 在 5 秒內 set）；之後按 `d` 不出現 `WaitSync`，直接進確認視窗。
- **C-13** 標題列：同步中而且起的時候 `sync_due` 為真，含 `同步 Drive 中`；`sync_due` 為假時不含。結束後照 §5 的表：`done` 時拿掉，`offline`／`failed` 時 `status` 各是那兩個字串，`failed` 時訊息列是 `detail` 而且是 failed 樣式。
- **C-14** 同步結束後：
  - 新的列與現在相同：`show` 被呼叫 0 次，訊息列不變（`failed` 時仍是 `detail`）。
  - 不同：`show` 恰好 1 次，游標在同一個 key，被拿掉的列的勾選消失、其他勾選留著，訊息列等於 `sync_note(...)`。
  - 組合（審查中-3）：`done`＋新增 1 → 「同步完成：新增 1」；`offline`＋新增 1 → 「本機清單有更新：新增 1」；`failed`（`detail`＝「X」）＋新增 1 → 「X（本機清單有更新：新增 1）」而且是 failed 樣式；`failed`＋沒有變化 → 訊息列仍是「X」。
- **C-15** 結束時有 `Confirm`／`Choose` 視窗開著，或焦點在 `#filter`、`#search`：不套用（`_pending_rows` 不是 None，表格的列沒變）。在篩選框繼續打字也不套用。之後下面任何一個都會套用：Enter 收起篩選框、Esc 收起篩選框、Tab 切出去、關掉預覽搜尋框、關掉視窗之後的下一次重試（`SYNC_APPLY_RETRY` 在測試裡是 0.01）。套用時篩選字不變。`reload()` 之後 `_pending_rows` 是 None。
- **C-16** 套用時，游標那一列的 key 沒變而且預覽檔沒換：`put_preview` 被呼叫 0 次，`shown_preview` 是同一個物件，預覽搜尋的 `query` 與 `which` 不變。Agora 頁只有那一列的雲端欄變了（`Row` 不相等、檔案沒換）也一樣是 0 次。檔案換了（測試改寫 mirror 的 `session.md`）：重讀一次；有搜尋字時，`#count` 那一行帶「內容已更新」。未匯入頁那一列的 `Row` 變了（`updated` 不同）：重讀一次。
- **C-17** 內文模式下有篩選字時，套用之後 `matches["agora"]` 含新列裡命中的 key，`find_in_agents` 不被重起（monkeypatch 計數 0）。
- **C-18** 同步中，下面八個動作都會出現 `WaitSync`，而且在同步結束之前 `app.spawn` 不被呼叫、`cli.calls`（`outside`）是空的：Agora 頁的 `d`、`m`、`e`、`p`、`P`、Enter，以及未匯入頁的 Enter、`p`。
  - 在 `WaitSync` 按 Esc：什麼都不起，勾選不變，同步繼續（`SyncProc` 沒有收到訊號）。
  - 被節流時（`sync_due` 為假）：`WaitSync` 的標題是「正在整理本機索引」；否則是「背景同步還沒完成」。
  - 等到同步結束：視窗自己關掉，先套用，再用 key 找回列：
    - 都在：確認視窗顯示的是新列的標題；
    - 少一個：不起子行程，訊息列是 §6.3 那一句。
  - 有勾選、游標在別列（審查高-1）：勾 A、B，游標停在 C，按 Enter（Agora 頁）與 `e`：同步結束後處理的是 C（`outside` 的 argv 是 C 的 key），不是 A。C 在同步後不見了：不起，訊息列是 §6.3 那一句，即使 A、B 都還在。
- **C-19** 沒在同步時，八個動作都不出現 `WaitSync`，起的指令與現在相同（既有測試全過就是證據，另加一個 `sync_on_start=True`、同步已結束之後按 `d` 的測試）。
- **C-20** 同步中按 `q`：app 結束；`SyncProc` 沒有收到任何訊號；`app._groups` 不含它的 pid。（測試在 `finally` 裡放行 `SyncProc`，不留卡住的執行緒。）
- **C-20′** 子行程已結束、列還在算（`compute_sync_rows` 被測試用 `threading.Event` 擋住）時按 `d`：出現 `WaitSync`，`app.spawn` 不被呼叫；放行之後才進確認視窗。
- **C-21** 第一次（mirror 是空的）：同步中訊息列是「正在同步 Drive，清單稍後出現」；套用之後還是空的，才是現在那句「Agora 還沒有 Session…」。
- **C-22**（探針，不進 CI）：§2.1 G-1 的量法，L=0 的中位數 < 3 秒。報告附 §6.3 探針的四項數字、不同步的基準，以及 L=100×3 的數字。
- **C-23**（整合）：§6.3 的整合測試。真的子行程被假 rclone 擋在列表時，清單可操作、`d` 等候、Esc 不起東西；放行之後列數與訊息列正確，`last-sync` 寫了。

## 8. 結構（模組、介面、依賴方向）

### 8.1 模組切割

| 模組 | 職責 | 不做什麼 |
|---|---|---|
| `store`（改兩處） | `sync_due`：節流的唯一判斷；`OFFLINE_NOTE`：離線那一行的開頭 | 不知道 TUI，不知道子行程 |
| `sync_child`（新） | 子行程本體：跑一次節流同步，用最後一行說結果 | 不畫畫面，不解讀結果，不經 `cli.main`（不跑 `recover_pending`、不印提醒，與 `background` 同理） |
| `tui_sync`（新） | 起子行程、讀輸出、只通知一次、把結束碼與輸出解讀成 `SyncOutcome` | 不 import Textual，不碰列、不碰畫面、不決定何時套用 |
| `tui`（改） | 啟動順序、標題列、`WaitSync`、閘門、算列（`build_rows`）、套用與延後、保留狀態 | 不跑同步本身，不解讀子行程的輸出格式 |

### 8.2 依賴方向（新增的邊以 ＋ 標示；沒有環）

```
store       → header；lazy: background                         （不變）
sync_child  → store                                            ＋（新模組）
tui_sync    → （標準函式庫）                                   ＋（新模組）
tui         → cache, header, store, agents.agy, tui_sync＋；lazy: agents.base, cli
```

- 行程邊界：`tui_sync` 以 `python -m agora.sync_child` 起子行程，不 import 它（結果行的兩個前綴在兩邊各寫一份，由 C-6 的測試鎖住）。與 `background`、`round_child` 的做法相同。
- `tui_sync` 不 import `store`：它需要的三個目錄由 `tui` 用參數傳進來，所以它的測試不必建 `Paths`。

### 8.3 SOLID

- **S**：同步本身（`store`）、子行程協議（`sync_child`／`parse_outcome`）、行程生命週期（`BackgroundSync`）、畫面（`tui`）各有一個變更理由。結果行的格式只在 `sync_child` 與 `parse_outcome` 兩處，由 C-3／C-6 一起鎖住。
- **O**：以後加手動再同步（Q-2），是再呼叫 `begin_sync()`，但那時 `reload` 可能在同步中被呼叫，要補回世代號（§4.3 的不變式）。加別的結果種類（例如日後的停止），是在 `parse_outcome` 與 §5 的表各加一列，`BackgroundSync` 只多一個 `stop()`（照 `Run.action_stop` 先看 `poll()`，§6.1）。
- **L**：`spawn` 回傳的是「像 Popen 的物件」，正式的 `Popen` 與測試的 `SyncProc` 都滿足 `stdout` 可迭代、`wait()`、`pid`。這與 `Run` 對 `FakeProc` 的要求相同。
- **I**：`BackgroundSync` 只要求三個可呼叫物件（`spawn`、`post`、`on_done`），不要求整個 App。
- **D**：`BackgroundSync` 依賴注入的 `post`，不依賴 Textual；`tui` 依賴 `tui_sync` 的抽象（`SyncOutcome`），不依賴子行程的輸出格式。
- **不符合的地方**：
  - `tui.py` 會從 2281 行長到約 2450 行（第 2 版拿掉停止與世代號，比第 1 版估的 2500 少）。理由：閘門、套用與延後都要碰 `AgoraApp` 的狀態（`rows`、`marked`、`cache`、焦點、計時器），拆出去只會多一層傳參。
  - 審查「交架構師」問要不要在 PR-3 就切出 `tui_screens`／`tui_apply`。**決定：不在 PR-3 切**，理由：
    - `WaitSync` 約 30 行，與 `Busy`、`Run`、`Choose`、`Tell` 同一類的視窗；只把它一個搬出去，視窗就分在兩個檔案，反而不一致。要搬就是所有視窗一起搬，那是純搬移的重構。
    - 套用與延後（`_sync_rows_ready`、`_try_apply_sync`、`_apply_rows`、`can_apply_sync`）讀寫的是 `AgoraApp` 的列、勾選、快取、焦點與計時器；切成另一個模組要嘛傳整個 app（沒有隔離到任何東西），要嘛先把這些狀態抽成物件，那是 PR-3 範圍外的重構。能抽的純邏輯（`row_changes`、`sync_note`、`build_rows`）已經是模組層函式，可以單獨測。
    - 行為改動與純搬移放在同一個 PR，審查者分不出哪些行是搬的、哪些是改的。
  - 與 P-7、P-11 同類，記在待決 P-17（§11）：日後另開一張純搬移的 d，把「列的資料面」（`Row`、`agora_rows`、`import_rows`、`build_rows`、`row_changes`、`sync_note`、`filtered`）切成 `tui_rows.py`，視窗類切成 `tui_screens.py`。建議排在 PR-3 合上之後（不與 PR-3 並行，免得衝突）。
  - 審查也問結果行的兩個前綴要不要放進第三個只有常數的模組。**決定：不要**。兩個字串為了共用多一個模組、多兩條依賴邊，比重複兩行的成本高；兩邊一致由 C-6 的測試鎖住，任何一邊改了測試就轉紅。

### 8.4 被捨棄的方案

- **背景執行緒（`@work(thread=True)`）**：§3.1。最致命的是同步中離開時要等最多 300 秒，而且沒辦法中斷。
- **動作與同步並行**：§3.2。刪除的 Session 會復活，而且動作的同步反而變慢。
- **動作直接取消背景同步再做**：按 `d` 就悄悄停掉同步，使用者不知道同步沒做完，下一次啟動前清單都是舊的。
- **`WaitSync` 裡提供「停止同步，馬上做」**（第 1 版的設計，第 2 版拿掉，PM 決定 Q-1）：停掉的同步沒寫 `last-sync`，動作自己的節流同步照樣做一次完整同步，所以並不會馬上做，只是把等待挪進 `Run` 視窗（continue／edit 甚至沒有進度視窗），標題列卻說同步已停止（§3.2）。還要多一條停止的路徑（`stop()`、STOPPED、`poll()` 的競態，審查低-3）與它的測試。要真的馬上做，得讓動作的子行程跳過同步（`--no-sync` 之類），那是指令模式的介面改動，要另開單。
- **閘門放在起子行程之前（問完問題之後）**：使用者是對同步前的畫面回答確認的。
- **用 md5 集合或 index 的計數判斷有沒有變化**：漏掉雲端欄與「未上傳」這類不在 md5 裡的變化，還要多一個查詢。直接比 `Row` 最準，而且 3000 列的比較是毫秒級（推論，沒有量）。
- **套用時重算 `import_rows`（重新列 agent）**：會在執行緒裡碰 adapter 的模組層快取，與內文搜尋的 worker 並行（§3.1）。sync 不改 agent 的資料，用上次的清單就夠。
- **加跨行程的同步鎖**：會讓另一個終端的指令在 TUI 背景同步時卡住，而且是同步演算法層級的改動（§2.2）。
- **新的公開指令 `agora sync`**：要進 spec 的指令清單、要說明、要處理參數；這裡只需要一個內部入口，`background` 與 `round_child` 已經有前例。

## 9. 主 spec 要補的地方（PM 撰寫；架構師只看介面能否一致表達）

`openspec/specs/interactive-mode/spec.md` 建議新增一個 Requirement「啟動與背景同步」。草稿：

- 互動模式啟動時 MUST 先顯示本機清單，而且清單 MUST 在不等 Drive 的情況下可以操作（移動游標、換頁、勾選、篩選、預覽）；距上次同步超過節流時間時，MUST 在背景同步一次。
- 背景同步中，標題列 MUST 顯示「同步 Drive 中」；結束時 MUST 依結果顯示：完成時不顯示；離線時「離線：只有本機資料」；失敗時「同步失敗：只有本機資料」，並且說明原因。清單有變化時 MUST 說新增、更新、移除各幾個；同步沒有完成（離線、失敗）時 MUST NOT 說「同步完成」，而且失敗的原因 MUST NOT 被蓋掉。
- 同步完成後清單有變化時，MUST 保留游標所在的 Session、篩選字與模式、仍在清單裡的勾選；預覽的 Session 沒有變時，MUST NOT 重讀預覽。有視窗開著、或正在篩選框或預覽搜尋框輸入時，MUST 等它們結束才更新清單。
- 背景同步中，會啟動指令或交出終端的動作（import、merge、delete、pull、push、continue、edit）MUST 等同步結束才執行，等的時候 MUST 讓使用者可以取消動作。等完之後，若動作要處理的 Session 有任何一個已不在清單，MUST NOT 執行，並且說明。
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
| 1 | `parse_outcome` 把 130 當 `DONE` | C-6 |
| 1 | `tui_sync.RESULT` 少一個空白 | C-6（與 `sync_child` 比對） |
| 1 | `BackgroundSync.start` 不檢查 `running` | C-7 |
| 1 | 讀輸出改成在 `_finish` 裡才讀 | C-7（2000 行） |
| 1 | spawn 失敗時改成 `post(on_done, FAILED…)` | C-7（同執行緒替身丟 RuntimeError，`on_done` 沒被呼叫；`start()` 沒丟例外） |
| 2 | `import_rows` 給了 `listed` 仍呼叫 `list_sessions` | C-8 |
| 2 | `build_rows` 用主執行緒的 `Index` | C-9 |
| 2 | `show(view=…)` 不做 `scroll_to` | C-11 |
| 2 | `show(view=…)` key 不在時移到 0 | C-11 |
| 2 | `rows_by_keys` 少一個時回傳找得到的部分 | C-11 |
| 2 | `sync_note` 不看 `done`，一律「同步完成」 | C-10 |
| 2 | `sync_note` 有 `error` 時只回傳變化 | C-10 |
| 3 | `start` 先 `begin_sync` 再 `reload` | C-12 |
| 3 | `begin_sync` 在 `start()` 之後才 `_sync_idle.clear()`，或 spawn 失敗時經 `call_from_thread` 送 `sync_done` | C-12′（`sync_settled` 逾時、`d` 出現 `WaitSync`） |
| 3 | 被節流時也顯示指示 | C-13 |
| 3 | `_sync_rows_ready` 不比較就套用 | C-14（`show` 1 次） |
| 3 | `can_apply_sync` 不看焦點 | C-15 |
| 3 | 套用時一律 `preview=True` | C-16 |
| 3 | Agora 頁的預覽也比 `Row`（雲端欄變了就重讀） | C-16（只有雲端欄變的那一個） |
| 3 | `_apply_rows` 的訊息一律用 `done=True`，或 failed 時直接 `say(note)` | C-14（組合案例） |
| 3 | 套用時呼叫 `search(self.text)` | C-17（`find_in_agents` 被重起） |
| 3 | 拿掉 `action_delete` 的閘門 | C-18 |
| 3 | `wait_for_sync` 改看 `self.sync.running` | C-20′ |
| 3 | 閘門之後不做 `rows_by_keys` | C-18（少一個那一個） |
| 3 | Agora 頁 Enter 的閘門用 `chosen_rows()`、閘門後 `row = rows[0]` | C-18（有勾選、游標在別列） |
| 3 | `WaitSync` 的標題不看 `shown` | C-18（被節流那一個） |
| 3 | `on_unmount` 停掉同步子行程 | C-20 |

## 11. 風險、沒驗到與待決

### 11.1 風險與沒驗到

- **R-1 真 Drive 的延遲沒量到**：
  - 驗收是用假 rclone 定的（G-1）；真 Drive 上同步更久，但這只影響「多久之後清單更新」，不影響可操作的時間。這是設計推論，真機上沒驗。
  - 真 Drive 上寫的動作等得更久：`WaitSync` 只能取消動作，不能停止同步（Q-1）。真 Drive 上要等好幾分鐘時，使用者可以 Esc 之後先做讀的操作，等標題列的指示消失再做。合上後本人在 Mac 上看一次（Q-6），若真的太久，另開單做 `--no-sync`（§3.2）。
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
| Q-1 | `WaitSync` 要不要有「停止同步，馬上做」 | **不要（PM 決定，2026-11-27；架構師建議相同）**。第 1 版建議要；審查中-2 指出停掉的同步沒寫 `last-sync`，動作自己的節流同步會再做一次完整同步（3000 筆變動約 23 秒），停止並不會馬上做（§3.2）。三個選項：(a) 保留並在視窗與標題列如實說「動作會自己再同步一次」；(b) 拿掉；(c) 讓動作的子行程能跳過同步（`--no-sync`）。架構師建議 (b)：(a) 保留的是一個「看起來能省時間、實際上不能」的選項，還要多一條停止路徑（`stop()`、STOPPED、`poll()` 競態）；(c) 是指令模式的介面改動，超出 §2.2，而且跳過同步的動作會用舊的本機資料做（例如 continue 一個別台更新過的 Session），要另外想清楚。若真 Drive 上等候太久（Q-6 看），再另開單評估 (c) | PM（已決） |
| Q-2 | 要不要手動再同步的按鍵（例如 `ctrl+r`） | **這一單不做（PM 決定，採建議）**。介面已經留好（`begin_sync()`），之後要做就開一張 d，那時要補世代號（§4.3、§8.3 O）；按鍵與按鍵列屬於行為，是 PM 的範圍 | PM（已決） |
| Q-3 | 同步中離開時，子行程要讓它跑完還是停掉 | **讓它跑完（PM 決定，採建議）**：與背景上傳器同一個做法，下一次啟動因此被節流而變快；代價是 R-3 | PM（已決） |
| Q-4 | 同步完成但沒有變化時，要不要說「同步完成」 | **不說（PM 決定，採建議）**。啟動後每一次都出現一句沒有資訊的話，會讓真正有變化的那一句被忽略 | PM（已決） |
| Q-5 | 要不要照 issue 的備註另建 `openspec/changes/agora-tui-background-sync/`（proposal、specs、design.md） | **主 spec 的條文由 PM 另建 change（PM 決定，採建議）**。§9 的草稿保留在這裡，給那個 change 的 `specs/` 參考；`proposal.md` 與 `specs/` 不在架構師可以改的路徑 | PM（已決） |
| Q-6 | 合上之後要不要請本人在 Mac 上用真 Drive 試一次（體感，以及 C-22 的探針換成真的 rclone 量一次） | 要，只看，不改設定。真機操作要本人授權，由 PMO 安排；不是合併的條件 | PMO／本人（待決） |
| P-17 | `tui.py`（PR-3 之後約 2450 行）的資料面與視窗類要不要切成 `tui_rows.py`／`tui_screens.py` | 不在 PR-3 切（§8.3）；PR-3 合上之後另開一張純搬移的 d；與 P-7、P-11 同一類 | 架構師設計、PM 排程 |

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
