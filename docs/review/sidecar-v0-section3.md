# #31 第 3 節（3.0～3.6）與核心複審 N 系列修正的非作者審查

- 對象：`sidecar-v0-agy4`，`5429d3c` 之後（3.0 `7a657cb`、3.1 `c95d268`、3.2 `80966b0`、N1～N6、3.5 `0311fb6`、3.6.1～3.6.4）；另外審了先前沒審過的 3.3（agora `continue --headless`，`55b6231`）。`git archive` 到 `/tmp/s31d`，只讀；沒有叫真的 claude、opencode 或 Drive。
- 對照：本分支的 design D3～D6、spec、`docs/review/sidecar-v0-core.md`、`docs/review/sidecar-v0-drivers-spike.md`。
- 審查者：架構師（Claude Sonnet 5.5）。
- 基準：副本上 `-k sidecar` 161 passed、1 skipped（lgw loopback，沒裝 extra）；`test_continue_headless.py`（agora 3.3）7 passed。

## 結論

**要先修，才進 3.4 的 PM 真機。** 規格面的東西大多接對了：agora 的 headless 三行、`add-tag`、fork／merge／port、兩個 driver 的 `env_for`（spike 的規則都有）、N1 在「換把進行中」的 close 都成立，11 個我挑的突變 10 個轉紅。但有兩個 High 會在真機上直接碰到，另外三個 Medium 都是 3.4 一定會踩的（額度誤判、opencode 的長回合、錯誤訊息被遮壞）。

## 問題

### High

**H1. N1 只修了「close 抵達時換把已經在進行」；close 先拿到鎖、慢慢 stop 的期間換把才開始，agent 仍然復活。**
- 位置：`core.py` 的 `close`（非 `STARTING`／非 `busy` 的那條路：鎖外 `_stop_handle`、之後才寫 `exited`）與 `_quota_exhausted`／`_swap`（只在 `busy` 為 False 時進入）。
- 實測（假 driver 的 `stop` 閘住，close 卡在 stop；這時 driver 回報 quota，scheduler 刷新）：`state: exited`，但 `handles: 2`，最新那個 **沒被 stop**——被關閉的 agent 有一個行程還活著。
- 原因：close 在釋放鎖到寫 `exited` 之間，沒有占用這個 agent 的轉移（`busy`）；換把因此認為可以進行，close 結束時又把 `rec.state` 寫成 `exited`，蓋掉換把的結果。
- 建議：`close` 在**同一個鎖內**檢查並設 `rec.busy = True`（已經 busy 才走 `closing`），做完（或失敗）才釋放；`_refresh_one` 看到 `busy` 就略過。補測試（就是上面的探測）。

**H2. `new` 模式：換把前的 import 失敗，每個 tick 都重新進入換把（cooldown、list、import 各打一次），而且沒有 job。**
- 位置：`core.py` 的 `_swap_new_mode`（import 失敗 → `_schedule_swap_retry`；`rec.session_id` 是 None 時只 log 就 return）。
- 實測（`new` 模式、`import_external` 一律失敗、driver 回報 quota；連續 5 次 `refresh_states`）：`cooldown` 被登錄 **5 次**、`list` 6 次、`state` 仍是 idle、**`/jobs` 是空的**。agent 沒被 stop，driver 的額度狀態又是「黏住」的，所以下一個 tick 又走一次，每秒一次地打 LLMGateway（含 SSH 連線）與 agora。
- 另外，`session_id` 已知時 `_schedule_swap_retry` 掛的是「`open` continue」的 job，但 agent 沒被停（還是 running），job 到時 `open` 會 409；它沒有重試換把。
- 這個行為也和 design D5 對不上：D5 寫的是「走 D4 的失敗那一支（waiting-quota＋重試 job，不 stop）」，本身就有矛盾（沒停卻 waiting-quota）；impl3 取了「不停」。**這是設計上要我改清楚的地方**：我建議的語意是——**import 失敗也照樣 stop**（對話仍在 agent 自己的 session 檔裡，沒有丟），標 `waiting-quota`、記住 `external_session_id`，由 scheduler 定期重試 import（`retry_interval`）；import 成功後才掛一般的自動重開 job。這樣沒有「停不下來又不斷換把」的路徑。
- 修法分兩步：(1) 馬上：換把失敗時不能讓下一個 tick 原樣再進來（至少 `busy`/退避：`rec.swap_retry_at = now + retry_interval`，到時間前略過）；(2) 語意：照上面改 D5，並補 Scenario。

