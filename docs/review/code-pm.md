# Code review：PM 寫的 header／store／cli／agents.base

2026-10-02，review。對象是 commit `e91d998`（`src/agora/header.py`、`store.py`、`cli.py`、`agents/base.py`，以及 `tests/unit/test_header.py`、`test_store.py`、`test_cli.py`、`tests/fakes/fake_rclone.py`）。對照的是 design.md 第 3 版、`docs/review/design.md`（S／H／L／N／T）與 `test-plan.md`。只提意見，沒有改程式。`agents/opencode.py`、`agents/claude.py` 不在這次的範圍。

## 結論

- **(1) 正確性：有條件可行。** 主路徑（import → search → continue → 存回、outbox 重送、merge 沒有 raw、S1 的寫入順序）都是對的，34 個單元測試全部通過。但有 **2 條 High**：
  - **C1**：agora 被殺掉、agent 還在跑的時候，flock 已經被釋放，另一個終端機會把還在進行的 session 提早存起來（讀到半份）。
  - **C2**：有三種壞掉的本機檔或 Drive 檔會讓**每一個** agora 指令都 crash，直到使用者手動清理。

  兩條都已經實際重現（見文末）。
- **(2) 測試：** U-HDR 只缺 1 條；U-ST 缺 7 條；指令層（test-plan 的 U-IMP／U-MRG／U-CON／U-SHW／U-SRC，PM 的檔叫 U-CLI）缺得最多，特別是訊號、pending 的競態與 `--dir`。
- **(3) 行數：** 目前 `src/` 約 1,690 行（含兩個 adapter，約 670 行）。2,000 行內做得到。可以刪或合併的大約 30 行，見第 (3) 節。

| # | 嚴重度 | 位置 | 一句話 |
|---|---|---|---|
| C1 | **High** | cli.py:247–259、design 4.2 | 只看 agora 的 flock 不夠：agora 被 `kill -9` 之後，agent 還在跑，pending 就會被提早補存 |
| C2 | **High** | header.py:89、store.py:205、226、373、cli.py:200 | 壞掉的 pending JSON、少了 session.md 的 outbox、YAML 壞掉的 session.md，都會讓所有指令 crash |
| C3 | Medium | cli.py:247–249、200–220 | pending 從「寫入」到「上鎖」之間有空窗；兩個指令同時補存會遇到 FileNotFoundError |
| C4 | Medium | store.py:159 | `stage` 先 rmtree 再寫入，不是原子操作；中途 crash 就會變成 C2 |
| C5 | Medium | cli.py:229 | 相對路徑的 `--dir` 沒有轉成絕對路徑 |
| C6 | Medium | cli.py:196 | continue 的 `--header title=…` 會被父 Session 的 title 蓋掉 |
| C7 | Medium | store.py:205–223 | 每次 push 都把整個 `sessions/` 遞迴列一次 |
| C8 | Low | store.py:294 | `children()` 把只是被 `refs` 參照到的 Session 也當成子 Session |
| C9 | Low | store.py:395–414、cli.py | N8 重抓 session.md 之後，`parents.raw_md5` 還是記舊的 |
| C10 | Low | cli.py:112 | 同一個來源出現多個 Session 時，「最新的」是靠排序決定，排序不穩定 |
| C11 | Low | cli.py | N13 說要「標示未上傳」，但 search 的輸出沒有標示 |
| C12 | Low | cli.py:89、326 | `EXIT_CODE` 是全域變數：補存失敗會讓不相干的指令也回 3 |
| C13 | Low | header.py:101 | `validate()` 在執行期間沒有被呼叫，所以 H4「不認得的版本要警告」實際上沒有生效 |
| C14 | Low | 其他 | 幾個小問題，見第 (1) 節的最後 |

---

## (1) 正確性

