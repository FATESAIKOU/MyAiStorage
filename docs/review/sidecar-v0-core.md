# #31 sidecar v0 核心（tasks 2.1～2.6）的非作者審查

- 對象：分支 `sidecar-v0-agy4`（impl3），`bf9d737`..`eb56a99`；`git archive` 解到 `/tmp/s31`，突變與探測都在副本或 `/tmp` 做，沒有動 impl3 的 worktree。
- 對照：本分支 `openspec/changes/sidecar-v0/`（含 6b5957b、6e5df9c、c61e7b1）與 `docs/review/v4-consistency.md`；LLMGateway 的 lgw 程式（只讀 `impl/v4-connect` 分支的 `lgw/client.py`、`lgw/connect.py`）。
- 審查者：架構師（Claude Sonnet 5.5）。
- 基準：副本上 `-k sidecar` 90 passed（和 impl3 回報一致）。

## 結論

**要先修，才進第 3 節。** 規格面的行為幾乎都有做、也有測：24 個我自己挑的突變，22 個轉紅；key 沒有出現在 status、`/jobs`、log；真的 SIGTERM 能乾淨結束。但有三個會在真環境壞掉的問題：

1. **`LLMGatewaySource` 和真的 `lgw.client` 不相容（H1）**：用真的 lgw，每一次 `list`、`get` 都會變成 Unreachable，`cooldown` 的失敗被當成成功。測試用的是 impl3 猜的 dict 形狀替身，所以全綠。
2. **兩個執行緒同時看到「額度用完」會做兩次換把（H2）**，第二次把剛換好的新 agent 停掉，最後落在 waiting-quota 而且沒有 job。`GET /agents` 本身就會觸發這件事。
3. **`close` 遇到還在 `starting` 的 agent 會漏掉一個行程（M1）**。

另外 `JobRegistry` 沒有鎖（M2）、換把中途失敗會讓 agent 靜默消失（M3）。都不大，一起修。

## 問題

### High

**H1. `LLMGatewaySource` 假設的 lgw 介面是猜的，和真的不一樣**
- 位置：`src/sidecar/sources.py:228-287`（`_BoundSource`、`_failure`）、`:171-199`（`operation`）、`:202-209`（`_import_lgw`）。
- 真的 `lgw.client.Client`（`impl/v4-connect`）每個方法回 `Result(ok, status, data, error, unreachable)`：
  - `list()` → `data` 是 `{schema, subscriptions: [...]}`；
  - `get()` → `data` 就是那一筆（平的，含 `key`）；
  - `cooldown()` → 204，`ok=True`、`data={}`；
  - 失敗 → `ok=False`、`status`（400／403／404／409）、`error` 是字串碼（`"agent_not_allowed"`、`"not_deployed"`…），連不上是 `unreachable=True`、`error="unreachable"`。
  - 建構式 `Client(base_url, timeout=10)`（impl3 猜的 `Client(base_url)` 剛好也能用）。
- impl3 的 `_BoundSource` 把回傳值當 dict 處理（`data.get("unreachable")`、`data.get("error")`、`data.get("subscriptions")`）。實測（真的 `lgw/client.py`＋本機 loopback 假 HTTP 服務，回 `LLMGateway` spec 的三個端點）：
  | 呼叫 | 實際結果 |
  |---|---|
  | `list()` | `Unreachable("LLMGateway list answered something unexpected")` |
  | `get(id, "claude-code")`（200，含 key） | `Unreachable("subscription row must be a JSON object…")` |
  | `get(id, "opencode")`（403） | 同上的 `Unreachable`，**不是** `SourceError("agent_not_allowed")` |
  | `cooldown(…, None)`（204） | `None`（對，湊巧） |
  | `cooldown(…, "bad")`（400） | `None`——**把失敗當成功**，D4 的「去掉 `resets_at` 重送」永遠不會觸發 |
  結果：真環境裡 open 一律 503，換把一律走 unreachable 那一支。
- 第二個洞：`operation()` 只把 `connect.connect(cfg)` 的**建立**包在 try 裡（`:191-197`），但 `contextlib.contextmanager` 的錯誤是在 `with ctx as base_url`（進入時）才丟（真的 `lgw.connect.connect` 的 `Unavailable` 就是在 `_wait_ready` 之後的 `__enter__` 裡丟）。實測：`Unavailable("timeout")` 直接穿出 `operation()`，不是 `Unreachable`；在 open 路徑變成 HTTP 500（不是 503），在 `_quota_exhausted` 路徑變成 `GET /agents` 的 500。
- 修法（見下面「lgw 接點」一節）。

