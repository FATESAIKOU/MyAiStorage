# Review：design 5.10 快取與 sync，以及 Textual 版互動模式的執行緒安全

2026-10-02，review。對象：`4dc53d0`（`src/agora/cache.py`、`agora cache agora|local`、`agora sync`／`Drive.upload_tree`、互動模式的 `r`／`s`），以及 `822fc11`（tui.py 改成 Textual）。只提意見，沒有改程式。依照指示，沒有跑整合測試、沒有碰 Drive、沒有讀任何真實的 Session，也沒有執行不帶參數的 `agora`。實測只用 repo 裡的**假 rclone**（`tests/fakes/fake_rclone.py`），所有的目錄都放在 scratchpad 裡。

## 結論

| # | 嚴重度 | 一句話 |
|---|---|---|
| K1 | **High（已實測）** | `agora sync` **不會先拉**，就把整個本機鏡像 `rclone copy` 回 `sessions/`。所以，**另一台機器刪掉（移到垃圾桶）的 Session，會被這台機器整個傳回去而復活**；這台機器鏡像裡留著的舊 raw、下載到一半的 `*.partial`、`.DS_Store` 也都會一起上傳 |
| K2 | Medium | `upload_tree` 的排除清單只有 `.files-from` 和 `.bad/**`，其中 `.bad` 根本不在鏡像裡（它在 state 底下），而真正會出現在鏡像裡的 `*.partial`、`.*`、`*.tmp` 都**沒有**被排除 |
| K3 | Medium | Busy 用 `contextlib.redirect_stdout／redirect_stderr` 在**工作執行緒**裡把**整個程式共用**的 `sys.stdout／sys.stderr` 換掉。這段時間裡，其他執行緒（預覽、內文搜尋）印出來的東西都會混進 Busy 的輸出；如果兩個 redirect 重疊，還原的順序錯了，`sys.stdout` 就會一直指向一個已經沒人在讀的 StringIO |
| K4 | Medium | `cache.local_reading` 的暫存檔名是固定的 `<id>.tmp`。互動模式的預覽執行緒和 `agora cache local`（Busy 的執行緒）可能**同時**寫同一個 session，`os.replace` 就會因為檔案被另一個執行緒搬走了而失敗；轉接器的模組層級快取（opencode 的 `_list_cache`、claude 的 `_LIST_CACHE`）也會被兩個執行緒同時改動 |
| K5 | Medium | `refresh_agora` 只接 `StoreError`。`fetch_raw` 丟出其他的例外（HeaderError、OSError，或者 raw 欄位格式不對造成的 KeyError／TypeError）時，**整個迴圈就停了**，和 design「某一個失敗照樣做下一個」不一致 |
| K6～K11 | Low | 過時判斷的精度、刪掉的 session 留在快取、快取檔的權限、sync 的 exit code、`cache agora` 的總容量、Textual 的幾個小地方 |

**過時的判斷**（`local_reading`）本身是對的：快取檔的 mtime 設成 session 自己的更新時間，session 比較新就重寫，`updated_at` 沒有給的時候就直接用快取。互動模式和 `refresh_local` 兩邊都**有**傳 `updated_at`，所以不會出現「永遠不更新」的情況。

**`cache agora` 的失敗處理**：大方向是對的（每個來源都印出 n/總數、累計失敗的數量、有失敗時 exit 2），只有 K5 這個缺口。

## K1（High，已實測）：`agora sync` 會讓別台機器刪掉的 Session 復活

`cache.sync_up` 的流程是：先 `push_outbox`，再 `drive.upload_tree(paths.mirror)`，也就是 `rclone copy <cache>/sessions gdrive:sessions --exclude .files-from --exclude ".bad/**"`。**它不會先同步（拉）一次**，所以本機鏡像裡還留著的東西，不論 Drive 上現在還有沒有，都會被傳上去。

