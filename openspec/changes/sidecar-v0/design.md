## Context

本人 2026-10-11 定案設計提案 v4（`docs/proposals/aicompany-design-v4.md`，本人對 Q1～Q8 的回答在該檔的「本人的回答（10-11）」）。sidecar 是 agent 的生命週期控管：控制者用 HTTP 下四種指示，sidecar 用子行程開 agent，用 agora 繼承與寫回 Session，用 LLMGateway 拿 subscription。

2026-10-12 的交叉核對（`docs/review/v4-consistency.md`）與 PM 的決定已折進本文件。

## Goals / Non-Goals

**Goals：** 對外只有四個動作；介面和實作分開（agent 種類、Session 保存處、subscription 來源、挑選規則都可以替換）；相依少；Mac 與 worker 同一份程式。

**Non-Goals：** 任何信道工具的收發（各實體自己做）；認證（只綁本機）；重啟後找回（狀態揮發）；同時租用上限、分辨是哪台 worker（v0 不做）；herdr。

## Decisions

### D1 對外：四個動作＋三種形式（HTTP，只綁 127.0.0.1）

| 動作 | HTTP | 參數 | 回傳 |
|---|---|---|---|
| open | `POST /agents` | `id`（呼叫方給，唯一）、`agent`（`claude-code`／`opencode`）、`workdir`（必填；存在的絕對路徑，不存在回 400，sidecar 不建目錄）、`session`（`{"mode": "new"}`／`{"mode": "continue", "from": <agora id>}`／`{"mode": "fork", "from": …}`／`{"mode": "merge", "from": [...], "by"?: <agent>}`；`by` 是寫要約的 agent 種類，預設＝`agent`）、`subscription`（選填，`{"id": …}`；省略時由 sidecar 用 `Selector` 挑）、`input`（選填，第一則輸入） | 201 與 agent 的狀態。id 已存在而且狀態是 starting／running／idle 時回 409；狀態是 waiting-quota 或 exited 時，用同一個 id 重開（取代舊的紀錄；waiting-quota 時順帶刪掉它的自動重開 job）。錯誤見 D4 的表 |
| close | `POST /agents/{id}/close` | — | 先溫和停下 agent，再把最後的對話寫回 agora；逾時才強制結束。回最後的狀態 |
| status | `GET /agents`、`GET /agents/{id}` | — | 只列它開的：`id`、`agent`、`session`（目前跑的那個 agora id；fork／merge 之後是新的那個，換把後不變）、`subscription`（id）、`state`（`starting`／`running`／`idle`／`waiting-quota`／`exited`）、`reopen_at`（只在 `waiting-quota` 而且有自動重開 job 時，否則 null）、`started_at`、`exit`；不含 key |
| poke | `POST /agents/{id}/poke` | `text`、`interrupt`（選填，預設 false） | 送一則輸入；`interrupt` 先打斷這一輪。id 不存在回 404；`exited`／`waiting-quota` 回 409（不排隊）；`starting` 時排隊，起來之後再送，逾時（設定 `poke_queue_timeout`，預設 30 秒）回 409 |

- 定時：`POST /jobs {"action", "args", "at"}`；輪詢：`POST /jobs {"action", "args", "every", "until"?}`；`GET /jobs`、`DELETE /jobs/{id}`。`action` 只能是上面四個之一（否則 400），`args` 和直接呼叫時相同；`at` 在過去回 400。
- `GET /jobs` 每筆有 `id`、`action`、`args`、`at` 或 `every`／`until`、`last_run`（時刻）、`last_error`（錯誤碼與訊息，不含 key；沒有錯誤是 null）。job 失敗不自動重試，除了 D4 的自動重開 job。
- 只綁 `127.0.0.1`（埠由設定給），不做認證（本人 Q5）。遠端控制者自己用隧道連進來。

### D2 揮發的狀態與結束

