# #413 設計：aicomm 的 poke body 帶 `via`／`ref`

- 單：MyAiStorage#413（d；期 6 M1「通訊看得見」）。配對：MyLinuxPool#786 poke-records-v1（sidecar 端已定稿並上線，PR #796）。
- 作者：設計作者隊員；介面決定來自架構師的 `ARCH-413-interface.md`（本文件是它的忠實整理，不新增介面）。核准：專案架構師（核准者模式）。
- 狀態：aicomm 側的決定已定；sidecar 端欄位名與格式依 MyLinuxPool `modules/sidecar/docs/sidecar.md` §2.2 與 `openspec/changes/sidecar-poke-records/design.md` D3，不需再確認。

## 0. 結論

- 層級 d，實作一個 PR。
- aicomm 每一次戳（本機與跨機）的 body 一律帶 `via`。訊息 id 合格時另外帶 `ref`；不合格就不帶 `ref`，照樣戳一次。
- 戳的**文字**（`text`）不變，仍然不含任何訊息欄位；只有 `ref` 這個中繼資料帶訊息 id。
- 不新增 wake 種類、原因碼、旗標或子指令。`wake:` 行一個字都不變，結束碼也不變。
- 主 spec 改 4 條 requirement（MODIFIED，見 §5）。
- 不擋本單、但要 MLP 回答：sidecar 的 `GET /pokes` 列表參數沒有 `ref`（D7：只有 `agent`、`since`／`until`、`result`、`source`、`via`、`before`、`limit`），見 §8 Q-MLP-1。

## 1. 現況（只讀，已核對）

- 本機 body 在 `src/aicomm/cli.py:628` 組：`{"text": WAKE_TEXT, "interrupt": bool(interrupt and state == "running")}`。跨機在 `cli.py:515` 用同樣的式子另外組一次，再於 `cli.py:519` 以 `json.dumps(payload)` 交給 `pool-peer call` 的 stdin。
- 訊息 id：`_cmd_send` 在 `cli.py:688` 呼叫 `_angareion(command, …)`，拿到 `out`，`cli.py:689` 把它原樣轉到 stdout。angareion 的 `send` 只印 `f"{message_id}\n"`（`src/angareion/cli.py:352`），id 是 ULID。所以**開始叫醒之前（`cli.py:693` 的迴圈），訊息 id 已經在 `out` 裡**。
- 收件人：`cli.py:657-660`，`--to` 加上 `--mention`，排除自己、去重；`--no-wake` 或沒有收件人時 `wake=False`，不發任何請求。
- 名稱撞到：`send` 已有一個 `--ref NAME=LOCATOR` 選項（`cli.py:180`），那是 Angareion 訊息上的參照，**和 poke body 的 `ref` 無關**。程式內部不要用 `ref` 當變數名（D-5），docs 要寫清楚兩者不同。
- `cli.py:62` 的註解寫「The poke carries this and nothing else: never the sender, the body, a ticket or an id.」，本單之後要改寫（D-6）。

## 2. 決定

### D-1 欄位與值

| 鍵 | 本機（`POST /agents/<id>/poke`） | 跨機（`pool-peer call <port> POST /agents/<id>/poke` 的 stdin） |
|---|---|---|
| `text` | `WAKE_TEXT`（不變） | 同左 |
| `interrupt` | 規則不變（`--interrupt` 而且那一台是 `running`） | 同左，每一台各自判斷 |
| `via` | `"aicomm"`（**一律帶**） | `"aicomm-peer"`（**一律帶**） |
| `ref` | 合格的訊息 id；不合格**不帶這個鍵** | 同左，同一個值 |

- 鍵的順序固定為 `text, interrupt, via, ref`。JSON 的語意不看順序，但固定下來，測試與 stdin 的逐字比對才穩。
- `ref` 不合格時**整個鍵都不出現**，不寫 `null` 也不寫 `""`。**被否決**：送 `null` 或 `""`——sidecar 的 regex 只對字串成立，送 `null` 會不會被當成格式錯誤，契約沒有寫，不能冒險。
- `via` 的兩個值寫成常數（建議 `POKE_VIA_LOCAL = "aicomm"`、`POKE_VIA_PEER = "aicomm-peer"`），而且都要完全符合 MLP 的 `^[a-z][a-z0-9-]{0,31}$`（C-8 用單元測試鎖住）。