### C1【High】agora 死了、agent 還活著時，pending 會被提早補存（讀到半份）
- **現況**：cmd_continue 對 pending 檔持有 flock，recover_pending 只要拿得到 flock 就補存。但 `subprocess.run` 預設是 `close_fds=True`，agent 子行程**沒有**繼承那個 fd。所以 agora 一被殺掉（例如 `kill -9`、OOM，或者 agora 自己的例外），鎖就釋放了，但 agent 可能還在終端機裡跑。
- **情境**：使用者在 A 終端機用 opencode 接續，agora 因為某種原因死掉了，opencode 還在。這時 B 終端機跑一次 `agora search`，就會把**對話到一半**的 session collect 起來存好，然後刪掉 pending。A 之後說的內容，永遠不會進 Agora。這正是第 2 版確認 N2 要防的情況；design 第 3 版的 4.2 只寫了「拿得到 flock＝那個 agora 不在了」，少了「agent 也不在了」這個條件。
- **已重現**：模擬 cmd_continue 的寫法，`kill -9` 父行程之後，子行程 `sleep` 還活著，另一個行程用 `LOCK_NB` 拿得到鎖。改成 `pass_fds` 之後就拿不到（見文末）。
- **建議**（一行）：`subprocess.run(..., pass_fds=(lock.fileno(),))`。flock 屬於「open file description」，只要 agent（或者它還開著的子行程）還持有這個 fd，鎖就不會釋放。如果失敗，失敗的方向是安全的：agent 的子行程活得比較久時，補存只是延後，不會變成提早。
  - design 4.2 改成：「agora **和 agent** 都持有 pending 的 flock（agent 用繼承的 fd），拿得到鎖就表示兩者都結束了」。
  - 如果不想讓 agent 繼承 fd，替代做法是記下 `agent_pid` 與它的啟動時間（`ps -o lstart= -p`），兩個條件都要成立才補存。這樣大約多 15 行。

### C2【High】三種壞掉的檔案，會讓所有指令都 crash
`main()` 只接住 `HeaderError`、`StoreError`、`AgentError`。下面三種情況丟出的是其他的例外，而且這些檔案會一直留著，所以使用者手動刪掉之前，**每一個**指令都會壞：

| 情境 | 怎麼來的 | 實際的結果 |
|---|---|---|
| 不完整的 pending JSON | `_write_pending` 寫到一半就 crash（或磁碟滿了） | `search --no-sync` 也是 rc=1，`JSONDecodeError`（recover_pending 在每個指令最前面就執行） |
| outbox 資料夾裡沒有 session.md | C4 的 stage 寫到一半 | `sync` rc=1，`FileNotFoundError`（push_one 的第一行就去讀它） |
| session.md 的 YAML 壞了（outbox 裡的，或 Drive 上另一台機器的） | 程式的 bug，或者手動編輯 | `sync` rc=1，`yaml.parser.ParserError`。在 sync 的下載迴圈裡，只接了 `(StoreError, HeaderError)`，所以 **Drive 上只要有一份壞檔，所有機器都會壞** |

**建議**：
- `split_document` 把 `yaml.YAMLError` 和 `UnicodeDecodeError` 都包成 `HeaderError`。這一條就能修好第 3 種，並且讓 sync 的 `except` 生效；
- `push_outbox` 改成接 `(StoreError, h.HeaderError, OSError)`；壞掉的 entry 移到 `outbox/.bad/`，並且警告；
- recover_pending 每一份檔都包一個 `try`，接 `json.JSONDecodeError`、`OSError`、`KeyError`；壞掉的移到 `pending/.bad/`，不要刪掉（裡面可能有唯一的 agent_session_id）；
- `main()` 最外層再加一個 `except Exception`：印一行，並且指出是哪個檔案，回傳 2，但保留 traceback 給 `AGORA_DEBUG=1` 時看。

### C3【Medium】pending 的兩個競態
1. **寫入與上鎖之間有空窗**（cli.py:247–249）：`_write_pending` 寫好檔案之後，才 `open` 加 `flock`。另一個終端機的指令如果剛好在這個空窗裡跑 recover_pending，它會拿到鎖，`collect` 時看到沒有新內容（agent 還沒啟動），就回傳 None，接著**把 pending 刪掉**。結果是：這次 continue 從此沒有 pending 保護；正常結束時 `pending.unlink()` 會丟出 FileNotFoundError，新的 id 雖然存好了，卻沒有印出來。**建議**：先用 `O_CREAT|O_EXCL` 開一個暫存檔，上鎖，寫入，然後 `os.rename` 成正式的名字（鎖跟著 inode 走）。
2. **兩個指令同時補存**：行程 1 補存完、`unlink` 之後，行程 2 才去 `open(path)`，就會 FileNotFoundError（沒有被接住）。另一種情況：行程 2 在 unlink 之前就 open 了，之後拿到鎖，會對一個已經 unlink 的 inode 再補存一次（agora_id 相同，所以只是多傳一次），最後 `path.unlink()` 一樣 FileNotFoundError。**建議**：`open` 時接 FileNotFoundError 就跳過；拿到鎖之後比對 `os.fstat(f).st_ino == os.stat(path).st_ino`，不一樣就跳過；`unlink` 改成 `missing_ok=True`。

