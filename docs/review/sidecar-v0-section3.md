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

---

# 複審（第二輪）

- 對象：`sidecar-v0-agy4` 的 `e5b246a` 之後十個 commit（`8d11da7` H1、`65de9fd` M1、`fc00eae` M3、`904bc6d` H1 後續、`2cbc925` M5、`e174545` L1、`98656ae` L3、`d154753` H2、`0732257` M2、`8e1cb3b` M4）。`git archive` 到 `/tmp/s31e`，只讀；沒有叫真的 claude、opencode、agora 或 Drive，沒有連網。
- 對照：PM 在 `e5b246a` 寫進 design 與 spec 的決定（D2/D3/D5 的公式與逾時契約、close 占用轉移、new 模式 import 失敗也 stop、Claude 輔助訊號收窄、opencode 的送出與狀態來源、遮罩、merge 不進 job）。
- 審查者：架構師（Space Bunny）。非作者。
- 基準：副本上 `-k sidecar` 190 passed、1 skipped；整個 `tests/unit` 1034 passed、1 skipped。

## 結論

**可以進 3.4 PM 真機。** 十個修正全部照 design 修好，而且每一個都有測試真的釘住：我逐一在副本裡把該修正「故意改壞」，十個全部轉紅（見下面的突變表），包括實作者自己加的 `904bc6d`。沒有引入新的競態、死鎖或逾時問題。

有三件事要帶進 3.4 的觀察名單（見「3.4 要順手看」）：兩個是既有程式本來就有的、不是這輪引入的，一個是這輪 M2 帶進來的新邊角。它們都不擋 3.4。

## 修正的複審

| # | commit | 真的照 design 修好？ | 證據 |
|---|---|---|---|
| H1 | `8d11da7` | 是 | `close` 在同一個鎖內設 `rec.busy = True`，`finally` 才放（`core.py:383`、`core.py:401-403`）；`_refresh_one` 開頭就 `if rec.busy: return`（`core.py:598`）。改壞後 `test_close_held_in_stop_prevents_swap_and_leaves_no_live_process` 轉紅。 |
| H1 後續 | `904bc6d` | **是，而且必要** | 把觀察拆成 `_observe_handle`（不設 busy gate，由呼叫端擁有轉移）與 `_refresh_one`（保留 busy gate）。見下。 |
| H2 | `d154753` | 是 | import 失敗照樣 stop、`_wait_import_retry` 標 `waiting-quota` 記住 `external_session_id`、`tick()` 裡 `_retry_pending_imports` 每 `retry_interval` 重試、成功才 `_schedule_reopen` 掛 job。改壞後兩個測試轉紅。 |
| M1 | `65de9fd` | 是 | 輔助訊號收成 `handle.is_api_key` 且 `api_retry` 要 `attempt >= max_retries`、文字要 `is_error: true`。改壞後 `test_subscription_ignores_auxiliary_signals`／`test_transient_api_retry_does_not_mark_quota` 轉紅。 |
| M2 | `0732257` | 是（有一個新邊角，見下） | `POST /message` 用 `self._message_timeout`；`state()` 以 `handle.sending` 為準、`/session/status` 只讀 `retry`；`info.error` 與非 2xx 走 `_fail_round` 寫進 `last_error` 與 `exit.reason`。改壞後 3 個測試轉紅。 |
| M3 | `fc00eae` | 是 | `extract_secrets` 只收 `*_API_KEY`／`*_AUTH_TOKEN`／`*_OAUTH_TOKEN`／`*_SECRET`／`*_KEY` 與 `OPENCODE_CONFIG_CONTENT` 的 `apiKey`；`mask_secrets` 還加 `len >= 4`。改壞後 13 個測試轉紅。 |
| M4 | `8e1cb3b` | 是 | `add_job` 在鎖外擋 `session.mode == "merge"` 回 400 `merge_not_allowed_in_job`；`wiring` 把 `import_timeout`（連同 `start_timeout`）綁到 `close_timeout`；docs 的公式改成「一般 `close_timeout + 5`、`new` 模式 `close_timeout × 2 + 5`」。改壞後 3 個測試轉紅。 |
| M5 | `2cbc925` | 是 | `AgoraProcess.stderr_lines` 改成持有 pump 的 live deque，`stderr_tail()` 在呼叫時才複製。改壞後 `test_stderr_tail_sees_lines_printed_after_start` 轉紅。 |
| L1 | `e174545` | 是 | `poke` 接住 driver 例外轉 `AgentNotRunningError`（409）並標 exited。改壞後 `test_poke_when_the_driver_send_fails_is_409_and_exits` 轉紅。 |
| L3 | `98656ae` | 是 | `proc = None` 先宣告、handler 記 `term_pending`、`Popen` 回來後立刻轉送。改壞後 `test_sigterm_in_the_window_before_popen_is_forwarded_not_unbound` 轉紅。 |

