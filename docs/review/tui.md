# Review：design 5.9 互動模式，以及 base.py 的新介面

2026-10-02，review。對象是 `1804d8e`（design 5.9）和 `f9fd72d`（base.py：`Listed`、`list_sessions()`、`last_message()`）。轉接器的實作還沒進來，所以這次只看設計和介面。只提意見，沒有改設計或程式。依照指示，沒有跑整合測試、沒有碰 Drive，也沒有讀任何真實的 Session。我只看了 `opencode session list --help` 有哪些選項。

## 結論

版面、分頁、按鍵、「所有動作都呼叫指令模式同一套程式」這幾個方向都很好，`curses`、純函式、單元測試的分工也對。要補進設計的有 **2 個安全相關的 Medium**，以及幾個**一定會遇到**的情況：

| # | 嚴重度 | 一句話 |
|---|---|---|
| T1 | **Medium（安全）** | 只要打 `agora`、不帶參數，就會列出**本機所有專案的真實 session**。隊員（AI agent）在 shell 裡誤打一次 `agora`，真實資料就會進到它的 context。設計應該寫明「**不是 TTY 就不啟動互動模式**」，並且把測試的防線寫清楚 |
| T2 | **Medium（安全）** | opencode 的「所有專案」要怎麼列出來，設計沒有寫。現在已知的方法有兩種：一是在沒有 commit 的資料夾裡跑 `opencode session list`（這正是我們在團隊規則裡**禁止**的那個怪行為），二是直接讀 `~/.local/share/opencode`。兩種都需要明確的規則和測試防線 |
| T3 | Medium | 中文：(a) 沒有寫 `locale.setlocale(LC_ALL, "")`，curses 印中文就會變成亂碼；(b) 欄寬用 `len()` 算會對不齊（中文字佔 2 欄），截斷也可能把一個寬字元切成兩半；(c) `/` 篩選如果用 `getch()` 讀取輸入，就**打不了中文**，要用 `get_wch()` |
| T4 | Medium | 很窄或很矮的終端機，以及縮放視窗：curses 寫到畫面外面會丟出 `curses.error`，導致整個程式 crash。設計要寫明最小尺寸、`KEY_RESIZE` 的處理 |
| T5 | Medium | 未匯入頁的雜訊：continue 時，`start_native` 一定會在 agent 那邊建一個新的 session。使用者如果什麼都沒說就離開（collect 回傳 None），那個複本會**留在 agent 裡**；在未匯入頁上看起來就是一個「還沒匯入」的 session（內容和已經存在的 Session 重複）。summarize 留下的孤兒（v6 Z2）也一樣 |
| T6 | Medium | 開啟時同步：perf 的量測記錄過冷啟動要 85 秒。設計寫的是「開啟時同步一次」，如果是**阻塞式**的，打開 TUI 就要等一分多鐘；離線時也要明確顯示 |
| T7 | Medium | 從 TUI 接續 merge（或者來源的 `dir` 在這台機器上不存在）時，工作目錄會退回「目前目錄」，也就是使用者啟動 `agora` 的地方，可能是 repo。`8f53e82` 那次意外就是這樣發生的。彈出的視窗必須**能改**目錄，不能只顯示 |
| T8～T15 | Low | 空清單、短 id 重複、批次匯入、動作失敗或 Ctrl-C 後回到選單、預覽的長度與控制字元、篩選失敗、已匯入但有更新、`last_message` 的契約 |

## 安全

### T1（Medium）：只有使用者本人在終端機前才能進入互動模式

設計第 5.9 節最後寫了「只有使用者本人用真實資料試」，但這只是**開發流程**的規則，程式本身沒有任何防線。在這個專案裡，隊員（impl1／impl2，以及 review）都是在 shell 裡執行指令的 AI。只要有一個人在任何地方誤打 `agora`、不帶參數（例如想看用法），而 stdout 被重新導向或者被擷取，互動模式就會讀出並顯示**所有專案的真實 session 標題和最後一則對話**，這些內容就會直接進到那個 AI 的 context，違反 design 第 7 節。

**建議**（寫進 5.9，大約 3 行程式）：