### Medium

**M1. `ClaudeCodeDriver` 的額度誤判：一次暫時性的 `api_retry rate_limit` 就把額度標成用完（而且是黏住的）。**
- 位置：`claude_code.py` 的 `_on_event`（`system/api_retry` 且 `error == "rate_limit"` → `_mark_quota`；`result` 文字以 `You've hit your` 開頭 → `_mark_quota`，沒有看 `is_error`）。
- 實測：送入一個 `api_retry`（`attempt 1/10`，`retry_delay_ms 1000`），之後回合正常 `result: success` → driver 的狀態是 **`quota_exhausted`（5h、沒有 resets_at）**，不會自己恢復；一個一般回覆的 `result` 文字只要以 `You've hit your goal…` 開頭也被判成額度用完。
- 後果：Claude 對 429 的短暫速率限制（每分鐘）會自行重試幾次；sidecar 卻立刻 stop agent、在 LLMGateway 登錄 **5 小時**的冷卻、換一把。api-key 類（沒有 `rate_limit_event`，輔助訊號是唯一訊號）最容易發生。
- D6 允許輔助訊號，但沒有說「單次 api_retry 就算」。建議：(a) `api_retry` 只在 `kind == api-key`（沒有 `rate_limit_event`）時使用，而且要等「重試用盡」（`attempt >= max_retries`），或該回合最後以 `is_error: true` 的 `result` 結束，才標額度；(b) 文字規則要求 `is_error: true`；(c) `rejected`／`credits_required` 維持現狀。補三個測試：暫時性 429 之後成功 → 不標；重試用盡 → 標；一般回覆的開頭巧合 → 不標。

**M2. `OpenCodeDriver` 的送出與狀態：長回合會被 10 秒逾時切斷、錯誤被吞掉、狀態可能永遠是 running。**
- 位置：`opencode.py` 的 `_request`（`timeout=timeout or self._http_timeout`，預設 10 秒，包含 `POST /session/{id}/message`，那個呼叫要等到整個回合結束）、`_send_round`（失敗只 log）、`state`／`_apply_status`（只有 `/session/status` 裡有這個 session 才更新）。
- 實測（假 server：status 回空的 map、POST 要 3 秒，driver 的 `http_timeout=1`）：回合其實早已結束，`state` 仍是 **`running`**；錯誤只在 log；`_verify_first_response` 因逾時根本沒跑。
- 這幾點對照我在 spike 看到的實形狀：`POST /session` 回 `{"id": "ses_…"}`（✓）、`POST /session/{id}/message` 的本文 `{"parts":[{"type":"text","text":…}]}` 可用，回應有 `info.providerID`／`info.modelID`（✓），出錯時是 HTTP 200 加 `info.error`，模型找不到是 HTTP 500。**沒有驗證到**：`GET /session/status` 對「閒著的 session」回什麼（空 map？`{"type":"idle"}`？）；`POST /session/{id}/abort` 的回應。
- 建議：(a) `POST /message` 用長逾時（它在自己的背景執行緒裡），或改用 `POST /session/{id}/prompt_async`（binary 裡有這個路由，字串出現 7 次；立即回應，狀態改看 `/session/status` 與事件）——3.4 實機確認哪個行得通；(b) 狀態不要只靠 status 的 map：送出的背景執行緒在跑＝running，結束＝idle，`/session/status` 只用來偵測 `retry`（額度）；(c) 回應 `info.error` 與非 2xx 要寫進 `exit.reason`／`last_error`，不要只 log。

**M3. 錯誤訊息的遮罩把數字 `1` 都遮掉。**
- 位置：`drivers/common.py` 的 `mask_secrets`；兩個 driver 的 `handle.secrets = tuple(value for value in env.values() if value)`——`env_for` 的輸出裡有 `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1"`、`DISABLE_AUTOUPDATER="1"`、模型名、endpoint 這些**不是秘密**的值，也被當成秘密。
- 實測：`"agent exited with code 1 after 12 retries"` → `"agent exited with code <masked> after <masked>2 retries"`。
- 建議：只遮真正的秘密（`key`，以及 `OPENCODE_CONFIG_CONTENT` 裡的 `apiKey` 值）；`env_for` 回傳之外另給一個 `secret_values(subscription)`，或遮罩前過濾長度小於 8 的值。補測試。

