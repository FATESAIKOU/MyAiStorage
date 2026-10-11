## Context

本人 2026-10-11 定案設計提案 v4（`docs/proposals/aicompany-design-v4.md`）。sidecar 是 agent 的生命週期控管：控制者用 HTTP 下四種指示，sidecar 用子行程開 agent，用 agora 繼承與寫回 Session，用 LLMGateway 拿 subscription。

## Goals / Non-Goals

**Goals：** 對外只有四個動作；介面和實作分開（agent 種類、Session 保存處、subscription 來源、挑選規則都可以替換）；相依少；Mac 與 worker 同一份程式。

**Non-Goals：** Angareion 的收發（各實體自己做）；認證（只綁本機）；重啟後找回（狀態揮發）；同時租用上限、分辨是哪台 worker（v0 不做）；herdr。

## Decisions

### D1 對外：四個動作＋三種形式（HTTP，只綁 127.0.0.1）

| 動作 | HTTP | 參數 | 回傳 |
|---|---|---|---|
| open | `POST /agents` | `id`（呼叫方給，唯一）、`agent`（`claude-code`／`opencode`）、`workdir`、`session`（`{"mode": "new"}`／`{"mode": "continue", "from": <agora id>}`／`{"mode": "fork", "from": …}`／`{"mode": "merge", "from": [...]}`）、`subscription`（選填，`{"id": …}`；省略時由 sidecar 用 `Selector` 挑）、`input`（選填，第一則輸入） | 201 與 agent 的狀態。id 已存在而且狀態是 starting／running／idle 時回 409；狀態是 waiting-quota 或 exited 時，用同一個 id 重開（取代舊的紀錄；waiting-quota 時順帶刪掉它的自動重開 job） |
| close | `POST /agents/{id}/close` | — | 先溫和停下 agent，再把最後的對話寫回 agora；逾時才強制結束。回最後的狀態 |
| status | `GET /agents`、`GET /agents/{id}` | — | 只列它開的：`id`、`agent`、`session`（agora id）、`subscription`、`state`（`starting`／`running`／`idle`／`waiting-quota`（附 `reopen_at`）／`exited`）、`started_at`、`exit`；不含 key |
| poke | `POST /agents/{id}/poke` | `text`、`interrupt`（選填，預設 false） | 送一則輸入；`interrupt` 先打斷這一輪 |

- 定時：`POST /jobs {"action", "args", "at"}`；輪詢：`POST /jobs {"action", "args", "every", "until"?}`；`GET /jobs`、`DELETE /jobs/{id}`。`action` 只能是上面四個之一，`args` 和直接呼叫時相同。
- 只綁 `127.0.0.1`（埠由設定給），不做認證（本人 Q5）。遠端控制者自己用隧道連進來。

### D2 揮發的狀態

agent 清單、job 清單只在記憶體。sidecar 結束時先對每個 agent 做 close（停下、寫回 agora）；被強制殺掉就什麼都不留。重啟從空的開始，不找回（本人：不用管重啟）。

### D3 內部介面（介面和實作分開）

| 介面 | 動作 | 實作 |
|---|---|---|
| `AgentDriver` | `env_for(subscription) → env、unset`、`launch(session, env, workdir) → handle`、`send(handle, text)`、`interrupt(handle)`、`state(handle) → running／idle／exited／quota_exhausted(window, resets_at)`、`stop(handle, graceful)` | `ClaudeCodeDriver`、`OpenCodeDriver` |
| `SessionStore` | `start(mode, ids, agent, workdir, tag) → 交給 driver 的行程`（`agora continue --headless`）、`sync`（由 continue 定期與結束時做） | `AgoraSessionStore`（呼叫 `agora`） |
| `SubscriptionSource` | `list()`、`get(id, agent)`、`cooldown(id, window（5h／1w）, resets_at)` | `LLMGatewaySource`（經 LLMGateway 的 `lgw connect`）、`FileSubscriptionSource`（本機開發：讀一個 JSON 檔） |
| `Selector` | `choose(agent, subscriptions) → id 或 None` | `PrioritySelector`：`available` 且 `agents` 含這個種類，照 `priority` 取第一個。之後換成外部注入的函式時，sidecar 本體不改（本人 Q8 補充） |

