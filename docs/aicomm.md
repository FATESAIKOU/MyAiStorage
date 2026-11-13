# aicomm

AI 之間的溝通指令（工單 MyAiStorage#212；設計在 `openspec/changes/archive/2026-10-25-comm-skill/`，行為規格在 `openspec/specs/comm-skill/spec.md`）。使用說明給 agent 看的是 `skills/aicomm/SKILL.md`，這份記設計上的對照。

## 它做什麼

aicomm 對外只有三個指令：send、inbox、log。

- `aicomm send`：用 `angareion send` 寫入訊息（這就是「送達」），成功後對 `--to` 與 `--mention` 的 AI 各判斷一次要不要叫醒（先問本機 sidecar，查不到再讀本機的 sidecar-link 埠清單，直接問那些本機埠上的 sidecar）。
- `aicomm inbox`：用 `angareion read`（要等時先 `angareion wait`）讀寄給自己的信；固定不帶 `--channel`。
- `aicomm log`：薄包 `angareion log`，同參數原樣轉發；使用者沒給 `--to`／`--mentioned` 時預設補 `--to me`；不叫醒任何人、不碰 sidecar；唯讀查詢，不動游標。
- 被叫醒先 `aicomm inbox`；要前後文或補讀用 `aicomm log`（不動游標）；不要用 `angareion read --since` 反覆撈。
- 只用標準函式庫；對 Angareion 只呼叫它的指令列（subprocess），不 import `angareion` 套件；沒有設定檔。

## 送訊息的兩種送法

`send` 寫入訊息後，只對 `--to` 與 `--mention` 的 AI 各戳一次（自己除外；沒給 `--to`／`--mention` 的廣播誰都不戳）。戳的方式有兩種，另加一個只寫不戳：

- **預設（不中斷）**：收件人在本機 sidecar 上是 `idle` 或 `running` 才戳；戳的 `interrupt` 是 false，不打斷對方現在的動作。對方何時讀到，由 sidecar 決定。`starting`、`waiting-quota`、`exited`，或不在本機，都不戳。
- **`--interrupt`（立刻看）**：收件人是 `running` 時，戳的 `interrupt` 為 true，打斷對方現在的動作；收件人是 `idle` 時沒有差別。與 `--no-wake` 不能同時給（exit 1，訊息不送出）。
- **`--no-wake`（只寫不戳）**：訊息照寫進 Angareion，不碰 sidecar；例如「收到」類的回覆。

使用節制：隊員基本上不對 PM、PMO 用 `--interrupt`（見 `skills/aicomm/SKILL.md`）。

不中斷的送法有一個問題：發信方不知道對方什麼時候才會讀到。由發信方判斷：太久沒回就再發一次 `--interrupt`（只用這一次，不是一直重送；隊員不為這件事對 PM、PMO 用 `--interrupt`，改請 PMO 轉達）。

## 送達與叫醒

- **送達＝訊息寫進 Angareion**。寫入成功，叫醒的結果就只寫在 stderr，不改變結束碼（`--require-wake` 除外，見下）：**exit 0 只代表已送達**。stdout 第一行還是訊息 id。
- 叫醒的結果每個收件人剛好一行，本機與跨機共用同一個格式；每一行之後沒有其他文字：

  ```
  wake: <agent> woken local[ interrupt| queued][ record=<records>]
  wake: <agent> woken peer:<port>[,<port>…][ interrupt| queued][ record=<records>]
  wake: <agent> not-woken <原因碼>[ record=<records>]
  wake: <agent> unknown <原因碼>[ record=<records>]

  <records> := <rec>[,<rec>…]
  <rec>     := local/<id> | peer:<port>/<id>
  <id>      := [A-Za-z0-9_-]{1,64}
  ```

  `woken` 是叫醒了；`not-woken` 是確定沒戳到、或對方明確拒絕；`unknown` 是**可能已經戳到**（例如戳逾時）——看到 `unknown` 不要重送。跨機叫醒成功時是 `woken peer:<port>`，`<port>` 是本機 sidecar-link 埠清單裡的一個埠號（見「對 sidecar-link 的依賴面」）。