**H2. 兩個執行緒同時觀察到同一個 quota 事件，換把做兩次，第二次把新 agent 停掉**
- 位置：`src/sidecar/core.py:468-501`（`refresh_states`／`_refresh_one`）、`:503-597`（`_quota_exhausted`）；`GET /agents` 與 scheduler 的 `tick()` 都呼叫 `refresh_states`。
- 檢查「這個 handle 還是現在的」只在 `driver.state()` 回來那一刻（`:489`）；進入 `_quota_exhausted` 之後沒有任何保留或重檢查，而換把包含網路操作（`cooldown`＋`list`＋`get`，各自走 SSH），要好幾秒。
- 實測（假 driver 把 handle 設成 `quota_exhausted`，`cooldown` 睡 0.4 秒，兩個執行緒同時 `refresh_states()`）：
  - `cooldown` 被登錄兩次（`claude-personal` 的 5h）；
  - `driver.stop` 兩次，第二次打到的是**第一個執行緒剛換好的新 handle**；
  - 最後 `state=waiting-quota`、`subscription=claude-company`、`GET /jobs` 是空的、`reopen_at=None`：agent 被停掉、沒有 job、沒人知道。
- 這不是人為的：控制者每隔幾秒輪詢 `GET /agents`（也是 MyLinuxPool 的健康檢查），scheduler 每秒 `tick`。
- 建議：
  1. 每個 `AgentRecord` 加 `busy`（或每個 id 一把鎖）：`_refresh_one` 在進入 quota／exited 處理前以 `with lock` 檢查並設定 `busy`，已經 busy 就直接回去；
  2. 更根本的做法見 M4：**只有 scheduler 行程改變狀態**，`GET` 只讀快取。

### Medium

**M1. `close` 遇到還在 `starting` 的 agent：回 exited，但行程之後還是被開起來而且沒人管**
- 位置：`core.py:298-325`（`close`）與 `:230-296`（`open`）。`open` 把 placeholder 放進表之後才跑 `store.start`／`driver.launch`（可能好幾秒），這段時間 `rec.handle is None`；`close` 看到 `handle is None` 就當作「沒有行程」直接標 exited。
- 實測（`launch` 被閘住時呼叫 `close`，再放行）：`close` 回 `exited`；`open` 完成後 state 仍是 `exited`，`driver.handles` 有一個**沒有被 stop 的 handle**（`stop_calls` 空）。shutdown 也不會處理它（`state != EXITED` 才關）。
- 建議：`close` 遇到 `starting` 時設一個 `closing` 旗標（或把 close 排進 `pending`）；`open` 完成後若看到旗標就立刻 `stop` 並回報 exited；或者 `close` 在 `starting` 時回 409（`agent_starting`，讓控制者重試）。補一個測試。

**M2. `JobRegistry` 完全沒有鎖，HTTP 執行緒與 scheduler 同時動它**
- 位置：`jobs.py:83-148`；`http.py` 的 `ThreadingHTTPServer`（每個請求一個執行緒）呼叫 `add_job`／`delete_job`／`list_jobs`，scheduler 執行緒呼叫 `due`／`finish`。
- 實測（兩個寫入、兩個 `list()`＋`due()`、一個 `delete`，2 秒）：81 次 `RuntimeError: dictionary changed size during iteration`；`_counter += 1` 不是原子的。
- 後果：`GET /jobs` 或 `tick` 偶發 500／丟 tick；`tick` 的例外被 `_scheduler_loop` 吞掉並 log，下一秒再來，所以平常不易察覺。
- 建議：`JobRegistry` 內部一把 `RLock`；`list()`／`due()` 在鎖內複製；測試用多執行緒打 1～2 秒（`test_sidecar_jobs.py` 目前沒有任何併發測試）。