- 「subscription 要變成 agent 的哪些環境變數」與「怎麼看出額度用完」都是 `AgentDriver` 的知識（和 LLMGateway PM 一致：共通 JSON 裡沒有 `launch`）。
- 環境變數只放在子行程的環境，不上命令列，不寫進任何 log 或檔案。

### D4 subscription 與換把

- open：有給 `subscription` 就用它的 id；沒給就 `list` → `Selector.choose`。然後 `SubscriptionSource.get(id, agent)` → `AgentDriver.env_for` → launch。
- `window` 只有 `5h` 與 `1w` 兩種（和 LLMGateway 一致）。
- 「停下」與「寫回」的順序一律是：先溫和停下 agent，再把最後的對話寫回 agora（close、換把、全部冷卻都一樣）。
- driver 回報 `quota_exhausted(window, resets_at)`：`cooldown(id, window, resets_at)` → `list` → `Selector.choose`：
  - 有：`get` → `stop`（溫和）→ `SessionStore.sync`（寫回）→ `prepare(continue 同一個 Session)` → `launch`；id 不變。
  - 沒有：`stop`（溫和）→ `sync`（寫回）；`state = waiting-quota`，並自動掛一個 job：`at` ＝ 這個 agent 種類能用的 subscription 最早解除冷卻的時刻，`action = open`，`args` 是同一個 id、同一個 agent 種類與工作目錄、`session = continue 同一個 Session`，**不帶 `subscription`**（open 的 subscription 是選填，省略時到時候由 `Selector` 重新挑）。不是新的對外動作。這個 job 在 `GET /jobs` 看得到、可以刪。
- LLMGateway 的共通 JSON 欄位見提案 v3 第 2.4 節（`schema`／`id`／`provider`／`kind`／`agents`／`priority`／`models`／`endpoint`／`key`／`cooldown`／`available`）。

### D5 Session 與 tag

- open 的 `session.mode`：`new`、`continue`、`fork`、`merge` 由 `SessionStore.prepare` 處理（`merge` 先用 `agora merge` 做出新的 Session 再 continue）。
- 每個 open 出來的 Session 在 agora 標頭的 `tags` 加 `sidecar:<id>`（本人 Q7：保留）。sidecar 本身不讀這個 tag；用途是人或程式在 agora 上查得到。
- 記憶同步：agent 跑著時由 agora 定期寫回；close 與換把前寫回一次；任何人隨時能直接用 agora。

### D6 agent 的開法（spike 已做，2026-10-11 PM 實測）

用程式化模式，不開 TUI。

**Claude Code 2.1.296**：`claude -p --input-format stream-json --output-format stream-json --verbose --resume <id> --settings '{"autoContinueAtUsageLimit": false}'`
- 送：stdin 一行一則 `{"type": "user", "message": {"role": "user", "content": …}}`，同一個行程可以一直送下去（多輪）。
- 狀態：送出後到 `result` 事件之前是 running；`result` 之後是 idle。
- 中斷：stdin 寫 `{"type": "control_request", "request_id": …, "request": {"subtype": "interrupt"}}`，0.2 秒內回 `control_response` 與 `result`（`error_during_execution`），之後照常可以再送。
- 結束：關掉 stdin，行程以 0 結束。
- 接續：另開一個行程 `--resume <同一個 session id>`，對話接得上（實測記得前面說過的字）。
- **額度**：每一輪都有 `rate_limit_event`，帶 `rate_limit_info.status`（實測 `allowed`）、`rateLimitType`（如 `five_hour`）、`resetsAt`（epoch 秒），以及 `unifiedWindows.five_hour／seven_day` 各自的 `utilization` 與 `resetsAt`。窗對應：`five_hour`→`5h`、`seven_day`→`1w`。**額度用完＝`status` 不是 `allowed`**（實際值推測是 `rejected`，沒辦法刻意用完來確認）；重置時刻直接取 `resetsAt`，不必解析畫面文字。輔助訊號：`system/api_retry`（`error: rate_limit`）與 `result` 文字以 `You've hit your` 開頭（LLMGateway 的 quota-signals 研究）。