1. 只有在 `sys.stdin.isatty() and sys.stdout.isatty()` 都成立時，才啟動互動模式；否則印出用法（和 `agora --help` 一樣），然後 exit 2。
2. 單元測試：用不是 TTY 的 stdin／stdout 呼叫 `main([])`，確認**沒有**呼叫任何轉接器的 `list_sessions`（用 spy 檢查）。
3. conftest 現在的防線（`AGORA_OPENCODE_CMD`／`AGORA_CLAUDE_CMD` 預設指向 `/nonexistent`、刪掉 `XDG_*`、`AGORA_CLAUDE_HOME` 指到 tmp）對單元測試是有效的。但**整合測試和 e2e 會把 `AGORA_CLAUDE_HOME` 設成真的家目錄**（`test_e2e_cli.py`），所以要再加一條規則：「整合測試不准呼叫 `list_sessions`，也不准呼叫不帶參數的 `main`」。可以用 `tests/integration/test_zz_guard_probe.py` 這種守衛測試，掃描整合測試的原始碼來把它鎖住。

### T2（Medium）：opencode 的「所有專案」要怎麼列出來

`opencode session list --help` 只有 `-n` 和 `--format`，**沒有任何「列出所有專案」的選項**。它只會列出 cwd 所屬專案的 session。已知可以拿到所有專案的方法有兩個：

- **在沒有 commit 的資料夾裡跑**：之前我們確認過，這樣會列出使用者全域的 session。但這是**沒有文件記載的行為**，以後的版本可能會改；而且這正是團隊規則裡明文禁止隊員做的事。
- **直接讀 opencode 的 SQLite**（`$XDG_DATA_HOME/opencode/…`，預設是 `~/.local/share/opencode`）：比較穩定，也比較快，但要依賴 opencode 內部的 schema。

**建議**：design 5.9 要明寫用哪一種方法。如果是讀資料庫：(a) 要尊重 `XDG_DATA_HOME`（conftest 已經會刪掉它），(b) 以唯讀方式開啟（`sqlite3.connect("file:…?mode=ro", uri=True)`），(c) 遇到不認得的 schema 時，回傳空清單並警告，不要 crash。如果是在空資料夾裡跑 CLI：資料夾要用 `tempfile.mkdtemp()`，跑完就刪掉，而且這段程式碼**只能**在 T1 的 TTY 檢查通過之後才會執行。不論選哪一種，都要在 spike/opencode.md 記錄實測的結果。

## 一定會遇到的情況

### T3（Medium）：中文

1. **locale**：Python 的 curses 要在 `curses.wrapper` 之前先呼叫 `locale.setlocale(locale.LC_ALL, "")`，否則多位元組的字元會印成亂碼或 `?`。
2. **顯示寬度**：不能新增依賴，所以沒辦法用 wcwidth。寫一個純函式 `display_width(s)`：`unicodedata.east_asian_width(c) in ("W", "F")` 算 2，`unicodedata.combining(c)` 算 0，其他算 1。對齊和截斷都用這個函式；截斷要用「顯示寬度」來算，結尾加上 `…`，而且不能把一個寬字元切成一半（剩下 1 欄時就補一個空格）。這個函式很適合寫單元測試：「驗收表格」的寬度是 10；「CSV 表格」的寬度是 8；混了 emoji 的字串，可以接受有一點誤差。
3. **輸入**：`/` 篩選要用 `stdscr.get_wch()`（會回傳 str），不能用 `getch()`（會回傳 int 或位元組），否則注音、倉頡等輸入法打出來的中文會壞掉。Backspace 要依「字元」刪除，不能依位元組。
4. 寫到畫面的**最右下角**那一格時，curses 會丟出 `curses.error`。寬字元剛好落在最後一欄時，也會遇到這個問題。所有的 `addstr` 都要先截到 `寬度 - 1` 欄，或者包一層 `try: … except curses.error: pass`。

### T4（Medium）：很窄或很矮的終端機、縮放視窗

