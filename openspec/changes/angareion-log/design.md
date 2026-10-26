## Context

工單 MyAiStorage#254（c 級）：`angareion`／`aicomm` 的唯讀查詢指令（log）設計。本 change 只設計不實作，經非作者審查、PMO 以介面為主向本人說明、本人同意才動工。本回答工單的 1～4＋PMO 補充的 1～3；#219（單一讀者）、#224（新身分會讀到別隊舊廣播）作為關聯單納入考量。

**標記**：【事實】附出處（行號以 main bff2430 為準）；【推論】是作者從事實推的；【未知】沒查到或者不做實測回答不了（本單不打真信、不量效能）。

### 事實

| # | 事實 | 出處 |
|---|---|---|
| F1 | `read` 有三種目標：`--channel`（整個頻道，stderr 印一行警告）、不給 `--channel`（「寄給我的」：`to` 是我、廣播、或 `mentions` 有我）、`--mentioned`（「@ 我」：`to` 是我或 `mentions` 有我，廣播不算；與 `--channel` 互斥） | `src/angareion/cli.py:75-78`（警告字串）、`:120-125`（目標建構）、`:130-131`（互斥）；「寄給我的」與「@ 我」的匹配在 `src/angareion/backend.py:298-302`（`recipient_matches`，廣播算「寄給我」）與 `:305-309`（`mention_matches`，廣播不算） |
| F2 | `read` 把訊息印完並 flush 之後才 `batch.commit()` 推進游標；`--since` 時 commit 不動游標（時間只游標） | `src/angareion/cli.py:386-424`（commit 在 `:422`）；`src/angareion/client.py:123-145、133`；`src/angareion/backend.py:496-498` |
| F3 | 「寄給我的」跨 channel：對每個已登記 entity 逐一 `read` 自己的游標鍵；跨 channel 排序鍵是 `(received_at, channel, id)` | `src/angareion/client.py:147-164`（排序在 `:158`） |
| F4 | GitHub 後端的讀取路徑：組 URL `repos/<repo>/issues/<n>/comments?per_page=100`（外加 `since`）→ 依 `Link` 標頭逐頁抓 → 每則留言 `parse_message`（非 angareion 訊息的留言丟掉）→ 以到達位置（comment id）升冪輸出 | `src/angareion/github.py:259-263`（URL）、`:144-155`（Link）、`:382-441`（read 迴圈，`:409-412` parse、`:436-437` 排序） |
| F5 | `read` 不送 `If-None-Match`（每次都是完整請求）；304 條件式查詢只在 `wait` 的 `check` 上有，且可記錄條件是「單頁、第 1 頁未滿、有 ETag」，查到新訊息就清掉記錄 | `src/angareion/github.py:382-398`（read 無 etag）與 `:280-380`（check，可記錄條件在 `:333-341`） |
| F6 | 時間語意：新舊只看後端（到達位置 comment id ＋後端 `created_at`），不看寄件者填的 `at`/`id`；`--since` 含邊界；游標比對窗是 `at - 60 秒` margin，容忍亂序晚到 | `src/angareion/backend.py:19、125-159`；`openspec/specs/angareion/spec.md`「目標與游標」 |
| F7 | 本機狀態檔：游標 `cursor-<slug>.json`（`CursorStore`，只有 `save`/`save_many` 會寫）與 wait 的 ETag `wait-<slug>.json`（`FileETagCache`，寫失敗只警告一次），都用「同目錄暫存＋`os.replace`」原子改寫；`wait-<slug>.json` 的目錄以 mode 0700 建立、暫存檔由 `mkstemp` 建立（0600） | `src/angareion/state.py:24-34、76-174、250-275`；`src/angareion/config.py:62-74`（slug） |
| F8 | 訊息模型：`from`/`to`（可空）/`channel`/`urgency`/`content`/`attachments` ＋ 寄件者產生的 `id`（ULID）/`at`；`mentions`/`in_reply_to` 選填，讀取用寬的（壞欄位丟掉、訊息照收）；內文上限 65536 字元；`CHANNEL_NAME_RE`＝`^[A-Za-z0-9_-]+$` | `src/angareion/messages.py:32、42、174-186、297-334、388-440` |
| F9 | `read --json` 的欄位＝`message_to_dict` 的 **10 個 key**（`id`、`at`、`from`、`to`、`channel`、`urgency`、`content`、`attachments`、`mentions`、`in_reply_to`；「六」是必要欄位的數目，不是 dict 的 key 數）＋`received_at`（後端時刻）；文字輸出的標頭 `[urgency] id at (received …) <from> channel: …` 之後縮排印完整內文（逐行），不截斷 | `src/angareion/messages.py:235-248`；`src/angareion/cli.py:134-151、154-158、414-415` |
| F10 | 結束碼契約：0 成功、1 用法／輸入錯、2 後端／網路／設定錯、3 只給 `wait` 逾時；401→AuthenticationError、403（含次級限制）→BackendError 不重試 | `src/angareion/cli.py:57-60、594-611`；`src/angareion/github.py:132-137` |
| F11 | 身分決定一切檔案位置：`config.json` 的 `identity`，`ANGAREION_IDENTITY` 優先；不同 seat 不碰彼此的檔案（cli 的註釋與程式結構都是同一個 `config.identity` 造檔名） | `src/angareion/config.py:130-152`；`src/angareion/cli.py:534-543` |
| F12 | channel→entity 只認設定檔（`ChannelStore`：`ensure_fresh`／`entity_for`／`entities`）；未登記的 channel 是輸入錯（exit 1） | `src/angareion/cli.py:555-558、594-597`；`src/angareion/client.py:76-79、149` |
| F13 | aicomm 不 import Angareion、不讀其設定檔，只以 subprocess 呼 `angareion`，stdout/stderr 原樣轉、結束碼跟著走（`inbox` 先 `wait` 後 `read` 的轉發是現成例子） | `src/aicomm/cli.py:114-133、261-275` |
| F14 | 文件是被測試守住的：`test_aicomm_docs.py` 要求補救句 `angareion read --since <時刻>` 同時出現在 SKILL、docs 與 `cli.py` 的提示句裡；`test_angareion_docs.py` 守 `docs/angareion.md` 的既有關鍵句 | `tests/unit/test_aicomm_docs.py:83、87-95、128-130`；`tests/unit/test_aicomm_inbox.py:99`；`tests/unit/test_angareion_docs.py` |
| F15 | spec 明文「Angareion 對外 MUST 只有五個介面」，`docs/angareion.md:3` 與 `skills/angareion/SKILL.md:8` 也寫「五個」（README 沒有介面數的字樣；「五個介面／五個指令」殘留逐檔六處，tasks 2.8 逐檔點名：channel-config spec（經本 change 的 delta 套用，低-17）、docs 三處、SKILL 兩處；`atelier/worker-team/pm/v0.md:37` 有殘留**但不改**（中-8）：v0 是 append-only 的已發布版本檔（`openspec/specs/atelier/spec.md`「版本檔 append-only」——既有版本檔 MUST NOT 被修改；`tests/unit/test_atelier_harness.py` 的 `test_published_version_files_are_append_only` 守），現行 PM harness（v2）沒有介面數字樣、不用動；要改只能新增下一個版本檔並另走 PM 決定）；#219 的補救規則寫在 SKILL/docs/提示句三處 | `openspec/specs/angareion/spec.md`「五個介面」；`openspec/specs/angareion/channel-config/spec.md:29`；`docs/angareion.md:3、11、225`；`skills/angareion/SKILL.md:8、21`；`atelier/worker-team/pm/v0.md:37`（僅事實：殘留在，不改） |
| F16 | TS 端（MyAiEntry）與 Python 端讀同一種留言（front matter 對照寫在程式碼註釋裡：兩邊都以 `---` 分界、`mentions` 一行 flow-style）；協定版次欄位 `angareion` 不因 log 變動 | `src/angareion/messages.py:250-258、321-322、350-353` |
| F17 | `Link`、`ETag` 標頭與 `since` 參數在列表 API 上都可依賴（`check` 已經在用它們）；GitHub 文件明載：List issue comments 的 `since` 是 "**Only show results that were last updated after the given time**"——按留言的**最後編輯時刻**（`updated_at`）過濾，不是 `created_at`，所以被編輯過的舊留言會在新時間再次出現在列表結果裡（`created_at`/`updated_at` 是回應裡每則的欄位）。D3.3 的「同 id 內容變就覆蓋」正是依這條成立 | `src/angareion/github.py:144-155、327-341`；GitHub 文件「List issue comments」的 `since` 參數（`per_issue` 列表與 repo 級列表都是 "last updated after"） |