### C4【Medium】`stage` 不是原子操作（store.py:159）
它會先 `rmtree` 舊的 outbox 資料夾，再一個一個寫入檔案。如果在中間 crash：(a) 留下一個沒有 session.md 的資料夾，就是 C2 的第 2 種；(b) 重新匯入時，舊的 entry（**可能還沒上傳**）已經被刪掉，而新的還沒寫完。**建議**：寫到 `outbox/.tmp-<ulid>/`，寫完之後 `os.replace` 成 `outbox/<ulid>/`（要取代已經存在的資料夾，就先把舊的 rename 成 `.old-<ulid>`，再刪掉）。`_outbox_ulids` 與 push_outbox 都略過開頭是 `.` 的名字。

### C5【Medium】相對路徑的 `--dir`（cli.py:229）
`Path(args.dir)` 沒有 `.resolve()`。結果是：(a) pending 裡的 `dir` 是相對路徑，之後在其他目錄補存時，collect 會找錯地方；(b) Claude 的專案目錄是依路徑編碼的，用相對路徑編出來的名字是錯的；(c) `source.dir` 會寫進相對路徑，違反 H1 的「絕對路徑」。**建議**：`workdir = Path(...).expanduser().resolve()`，而且目錄不存在就直接報錯。

### C6【Medium】continue 的 title 會蓋掉 `--header title=`（cli.py:196）
`_new_header` 已經套用了 header_updates，但下一行 `hdr["title"] = record.get("title") or exported.title` 又用父 Session 的 title 蓋掉它。PM 的測試只驗證了 `tags`，所以沒有抓到。**建議**：改成 `hdr["title"] = hdr.get("title") or record.get("title") or exported.title`，並且補一個測試。

### C7【Medium】每次 push 都遞迴列一次全部（store.py:205–223）
push_one 為了驗證一個 Session，呼叫的是 `list_sessions()`，也就是把整個 `sessions/` 遞迴列一次。一次 import 就要列兩次（sync 一次、push 一次）；outbox 有 N 筆時就要列 N 次。Session 一多，每次存檔都要多等幾秒，違反「等待短」。**建議**：只列這個 Session 的資料夾：`lsjson gdrive:sessions/<ulid> --hash --files-only`。在 Drive 類別裡加一個 `list_one(ulid)`，大約 5 行。

### C8【Low】`children()` 會誤判（store.py:294）
它是在 header 的 JSON 裡找 `"agora:<ulid>"` 這個字串，所以只是 `refs` 裡**提到**這個 Session 的項目也會被當成子 Session。結果是重新匯入時會建新的 Session，而不是更新原本的。不會掉資料，但會讓人搞不清楚。**建議**：只看 `parents[*].id`。把 parents 的 id 另外存成一欄，或者在 Python 端解析 header 之後再比對。

### C9【Low】N8 重抓之後，parent 的版本記錯了
`fetch_raw` 抓不到 raw 時，會重抓 session.md，然後改抓新的 raw。但 cmd_continue 用的 `parent` 還是舊的 header，所以 `parents[0].raw_md5` 記的是**已經被刪掉的舊版**。**建議**：讓 `fetch_raw` 回傳 `(raw_bytes, header)`，cmd_continue 用回傳的 header 來寫 parent。

### C10【Low】「同一個來源取最新的」不穩定（cli.py:112）
search 依 `source.created_at` 排序，但同一個來源的多個 agora Session，`source.created_at` 是一樣的，排序的結果就看 sqlite 的列順序，不一定是最新的那個。**建議**：排序鍵改成 `(source.created_at, updated_at, ulid)`。

### C11【Low】N13 的「未上傳」沒有顯示
outbox 裡的 Session 搜得到（`remember` 有放進索引），但 search 的輸出看不出它還沒上傳。**建議**：在 cmd_search 裡，ulid 如果在 `_outbox_ulids` 裡，就在那一行最後加 `(未上傳)`。這樣索引裡的 md5 也不必再用 `"outbox"` 這個特殊值（見第 (3) 節）。

