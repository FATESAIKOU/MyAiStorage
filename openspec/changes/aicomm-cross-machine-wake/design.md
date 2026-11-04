# #368 設計筆記：跨機叫醒的 aicomm 側（配合 MyLinuxPool `pool-peer`，#677 D5＋#696 擴充）

- 單：#368（MAS，c；上游 MyPMO#299，b，M5 必做）。主設計是 MyLinuxPool PR #677（change `pool-sidecar-mesh`）：各機 sidecar 經 Gateway 互綁，提供 CLI `pool-peer`。本 note 只寫 **aicomm 這一側**。介面以 #677 design §D5 為準，另加 MLP PR #696 架構師留言的 D5 擴充（PMO 2026-11-04 轉達）。
- 作者：專案架構師（初稿 myaistorage-arch-m1-1，2026-11-03；定稿 myaistorage-arch-w1-1，2026-11-04）。
- 開工依據：PMO 2026-11-04 指示（本人授權的傳話）接 #368。PR-1（只改本機）不依賴 `pool-peer`；PR-2 要等 MLP 確認 D5 擴充是定案版才 merge（見第 6 節）。
- 本人 11-03 的方向：不做新服務（先前「每台一個 watcher」的方案作廢）；各機 sidecar 全互通；安全議題記在 MyPMO#297，期 6 再議。

## 1. 現況

`src/aicomm/cli.py` 的 `_wake_one_inner` 流程：先 `GET /agents/<id>` 問本機 sidecar，404 就印「不在本機，不戳」；只戳 `idle`／`running`；`interrupt` 只對 `running` 生效；GET 逾時 3 秒、poke 逾時 5 秒；固定句是「有新訊息：請執行 aicomm inbox。」。`send` 的 exit 0＝已送達。跨機的收件人永遠叫不醒。stderr 是自由格式的中文句子，沒有程式解析它（`pmo` 只讀 stdout 第一行）。

## 2. 介面（aicomm 依賴的 `pool-peer`：#677 D5＋#696 擴充）

```
pool-peer find <agent-id> [--json] [--timeout <秒>]
    → --json：一律是 JSON 陣列 [{"port": <sidecar_port>, "agent": <該台 GET /agents/<id> 的 body 原樣>}, …]
      （同一個 id 在多台時全部列出；沒找到也是陣列，可能是 []）
    → 結束碼：0＝至少找到 1 台（其他台可能有沒查到的，但結束碼不分）
              1＝每一台都確定沒有
              2＝查不了（設定、Gateway 等；沿用 D5，擴充沒有改這一條）
              3＝找到 0 台，而且有查不到的台（預算用完或逾時）：不知道有沒有
pool-peer call <sidecar_port> <METHOD> <path> [--timeout <秒>]   （body 走 stdin，帶 Content-Type: application/json）
    → 目標埠只能在 5200–5399；超出範圍是 rc 2
    → stdout 第一行是 HTTP 狀態碼，之後是 body 原樣
    → 結束碼：0 有回應（不論 HTTP 碼）；2 連不到或埠超出範圍；3 逾時（結果未知）
pool-peer list …   （aicomm 不用；擴充：預算用完時該台印 unknown，整體回 3）
```

aicomm 只用 `find --json` 和 `call … POST /agents/<id>/poke`。`find` 的陣列只用每一筆的 `port`（整數，不是 bool）與 `agent.state`（字串），其他欄位不讀。

## 3. aicomm 的行為（實作歸 #368）