我用假的 rclone 實測（全部都在 scratchpad 裡）：先建一個 Session，把它推上去，再拉回鏡像（也抓了 raw）；接著在「遠端」把 `sessions/<ULID>/` 刪掉，模擬另一台機器做了 `agora delete`；然後在這台機器上執行 `cache.sync_up()`。結果是：

```
deleted session after agora sync: RESURRECTED with ['raw-000000000000.json.partial', 'raw-3ad65749fb85.json', 'session.md']
junk on Drive: ['.DS_Store']
```

- **刪除被撤銷了**：真的 Drive 上，`delete` 會把資料夾移到垃圾桶，而 `sync` 會在 `sessions/` 底下**重新建一個同名的資料夾**。結果是那個 Session 在所有機器上都回來了，垃圾桶裡還有一份。使用者在 A 機器刪除，在 B 機器按一次 `s`，就白刪了。design 5.10 接受的是「**覆蓋**別台機器較新的版本」，並沒有提到「**撤銷**別台機器的刪除」。
- **舊的 raw 也會被傳回去**：另一台機器重新匯入之後，會刪掉舊的 raw（S1 的第 4 步），但這台機器的鏡像裡，`fetch_raw` 抓過的舊 raw 還在，`sync` 就會把它傳回去（變成沒有人用的垃圾檔）。
- **半成品和系統檔也會被傳上去**：下載中斷時留下的 `*.partial`、macOS Finder 產生的 `.DS_Store`（見 K2）。

**建議**（大約 10 行）：

1. `sync_up` 先呼叫 `drive.list_sessions()`，取得遠端**現有的** ULID 集合。
2. 只上傳「遠端還在」的 ULID 資料夾（覆蓋，符合 design 的語意）。只在本機鏡像裡、遠端已經不在的 ULID，**不要**上傳，並且印出「agora:X 在 Drive 上已經刪除，沒有寫回」（新建的 Session 本來就會經過 outbox，而 outbox 已經在前一步推上去了，所以不會漏掉）。最後再做一次一般的 sync，讓本機的鏡像也跟著清掉這些資料夾。
3. 每一個 ULID 資料夾裡，只上傳 `session.md`，以及 `session.md` 標頭裡 `agora.raw.file` 指到的那**一個** raw。其他的舊 raw、`*.partial`、`.*` 都不要上傳。這樣也就不需要依賴 K2 的排除清單了。
4. 單元測試：用假的 rclone 鎖住「遠端刪掉的不會被傳回去」「舊的 raw 不會被傳回去」「`*.partial` 和 `.DS_Store` 不會被傳上去」。
5. design 5.10 補一句：「`sync` 不會撤銷其他機器的刪除；只會覆蓋 Drive 上**還在**的 Session」。

## K2（Medium）：排除清單

- `.bad/**`：`.bad` 是 `<state>/outbox/.bad` 和 `<state>/pending/.bad`，**不在**鏡像（`<cache>/sessions`）裡，所以這條排除規則沒有作用。
- 鏡像裡實際上可能出現的東西：`session.md`、`raw-*.json`（目前的，以及舊的）、rclone 下載中斷時留下的 `*.partial`（rclone 預設會先寫到 `.partial` 再改名；只要中途被殺掉，它就會留下來）、`.files-from`（已經排除）、`.DS_Store`、編輯器的暫存檔。
- 如果採用 K1 第 3 點的「白名單」（只上傳 `session.md` 和它指到的 raw），這一條就自然解決了；如果繼續用 `rclone copy`，至少要加上 `--exclude ".*"`、`--exclude "*.partial"`、`--exclude "*.tmp"`。

## K3（Medium）：Busy 的 redirect_stdout 會影響其他執行緒