### 推論（不是事實）

- **I1（現況回頭查的成本）**：log 若不做快取、每次現場查＝對每個涉及 channel 從頭（或自 `since`）逐頁列出：每頁 1 個請求、每頁 100 則留言（F4）。以 10 個 channel、每個 500 則留言的規模推估一次查詢約 50 個請求；每請求 0.2～0.5 秒則一次 10～25 秒（依網路；【推論】）。`--around`／`--thread`／`--grep` 都把成本推回「列全部頁再本機跑」（F4：後端只會照 `since` 過濾）。同樣的查詢反覆做就重付同樣的成本。
- **I2（加快取後的常態成本）**：安靜的 channel 每次查詢＝1 個條件式請求（304；#96 已證明的模式，304 不計 GitHub 額度）＋本機過濾（F5 的可記錄規則沿用）；有新訊息的 channel＝1～2 個完整請求把增量補上。10 個 channel 的常態查詢是 10 個 304。【推論，量測另驗（U3）】
- **I3（磁碟成本）**：內文上限 65536 字元（F8）但實務訊息多為數百字元；以平均 1～2 KB 計，1000 則約 1～2 MB／channel；整個身分的快取在數十 MB 量級。【推論】
- **I4（限流額度）**：GitHub 對已認證的 REST 核心請求是每小時 5,000 次（引用 GitHub 文件；fine-grained PAT 計入同額度）；次級限制的精確數字未公開【未知】。angareion 對 403 一律不重試（F10），所以「查詢一次吃掉幾十個請求」的重複行為才是本設計要消掉的——不是靠重試。

## Goals / Non-Goals

**Goals**

- 回答工單 1～4：介面（D1）、不動游標的保證（D2）、效能與快取（D3）、與 `read`／`inbox` 的關係（D4）。
- 回答 PMO 補充 1～3：`aicomm log` 薄包（D5）、SKILL.md 那句話（D5）、`peek` 只列後續（D5）。
- 一次查詢拿到全部相關信件（過濾＋前後文＋串），消除「`--since` 反覆撈會漏」這一類錯。

**Non-Goals**

- 不改游標語意、不改 `read`／`wait` 行為、不改協定（`angareion: 1`）、不動 TS 端（D6）。
- 不做 ack／已讀回條／限流（angareion 協定明文不做）。
- 不做 `aicomm peek`（sidecar 畫面，MyLinuxPool#366 上線後另議）。
- 實作（只列 tasks）。

## D1 介面：`angareion log`

```
angareion log [--channel <ch>]… [--from <AI 名>] [--to <AI 名|me>] [--mentioned]
              [--since <時刻>] [--until <時刻>] [--grep <字串>]
              [--around <訊息 id> [--before N] [--after N] | --thread <訊息 id>]
              [--limit N] [--json]
```

### D1.1 旗標語意（每個旗標一個實際用例）