agent 清單、job 清單只在記憶體。sidecar 收到 SIGTERM 時，先對每個 agent 做 close（停下、寫回 agora），全部做完才結束；被強制殺掉就什麼都不留。重啟從空的開始，不找回（本人：不用管重啟）。因為重啟會殺掉它開的 agent（子行程），設定的改變一律不靠重啟或 SIGHUP（見 D7 的連線設定）。

### D3 內部介面（介面和實作分開）

| 介面 | 動作 | 實作 |
|---|---|---|
| `AgentDriver` | `env_for(subscription) → env、unset`、`launch(session, env, workdir) → handle`、`send(handle, text)`、`interrupt(handle)`、`state(handle) → running／idle／exited／quota_exhausted(window, resets_at?)`、`stop(handle, graceful)` | `ClaudeCodeDriver`、`OpenCodeDriver` |
| `SessionStore` | `start(mode, ids, by, agent, workdir, tag, env) → (session_id, process)`（`agora continue … --headless`；`merge` 先 `agora merge`；`new` 見 D5）。**沒有 `sync`**：寫回是 agora 行程自己在定期與結束時做的，sidecar 只等它退出 | `AgoraSessionStore`（呼叫 `agora`） |
| `SubscriptionSource` | `list()`、`get(id, agent)`、`cooldown(id, window（5h／1w）, resets_at?)`；每個方法回結果或「無法確認」（unreachable），不丟例外 | `LLMGatewaySource`（經 LLMGateway 的 `lgw connect`）、`FileSubscriptionSource`（本機開發：讀一個 JSON 檔） |
| `Selector` | `choose(agent, subscriptions, exclude=[]) → id 或 None`；輸入是整份 `list`（含不可用的），實作自己過濾 | `PrioritySelector`：`available` 且 `agents` 含這個種類、不在 `exclude` 裡，照 `priority` 取第一個，相同時以 `id` 排序（結果確定）。v0 只有 priority，替換靠改程式（本人 Q8）；要換成外部注入的函式時 sidecar 本體不改 |

- 「subscription 要變成 agent 的哪些環境變數」與「怎麼看出額度用完」都是 `AgentDriver` 的知識（和 LLMGateway PM 一致：共通 JSON 裡沒有 `launch`）。driver 自己解析事件；`lgw.signals` 只是選用，**不是義務**（driver 看到的是 CLI 事件不是 HTTP 回應，多半用不到）。
- 環境變數只放在子行程的環境，不上命令列，不寫進任何 log 或檔案。
- agent 種類 `claude-code`／`opencode` 對 agora 的 `--agent` 是 `claude`／`opencode`，對應表在 `AgoraSessionStore`。

### D4 subscription 與換把

LLMGateway 的共通 JSON 欄位以 LLMGateway 的 `gateway-v0` 規格（`specs/subscription-registry`）為準：`schema`／`id`／`provider`／`kind`／`agents`／`priority`／`models`／`endpoint`／`key`／`cooldown`／`available`。`schema` 比 sidecar 認得的更高時拒絕並說明，不默默解析。

**open 的流程**：有給 `subscription` 就用它的 id（**指定的在冷卻中也照辦**，由呼叫方決定；一開起來用完就走下面的換把）；沒給就 `list` → `Selector.choose`。然後 `get(id, agent)` → `AgentDriver.env_for` →（merge 時先做要約，見 D5）→ `SessionStore.start` → `launch`。

**LLMGateway 的錯誤怎麼處理**

| 情況 | open（控制者看到的） | 換把或自動重開途中 |
|---|---|---|
| `get` 403 `agent_not_allowed` | 400 `subscription_not_allowed`，什麼都沒開 | 當作這一把不能用，換下一個候選 |
| `get` 404 | 400 `subscription_not_found` | 同上 |
| `get` 409 `not_deployed` | 呼叫方指定的：409 `not_deployed`；`Selector` 挑的：換下一個候選，全部不行才回 409 | 換下一個候選；沒有就走「沒有」那一支 |
| unreachable（`list`／`get`／`cooldown` 任一個） | 503 `gateway_unreachable`，什麼都沒開、不掛 job、不重試 | `stop`＋寫回、`waiting-quota`，掛一個 `now + retry_interval`（設定，預設 5 分鐘）的 open job；到時還是 unreachable 就再掛一次 |