- `woken … queued`：對方忙（對方正在跑），這一則排在它這一輪之後才會被處理；文字已經交出去，`queued` 不要重送。完整樣子是 `wake: <agent> woken local queued record=local/<id>`（跨機是 `woken peer:<port> queued`）。`interrupt` 與 `queued` 互斥：帶 `--interrupt` 的戳不會排隊，同時回報時以 ` interrupt` 為準。`queued` **不是原因碼**。
- 對方在提問框裡（409 `awaiting-answer`）也是「忙」，但 sidecar 對這種**不送、不排隊**，所以維持 `not-woken awaiting-answer`，不是 `queued`。
- `record=` 是那一戳在 sidecar 的紀錄：本機是 `local/<id>`，跨機是 `peer:<port>/<id>`；多台依 port 由小到大以逗號串接，失敗的那幾台也會列。只在至少拿到一個合格 id 時才寫，所以沒有 id 的行和舊版一字不差。`woken` 只代表 sidecar 收下了，**不代表對方已讀**（那要用紀錄的 `confirmed_at` 查）。
- 叫醒是盡力而為：收件人在本機 sidecar 上是 `idle` 或 `running` 才戳；不在本機時讀本機的 sidecar-link 埠清單，對清單上每一台（跳過本機自己的埠）直接查、直接戳，能戳的那幾台也照同一個條件（訊息還是在 Angareion，對方之後 `inbox` 一定讀得到）。`starting`、`waiting-quota`、`exited` 都不戳。查不到人（本機 404、連不上或查詢逾時）時的行為見「叫醒的順序」。
- `--require-wake`：任何一位是 `not-woken` 或 `unknown` 就回 **exit 4**——訊息已送達，**不要重送**；全部叫醒（含 `woken … queued`）、或沒有人要叫醒（廣播、只寄給自己）回 0。與 `--no-wake` 不能同時給（exit 1，而且在寫進 Angareion 之前就拒絕）。
- 原因碼（與 `src/aicomm/cli.py` 的常數一致；新增要改設計文件並同步這裡）：
  - `not-woken` 的原因碼：`not-found`、`peer-unavailable`、`lookup-incomplete`、`peer-port-out-of-range`、`peer-bad-answer`、`bad-answer`、`bad-name`、`agent-not-running`、`awaiting-answer`、`state-<state>`、`http-<碼>`
  - `unknown` 的原因碼：`poke-timeout`、`poke-error`、`peer-bad-answer`、`internal-error`
  - 其中 `not-found`、`lookup-incomplete`、`peer-port-out-of-range` 與 `peer-bad-answer` 是跨機後援（sidecar-link）才有的；`peer-unavailable` 是查不了：心跳或埠清單不可用（沒有 sidecar-link、心跳不新鮮、清單空或扣掉本機埠後沒有埠），或某一台的戳連線被拒。清單只要有一行不合格就整份不用：某行是整數但不在 1–65535 是 `peer-port-out-of-range`，格式不對是 `peer-bad-answer`（同時有好幾行時，依檔案順序第一個不合格的行決定）。`state` 不是一個 token 時，本機是 `bad-answer`、別台是 `peer-bad-answer`。
- 連送兩則會戳兩次，沒有去重；自己不會戳自己（見「自己是誰」）。
- **回報一律帶 `--to`，送完看 stderr 的 `wake:` 行**：廣播不叫醒任何人；要對方動作的回報（完成、提問）必須帶 `--to`。看到什麼就怎麼做：
  - `woken`：不用再做。
  - `woken … queued`：對方忙，這一輪結束就會看到；急的話下一則用 `--interrupt`（仍然照軟規則，隊員不中斷 PM／PMO），`queued` 不要重送。
  - `not-woken`：信仍在 Angareion，對方之後 `inbox` 讀得到；若需要對方動作，另外告知 PM／PMO（例如對方沒開席位）。
  - `unknown`：不要重送。
- 對照 sidecar 的紀錄：用 `record=` 的位置和 id 直接查——本機 `curl -s 127.0.0.1:8471/pokes/<id>`，跨機同一個路由 `curl -s 127.0.0.1:<port>/pokes/<id>`。查回 404 `poke_not_found` 是紀錄已過期或沒有留下（沒查到 agent 的 404／400／413 只放在 1000 筆的小緩衝，很快就查不到），**不代表沒叫醒**。

## 叫醒的順序（本機優先，查不到才跨機）