**M3. 換把中途失敗（`store.start`、`driver.launch`、`driver.stop` 丟例外）：500、agent 被停掉、沒有 job**
- 位置：`core.py:550-588`；`_refresh_one` 不接 `_quota_exhausted` 的例外。
- 實測（`store.start` 丟 `RuntimeError`）：`GET /agents/t1` 丟出 `RuntimeError`（HTTP 500，連帶讓不相關的 `GET /agents` 也 500）；agent 被 stop；state 仍是 `idle`，下一次刷新變 `exited`（`reason: closed`）；`GET /jobs` 空。違反 D4「停下、寫回、掛 job」的精神：換把失敗等於資料沒丟但工作靜默結束。
- 建議：把 stop 之後的部分包起來：失敗就走 `_wait_quota(rec, now, reopen_at=now+retry_interval)`（和 unreachable 一樣的重試），並 log；`refresh_states` 對每個 agent 各自 `try/except`，一個 agent 的錯不要影響別的。

**M4. `GET /agents` 會觸發狀態轉移，包括換把（網路與 SSH）**
- 位置：`core.py:366-377`（`get`／`list_agents` 先 `refresh_states()`）。
- 後果：MyLinuxPool 的健康檢查（`GET /agents` 有回應就是「在跑」）在換把期間可能被卡 15 秒以上（`lgw.connect` 預設逾時 15 秒，再加 `cooldown`／`list`／`get` 各自的 10 秒），而且正是 H2 的觸發點。
- 建議：只有 scheduler 執行緒呼叫 `refresh_states`；`GET` 回記憶體裡最後一次的狀態（延遲最多一個 tick）。`status` 的「現況」精度只有 1 秒，不損失什麼。

**M5. 結束時 scheduler 還在跑，可能在 `shutdown` 的快照之後又開一個 agent**
- 位置：`cli.py:97-107`：`stop.set()` 之後直接 `core.shutdown()`，沒有等 scheduler 執行緒結束，也沒有「正在關閉」的旗標；`shutdown` 在 `ids = [...]` 取得快照之後，進行中的 `tick` 還可以 `open` 一個到期的 job。
- 建議：`shutdown()` 先設 `_closing`（`open`／`poke` 之後都回 503），再 join scheduler；`close` 全部做完才 `server_close`。補一個測試（job 到期與 shutdown 同時）。同時 N 個 agent 逐一 `close_timeout=30s`，最壞要 30×N 秒，supervisor 的 kill timeout 要比這大，或改成並行 close（文件化即可）。

**M6. 測試的缺口（我的突變裡漏掉的兩個、以及整類沒測的）**
- 漏抓：`is_deployed()` 永遠回 True（U15，未部署的 subscription 會被當作重開候選）；「解除時刻已過但仍不可用 → 改掛 `now+retry_interval`」（U23，impl3 第 6 點）。這兩個是 D4 / M3 的邊界，spec 有寫、測試沒有。
- 整類沒測：任何多執行緒（H2、M1、M2 都因此沒被抓到）；用「真的形狀」的 lgw 回傳（H1）；SIGTERM 真訊號。
- 其餘 22 個突變都紅：換把不排除自己、解除時刻取 min、IDLE 不算占用、interrupt 被忽略、close／delete 不清 reopen_job／reopen_at、綁 0.0.0.0、status 洩漏 key、對已結束的 poke 放行、403 對應錯、不去掉 `resets_at` 重送、resume 不送、shutdown 不關、不檢查 workdir、過去的 `at` 放行、失敗的自動 job 不重掛、merge `by` 用錯 env、`until` 被忽略、file source 的 cooldown 沒作用、unreachable 的 open 不是 503、poke 逾時不逾時、重開不刪舊 job。

### Low

- **L1　`poke`、`_flush_pending` 在持有 `RLock` 時呼叫 `driver.send`／`interrupt`**（`core.py:358-364`、`:696-710`），而檔頭 docstring 說「driver 與 store 的呼叫在鎖外」。真的 driver 的 `send` 若會阻塞（opencode 的 `POST /message` 等到一輪做完才回，design D6 說 driver 在背景送，但沒有保證）會把所有 status 凍住。改成鎖外呼叫。
- **L2　`new` 模式在第一次 import 之前遇到 quota：agent 被停掉卻沒有 session 可接**（`core.py:558-566`），`waiting-quota` 且沒有 job；對話只在 agent 自己的 session 檔裡。第 3.5 節要保證「第一次 import 發生在第一輪結束時」，或這裡改成不 stop、只標記，讓人處理。
- **L3　`every` 的 job 停頓後會連續補跑**（`jobs.py:143`：`next_run_at + every`，不是 `max(now, …)`）：scheduler 卡 10 分鐘，之後每秒補一次，直到追上。改成「下一次＝`max(next_run_at + every, now)`」。
- **L4　HTTP 請求本文沒有大小上限**（`http.py:118-123`），只綁 loopback；本人接受的取捨，順手給 1 MB 上限即可。
- **L5　`open` 內的 `input` 走 `poke`：在 starting 時會阻塞到 `poke_queue_timeout`，逾時後 open 回 409 但 agent 其實已經開了**（`core.py:294-295`）。目前 driver 只會回 running／idle，所以走不到；真的 driver 若有 starting 態就會踩到。第一則輸入應該排隊但不讓 open 失敗。
- **L6　`wiring.default_drivers`／`default_session_store` 回空**：`sidecar run` 現在能啟動但開不了任何 agent（`BadRequest unsupported agent kind`），符合「3.x 才做」；請確認 `docs/sidecar.md`（4.1）寫明這一點，避免 PM 以為壞了。