| 旗標 | 語意 | 實際用例 |
|---|---|---|
| `--channel <ch>`（可重複） | 只查列出的 channel；重複給＝多頻道聯集；不給＝所有已登記 channel（含寄給別人的訊息）。未登記 → exit 1。**不印 `read --channel` 那行警告**：log 本來就是對整個頻道存檔的查詢，「會看到別人的信」不是副作用而是目的 | `angareion log --channel team-alpha --channel team-beta --from mylinuxpool-impl-w1-1`——某隊員在兩個頻道寄過的全部信 |
| `--from <AI 名>` | 寄件人精確相等（不做模糊比對；舊文法的名字照訊息裡的字串比對，讀信用寬的）。`me` 是身分別名（P1，作者提議） | `angareion log --from me --since 2026-10-25T00:00:00Z`——我昨天寄過什麼 |
| `--to <AI 名\|me>` | 「寄給 `<名>`」＝與 `read` 的「寄給我的」同一條件（F1）：`to` 是該名、或沒有 `to`（廣播）、或 `mentions` 有該名。`me`＝本指令解析出的身分（`config.json` 或 `ANGAREION_IDENTITY`） | `angareion log --to me --until 2026-10-25T23:59:59Z`——我今天以前收到的信（游標不動的補讀） |
| `--mentioned` | 「@ 我」＝`to` 是我或 `mentions` 有我，廣播不算，跨所有 channel（語意同 `read --mentioned`，F1）但沒有游標。在 log 裡**可以**與 `--channel` 組合（P2；`read` 不行，因為它有自己的游標鍵） | `angareion log --mentioned --channel team-alpha`——team-alpha 裡點名過我或寄給我的信 |
| `--since <時刻>`／`--until <時刻>` | 時間下界／上界，都是**後端記錄的時刻**（即 `received_at`、GitHub 的 `created_at`；RFC 3339，含時區），**皆含邊界**；與 `read --since` 同一條 `parse_backend_time`（F6）。`--since > --until` → exit 1（P8：比「悄悄空結果」安全） | `angareion log --channel team-alpha --since 2026-10-25T00:00:00Z --until 2026-10-25T23:59:59Z`——該頻道昨天的信 |
| `--grep <字串>` | 內文的**字面**（非 regex）搜尋，**不分大小寫**，只比對 `content`（寄收人過濾有專屬旗標；附件不看）。單值不重複（要 AND 就跑兩次或縮小視窗） | `angareion log --channel team-alpha --grep blocking`——頻道裡提過阻塞的信 |
| `--around <訊息 id>` | 以**訊息 id**（ULID，輸出標頭裡的那個）錨定一則，視窗＝同 channel 內以到達位置（comment id）計的**前 `--before N` 則＋後 `--after N` 則**＋錨點本身。`--before`／`--after` 各自預設 0（單獨 `--around <id>`＝只印該則，P3）；N ≥ 0 整數，負或非整數 exit 1；非 26 字 ULID exit 1；同步後仍找不到 → exit 1（J3；空結果是 exit 0，兩者可區分） | 補看多行訊息漏掉的中間段：`angareion log --around 01JD8X5P3Q9V2W7M4T6RHZABCD --before 3 --after 10` |
| `--thread <訊息 id>` | 沿 `in_reply_to` 取整串：從錨點沿 `in_reply_to` 往上走到根，取根＋全部子孫（可傳遞閉包），按到達順序印。錨點在 channel 內找；自己的 `in_reply_to` 指到的訊息不存在時，錨點視為根（**已知限制**見 D3.6：串不跨 channel——`in_reply_to` 指到別的 channel 的訊息時，鏈在跨 channel 前停下，該訊息視為根） | 回顧決策串：`angareion log --channel team-alpha --thread 01JD8X5P3Q9V2W7M4T6RHZABCD` |
| `--limit N` | 過濾完之後**依輸出順序排序、取排序結果的最後 N 則**——「最新」就由這個排序鍵定義：單 channel 以到達位置（comment id 與 `read` 同源，F4），跨 channel 以 `(received_at, channel, id)`（F3）「全域最新的 N 則」，**不是每個 channel 各取 N 則**；取完仍由舊到新印（J2）。N=0 合法（空結果 exit 0）；負數 exit 1；**預設不限**——查詢工具的禁手就是把答案悄悄截掉；預設不限的用量代價（aicomm 層緩衝）寫明在 D5.1 | `angareion log --to me --limit 20`——最近收到的 20 封，由舊到新 |
| `--json` | 與 `read --json` 同欄位（`message_to_dict`＋`received_at`；F9），空結果印 `[]` | `angareion log --mentioned --json`——程式解析用 |

### D1.2 組合規則（工單點名 `--around` 跟 `--since` 這一類）

1. **過濾旗標（`--channel`／`--from`／`--to`／`--mentioned`／`--since`／`--until`／`--grep`）任意組合、全部取交集**；`--channel` 重複給是聯集。
2. **錨點旗標 `--around` 與 `--thread` 互斥**（同給 exit 1）；`--before`／`--after` 只能跟 `--around` 一起給（否則 exit 1）。
3. **`--around`／`--thread` 與過濾旗標可以同時用**：套用順序固定——**（1）** 由錨點定出候選集（`--around`：以到達位置數出的前 N＋後 N＋錨點；`--thread`：整串），**（2）** 套過濾交集，**（3）** 依輸出順序排序（單 channel 到達位置；跨 channel `(received_at, channel, id)`），**（4）** `--limit` 取排序後的最後 N 則，**（5）** 以同序印出。所以「窗口是位置先決、過濾再縮小」：`--around X --before 50 --since <t>` 會先取 X 前 50 則、再留下不早於 <t> 的；`--limit` 拿的是**排序尾端**那 N 則。
4. **錨點的查找與過濾是兩件事，規則只有一條（中-1 定案）**：錨點的查找**只受「錨點所在的 channel 在選取集合內」限制**（`--channel` 的聯集含錨點所在的 channel、或根本沒給 `--channel`）；同選取集合內同步後沒有那個訊息 id → **exit 1**。錨點若不符合**其他**過濾條件（`--from`／`--to`／`--mentioned`／`--since`／`--until`／`--grep`）**不是錯誤**：照常以它定位、框出窗口，只是錨點自己會被過濾交集濾掉、不出現在輸出裡——結果是 exit 0 加窗口內符合條件的訊息。`--limit` 同理：先篩後截，錨點被截掉也照常（沒有「錨點保留地位」）。design 與 spec 用同一句話釘住這條（D1.2 與「唯讀查詢 log」的套用順序），不再有第二種讀法。
5. **輸出固定由舊到新**：單 channel 內以到達位置（comment id，與 `read` 同源，F4）；跨 channel 以 `(received_at, channel, id)`（同 `client.py:158`，F3）。不提供排序旗標（J1；要看時間序請排序 `--json`）。
6. `--mentioned` 與 `--channel` 在 log 允許同給（P2）；與 `--to` 同給不衝突（交集＝`--mentioned`，因為「@ 我」是「寄給我」的子集）。
7. **已知限制**（D3.6）：`--thread` 不跨 channel；連帶地，若給了 `--channel` 又給 `--thread`，跨 channel 的祖先鏈在 channel 邊界停下（該訊息視為根）。

### D1.3 輸出格式

- 文字輸出**照抄 `read` 的 `_format_message`**（cli.py:134-151，F9）：標頭＋`mentions:`／`in_reply_to:` 兩行（有值時）＋內文逐行縮排**完整印出**＋附件清單。多行內文不截斷——這正是 #254 要修的「`--since` 撈漏」的一半（另一半是過濾能力）。
- `--json`：與 `read --json` 同一組欄位（F9），`content` 是一個 JSON 字串（換行轉義、完整保留）。不額外帶 `comment_url`（與 `read --json` 保持同形；快取裡有但不浮出）。

### D1.4 錯誤與結束碼