- 設計只寫了「寬度 ≥ 100 欄時把預覽放在右邊，否則放在下面」。**最小尺寸**（例如 40 欄 × 10 行）要寫明：比這個小時，整個畫面只顯示一行「終端機太小（需要 40×10）」，不要嘗試畫版面。
- `KEY_RESIZE`（SIGWINCH）要重新計算版面，並且重畫。純函式 `layout(width, height) -> 各區塊的位置` 可以把 39×9、40×10、99×30、100×30、200×60 都寫成單元測試。
- 按鍵列在窄的終端機裡放不下時，要依優先順序縮短（例如先拿掉「Tab 換頁」的說明文字，只留下 `Tab`），或者改成可以捲動。

### T5（Medium）：未匯入頁會出現 agora 自己留下的複本

- continue 的時候，`start_native` 一定會在 agent 那邊建一個**新的** session（opencode 會 import 一份，Claude 會寫一份新的 jsonl）。如果使用者打開之後，什麼都沒說就離開，collect 會回傳 None，agora 印出「這次沒有新內容，沒有存」，但 agent 那邊的**複本還在**。它從來沒有被匯入過，所以 `Index.by_source` 找不到它，就會出現在**未匯入頁**上，內容和已經存在的 Session 一模一樣。使用者打開 continue 再離開幾次之後，未匯入頁就會被這些複本塞滿。
- summarize 留下的孤兒 session（v6 Z2，在 `<state>/summarize/` 這個專案裡），也會出現在未匯入頁上。

**建議**：(a) continue 結束、collect 回傳 None 時，依 id 刪掉 agora 自己剛建的那一個 agent session（Claude 刪掉那個 jsonl；opencode 用 `session delete <id>`），這和「只刪自己建的」原則一致；(b) 未匯入頁要排除 `dir` 是 `<state>/summarize` 的 session；(c) 也可以另外記一份「agora 建過的 agent session id」清單（pending 本來就有記錄），未匯入頁直接略過這些 id。

### T6（Medium）：開啟時的同步

- perf 的量測（`2e7b677`）：冷啟動 85 秒、push 11 秒。之後的 `download_many` 有改善，但「第一次打開」仍然可能要等很久。
- **建議**：一打開，就先用**本機的索引**把畫面畫出來；同步改在背景執行緒裡跑（同步本身不碰 curses），完成之後用一個旗標通知主迴圈重新整理。標題列要顯示「同步中…」／「離線（只有本機資料）」／「上次同步 3 分鐘前」。離線時（`store.sync` 會警告，然後回傳本機的索引），標題列要一直顯示「離線」，而需要 Drive 的動作（接續要抓 raw、合併子 merge 要抓 sections.json、刪除）就照常讓它失敗，顯示錯誤訊息，然後回到選單。
- 每一次動作之後的「重新整理清單」要用**本機的索引**，不要每次都完整同步一次。

### T7（Medium）：從 TUI 接續時的工作目錄

- 彈出的視窗設計是「選 agent、顯示工作目錄」。merge 沒有 `source.dir`，所以預設會是**目前目錄**，也就是使用者啟動 `agora` 的地方。如果是在 repo 裡啟動的，就會重演 `8f53e82` 那次意外（session 被留在使用者真正的專案裡）。
- **建議**：彈出的視窗要讓使用者能夠**修改**工作目錄（預設值依照 5.4 第 4 點）。退回目前目錄的時候，要用醒目的顏色或文字標示「⚠ 來源沒有記錄目錄，會在 <cwd> 開」，並且要使用者**再按一次確認**。

## 其他（Low）

