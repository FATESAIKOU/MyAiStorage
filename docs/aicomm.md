# aicomm

AI 之間的溝通指令（工單 MyAiStorage#212；設計在 `openspec/changes/archive/2026-10-25-comm-skill/`，行為規格在 `openspec/specs/comm-skill/spec.md`）。使用說明給 agent 看的是 `skills/aicomm/SKILL.md`，這份記設計上的對照。

## 它做什麼

aicomm 對外只有三個指令：send、inbox、log。

- `aicomm send`：用 `angareion send` 寫入訊息（這就是「送達」），成功後對 `--to` 與 `--mention` 的 AI 各判斷一次要不要叫醒（先問本機 sidecar，查不到再經 `pool-peer` 找 pool 裡的其他機器）。
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
- 叫醒的結果每個收件人剛好一行，本機與跨機共用同一個格式（`<說明>` 只有一行）：

  ```
  wake: <agent> woken local[ interrupt]
  wake: <agent> woken peer:<port>[,<port>…][ interrupt]
  wake: <agent> not-woken <原因碼>[ <說明>]
  wake: <agent> unknown <原因碼>[ <說明>]
  ```

  `woken` 是叫醒了；`not-woken` 是確定沒戳到、或對方明確拒絕；`unknown` 是**可能已經戳到**（例如戳逾時）——看到 `unknown` 不要重送。跨機叫醒成功時是 `woken peer:<port>`，`<port>` 是那一台 sidecar 的埠號（見「對 `pool-peer` 的依賴面」）。
- 叫醒是盡力而為：收件人在本機 sidecar 上是 `idle` 或 `running` 才戳；不在本機時經 `pool-peer` 找 pool 裡的其他機器，找到的那幾台也照同一個條件戳（訊息還是在 Angareion，對方之後 `inbox` 一定讀得到）。`starting`、`waiting-quota`、`exited` 都不戳。查不到人（本機 404、連不上或查詢逾時）時的行為見「叫醒的順序」。
- `--require-wake`：任何一位是 `not-woken` 或 `unknown` 就回 **exit 4**——訊息已送達，**不要重送**；全部叫醒、或沒有人要叫醒（廣播、只寄給自己）回 0。與 `--no-wake` 不能同時給（exit 1，而且在寫進 Angareion 之前就拒絕）。
- 原因碼（與 `src/aicomm/cli.py` 的常數一致；新增要改設計文件並同步這裡）：
  - `not-woken` 的原因碼：`not-found`、`peer-unavailable`、`lookup-incomplete`、`peer-port-out-of-range`、`peer-bad-answer`、`bad-answer`、`bad-name`、`agent-not-running`、`awaiting-answer`、`state-<state>`、`http-<碼>`
  - `unknown` 的原因碼：`poke-timeout`、`poke-error`、`peer-bad-answer`、`internal-error`
  - 其中 `not-found`、`lookup-incomplete`、`peer-port-out-of-range` 與 `peer-bad-answer` 是跨機後援（`pool-peer`）才有的；`peer-unavailable` 是 `pool-peer` 用不了：沒裝或起不來、`find` 回 2（查不了），或找到的那一台 `call` 回 2（連不到）。`state` 不是一個 token 時，本機是 `bad-answer`、別台是 `peer-bad-answer`。
- 連送兩則會戳兩次，沒有去重；自己不會戳自己（見「自己是誰」）。

## 叫醒的順序（本機優先，查不到才跨機）

1. **先問本機 sidecar**（`GET /agents/<id>`，逾時 3 秒）。回 200 就照本機的結果處理：`idle`／`running` 才戳，其他 state 是 `not-woken state-<state>`，回應看不懂是 `not-woken bad-answer`，其他 HTTP 碼是 `not-woken http-<碼>`。這些都**不再查別台**。
2. **只有本機回 404、連不上或查詢逾時**，才用 MyLinuxPool 的 `pool-peer` 找 pool 裡的其他機器（見下節）。這時還沒戳任何人，所以不會重複戳。
3. 本機戳過之後（不論成功、逾時或連線出錯）都**不再查別台**——可能已經戳到了。
4. 收件人的名字以 `-` 開頭時不交給 `pool-peer`（會被讀成選項），直接是 `not-woken bad-name`。

跨機找到的每一台各自判斷：`agent.state` 不是 `idle`／`running` → `not-woken state-<state>`；埠不在 5200–5399 → `not-woken peer-port-out-of-range`，**不呼叫 `call`**；其他都戳，能戳的每一台都戳。同一個收件人在好幾台的結果合併成一行：只要有一台回 200 就是 `woken peer:<port>[,<port>…]`（列成功的埠，由小到大；至少一台成功的戳帶 interrupt 才加 ` interrupt`），否則有一台 `unknown` 就是 `unknown`，都不是就是 `not-woken`（原因碼取埠由小到大的第一個）。不論哪一種都**不重送**。

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
| `POST /agents/{id}/poke`（逾時 5 秒） | 叫醒 | body `{"text": <固定短句>, "interrupt": <bool>}` |