1. **順序**：先問本機 sidecar（照現況）。只有本機回 404、本機 sidecar 連不上，或 GET 逾時（這時還沒戳，不會重複），才執行 `pool-peer find <to> --json --timeout 10`。本機回 200 就照本機結果處理（戳、`state-<state>`，或回應看不懂時 `not-woken bad-answer`），不再查別台。本機回其他 HTTP 碼也一樣，就是 `not-woken http-<碼>`，不查別台。「看不懂」＝ body 不是 JSON 物件、`state` 不是字串，或 HTTP 層讀不懂（`http.client.HTTPException`）；這時還沒戳任何人，所以是 `not-woken`。本機的戳怎麼判讀見第 5 點，本機戳之後無論結果如何都不查別台。
2. **找 pool-peer**：有設環境變數 `AICOMM_POOL_PEER` 就用它（只給測試和手動用；sidecar 開的 agent 的環境是白名單，拿不到這個變數）；沒設就從 PATH 找 `pool-peer`。找不到或不能執行，就是 `not-woken peer-unavailable`，不算錯。一律用 argv 呼叫，不經 shell。名字以 `-` 開頭的收件人不交給 `pool-peer`，直接 `not-woken bad-name`。
3. **讀 `find`**：
   - rc 0：解析陣列，丟掉格式不對的項目，同一個 port 只留一筆。每一筆照第 4 點處理。如果一筆合格的都沒有，就是 `not-woken peer-bad-answer`。rc 0 時其他台可能還沒查到，aicomm 不另外標示：既然已經找到人，只回報找到的那幾台的結果。
   - rc 1 → `not-woken not-found`。
   - rc 2、執行失敗（OSError）→ `not-woken peer-unavailable`。
   - rc 3，或 aicomm 自己的子行程逾時（`--timeout` 加 2 秒）→ `not-woken lookup-incomplete`。這是「不知道對方在不在」，但 aicomm **確定沒有戳任何人**，所以狀態是 `not-woken`，不是 `unknown`。
   - 其他 rc，或 rc 0 但 stdout 不是 JSON 陣列 → `not-woken peer-bad-answer`。
4. **每一台怎麼處理**：`agent.state` 不是 `idle`／`running` 的 → `state-<state>`。port 不在 5200–5399 → `peer-port-out-of-range`，**不呼叫 `call`**（在 aicomm 這邊先擋，這樣這個情況不會和「連不到」都變成 rc 2 而分不出來）。其他的就戳：`pool-peer call <port> POST /agents/<quoted-id>/poke --timeout 5`，stdin 放和本機一樣的 body（固定句＋`interrupt`；只有對方是 `running` 且呼叫端帶 `--interrupt` 時 `interrupt` 才是真）。好幾台都可以戳時全部戳。
5. **讀戳的結果（本機與跨機共用一張表）**：
   - **戳的 HTTP 回應**（本機是 sidecar 的回應；跨機是 `call` rc 0 時 stdout 的第一行與其後的 body）：200 → 這台叫醒了。409：body 的錯誤碼（JSON 物件的 `code`，沒有就 `error`，要是字串）是 `agent_not_running` → `not-woken agent-not-running`，是 `agent_awaiting_answer` → `not-woken awaiting-answer`，其他、沒有或 body 看不懂 → `not-woken http-409`。其他任何碼（含 200 以外的 2xx）→ `not-woken http-<碼>`。本機讀 409 的 body 時，`HTTPError` 的 body 要讀出來（讀不到當空的）。
   - **本機的傳輸結果**：逾時 → `unknown poke-timeout`。其他連線錯誤（`OSError`／`URLError`，含連線被拒、被重置）或 HTTP 層讀不懂（`http.client.HTTPException`）→ `unknown poke-error`：請求可能已經送出，aicomm 分不出來，所以不說 `not-woken`。
   - **跨機的傳輸結果**：`call` rc 0 但第一行不是 HTTP 狀態碼 → `unknown peer-bad-answer`（對方回了東西，可能已經戳到）。rc 2 → `not-woken peer-unavailable`。執行 `call` 失敗（`OSError`）→ `not-woken peer-unavailable`（子行程沒起來）。rc 3 或子行程逾時（加 2 秒）→ `unknown poke-timeout`。其他 rc → `unknown peer-bad-answer`（`pool-peer` 沒照約定回答，不知道送了沒）。
   - 規則：`woken` 只在拿到 200 時說；`not-woken` 只在確定沒戳到、或對方明確拒絕時說；其他都是 `unknown`。不論哪一種都不重送，本機戳之後也不改查別台。