## lgw 接點的結論（對照 `impl/v4-connect` 分支）

先說現況：**LLMGateway 的 `origin/main` 只有規格，沒有 `lgw/` 程式**；程式在 `impl/v4-connect`（`lgw/client.py`、`lgw/connect.py`、`lgw/signals.py`、`pyproject.toml` 的 `lgw` 0.1.0，setuptools），還沒進 main、沒有 `lgw-v0.1.0` tag。

### sidecar 這邊要改的

1. **`_BoundSource` 改吃 `Result`**（`sources.py`）：
   ```python
   def _failure(r):                     # Result -> SourceError | Unreachable | None
       if r.unreachable:                return Unreachable("LLMGateway cannot be reached")
       if r.ok:                         return None
       code = r.error if isinstance(r.error, str) else str(r.status)
       return SourceError({403: "agent_not_allowed", 404: "not_found", 409: "not_deployed"}.get(r.status, code))
   list : r = client.list();   rows = r.data["subscriptions"]
   get  : r = client.get(id, agent);  row = r.data      # 平的，含 key
   cooldown: r = client.cooldown(id, window, resets_at); return None if r.ok else _failure(r)
   ```
   400 要回 `SourceError("invalid_request"/error 字串)`，讓 `core._quota_exhausted` 的「去掉 `resets_at` 重送」真的觸發。
2. **`operation()` 把 `with ctx as base_url` 包進 try**，捕捉 `lgw.connect.Unavailable`（用 `str(e)` 即 kind）與一般例外，轉成 `_UnreachableSource`；注意 yield 之後的例外不能再 yield 一次（`@contextmanager` 的限制）：做法是先 `try: base_url = ctx.__enter__() except … : yield _UnreachableSource(...); return`，之後 `try: yield … finally: ctx.__exit__(...)`。
3. **`load_config` 的例外**：真的會丟 `OSError`（檔案不在）與 `lgw.connect.ConfigError`（`ValueError` 的子類）；現有的 `except Exception` 已涵蓋，OK。
4. **測試替身要換成真的形狀**：`FakeGatewayClient` 回 `Result`-like（`ok`／`status`／`data`／`error`／`unreachable`；可在 `_sidecar_fakes.py` 定義一個欄位完全相同的 dataclass，不 import lgw）；再加一個 `pytest.importorskip("lgw")` 的整合測試，用 loopback 的假 HTTP 服務打真的 `lgw.client.Client`（和我的探測一樣；`lgw` 不在 extra 時跳過）。
5. **一次邏輯操作一個 `with`** 已經做了（S1），OK；但要注意 `connect()` 預設逾時 15 秒，換把時 `cooldown`＋`list`＋`get` 在同一個連線裡，不要為了換把重連（現在的 `_source_op()` 是對的）。
6. `lgw.signals` 沒用到，OK（design 已寫「不是義務」）。

### 要問 LLMGateway PM 的

1. `impl/v4-connect` 什麼時候進 main、什麼時候打 `lgw-v0.1.0` tag？（sidecar 的 `gateway` extra 在那之前加不進 pyproject。）
2. `Result.error` 是否保證是服務的 `{"error": "<code>"}` 字串（我看到 `lgwd/errors.py` 是 `ApiError(403, "agent_not_allowed")`、body `{"error": "agent_not_allowed"}`）？400 的 `error` 會是哪些碼（`invalid_window`、`invalid_resets_at`…）？sidecar 用 `status` 為主、`error` 為輔，但想確認碼表。
3. `lgw.connect.connect()` 的錯誤只有 `Unavailable`（`kind` 五種）嗎？`load_config` 會丟 `OSError`／`ConfigError`，請確認。
4. `lgw.client.Client` 的 `timeout=10` 與 `connect(..., timeout=15)` 是否可由 sidecar 傳入（設定檔需要嗎）？