- `contextlib.redirect_stdout` 換掉的是**整個程式共用**的 `sys.stdout`，不是只有這個執行緒的。所以在 Busy 的工作執行緒還在跑的時候：
  - 預覽執行緒（`load_full` → `cache.local_reading` → 轉接器的 export）和內文搜尋執行緒（`find_in_agents`）印出的任何東西（例如 opencode 的 `_warn_schema`、`[agora]` 警告），都會**混進** Busy 的輸出，`Tell` 最後顯示的就會是兩件事的輸出混在一起。
  - 如果兩段 redirect 重疊（例如以後有兩個 Busy，或者有其他程式碼也用了 redirect），每一段在結束時都會還原成它**開始時**看到的那個物件。只要結束的順序和開始的順序不一樣，`sys.stdout` 最後就會停在一個已經沒人在讀的 StringIO 上，之後所有的 `print` 都會消失，什麼訊息都不會有。
- Textual 畫畫面用的是它自己的 driver（寫到終端機），不是 `sys.stdout`，所以這不會弄壞畫面。但是，由 `cli.main` 啟動、**沒有**擷取輸出的子行程，會直接寫到 fd 1／2，也就是終端機，畫面就會被弄亂。目前 rclone、opencode、claude 的呼叫都有 `capture_output`，`_summary_dir` 的 `git` 也有 `-q`，所以實際上沒有問題；以後新增子行程呼叫時要注意這一點。
- **建議**：不要用 `redirect_stdout`。改成讓 `cli.main`／`store._warn` 等函式接受一個「輸出要寫到哪裡」的參數，或者用 `contextvars.ContextVar` 存「目前的輸出目的地」，`_warn`／`print` 都改寫到它那裡；`contextvars` 在 `threading` 裡是每個執行緒各自一份。如果暫時還要留著 redirect，至少要確保同一時間只會有一個 Busy（用 `exclusive=True, group="busy"`），並且在 Busy 執行期間，暫停預覽和搜尋執行緒裡的 `print`。

## K4（Medium）：多個執行緒同時寫同一個快取

- `local_reading` 寫入時用的暫存檔是 `path.with_suffix(".tmp")`，名字是固定的。如果互動模式的預覽（`load_full`，在一個執行緒裡）和 `agora cache local`（Busy，在另一個執行緒裡）剛好處理同一個 session：A 寫好 tmp，B 把 tmp 截斷並重寫，A 用 `os.replace` 搬走，接著 B 的 `os.replace` 就會丟出 `FileNotFoundError`。那個 session 在 `refresh_local` 裡會被計成「失敗」，預覽則會顯示「讀不到這個 session」。**建議**：用 `tempfile.NamedTemporaryFile(dir=path.parent, delete=False, suffix=".tmp")` 產生唯一的檔名，再 `os.replace`。
- 轉接器的模組層級快取：claude 的 `_LIST_CACHE`（`_forget_other_keys` 會一邊走訪一邊刪除），以及 opencode 的 `_list_cache`（全域的 tuple）。`refresh_local`（Busy 執行緒）和 `reload()`（主執行緒）可能同時呼叫 `list_sessions`，claude 那邊可能會出現 `RuntimeError: dictionary changed size during iteration`。**建議**：在 `list_sessions` 外面包一個模組層級的 `threading.Lock`；或者 `_forget_other_keys` 改成先複製 key 的清單（現在已經是 `[k for k in _LIST_CACHE …]`，可以減少問題，但另一個執行緒同時在寫，還是不安全）。

## K5（Medium）：`refresh_agora` 只接 StoreError

`fetch_raw` 可能丟出的例外，不只有 `StoreError`：走 N8 的重抓路徑時，重新解析 session.md 可能會丟出 `HeaderError`；磁碟滿了會丟出 `OSError`；raw 欄位的格式不對時，會丟出 `KeyError`／`TypeError`。這些都會跳出迴圈，被 `cli.main` 的 catch-all 接住，結果是 exit 2，**後面的 Session 都不會被處理**。**建議**：和 `refresh_local` 一樣接 `Exception`，計成失敗，然後繼續下一個。另外，連續好幾個都是「連不上 Drive」的時候，可以提前結束，並且提示「看起來是離線」，不必再跑完 N 次 timeout。

