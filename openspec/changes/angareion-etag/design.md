## Context

MyAiStorage#96（來源：MyAiStorage#93／M0，本人 2026-10-18 裁定「記住上次查到哪」）。M0 的量測與推估：單次 `read` 是每個註冊 channel 一個完整查詢；`wait` 每次**重新開始**時對每個 channel 的第一個查詢是完整請求（計額度），之後帶 ETag 的輪詢是 304（量到 45 次中 39 次 `x-ratelimit-used` 沒增加；有外部雜訊，所以驗收看 200 的請求數，不看 rate-limit 標頭）。期 2 規模下重新開始 `wait` 的閒置成本：`--timeout 100` 約主額度 52%、`--timeout 600` 約 8.6%，保留 ETag 後推估接近 0。

現況（`src/angareion/`）：

- `github.py` 的 `check`：`_check_etags: dict[repo#issue, str|None]` 只在**單一行程的記憶體**；查詢 URL 由 `_listing_url(entity, since=cursor_since(cursor))` 組出，`since` 是游標 `at − 60 秒`（`cursor_since`）；行程內另有 `_scan_since` 把視窗往前推（不重掃）。回應有下一頁時不記 ETag（它只代表第 1 頁，M1）；304 直接回「沒有新訊息」。
- `client.py` 的 `check`／`wait`：**不寫游標**；每次 check 都重新讀游標檔；`wait` 在 `check` 上輪詢（15 秒），`--timeout 0` 只查一次，不給逾時就無限等。
- `state.py` 的 `CursorStore`：`cursor-<agent 名>.json`，每個目標一筆 `{position, at, seen}`，整檔原子替換（`os.replace`）；沒有其他本機狀態。`docs/angareion.md` 目前明說 ETag 與 `scan_since` 只在記憶體、不寫進游標檔（「`wait` 不動游標、之後的 `read` 不會被 304 跳過」）。
- `cli.py`：`GitHubIssuesBackend(active_store, token=…, transport=…)` 沒有狀態目錄；`read`／`wait` 的 `--channel` 目前沒有任何警告。

約束：`wait` MUST NOT 動游標；`read` MUST NOT 被 304 跳過；對 AI 的用法（參數、stdout、exit code）不變；測試只用假後端／假 transport，不連網；本 change 不 commit。

## Goals / Non-Goals

**Goals：**

- 重新開始 `wait`（同一個身分、同一份游標、沒有新訊息）時，對每個 channel 的第一個查詢就是條件式查詢；計額度的 200 接近 0。
- 任何「狀態檔壞掉／對不上」的情況都退回完整查詢，不掉訊息；找不到可重用記錄時 304 不可能被誤判。
- `wait` 的這個記憶與游標、`read` 完全分開：游標檔格式不變，`read` 不讀也不寫它。
- `read`／`wait --channel` 的警告清楚可見，但 stdout 與 exit code 不變。
- 預設逾時重新評估後有明確結論（不必改）。

**Non-Goals：**

- 不改游標檔的格式、位置與語意，也不把 ETag 放進游標檔。
- 不改 `read`（它要的是訊息本體，仍然完整查詢、翻所有頁）；不改 `send`／`channel`。
- 不把 `scan_since`（掃描視窗）寫進狀態檔：它依賴「這個行程已經掃過」的前提，跨行程沿用會讓複寫延遲（較舊時間的留言晚到）更容易被跳過。狀態檔只記 ETag／URL／游標。
- 不把變窄的掃描視窗當成下一次重啟的查詢視窗（跨行程沿用會藏住複寫延遲的留言）；重用只發生在游標視窗的記錄，或後端自己說「兩份表示相同」時。
- 不改預設逾時、不加新 exit code、不改 `ChannelBackend` 的 `check` 簽名。
- 不碰真的 GitHub（實測驗收由 PM 做）；不改 `experiments/m0/`。
- 不處理兩個 `wait` 共用同一個身分的既有語意（它們本來就共用游標）；這次只保證狀態檔不被寫壞、最壞多一次完整查詢。