## impl3 自己判斷、和 design 不同的 10 點

| # | 做法 | 判斷 | 理由 |
|---|---|---|---|
| 1 | `gateway` extra 沒加進 pyproject，用註解＋lazy import 的明確訊息 | **可以接受** | tag 不存在，加了 `uv sync` 會壞；lazy import 的 Unreachable 訊息夠清楚。待 LLMGateway 打 tag 後補（上面「要問」第 1 題）。 |
| 2 | `SessionStore.start` 回 `RunningSession(session_id, process)` dataclass、多一個選填 `merge_env` | **可以接受** | 比 tuple 好讀；`merge_env` 正是 D5「`by` 不同種類時另外挑一把」需要的通道。請把 design D3 的表更新成 dataclass 與 `merge_env`。 |
| 3 | `new` 模式 core 直接跳過 `SessionStore`，`driver.launch(None, …)`，`status.session` 先 null 等 3.5 的 import | **可以接受（附 L2）** | 符合 D5「`new` 不經 agora 行程」；缺口是第一次 import 之前遇到 quota 會靜默丟掉 agent，3.5 要先於這個狀況，或在這裡先不 stop。 |
| 4 | `env_for` 只回 env dict（D3 的 `unset` 沒有獨立通道） | **可以接受** | 前提：driver 自己負責組出子行程的完整環境（不是繼承 sidecar 的環境再疊加），也就是 driver 必須在 `launch` 時把繼承來的 `ANTHROPIC_*`／`OPENAI_*` 等清掉。請在 3.1／3.2 的測試裡驗：父行程設了 `ANTHROPIC_API_KEY`，子行程的環境裡沒有。D3 的 `unset` 拿掉即可。 |
| 5 | open 省略 subscription 且完全沒候選 → 409 `no_subscription` | **可以接受** | design 沒給，但和 `no_subscription_for_merge` 對稱；請把它補進 D4 的錯誤表。 |
| 6 | 自動重開算出的解除時刻已過、候選仍不可用 → 改掛 `now + retry_interval` | **可以接受** | 避免每個 tick 重掛；合理。但**沒有測試**（突變 U23 沒紅），請補。 |
| 7 | poke 排隊逾時用真實 monotonic 秒，job 用注入時鐘 | **可以接受** | `Condition.wait` 本來就必須用真時間，用注入時鐘反而會永遠不逾時；job 用注入時鐘讓測試可控。測試只能用很小的真實逾時（`test_poke_while_starting_times_out_with_409`），可以接受。 |
| 8 | SIGTERM 只測到 `cli.run` 的 stop→finally→`core.shutdown()`，真訊號沒做整合測試 | **可以接受（我補驗了，另見 M5）** | 我用真的 `SIGTERM` 跑 `python -m sidecar.cli run`（隔離的 HOME、port 0）：1.5 秒內 exit 0、有印出 listening。沒有 agent 時只能驗到這裡；有 agent 的順序與 scheduler 競態見 M5。 |
| 9 | 第一筆 commit `bf9d737` 沒有 Co-Authored-By | **可以接受** | 規則禁止 amend；不影響行為。後面的 commit 都有。 |
| 10 | 真的 `lgw.client.Client(base_url)` 是猜的 | **建構式猜對了，回傳形狀猜錯（H1）** | 見上面 lgw 接點：要改。 |

## 建議順序

1. **H1**：改 `_BoundSource` 吃 `Result`、`operation()` 接住進入時的 `Unavailable`、替身換成真的形狀，再加 `importorskip("lgw")` 的 loopback 整合測試。
2. **H2＋M4**：只讓 scheduler 改狀態、`GET` 讀快取；補多執行緒測試（兩個執行緒同時 `refresh_states`、`close` 與 `open` 同時）。
3. **M1、M2、M3、M5**（`closing` 旗標、`JobRegistry` 鎖、換把失敗走 `_wait_quota`、shutdown 先擋新請求再 join scheduler）。
4. **M6**：補 U15、U23 兩個測試。
5. L1～L6 順手；D3／D4 的文字更新（dataclass、`merge_env`、`no_subscription`、去掉 `unset`）。
6. 問 LLMGateway PM 四個問題；他們 tag 之後把 `gateway` extra 加進 pyproject。