| # | 情況 | 建議 |
|---|---|---|
| T8 | **空清單**：Agora 頁沒有任何 Session、未匯入頁沒有任何 session、篩選之後沒有結果 | 各自顯示一行說明（例如「還沒有 Session：按 Tab 到未匯入頁匯入」「沒有符合『表格』的 Session」）；這時 `Enter`／`m`／`e`／`d` 什麼都不做，只顯示提示 |
| T9 | **短 id**：範例 `01M3XB78`、`01M3XSSQ`、`01M3XED5` 都是 ULID 的前 8 碼，也就是**時間**的部分（前 10 碼是毫秒時間戳），所以同一個月的 Session 都以 `01M3X` 開頭，同一秒內建立的（例如批次匯入）前 8 碼會**完全一樣** | 改成顯示 ULID 的**最後** 8 碼（隨機的部分），或者顯示「日期」加上最後 4 碼。動作一律用完整的 id，所以這只是顯示上的問題，但很容易讓人看錯 |
| T10 | **批次匯入**：勾選了 N 個 | 一個一個匯入，某一個失敗時繼續下一個，最後顯示「成功 k 個、失敗 N−k 個」以及原因。`cmd_import` 每次都會做一次不節流的 sync（S6），N 次會很慢，建議批次匯入時只在開始前同步一次 |
| T11 | **動作失敗或 Ctrl-C 之後回到選單** | 和指令模式的 `main()` 一樣接住 `InputError`／`HeaderError`／`StoreError`／`AgentError`／其他例外，印出訊息（以及 exit code 的意義），然後「按任意鍵回到選單」。merge 寫要約時可能要等好幾分鐘，這時按 Ctrl-C 應該取消這一次的動作，回到選單，而不是結束整個 TUI（merge 要到最後才存檔，所以取消不會留下半份） |
| T12 | **預覽**：最後一則對話可能很長，也可能包含控制字元或 ANSI 跳脫序列（例如貼上的終端機輸出） | 預覽只取最後 N 行（依預覽區的高度），把 `\x00-\x1f`（`\n` 和 `\t` 除外）和 `\x7f` 換成可見的符號；Claude 要套用 `_is_noise`（不顯示本機指令和 isMeta 行），opencode 要略過 `synthetic` 的 part |
| T13 | **篩選失敗**：例如 `text=表格`（全文只能用 `~=`）會丟出 HeaderError | 在狀態列顯示錯誤，保留輸入的內容，讓使用者修改；不要 crash，也不要清空輸入。未匯入頁的篩選要比對什麼（標題、目錄、agent？），設計沒有寫，要補上 |
| T14 | **已經匯入、但後來有更新**：使用者匯入之後，又直接在 agent 裡繼續聊，這個 session 因為 `by_source` 找得到，所以**不會**出現在未匯入頁上，在 TUI 裡就沒辦法重新匯入它 | 未匯入頁再列出「已匯入，但 agent 那邊比較新」（`Listed.updated_at` 晚於 agora 的 `agora.updated_at`）的 session，並且標示 `↻`，按 Enter 就走 5.2 的重新匯入流程 |
| T15 | **`last_message`／`list_sessions` 的契約**（base.py） | docstring 補上：(a) `last_message` 回傳的 text 最多 N 字（例如 2,000 字），轉接器只讀檔案的**結尾**（Claude 的 jsonl 從後面往前讀），不要整個檔案讀進來；(b) `list_sessions` 不能解析每一個檔案的全文（Claude 一個專案裡可能有幾百 MB 的 jsonl），標題只讀開頭的幾行，並且依 `(路徑, mtime, size)` 做快取；(c) 兩個方法都要遵守 `config_dir()`／`XDG_DATA_HOME`，不能寫死家目錄；(d) 讀不到時回傳 None 或空清單，不要丟出例外 |

## 和既有設計的對照

- 「互動模式不新增功能、所有動作都呼叫同一套程式」：這樣 S／N／C／R 的各項安全機制（pending 鎖、`pass_fds`、SIGINT、PWD、outbox）都會自動沿用 ✅。只要注意，接續之前一定要先 `curses.endwin()`，把終端機還原成正常模式，再交給 agent；agent 結束之後，要重新初始化畫面（`stdscr.clear()`／`curses.doupdate()`），並且重新讀取視窗大小，因為 agent 執行期間使用者可能改過視窗大小。
- 刪除只作用在單一一個，不做多選 ✅，和 5.6「有子 Session 就拒絕」一致。
- 合併「至少 2 個、彈出視窗選誰寫要約」和第 7 版 5.3 一致 ✅；彈出的視窗還應該提醒「每個來源會叫一次 AI，材料會送到該 agent 的模型供應商」（v6 Y4）。

## 這次跑過的指令

| 指令 | 結果（只記形狀） |
|---|---|
| `git show --stat 1804d8e f9fd72d`、讀 design 5.9 全文、`grep` base.py 的 `Listed`／`list_sessions`／`last_message` | 見上面 |
| `opencode session list --help` | 只有 `-n/--max-count`、`--format`，沒有「所有專案」的選項（T2） |