404 刻意不用在 subscription 不存在上：`/agents/{id}` 的 404 已經是「沒有這個 agent」。

**window 與 resets_at**

- `window` 只有 `5h` 與 `1w` 兩種（和 LLMGateway 一致）。`resets_at` 選填：driver 拿不到時間就不帶（LLMGateway 會用窗口的預設長度），不要自己猜。
- `resets_at` 一律在 driver 的出口轉成 RFC 3339 UTC（Claude 是 epoch 秒、opencode 是 epoch 毫秒）。被 LLMGateway 400 拒絕（過去或超過 8 天）時，去掉 `resets_at` 重送一次。

**換把**（driver 回報 `quota_exhausted(window, resets_at?)`）

1. `cooldown(id, window, resets_at?)`。登錄失敗也繼續，但**這一輪換把把剛用完的 id 排除在候選之外**（`Selector.choose(..., exclude=[id])`），不依賴 `list` 的 `available`，避免挑回同一把原地打轉。
2. `list` → `Selector.choose`：
   - 有：`get` → `stop`（溫和）→ 等 agora 行程退出（它在結束時寫回）→ `SessionStore.start(continue 同一個 Session)` → `launch`；id 不變；成功後送一則 `resume_prompt`（設定，預設「額度恢復了，請接著剛才的工作」；設成空字串就不送）。
   - 沒有：`stop`（溫和）→ 等 agora 行程退出；`state = waiting-quota`，並自動掛一個 job（算式如下）。
3. 「停下」與「寫回」的順序一律是：先溫和停下 agent，再讓 agora 把最後的對話寫回（close、換把、全部冷卻都一樣）；要等 agora 行程退出才能再 `continue`（同一個 Session 的鎖）。

**自動重開 job**（不是新的對外動作；`GET /jobs` 看得到、可以刪）

- `action = open`，`args` 是同一個 id、同一個 agent 種類與工作目錄、`session = continue 目前跑的那個 Session`，**不帶 `subscription`**（到時由 `Selector` 重新挑）、不帶 `input`（成功後送 `resume_prompt`）。
- `at` 的算式：候選＝`agents` 含這個種類、**key 已部署**的 subscription（`available` 為 false 但兩個 `cooldown` 都是 null 的，就是沒部署，不算）；每一把的解除時刻＝它兩個窗口 `cooldown.*.until` 中非 null 的最大值（`until` 已含 LLMGateway 的 2 分鐘，不再加）；`at`＝各候選解除時刻的最小值。這個時刻只看 `agents` 與 `cooldown`，不經 `Selector`。
- 邊界：沒有任何候選時不掛 job，`waiting-quota`、`reopen_at` 是 null，由控制者決定；job 到時 open 失敗（挑不到、409）就重新算再掛一次，unreachable 用 `retry_interval`；控制者 `DELETE` 了這個 job，`reopen_at` 變回 null。

### D5 Session 與 tag

- open 的 `session.mode`：`continue`、`fork`、`merge` 都經 `agora continue … --headless`（`merge` 先用 `agora merge --agent <by>` 做出新的 Session 再 continue；摘要是呼叫模型的，所以要約的 env 一律來自 subscription，不用機器上自己的登入）。
  - `by` 與 `agent` 同種類：用 open 的那把 subscription 的 env。
  - `by` 與 `agent` 不同種類：替 `by` 的種類另外用 `Selector` 挑一把（`list` → `choose(by, …)`），經 `get(id, by)` 取得，用 `by` 的 driver 的 `env_for` 跑 `agora merge --agent <by>`；open 指定的 subscription 只用在 `agent`。錯誤同 D4 的表（403／404／409／unreachable 都使 open 失敗、什麼都沒開）；`by` 的種類沒有任何能用的候選時回 409 `no_subscription_for_merge`。