### D-2 ref 的來源與驗證（一定要在 aicomm 端先檢查）

- 來源：angareion `send` 成功（exit 0）之後，取 `out` 的**第一行**（以 `"\n"` 切開取第 0 段），去掉頭尾的空白。
- 驗證：新增常數 `POKE_REF_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")`，用 `fullmatch` 檢查。**被否決**：`match` 加 `$`——`$` 會放過結尾的 `\n`（`re.match(r'^…$', 'a\n')` 已用 Python 驗過會放過）。
- 這個格式和 `RECORD_ID_RE` 一樣，但兩者分屬不同的外部契約（一個是 sidecar 回給我們的 id，一個是我們送給 sidecar 的 ref），以後可能各自改，所以**分開定義**。**被否決**：共用常數。
- 不合格的情況：`out` 是空的、第一行中間有空白、超過 64 字、有非 ASCII 或其他字元。這些情況都不帶 `ref`，其餘照常戳一次。**被否決（Q-2，PM 已接受不出聲）**：加 stderr 行、加原因碼；`wake:` 行仍然每個收件人恰好一行。
- 為什麼一定要先檢查：sidecar 收到格式不對的 `ref` 會回 400 `invalid_request`，而且**不送出**。**被否決**：不先檢查、直接把第一行送出——「angareion 的輸出格式變了」這種跟叫醒無關的事，就會讓每一次叫醒都變成 `not-woken http-400`。
- ref 只算一次（在叫醒迴圈之前），之後所有收件人、所有機器都用同一個值。stdout 的轉印（`_relay`）仍然原樣，不受影響。

### D-3 多收件人與 `--mention`

- `--to` 加上每一個 `--mention` 的收件人，**每一個戳都帶同一個 `ref`**（同一則訊息）。同一個收件人在好幾台機器上時，每一台的戳也都是同一個 `ref`。
- 戳的次數規則不變：同一次 `send`、同一個對象、同一台 sidecar，最多戳一次。
- `--no-wake`（以及沒有收件人、收件人只有自己）時，不發任何請求，所以也就不會帶出 ref；不需要另外寫邏輯。
- `--interrupt` 只影響 `interrupt`，不影響 `via`／`ref`。

### D-4 跨機（`pool-peer call` 的 stdin）

- 本機和跨機要用**同一個** body 組法，不能各自手寫 dict。抽出一個純函式：`_poke_body(interrupt: bool, via: str, message_id: str | None) -> dict`，回傳依 D-1 的順序排好的 dict。本機的 `_wake_one_inner` 與跨機的 `_poke_peer` 都呼叫它，只差在 `via` 傳的常數不同。
- stdin 仍然是 `json.dumps(body)`（預設的 separators 與 `ensure_ascii`，不變）。argv 不變（`call <port> POST /agents/<quoted>/poke --timeout 5`）。
- 讀回應的路徑（`_poke_answer`、`record=`、`queued`）一行都不改。

### D-5 呼叫鏈的簽名變更（只限 `cli.py` 內部，不是對外介面）

- 多一個參數 `message_id: str | None`（已經檢查過，`None` 代表不帶 `ref`），依序往下傳：`_cmd_send` → `_wake_one` → `_wake_one_inner` → `_wake_elsewhere` → `_poke_peer`。參數名用 `message_id` 或 `poke_ref`，**不要用 `ref`**（以免和 `args.ref` 混在一起）。
- 驗證放在一個小函式 `_poke_ref(out: str) -> str | None`（方便單元測試），由 `_cmd_send` 呼叫一次。下游拿到的值就當作已經合格，不再檢查第二次。
- `_wake_one` 的兜底（例外時回 `unknown internal-error`）不變。

### D-6 文件與註解

