# sidecar：agent 的生命週期控管 daemon

> change `sidecar-v0`（issue #31）的文件。內容以實作為準（`src/sidecar/` 與 `src/agora/` 的
> headless 模式）。沒有認證、只綁本機、狀態全部在記憶體；不含任何秘密。

## 1. 這是什麼

sidecar 是每台機器（worker 或 Mac）上的一個 daemon，用來控制 AI agent（Claude Code、opencode）
的行程。對外只有**四個動作**，經 HTTP（只綁 `127.0.0.1`，不做認證）：

| 動作 | HTTP |
|---|---|
| 開啟 open | `POST /agents` |
| 關閉 close | `POST /agents/{id}/close` |
| 搜集狀態 status | `GET /agents`、`GET /agents/{id}` |
| 戳 poke | `POST /agents/{id}/poke` |

另外有三種控制形式：立即（直接呼叫上面四個動作）、定時（`POST /jobs` 帶 `at`）、輪詢
（`POST /jobs` 帶 `every`，可設 `until`），job 的動作只能是四個動作之一，另有 `GET /jobs`、
`DELETE /jobs/{id}`。

- **沒有暫停**：「暫停」＝ `poke` 加 `interrupt`（打斷這一輪，再送一句「停下來等指示」）。
- **狀態揮發**：agent 清單、job 清單只在記憶體；sidecar 重啟就從空的開始（對話記憶在 agora，
  要接回就由控制者再 `open` 一次 `continue`）。
- **相依**：sidecar → agora（`agora continue --headless` 開 agent、寫回 Session）、
  sidecar → LLMGateway（拿 subscription；經它的 `lgw connect`）。不依賴任何信道工具，也不依賴 herdr。
- **秘密**：subscription 的 key 只放進 agent 子行程的環境，不上命令列、不進 log、不寫進任何檔案。

## 2. HTTP 介面

請求與回應都是 JSON（`Content-Type: application/json`）。錯誤一律是
`{"error": {"code": "<碼>", "message": "<說明>"}}`。請求本文上限 1 MB（超過回 413）。

### 2.1 agent 物件（status 回傳的形狀）

```json
{
  "id": "t1",
  "agent": "claude-code",
  "session": "agora:01J…",
  "subscription": "claude-personal",
  "state": "running",
  "reopen_at": null,
  "started_at": "2026-10-12T00:00:00Z",
  "exit": null
}
```

- `state`：`starting`／`running`／`idle`／`waiting-quota`／`exited`。
- `session`：目前跑的那個 agora Session id（fork／merge 之後是新的那個，換把後不變）；
  `new` 模式在第一次 import 成功之前是 `null`。
- `reopen_at`：只有 `waiting-quota` 而且有自動重開 job 時才有時刻，否則 `null`。
- `exit`：`{"code": <int|null>, "reason": "<字串>"}` 或 `null`。
- 只列 sidecar 自己開的 agent；不含任何 key。

### 2.2 四個動作

**open**：`POST /agents`

```bash
curl -s 127.0.0.1:8471/agents -X POST -H 'content-type: application/json' -d '{
  "id": "t1",
  "agent": "claude-code",
  "workdir": "/home/me/proj",
  "session": {"mode": "fork", "from": "agora:01J…"},
  "subscription": {"id": "claude-personal"},
  "input": "用 angareion 讀 channel X"
}'
```

- `id`：呼叫方給，唯一；重複而且狀態是 `starting`／`running`／`idle` 時回 409 `id_conflict`
  （什麼都沒開）。`waiting-quota` 或 `exited` 時用同一個 id 重開（取代舊紀錄；`waiting-quota`
  時順帶刪掉它的自動重開 job）。
- `agent`：`claude-code` 或 `opencode`（沒有對應 driver 的種類回 400）。
- `workdir`：必填，存在的絕對路徑；不存在回 400 `workdir_not_found`（sidecar 不建目錄）。
- `session`：`{"mode": "new"}`／`{"mode": "continue", "from": "<agora id>"}`／
  `{"mode": "fork", "from": "<agora id>"}`／`{"mode": "merge", "from": ["<id>", …], "by": "<agent 種類>"}`。
  `by` 只在 merge 有意義（要約由誰寫，預設＝`agent`）。