6. **只打兩個路由**：`pool-peer call` 雖然全互通，aicomm 只做 `GET /agents/<id>`（經 `find`）和 `POST /agents/<id>/poke`。poke 的 body 只有 `text` 和 `interrupt`，不帶寄件人、內文或 id。
7. **結果的呈現（叫不醒要看得到，不說謊）**：
   - `send` 的**結束碼不變**：0＝已送達。改成非 0 會讓呼叫端重送，變成重複信。
   - stderr 每個收件人剛好一行，本機和跨機都用同一個格式（取代現在的中文句子）：
     ```
     wake: <agent> woken local[ interrupt]
     wake: <agent> woken peer:<port>[,<port>…][ interrupt]
     wake: <agent> not-woken <原因碼>[ <說明>]
     wake: <agent> unknown <原因碼>[ <說明>]      # 可能已經戳到：不要重送
     ```
     `<說明>` 只有一行，不含換行。同一個收件人戳了好幾台時，照這個順序取一個結果：只要有一台 200 就是 `woken`（列出成功的那幾個 port）；否則只要有一台 `unknown`，就是 `unknown`；都不是就是 `not-woken`，原因碼取 port 由小到大的第一個。
   - 原因碼（只有這些，新增要改這份 design.md 或後續 change，並同步 cli 的常數與文件）：
     - `not-woken`：`not-found`、`peer-unavailable`、`lookup-incomplete`、`peer-port-out-of-range`、`peer-bad-answer`（`find` 的回答看不懂）、`bad-answer`（本機查詢的回應看不懂）、`bad-name`、`state-<state>`、`agent-not-running`、`awaiting-answer`、`http-<碼>`
     - `unknown`：`poke-timeout`、`poke-error`（本機戳送出後連線出錯）、`peer-bad-answer`（`call` 的回答看不懂或 rc 不在約定內）、`internal-error`（`_wake_one` 的兜底：不知道是不是已經戳了）
   - 選用旗標 `--require-wake`：只要有任何一位是 `not-woken` 或 `unknown`，就回 **4**（訊息已送達，不要重送）。沒有人要叫醒時（廣播、只寄給自己）回 0。和 `--no-wake` 同時給是 exit 1，而且在寫進 Angareion 之前就拒絕。不加旗標時行為不變。
8. **預算**：本機 GET 3 秒、本機 poke 5 秒；`find` 10 秒、`call` 5 秒，都用 `--timeout` 傳進去，子行程另外多等 2 秒當上限。收件人一個一個處理。#677 的 R-2 會量 `find` 在整個 pool 的實際耗時，量完再調。

## 4. 不變的

`aicomm inbox`、`aicomm log`、送信本身（`angareion send`）、`--no-wake`、`--interrupt` 的語意、固定句、`AICOMM_SIDECAR` 的 loopback 檢查，都不變。

## 5. 不在本單

- #314（`angareion status`／`aicomm wake`，b，未 merge）的 `aicomm wake`（只戳不送）：**不在 #368 範圍內**。#314 定案後如果要跨機，就重用本單的 `_wake_one`（同一套規則與 `wake:` 格式），另開單處理。
- `pool-peer` 本身、它的埠段、Gateway 的綁定，都歸 MLP。
- 環境說明範本（MyPMO 那邊）的措辭不在本單；本單只改 `docs/aicomm.md` 和 `skills/aicomm/SKILL.md`。

## 6. 待決

- D5 擴充的原文目前是 MLP PR #696 的一則架構師留言（PMO 轉達）。PR-2 merge 前，要由 MLP 確認它已經寫進 #677 design（或 #696 已 merge）。如果有出入，以 MLP 定案的文字為準，再回來改這份 note 的第 2、3 節。
- `find` rc 0 時，陣列裡會不會有「查不到的台」的項目（擴充只說「為 JSON 陣列」）。aicomm 的作法是丟掉格式不對的項目，所以不管有沒有都不會壞；如果 MLP 定了 unknown 項目的形狀，再決定要不要多報。
- `find` 會不會把本機自己的 sidecar 也列進來。aicomm 不知道哪個 port 是本機。本機 404 時，那台本來就不會出現在 `find` 的結果裡；本機逾時時，最壞的情況是同一台被多查一次，不會重複戳。