## Decisions

### 1. 記什麼、記在哪：獨立的 `wait-<agent 名>.json`

- **位置**：`<state_dir>/wait-<identity_slug>.json`（`ANGAREION_STATE_DIR` 照舊可覆寫；與 `cursor-<agent 名>.json` 同目錄）。檔名含身分：換 identity 或換狀態目錄就是另一個檔，互不影響。
- **格式**（版本 1，UTF-8、2 格縮排、檔尾換行）：

  ```json
  {
    "version": 1,
    "targets": {
      "owner/repo#10|*": {
        "url": "https://api.github.com/repos/owner/repo/issues/10/comments?per_page=100&since=2026-10-12T03%3A00%3A00Z",
        "cursor": {"position": "104", "at": "2026-10-12T03:00:05Z", "seen": ["102", "103", "104"]},
        "etag": "W/\"…\""
      },
      "owner/repo#10|MyLinuxPool-impl-2": { "…": "同上，寄給我的那份" }
    }
  }
  ```

- **鍵**：`<repo>#<issue>|<收件模式>`，收件模式是 `*`（`--channel`：整個 channel）或身分名（「寄給我的」）。同一個實體在兩種目標下的「沒有新訊息」不是同一句話（「寄給我的」只看 `to` 是自己或廣播的留言），所以分開記。
- **記錄三欄**：`url` 是**當時實際請求的完整 URL**（含 `per_page=100` 與 `since` 的百分號編碼）；`cursor` 是當時用的游標完整值；`etag` 是後端回傳的 ETag 原字串（不解析強弱）。
- **舊檔相容**：這是新檔，不存在就是「沒有任何記錄」→ 完整查詢。游標檔格式與舊游標檔都不動；舊游標（甚至舊格式被丟棄的游標）只會讓記錄對不上 → 一次完整查詢。
- **為什麼不寫進游標檔**：(a) `read` 的 `commit` 是「讀整檔、更新幾筆、整檔重寫」，把 wait 的快取混進去會被讀寫路徑互相牽動（要改 schema、保留未知鍵），也讓「`wait` 不動游標檔」不再是由結構保證；(b) 游標檔被刪掉代表「從頭讀」，而記錄必須跟著失效——分成兩個檔時，用「游標指紋」比對就自動失效，不必讓 `read` 去清 wait 的檔；(c) 既有測試釘住游標檔只能有 `position`／`at`／`seen`。**兩個檔都只是本機快取：刪掉 `wait-…json` 無害（下次完整查詢），它不含任何訊息內容。**
- **寫入**：`put`／`drop` 是「讀整檔 → 改一筆 → 唯一暫存檔 → `os.replace`」（照 `CursorStore` 的 L6 做法）。檔案不存在時 `drop` 不建檔。`put` 內容與現有記錄相同就不重寫（304 的穩定狀態不會造成磁碟 churn）。**狀態檔寫不進去（唯讀、磁碟滿）時吞掉錯誤**：它只是最佳化，不能讓 `wait` 從 exit 3／0 變成 exit 2；同一行程第一次失敗時在 stderr 印一次 `Warning: could not write the wait state file …`（之後不再重複）。
- 讀取失敗（不是 JSON、不是物件、`version` 不是 1、`targets` 不是物件、單筆缺欄位／型別不對）→ 當成沒有記錄（整檔或該筆）。載入失敗時視同「空檔但需要重寫」：下一次 `put`／`drop` 一定整檔重寫成合法內容，不留壞檔（即使該次 `drop` 的鍵不在讀到的內容裡）。

### 2. 什麼時候可以重用 ETag：游標 ＋ 收件模式；URL 是「哪一個視窗」的記錄