- `subscription`（選填）：`{"id": "…"}`。省略時 sidecar 自己挑（見第 5 節）；**指定的一把就算在
  冷卻中也照辦**。
- `input`（選填）：第一則輸入；排進佇列等 agent 起來再送，**不會**讓 open 失敗。
- 回應 201 與 agent 物件。

**close**：`POST /agents/{id}/close` → 200 與最後的狀態。先溫和停下 agent（SIGTERM 給 agora
行程，讓它轉給 agent、寫回最後的對話、放鎖），逾時才 SIGKILL。id 不存在回 404 `agent_not_found`；
已 `exited` 的 id 再 close 是 200（idempotent）。`starting` 或正在換把時 close：記下
closing、回 200，轉移的執行緒會把剛啟動的行程停掉並標 `exited`（不會復活）。

**status**：`GET /agents` → `{"agents": [ … ]}`；`GET /agents/{id}` → 一個 agent 物件
（不存在回 404 `agent_not_found`）。只讀記憶體裡最後一次的狀態（最多晚一個 scheduler tick）；
不會因為被查詢而觸發換把或任何網路操作。健康檢查＝`GET /agents` 有回應。

**poke**：`POST /agents/{id}/poke`

```bash
curl -s 127.0.0.1:8471/agents/t1/poke -X POST -H 'content-type: application/json' \
     -d '{"text": "停下來等指示", "interrupt": true}'
```

- `text` 必填；`interrupt` 選填（預設 false）。為真時先打斷這一輪再送；為假時**不**打斷，
  agent 正在跑就排到這一輪之後。
- id 不存在 404 `agent_not_found`；`exited`／`waiting-quota` 回 409 `agent_not_running`；
  `starting` 時排隊到起來再送，逾時（`poke_queue_timeout`，預設 30 秒）回 409 `poke_timeout`。

### 2.3 job（定時與輪詢）

`POST /jobs` 三種寫法（`action` 只能是四個動作之一，`args` 和直接呼叫時相同）：

```bash
# 定時：明天 9:00 開 t1（at 給 epoch 秒或 ISO 8601；在過去回 400）
curl -s 127.0.0.1:8471/jobs -X POST -H 'content-type: application/json' -d '{
  "action": "open",
  "args": {"id": "t2", "agent": "claude-code", "workdir": "/home/me/proj",
           "session": {"mode": "new"}},
  "at": "2026-10-13T09:00:00+09:00"
}'

# 輪詢：每 10 分鐘戳一次，一小時後停
curl -s 127.0.0.1:8471/jobs -X POST -H 'content-type: application/json' -d '{
  "action": "poke", "args": {"id": "t1", "text": "看一下 channel"}, "every": "10m",
  "until": "2026-10-12T09:00:00Z"
}'
```

- `at`：epoch 秒或 ISO 8601；在過去回 400。
- `every`：秒數或 `30s`／`5m`／`2h`／`1d`。下一次執行時刻是**原本節奏上第一個晚於現在的
  預定點**：scheduler 停頓之後只補執行一次，不連續補跑。
- `until`（選填，只在 `every` 時）：過了就不再執行。
- `GET /jobs` → `{"jobs": [ … ]}`，每一筆有 `id`、`action`、`args`、`at`、`every`、`until`、
  `next_run_at`、`auto`（自動重開的 job 為 true）、`last_run`、`last_error`。
  `last_error` 是 `{"status": <HTTP 碼>, "code": "<碼>", "message": "<說明>"}` 或 `null`
  （不含 key）。job 失敗**不自動重試**（自動重開 job 例外，見第 5 節）。
- `DELETE /jobs/{id}` → 204（沒有這個 job 回 404 `job_not_found`）。刪掉某個 agent 的自動重開
  job 時，它的 `reopen_at` 變回 `null`。

### 2.4 錯誤碼表