**opencode 1.18.35**：`opencode serve --hostname 127.0.0.1 --port <埠>`（工作目錄設成 agent 的工作目錄；opencode 依目錄分專案）
- 送：`POST /session/{id}/message`（這個呼叫會等到這一輪做完才回，所以 driver 在背景送、用事件看狀態）。
- 狀態：`GET /event`（SSE）的 `session.status`：`busy`→`idle`，另有 `session.idle`；`GET /session/status` 可以直接查。
- 中斷：`POST /session/{id}/abort`，之後狀態變 `idle`。
- 接續：對 server 用同一個 session id 送就是接續。
- **額度（實測，Zen 免費額度用完時）**：`session.status` 變成 `{"type": "retry", "message": "Free usage exceeded…", "action": {"reason": "free_tier_limit", …}, "next": <epoch 毫秒>}`。**額度用完＝`retry` 且 `action.reason` 是額度類**；重置時刻取 `next`。opencode 只給一個時刻，不分窗；driver 依 subscription 的種類決定登錄 `5h` 或 `1w`（距離重置超過 5 小時就當 `1w`）。

**agora 的接法：選 (a)**——`agora continue` 加一個 `--headless`：照常準備 Session（continue／fork）、上鎖、定期寫回、結束時寫回，只是用上面的程式化模式開 agent，stdio 直接交給 agent（agora 自己在 agent 跑著時不寫 stdout，說明都寫 stderr）。sidecar 開 `agora continue … --headless` 這個行程，透過它的 stdio（Claude Code）或 agent 印出的埠（opencode）和 agent 說話。這樣接續、fork、寫回、同一個 Session 不能同時接續的鎖，都沿用 continue 已經測好的邏輯；agora 只多一個選項（不必做 (b) 的兩個新指令）。`merge` 先用 `agora merge` 做出新的 Session，再 `continue --headless`。

### D7 設定

- `~/.config/sidecar/config.json`（`SIDECAR_CONFIG` 可覆寫）：`port`（預設 8471；不在程式裡寫死，MyLinuxPool 的 `--check` 從設定讀）、`subscription_source`（`llmgateway`＋連線設定，或 `file`＋路徑）、`agents`（各種類的執行檔路徑與額外參數）、`sync_period`（分鐘，預設 5）。
- 不含任何秘密。LLMGateway 的連線憑證由 `lgw connect` 自己處理。

### D8 版本與健康檢查（MyLinuxPool 的 `install.sh --check` 用）

- `sidecar --version`：印出安裝時的 git sha（`uv tool install git+…@<sha>` 的套件中繼資料 `direct_url.json` 裡的 commit；從原始碼目錄跑時印套件版本並註明）。這是指令列的旗標，不是給 agent 的指示。
- 健康檢查不另加端點：`GET /agents`（status）有回應就是「在跑」，連不上就是「沒在跑」。對外仍只有四個動作。

## Risks / Trade-offs

- [程式化模式沒有 TUI 可看] → 人看進度用 agora 的 Session 或 sidecar 的 log；之後若要，人可以另外用 herdr 管。
- [只綁本機、不認證] → 同一台機器上的任何行程（包含 agent）都能呼叫 sidecar；本人選的取捨。
- [狀態揮發] → sidecar 被強制殺掉時 job 清單會消失；記憶在 agora 上，由控制者重新 open。
- [agent 的 CLI 介面隨版本變動] → driver 是實作，版本和 sidecar 一起釘在 worker 映像裡。