沒有跑整合測試，沒有碰 Drive，沒有叫任何 agent 執行任務，也沒有讀任何真實的 Session，也沒有在沒有 commit 的資料夾裡跑 `opencode session list`。

---

## 實作確認（`70a5a9e`、`3de46ef`、claude `31c362c`＋`051e5c6`、opencode `21f0aa3`＋`3ca81fb`）

依照指示，沒有跑整合測試、沒有碰 Drive、沒有讀任何真實的 Session，也**沒有執行不帶參數的 `agora`**。我讀了程式和 design 5.9，用 scratchpad 裡自編的 Claude 檔案（`AGORA_CLAUDE_HOME` 指到 scratchpad）實測了例外處理，並且跑了單元測試：**266 passed**。

**結論：T1～T13、T15 都落實了。T6 和篩選這兩項是刻意改了做法，design 裡也寫了理由，可以接受。只剩一個 Medium（U1：Claude 的 `list_sessions` 遇到格式怪的檔案會丟出例外，讓 TUI 在啟動時就 crash，已實測），以及幾個 Low。**

### 兩個轉接器的 list_sessions／last_message：效能、唯讀、例外

| | opencode（`21f0aa3`＋`3ca81fb`） | claude（`31c362c`＋`051e5c6`） |
|---|---|---|
| 唯讀 | ✅ `sqlite3.connect("file:…?mode=ro", uri=True)`；檔案不存在就回傳空的 | ✅ 只用 `open()` 讀取；也略過 symlink |
| 位置 | ✅ `$XDG_DATA_HOME/opencode/opencode.db`，預設是 `~/.local/share`（conftest 會刪掉 `XDG_*`，HOME 是 tmp，所以單元測試是隔離的） | ✅ `config_dir()`（`AGORA_CLAUDE_HOME` > `CLAUDE_CONFIG_DIR` > `~/.claude`）；刻意**不**理 XDG，因為 Claude Code 本身也不理 |
| 效能：清單 | ✅ 只讀 `session` 表的 4 個欄位（不讀任何對話內容）；用 `(路徑, mtime, size, WAL 的 mtime 和 size)` 做快取（`3ca81fb` 修正了「新資料還在 -wal 裡、主檔沒有變」的情況） | ✅ 只看 `projects/*/*.jsonl`（不含 subagents）；每個檔案只讀開頭的 40 行來取標題與 cwd，而且先用正規表示式篩過，只有少數幾行才真的 `json.loads`；用 `(路徑, mtime, size)` 做快取，沒再出現的 key 會被清掉，不會無限增長 |
| 效能：預覽 | ✅ 最多 20 則訊息，取每則的 parts，最多 2,000 字 | ✅ 只讀檔案**最後 1 MB**（`TAIL_BYTES`），由後往前找，略過雜訊行和寫到一半的最後一行，回傳最後 2,000 字 |
| 例外 | ✅ `sqlite3.Error`、JSON 解析失敗，都會回傳空的或 None，並且只警告一次「資料庫結構認不出來」 | ⚠️ **U1**（見下） |

### U1（Medium，已實測）：Claude 的 list_sessions／last_message 還是會丟出例外

用自編的檔案（`AGORA_CLAUDE_HOME=<scratchpad>`）實測：

| 檔案內容 | 結果 |
|---|---|
| 某一行的 `"cwd":"bad\x escape"`（JSON 裡不合法的跳脫序列） | `list_sessions()` 丟出 **`JSONDecodeError`**。`_json_str(_CWD_RE.search(line))` 會 `json.loads` 正規表示式抓到的字串，但 `_peek_session` 只接了 `OSError` |
| 某一行是 JSON 陣列（`["type","user"]`），不是物件 | `last_message()` 丟出 **`AttributeError: 'list' object has no attribute 'get'`**（`_is_noise(o)`／`o.get(...)`） |