## 其他（Low）

| # | 問題 | 建議 |
|---|---|---|
| K6 | 過時判斷的精度：`Listed.updated_at` 只精確到**秒**（opencode 的 `_iso`、claude 的 `strftime("%S")`），而快取檔的 mtime 就設成那一秒。如果 session 在快取寫好之後、**同一秒內**又更新了，就會被誤判成「沒有過時」 | 比較時改用 `>`（相同的秒數就重寫，最多只是多寫一次），或者讓 `Listed` 帶一個 float 的 mtime |
| K7 | agent 那邊已經刪掉的 session，它在 `reading/<agent>/` 裡的快取會**一直留著**，`search_cached` 也會繼續把它當成搜尋結果（互動模式會顯示一個已經不存在的 session） | `refresh_local` 結束時，刪掉「這次 `list_sessions` 裡沒有」的快取檔；互動模式顯示搜尋結果之前，先和清單比對一次 |
| K8 | `reading/` 存的是**這台機器上所有 agent session 的全文**（包括從來沒有匯入過的），檔案的權限是預設的（umask，通常是 644），同一台機器上的其他使用者也讀得到 | `reading/` 這個資料夾建立時設成 `0o700`。鏡像也一樣，不過它本來就是 agora 自己的資料 |
| K9 | `cmd_sync`：outbox 還有沒推上去的時候，也是回傳 0 | 改成回傳 `EXIT_IN_OUTBOX`（3），和其他寫入的指令一致 |
| K10 | `agora cache agora` 會把**所有** Session 的 raw 都下載下來，可能有好幾 GB，而且事前沒有任何提示 | 開始之前，先從 `list_sessions` 的大小（`lsjson` 會有 Size）算出總量，印出「要下載 N 個、共 X MB」；互動模式的 `r` 也在確認視窗裡顯示這個數字 |
| K11 | Textual：(a) Busy 執行中沒辦法取消（Textual 會攔截 Ctrl-C），寫要約的 merge 可能要等好幾分鐘；(b) 使用者在內文搜尋執行緒還在跑的時候按 `q` 離開，最後那個 `call_from_thread(self.say, …)` 在 `try` 外面，可能會在離開之後印出一段 traceback；(c) `self.index` 只在主執行緒裡使用 ✅，Busy 裡的 `cli.main` 會自己開一個新的 `Index`（新的 sqlite 連線），所以沒有跨執行緒共用 sqlite 連線的問題 ✅ | (a) Busy 加一個「取消」的按鍵（取消時讓工作在下一個來源之前結束；merge 本來就是最後才存檔，所以取消不會留下半份）；(b) 把最後那一行也包進 `try`，或者先檢查 `self.is_running` |

## 這次跑過的指令

| 指令 | 結果（只記形狀） |
|---|---|
| `git show --stat 4dc53d0 822fc11`、讀 `cache.py` 全文、`git show 4dc53d0 -- src/agora/store.py src/agora/cli.py tests/fakes/fake_rclone.py`、讀 design 5.10 | 見上面 |
| 讀 tui.py 的 `Busy`、`act`、`outside`、`load_full`／`import_preview`、`find_in_agents`、`self.index` 被用到的地方 | K3、K4、K11 |
| 在 scratchpad 用假的 rclone（`FAKE_REMOTE`、`AGORA_RCLONE`、`AGORA_CONFIG／CACHE_DIR／STATE_DIR` 全部指到 scratchpad）：stage 並 push 一個 Session → sync → fetch_raw → 刪掉遠端的資料夾 → 在鏡像裡放 `*.partial` 和 `.DS_Store` → `cache.sync_up()` | K1 的實測結果（復活，並且多了 `.partial`、`.DS_Store`）；之後 scratchpad 已經刪掉 |

沒有跑整合測試，沒有碰 Drive，沒有讀任何真實的 Session，也沒有執行不帶參數的 `agora`。