- 記錄裡的 `url` 是「當時實際查詢的視窗」（含 `since`），用來診斷，也讓同一個視窗直接重用；**重用的判準是游標（`position`／`at`／`seen`）與收件模式相同**，不要求 URL 相同。理由：ETag 是「那份回應本體」的驗證器；304 代表後端說「你現在要的這份表示，與我手上這個驗證器代表的那份相同」。那份已經完整掃過、對這個游標判定乾淨，現在這份相同的表示也一定乾淨；表示不同就是 200、走完整查詢，不會漏。
- **游標前進後 ETag 還有沒有意義**：`read` 一動游標，游標指紋就不同 → 記錄不重用，該 channel 做一次完整查詢（這是 M0 工作成本公式裡已算進去的部分，每則訊息約 10 個計額度請求）；閒置時游標不動，記錄一直有效。
- **掃描視窗漂移（REVIEW-96 M1）**：行程內 `scan_since` 可能因「較小的 id、較晚的後端時刻」這種亂序留言被推到 cursor 的視窗之後。修法是：**只要有一筆以 cursor 視窗查過的乾淨記錄，就優先查那個視窗**（不採用更窄的 `scan_since`），所以重啟後第一次查詢就是 304；只有沒有 cursor 視窗記錄時才用 `scan_since`。這樣也**不會把變窄的掃描視窗帶過重啟**——那正是原設計拒絕持久化 `scan_since` 的理由（可能藏住複寫延遲的留言）。若記錄是更窄的視窗（例如先前多頁、後來才變單頁），重啟時仍以 cursor 視窗查，只在後端說表示相同時 304，否則一次完整查詢；安全優先。
- **多個 channel**：鍵含實體（`repo#issue`），每個 channel 各自一筆；一個 channel 的 ETag 不會被另一個用到。
- **兩個目標共用實體**：`wait --channel general`（`channel:general` 游標）與不給 `--channel` 的 `wait`（`to:<我>:general` 游標）是不同模式、不同鍵；即使游標值碰巧相同也不會互相重用較弱的「乾淨」判定。

### 3. 為什麼不會因 304 而漏訊息

不變量（全部成立才會出現「304 ⇒ 沒有新訊息」）：

1. **只有送過 `If-None-Match` 才解讀 304**：ETag 來自一筆記錄，而記錄的游標與收件模式必須與這次請求相同；記錄的 URL 可能是別的掃描視窗，但 304 只會在後端說兩份表示相同時發生，而那份表示已經完整掃過、對同一個游標乾淨。
2. **記錄只在「完整掃過、而且沒有新訊息」之後才寫**：回應是 200、沒有下一頁、第 1 頁沒有滿（回傳筆數 < `per_page`）、整份回應都掃完且 `found=False`，而且後端有給 ETag。記錄的意思是「這個 URL 的回應本體，我從頭到尾看過，對這個游標沒有新訊息」。
3. **304 的語意**：後端說「你要的表示與我手上這個驗證器代表的那份逐位元相同」。同一份表示、同一個游標、上次已判定乾淨 ⇒ 現在也乾淨。游標只會由 `read` 往前推、不會自己後退；游標檔被刪掉（從頭讀）→ 游標值不同 → 不重用（見第 4 點）。
4. **游標一變就失效**：游標前進、被刪、被換掉，記錄的游標指紋就對不上；`read` 也不碰這個檔，所以不會出現「`read` 之後 `wait` 拿舊 ETag 跳過」。
5. **找到新訊息就清掉該筆**：`wait` exit 0 的那次回應含新訊息，**不記**它的 ETag，並把該實體既有的記錄刪掉。所以「`wait` 叫過、還沒 `read`」的訊息，下一次 `wait` 一定用完整查詢再叫一次（也順便把現行行程內「找到後再查同一個實體可能被自己的 ETag 回 304」的潛在問題修掉）。
6. **多頁與整頁滿不用 ETag**：ETag 只代表第 1 頁。除了現行「有下一頁就不記」，再加「第 1 頁剛好滿 `per_page`（即使沒有 `Link`）也不記」。理由是一個真的會凍住的邊界：視窗裡剛好 100 則（單頁、無 `Link`）→ 記下 ETag；第 101 則到達後，第 1 頁仍是那 100 則、不變 → 304；之後新留言全落在第 2 頁以後，而第 1 頁永遠不變 → 這個 ETag 會讓 `wait` 永遠說沒有新訊息，直到有人 `read`。滿頁不記就沒有這個狀態（超過 100 則本來就有下一頁、也不記）。
7. **壞檔不是 304**：讀不到、版本不認得、欄位不對、游標／模式不符，一律當成沒有記錄（不送 `If-None-Match`）；後端若在沒送條件式查詢時回 304，照現行行為當「沒有新訊息」，但這次不會寫入任何東西。