| 情況 | 結束碼 | 說明 |
|---|---|---|
| 成功（含空結果） | 0 | 空＝沒有訊息印出／`--json` 印 `[]` |
| 用法錯：未知 channel、壞時間格式（`--since`／`--until`）、非 ULID 的 `--around`／`--thread`、`--around`＋`--thread`、`--before`／`--after` 孤立或負、`--limit` 負、`--since > --until`、錨點同步後找不到 | 1 | stderr 說明，標準 `Error:` 開頭；什麼都不印在 stdout |
| 後端／設定錯：token、config、HTTP 錯、同步任一涉及 channel 失敗 | 2 | 同 D3.5：**不同步成功不出結果**，避免拿舊鏡射回報完整答案 |

沒有 exit 3（那是 `wait` 的逾時碼；log 是即時查詢，不會等）。

### D1.5 不做（工單說「用不到的就不做」，逐條給理由）

| 不做 | 理由 |
|---|---|
| regex 搜尋／`--grep` 重複給（AND） | 呼叫端是 AI，字面比對已夠；regex 是錯誤溫床。要 AND 可跑兩次或用 `--json` 自行過濾 |
| `--follow`／`-f` 即時跟隨 | 「有新的來」是 `wait`＋`read`（或 `inbox --wait`）的事；log 是一發式查詢。兩種語意混一個指令會讓「單一讀者」規則（#219）的邊界變糊 |
| `--page`／`--offset` 分頁 | 新訊息不斷到達時 offset 會漂移（答案不穩定）；「往前往後翻」的正解是 `--around`＋時間窗，翻頁讓呼叫端用 `--json`＋`id` 自己做 |
| `--sort`／`--newest-first` | 一種固定順序（J1）對腳本最可預期；排序在 `--json` 端一行程式 |
| `--raw`（印原始留言） | 訊息格式是內部細節；結構化輸出已有 `--json`。介面不該把 front matter 洩出去 |
| 樹狀縮排／圖形化 thread | 呈現不是查詢的本分；`--json`＋`in_reply_to` 足夠畫 |
| `log --ticket`／`--attach`／`--ref` | 那是 `send` 的旗標；log 不送信 |
| `--no-sync`（跳過同步） | 查詢的完整性建立在「先同步再看」（D3.5）；跳過同步＝宣告答案可能漏，回到「容易漏」的老問題 |

## D2 永遠不動游標

### D2.1 程式結構上的保證

- **log 不用 `ReadBatch`**：`client.py` 的 `read` 回傳 `ReadBatch(messages, commit)`（F2），`commit` 是唯一推進游標的通路；`cmd_read` 在輸出後呼叫它。log 的 client 方法回傳普通 `list[Message]`，**沒有 commit、也不接游標**——log 路徑上不存在「推進」這個動作的程式入口。
- **log 不碰 `CursorStore`**：`CursorStore.save`／`save_many`（F7）是游標檔僅有的寫入者；log 的實作不呼叫它們（連讀也不用——log 的答案與游標無關）。快取是**另一個模組、另一組檔案**（`log-<slug>/`），與 `cursor-<slug>.json`、`wait-<slug>.json` 的檔名與讀寫路徑完全不相交。
- **重跑安全**：log 對本機的唯一寫入是快取檔（冪等 upsert）；重跑、並行跑、跑到一半斷線都不會改任何人的「已讀」狀態。
- **與 #219 的關係**：單一讀者規則管的是「消費未讀信」的讀者（wait/read/inbox）；log 不消費任何未讀信，**不算讀者**。同一身分可以一邊在前景 `aicomm inbox`、一邊 `aicomm log` 補查，互不搶信。
- **與 #224 的關係（只說明，不通；低-7）**：#224（新身分會讀到別隊的舊廣播）不在本單處理，但 log 改變了它的呈現面貌：預設無旗標的 log 會把每個已登記 channel 的**全部歷史**（含寄給別人的、含別隊的舊廣播）一次交出——「新身分一查就看到別隊舊廣播」的現象更容易被一次看到。`--since` 縮窗與 `aicomm log` 的預設 `--to me`（含廣播）能縮小視野，#224 本身（廣播的歸屬／開信時點）已另依其單。

### D2.2 用什麼測試守

1. **假游標守衛**：把 `CursorStore` 換成 `save`/`save_many` 會 raise 的假件，跑 log 的全部旗標組合（每張用例），全程 exit 0——證明 log 從頭到尾沒碰游標寫入。
2. **位元組不變**：跑 log 前後對 cursor 檔做 sha256 相等斷言（「從未讀過的空身分」與「有游標的身分」各一）。
3. **錯位的通路**：故意在 cmd_log 加一次 `cursors.save(...)` 的改壞驗證，上面兩個測試要轉紅（照倉庫的改壞驗證慣例，抽樣即可）。
4. **文件測試**：SKILL／docs 有「log 不動游標」的關鍵句（`test_angareion_docs.py`／`test_aicomm_docs.py` 各加一條）。

## D3 效能：後端是 GitHub Issues

### D3.1 現況怎麼讀（事實＋行號）

- `read`（無 ETag）：`GitHubIssuesBackend.read`（github.py:382-441）組 listing URL（`:259-263`，`per_page=100`＋`since`）、依 `Link` 逐頁抓（`:144-155`）、每則 `parse_message`、以到達位置排序。「寄給我的」要對**每個**已登記 entity 跑一輪（client.py:149-155，F3）。
- 每頁＝1 個完整請求（F5：read 送不出 `If-None-Match`）；`--since` 以外的過濾（from／to／grep／id 定位）都得「列全部頁再本機過濾」（F4）。這是 I1 的成本模型，也是反覆補讀重付額度的原因。
- `wait` 已把「重跑近乎免費」的需求解在 `check`（304 條件式查詢、可記錄條件單頁未滿、查到新訊息清記錄，github.py:280-380＋wait 狀態檔）：log 的快取沿用同一套證明過的規則，不新發明。

### D3.2 快取放哪、什麼格式