| HTTP | code | 什麼時候 |
|---|---|---|
| 400 | `invalid_request` | 參數缺漏／型別錯、`at` 在過去、action 不是四個之一、沒有對應 driver 的 agent 種類、不認得的 `session.mode` |
| 400 | `workdir_not_found` | `workdir` 不存在或不是絕對路徑 |
| 400 | `subscription_not_allowed` | 指定的 subscription 不支援這個 agent 種類（LLMGateway 403） |
| 400 | `subscription_not_found` | 指定的 subscription 不存在（LLMGateway 404） |
| 400 | `no_model` | subscription 沒有 `models.default`、`models.allow` 也沒有不含萬用字元的項目 |
| 404 | `agent_not_found` | `GET`／`close`／`poke` 一個沒開過的 id |
| 404 | `job_not_found` | `DELETE` 一個沒有的 job |
| 409 | `id_conflict` | `open` 的 id 還在 `starting`／`running`／`idle` |
| 409 | `not_deployed` | 指定的 subscription 的 key 沒部署（LLMGateway 409） |
| 409 | `no_subscription` | `open` 省略 subscription 而 `list` 裡沒有任何能用的候選 |
| 409 | `no_subscription_for_merge` | merge 的 `by` 種類沒有任何能用的候選 |
| 409 | `agent_not_running` | `poke` 一個 `exited`／`waiting-quota` 的 agent |
| 409 | `poke_timeout` | `poke` 的 agent 在 `poke_queue_timeout` 內沒有起來 |
| 413 | `body_too_large` | 請求本文超過 1 MB |
| 500 | `agora_session_failed` | `agora` 準備 Session 失敗（含逾時） |
| 500 | `internal_error` | 非預期的錯誤（`sidecar` 的 bug；看 log） |
| 503 | `gateway_unreachable` | LLMGateway 連不上（`list`／`get`／`cooldown` 任一；open 不開、不掛 job） |
| 503 | `shutting_down` | sidecar 正在關閉中（`open`／`poke`／`POST /jobs`） |

## 3. 設定檔

`~/.config/sidecar/config.json`（`SIDECAR_CONFIG` 環境變數可覆寫路徑；檔案不存在時全部用預設）。
**不含任何秘密**。

```json
{
  "port": 8471,
  "subscription_source": {"kind": "llmgateway"},
  "gateway_connect_file": "~/.config/sidecar/gateway-connect.json",
  "agents": {
    "claude-code": {"command": "claude", "extra_args": []},
    "opencode": {"command": "opencode", "extra_args": []}
  },
  "sync_period": 5,
  "resume_prompt": "額度恢復了，請接著剛才的工作",
  "retry_interval": 300,
  "poke_queue_timeout": 30,
  "close_timeout": 30,
  "quota_reasons": ["free_tier_limit"]
}
```

| 欄位 | 預設 | 說明 |
|---|---|---|
| `port` | `8471` | HTTP 埠；只綁 `127.0.0.1`。 |
| `subscription_source` | 無 | `{"kind": "file", "path": "<JSON 檔>"}`（本機開發）或 `{"kind": "llmgateway"}`。 |
| `gateway_connect_file` | `~/.config/sidecar/gateway-connect.json` | 見第 4 節。 |
| `agents` | `{}` | 各種類的執行檔路徑與額外參數（值是字串＝只有 command，或 `{"command", "extra_args"}`）。 |
| `sync_period` | `5`（分鐘） | `new` 模式的定期 import 間隔；也傳給 `agora continue --headless --sync-period`。 |
| `resume_prompt` | `額度恢復了，請接著剛才的工作` | 換把／自動重開成功後自動送的提示；空字串＝不送。 |
| `retry_interval` | `300`（秒） | LLMGateway 連不上或換把中途失敗時的重試 job 間隔。 |
| `poke_queue_timeout` | `30`（秒） | `starting` 的 `poke` 排隊上限。 |
| `close_timeout` | `30`（秒） | 溫和停的寬限；逾時才 SIGKILL。也是 driver／store 每個呼叫的內部逾時上限。 |
| `quota_reasons` | `["free_tier_limit"]` | opencode 的額度類 `retry action.reason` 白名單。 |

`subscription_source` 的 `file` 實作讀一個共通 JSON 形狀的檔案（一個陣列，或
`{"subscriptions": [...]}`），每次呼叫重讀；`llmgateway` 實作見第 4 節。

## 4. LLMGateway 的連線設定檔（gateway-connect）

- 內容格式**由 LLMGateway 的 `lgw` 擁有**（`lgw-v0.1.0`：`{"schema": 1, "rendezvous", "jumps"?,
  "identity", "host_keys" 或 "known_hosts", "remote_api"?}`，細節在 `lgw/connect.py` 的
  docstring）。sidecar **不自己解析欄位**。