### C12【Low】`EXIT_CODE` 是全域狀態（cli.py:89、326）
recover_pending 補存時如果上傳失敗，會把 `EXIT_CODE` 設成 3，結果使用者跑的 `search` 也回 3。測試也得用 monkeypatch 把它歸零。**建議**：讓 `_save` 回傳 `(id, uploaded)`，由呼叫它的指令自己決定 exit code。

### C13【Low】H4 在執行期間沒有生效（header.py:101）
`validate()` 只有測試在用。sync 或 show 讀到 `header: 99` 時不會警告。**建議**：sync 的 `index.put` 之前呼叫 `h.validate(hdr)`，把警告印出來；遇到 HeaderError 就當作讀不到，跳過（這樣也會一起走 C2 的處理）。只要 3 行。

### C14【Low】其他
- `store._fault("after-agent-launch")`（cli.py:250）其實是在 agent **啟動之前**，名字和 test-plan 的意思不一致。改名成 `before-agent-launch`，或者移到 Popen 之後（那就要改用 Popen 加 wait）。
- 從 `start_native`／`start_injected`，到寫好 pending、設好 SIG_IGN 之前，如果使用者按 Ctrl-C，opencode 那邊已經 import 好的新 session 就變成孤兒，沒有 pending 會去處理它。不會掉資料（使用者還沒開始說話），只是留下一筆垃圾 session。可以在 design 裡寫明這是已知的情況。
- 每個指令開始時都會跑 recover_pending，而補存要上傳，所以 `search --no-sync` 也可能卡在網路上。建議 `--no-sync` 時只提示「有 N 筆待補存」，不實際補存。
- `_snippet` 是從正規化（lower 加 NFKC）之後的文字裡截出來的，所以使用者看到的片段是小寫的，而且混著 title／note。可以接受，但要在 design 4.5 寫一句。
- 重新匯入時，如果內容沒變（cli.py 的 import），會連同 `--header` 的更新一起忽略。使用者想補一個 title 就沒辦法。建議：內容沒變、但有 `--header` 時，只更新 header。
- `folder_id()` 只在 Drive 根目錄找同名的資料夾，所以 `AGORA_FOLDER_NAME` 不能是 `agora-test/it-<run>` 這種巢狀的名字。目前整合測試直接用 `agora-test`，可以運作；但 test-plan (b) 建議的「每次執行用一個子資料夾」就做不到。看要改程式，還是改 test-plan。
- 設定檔的實作是 `config.json`，design 第 4 節寫的是 `config.toml`。要讓兩邊一致。

---

## (2) test-plan 還缺的測試

PM 的測試裡已經有：U-HDR-01～07、03b；U-ST-01/02、03（只驗了最後的狀態）、04/05（只驗了 md5 被竄改）、06/08、09、12、13、14、15、17、22；T1／T2 的中日文；以及指令層的 IMP-01、06、07、08、CON-03、05、07b、09、15（部分）、MRG-01/02、N13、S7。下面列出還缺的。

### U-HDR
| 缺的 | 為什麼重要 |
|---|---|
| U-HDR-08：import 之後，`source.dir`、`host`、`agent_version`、`source.created_at`、`header: 1` 都有值 | H1、H7 的欄位只有 `session_id` 被驗證過 |
| 新增：`split_document` 遇到壞掉的 YAML 和非 UTF-8 的內容，丟的是 `HeaderError` | C2 |

### U-ST
| 缺的 | 為什麼重要 |
|---|---|
| U-ST-03 的「每一次 rclone 呼叫之後，都沒有懸空的指標」 | 目前只驗了最後的狀態。fake_rclone 要加上「每次呼叫後拍一份快照」（test-plan 0.3 (b)） |
| U-ST-04 的「raw 不存在」與「之後補上就搜得到」 | 目前只測了 md5 被竄改 |
| U-ST-07：session.md 上傳失敗時，遠端只有 raw，另一組 cache 不會建索引 | 這是 S1 加 S2 的交界 |
| U-ST-16：同名資料夾會讓 `folder_id()` 報錯 | fake_rclone 還沒有支援 `FAKE_RCLONE_DUP` |
| U-ST-18：merge 的 session.md（沒有 raw）可以被另一組 cache 建索引 | N5 |
| U-ST-19：判斷是否寫完時沒有下載 raw（看 calls.log） | N11 |
| U-ST-20：依 design 第 3 版改寫：`sessions/` 不存在時不刪鏡像；列檔失敗時也不刪 | N12。test-plan 寫的是「清單是空的就不刪」，和 design 第 3 版不一致，**test-plan 要改**（這次只能 commit 這個檔，所以留到下一次） |
| U-ST-21：fetch_raw 抓不到時，會重抓 session.md 再試 | N8，加上 C9 |
| 新增：outbox 裡壞掉的 entry 會被移到 `.bad/`，sync 照常完成 | C2、C4 |
| 新增：push_one 只列這一個 Session 的資料夾（看 calls.log 的參數） | C7 |