- **位置**：state 目錄（`ANGAREION_STATE_DIR`，與游標檔同根，F11 身分隔離）下 `log-<identity_slug>/`，**每個 channel 一檔** `<channel_slug>.json`。`channel_slug` 是 channel 名的**可逆轉義**（中-6 定案）：屬 `[a-z0-9_-]` 的字元一字不動，**大寫 ASCII 字母（`A`–`Z`）與其餘字元一律以 `%XX` 編碼其 UTF-8 位元組**（percent-encode 式；`%` 本身先編碼，保證可逆與單射）。**大寫也轉義是低-16 的定案**：大小寫不敏感的檔案系統（macOS APFS、Windows NTFS）會把 `A.json` 與 `a.json` 指到同一個檔，而 `A` 與 `a` 都是 `CHANNEL_NAME_RE` 的合法名、可以同時存在 `channels.json`——只轉義 `/` 這類字元會讓兩個 channel 共用同一個快取檔，檔內記的 channel 名與載入時的比對**永遠不符**（→ D3.4「整檔視為空」）＝這組 channel 的快取每次都失效、每次重付全量同步（正是 J6 要消掉的成本）。把大寫一併轉義，「可逆與無碰撞」就在檔名層**無條件**成立（任何檔案系統）：轉義結果只含 `[a-z0-9_%]`，大小寫不再是變數；代價只是 slug 變醜，對既有的 channel 名（小寫字母、數字、`-`、`_`）零影響。**檔名安全不建立在 channel 名的字元限制上**：`CHANNEL_NAME_RE`（F8）只在 `channel create` 與註冊簿重建時檢查，`channel_config.py:261-282` 的 `channels()` 對組態檔裡的名字**只查非空字串**——手改的 `channels.json` 可能給出 `../x`、`a/b`，log 第一次把組態字串變成路徑元件，所以快取層自己解：轉義保證任何字串都成單一檔名元件、寫不出 `log-<slug>/`。與 `identity_slug` 的替換式不同（那會讓 `a/b` 與 `a_b` 撞同一個 slug），這裡可逆；檔內仍記原始 channel 名，載入時與 `ChannelStore` 比對（不符＝整檔視為空，D3.4）。
- **內容**：`{"version": 1, "channel": <名>, "entity": {"repo", "issue"}, "last_comment_id": "<十進位字串>", "last_created_at": "<RFC 3339>", "etag": {"url", "etag"} 或 null, "messages": [{"comment_id": "<十進位字串>", "created_at": "<RFC 3339>", "comment_url": <url>, "message": <message_to_dict 欄位>}…]}`。**`comment_id` 是 GitHub 的 int64，以十進位字串儲存（躲 JSON→浮點的精度風險），但一切的比較與排序都用 `int`**：`comment_id` 之間、以及與 `last_comment_id` 之間都是**整數比較**——字串只是儲存格式；既有程式本來就是整數比（github.py:436、backend.py:154），照抄（低-5：跨位數混排（9 位與 10 位 id 同 channel）若照字典型比較會把新留言當舊的丟掉、`--around` 的前後 N 也會數錯）。`messages` 以 `comment_id` **整數**升冪。
- **與游標檔的關係**：**不相交**。快取不是游標：沒有 `to:*`/`mention:*`/`channel:*` 的游標鍵、不參與「讀走」語意、`read`／`wait` 不讀它，log 也不讀游標檔。與 `wait` 的 ETag 檔（`wait-<slug>.json`）亦是兩組檔案：log 的 ETag 記在每個 channel 快取檔自己身上。
- **權限**：目錄以 mode 0700 建立（同 `FileETagCache` 的慣例，state.py:149）；檔案由 `mkstemp`（0600）＋`os.replace` 原子改寫（F7）。快取放的是頻道訊息內文（可能含「寄給別人」的信），本機只留給該身分讀——0700／0600 是最低要求（J6：接受用磁碟換查詢速度）。

### D3.3 怎麼知道新舊（增量同步）

1. 對涉及查詢的每個 channel：讀快取檔 → 有上次位置。
2. 列表請求帶 `since = last_created_at - 60 秒`（與游標同源的 margin，F6；快取為空時不帶 `since`＝全量）。`since` 依 F17 是按留言的**最後編輯時刻**過濾——被編輯過的舊留言會帶著新的 `updated_at` **再次出現在列表結果裡**，這就是第 3 步「同 id 內容變就覆蓋」在真後端上的機理；這條語意已寫進 spec 的列舉動作要求（「後端可以替換」），假後端 MUST 照守，不會被「照 `created_at` 或直接忽略 `since`」的假件騙過（低-5）。
3. **合併規則（冪等 upsert）**：後端回的每則留言，`comment_id` **以整數比較**比 `last_comment_id` 新、或從未出現在快取（接住亂序晚到的較小 id——比 cursor 的 `seen` 視窗更強，F6），就進清單；同 id 內容不同（留言在 GitHub 被編輯）就覆蓋（機理見第 2 步：`since` 按 `updated_at` 過濾，讓編輯過的舊留言回到列表上；`last_created_at` 本身照 `created_at` 記——編輯不改 `created_at`）。`last_comment_id`/`last_created_at` 只進不退。
4. 有新回應時**不記 ETag**（回應含新訊息不得成為下次的「沒有新訊息」驗證器，照 `check` 的規則，github.py:363-367）；掃過且乾淨、單頁未滿才記（github.py:333-341 的條件原樣沿用，P4）。下次同 sync 若記錄的 URL 一致就送 `If-None-Match`：304＝沒有增量、不吃額度。
5. 合併有變動才重寫檔（原子改寫）；無變動不寫，免得無謂 I/O。

### D3.4 失效、壞掉、並行

| 情況 | 行為 |
|---|---|
| 快取檔不存在 | 當空快取，全量同步後服務查詢 |
| 快取檔是壞 JSON／版本不認得／缺欄位／`entity` 與 `ChannelStore` 解析不符（channel 刪了重建換 issue） | **整檔視為空**，全量重建；絕不「部分信任」 |
| 磁碟寫不進去 | 照 `FileETagCache` 的慣例：stderr 警告一次，查詢繼續（記憶體內鏡射服務本輪）；下一輪多付一次同步而已，結果正確性不變 |
| 兩個 log 並行同一身分 | 暫存＋`os.replace` 保證檔案不會半寫（F7）；最壞情況是其中一輪的增量被覆蓋、下一輪重補（upsert 冪等），不漏 |
| 要重來 | 刪掉 `log-<slug>/` 即全量重建（與「刪游標檔＝從頭讀」同一套說法） |

### D3.5 正確性界線（不漏的底線）