- sidecar **每一次** LLMGateway 操作都呼叫 `lgw.connect.load_config(<gateway_connect_file>)`
  重新讀取，**不快取**；檔案由外部以原子方式改寫（寫暫存檔再 rename），所以 LLMGateway rotate
  時不必重啟 sidecar。
- 一次邏輯操作（一次 `open`、一次換把）包在同一個 `with lgw.connect.connect(cfg)` 裡，用完就關。
- 讀不到或格式錯誤（`OSError`／`lgw.connect.ConfigError`）＝無法確認：`open` 回 503
  `gateway_unreachable`，換把走 `waiting-quota`＋重試 job；`lgw.connect.connect` 在**進入
  `with` 時**丟的 `Unavailable` 也一樣（不會變成 500）。
- worker 上建議用 inline 的 `host_keys`（整份設定自成一檔），由 MyLinuxPool 的單元從它的
  `gateway.json` 產生（含 sidecar 專用的 `known_hosts`，放 state 目錄、0600）；sidecar 不讀
  MyLinuxPool 的任何檔。Mac 上手寫。
- `lgw-v0.1.0` tag 還沒打，所以 `pyproject.toml` 裡的 `gateway` extra 目前是**註解**；沒裝
  `lgw` 時每次操作回 `Unreachable`（open 503），訊息會說要裝 gateway extra。tag 出來後把
  extra 打開（`gateway = ["lgw @ git+https://github.com/FATESAIKOU/LLMGateway@lgw-v0.1.0"]`）。

## 5. 額度用完：換把與自動重開

- agent 回報額度用完（Claude 只認 `rate_limit_event.status == "rejected"`、
  `errorCode == "credits_required"`、`system/api_retry error: rate_limit`、`result` 文字以
  `You've hit your` 開頭；opencode 只認 `retry` 且 `action.reason` 在 `quota_reasons` 白名單內）時：
- **登記冷卻**：`cooldown(id, window, resets_at)`；`window` 只有 `5h` 與 `1w`。`resets_at` 是
  RFC 3339 UTC；拿不到就不帶（由 LLMGateway 用窗口預設長度）；被以 `invalid_resets_at`／
  `resets_at_out_of_range` 拒絕時，去掉 `resets_at` 重送一次。
- **挑下一把**：`list` → 依 priority（同 priority 以 id 排序）挑第一個現在能用、支援這個
  種類、而且**不是剛用完那一把**的 subscription（不看 `list` 的 available，避免原地打轉）。
  挑到就：溫和停下 agent → 等 agora 寫回 → 用 `continue` 同一個 Session 開新的 subscription
  → 送 `resume_prompt`（id 不變）。
- **全部冷卻**：停下、寫回、狀態標 `waiting-quota`，並自動掛一個 `at`＝最早解除時刻的
  `open` job（同一個 id、`continue` 同一個 Session、**不指定** subscription，成功後送
  `resume_prompt`）。這個 job 在 `GET /jobs` 看得到、可以刪。
  - 候選＝支援這個種類、key 已部署（`available` false 且兩個窗口都沒冷卻＝沒部署，不算）的
    subscription；一把的解除時刻＝它兩個窗口 `until` 中較晚的那個；`at`＝各候選的最小值。
  - 沒有候選時**不掛 job**，`reopen_at` 是 `null`，由控制者決定。
  - 算出的解除時刻已經過了、候選仍不可用時，改掛 `now + retry_interval`（不是每個 tick 重掛）。
  - job 到時 open 失敗（挑不到、409）就重新算再掛一次；LLMGateway 連不上用 `retry_interval`。
- **換把中途失敗**（停下之後的 `SessionStore.start`、`launch` 等丟例外）：`waiting-quota`＋
  `now + retry_interval` 的 job＋log；一個 agent 的錯不影響別的 agent，也不會讓 HTTP 回應變 500。

## 6. `new` 模式（沒有 agora 行程）

`session.mode == "new"` 時 sidecar 直接開 agent（`claude -p …`／`opencode serve …`），沒有
`agora continue` 行程；agent 的 session id 從它的事件取得：

- 每 `sync_period` 分鐘（以及 close、換把時）用
  `agora import session --external-session-id <agent session id> --agent <種類> --add-tag sidecar:<id>`
  存進 agora（再 import 是原地更新）。第一次 import 成功之後 `status.session` 才有值。