### 指令層（test-plan 的 U-IMP／U-MRG／U-CON／U-SHW／U-SRC；PM 的檔叫 U-CLI）
| 缺的 | 為什麼重要 |
|---|---|
| U-CON-11、11b：fake agent 改成真的子行程（例如 `[sys.executable, script]`），對 process group 送 SIGINT：agora 不會死；子行程的 SIGINT 是預設行為 | N1。現在的 FakeAgent 用 `argv=["true"]`，完全沒測到訊號 |
| U-CON-16：agora 被 `kill -9`、agent 還活著時，另一個指令**不會**補存 | **C1**。照目前的程式，這條會失敗 |
| U-CON-17，以及 C3 的兩個競態：同時補存只會產生一個資料夾，而且不會 crash；寫入 pending 與上鎖之間不會被搶走 | C3 |
| U-CON-08：agent 啟動的那一刻，pending 已經存在，而且是鎖住的 | S3 |
| U-CON-10：`AGORA_TEST_FAULT=before-finalize` 之後，下一個指令會補存 | S3 |
| U-CON-13：收尾時上傳失敗 → 存進 outbox，pending 被清掉，exit 3 | S2、S3 |
| U-CON-06：`--dir` 的預設值（`source.dir` 存在、不存在、明確指定），加上相對路徑會轉成絕對路徑 | H1、**C5** |
| 新增：continue 加 `--header title=X`，結果的 title 是 X | **C6** |
| U-CON-14：鏡像裡沒有 raw → 去抓，md5 不符就 exit ≠ 0 | L2、S1 |
| U-CON-18、19：從 merge 接續時 `raw_md5: null`；continue 結果的 `source` 是新的 agent session | N5、N6 |
| U-IMP-08b：只被 merge 過，也算有子 Session；另加 C8：只是被 refs 參照到，**不算** | N16、C8 |
| U-IMP-09：cache 2 從來沒 sync 過，匯入同一個來源 → 得到同一個 id | S6 |
| U-IMP-10：同一個來源有兩個 Session → 顯示最新的那個，並且警告（加上 C10 的排序） | S6、C10 |
| U-MRG-02b、03：merge 的結果再拿去 merge；給了不存在的 id 時，遠端與 outbox 都沒有新東西 | N5、5.3 |
| U-SHW-01～03：`show` 的輸出格式；不存在的 id；`--raw` 會延遲去抓，merge 用 `--raw` 時給清楚的訊息 | 5、4.3 |
| U-SRC-01：輸出的格式與日期欄；U-SRC-07：`C++`、`"`、`AND`、`NEAR`、`*` 不會 crash；U-SRC-09：六個別名都透過 CLI 測一次；U-SRC-10：刪掉 index.sqlite 後重建；U-SRC-12：`ui` 比得到 `ＵＩ`；U-SRC-13：merge 那一行的顯示 | 5.1、T1、T2、N6、N10 |
| 新增：outbox 裡的 Session 在 search 的輸出標示 `(未上傳)` | C11 |
| 新增：補存失敗不會改變 search 的 exit code | C12 |

另外，`test_merge_then_continue_uses_injection` 用的是 `merge-session "a,b"` 這種逗號寫法，但 design 沒有這個寫法。測試應該照 design 寫成兩個參數（見第 (3) 節）。

---

## (3) 可以刪掉或合併的程式