- **`new`**（agora 沒有「新建一個空 Session 再開 agent」的指令，這是 v0 的做法，PM 確認中）：sidecar 直接開 agent（沒有 agora 行程），agent 的 session id 從事件取得；每 `sync_period` 分鐘與 close／換把時用 `agora import session --external-session-id <id> --agent … --add-tag sidecar:<id>` 存進 agora（再次 import 是原地更新）。第一次存進去之後，`status.session` 才有 agora id；之後換把照 `continue` 那個 agora id 走。
- **tag**：每個 open 出來的 Session 在標頭的 `tags` **附加** `sidecar:<id>`（本人 Q7）。附加不是取代：agora 的 `--header tags=…` 是整個取代（`overlay` 對 list 的語意），所以 agora 要提供附加的選項 `--add-tag <tag>`（可重複；`continue`、`fork`、`import` 都有；見 `session-continue` 的 delta）。不 fork 的 `continue` 會把 tag 加在原 Session 上，這是預期的。sidecar 本身不讀這個 tag；用途是人或程式在 agora 上查得到。
- **新 Session 的 id**：`--headless` 時 stdout 屬於 agent，所以 agora 在 stderr 印一行 `agora-session <id>`（只有這一行有固定格式）；sidecar 讀它設定 `status.session`。
- 記憶同步：agent 跑著時由 agora 定期寫回；close 與換把時 agora 在結束時寫回；任何人隨時能直接用 agora。

### D6 agent 的開法（spike 已做，2026-10-11 PM 實測）

用程式化模式，不開 TUI。

**Claude Code 2.1.296**：`claude -p --input-format stream-json --output-format stream-json --verbose --resume <id> --settings '{"autoContinueAtUsageLimit": false}'`（由 `agora continue --headless` 組出來；`--settings` 的內容固定帶，寫在 `session-continue` 的 delta）
- 送：stdin 一行一則 `{"type": "user", "message": {"role": "user", "content": …}}`，同一個行程可以一直送下去（多輪）。
- 狀態：送出後到 `result` 事件之前是 running；`result` 之後是 idle。
- 中斷：stdin 寫 `{"type": "control_request", "request_id": …, "request": {"subtype": "interrupt"}}`，0.2 秒內回 `control_response` 與 `result`（`error_during_execution`），之後照常可以再送。
- 結束：關掉 stdin，行程以 0 結束。
- 接續：另開一個行程 `--resume <同一個 session id>`，對話接得上（實測記得前面說過的字）。
- **額度**：每一輪都有 `rate_limit_event`，帶 `rate_limit_info`。
  - `status`：官方型別是 `"allowed" | "allowed_warning" | "rejected"`（出處：Claude Agent SDK TypeScript 文件的 `SDKRateLimitEvent`，https://code.claude.com/docs/en/agent-sdk/typescript ，2026-10-12 讀取）。**額度用完＝`status` 等於 `rejected`**；`allowed_warning` 只是快到了，不換把。未知值當沒用完，並把原事件（去掉任何秘密）記進 log，不換把（本人／PM 的 Q6）。
  - `rateLimitType` 不在官方型別裡，實測有 `five_hour`；第三方整理的值還有 `seven_day`、`seven_day_opus`、`seven_day_sonnet`、`overage`（未經官方確認）。窗對應只認 `five_hour`→`5h`、`seven_day`→`1w`；其他值當 `5h`、不帶 `resets_at`，並 log 一行。
  - 重置時刻取 `resetsAt`（epoch 秒），不必解析畫面文字。官方型別另有 `errorCode: "credits_required"`（claude.ai 訂閱的內含額度用完，要買額外額度才能繼續）：當作用完，窗口以 `rateLimitType` 為準，沒有就 `5h`。
  - 事件裡還有 `overageStatus` 之類的欄位；已知有回報「`status: allowed` 但 `overageStatus: rejected` 時 `claude -p` 的 `result` 是空的」（claude-code 的 issue，已關閉）。driver 只認 `status` 與輔助訊號，不因 `overageStatus` 換把。
  - 輔助訊號：`system/api_retry`（`error: rate_limit`）與 `result` 文字以 `You've hit your` 開頭（LLMGateway 的 quota-signals 研究）。
  - **kind 是 `api-key`**（例如 OpenRouter 經 `ANTHROPIC_BASE_URL`）時不會有 `rate_limit_event`，輔助訊號就是唯一的訊號，也沒有窗口與重置時刻：窗口 `5h`、不帶 `resets_at`。