- **任一涉及 channel 的同步失敗（HTTP 錯、逾時）→ exit 2、不出任何結果**。快取只在「同步成功（304 或 200 合併）之後」用來回答；絕不出「部分同步＋舊鏡射當完整答案」——那正是 #96 用 304 規則消掉的漏信型錯誤。
- log 的答案由「同步後的鏡射＋本機過濾」組成；`--since`／`--until` 在本機套用（後端只保證 `since` 過濾，F4），所以時間窗不依賴後端對 `until` 的支援。
- **成本總結（I1/I2）**：查詢一次＝每涉及 channel 1 個增量請求（常態 304）＋本機 O(訊息數) 的過濾；首次使用＝每 channel 全量（等同一次完整的 read 掃描，只付一次）。

### D3.6 已知限制（PM 決定寫進 design 與 spec，不是 bug；低-6、低-9）

1. **`--thread` 不跨 channel**：串只在錨點所在 channel 內組裝。`in_reply_to` 若指到別的 channel 的訊息，鏈在跨到別的 channel 前停下——那則視為根（與「指不到就視為根」同一處置）。跨 channel 的「整段討論」要看涉及的 channel 各查一次（或 `--json` 之後自行合併）。
2. **快取不解後端的刪除**：合併只有 upsert（D3.3），後端刪掉的留言會留在快取與 log 的結果裡；`--since` 增量也不會回報「這則不見了」。全量重建（刪掉快取目錄）之後，已刪留言才會消失（列表不再回它）。skill 的說明會寫明：**log 裡看得到不代表這則現在還在**。
3. **`aicomm log --thread` 的串可能被預設 `--to me` 截一半**（中-1 的規則自然套用的結果；注入規則本身不因本條變動）：`--thread` 照常定位整串，但串裡「不寄給我」的訊息會被過濾交集濾掉——exit 0、拿到的是串的可見部分。要看整串的補查走 `angareion log --thread`（angareion 層沒有注入）。

## D4 與 `read`／`inbox` 的關係

- **游標語意不變**：`read`（與 `inbox`）的行為、`#219` 的軟規則與提示、`read --since` 的規格原樣不動（spec 的「目標與游標」不需修改）。log 是平行的唯讀通道。
- **`read --since` 的說明要不要改指向 log**：**要**，但只改「補讀」的敘事，不動規格。log 上線後，「被叫醒但 inbox 是空的」這一類補查的主要工具應該是 log（能取到完整內文與前後文），`read --since` 退到「知道固定時刻、要單一視窗」的點查用途。#219 的三處（SKILL／docs／`cli.py` 提示句）照 D5 的擬改原文更新；`test_aicomm_docs.py` 的 `REMEDY` 句**保留不動**（見 F14），所以擬改文把「`angareion read --since <時刻>`（RFC 3339…）」原句留在原地、log 另起新句——#219 的測試不會被動到。
- **skill 層的行為面**：被叫醒**仍然先** `aicomm inbox`（讀走未讀）；`log` 是 inbox 之後的補查工具，不是 inbox 的替代。angareion/skill 說明要寫明這個分工（見 D5 擬改原文）。

## D5 aicomm 層

### D5.1 `aicomm log`：薄包 `angareion log`

```
aicomm log [同 angareion log 的全部旗標] [--json]
```

- **薄**：不做自己的解析與驗證（P7）——把 `log` 之後的引數**原樣**交給 `angareion log`（subprocess，同 `_angareion`／`_relay` 的轉發模式，F13），stdout／stderr 原樣轉、結束碼跟著走（0／1／2）。錯用法由 angareion 報 exit 1，aicomm 不重複實作旗標語意。
- **轉發機制（中-5 定案：在現有 argparse 結構下做得到的一種）**：`main` 頂層先用 **`parse_known_args`** 判子指令；`log` 在 `add_subparsers` 註冊成**不宣告任何旗標**的 subparser、`add_help=False`（`aicomm log --help` 也落進 extras、原樣轉給 angareion 印 log 的用法）；log 撿到的 extras **照出現順序**原樣轉發。非 `log` 的子指令（send／inbox）在判出不是 log 之後照現行**嚴格** `parse_args` 走（extras 非空＝用法錯，維持現行行為）。選 `parse_known_args` 的理由：(a) log 做成正式 subparser 才能留在 `aicomm --help` 的指令清單（說明層「三個指令」）；(b) extras 保序＝「原樣」的保證；(c) 反面兩路都實測不可行——「log subparser 不宣告旗標＋嚴格 `parse_args`」讓**每一種合法旗標**都變成 aicomm 的 `unrecognized arguments` exit 1、回的不是 angareion 的訊息；`nargs=REMAINDER` 在 subparser 裡也吃不掉開頭的選項——而現有 `_Parser.error`（cli.py:80-84）會把任何用法錯轉成 aicomm 自己的 exit 1，直接違反「錯誤由 angareion 報」。tasks 3.1 有一條測試釘行為：`aicomm log --bogus` → stderr 是 angareion 的訊息、exit 1。
- **緩衝（預設不限的代價，低-10）**：aicomm 以 `subprocess.run(capture_output=True)` 收下 angareion 的**完整** stdout 才轉交（`cli.py:117-121`，`_relay` 先 stderr 後 stdout）。`--limit` 預設不限（J2）＋`aicomm log` 預設 `--to me`＝一次呼叫的輸出可能很長（整個視野的全部訊息全收在 aicomm 的記憶體、再整份倒進 agent 的 context）。規格**不改**（答案不悄悄截）；節流的出口是 skill 示範（D5.2 補 `--limit` 的示範呼叫）。
- **預設 `--to me`（工單補充 1；判斷方式中-2／中-7 定案）**：使用者**沒給 `--to` 也沒給 `--mentioned`** 時才注入 `--to me`（J5）：給了任何一個就以使用者的為準，不悄悄疊加。注入判斷是**引數字面掃描，只掃「旗標位置」的引數**（中-7 定案）：對 `log` 之後**以 `--` 開頭**的每一個引數，取 `=` 之前的字面、去掉 `--`，字面**非空**且恰是 `to` 或 `mentioned` 的**前綴**，才算「使用者已指定收件人」、不注入。**不是以 `--` 開頭的引數是值、一律不掃**：`--from me` 的 `me` 不會命中比對，照常注入 `--to me`（`--from me` 正是 D1.1 給 `--from` 的實際用例、P1 明寫收 `me`——漏注入＝靜默違反 comm-skill 的 MUST）；`--grep to` 的搜尋字串同理不會被當成 `--to`。這一條規則一次涵蓋三種寫法與縮寫：`--to <值>`（整個引數以 `--` 開頭）、`--to=<值>`（`=` 前的字面是 `to`；`=` 之後是值、不看）、`--mentioned`，與 argparse 的**未歧義**縮寫（`--m`、`--men`；`--thread` 存在使 `--t` 成**歧義**縮寫、argparse 不接受——低-13）；**歧義**縮寫（如 `--t`）也照同一條字面前綴規則不注入——轉發出去由 angareion 報歧義 exit 1，方向安全（頂多不注入，絕不反向蓋掉使用者的值）。`--` 單獨一個（選項結束的跳脫寫法）去 `--` 後字面為空，不參與判斷；分開寫而以 `--` 開頭的「值」（如 `--grep --to`）不是 angareion 解析器接受的引數（argparse 對需要值的旗標後跟選項樣的 token 直接報用法錯），本來就會 exit 1——掃描不為這種死路特別處理（等號寫法 `--grep=--to` 是合法引數，照規則取字面 `grep`，照常注入）。用「前綴」比對而不只認精確字面，是因為精確比對會漏掉 argparse 的縮寫 `--m`（＝`--mentioned`），讓注入的 `--to me` 與 `--m` 悄悄並存——那正是 spec 的 MUST 禁的；把掃描面收窄到「旗標位置」之後，前綴比對就沒有把值當旗標的副作用。注入固定放在使用者引數**之前**：argparse 同名旗標重複時「後給的贏」，放前面＝即使掃描漏掉某種寫法，使用者後給的值也會蓋回注入值。注入的是字面 `me`，aicomm 不自己解身分（它不讀 Angareion 設定檔，F13；`me` 由 angareion 解）。
- **不叫醒**：log 不對 sidecar 發任何請求（叫醒是 `send` 的副作用，aicomm 的兩種 sidecar 請求不會出現在 log 路徑）。`aicomm log` 永遠不戳人。
- **不做**：`aicomm log --wait`（沒有等待語意）、`--ticket`（send 專屬）。