| 項目 | 位置 | 省下的行數 | 建議 |
|---|---|---|---|
| `Ref.__str__` 與 `quote` | header.py:55–61 | 約 8 行 | `src` 裡沒有任何地方用到，只有一個 round-trip 測試在用。等真的要產生 ref 再加回來 |
| `outbox_count` 與 `_outbox_ulids` 重複了 | store.py:177–183 | 約 3 行 | 只留 `_outbox_ulids`，數量用 `len()` 算（同時讓它略過開頭是 `.` 的名字，見 C4） |
| `_index_outbox` 與 `remember` 做的事一樣 | store.py:185–203 | 約 6 行 | `_index_outbox` 改成對每一筆呼叫 `remember` |
| `_save` 在成功之後又 `put` 一次 | cli.py:79–92 | 約 4 行 | push 之前就用真正的 md5 呼叫 `remember` 一次；「未上傳」改成用 outbox 判斷（C11），就不需要 `"outbox"` 這個特殊的 md5，也不用 put 第二次 |
| `EXIT_CODE` 全域變數 | cli.py | 約 3 行 | 見 C12 |
| merge 的逗號寫法 | cli.py:161 | 1 行 | design 沒有這個寫法；移除後，`ids` 直接用 argparse 的結果 |
| fake_rclone 的 `purge` | tests/fakes | — | 不算在 `src` 的行數裡，留著也沒關係（之後 U-ST-12 可以改用它） |

刪掉這些大約省 25～30 行。C1～C7 的修正大約會**增加** 40～50 行（其中 C2 約 15 行、C3 約 10 行、C4 約 8 行、C7 約 5 行）。整體仍然是 1,700 多行，在 2,000 行以內。

---

## 對 design.md 的修改建議

1. **4.2 pending**：改成「agora 與 agent 都持有 pending 的 flock（agent 繼承 fd）；拿得到鎖＝兩者都結束了」。另外加上：pending 用「先寫暫存檔再 rename」的方式建立；壞掉的 pending／outbox 移到 `.bad/`，不要刪掉（C1、C2、C3）。
2. **4.1**：outbox 的 entry 先寫到暫存資料夾，再 rename 成正式的名字（C4）。
3. **4.3**：push 之後只列這個 Session 的資料夾來驗證（C7）。
4. **5.4**：`--dir` 一律轉成絕對路徑，目錄不存在就報錯（C5）。
5. **第 4 節**：設定檔是 `config.json` 還是 `config.toml`，統一一個寫法（C14）。
6. **4.5**：寫一句「search 顯示的片段是正規化之後的文字」（C14）。

## 這次實際跑過的指令

沒有碰 Drive，也沒有跑任何 agent，沒有讀任何 Session 或 MyBrain，也沒有讀 rclone.conf。

| 指令 | 看到的結果（只記形狀） |
|---|---|
| `git fetch`、`git log`、`git diff ab93129 HEAD` | review 進行到一半時，PM 推了 `840344f`（design 第 3 版）與 `e91d998`（`remember` 與 `test_cli.py`），所以這份 review 以 `e91d998` 為準 |
| `uv run pytest -q tests/unit` | 34 passed |
| `scratchpad/cr.sh`：用 scratchpad 裡的 HOME／CONFIG／CACHE／STATE，`AGORA_RCLONE=/usr/bin/false`，用 `.venv/bin/python -m agora.cli` 執行 | (1) 不完整的 pending JSON → `search --no-sync` rc=1，`JSONDecodeError`；(2) outbox 裡沒有 session.md → `sync` rc=1，`FileNotFoundError`；(3) YAML 壞掉的 session.md → `sync` rc=1，`yaml.parser.ParserError`（C2） |
| `scratchpad/flock_demo.py`：父行程持有 flock 之後執行 `sleep 6`，然後 `kill -9` 父行程，再用另一個行程 `LOCK_NB` | 沒有加 `pass_fds`：鎖是**空的**，`sleep` 還活著（C1 成立）；加了 `pass_fds`：鎖**仍然被持有** |

---

## 修正確認（2026-10-02，對象 `1c0177e`）

**結論：C1～C14 都修了，方向都對，39 個單元測試全部通過，我原本的三個 C2 重現也都不再 crash。** 但 C2 的修法帶進了一個新的 **High**（R1）：rclone 叫不起來時，**完好的 outbox entry 會被當成壞檔，移進 `.bad/`**，從此不再重送。這已經實際重現了。另外還有 2 條 Medium、4 條 Low。

### 逐條確認