### 4. 壞檔與兩個行程同時 wait

- **壞檔**：整檔解析失敗 → 當空；單筆格式不對 → 跳過該筆；版本不是 1 → 當空（不猜測未來格式）；下一次寫入覆寫成合法內容。不會因為壞檔回 304 或 exit 2。
- **競態**：沒有鎖。兩個 `wait`（同身分、同狀態目錄）各自「讀整檔 → 改自己那筆 → 原子替換」；最後寫的人贏，可能少掉對方剛記的一筆 → 下一次那個實體用完整查詢，只多花一次請求。整檔替換保證不會讀到半截 JSON。**不會因此出現假 304**：能用的記錄一定是 URL／游標／模式完全相同的，而這種記錄的「乾淨」是當初真的掃過的結論。
- 兩個 `wait` 本來就共用同一個游標檔與身分；這個 change 不新增或改變那個語意。
- **刪掉游標檔**（從頭讀）：記錄的游標指紋對不上 → 完整查詢 → 重新建立記錄。不需要清 `wait-…json`。

### 5. `read`／`wait --channel` 的警告

- 文字（stderr，一行；與其他訊息一樣用英文）：

  `Warning: --channel reads every message in the channel, including messages addressed to other agents; omit --channel to get only messages addressed to you.`

- 位置：`cmd_read`／`cmd_wait` 先 `ensure_fresh` 並查一次 channel（`store.entity_for`；不存在就是 exit 1、**不印警告**），之後才印警告、才開始等待（REVIEW-96 L2）。只在有 `--channel` 時印（`--channel`＋`--since` 也印）。
- **stdout、exit code 完全不變**；警告不影響 `--json` 輸出。`--channel` 的說明文字（`-h`）同步補一句同義的話。
- 理由：M0 §3.1 的實際事故——審查員用 `read --channel` 讀到寄給 impl 的指示並照做。收訊規則（不給 `--channel`）要寫進 `SKILL.md`，警告是工具層的提醒。

### 6. 預設逾時：維持「不給 `--timeout` 就一直等」

- **結論：不改預設值。** 理由：
  1. `wait` 的用途就是阻塞等待；規格已經有「不給逾時就一直等」的情境。改預設會讓正常的「這段時間沒有人寄訊息」變成 exit 3，改變既有呼叫端與 harness 的契約，卻沒有換到什麼。
  2. 保留 ETag 之後，閒置的 `wait`（含逾時後重新開始）計額度請求接近 0；M0 當初用「拉長逾時」換額度的理由消失——**限時變成單純的 harness 行為**（工具單次呼叫上限），不是額度問題。
  3. M0 的建議本來就是「agent 的 harness 用 `--timeout 600`」；`SKILL.md` 已經這樣寫，這次補一句「重新開始 `wait` 很便宜」，不必動 CLI。
- 什麼情況會再評估：觀察到 agent 開著無限 `wait` 卡住自己的工作、或後端把長連線本身算成本（目前沒有）。
- `wait --timeout N` 的既有限時行為（每 15 秒查一次、網路預算 min(30s, max(5s, 剩餘))）不變。

### 7. 程式接縫