- `docs/aicomm.md:103`（對 sidecar 的依賴面表格）（body 欄現有說明）與 `:120`（`pool-peer call` 那一列，目前沒有 body 欄，要補一句：stdin 與本機同一個 body、含 `via: aicomm-peer`）的 body 改成 `{"text", "interrupt", "via", "ref"?}`，並加一段說明：`via` 是自己宣告的來源；`ref` 是這則訊息的 id，只在格式合格時才帶；這不是 `send --ref`；舊的 sidecar 會忽略這兩個鍵；查紀錄時可以用紀錄裡的 `ref` 對照訊息 id。
- `cli.py:62` 的註解改成：**文字**永遠是這個短句；body 另外只帶 `via` 和訊息 id 的 `ref`，絕不帶寄件者、內文或工單。
- `skills/aicomm/SKILL.md` **不需要改**：使用者看得到的行為沒有變。

## 3. 主 spec 的意圖（不變的那一半）

「戳的內容固定且不含訊息」原本要防的是：**收件人的輸入框**看到訊息內容或寄件者，以及 sidecar 的紀錄留下私人資訊。改了之後：

- `text` 仍然是固定短句，對方的輸入框看到的字一個都不變。
- `ref` 只是 sidecar 拿來做紀錄的中繼資料，不會貼進對方的輸入。（推論：sidecar.md §2.2 只說 `via`／`ref` 是拿來對照的，沒有寫「會送進 tui」。）
- 訊息 id（ULID）本身不是私人資訊：它已經印在寄件者的 stdout、Angareion 的紀錄裡，而且 Angareion 的紀錄本來就是公開的。

## 4. 契約（給實作，逐條可以用單元測試驗）

| # | 契約（輸入 → 輸出或錯誤） |
|---|---|
| C-1 | 本機 `idle`、angareion 印 `<ULID>\n`、沒帶 `--interrupt` → 假 sidecar 收到的 body 恰為 `{"text": WAKE_TEXT, "interrupt": false, "via": "aicomm", "ref": "<ULID>"}`；戳一次；`wake:` 行與現行一模一樣 |
| C-2 | 本機 404，`find` 回 5201 `idle`，`call` 回 200 → `call` 的 stdin 逐字等於 `json.dumps({"text": WAKE_TEXT, "interrupt": false, "via": "aicomm-peer", "ref": "<ULID>"})`；argv 不變 |
| C-3 | angareion 的 stdout 第一行不合格（樣本：`"has space"`、65 個 `a`、`""`、`"abcé"`、`"a.b"`）→ body 恰為 `{"text", "interrupt", "via"}`，**沒有 `ref` 這個鍵**；仍然戳恰好一次；`wake:` 行與結束碼和合格時相同 |
| C-4 | `_poke_ref` 純函式：`"<ULID>\r\n"`、`"  <ULID>  \n"`、`"<ULID>\nextra\n"` → `<ULID>`；整合測試（假 angareion 的 stdout 經 subprocess）只用 `"  <ULID>  \n"` 與 `"<ULID>\nextra\n"` → body 的 `ref` 是 `<ULID>`（只取第一行，去掉頭尾空白）。`\r` 樣本只放純函式測試：subprocess 的 universal newlines 會把 `\r\n` 轉成 `\n`，整合測試分辨不出對錯 |
| C-5 | `_poke_ref` 是純函式：64 字的合格 token 原樣回傳；65 字回 `None`；token 後面接著 strip 剝不掉的字元（例如 `\x00`）時回 `None`；實作要用 `fullmatch`，不要用 `match`＋`$` |
| C-6 | `--to A --mention B --mention C`，三人都在本機 `idle` → 三個戳的 `ref` 都是同一個 ULID，`via` 都是 `"aicomm"`；每人只戳一次 |
| C-7 | 同一個收件人跨機在 5201、5202 兩台（一台 `running`、一台 `idle`，帶 `--interrupt`）→ 兩台的 stdin 都有相同的 `via: "aicomm-peer"` 與 `ref`；`interrupt` 仍然各自是 true／false（取代現行 spec 該 scenario） |
| C-8 | `POKE_VIA_LOCAL`、`POKE_VIA_PEER` 都完全符合 `^[a-z][a-z0-9-]{0,31}$`；`POKE_REF_RE` 的樣式等於 `[A-Za-z0-9_-]{1,64}` |
| C-9 | `--no-wake`、沒有收件人、收件人只有自己 → 對 sidecar 與 `pool-peer` 都沒有任何請求（現行行為，回歸測試） |
| C-10 | angareion 失敗（exit ≠ 0）→ 不叫醒，也不計算 ref（現行行為，回歸測試） |
| C-11 | 戳的 `text` 仍然等於 `WAKE_TEXT`，而且 `text` 不含寄件者名、ULID 與內文；body 的鍵集合是 `{"text","interrupt","via"}` 或 `{"text","interrupt","via","ref"}`，不會有其他鍵（取代 `test_aicomm_send.py:387` 現行的 `{"text","interrupt"}`） |
| C-12 | `send --to <agent> --ref note=REF-SENTINEL-413` → poke body 序列化後不含 `note=` 也不含 `REF-SENTINEL-413`（poke 的 `ref` 只會是訊息 id；哨兵值刻意不用單字母，免得撞到 `WAKE_TEXT` 的固定短句） |
| C-13 | 本機與跨機的 body 由同一個函式產生：對同一組 `(interrupt, message_id)`，兩者只差在 `via` 的值（測 `_poke_body`） |
| C-14 | 假 sidecar 對帶 `via`／`ref` 的 body 回 200 並附 `poke_id` → `wake:` 行有 `record=local/<id>`（確認 #406 的路徑不受影響） |