| # | 狀態 | 確認的內容 |
|---|---|---|
| C1 | ✅ | `subprocess.run(..., pass_fds=(lock.fileno(),))`。design 4.2 也改成「agora **和** agent 都不在了，才拿得到鎖」。但測試沒有真的走過 cmd_continue（R3） |
| C2 | ⚠️ | `split_document` 會把 YAML 錯誤包成 HeaderError；pending 與 outbox 的壞檔會移進 `.bad/`；main 最外層有 catch-all。我的 `cr.sh` 三個情境現在 rc 都是 0，壞掉的 outbox 也確實移進了 `.bad/`。**但 push_outbox 把所有 `OSError` 都當成「壞檔」**（R1）；另外，recover_pending 把 `_finish` 裡的 `KeyError` 也當成壞檔（R4） |
| C3 | ⚠️ | 改成建立時就上鎖，並且容忍同時補存（`path.exists()` 檢查加上 `missing_ok`），主要的競態已經解決。從 `open("a+")` 建立檔案到 `flock` 之間，還有一個非常短的空窗（R5） |
| C4 | ⚠️ | 先寫到 `.tmp-<ulid>`，再 rename 成正式的名字，最常見的情況已經解決。但 `rmtree(folder)` 和 `tmp.rename(folder)` 之間還有一個空窗（R6） |
| C5 | ✅ | `.expanduser().resolve()`；也有測試。目錄不存在時沒有報錯，但不影響正確性 |
| C6 | ✅ | `hdr.get("title") or ...`；也有測試 |
| C7 | ✅ | 新增 `list_one(ulid)`，push 只列自己這一個資料夾 |
| C8 | ✅ | 只看 `parents[*].id` |
| C9 | ✅ | 改成用實際抓到的 raw 算 md5 |
| C10 | ✅ | 排序鍵改成 `(source.created_at, updated_at, ulid)` |
| C11 | ✅ | search 的輸出加了 `(未上傳)`，也有測試 |
| C12 | ✅ | 改成 `_save` 回傳 `(id, uploaded)` 再由 `_emit` 決定；全域變數已經移除，也有測試 |
| C13 | ✅ | sync 時會呼叫 `validate`。只是嚴格程度可以再調整（R7） |
| C14 | ✅ | fault 點改名成 `before-agent-launch`；`--no-sync` 時只提示、不補存；內容沒變但有 `--header` 時會更新 header；design 補上了片段是正規化文字的說明，`config.json` 也統一了。孤兒 session 這點沒有寫進 design（可以接受） |

第 (3) 節的刪減建議（`Ref.__str__`、`remember` 與 `_index_outbox` 合併、`"outbox"` 這個特殊 md5、merge 的逗號）都沒有採納。其中**逗號寫法是使用者原本的指令格式，要保留**，所以那一條撤回，test-plan 也已經補上測試（U-MRG-05）。其他幾條都不影響正確性，要不要做都可以。

### 新的問題