- **close**：先 import → 溫和停下 → 再 import 一次（寫回停下之前最後的對話）→ `exited`。
  import 失敗時仍然停下，但 `exit.reason` 會說明沒有寫回。
- **換把**：先 import 拿到 agora id → 停下 → 再 import → 用 `continue` 那個 agora id 接續
  （之後就照一般 headless 路徑走）；import 失敗時**不停下** agent，掛一個
  `now + retry_interval` 的重試 job 並 log。

## 7. 在 Mac 與 worker 上怎麼跑

```bash
uv tool install 'agora @ git+<repo>@<sha>'   # 安裝（repo 與 sha 由部署的 pull 決定）
sidecar --version          # 印安裝的 git sha；從原始碼目錄跑時印版本並註明 install sha unknown
sidecar run                # 起 daemon（讀設定檔；HTTP 只綁 127.0.0.1）
```

- 健康檢查不另加端點：`GET /agents` 有回應就是「在跑」，連不上就是「沒在跑」。
- **worker**：由 MyLinuxPool 的 supervisor 拉起 `sidecar run`。收到 SIGTERM 時 sidecar 會先
  進入「關閉中」（`open`／`poke`／`POST /jobs` 回 503 `shutting_down`，到期的 job 不再執行），
  等 scheduler 的進行中那一次（上限 `close_timeout + 5` 秒，逾時只記 log），再逐一 close
  每個 agent，全部做完才結束。
- **supervisor 的 kill timeout 必須大於 `close_timeout × agent 數`＋一個 scheduler beat**，
  否則 SIGKILL 一來，還沒 close 的 agent 會變成孤兒。
- **v0 只有一個 scheduler 執行緒**：一個 agent 的換把（網路加 stop，可能數十秒）會延遲其他
  agent 的刷新、佇列中的 poke 與 job。這是 v0 接受的取捨。
- 沒有 driver 的 agent 種類：`POST /agents` 回 400（訊息 unsupported agent kind）；
  `GET /agents` 仍然正常（健康檢查會過），`docs/sidecar.md` 這節就是說明這件事。

## 8. 真 HOME 下的注意事項

- **opencode 會合併使用者自己的全域設定**（`~/.config/opencode/opencode.json`）；這次
  **不設** `OPENCODE_PURE`／`OPENCODE_DISABLE_PROJECT_CONFIG`，實際影響由 tasks 3.4 的實機
  測試確認。
- **key 的優先序**：sidecar 一律把 subscription 的 key 放進子行程環境（claude：
  `ANTHROPIC_API_KEY`／`ANTHROPIC_AUTH_TOKEN`／`CLAUDE_CODE_OAUTH_TOKEN`；opencode：
  `OPENCODE_CONFIG_CONTENT`），但真 HOME 下有已儲存的登入（例如 macOS Keychain）時的優先序
  由 tasks 3.4 的實機測試確認。
- 子行程的環境由 driver 以允許清單（`PATH`、`HOME`、`LANG`、`AGORA_*`、`XDG_*` 等）加上
  `env_for` 的輸出組出；sidecar 自己環境裡的 `ANTHROPIC_*`／`OPENAI_*`／`OPENCODE_*` 不會漏進
  子行程。

## 9. 測試

- 單元測試全用假實作（假 driver、假 agora 指令、假 subscription 來源、假 opencode server），
  不開真的 agent、不連網、不碰 Drive：`uv run pytest`。
- **打真 binary 的 loopback 測試**在 `tests/integration/test_sidecar_driver_env_real.py`：
  預設**不跑**（`pytest` 的 testpaths 只有 `tests/unit`），要
  `SIDECAR_REAL_AGENT_TESTS=1 uv run pytest tests/integration/test_sidecar_driver_env_real.py`
  才會跑，而且需要機器上有 `claude`／`opencode`。它用一個 loopback 的假 provider 只記錄
  **布林**（key 是否等於 subscription 的、父行程的 key 有沒有漏），不記錄任何 key 的值。
- `LLMGatewaySource` 另有一個 `pytest.importorskip("lgw")` 的 loopback 測試
  （`tests/unit/test_sidecar_lgw_loopback.py`）：裝了 `lgw`（`gateway` extra）才跑。