### `904bc6d` 特別確認（實作者自己加的）

**對，而且非有不可。** `8d11da7` 把 busy gate 提到 `_refresh_one` 最前面之後，換把路徑尾端那個「啟動新 handle 後立刻觀察狀態」的呼叫也被 gate 掉了——但那時 `rec.busy` 正是換把自己設的，於是換把剛啟動的 handle 永遠停在 `starting`。`904bc6d` 把這一步拆成獨立的 `_observe_handle`（不做 busy gate，由呼叫端負責轉移的擁有權），`_refresh_one` 只保留給 scheduler 端的觀察。

我把 `904bc6d` 整個還原（兩個呼叫點都改回 `_refresh_one`）當作突變，**三個測試轉紅**：`test_quota_exhaustion_switches_to_the_next_subscription`、`test_busy_is_released_after_a_swap_allowing_another`、`test_swap_imports_then_continues`。這個修正本身沒有附測試（commit 只動 `core.py`），但既有的測試就釘得住它——作者說的三個正是實際轉紅的三個。判斷正確、範圍剛好（沒有順手把 `_observe_handle` 也放進鎖裡）。

### 突變表（每一個都在副本裡改壞，跑該區的測試）

| 突變 | 結果 |
|---|---|
| `close` 不設 `busy` | RED（`test_close_held_in_stop_prevents_swap_and_leaves_no_live_process`） |
| `904bc6d` 還原成 `_refresh_one` | RED（3 個） |
| H2 還原成「import 失敗不停下」 | RED（`test_swap_import_failure_stops_and_waits_for_the_retry`、`test_import_retry_success_hangs_the_auto_reopen_job`） |
| `tick()` 不呼叫 `_retry_pending_imports` | RED（`test_import_retry_success_hangs_the_auto_reopen_job`） |
| M1 拿掉 `is_api_key` gate | RED（`test_subscription_ignores_auxiliary_signals`） |
| M1 拿掉 `attempt >= max_retries` gate | RED（`test_transient_api_retry_does_not_mark_quota`） |
| M2 `POST /message` 改回短逾時 | RED（`test_a_long_round_is_not_cut_by_the_short_http_timeout`） |
| M2 `info.error`／非 2xx 只 log | RED（`test_info_error_marks_the_round_failed`、`test_non_2xx_marks_the_round_failed`） |
| M3 遮所有 `env` 值 | RED（13 個） |
| M4 拿掉 merge-in-job guard | RED（`test_open_with_a_merge_session_is_refused_in_a_job`、`test_job_open_with_a_merge_session_is_a_400`） |
| M4 `import_timeout` 改回 60 | RED（`test_session_store_timeouts_follow_close_timeout`） |
| M5 改回啟動當下的快照 | RED（`test_stderr_tail_sees_lines_printed_after_start`） |
| L1 拿掉 `poke` 的例外轉換 | RED（`test_poke_when_the_driver_send_fails_is_409_and_exits`） |
| L3 還原成讀未賦值的 `proc` | RED（`test_sigterm_in_the_window_before_popen_is_forwarded_not_unbound`） |

**十一項、十四個突變，全部轉紅。** 這一輪的測試是真的釘住修正，不是寫來裝飾的。

### 新的競態、死鎖、逾時

沒找到會擋 3.4 的。具體查過：