| # | 嚴重度 | 位置 | 問題 | 建議 |
|---|---|---|---|---|
| R1 | **High** | store.py 的 `push_outbox`，以及 `Drive._run` | push_outbox 接到 `(h.HeaderError, OSError)` 就 quarantine。但 `subprocess.run` 找不到 rclone 時丟的是 `FileNotFoundError`，它也是 OSError，所以**完好的 outbox entry 會被移進 `.bad/`**。之後沒有任何指令會重送它，也不會再提醒。會觸發的情況：rclone 不在 PATH 上（例如 launchd 或 cron 的環境、`brew upgrade` 的途中、`AGORA_RCLONE` 打錯）。outbox 裡往往是**還沒上傳的唯一一份**（agent 那邊雖然還在，但 agora 的關聯會斷掉）。**已重現**：stage 一筆正常的 entry，用 `AGORA_RCLONE=/nonexistent/rclone` 執行 `sync`，結果那筆被移進了 `outbox/.bad/`，而且 main 的 catch-all 又印了一次 FileNotFoundError | (a) `Drive._run` 把 `OSError` 包成 `StoreError`（「叫不起 rclone」屬於暫時性的失敗，下次可以再試）；(b) push_one 先**單獨**讀取並解析本機的檔案，只有這一步失敗才 quarantine，rclone 階段的任何錯誤都留在 outbox。只要 (a) 就能修好這次的重現，(b) 是讓「什麼算壞檔」的界線清楚 |
| R2 | Medium | cli.py 的 main、store.py 的 quarantine | `.bad/` 只在被移進去的那一次警告一行，之後就沒有任何提醒。使用者很容易沒看到，而裡面的東西可能是唯一的一份 | 每個指令開始時，如果 `outbox/.bad` 或 `pending/.bad` 不是空的，就印一行「有 N 筆壞檔在 …，請檢查」（和 outbox 的提示放在一起，大約 3 行） |
| R3 | Medium | tests/unit/test_cli.py 的 `test_lock_survives_agora_death_while_agent_lives` | 這個測試是自己 `Popen(..., pass_fds=...)`，驗證的是 flock 的語意，**沒有**走過 cmd_continue。就算有人刪掉 cmd_continue 裡的 `pass_fds`，它也照樣會過 | 用 cmd_continue 跑一個真的子行程 agent（像 N1 測試那樣的 python script，裡面 sleep）：用 subprocess 啟動 `python -m agora.cli continue-session ...`，等 agent 寫好 marker 之後，`kill -9` agora，確認 LOCK_NB 拿不到鎖；殺掉 agent 之後，`sync` 就會補存。或者至少 monkeypatch `subprocess.run`，確認收到的參數裡有 `pass_fds` |
| R4 | Low | cli.py 的 recover_pending | 這裡的 `except (json.JSONDecodeError, KeyError, UnicodeDecodeError)` 把整個 `_finish` 包在一起。adapter 的 `collect` 如果因為 bug 丟出 KeyError，**好的 pending 也會被 quarantine**，就不再自動重試 | 先 `json.loads`，並且檢查必要的 key（`agora_id`、`agent`、`dir`、`parent`），只有這一步失敗才 quarantine；`_finish` 丟出的任何例外，都當成「下次再試」 |
| R5 | Low | cli.py 的 `_write_pending` | `open(path, "a+")` 建立檔案之後、`flock` 之前，另一個指令可能搶先拿到鎖，讀到空檔案，然後把它 quarantine。結果是：這次 continue 沒有 pending 保護，而且結束時的 `pending.unlink()` 會丟出 FileNotFoundError。空窗只有幾微秒 | 寫到 `<ulid>.json.tmp`（glob 是 `*.json`，所以不會被掃到），上鎖、寫入之後再 `os.rename`；或者 recover_pending 遇到**空檔案**時直接跳過，不要 quarantine（一行就好） |
| R6 | Low | store.py 的 `stage` | 如果在 `rmtree(folder)` 之後、`tmp.rename(folder)` 之前 crash，新的內容只在 `.tmp-<ulid>` 裡，而 `_outbox_ulids` 會跳過開頭是 `.` 的名字，所以它不會被上傳，舊的也已經刪掉了 | 先把舊的 rename 成 `.old-<ulid>`，再把 tmp rename 成正式名字，最後才 rmtree 舊的；或者 push_outbox 時，如果看到含有 session.md 的 `.tmp-*`，而正式的資料夾不存在，就把它補上去 |
| R7 | Low | store.py 的 sync | `validate` 遇到 HeaderError（例如 refs 裡有一個未來才會有的 entity）就整份跳過。這和 H3／H4「不認得就警告，照樣讀共通欄位」的精神不一致 | 在 sync 裡，ref 或 case 的錯誤降級成警告；只有 `entity`／`id` 錯了才跳過 |

### 這次跑過的指令

| 指令 | 結果（只記形狀） |
|---|---|
| `git fetch`、`git diff ef9f90a 1c0177e` | 改到的是 src 的 3 個檔、test_cli.py、design.md（4.2、D5、4.5） |
| `.venv/bin/python -m pytest -q tests/unit/test_header.py test_store.py test_cli.py` | 39 passed |
| `scratchpad/cr.sh`（同上一節，`AGORA_RCLONE=/usr/bin/false`） | 三個情境都是 rc=0；壞掉的 outbox entry 移進了 `outbox/.bad/`；`--no-sync` 時不會去動 pending |
| 用 scratchpad 的目錄 stage 一筆正常的 entry，`AGORA_RCLONE=/nonexistent/rclone`，執行 `agora sync` | 那筆**正常的** entry 被移進了 `outbox/.bad/`，outbox 變成只剩 `.bad`（R1） |

沒有碰 Drive，沒有跑任何 agent，沒有讀任何 Session、MyBrain 或 rclone.conf。