**opencode 1.18.35**：`opencode serve --hostname 127.0.0.1 --port <埠>`（工作目錄設成 agent 的工作目錄；opencode 依目錄分專案）。埠由 sidecar 分配（取一個空的），經 agora 的 `--headless-port` 交給 opencode；agora 在 stderr 印 `agora-port <埠>` 確認。
- 送：`POST /session/{id}/message`（這個呼叫會等到這一輪做完才回，所以 driver 在背景送、用事件看狀態）。
- 狀態：`GET /event`（SSE）的 `session.status`：`busy`→`idle`，另有 `session.idle`；`GET /session/status` 可以直接查。
- 中斷：`POST /session/{id}/abort`，之後狀態變 `idle`。
- 接續：對 server 用同一個 session id 送就是接續。
- **額度（實測，Zen 免費額度用完時）**：`session.status` 變成 `{"type": "retry", "message": "Free usage exceeded…", "action": {"reason": "free_tier_limit", …}, "next": <epoch 毫秒>}`。
  - **額度用完＝`retry` 且 `action.reason` 在白名單內**。白名單放設定（`quota_reasons`，預設只有觀察到的 `free_tier_limit`；之後只改設定就能加）。`retry` 也用在暫時性錯誤（連線、過載），那些 `next` 只有幾秒；不在白名單的 `retry` 只 log，狀態維持 `running`，不換把。
  - 重置時刻取 `next`（毫秒，轉成 RFC 3339 UTC）。opencode 只給一個時刻，不分窗。**窗口對應是單一規則**：`next` 距現在大於 5 小時加 5 分鐘 → `1w`；否則 → `5h`。理由：5h 窗口的重置不可能超過 5 小時以外，所以超過就只能是 1w；反過來不超過不保證是 5h（1w 的最後幾小時也會落在這裡），但標錯幾乎無害——冷卻以 `until` 為準，窗口名稱只影響「沒給 `resets_at` 時的預設長度」與「同窗口取較晚的」。沒有 `next` 或不是時間：窗口 `5h`、不帶 `resets_at`。
- 還要在 opencode 1.18.35 上驗證的：key 與 baseURL 的給法（例如 `OPENCODE_CONFIG_CONTENT`，LLMGateway gateway-v0 的 design 把它列為 Open Question），列在 tasks 3.2。

**agora 的接法：選 (a)**——`agora continue` 加一個 `--headless`：照常準備 Session（continue／fork）、上鎖、定期寫回、結束時寫回，只是用上面的程式化模式開 agent，stdio 直接交給 agent（agora 自己在 agent 跑著時不寫 stdout，說明都寫 stderr）。sidecar 開 `agora continue … --headless` 這個行程，透過它的 stdio（Claude Code）或 opencode 的埠和 agent 說話。這樣接續、fork、寫回、同一個 Session 不能同時接續的鎖，都沿用 continue 已經測好的邏輯；agora 只多幾個選項（`--headless`、`--headless-port`、`--add-tag`）。
- **停下**：溫和停＝對 agora 行程送 SIGTERM，agora 轉給 agent、等它結束、做最後一次寫回、放鎖後退出（Claude 也可以先關 stdin）。逾時才 SIGKILL agora（`close_timeout`，預設 30 秒）；SIGKILL 的後果是不做最後一次寫回（最多掉一個 `sync_period` 的對話；鎖是 `flock`，行程沒了就釋放），所以先 SIGTERM。

### D7 設定