- **close 現在整段持有 `busy`**（含 `new` 模式的 import → stop → import）。這段時間裡 `_refresh_one` 與 `_retry_pending_imports` 都會跳過這個 agent，正是 D5/D2 要的。我用假的 store 把 close 的 import 閘住、讓 scheduler 連續 tick，結果：`state: exited`、`/jobs` 空、沒有任何 handle 活著。close 結束後 `busy` 一定釋放（`finally`），不會卡住後續的換把。
- **close 撞上正在跑的 import retry**：close 看到 `busy` 會記 `closing` 交給那條執行緒，那條執行緒的 `finally` 檢查 `rec.closing and rec.state == WAITING_QUOTA` 再 `_set_exited`。實測兩種交錯（close 在 import 之前到、close 在 import 中間到）都是 `exited`、沒有 job、沒有活著的 handle。
- **鎖的取得次序**：`self._lock` 是 `RLock`，`self._cond` 建在它上面。close 的 `finally` 是在鎖外呼叫的，不會跟 `_cond.wait` 死鎖。沒有新增的跨鎖取得。
- **逾時**：這輪把 `import_timeout` 綁到 `close_timeout`（預設 30 秒），D2 的公式（一般 35 秒、`new` 65 秒）因此成立。opencode 的 `message_timeout` 上限 1 小時只在背景執行緒裡，不是 shutdown 路徑，不影響 kill timeout。沒有逾時被拉長。

## 3.4 要順手看（不擋）

**A（Low，這輪 M2 帶進來的）：`_send_round` 沒有 `finally`，非 `OpenCodeDriverError` 會讓 agent 永遠 `running`。**
`_request` 只接 `HTTPError`、`URLError`、`OSError`、`ValueError`。`http.client.IncompleteRead`（server 在回應中途斷線——這正是 opencode 當掉時最常見的形狀）繼承的是 `HTTPException`，不是 `OSError`，所以會穿出 `_request`、穿出 `_send_round`。`handle.sending` 留在 `True`，`state()` 就永遠回 `running`：scheduler 不會換把、也不會標 exited。實測（`/tmp/s31e`，把 `_request` 換成拋 `IncompleteRead`）：`wait_state(IDLE)` 等滿 5 秒仍是 `running`。
**修法**：`_send_round` 的兩個出口（成功與 `_fail_round`）改成 `try/finally: with handle.lock: handle.sending = False`。三分鐘的事。3.4 實機如果 opencode 當過，症狀會是「狀態卡在 running 不換把」，對得上就順手修。

**B（Low，既有）：兩個 send 交錯時，先完成的那個會把 `sending` 清掉，回合仍在跑但狀態報 `idle`。**
`send()` 只是 `handle.sending = True` 再開一個執行緒，沒有計數。連續兩次 send（第一回合 1 秒、第二回合 1 秒），t=1.3 秒時第一個已完成、第二個還在跑，`state()` 回 `idle`。3.4 連續 poke 兩次就會碰到。修法同上：把 `sending` 改成計數器，或記住「目前有幾個回合在跑」。

**C（Low，既有）：`interrupt` 之後被中止的回合會把整個 agent 標成 `exited`。**
`interrupt` 先把 `sending` 清成 `False` 並設 `IDLE`，但背景執行緒還在；那一回合之後回來的錯誤（中止就是中止，server 會回 error）會走 `_fail_round`，把狀態寫成 `exited`——即使 opencode 行程還活著。實測：`driver.interrupt()` 之後那一回合以 `info.error: aborted by user` 結束，`state()` 變 `exited`。3.4 用「poke + interrupt」停一個長回合時會看到。修法：`_fail_round` 在 `handle.stopped` 或是「已被 interrupt」時不要升成 `exited`。

**D（Low，既有）：close 完成後才回來的 poke 會把記錄從 `exited` 寫回 `running`。**
`poke` 在鎖外送完之後無條件寫 `rec.state = STATE_RUNNING`。我在 3.4 之前就能重現：poke 的 `send` 被閘住、close 跑完（`exited`）、poke 放行——記錄變 `running`，但 handle 都是 stopped（沒有行程復活）。這在 `5429d3c` 就存在，**不是這輪引入的**，H1 也沒有讓它更糟。修法：poke 寫回前檢查 `rec.state != STATE_EXITED`（或在鎖內確認 handle 沒換過）。