測試層級：全部用單元測試，搭配現有的假 sidecar 與假 `pool-peer`，不連真的 sidecar。驗收門檻由 PM 定。

## 5. 對現有契約的影響清單

### 5.1 主 spec（`openspec/specs/comm-skill/spec.md`）MODIFIED 四條

標題與主 spec 逐字一致；MODIFIED 貼整段 requirement（openspec 的規則）。

| Requirement | 改什麼 | 契約 |
|---|---|---|
| `戳的內容固定且不含訊息` | 本文：body MUST 只有 `text`、`interrupt`、`via`、選用 `ref`；`via` 本機 `"aicomm"`、跨機 `"aicomm-peer"`；`ref` 規則與不合格處理；同一次 `send` 同一個 `ref`；body MUST NOT 含寄件者、內文、附件、工單號或 `--ref` 的值；「文字」指 `text`，原禁令保留。Scenario「戳的文字」「跨機的戳與本機相同」改寫，新增「訊息 id 不合格就不帶 ref」「多收件人帶同一個 ref」等；「戳逾時」原樣帶著 | C-1～C-6、C-8、C-9～C-14 |
| `不改 Angareion 與 sidecar，不攜帶私人資訊` | 本文加一句 skill MAY 在戳的 body 帶 `via`／`ref`（MyLinuxPool#786 D3；舊版 sidecar 會忽略）。Scenario「不發查紀錄的請求」的 THEN 改成 body 只有 `text`、`interrupt`、`via` 與選用 `ref`；「對 sidecar 的請求種類」不變 | C-11 |
| `跨機叫醒只經 \`pool-peer\` 的兩個用法` | 本文不變（但這裡有兩個 scenario 把 body 寫死）。Scenario「只打兩個路由」「找到一台並戳成功」的 THEN 改 | C-2 |
| `\`find\` 找到的每一台怎麼戳` | 本文加「每一台的 `via`／`ref` 都相同」。Scenario「多台時只要有一台被打斷就加 interrupt」的 THEN 兩個 stdin 都加 `via`、`ref`；其他 scenario 原樣帶著 | C-7 |

不改的：`## Purpose`、「叫醒是盡力而為，可關閉」（`--no-wake` 的行為沒有變）、「`wake:` 行的文法」、原因碼清單、結束碼。

### 5.2 程式、測試、文件