**M4. 「所有呼叫都在 `close_timeout` 以內」仍有三處不成立，shutdown 的最壞時間公式因此是錯的。**
- `AgoraSessionStore.START_TIMEOUT = 60`、`IMPORT_TIMEOUT = 60`、`MERGE_TIMEOUT = 900`，都大於預設 `close_timeout = 30`；`terminate_process` 在 `timeout` 之後還 `kill` 並等 5 秒，所以一次 stop 最壞是 `close_timeout + 5`。
- 影響最大的是 `new` 模式的 shutdown：`_close_new_mode` 做「import → stop → import」，每個 `new` agent 最壞 60＋35＋60 ≈ 155 秒，不是 design D2 寫的 `close_timeout × agent 數`。supervisor 的 kill timeout 若按 D2 的公式設，SIGKILL 會落在 import 中間。
- 建議：`import_external` 的逾時不要超過 `close_timeout`（它有自己的參數，由 `wiring` 傳 `close_timeout`）；`start` 不在 shutdown 路徑上（只在 open／換把），60 秒可以留，但 D3 的契約要改寫成「shutdown 路徑上的呼叫」；D2 的公式改成「每個 agent 最壞 `close_timeout × 2 + 5`（`new` 模式）」。
- merge 路徑（impl3 第 1 點）可以接受，但要註明：job 裡的 `open merge` 在 scheduler 執行緒內做，最多卡 15 分鐘，其間所有 agent 的刷新、佇列中的 poke 都被延後（N7 的放大）。要避免就讓 `merge` 的 open 只能直接呼叫（拒絕出現在 job 裡），或把 job 的 open 交給工作執行緒。

**M5. `AgoraProcess.stderr_lines` 是啟動當下的快照，之後的錯誤輸出看不到。**
- 位置：`agora_store.py:307-312`（`stderr_lines=list(pump.lines)` 複製一份）與 `stderr_tail()`。`_StderrPump` 之後仍在 `lines` 這個 deque 裡累積，但 `AgoraProcess` 拿的是舊的複本，driver 的錯誤訊息（`stderr_excerpt` → `proc.stderr_tail()`）永遠只含啟動前的幾行。
- 建議：`AgoraProcess` 持有 pump 的 deque 本身（或一個取得最新末幾行的 callable）。

### Low

- **L1　driver 的例外不是 `SidecarError`**：`ClaudeDriverError`／`OpenCodeDriverError`（`RuntimeError`）在 `poke` 時（agent 剛好結束、stdin 斷了）會變成 HTTP 500，應該是 409 `agent_not_running`。`core.poke` 接住 driver 例外轉成 `AgentNotRunningError`，並把狀態標 exited。
- **L2　`AgoraSessionStore` 預設用 `python -m agora.cli`，`child_env` 的允許清單沒有 `PYTHONPATH`**：用 `uv tool install` 沒問題，開發時從原始碼目錄跑會 `ModuleNotFoundError`。文件寫一句，或在 `command` 找不到時給明確訊息。
- **L3　agora 3.3：SIGTERM 在 handler 安裝之後、`Popen` 回來之前抵達**，`_forward_term` 引用還沒有賦值的 `proc`，會 `UnboundLocalError` 並在寫回之前離開（視窗是幾十毫秒）。把 `proc = None` 先宣告，handler 遇到 None 就記一個旗標、`Popen` 後立刻轉送。
- **L4　periodic import 在 scheduler 執行緒裡做**（`_run_periodic_imports`），每個到期的 `new` agent 最多 60 秒；多個 agent 時延遲其他 agent 的刷新。可以接受，但請 `docs/sidecar.md` 寫明。
- **L5　突變 W11（換把後在「寫回 handle 之前」的第二個 `closing` 檢查拿掉）沒有紅**：它與「寫回之後」的那一個重複（兩個都會 stop 並標 exited），算冗餘，不是缺口。

## 其他問題的判斷

### 2. 額度判斷（要求 2）