- `backend.py`：`ETagRecord(url: str, cursor: Cursor, etag: str)`（`NamedTuple`）、`ETagCache` Protocol（`get(key) -> ETagRecord | None`、`put(key, record)`、`drop(key)`）、`MemoryETagCache`（預設；維持沒有狀態目錄時的行程內行為）。`ChannelBackend.check` 的簽名不變。
- `state.py`：`wait_state_file_path(state_dir, identity)` → `wait-<slug>.json`；`FileETagCache(state_dir=…, identity=… | path=…)` 實作同一組三個方法（版本、原子寫入、壞檔當空、寫入失敗吞掉）。
- `github.py`：`GitHubIssuesBackend(..., etag_cache: ETagCache | None = None)`，預設 `MemoryETagCache()`；`check` 用 `_etag_key(entity, recipient)`（`repo#issue|*` 或 `repo#issue|<身分>`）查／存／清。`_check_etags` 移除。`scan_since` 留在記憶體。
- `cli.py`：建 GitHub 後端時傳 `FileETagCache(config.state_dir, config.identity)`；`read`／`wait` 的警告用同一個常數。
- `read`、`CursorStore` 不動；`client.py` 只改 `check()` 的 docstring（ETag 不再只在行程內，由 CLI 注入的檔案 cache 跨行程保存；REVIEW-96 L1）。

### 8. 測試與改壞驗證

測試先行（`tests/unit/test_angareion_etag.py`，全部假 transport／假後端、不連網；CLI 警告在 `test_angareion_cli.py`）：

- 重啟：backend A 完整查一次（乾淨）→ backend B（新的實例、同一個狀態檔）第一次查詢就帶 `If-None-Match`（值等於 A 存下的），回 304 → `False`。連續模擬 10 次重啟：只有第一次是 200。
- ETag 失效：回應變了 → 200 → 找到新訊息 → `True`，記錄被清掉；下一次（新實例）不帶舊 ETag、用完整查詢仍 `True`。
- 狀態檔不存在／壞 JSON／版本不認得／記錄游標不符 → 不帶 `If-None-Match`、完整查詢、新訊息照樣找到；壞檔之後被改寫成合法內容。記錄的 URL 是別的查詢視窗、但游標相同時仍然重用（304 才算相同表示；表示不同就 200）。
- 多個 channel：各自一筆；查 A 帶 A 的 ETag、不帶 B 的。
- 換 identity／換狀態目錄：另一個檔、另一個身分不重用。
- 多頁（有 `Link`）→ 不留 ETag（新實例不帶）；（加嚴）第 1 頁剛好 `per_page` 筆、無 `Link` → 也不留。
- `read` 之後：游標前進 → 記錄不重用 → 完整查詢；`wait`／cache 不寫游標檔（內容不變）、`read` 不碰 `wait-…json`。
- 「找到但沒讀走」再 `wait`：仍 exit 0（不是 304）。
- M1（REVIEW-96）：cursor 停在 id=150 created 13:00、亂序的 id=120 created 13:01（另加 14:29 的漂移）：一場 wait 兩次 check、重啟 6 次，200 總數 1（修前 12）。
- 兩個 writer 交錯寫同一個狀態檔：每次寫完都是合法 JSON、last writer wins、掉記錄只會多一次完整查詢（新訊息仍找到）。
- 刪掉游標檔後 `wait`：不帶舊 ETag、完整查詢、新訊息仍找到。
- 狀態檔寫不進去（唯讀目錄）：`check` 照常回結果、不掉訊息、不 exit 2、stderr 恰好一行。
- 狀態檔本身：roundtrip、`drop` 不建檔、原子替換（寫入後檔案永遠是合法 JSON）、未知版本當空；新建狀態目錄 mode 0700。
- CLI：`read --channel`／`wait --channel` 在 stderr 有警告、stdout 與 exit code 不變；不加 `--channel` 沒有警告；channel 不存在時只有錯誤、沒有警告。
- 改壞驗證（`git archive` 副本＋本次改動的檔案；假資料）：(1) 拿掉讀狀態檔的 ETag（永遠不送 `If-None-Match`）→ 重啟測試紅；(2) 比對時忽略游標（只按實體鍵）→ 游標前進／游標不符的測試紅；(3) 壞檔直接當 304（或當成有記錄）→ 壞檔測試紅；(4) 警告不印 → CLI 警告測試紅；(5) 找到新訊息後仍記住那次回應的 ETag →「沒讀走的新訊息」測試紅；(6) 整頁滿仍記 ETag → 滿頁測試紅；(7) 拿掉 M1 的「cursor 視窗優先」並要求 URL 完全相同 → M1 漂移測試紅。