1. **先問本機 sidecar**（`GET /agents/<id>`，逾時 3 秒）。回 200 就照本機的結果處理：`idle`／`running` 才戳，其他 state 是 `not-woken state-<state>`，回應看不懂是 `not-woken bad-answer`，其他 HTTP 碼是 `not-woken http-<碼>`。這些都**不再查別台**。
2. **只有本機回 404、連不上或查詢逾時**，才讀本機的 sidecar-link 目錄（`heartbeat` 與 `ports`），對清單上的每個本機埠直接查、直接戳（見下節）。這時還沒戳任何人，所以不會重複戳。
3. 本機戳過之後（不論成功、逾時或連線出錯）都**不再查別台**——可能已經戳到了。
4. 收件人的名字以 `-` 開頭時的行為不變：直接是 `not-woken bad-name`（原本是為了不讓它被當成舊跨機指令的選項；改成直連後技術上不需要，但改它會動 `wake:` 行的行為，另案處理）。

清單上的每一個埠各自查、各自判斷：回 404 是這台確定沒有；回 200 且 `state` 是 `idle`／`running` 就戳；回 200 但讀不懂（body 不是 JSON 物件、`state` 不是字串或不是一個 token）是 `not-woken peer-bad-answer`；其他 state 是 `not-woken state-<state>`；連不上、逾時、其他 HTTP 碼或查詢預算用完都算「查不到」。能戳的每一台都戳。同一個收件人在好幾台的結果合併成一行：只要有一台回 200 就是 `woken peer:<port>[,<port>…]`（列成功的埠，由小到大；至少一台成功的戳帶 interrupt 才加 ` interrupt`），否則有一台 `unknown` 就是 `unknown`，都不是就是 `not-woken`（原因碼取埠由小到大的第一個）。清單上每一台都確定 404 才是 `not-found`；有查不到（連不上、逾時、預算用完）的台就是 `lookup-incomplete`。不論哪一種都**不重送**。

## 參照：`--ticket` 與 `--ref`（#278）

`send` 附參照一律走 Angareion 的 `--ref`，不改訊息內文、不去讀被指到的東西：

- `--ticket <repo>#<n>`：一個 `--ref ticket=<repo>#<n>`（格式 `^[A-Za-z0-9._-]+#[0-9]+$`，不符 exit 1）。
- `--ref <name>=<locator>`（可重複，例 `--ref pr=https://github.com/...` 附 PR 連結）：原樣一個 `--ref <name>=<locator>`；值裡沒有 `=` 就 exit 1，而且在寫進 Angareion 之前就拒絕（跟 `--ticket` 一樣，不會送出一則才說失敗）。
- 兩者並用都送：`--ticket` 的 ref 在前，其他 `--ref` 照給的順序。
- 每個值都是一個 `--ref=...` token，值以 `-` 開頭也不會被當成旗標。
- 叫醒不變：**`aicomm send --ref` 仍然會叫醒**收件人，判斷只看 `--to`／`--mention`／`--no-wake`／`--interrupt`；與 `angareion send --ref` 不叫醒不同（angareion 那一層不碰 sidecar）。

## 新身分第一次 `inbox`（#224）

`aicomm inbox` 固定用「寄給我的」讀信，所以新開的 agent（游標檔裡還沒有任何一筆「寄給我的」游標）第一次讀時，Angareion 只把**寄給自己的**（`to` 是自己，或點名自己）從頭讀；**沒有 `to` 的舊廣播**只讀「現在」之後的，不會把各頻道開機前留下的廣播全部倒進 inbox。要看舊廣播就自己用 `angareion read --since <時刻>`（或 `angareion log`），`aicomm` 沒有為它加旗標。

- 派工不受影響：派工是「先 send 再 open」，那則訊息 `to` 是自己，開機後第一次 `inbox` 讀得到。
- `inbox --wait` 的 `wait` 不動游標、也不套這條：它可能因為一則舊廣播而 exit 0，接著的 `read` 卻什麼都沒有（stderr 會印「沒有新信」的提示）；補讀一樣用 `--since`。
- `--mentioned` 與 `angareion read --channel` 的第一次讀行為沒改。

## 同一個身分只留一個讀者（#219）

`angareion read` 讀走信就推進游標，所以同一個身分有兩個讀者時，先讀的拿走信，另一個看到空的（#212 驗收前的調查：背景的 `wait → read` 迴圈搶先讀走，被 poke 叫醒的前景 `inbox` 是空的）。`aicomm inbox --wait` 在 wait 之後會接著 read，所以放到背景一樣會讀走信，不算「只等不讀」。