- `state` 是 `idle` 或 `running` 才戳；`starting`、`waiting-quota`、`exited` 不戳。不看 tui 或 headless，不讀 `attach`。
- `interrupt` 預設 false；給了 `--interrupt` 且對方 `running` 才是 true（對 `idle` 的戳本來就不打斷）。
- 回應不符預期（非 JSON、沒有字串的 `state`、不認得的 HTTP 狀態）一律當叫醒失敗、不戳，訊息已送達的結果不變。
- 戳逾時不重送（重送可能變兩份），stderr 是 `wake: <agent> unknown poke-timeout`；其他連線錯誤是 `unknown poke-error`（請求可能已經送出，分不出來）。
- sidecar 若改了這些形狀，aicomm 要跟著改；對照以這張表為準。

## 對 `pool-peer` 的依賴面（跨機叫醒，#368）

本機查不到人時，aicomm 只用 MyLinuxPool 的 `pool-peer` 兩個用法（一律 argv 呼叫，不經 shell）：

| 用法 | 用途 | 讀什麼 |
|---|---|---|
| `pool-peer find <id> --json --timeout 10` | 這個 AI 在 pool 裡哪幾台 sidecar 上 | 結束碼與 JSON 陣列；每一筆只讀 `port`（整數，不是 bool）與 `agent.state`（字串） |
| `pool-peer call <port> POST /agents/<id>/poke --timeout 5` | 叫醒那一台 | 結束碼；rc 0 時 stdout 第一行是 HTTP 狀態碼，之後是 body（與本機戳同一張判讀表） |

- 埠段 **5200–5399**（含）。aicomm 先在這一側擋掉其他埠（`not-woken peer-port-out-of-range`），不會把它和「連不到」（`call` rc 2）混在一起。
- 結束碼：`find` 0＝至少找到一台、1＝每一台都確定沒有、2＝查不了（設定或 Gateway）、3＝找到 0 台但有的台查不到；`call` 0＝有回應（不論 HTTP 碼）、2＝連不到、3＝逾時（結果未知）。約定外的結束碼都是 `peer-bad-answer`。
- aicomm 對子行程另外多等 2 秒（`--timeout` 加 2）當上限，超過就自己收掉子行程：`find` 是 `not-woken lookup-incomplete`、`call` 是 `unknown poke-timeout`。
- `peer:<port>` 是 pool 內 sidecar 的**埠號，不是機器位址**；`wake:` 行的 `<說明>` 只放原因碼（而且只有一行），`pool-peer` 的 stdout／stderr 不會被轉印進去。
- 執行檔：環境變數 `AICOMM_POOL_PEER`（只給測試與手動用）優先，沒設就從 PATH 找 `pool-peer`；找不到或不能執行是 `not-woken peer-unavailable`。這個變數不是 sidecar 位址，也不是使用者可設定的叫醒位址；能打哪些 port 由 `pool-peer` 與上面的埠段決定。

## 位址覆寫（只給測試與手動）

環境變數 `AICOMM_SIDECAR` 可以把位址改成**另一個單一 loopback 位址**（`localhost`、`127.0.0.0/8`、`::1`），測試靠它指到自己起的假 sidecar。sidecar 開的 agent 的子行程環境是白名單，拿不到這個變數，所以正式部署一律是預設位址。

- 只有「需要叫醒」時才檢查：沒有 `--no-wake`，且 `--to` 加 `--mention` 排除自己後還有人。覆寫值不合格就 exit 1，而且是在寫進 Angareion 之前。
- 這個檢查**不能防止跨機叫醒**（轉發埠本身也是 loopback），也不宣稱能。

## 自己是誰

「自己不戳自己」用環境變數 `ANGAREION_IDENTITY` 判斷；aicomm 不讀 Angareion 的設定檔。沒有設這個變數時不會排除任何人。

## 結束碼

見 `skills/aicomm/SKILL.md`。`send` 的 **exit 0 只代表已送達**：訊息已寫進 Angareion，叫醒失敗不改變結束碼；叫醒結果看 stderr 的 `wake:` 行。帶 `--require-wake` 時，有人沒叫醒或結果未知最多回 exit 4，也仍代表訊息已送達（**不要重送**）。

## 版本

`aicomm --version` 印套件版本。指令與說明檔跟 `angareion` 在同一個 distribution，用同一個 git sha 釘。