**E（Low，既有）：`_run_periodic_imports` 不看 `busy`，可以和 close 自己的 import 併行。**
到期的 periodic import 和 close 的 `import → stop → import` 在不同執行緒（scheduler 與 HTTP），`import_external` 對同一個 agent 同時被呼叫兩次。實測 peak = 2。也存在於 H1 之前。影響小（同一個 external id，agora 端是原地更新），但兩個 agora 行程同時 import 同一個 Session 不是設計想要的。3.4 用 `new` 模式時順手觀察。

## 實作者的其他判斷

| # | 判斷 | 結論 |
|---|---|---|
| 1 | **opencode 一輪失敗就標 `EXITED` 但不殺行程** | **可以接受，但要在 3.4 觀察。** 這是 D6 的指定語意（失敗要寫進 `exit.reason`，不是只 log），而且不殺行程是安全的——`stop` 之後仍然會收。但它跟 A、C 是同一個家族：任何一次回合失敗都會讓 sidecar 認為 agent 死了，之後 poke 得到 409，控制器只能 close 再 open。3.4 連續跑長回合時看一下失败的頻率；如果 provider 偶發 5xx 會誤殺，就把它降成「記 `last_error`、留在 idle」。這是 3.4 的量測題，不是現在的裁定。 |
| 2 | **`message_timeout` 上限 1 小時** | **可以接受。** 這個逾時只在背景執行緒的 `POST /session/{id}/message` 上，不是 shutdown 路徑，不影響 supervisor 的 kill timeout（D2 的公式不變）。實作上還多了一層保險：`min(message_timeout, MESSAGE_TIMEOUT)`，而且 `Config` 沒有這個欄位、連 wiring 都沒有傳，**所以外部改不了上限**——這正是 design 要的「MUST 是長逾時（上限 1 小時）」。1 小時對 coding agent 的最壞回合夠用。 |
| 3 | **merge 在 `core.add_job` 拒絕（`merge_not_allowed_in_job`）** | **可以接受，而且位置對。** 放在 `add_job`（不是 HTTP 層）代表三種外部控制形式（`at`、`every`、`every+until`）與直接呼叫 `core` 的路徑都經過同一個門。我另外驗了三件事都擋住（`every` 形式、沒有 `at` 也沒有 `every` 的 job、以及 HTTP 層走的就是 `core.add_job`），而**同樣的內容用 `POST /agents` 直接呼叫仍然可行**（我實跑了一次真的 merge，`session_id` 有值）。15 分鐘的 summarizer 不會卡住 scheduler 執行緒。 |

## 規格面的修正（我做掉的）

實作者指出的 spec 矛盾是真的，我改在本 worktree：

1. **`specs/sidecar/spec.md` 的「new 模式的換把與關閉先 import」**：末句「import 失敗時換把 MUST NOT stop agent」與 40 行後新增的「new 模式換把前 import 失敗」（MUST stop）直接矛盾。已把舊 Requirement 改成只規定 **close** 路徑（close 的 import 失敗仍然停下、`exit.reason` 說明沒有寫回、close 不重試），並把矛盾的 Scenario「import 失敗時不停下」換成「close 前的 import 失敗仍然停下」。換把的失敗語意交給新的那條 Requirement，兩邊不再打架。換把成功路徑與 close 成功路徑的兩個 Scenario 一字未動。
2. **`design.md` 的 agora 路徑那句**：原本寫「從原始碼目錄開發時要用 `command` 設定指定 agora 的路徑」，但 `Config` 沒有這個選項（我查過 `config.py`：只有 `port`、`agents`、`close_timeout` 等，沒有 agora 路徑），`child_env` 也不傳 `PYTHONPATH`。已改成和實作一致：先用 `uv tool install` 從本 repo 裝；直接跑原始碼時要讓同一個 python 找得到 `agora` 套件（`uv run` 的同步 venv 或 `pip install -e .`）。這也和實作者寫的 `docs/sidecar.md` 第 7 節一致。
3. `tasks.md` 沒有動。`openspec validate sidecar-v0 --strict` 過。

## 建議順序