- 軟規則：被 sidecar 管的 agent（會被 poke 叫醒）不跑背景讀信迴圈，被叫醒就在前景 `aicomm inbox`；沒有人會 poke 的，才用 `aicomm inbox --wait` 當唯一的讀者。
- 補救：被叫醒但 inbox 是空的 → 先看自己有沒有背景讀信任務，它的輸出裡可能已經有那一則；要補讀用 `angareion read --since <時刻>`（RFC 3339，例如 2026-10-25T03:00:00Z；不移動游標）。
- 提示：`aicomm inbox` 的 read 成功（exit 0）但 stdout 是空的（`--json` 時是 `[]`），就在 stderr 加一行同樣的補救；exit 0 與 stdout 不變。`--wait` 逾時（exit 3）與後端錯（exit 2）不出提示。

## 對 sidecar 的依賴面

aicomm 只對 sidecar 發兩種請求，位址固定為 sidecar 文件的預設 loopback 位址 `http://127.0.0.1:8471`：

| 請求 | 用途 | 讀什麼 |
|---|---|---|
| `GET /agents/{id}`（逾時 3 秒） | 這個 AI 在不在這台、狀態是什麼 | 200 的 `state`；404 ＝不在這台 |
| `POST /agents/{id}/poke`（逾時 5 秒） | 叫醒 | body `{"text": <固定短句>, "interrupt": <bool>, "via": "aicomm", "ref": <訊息 id>?}`；回應（任何 HTTP 碼）可選讀頂層 `poke_id`，200 另讀 `poke_outcome`（都沒有＝舊版 sidecar） |

- 戳的回應會讀兩個選用欄位（MyLinuxPool#786，poke-records-v1）：`poke_id` 只在是字串且完全符合 `[A-Za-z0-9_-]{1,64}` 時採用，否則當沒有；200 的 `poke_outcome` 只有值是 `"queued"` 才算「對方忙（排隊）」。這兩個欄位**不改**上面判讀的種類與原因碼，也**不加發**任何請求。409 的錯誤碼先讀巢狀的 `error.code`，再讀頂層的 `code`、頂層字串的 `error`（第一個是字串的定案；都不認得是 `http-409`）。

- 戳的 body 多帶兩個選用的中繼資料（MyLinuxPool#786 D3，#413），只給 sidecar 寫進叫醒紀錄，**不貼進對方的輸入**（`text` 仍是固定短句，一個字都沒變）：`via` 是誰戳的（本機 `aicomm`，跨機 `aicomm-peer`），是自己宣告的來源，只當線索、不當證據、不能拿來授權；`ref` 是這則訊息的 id（`angareion send` stdout 第一行去頭尾空白），只在完全符合 `[A-Za-z0-9_-]{1,64}` 時才帶，不合格就整個鍵不出現、照樣戳一次（不另印訊息）。同一次 `send` 的所有收件人與所有機器帶同一個 `ref`。這個 `ref` 不是 `send --ref`（那個的值不會進戳的 body）。舊的 sidecar 會忽略這兩個鍵；查叫醒紀錄時可用紀錄裡的 `ref` 對照訊息 id。
- `state` 是 `idle` 或 `running` 才戳；`starting`、`waiting-quota`、`exited` 不戳。不看 tui 或 headless，不讀 `attach`。
- `interrupt` 預設 false；給了 `--interrupt` 且對方 `running` 才是 true（對 `idle` 的戳本來就不打斷）。
- 回應不符預期（非 JSON、沒有字串的 `state`、不認得的 HTTP 狀態）一律當叫醒失敗、不戳，訊息已送達的結果不變。
- 戳逾時不重送（重送可能變兩份），stderr 是 `wake: <agent> unknown poke-timeout`；其他連線錯誤是 `unknown poke-error`（請求可能已經送出，分不出來）。
- sidecar 若改了這些形狀，aicomm 要跟著改；對照以這張表為準。

## 對 sidecar-link 的依賴面（跨機叫醒，#457）

本機查不到人時，aicomm 讀本機的 sidecar-link 目錄，直接對清單上的本機埠查、戳：沒有子行程、沒有外部指令。

| 檔案 | 內容 | aicomm 讀什麼 |
|---|---|---|
| `heartbeat` | 寫入那一輪的行程 pid；mtime 是最近一輪 | 「新鮮」＝mtime 與現在的差在 ±90 秒（`LINK_FRESH_SECONDS`）內，**而且** pid 合格並活著（`os.kill(pid, 0)`；pid 必須符合 `^[1-9][0-9]*$` 且不大於 2147483647） |
| `ports` | 每行一個十進位埠，遞增、不重複 | 每行必須符合 `^[0-9]{1,5}$` 且 1–65535；重複的埠只留第一次；處理時由小到大 |