- `last_message` 在 TUI 裡被 `import_preview` 的 `except Exception` 接住了，**不會**讓畫面 crash ✅。
- 但是 **`list_sessions` 沒有被包住**：`tui.import_rows` 會直接呼叫它，而 `cli.main` 是在 `try` 區塊**之前**就 `return tui.main(...)`，所以只要使用者本機上有**任何一個** jsonl 的開頭 40 行裡有這種行，打開 `agora` 時就會直接印出 traceback，互動模式完全沒辦法用，直到那個檔案被修好為止（而那是 Claude 自己的檔案，使用者通常不會去碰）。
- **建議**：(a) `_peek_session` 改成接 `(OSError, ValueError, TypeError, AttributeError)`，遇到就略過那個檔案；`last_message` 的迴圈在 `json.loads` 之後，`if not isinstance(o, dict): continue`。(b) `tui.import_rows` 呼叫每一個轉接器的 `list_sessions()` 時，都包一層 `try/except Exception`，失敗就回傳空的，並且在狀態列顯示「<agent> 的 session 清單讀不到」。(c) 單元測試加上這兩個檔案（不合法的跳脫、非物件的行）。

### T1～T15 逐條確認

| # | 狀態 | 確認的內容 |
|---|---|---|
| T1 | ✅ | `cli.main([])`：stdin **或** stdout 不是 TTY 時，只印出用法，exit 2，**不會** import tui，也不會列出任何東西；有單元測試（pytest 的 stdin 不是 TTY）。design 5.9 也寫了。整合測試的守衛（「整合測試不准呼叫 `list_sessions`，也不准不帶參數呼叫 `main`」）還沒有看到，見 U5 |
| T2 | ✅ | 讀 opencode 的 SQLite，唯讀、尊重 `XDG_DATA_HOME`、遇到不認得的 schema 就回傳空清單並警告；spike/opencode.md 也記錄了（`21f0aa3`）。**完全沒有**用到「在沒有 commit 的資料夾裡跑 `session list`」那個怪行為 |
| T3 | ✅ | `locale.setlocale(LC_ALL, "")`；`display_width` 用的是 `east_asian_width` 的 W／F；截斷和對齊都依照顯示寬度（`test_columns_line_up_by_display_width`）；輸入用 `get_wch()`；寫到右下角那一格時，會接住 `curses.error` |
| T4 | ✅ | 小於 40×10 時，只顯示「終端機太小」；`KEY_RESIZE` 會重畫 |
| T5 | ✅ | 接續之後「沒有新內容」時，會把 `agent:session_id` 記到 `<state>/unsaved-launches`，未匯入頁會略過這些；`dir` 在 `<state>` 底下的（summarize）也不會列出來（`test_import_tab_leaves_out_agoras_own_copies`）。做法是「記下來、不列出」，不是「刪掉」，也可以接受，因為不會多刪任何東西 |
| T6 | ✅（改了做法） | 打開時做一次**節流**的同步，同步完才進全螢幕，**沒有**改成背景同步。design 寫了理由（同步和動作會同時寫同一份索引和鏡像），可以接受。每個動作結束之後，只重新讀本機的索引 ✅ |
| T7 | ✅ | `ask_dir`：預設是來源的 `dir`（在這台機器上存在的話），否則是目前目錄，並且顯示「⚠ 來源沒有記錄目錄…會在目前目錄開」；**一定可以選「改目錄…」**，輸入的目錄不存在時會提示 |
| T8 | ✅ | 清單是空的時候，顯示「（沒有東西；按 / 改篩選，或按 Tab 換頁）」；合併少於 2 個時，有提示 |
| T9 | ✅ | 短 id 改成 ULID 的**最後 8 碼**（隨機的部分）；未匯入頁顯示 session id 的最後 12 碼 |
| T10 | ✅（有一個 Low） | 勾選多個匯入時，會一個一個執行 `cli.main(argv)`，失敗也會繼續下一個，最後顯示「成功 k 個、失敗 m 個」。每一次匯入仍然會各自做一次不節流的 sync（U4） |
| T11 | ✅ | 每個動作都透過 `cli.main` 執行，例外都會變成 exit code，不會讓 TUI 結束；Ctrl-C 也會被 `cli.main` 的 `except KeyboardInterrupt` 接住（回傳 130），merge 這時還沒存檔，所以不會留下半份。接下來是「按 Enter 回到選單」 |
| T12 | ✅ | 預覽裡不是 `isprintable()` 的字元都會顯示成 `·`；兩個轉接器都把長度限制在 2,000 字；Claude 會略過雜訊行，opencode 會略過 `synthetic` |
| T13 | ✅（改了做法） | `/` 篩選改成「清單上看得到的文字（id、agent、標題、目錄）；用空白分開的每一個字都要出現」，所以不會有語法錯誤，也就不會出現 HeaderError。design 已經改成這種寫法。代價是**沒辦法搜尋對話的內容**，要搜尋內容，請用指令模式的 `--filter text~=` |
| T14 | ❌ 沒有做 | 「已經匯入、但 agent 那邊有更新」的 session，在 TUI 裡看不到，沒辦法重新匯入；只能用指令模式的 `agora import session …`（Low，不影響正確性） |
| T15 | ✅（除了 U1） | 見上面的表 |