- `rejected`、`credits_required`：**符合 D6**。
- `api_retry rate_limit` 與 `result` 文字：D6 把它們列為「輔助訊號，api-key 類唯一的訊號」，**允許**，但現在的觸發條件太寬，見 M1；對訂閱類（有 `rate_limit_event`）根本不該用輔助訊號。

### 3. ClaudeCodeDriver 沒有 `models.default` 的行為（要求 3）

程式是：`provider != anthropic` 且沒有 default → `pick_model`（`allow` 第一個明確的，沒有就 `no_model`）；`provider == anthropic`（或沒有 provider）沒有 default → **不設**。這**正好是 D6 寫的**（「沒有 default 的 Anthropic 訂閱不設」；非 Anthropic provider 才依 `allow` 的順序），**不用改回**。

### 4. impl3 的 6 點

| # | 判斷 | 理由 |
|---|---|---|
| 1 | **可以接受（附 M4）** | merge 不在換把／shutdown 路徑上，900 秒合理；但 job 裡的 merge 會卡住 scheduler，且 D3 的逾時契約要寫清楚哪些呼叫適用。 |
| 2 | **部分確認；其餘留 3.4** | 已確認（spike 實測）：`POST /session` 回 `{"id": …}`、`POST /session/{id}/message` 的 `{"parts": […]}` 本文、回應的 `info.providerID`／`modelID`、出錯時 200 加 `info.error`、模型找不到 500。**未確認**：`/session/status` 對閒著 session 的形狀、`abort`、以及 10 秒逾時與長回合的關係（M2）；`prompt_async` 路由在 binary 裡存在。 |
| 3 | **不接受（H2）；邊角 (b) 可以接受** | (a) 沒有 agora id 時換把遇到 import 失敗 → 沒有 job，且 agent 不停、每個 tick 重進換把（實測 5 次登錄）。(b) 還沒跑過任何一輪（`external_session_id` 是 None）→ 停掉、waiting-quota、沒有 job：這個狀況極少（還沒送過訊息就額度用完），可以接受。(c)「import 失敗不停 agent、掛 retry job」這個選擇本身：**不要**；對話在 agent 自己的檔案裡不會丟，停下再重試 import 才不會原地打轉（見 H2 的建議）。 |
| 4 | **可以接受** | 我看了 agora 的實作：`agora-session`、`agora-agent-session`（claude 與 opencode 都印）、`agora-port`（opencode）都在寫入 pending 之後、agent 開起來之前印出；`AgoraSessionStore.start` 正確等三行（claude 兩行）。 |
| 5 | **可以接受** | `pick_model` 在兩個 driver 共用；`NoModelError` 是 400 `no_model`。 |
| 6 | **可以接受** | 整合測試預設不跑（`SIDECAR_REAL_AGENT_TESTS=1`、不在 testpaths），只驗「請求到了假 provider、key 相等、不洩漏父行程的值」，和 spike 做的一樣，足夠。 |

### 5. 併發與逾時

- 新 driver 的背景送出（opencode）不持有 core 的鎖，也不持有 `handle.lock` 跨 HTTP；`stop` 時 daemon 執行緒隨 server 結束。N1／N4（busy 與 closing）在「換把進行中」成立（W11 以外的突變都紅）；但見 **H1**（close 先到）。
- 逾時盤點：opencode 的 HTTP 預設 10 秒、`_wait_listening` 15 秒、status 2 秒 ✓；lgw 15／10 秒 ✓；`terminate_process` `close_timeout`＋5 ✓（略超出契約）；`AgoraSessionStore` 的 start 60／import 60／merge 900 ✗（M4）；`ClaudeCodeDriver._write` 在 stdin 管線滿了時會阻塞（訊息很小，實務上不會，Low）。

## 建議順序

1. **H1、H2**（close 占用轉移；`new` 模式換把失敗的退避與語意）。D5 那句我來改。
2. **M1、M3、M5**（額度判斷的條件、遮罩、stderr 末幾行）：都小，3.4 實機一定會看到。
3. **M2**（opencode 的送出方式與狀態來源）；`prompt_async` 與 `/session/status` 的形狀請 PM 在 3.4 實機確認後定案。
4. **M4**（逾時契約與 D2 的公式）、L1～L4。
5. 然後進 3.4。