- `~/.config/sidecar/config.json`（`SIDECAR_CONFIG` 可覆寫）：
  - `port`（預設 8471；不在程式裡寫死，MyLinuxPool 的 `--check` 從設定讀）；
  - `subscription_source`（`llmgateway` 或 `file`＋路徑）；
  - `gateway_connect_file`（LLMGateway 連線設定的檔案路徑，預設 `~/.config/sidecar/gateway-connect.json`）；
  - `agents`（各種類的執行檔路徑與額外參數）；
  - `sync_period`（分鐘，預設 5）；
  - `resume_prompt`、`retry_interval`（預設 5 分鐘）、`poke_queue_timeout`（預設 30 秒）、`close_timeout`（預設 30 秒）、`quota_reasons`（opencode 的額度類 reason 白名單）。
- 不含任何秘密。
- **LLMGateway 的連線設定**放在獨立的檔案 `gateway-connect.json`，欄位對應 LLMGateway 的 `lgw.connect.ConnectConfig`（名稱照 LLMGateway 的規格：`jumps`、`rendezvous`、`identity`、`known_hosts`、`remote_api`…）。
  - sidecar **每一次** LLMGateway 操作都重新讀這個檔，不快取；檔案由外部以原子方式改寫（寫暫存檔再 rename）。理由：LLMGateway rotate 時 worker 上的連線資訊會變，而重啟 sidecar 會殺掉它開的 agent（狀態揮發、agent 是子行程），所以不靠重啟也不靠 SIGHUP。
  - worker 上由 MyLinuxPool 的單元從它的 `gateway.json` 產生這個檔（含 sidecar 專用的 `known_hosts`，放 sidecar 的 state 目錄、0600）；sidecar 不讀 MyLinuxPool 的任何檔。Mac 上手寫。
  - 讀不到或格式錯誤時，該次操作當 unreachable 處理（D4 的表）並 log 原因，不丟例外。
- LLMGateway 的客戶端：依賴 LLMGateway 的 `lgw`（只用標準函式庫、含 client／connect／signals），放在 extra：`gateway = ["lgw @ git+https://github.com/FATESAIKOU/LLMGateway@lgw-v0.1.0"]`，只用 agora 的人不會裝到；worker 的 tool-pins 裝這個 extra。**一次邏輯操作**（一次 open、一次換把）包在同一個 `with lgw.connect.connect(cfg) as base_url` 裡，用完就關（不長期持有、不自己重連，和 LLMGateway PM 確認過）；`lgw.client` 連不上回 `unreachable=True`，sidecar 當「無法確認」（D4 的表）。

### D8 版本與健康檢查（MyLinuxPool 的 `install.sh --check` 用）

- `sidecar --version`：印出安裝時的 git sha（`uv tool install git+…@<sha>` 的套件中繼資料 `direct_url.json` 裡的 commit；從原始碼目錄跑時印套件版本並註明）。這是指令列的旗標，不是給 agent 的指示。
- 健康檢查不另加端點：`GET /agents`（status）有回應就是「在跑」，連不上就是「沒在跑」。對外仍只有四個動作。

## Risks / Trade-offs

- [程式化模式沒有 TUI 可看] → 人看進度用 agora 的 Session 或 sidecar 的 log；之後若要，人可以另外用 herdr 管。
- [只綁本機、不認證] → 同一台機器上的任何行程（包含 agent）都能呼叫 sidecar；本人選的取捨。
- [狀態揮發] → sidecar 被強制殺掉時 job 清單會消失（自動重開 job 也是，waiting-quota 的 agent 要由控制者重新 open）；記憶在 agora 上。
- [agent 的 CLI 介面隨版本變動] → driver 是實作，版本和 sidecar 一起釘在 worker 映像裡。
- [Claude 的 `rejected` 與 opencode 的額度類 reason 只觀察到少數值] → 未知值只 log 不換把；第一次真實用完時把原事件貼回 D6（tasks 3.4）。
- [`new` 模式不經 agora 行程] → 定期 import 之間若被強制殺掉，最多掉一個 `sync_period` 的對話；PM 確認中。