- 目錄：`~/.mylinuxpool/sidecar-link/`。測試與手動用 `AICOMM_SIDECAR_LINK` 換成別的目錄（只決定**讀哪個目錄**，不是 sidecar 位址，也不是使用者可設定的叫醒位址）；設了但是空字串或不是絕對路徑＝沒有目錄（查不了），不是用法錯誤（它在訊息送達之後才讀，不改結束碼）。
- 對清單上的每個埠 P（**跳過與本機 sidecar 同一個埠**；本機埠由 `AICOMM_SIDECAR` 的位址算，沒有埠時用 80，只比埠號不比主機）只打兩個路由：`GET http://127.0.0.1:P/agents/<id>`（逾時 3 秒）與 `POST http://127.0.0.1:P/agents/<id>/poke`（逾時 5 秒）；`<id>` 先 URL quote。戳的 body 與本機同一個組法，`via` 是 `aicomm-peer`。
- 查詢有總預算 10 秒（`PEER_LOOKUP_BUDGET`，用單調時鐘計）；每個埠的逾時是 3 秒（`PEER_GET_TIMEOUT`）與「剩下的預算」中較小的一個，用完還沒查的埠一律算「查不到」。依埠號由小到大**依序**查，不平行。
- 「查不了」＝`not-woken peer-unavailable`，**不改走別的路徑**：沒有目錄、心跳不新鮮（讀不到、mtime 超過 90 秒或遠在未來、pid 讀不懂或不在）、清單讀不到或空檔、扣掉本機埠後一個埠都不剩。不退回舊的跨機指令、不讀別的檔、不試非 loopback 位址，也不快取（每位收件人都重新讀）。
- 清單的錯誤是整份的：只要有一行不合格，這台機器上所有跨機收件人都拿到同一個原因碼，不會跳過壞行用剩下的。不是 UTF-8、有空行、某行不是 `^[0-9]{1,5}$`＝`not-woken peer-bad-answer`；某行是整數但不在 1–65535＝`not-woken peer-port-out-of-range`。同時有好幾個不合格的行時，依檔案順序第一個不合格的行決定。只允許最後一個換行造成的一個空字串（去掉它之後是空的＝空檔）。
- `peer:<port>` 是 sidecar-link 清單裡的一個**本機埠號，不是機器位址**；`wake:` 行只放種類、原因碼、驗證過的 `state` token、` interrupt`／` queued` 與 `record=`（而且只有一行）。sidecar 回應的 body 不會被轉印進去，清單內容與埠以外的東西也不會寫進 stderr／stdout。
- 只連 loopback：目標一律 `127.0.0.1:P`，proxy 一律關掉。aicomm 對每一台 sidecar 只有那兩個路由的請求，不發 `GET /pokes/…`，也不發 `/agents`（清單）。

## 位址覆寫（只給測試與手動）

環境變數 `AICOMM_SIDECAR` 可以把位址改成**另一個單一 loopback 位址**（`localhost`、`127.0.0.0/8`、`::1`），測試靠它指到自己起的假 sidecar。sidecar 開的 agent 的子行程環境是白名單，拿不到這個變數，所以正式部署一律是預設位址。

- 只有「需要叫醒」時才檢查：沒有 `--no-wake`，且 `--to` 加 `--mention` 排除自己後還有人。覆寫值不合格就 exit 1，而且是在寫進 Angareion 之前。
- 這個檢查**不能防止跨機叫醒**（轉發埠本身也是 loopback），也不宣稱能。

## 自己是誰

「自己不戳自己」用環境變數 `ANGAREION_IDENTITY` 判斷；aicomm 不讀 Angareion 的設定檔。沒有設這個變數時不會排除任何人。

## 結束碼

見 `skills/aicomm/SKILL.md`。`send` 的 **exit 0 只代表已送達**：訊息已寫進 Angareion，叫醒失敗不改變結束碼；叫醒結果看 stderr 的 `wake:` 行。`woken … queued` 算叫醒（`--require-wake` 下也是 0）。帶 `--require-wake` 時，有人沒叫醒或結果未知最多回 exit 4，也仍代表訊息已送達（**不要重送**）。

## 版本

`aicomm --version` 印套件版本。指令與說明檔跟 `angareion` 在同一個 distribution，用同一個 git sha 釘。