1. **可以進 3.4 PM 真機。**
2. 3.4 順手確認 A（opencode 當掉時狀態卡 `running`）、B（連續 poke 的 idle 誤報）、C（interrupt 後誤標 exited）——症狀對得上就修，都是 `drivers/opencode.py` 裡幾行。
3. D、E 是既有的，可以排到 3.4 之後。

## A～D 的確認

- 對象：分支 `sidecar-v0-agy4` 的 `3b54b7c`（A）、`9b0aec7`（B）、`d663a5b`（C）、`5db66b6`（D）；`git archive` 解到 `/tmp/a4`，測試與突變都在副本做。
- 審查者：架構師（Claude Sonnet 5.5）。只碰程式與假資料。
- 副本的 `tests/unit` 全部：1038 passed、1 skipped（與實作者回報相同）。

### 結論：**可以進 3.4。** 四項都照上一輪的描述修好，沒有引入新問題；有兩個測試漏洞（Low）。

| 項 | 修好了？ | 依據 |
|---|---|---|
| A | 是 | `_send_round` 外層多了 `except Exception`（`IncompleteRead` 之類轉成 `OpenCodeDriverError`）與 `finally: _finish_round`，兩個出口都清掉 `sending`。突變「拿掉 catch-all」→ 紅。 |
| B | 是 | `sending` 改成計數，`send` 加一、回合結束減一、歸零才設 `IDLE`；`interrupt` 與 `stop` 都歸零。突變：結束時直接設 0 → 紅；`interrupt` 不歸零 → 紅。 |
| C | 是 | `abort_epoch`：`send` 取快照，`interrupt`／`stop` 遞增；epoch 不符或已 stop 的回合失敗只記 `last_error`，不改狀態、不動計數。突變：失敗路徑不看 epoch → 紅；`interrupt` 不遞增 → 紅。 |
| D | 是 | `poke` 寫回前要 `rec.state != EXITED` 且 `rec.handle is handle`。突變：拿掉 EXITED 檢查 → 紅。 |

### 你問的兩件事

- **B 的計數在 interrupt／stop 時有沒有歸零**：有（`interrupt` 在 abort 成功後的鎖內、`stop` 在設 `stopped` 的同一個鎖內）。而且因為 epoch 遞增，被中止的舊回合之後回來**不會再減計數**，不會把 interrupt 之後新開的回合減成負的或誤報 idle。我另外寫了探針（副本裡，不進 repo）：send → interrupt → send 第二個 → 放行第一個，`state()` 仍是 `running` 直到第二個結束，綠。
- **D 的檢查會不會擋掉正常的 poke**：不會。守門條件在 poke 開頭已經要求 `running`／`idle`（排隊中的 `starting` 走另一條路），正常 poke 的寫回只在「這段期間記錄被 close 成 exited，或換了 handle」時才被擋掉；既有的 poke 測試全綠。

### Low（不擋）

1. **`finish` 路徑的 epoch 檢查沒有測試擋住。** 突變「成功回合不看 epoch」→ 全綠（61 passed）。壞處是：interrupt 之後開了新回合，舊回合稍後成功回來會把新回合的計數減掉、提早報 idle。我的探針（上面那個）在這個突變下轉紅，建議把它收進 repo 的測試。
2. **`poke` 的 `rec.handle is handle` 沒有測試擋住。** 突變拿掉 → 全綠。換把（handle 換了）與 poke 的 send 交錯的情形只有 EXITED 那一半有測試。
3. 觀察（不是問題）：`interrupt` 是 abort 成功之後才遞增 epoch，若 server 在 abort 回應之前就讓舊回合失敗，那一次失敗會走 `exited` 路徑；但 `interrupt` 隨後在鎖內把狀態改回 `IDLE`、計數歸零，探針（abort 回應慢 0.8 秒、回合 0.3 秒就失敗）顯示最終狀態不是 `exited`。時間窗極小，3.4 實機要是看到 poke+interrupt 後偶爾 `exited` 再回頭看。
4. 等價突變：「結束時不判斷計數就設 IDLE」不會紅，因為 `state()` 以 `sending` 為準先回 `running`；「`stop` 不歸零」不會紅，因為 stop 之後本來就是 exited。這兩個不算漏洞。