| 位置 | 改什麼 |
|---|---|
| `src/aicomm/cli.py` | 新常數 `POKE_VIA_LOCAL`／`POKE_VIA_PEER`／`POKE_REF_RE`；新函式 `_poke_ref`、`_poke_body`；`message_id` 沿呼叫鏈往下傳；本機（約 `:628`）與跨機（約 `:515`）改呼叫 `_poke_body`；`:62` 註解改寫 |
| `tests/unit/_aicomm_fakes.py`（假 sidecar／假 `pool-peer`） | 若現有 fixture 不能指定 angareion 印出的 stdout 或不能取回 poke body 逐字，就擴充 |
| `tests/unit/test_aicomm_send.py` | 逐字比對 body 的現有測試跟著改（行號列在 tasks.md）；新增 C-1～C-14 |
| `docs/aicomm.md` | 約 `:103`、`:120` 的 body 欄與說明（D-6） |
| `skills/aicomm/SKILL.md` | 不改 |
| `docs/architecture/decisions.md` | 新增 D-26；D-25 註明「由 D-26 擴充」（本設計 PR） |
| `docs/architecture/overview.md` | 不改（已查過，沒有寫 aicomm 的 poke body 形狀） |

## 6. 模組切割與依賴方向

- 全部在 `src/aicomm/cli.py` 內，不新增模組、不新增依賴。
- 兩個純函式：`_poke_ref(out) -> str | None`（驗證）、`_poke_body(interrupt, via, message_id) -> dict`（組 body）。本機的 `_wake_one_inner` 與跨機的 `_poke_peer` **共用同一個 `_poke_body`**，不各自手寫 dict；差別只有傳進去的 `via` 常數。
- 依賴方向不變：`_cmd_send` → 叫醒鏈 → `_poke_body`；`_poke_body` 與 `_poke_ref` 不依賴任何 I/O。

## 7. 跨專案介面（給 MyLinuxPool，已定稿）

- 欄位：poke body 的選用 `via`（`^[a-z][a-z0-9-]{0,31}$`）與 `ref`（訊息 id，`^[A-Za-z0-9_-]{1,64}$`）；格式不對 sidecar 回 400 `invalid_request` 且不送出。依據 MyLinuxPool `modules/sidecar/docs/sidecar.md` §2.2、`openspec/changes/sidecar-poke-records/design.md` D3。
- 不認得的鍵照舊忽略，MLP D3 已寫成以後不能改的承諾；舊 sidecar（8b1acdd 到 c2b945f）的 poke 路由只讀 `text`／`interrupt`。
- 若 MLP 要改 `via` 或 `ref` 的格式，要先走跨專案的介面變更（R-3）。

## 8. 風險與未決

- **R-1 via 是自己宣告的**：sidecar 分不出真假（MLP design.md R-2、F-2）。巡檢只能把 `via` 當線索，不能當證據，也不能拿來做授權。docs 要寫這一句。
- **R-2 ref 格式不對，sidecar 會回 400、不送出**：所以 aicomm 一定要先檢查（D-2，C-3、C-5）。漏掉檢查的後果，是每一次叫醒都靜靜地變成 `not-woken http-400`。
- **R-3 MLP 之後收緊 `via` 的格式**：會讓每一次戳都 400。MLP D3 已把「不認得的鍵照舊忽略」寫成以後不能改的承諾，`via` 的格式也寫進了 sidecar.md；C-8 把我們的常數鎖在這個格式內。如果 MLP 要改格式，要先走跨專案的介面變更。
- **R-4 舊 sidecar**：MLP 確認從 8b1acdd 到 c2b945f 的 poke 路由都只讀 `text`／`interrupt`，不認得的鍵會忽略。所以不需要能力標記，也不需要重試。
- **R-5 ref 不合格的情況看不到**：設計上就是靜默不帶 ref（Q-2）。實際上只有 angareion 改了 id 格式才會發生；這時用 sidecar 紀錄的 `ref: null` 就看得出來，所以不加新的 stderr 輸出（`wake:` 行的文法不動）。
- **R-6 巡檢還不能用 ref 直接查**：要等 MLP 回答 Q-MLP-1，不擋本單。

### 要 MLP 回答的事（已送 `--no-wake`，不擋本單）

- **Q-MLP-1**：`GET /pokes` 的列表查詢能不能加 `ref=` 參數（完全相等），讓巡檢可以直接用訊息 id 找到它的叫醒紀錄？如果不加，巡檢就是先用 `agent` 加時間區間列出，再在本地用紀錄的 `ref` 欄篩選。

### 已決定（PM）

- **Q-1**：設計文件的形式用 openspec change `aicomm-poke-ref`，不用 design-notes。
- **Q-2**：D-2 的「ref 不合格時不出聲」接受。