### 9. 驗收（PM 對真 GitHub 做）

照 M0 §4 的量法，對真的後端（測試 channel、自己的身分）：

1. 游標先就位（先 `read` 一次）；記下 `PYTHONPATH=<experiments/m0/count>`、`M0_COUNT_LOG=<新的 jsonl>`、`M0_AGENT=<名字>`。
2. 連續執行 `angareion wait --timeout 60` 10 次（其間不送訊息）。
3. 在 jsonl 裡數 `status == 200`（計額度的完整請求）與 `304`。預期：**第一次執行的每個 channel 各 1 個 200**（建立記錄），第 2～10 次重啟的第一次查詢就是 304；200 的總數 ≈ N_ch（不是 10 × N_ch，M0 的基準）。
4. 再送一則訊息：`wait` exit 0；`read` 之後再 `wait` → 該 channel 一次 200、之後回 304（工作成本，符合 M0 公式）。
5. 若某個 channel 的列表本來就多頁或剛好滿 100 筆，它會每次都是 200（設計如此，滿頁不留 ETag）；在驗收紀錄裡註明是哪個 channel。

## Risks / Trade-offs

- [狀態檔過期或被手改] → 記錄只在 URL／游標／模式完全相同時使用，且只可能是「當時真的掃過且乾淨」的那一筆；對不上就是完整查詢。手改檔案讓 ETag 對到別的表示是使用者自傷，文件會說它是快取、不要手改。
- [每次都完整查詢的情況變多] → 游標一動、多頁／滿頁、狀態檔不見，都會多一次完整查詢；閒置（沒有新訊息、游標不動）才是被壓到 0 的那條路。這是刻意的保守。
- [兩份本機狀態] → 游標檔與 wait 狀態檔可能不同步（刪了一個、留了另一個）：只會多一次完整查詢，不會漏訊息；文件寫明「刪掉任一都安全（游標從頭讀／ETag 重建）」。
- [304 的語意依賴後端] → 我們只依賴「304 ⇒ 同一份表示」這個 HTTP 契約（也是現行行程內 ETag 已經在依賴的）；沒有依賴 GitHub ETag 的強弱或跨頁行為。
- [滿頁規則降低忙碌 channel 的節流] → 視窗裡 ≥100 則留言的 channel 不記 ETag（本來 >100 也因多頁不記）；這種 channel 通常正在忙、`wait` 會很快 exit 0，閒置成本不是問題。
- [write-through 失敗] → 吞掉寫入錯誤並在 stderr 提示一次（狀態檔只是快取）；代價是那種環境每次重啟都完整查詢，行為與今天相同，`wait` 的 exit code 不變。
- [與 `angareion-v5` 的 delta 衝突] → 兩個 change 都改 `確認有沒有新訊息`／`目標與游標`；v5 還沒歸檔，主 spec 還是 v0。delta 以 v5 的文字為底（＝現在的程式），套用與歸檔順序由 PM 決定（前例：`2026-10-18-agora-lock-ux` 手動套進主 spec、歸檔時「Specs already in sync」）。

## Open Questions

1. **歸檔順序**：`angareion-v5` 先歸檔、再把本 delta 套進主 spec，或反過來？delta 已按 v5 文字寫，兩種都可以，但主 spec 最後要同時有 v5 與本 change 的內容；時機留給 PM。
2. 要不要把「第 1 頁滿 `per_page` 就不留 ETag」變成 `wait` 的可觀察提示（例如 stderr 一次）：這次不做（沒有新介面，`wait` 的輸出契約不變）。
3. 狀態檔要不要在檔案裡記 `updated_at` 供人除錯：這次不做（多一個會過期的欄位，沒有用途）。