### 其他（Low）

| # | 問題 | 建議 |
|---|---|---|
| U2 | opencode 的 URI 是直接把路徑接成 `file:{path}?mode=ro`。如果路徑裡有 `?`、`#`、`%`，SQLite 會把它解析錯 | 改成 `f"{path.as_uri()}?mode=ro"`（`as_uri` 會做百分比編碼） |
| U3 | opencode 的 `last_message` 回傳的是那則訊息的**最後一個** text part，不是整則訊息的內容（一則長的回覆可能分成好幾個 part） | 預覽本來就只是用來辨認的，可以接受；或者依順序把那則訊息的所有 text part 接起來，再截到 2,000 字 |
| U4 | 批次匯入 N 個時，`cmd_import` 每次都會做一次不節流的 sync | 批次匯入之前先同步一次，再用一個環境變數或參數，讓之後的 N 次 import 跳過 sync |
| U5 | 整合測試的守衛：「整合測試不准呼叫 `list_sessions`，也不准不帶參數呼叫 `main`」。這條規則還沒有被測試鎖住（e2e 會把 `AGORA_CLAUDE_HOME` 設成真的家目錄） | 在 `tests/integration/` 加一個守衛測試，掃描原始碼，確認沒有 `list_sessions(`／`main([])`／`last_message(` |
| U6 | `<state>/unsaved-launches` 只會一直追加，從來不會被清理 | 讀取的時候，順便把「agent 那邊已經不存在的 id」清掉，或者只保留最近 N 筆 |
| U7 | `display_width` 只看 `east_asian_width`，把組合字元（例如重音符號）也算成 1 欄 | 加一行 `unicodedata.combining(c)` 算 0；中文和日文不受影響 |
| U8 | curses 執行期間，stderr 被導到 `io.StringIO()`，之後就**丟掉**了，所以 `_warn_schema` 這類警告，使用者完全看不到 | 回到選單時，如果 `noise` 不是空的，就把它的第一行顯示在狀態列上 |

### 這次跑過的指令

| 指令 | 結果（只記形狀） |
|---|---|
| `git log c730987..HEAD`，對 6 個 commit 跑 `git show --stat`，讀 `opencode.py`／`claude.py` 的 `list_sessions`／`last_message` 與相關的輔助函式、`tui.py` 的 `agora_rows`／`import_rows`／`import_preview`／`ask_dir`／`main`、`cli.main` 的 TTY 檢查和 `KeyboardInterrupt` | 見上面 |
| 在 scratchpad 建 `AGORA_CLAUDE_HOME=<scratchpad>/.claude/projects/-tmp-x/` 和兩個自編的 jsonl（不合法的跳脫、非物件的行），再呼叫 `C.ADAPTER.list_sessions()`／`last_message()` | U1 的實測結果；之後已經刪掉 |
| 讀 design 5.9（篩選、TTY、同步、unsaved-launches 的說明） | T6、T13 的做法改了，design 也已經同步 |
| `.venv/bin/python -m pytest -q tests/unit` | 266 passed |

沒有跑整合測試，沒有碰 Drive，沒有讀任何真實的 Session，也沒有執行不帶參數的 `agora`。