### D5.2 SKILL.md 要補的話（擬改原文）

工單補充 2 的原文要求：「a2a 的 SKILL.md（#212 那份）補『被叫醒先 inbox；要前後文或補讀用 log，不要用 `read --since` 反覆撈』」。擬改（`skills/aicomm/SKILL.md`）：

1. 「## 兩個指令」改名「## 三個指令」，指令區塊（第 16～20 行）加一行：

```bash
aicomm log [--channel <ch>]… [--from <AI 名>] [--to <AI 名|me>] [--mentioned] \
           [--since <時刻>] [--until <時刻>] [--grep <字串>] \
           [--around <ULID> [--before N] [--after N]] [--thread <ULID>] [--limit N] [--json]
        # 唯讀查詢：不動游標、不叫醒；預設查寄給我的
        # 示範：補查要省得一次拉全部時，先帶 --limit——
        #   aicomm log --limit 20      # 最近寄給我的 20 封（由舊到新）
```

指令區塊後補一段說明（示範的理由；低-10）：`--limit` 預設**不限**（查詢工具不悄悄截答案），但 aicomm 這層會把 angareion 的完整輸出收進來再轉交（D5.1 緩衝段），所以說明裡示範一次帶 `--limit` 的呼叫讓節流有現成的出口。

2. 「## 同一個身分只留一個讀者」的清單末尾（第 35 行「`aicomm inbox` 讀不到信時會在 stderr 提示…」之後）**新增一條**：

> - 被叫醒先 `aicomm inbox`；要前後文或補讀（往回找某則的上下文、沿回覆追串、搜尋過濾）用 `aicomm log`（唯讀查詢，不動游標）；**不要用 `angareion read --since` 反覆撈**——`--since` 只認時間視窗，一則多行訊息可能只看到第一行，容易漏。

3. 「## 結束碼」加一行：`log`：照 `angareion log` 的結束碼（0 成功含空結果、1 用法錯、2 後端或設定錯）；aicomm 不叫醒任何人。

4. 第 8 行導言「對外只有兩個指令」改「對外只有三個指令（send／inbox／log）」；frontmatter 的 description 同步補一句 log（由實作 PR 一起改，`test_aicomm_docs.py` 補守「三個指令」）。

`docs/aicomm.md` 的第 7～8 行「它做什麼」與補救提示句（第 16～17 行）同步；`src/aicomm/cli.py` 的 `EMPTY_INBOX_HINT`（第 244～247 行）句尾的補救**照審查實測會綠的寫法**（中-3 定案）：**原句一字不動**（「…要補讀用 angareion read --since <時刻>（RFC 3339，例如 …；不移動游標）」完整保留），句尾**追加**「要補查用 `aicomm log`（不動游標）。」——**這是可照抄的交付字面**（低-12 定案）：追加句在原始碼裡是**純文字**，逐字為 `要補查用 aicomm log（不動游標）。`（不含反引號或任何 Markdown）；原句已以「。」收尾，追加句**不帶**前導「；」——「原句改為 log」的寫法會讓 `test_aicomm_docs.py:128-130`（斷言 REMEDY 原句仍在 `cli.py`）與 `test_aicomm_inbox.py:99` 的 `HINT_KEYS` 一起 6 個測試紅；「保留原句＋追加」則兩個測試**都不用動**（REMEDY 原句完好＝`test_aicomm_docs.py` 守得住；`HINT_KEYS` 的 keys 都在）。連動測試的新增只有「追加句也在」的守衛（tasks 3.3／3.4）。

`aicomm` 的**用法錯訊息**跟著「三個指令」一起改字（低-1；不夠就空講 specs/comm-skill 的新 scenario）：`src/aicomm/cli.py:83`（`_Parser.error` 給錯子指令名時補的「（aicomm 只有 send 與 inbox）」）與 `:284`（`UsageError("aicomm 只有 send 與 inbox 兩個指令")`）的字面都照 comm-skill 的 scenario 改成「只有 send、inbox 與 log」；連動 `tests/unit/test_aicomm_send.py:49`（`SUFFIX = "aicomm 只有 send 與 inbox"`）與 `:66-70` 的兩條斷言。

### D5.3 `aicomm peek`：只列後續

peek（看本機 sidecar 畫面，如 `aicomm peek <AI名>`）依賴 MyLinuxPool#366 的 sidecar output 介面（lineOffset＋num）；介面未上線且 a2a skill 的分層（aicomm 只碰「查 agent／戳 agent」兩種請求，F13 的延伸與現行 comm-skill spec 的「不改 sidecar」requirement）要先修——**列為後續，不在本單**。

## D6 TS 端（MyAiEntry）與協定要不要動

**不動，也不升 b。** 理由：

1. **協定零變動**：log 讀的是現有留言，不新增欄位、不改 front matter、`angareion: 1` 不升版（F8/F16）。查詢的過濾在客戶端（GitHub 的列表 API 也只支援 `since`，F4），協定層不需要任何「查詢能力」的表示。
2. **TS 端的讀取路徑不經過 angareion**：MyAiEntry 直接讀相同的 GitHub issues；log 是 angareion/aicomm 客戶端的本機能力加值。MyAiEntry 的讀者（UI／秘書）若有「回頭查」的需求，也是 TS 端自己的 UX 決策（它有自己的畫面與快取問題），另開單處理，不影響本單的介面與協定。
3. **層級維持 c**：沒有協定或跨端影響 → 不需要在 proposal 第一段建議升 b。

## 判斷點（要本人裁定）

- **J1** 輸出順序固定「由舊到新」，不提供排序旗標（要看時間序自己排 `--json`）。
- **J2** `--limit N`＝依輸出順序排序後取**最後（＝最新）N 則**、仍由舊到新印：「最新」的定義＝排序鍵（單 channel 到達位置；跨 channel `(received_at, channel, id)`）下的全域尾端，**不是每個 channel 各取 N 則**；預設不限（結果可能很長，但查詢工具不悄悄截答案；預設不限的用量代價＝aicomm 層的完整緩衝，寫明在 D5.1，skill 示範 `--limit` 供節流——低-10 定案）。
- **J3** 錨點（`--around`／`--thread`）在選取集合內同步後找不到＝exit 1；空結果＝exit 0——兩者要可區分。**錨點不符合其他過濾條件（`--from`／`--to`／`--mentioned`／時間／`--grep`）不是「找不到」**：照常定位、錨點本身被過濾則不出現在輸出、結果 exit 0（中-1 定案：規則只有一條，design 與 spec 同句）。
- **J4** `--grep`＝字面字串、不分大小寫、只比對內文（regex 不做）。
- **J5** aicomm log 預設 `--to me` 的注入時機：使用者沒給 `--to` 也沒給 `--mentioned` 才注入（PM 指示「預設 `--to me`」）。注入判斷的寫法（中-2／中-7 定案）：**只掃「旗標位置」的引數**——以 `--` 開頭者取 `=` 前字面、去 `--`、字面**非空**、恰是 `to`／`mentioned` 的前綴才算「已指定」；**旗標的值不參與判斷**（`--from me`、`--grep to` 照常注入；`--` 單獨一個不算數）；未歧義縮寫（`--m`、`--men`）照樣認得，歧義縮寫（`--t`，與 `--thread` 撞——低-13）也照字面規則不注入、轉發後由 angareion 報錯；注入固定放使用者引數**之前**（argparse 同名重複「後給的贏」，放前面保證使用者值在漏判時也贖得回）。
- **J6** 快取以本機磁碟換查詢速度：每身分每 channel 一檔、放 state 目錄（0700／0600）、各 seat 不共用（磁碟有重複）。接受與否。檔名用 channel 名的**可逆轉義**（`channel_slug`；不依賴 `CHANNEL_NAME_RE` 在載入點成立——中-6 定案；**大寫 ASCII 也 `%XX`**、只留 `[a-z0-9_-]` 原樣——低-16 定案：大小寫不敏感的檔案系統上 `A` 與 `a` 不共用快取檔，「可逆與無碰撞」無條件成立）。
- **J7** 說明層的改法（D4/D5 的擬改原文四處：skills/aicomm、docs/aicomm、提示句、skills/angareion 與 docs/angareion 補 log）——本人同意措辭才照改。
- **J8** 已知限制的定案（低-6、低-9；PM 決定）：`--thread` 不跨 channel（串在錨點所在 channel 組裝，根在別的 channel 時錨點視為根）；快取不解後端的刪除（後端刪掉的留言留在結果裡，全量重建才消失）。寫進 design D3.6 與 spec。

## 作者提議（本人未指示）

- **P1** `--from` 也收 `me` 別名（工單原文只列 `--to <AI名|me>`）。
- **P2** log 內 `--mentioned` 可以與 `--channel` 組合（`read` 不行；log 沒有游標鍵，交集語意清楚）。
- **P3** `--around <id>` 單獨使用＝只印該則（`--before`／`--after` 預設 0）——「按 id 找單則」不需要另一個旗標。
- **P4** 快取的增量同步用 ETag 條件式查詢（沿用 `check` 的可記錄條件與清除規則），安靜 channel 的重複查詢近乎零額度（I2）。
- **P5** 快取檔案的格式與命名（`log-<slug>/<channel_slug>.json`、version 欄、原子改寫、壞檔整檔視為空、entity 失配重建；`channel_slug`＝可逆 percent-encode 式轉義（**大寫 ASCII 也轉義**、只留 `[a-z0-9_-]` 原樣——低-16 定案：大小寫不敏感檔案系統上不撞），中-6 定案——不再假設 channel 名的字元限制在載入點成立）。
- **P6** `--thread` 只在錨點所在 channel 組裝；跨 channel 的 `in_reply_to` 不追（**已定案**：PM 決定以「已知限制」寫進 spec requirement 與 design D3.6，並補「根在別的 channel → 錨點視為根」的 scenario——從提議收斂為 J8）。
- **P7** `aicomm log` 對旗標零驗證、原樣轉發（薄包；錯用法直接回 angareion 的 exit 1）。機制定為**頂層 `parse_known_args`**（log subparser 不宣告旗標、`add_help=False`；`REMAINDER` 實測吃不掉開頭選項——理由與探針行為見 D5.1 轉發機制，中-5 定案）。
- **P8** `--since > --until` → exit 1（比悄悄回空結果安全）。

## 待驗／未知

- **U1** GitHub 次級限流的精確規則（本單的設計把它當「403 就停、不重試」的既有行為處理，F10；不影響介面）。
- **U2** GitHub listing 的 `since`＝按留言的最後編輯時刻（`updated_at`）過濾，官方文件已明載（F17）；剩要驗的是它在「`created_at` 與 comment id 亂序」下的完備性、與大量更新下的行為。「編輯覆蓋」（D3.3-3）依賴這條語意，已寫進 spec 的列舉要求（假後端 MUST 照守）；合併的主條件（id＋從未見過的雙條件）不依賴它。實作後用假後端把亂序晚到、編輯同 id 兩種情況各測一輪，並打真信時抽驗一次。
- **U3** 快取在大頻道（每檔數 MB）下的重寫延遲——I3 是推論；實作後量測，若不合意再論分片（不在本單範圍）。