## Context

本人 2026-10-11 定案設計提案 v4（`docs/proposals/aicompany-design-v4.md`，本人對 Q1～Q8 的回答在該檔的「本人的回答（10-11）」）。sidecar 是 agent 的生命週期控管：控制者用 HTTP 下四種指示，sidecar 用子行程開 agent，用 agora 繼承與寫回 Session，用 LLMGateway 拿 subscription。

2026-10-12 的交叉核對（`docs/review/v4-consistency.md`）與 PM 的決定已折進本文件。

本人 2026-10-12 中午看完介面圖後改成 **v5**（`design-v5-changes.md` 為唯一依據；以 v4 為底，下面只列改的地方，未列的不變）。v5 的系統分層（本人）：服務層（有狀態）LLMGateway、Sidecar；指令層（沒狀態或狀態外部持久化）Agora、Angareion、Sidecar 內的 AgentDriver／SessionStore、tunnel、aiagent（claude／opencode）；外部服務層 GitHub（各 repo）、Google Drive。機器層（Mac、手機、各 MLP 節點）由部署決定，不進本設計。

本人 2026-10-12 對 v5 疑點的裁定（`v5-decisions.md`）再把三件事定案：`open` 只收 agora id 或不給（fork／merge 的入口拿掉、store 的能力保留）；`/jobs` 保留現狀（at／every／until＋`GET`／`POST`／`DELETE`，不加功能）；opencode driver 改寫成 v2（先做 v2 server API spike；worker 映像的 opencode 一併升 v2）。本文件照裁定更新。

## Goals / Non-Goals

**Goals：** 對外只有四個動作；介面和實作分開（agent 種類、Session 保存處、subscription 來源、挑選規則都可以替換）；相依少；Mac 與 worker 同一份程式。

**Non-Goals：** 任何信道工具的收發（各實體自己做）；認證（只綁本機）；重啟後找回（狀態揮發）；同時租用上限、分辨是哪台 worker（v0 不做）；herdr。

## Decisions

### D1 對外：四個動作＋三種形式（HTTP，只綁 127.0.0.1）

| 動作 | HTTP | 參數 | 回傳 |
|---|---|---|---|
| open | `POST /agents` | `id`（呼叫方給，唯一；建議用共通命名 `<repo 名>-<pm|arch|impl|test>[-<id>]`——**這只是慣例，sidecar MUST NOT 驗證格式**，只做基本字元／長度檢查；不做嚴格的身分認證）、`agent`（`claude-code`／`opencode`）、`workdir`（必填；存在的絕對路徑，不存在回 400，sidecar 不建目錄）、`session`（選填的 agora session id；給＝在上面接續，不給＝新開）、`subscription`（選填，`{"id": …}`；省略時由 sidecar 用 `Selector` 挑）、`input`（選填，第一則輸入） | 201 與 agent 的狀態。id 已存在而且狀態是 starting／running／idle 時回 409；狀態是 waiting-quota 或 exited 時，用同一個 id 重開（取代舊的紀錄；waiting-quota 時順帶刪掉它的自動重開 job）。第一則輸入一律排進該 agent 的佇列（等它起來再送），不會讓 open 失敗。錯誤見 D4 的表 |
| close | `POST /agents/{id}/close` | — | 先等 agent 回報 `idle`（上限 `close_idle_wait`；sidecar 的 shutdown 路徑跳過），再送 SIGTERM 讓 agora 停下並寫回，逾時才 SIGKILL（D6）。回最後的狀態。agent 還在 `starting`（open 進行中），或正在做狀態轉移（`busy`，例如換把中）時：記下 `closing`、回 200（不回 409）；做轉移的執行緒在**把新的 handle 寫回之前**（以及 scheduler 刷新時在 ACTIVE 狀態）看到 `closing` 就停下行程並標 `exited`。最後是 `exited`，行程確實被停掉，不會因為換把而復活 |
| status | `GET /agents`、`GET /agents/{id}` | — | 只列它開的：`id`、`agent`、`session`（目前跑的那個 agora id；換把後不變）、`subscription`（id）、`state`（`starting`／`running`／`idle`／`waiting-quota`／`exited`）、`reopen_at`（只在 `waiting-quota` 而且有自動重開 job 時，否則 null）、`started_at`、`exit`；不含 key。**讀記憶體裡最後一次的狀態**（最多晚一個 tick），不觸發任何狀態轉移、不做網路操作（D2） |
| poke | `POST /agents/{id}/poke` | `text`、`interrupt`（選填，預設 false） | 送一則輸入；`interrupt` 先打斷這一輪。id 不存在回 404；`exited`／`waiting-quota` 回 409（不排隊）；`starting` 時排隊，起來之後再送，逾時（設定 `poke_queue_timeout`，預設 30 秒）回 409。要 agent 立刻讀下一則＝帶 `interrupt` 的 poke：跑著就先打斷再送，不跑就直接送 |

- 定時：`POST /jobs {"action", "args", "at"}`；輪詢：`POST /jobs {"action", "args", "every", "until"?}`；`GET /jobs`、`DELETE /jobs/{id}`。`action` 只能是上面四個之一（否則 400），`args` 和直接呼叫時相同；`at` 在過去回 400。
- **`/jobs`（本人 2026-10-12 定案）**：保留現在的樣子（at／every／until＋`GET`／`POST`／`DELETE`），越簡單越好，不再加功能。
- `GET /jobs` 每筆有 `id`、`action`、`args`、`at` 或 `every`／`until`、`last_run`（時刻）、`last_error`（錯誤碼與訊息，不含 key；沒有錯誤是 null）。job 失敗不自動重試，除了 D4 的自動重開 job。
- 只綁 `127.0.0.1`（埠由設定給），不做認證（本人 Q5）。遠端控制者自己用隧道連進來。
- 請求本文上限 1 MB，超過回 413。
- sidecar 進入關閉中（D2）之後，`open`、`poke` 與 `POST /jobs` 回 503 `shutting_down`；`status`、`close`、`DELETE /jobs` 照常。
- `every` 的下一次執行時刻是**原本節奏上第一個晚於現在的預定點**（`現在 + (every − 落後的時間 mod every)`）：scheduler 停頓之後不補跑，只補執行一次，再照原本的節奏走。（不是「上一次預定時刻 + every 與現在中較晚者」：那會在停頓後的下一個 tick 立刻再跑一次。）

### D2 揮發的狀態與結束

agent 清單、job 清單只在記憶體。重啟從空的開始，不找回（本人：不用管重啟）。因為重啟會殺掉它開的 agent（子行程），設定的改變一律不靠重啟或 SIGHUP（見 D7 的設定）。

**誰改變狀態（執行緒模型）**

- **只有 scheduler 執行緒改變 agent 的狀態**：刷新（問 driver 現況）、換把、自動重開、job 的執行都在它那裡做。HTTP 執行緒只做三件事：讀記憶體裡最後一次的狀態（`GET /agents`）、把請求排給 scheduler 或直接呼叫 driver（`close`、`poke`、`open` 的前置），以及登記／刪除 job。所以 `GET /agents` 不會因為換把的網路操作（SSH）而卡住，MyLinuxPool 的健康檢查也不會。
- **每個 agent 同一時間只有一個狀態轉移在進行**（每個 id 一把鎖或一個 `busy` 旗標）：兩個執行緒（或同一個 tick 內兩次）同時看到「額度用完」，只有一個進去換把，另一個看到 `busy` 就直接回去。換把中的 agent 不會被第二次停掉。
- **`close` 占用轉移**：`close` 在**同一個鎖內**檢查並設 `busy = True`（占用這個 agent 的轉移）再去停下行程，結束才釋放；所以 close 進行中，scheduler 不會對同一個 agent 開始換把或刷新（否則 close 還在 stop 時換把才開始，會讓已關閉的 agent 復活）。
- **`closing`（close 遇到轉移）**：`close` 遇到 `starting` 或已經 `busy`（轉移進行中）的 agent，只記下 `closing` 並回 200；轉移的執行緒在寫回新 handle 之前檢查它：看到就停掉剛啟動的行程、標 `exited`、不再 `starting`。scheduler 刷新時，ACTIVE 狀態的 agent 看到 `closing`（例如 `driver.state` 暫時失敗而漏了 open 的檢查）也停下並標 `exited`。所以任何情況下，被關閉的 agent 不會復活，也不會留下行程。
- **v0 只有一個 scheduler 執行緒**：一個 agent 的換把（網路加 stop，可能數十秒）會延遲其他 agent 的刷新、排隊中的 poke（逾時 30 秒）與 job。`new` 模式的 periodic import（D5，每個到期的 agent 最多 `close_timeout` 秒）也在這個執行緒裡做，多個 `new` agent 時同樣延遲其他 agent。這是 v0 接受的取捨（`docs/sidecar.md` 寫明）；之後可以每個 agent 的轉移各開一個工作執行緒（`busy` 已經是每個 agent 一個）。
- 一個 agent 的錯誤（driver 丟例外、agora 失敗）只影響它自己：該 agent 的轉移失敗走 D4 的 `waiting-quota` 重試，其他 agent 的狀態與 `GET /agents` 不受影響。
- **driver 與 store 的呼叫一律在鎖外**（`send`、`interrupt`、`stop`、`launch`、`SessionStore.start` 都可能阻塞）；鎖只保護記憶體裡的表。
- job 清單（`JobRegistry`）是執行緒安全的（HTTP 執行緒新增、刪除、列出，scheduler 執行緒取出到期的與重排，不會互相打斷或重複 id）。

**結束（SIGTERM）**

1. 進入「關閉中」：之後 `open`、`poke`、`POST /jobs` 回 503；到期的 job 不再執行（不會在關閉的途中又開一個 agent）。
2. 停掉 scheduler：`join(timeout = close_timeout + 5 秒)`；逾時只記 log，**照樣**進行下一步（不因為某一次 tick 卡住就讓 SIGTERM 卡住、讓 agent 變成孤兒）。
3. 對每個 agent 逐一 close（停下、寫回 agora；`new` 模式先 import，D5）。
4. 全部做完才結束。被強制殺掉就什麼都不留。

逐一 close 的最壞時間（審查 M3 後分兩條路）：`close` API 走三段＝等閒＋SIGTERM＋SIGKILL——一般 agent `close_idle_wait + close_timeout + 5` 秒、`new` 模式 `close_idle_wait + close_timeout × 2 + 5`（import → stop → import，每個 import 的逾時不超過 `close_timeout`，D3）。sidecar 收到 SIGTERM 的 shutdown 路徑跳過等閒，時間與舊版相同：一般 `close_timeout + 5`、`new` `close_timeout × 2 + 5`。supervisor（MyLinuxPool 的單元）的 kill timeout MUST 大於「shutdown 路徑各 agent 的最壞時間加總」（`docs/sidecar.md` 寫明；預設 `close_idle_wait` 300 秒、`close_timeout` 30 秒；shutdown 一般 35 秒、`new` 65 秒，乘以同時的 agent 數）。

### D3 內部介面（介面和實作分開）

| 介面 | 動作 | 實作 |
|---|---|---|
| `AgentDriver` | `launch(session, workdir, credentials, env) → handle`（`session`＝選填的 agora id；`credentials`＝subscription 的 key／endpoint 等；`env`＝子行程要的環境變數；driver 自己組出子行程的完整環境）、`send(handle, text)`、`interrupt(handle)`、`state(handle) → running／idle／exited／quota_exhausted(resets_at?)`、`stop(handle, graceful)` | `ClaudeCodeDriver`、`OpenCodeDriver` |
| `SessionStore` | `start(mode, ids, by, agent, workdir, tag, env, merge_env=None) → RunningSession`（dataclass：`session_id`、`process`；`merge_env` 是 `by` 與 `agent` 種類不同時要約用的環境，D5）（`agora continue … --headless`；`merge` 先 `agora merge`；`new` 不經這裡，見 D5；`mode` 的 continue／fork／merge 能力保留，本期對外只有 continue 與 new 兩條路）。**沒有 `sync`**：寫回是 agora 行程自己在定期與結束時做的，sidecar 只等它退出 | `AgoraSessionStore`（呼叫 `agora`） |
| `SubscriptionSource` | `list()`、`get(id, agent)`、`cooldown(id, until)`；每個方法回結果或「無法確認」（unreachable），不丟例外 | `LLMGatewaySource`（curl 本機 LLMGateway port）、`FileSubscriptionSource`（本機開發：讀一個 JSON 檔） |
| `Selector` | `choose(agent, subscriptions, exclude=[]) → id 或 None`；輸入是整份 `list`（含不可用的），實作自己過濾 | `PrioritySelector`：`available` 且 `agents` 含這個種類、不在 `exclude` 裡，照 `priority` 取第一個，相同時以 `id` 排序（結果確定）。v0 只有 priority，替換靠改程式（本人 Q8）；要換成外部注入的函式時 sidecar 本體不改 |

- 「subscription 要變成 agent 的哪些環境變數」與「怎麼看出額度用完」都是 `AgentDriver` 的知識（和 LLMGateway PM 一致：共通 JSON 裡沒有 `launch`）。v5 拿掉 `env_for`：sidecar `get` 到 subscription 後，把憑證與環境變數直接當 `launch` 的參數傳進去，driver 在 `launch` 裡面組出子行程的完整環境。driver 自己解析事件；`lgw.signals` 只是選用，**不是義務**（driver 看到的是 CLI 事件不是 HTTP 回應，多半用不到）。
- **【待本人確認：去掉 `env_for` 之後的環境放置（審查 H1）】** `agora continue --headless` 是 agent 的父行程，subscription 的憑證與環境變數必須在 `SessionStore.start` 時就放進 agora 行程的環境；`launch` 只在 `new` 模式真的啟動 agent。兩個選項：(a) 保留一個由 subscription 組環境的純函式（例如 `driver.build_env(credentials)`），在 `start` 之前呼叫，結果同時給 `start` 與 `launch`；`no_model` 照舊在 `start` 之前失敗、什麼都沒開。(b) 照現在寫（`launch` 才組環境），但明寫 `start` 之前由 driver 先 `validate(credentials)`、環境怎麼傳給 `start`，以及 `start` 之後失敗的收拾（停 agora、放鎖、不留半成品）。本文件先不改，等本人定：「不要 `env_for` 這個介面」還是「只要拿掉名字」——決定後照辦。
- 環境變數只放在子行程的環境，不上命令列，不寫進任何 log 或檔案。
- **逾時契約（適用 shutdown 路徑上的呼叫）**：close／換把／shutdown 會走到的呼叫——`AgentDriver.stop`、`send`、`interrupt`、`state`，以及 `new` 模式的 `import_external`——MUST 在 `close_timeout` 以內回來（`stop` 在逾時後強制結束再多等最多 5 秒，所以是 `close_timeout + 5`），逾時由實作自己處理（內部逾時，丟錯誤或回失敗），不能無限期阻塞——否則進行中的 tick 會卡住 SIGTERM（D2）。`launch` 與 `SessionStore.start` 只在 open 與換把時呼叫、不在 shutdown 路徑上，各有自己有上限的逾時（等 agora 印出固定行 60 秒、opencode 開始聽 15 秒）；**merge 是例外**：`agora merge` 要呼叫模型，上限 15 分鐘；這個能力保留在 store，本期沒有對外入口（D5）。`LLMGatewaySource` 同樣：直連 HTTP 每次請求有內部逾時（預設 10 秒），一次邏輯操作的總時間有上限。實作與測試要各自驗證（tasks 3.x）。（`close` 的整體最壞時間另含 D6 的等閒段，見 D2。）
- **driver 組出子行程的完整環境**，不是在 sidecar 自己的環境上疊加：父行程（sidecar）環境裡的 `ANTHROPIC_*`、`OPENAI_*` 之類的憑證與端點變數 MUST NOT 漏進子行程；只保留 driver 明確列出的少數必要變數（`PATH`、`HOME`、`LANG` 等）加上 `launch` 拿到的 env。因此不需要另外的 `unset` 通道。測試：父行程設了 `ANTHROPIC_API_KEY`，子行程的環境裡沒有。
- agent 種類 `claude-code`／`opencode` 對 agora 的 `--agent` 是 `claude`／`opencode`，對應表在 `AgoraSessionStore`。

### D4 subscription 與換把

LLMGateway 的共通 JSON 欄位以 LLMGateway 的 `gateway-v0` 規格（`specs/subscription-registry`）為準：`schema`／`id`／`provider`／`kind`／`agents`／`priority`／`models`／`endpoint`／`key`／`cooldown`／`available`。`schema` 比 sidecar 認得的更高時拒絕並說明，不默默解析。

**open 的流程**：有給 `subscription` 就用它的 id（**指定的在冷卻中也照辦**，由呼叫方決定；一開起來用完就走下面的換把）；沒給就 `list` → `Selector.choose`。然後 `get(id, agent)` → 憑證與環境變數直接當 `launch` 的參數 →（merge 的能力保留、沒有對外入口，見 D5）→ `SessionStore.start` → `launch`。

**LLMGateway 的錯誤怎麼處理**

| 情況 | open（控制者看到的） | 換把或自動重開途中 |
|---|---|---|
| `get` 403 `agent_not_allowed` | 400 `subscription_not_allowed`，什麼都沒開 | 當作這一把不能用，換下一個候選 |
| `get` 404 | 400 `subscription_not_found` | 同上 |
| `get` 409 `not_deployed` | 呼叫方指定的：409 `not_deployed`；`Selector` 挑的：換下一個候選，全部不行才回 409 | 換下一個候選；沒有就走「沒有」那一支 |
| unreachable（`list`／`get`／`cooldown` 任一個；含連線程式進入時的 `Unavailable`，D7） | 503 `gateway_unreachable`，什麼都沒開、不掛 job、不重試 | `stop`＋寫回、`waiting-quota`，掛一個 `now + retry_interval`（設定，預設 5 分鐘）的 open job；到時還是 unreachable 就再掛一次 |
| `open` 省略 `subscription`，`list` 裡沒有任何能用的候選（或全部 `get` 都被拒） | 409 `no_subscription`，什麼都沒開 | —（走「全部冷卻」那一支） |
| 換把途中任何一步失敗（`get` 之後的 `stop`、`SessionStore.start`、`launch` 丟例外或失敗） | — | `waiting-quota`，掛一個 `now + retry_interval` 的 open job（同 unreachable），log 原因；不影響其他 agent，也不讓任何 HTTP 回應變成 500。**換把失敗 MUST 設退避**（`swap_retry_at = now + retry_interval`）：到時間之前，scheduler 的刷新不再對這個 agent 重新進入換把（不會每個 tick 再登錄一次 cooldown、再 list 一次） |

404 刻意不用在 subscription 不存在上：`/agents/{id}` 的 404 已經是「沒有這個 agent」。

**恢復時間與 `cooldown`（v5：只送 `until`）**

- driver 盡量偵測「額度用完」與「恢復時間」，`state` 回 `quota_exhausted(resets_at?)`。
- `cooldown` 只收「何時能再用」：`cooldown(id, until)`（RFC 3339 UTC），**拿掉 `window`（5h／1w）**。driver 拿不到恢復時間時，sidecar 報「現在＋1 小時」（審查 M1）；LLMGateway 對同一把的重複登錄取**較晚**的 `until`，所以登錄只會延長、不會縮短。額度用完後 agent 已停下（`waiting-quota`），不會再自動刷新那一把——唯一會再登錄的時機，是自動重開 job 到時、那一次又被拒（用完），新報的 `until` 只會往後延。
- `resets_at` 一律在 driver 的出口轉成 RFC 3339 UTC（Claude 是 epoch 秒、opencode 是 epoch 毫秒）。`until` 被 LLMGateway 以 `until_out_of_range`／`invalid_until`（碼名見 LLMGateway v5）拒絕時，改報「現在＋1 小時」重送一次；其他的 400 不重送（登錄失敗也繼續換把，不依賴 `list` 的 `available`）。
- Claude 的 5h／1w 窗資訊：driver 照常解析（log 與內部判斷用），但不進 `cooldown`——core 只送 `until`（見 `docs/review/v5-impact.md` 的疑點）。

**換把**（driver 回報 `quota_exhausted(resets_at?)`）

1. `cooldown(id, until)`。登錄失敗也繼續，但**這一輪換把把剛用完的 id 排除在候選之外**（`Selector.choose(..., exclude=[id])`），不依賴 `list` 的 `available`，避免挑回同一把原地打轉。
2. `list` → `Selector.choose`：
   - 有：`get` → `stop`（溫和）→ 等 agora 行程退出（它在結束時寫回）→ `SessionStore.start(continue 同一個 Session)` → `launch`；id 不變；成功後送一則 `resume_prompt`（設定，預設「額度恢復了，請接著剛才的工作」；設成空字串就不送）。`new` 模式沒有 agora 行程，換把前先 import（D5）。
   - 這整段是這個 agent 的**一個**狀態轉移（D2）；中途任何一步失敗見上面的表。
   - 沒有：`stop`（溫和）→ 等 agora 行程退出；`state = waiting-quota`，並自動掛一個 job（算式如下）。
3. 「停下」與「寫回」的順序一律是：先溫和停下 agent，再讓 agora 把最後的對話寫回（close、換把、全部冷卻都一樣）；要等 agora 行程退出才能再 `continue`（同一個 Session 的鎖）。

**自動重開 job**（不是新的對外動作；`GET /jobs` 看得到、可以刪）

- `action = open`，`args` 是同一個 id、同一個 agent 種類與工作目錄、`session = continue 目前跑的那個 Session`，**不帶 `subscription`**（到時由 `Selector` 重新挑）、不帶 `input`（成功後送 `resume_prompt`）。
- `at` 的算式：候選＝`agents` 含這個種類、**key 已部署**的 subscription（`available` 為 false 而且沒有 `until` 的，就是沒部署，不算）；每一把的解除時刻＝它的 `until`（沒在冷卻的就是現在）；`at`＝各候選解除時刻的最小值。這個時刻只看 `agents` 與 `cooldown`，不經 `Selector`。
- 邊界：沒有任何候選時不掛 job，`waiting-quota`、`reopen_at` 是 null，由控制者決定；job 到時 open 失敗（挑不到、409）就重新算再掛一次，unreachable 用 `retry_interval`；控制者 `DELETE` 了這個 job，`reopen_at` 變回 null。
- **算出的解除時刻已經過了、候選卻仍不可用**（例如 LLMGateway 還回冷卻中）時，不要每個 tick 重掛：`at` 改成 `now + retry_interval`。
- **沒有部署 key 的 subscription 不是候選**（`available` 為 false 而且沒有 `until`＝沒部署），它不影響 `at`，也不會讓一個其實沒東西可開的 agent 被掛上 job。

### D5 Session 與 tag

本人 2026-10-12 裁定：`open` 的 `session` 參數就是選填的 agora session id（給＝在上面接續，不給＝新開：直接開 agent，之後走下面的 `new` 式 periodic import）。重開 exited 或 waiting-quota 的 id 時，呼叫方 MUST 從 `GET /agents` 的 `session` 取回再當 `session` 帶進來（v5 的 `open` 沒有「自動接續」）。`fork`／`merge` 的**入口拿掉**——由 PMO／PM 先在 agora 做好 Session 再以 id 交進來；`SessionStore` 的 fork／merge **能力保留**（介面與實作照舊），本期對外只有 continue（給 id）與 `new`（不給 id）兩條路。

- `SessionStore.start` 的 `mode` 能力（保留、本期沒有對外入口）：`continue`、`fork`、`merge` 都經 `agora continue … --headless`（`merge` 先用 `agora merge --agent <by>` 做出新的 Session 再 continue；摘要是呼叫模型的，所以要約的 env 一律來自 subscription，不用機器上自己的登入）。
  - `by` 與 `agent` 同種類：用 open 的那把 subscription 的憑證與環境變數。
  - `by` 與 `agent` 不同種類：替 `by` 的種類另外用 `Selector` 挑一把（`list` → `choose(by, …)`），經 `get(id, by)` 取得，把那把的憑證與環境變數當參數跑 `agora merge --agent <by>`；open 指定的 subscription 只用在 `agent`。錯誤同 D4 的表（403／404／409／unreachable 都使 open 失敗、什麼都沒開）；`by` 的種類沒有任何能用的候選時回 409 `no_subscription_for_merge`。
- **`new`**（agora 沒有「新建一個空 Session 再開 agent」的指令，這是 v0 的做法，已由 PM 確認）：sidecar 直接開 agent（沒有 agora 行程），agent 的 session id 從事件取得；每 `sync_period` 分鐘用 `agora import session --external-session-id <id> --agent … --add-tag sidecar:<id>` 存進 agora（再次 import 是原地更新）。第一次存進去之後，`status.session` 才有 agora id。
  - **close 與換把時一律先 import**：close 先在 `close_idle_wait` 內等閒（D6 第一段），再 import（得到 agora id，已經有的就是原地更新）→ 再 `stop`（溫和）→ 再 import 一次（原地更新，寫回停下之前最後的對話）→ `close` 到此結束；換把不等閒，直接 import → `stop` → import → `continue` 那個 agora id（之後走一般的 headless 路徑，不再是 `new`）。因此 `new` 的 agent 在任何情況下被停下之前，都已經有 agora id；沒有「停下卻沒有 Session 可接」的情形。
  - **換把前的 import 失敗（agora 或網路）**：**照樣 stop**——對話仍在 agent 自己的 session 檔裡（Claude 的 `~/.claude/projects/…`、opencode 的資料庫），沒有丟——標 `waiting-quota`、記住 `external_session_id`（以及已知的 `agora id`，如果有）。**由 scheduler 每 `retry_interval` 重試 import**（`GET /agents` 的 `reopen_at` 在這段期間是下一次重試的時刻）；import 成功後才掛一般的自動重開 job（D4：`continue` 那個 agora id、算出的解除時刻）。這段期間不會每個 tick 重新換把（D4 的退避）。
  - close 回 `exited` 之前如果 import 失敗，要在回應裡說明沒有寫回（`exit.reason`）；close 不重試（agent 仍然被停下）。
  - 還沒跑過任何一輪（沒有 `external_session_id`）就額度用完：stop、`waiting-quota`、沒有 job（沒有東西可以接，控制者決定）。
- **tag**：每個 open 出來的 Session 在標頭的 `tags` **附加** `sidecar:<id>`（本人 Q7）。附加不是取代：agora 的 `--header tags=…` 是整個取代（`overlay` 對 list 的語意），所以 agora 要提供附加的選項 `--add-tag <tag>`（可重複；`continue`、`fork`、`import` 都有；見 `session-continue` 的 delta）。不 fork 的 `continue` 會把 tag 加在原 Session 上，這是預期的；同一個 id 重複接續會重複附加同一個值（查詢以集合看待，重複無害）。sidecar 本身不讀這個 tag；用途是人或程式在 agora 上查得到。
- **同一 Session 的鎖（審查 L7）**：同一個 Session 不能同時被兩個行程接續（agora 的鎖）。`open` 指定一個正被鎖住的 Session 時 `SessionStore.start` 會失敗——open MUST 回 409 `session_locked`、什麼都沒開；換把／自動重開遇到鎖衝突比照其他 `start` 失敗（`waiting-quota` ＋ `retry_interval` 後再試）。
- **固定格式的 stderr 行**：`--headless` 時 stdout 屬於 agent，所以 agora 在 stderr 印固定格式的行（依前綴解析，不依順序）：`agora-session <id>`（agora 的 Session id；新 Session 的 id 就在這裡，sidecar 讀它設定 `status.session`）、`agora-agent-session <id>`（agent 自己的 session id，例如 opencode 的 `ses_…`；`OpenCodeDriver` 要用它對正確的 session 送 HTTP，`session-continue` 的 delta）、`agora-port <埠>`（opencode）。
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
  - `rateLimitType` 不在官方型別裡，實測有 `five_hour`；第三方整理的值還有 `seven_day`、`seven_day_opus`、`seven_day_sonnet`、`overage`（未經官方確認）。v5 不再分窗：這些值只記進 log 供人看，不進 `cooldown`（core 只送 `until`）。
  - 重置時刻取 `resetsAt`（epoch 秒），不必解析畫面文字；拿不到就不帶（core 會報「現在＋1 小時」，D4）。官方型別另有 `errorCode: "credits_required"`（claude.ai 訂閱的內含額度用完，要買額外額度才能繼續）：當作用完。
  - 事件裡還有 `overageStatus` 之類的欄位；已知有回報「`status: allowed` 但 `overageStatus: rejected` 時 `claude -p` 的 `result` 是空的」（claude-code 的 issue，已關閉）。driver 只認 `status` 與輔助訊號，不因 `overageStatus` 換把。
  - **輔助訊號只在 `kind == api-key`（沒有 `rate_limit_event`）時使用**；`kind == claude-subscription` 一律只看 `rate_limit_event` 的 `rejected`／`credits_required`。api-key 類的輔助訊號是唯一的訊號，也沒有窗口與重置時刻（core 會報「現在＋1 小時」，D4）。
  - **條件不能太寬**：Claude 對 429 的短暫速率限制會自己重試，一次 `system/api_retry`（`error: rate_limit`）不是額度用完。要等**重試用盡**（`attempt >= max_retries`）或**該回合以 `is_error: true` 的 `result` 結束**，才算；`result` 文字以 `You've hit your` 開頭也**要同時 `is_error: true`**（一般回覆碰巧以這句話開頭不算）。暫時性 429 之後回合成功 MUST NOT 標額度。

**opencode（v2；本人 2026-10-12 裁定）**：driver 改寫成 opencode v2（Mac 上已是 2.0.26）。v2 的 server API 先做 spike（tasks 1.4），介面（開 server、送訊息、中斷、狀態、額度、session id 的取得）以 spike 結果為準，**不得照 v1 的形狀猜**。spike 在 Mac 上用 `--standalone` 開私有 server 做，不動共用的背景服務。本人實測 v2 TUI 的緊急插播要先按 esc 兩次再送；server API 的對應（打斷要用哪個呼叫、以及確認「打斷後再送」不會只排進佇列）列 spike 必查（tasks 1.4）。worker 映像的 opencode 也要升 v2（MyLinuxPool 開單；版本與 driver 一起釘）。`launch` 的 key 與 baseURL 給法在 v2 上重驗（tasks 3.2）。

**以下 v1＝opencode 1.18.35 的實測段落作廢**（保留供對照，不代表 v2 的行為）：

**opencode 1.18.35**：`opencode serve --hostname 127.0.0.1 --port <埠>`（工作目錄設成 agent 的工作目錄；opencode 依目錄分專案）。埠由 sidecar 分配（取一個空的），經 agora 的 `--headless-port` 交給 opencode（opencode 的 headless 必填，沒給 agora exit 1、什麼都不開）；agora 在 stderr 印 `agora-port <埠>`——**印出時 server 還沒有開始聽**（先印、再開 agent），所以 `OpenCodeDriver` 要自己 poll 到埠連得上才送，逾時當作啟動失敗；session id 用 agora 印的 `agora-agent-session <id>`，不要自己去猜。
- 送：`POST /session/{id}/message`（這個呼叫會等到這一輪做完才回，所以 driver 在**背景執行緒**送）。這個呼叫的逾時 MUST 是長逾時（上限 1 小時；不是一般請求的 10 秒，否則長回合會被切斷）；**`prompt_async` 與 `GET /session/status` 對閒置 session 的形狀由 PM 在 3.4 實機確認後再定**（binary 裡有 `prompt_async` 這個路由）。
- **狀態來源**：背景送出的執行緒在跑＝`running`，結束＝`idle`（不依賴 `/session/status` 的 map 裡有沒有這個 session）；`GET /session/status` 與事件只用來偵測 `retry`（額度）。送出結束時回應的 `info.error` 或非 2xx MUST 寫進 `last_error`／`exit.reason`（不含 key），不是只 log。
- 中斷：`POST /session/{id}/abort`，之後狀態變 `idle`。
- 接續：對 server 用同一個 session id 送就是接續。
- **額度（實測，Zen 免費額度用完時）**：`session.status` 變成 `{"type": "retry", "message": "Free usage exceeded…", "action": {"reason": "free_tier_limit", …}, "next": <epoch 毫秒>}`。
  - **額度用完＝`retry` 且 `action.reason` 在白名單內**。白名單放設定（`quota_reasons`，預設只有觀察到的 `free_tier_limit`；之後只改設定就能加）。`retry` 也用在暫時性錯誤（連線、過載），那些 `next` 只有幾秒；不在白名單的 `retry` 只 log，狀態維持 `running`，不換把。
  - 重置時刻取 `next`（毫秒，轉成 RFC 3339 UTC）；沒有 `next` 或不是時間就不帶（core 會報「現在＋1 小時」，D4）。v5 不再分窗：`cooldown` 只收 `until`，5h／1w 的對應規則拿掉。
- key 與 baseURL 的給法已在真 binary 上驗證（spike，2026-10-12，`docs/review/sidecar-v0-drivers-spike.md`），規則見下面「`launch` 的環境與憑證規則」。

**`launch` 的環境與憑證規則（v5；原 `env_for` 規則整組搬進 `launch`。Claude 在 2.1.296 上驗證過；opencode 的驗證是 v1 做的，v2 要重做，見 tasks 1.4／3.2）**

兩個 driver 共通：
- `launch` 拿到的只有憑證（key／endpoint 等）與環境變數（沒有 `unset` 通道）；子行程的環境由 driver 以 `subproc.child_env`（允許清單＋拿到的 env）組出，父行程的 `ANTHROPIC_*`、`OPENAI_*`、`OPENCODE_*` 等不進去（實測：假 provider 看到的 key 等於 subscription 的 key、不等於父行程的值）。因此「關自動更新與關模型清單」的變數 MUST 由 driver 自己放進子行程環境。
- 沒有可用的模型來源時 `launch` MUST 丟錯，open 回 4xx（`no_model`），MUST NOT 讓 agent 自己挑預設模型。
- 真的 HOME 下，使用者自己的設定會被合併（opencode 的 `~/.config/opencode/opencode.json`、Claude 的 `~/.claude`）；這次 MUST NOT 設 `OPENCODE_PURE`／`OPENCODE_DISABLE_PROJECT_CONFIG`，由 tasks 3.4 的 PM 實機確認（`docs/sidecar.md` 註明）。
- 驅動錯誤（起不來、HTTP 500 沒有可讀原因）的訊息 MUST 帶子行程 stderr 的**最新**末幾行（含啟動之後才印出的，不是啟動當下的快照），不要只有「500」。**只遮真正的秘密**：subscription 的 `key`，以及 `OPENCODE_CONFIG_CONTENT` 裡的 `apiKey` 值；其他環境值（`DISABLE_AUTOUPDATER=1`、模型名、endpoint）不是秘密，不遮（否則 `code 1` 會被遮成 `code <masked>`）。
- 打真 binary 的 loopback 測試 MUST NOT 放進預設的 unit（macOS 的 Keychain 不受隔離的 HOME 影響，而且會有對外流量）：放 `tests/integration/`，或要設 `SIDECAR_REAL_AGENT_TESTS=1` 才跑，預設 skip。`launch` 的環境輸出用純單元測試驗。

**opencode**（`launch` 裡組 `OPENCODE_CONFIG_CONTENT`）
- 輸出 `OPENCODE_CONFIG_CONTENT`、`OPENCODE_DISABLE_AUTOUPDATE=1`、`OPENCODE_DISABLE_MODELS_FETCH=1`。config 的形狀：
  ```json
  {"provider": {"<opencode provider id>": {"options": {"apiKey": "<key>", "baseURL": "<endpoint>"},
                                           "models": {"<model-id>": {"name": "<model-id>"}}}},
   "model": "<provider id>/<model-id>", "small_model": "<provider id>/<model-id>"}
  ```
- **provider id 對應表**（LLMGateway 的 `provider` → opencode 的 provider id）：`opencode-zen`→`opencode`、`openrouter`→`openrouter`、`ollama-cloud`→`ollama-cloud`（最後一個沒在真 binary 上驗，3.4 確認）。對應不到的 MUST 丟錯，MUST NOT 用原名。
- **`model` 與 `small_model` 必設**，兩個設同一個值（否則 opencode 自己挑，實測會挑到圖像預覽模型，而且每則訊息多一次標題請求用 small model）。來源依序：`subscription.models.default` → `models.allow` 裡第一個不含萬用字元的 → 沒有就拒絕（`no_model`）。
- **宣告 `provider.<id>.models`**：選出的模型 id 一定放進去（不在 opencode 內建登錄的模型不宣告會 500）。
- config 裡的 `apiKey` 優先於 opencode 已儲存的登入（實測），不需要 `OPENCODE_AUTH_CONTENT`。
- **首次回應核對**：第一則訊息的回應 `info.providerID`／`info.modelID` MUST 等於選出的（`provider id`、`model-id`）；不是就視為錯誤（log 與 `exit.reason`），不當作正常運作。

**Claude Code**（`launch` 裡組環境變數）
- `kind=claude-subscription`：`CLAUDE_CODE_OAUTH_TOKEN=<key>`。
- `kind=api-key`：`provider == "anthropic"` 用 `ANTHROPIC_API_KEY=<key>`（送 `x-api-key`）；其他 provider（例如 OpenRouter）用 `ANTHROPIC_AUTH_TOKEN=<key>` 加 `ANTHROPIC_API_KEY=""`（只送 `Authorization: Bearer`，和 LLMGateway design 決定 7 一致）。
- `endpoint` 有值時 `ANTHROPIC_BASE_URL=<endpoint>`。
- `models.default` 有值時 `ANTHROPIC_MODEL` 與 `ANTHROPIC_SMALL_FAST_MODEL` 都設成它；沒有 default 的 Anthropic 訂閱不設（用 Claude 的預設）。非 Anthropic 的 provider 沒有 default 時依 opencode 同樣的順序找（`allow` 第一個不含萬用字元的），沒有就拒絕（`no_model`）。
- 一律輸出 `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`、`DISABLE_AUTOUPDATER=1`。

**agora 的接法：選 (a)**——`agora continue` 加一個 `--headless`：照常準備 Session（continue／fork）、上鎖、定期寫回、結束時寫回，只是用上面的程式化模式開 agent，stdio 直接交給 agent（agora 自己在 agent 跑著時不寫 stdout，說明都寫 stderr）。sidecar 開 `agora continue … --headless` 這個行程，透過它的 stdio（Claude Code）或 opencode 的埠和 agent 說話。這樣接續、fork、寫回、同一個 Session 不能同時接續的鎖，都沿用 continue 已經測好的邏輯；agora 只多幾個選項（`--headless`、`--headless-port`、`--add-tag`）。
- **停下**（v5；審查 M3 改寫）：`close` 分三段：(1) **等閒**——等 driver 回報 `idle`（等手上這輪做完，不中途打斷），上限 `close_idle_wait`（預設 300 秒，可設定）；(2) **SIGTERM**——對 agora 行程送 SIGTERM（agora 轉給 agent、等它結束、做最後一次寫回、放鎖後退出），上限 `close_timeout`（預設 30 秒）；(3) **SIGKILL**——逾時才 SIGKILL agora（不做最後一次寫回，最多掉一個 `sync_period` 的對話；鎖是 `flock`，行程沒了就釋放）。sidecar 自己收到 SIGTERM（shutdown）時 MUST 跳過第一段、直接第二段；換把與全部冷卻的停下也不等閒（額度已用完，直接第二、三段）。強制停＝不等閒、直接 SIGKILL。退出＝結束整個 agent 程序（claude／opencode 行程本身）。

### D7 設定與 subscription 來源（v5：curl 本機 port，不用 `lgw connect`）

- `~/.config/sidecar/config.json`（`SIDECAR_CONFIG` 可覆寫）：
  - `port`（預設 8471；不在程式裡寫死，MyLinuxPool 的 `--check` 從設定讀）；
  - `subscription_source`（`llmgateway` 或 `file`＋路徑，見下）；
  - `agents`（各種類的執行檔路徑與額外參數）；
  - `sync_period`（分鐘，預設 5）；
  - `resume_prompt`、`retry_interval`（預設 5 分鐘）、`poke_queue_timeout`（預設 30 秒）、`close_timeout`（預設 30 秒）、`close_idle_wait`（預設 300 秒；`close` 第一段的等閒上限，shutdown 不用）、`quota_reasons`（opencode 的額度類 reason 白名單）。
- 不含任何秘密。v4 的 `gateway_connect_file`（`gateway-connect.json`）拿掉：沒有 `lgw connect`，沒有連線設定檔。
- `SubscriptionSource` 是 sidecar 內部的介面，不是獨立實體；兩種實作：
  - `{"kind": "file", "path": "<JSON 檔>"}`（本機開發：讀一個 JSON 檔，不變）；
  - `{"kind": "llmgateway", "base_url": "http://127.0.0.1:<port>"}`：sidecar 直接對本機的 LLMGateway port 做 HTTP（標準函式庫即可，不用 `lgw` 套件、不加 gateway extra）。本機 port 的位置由 `base_url` 設定給（欄位名已定案）。worker 上由 MyLinuxPool 的 LLMGateway capability 把 LLMGateway 帶到本機某個 port；Mac／手機用既有的 `mlp` 或 `ssh -J` 轉發。金鑰正本、通道建立與定期拉新都由 LLMGateway 側自理，sidecar 不管。
- 每次操作重新讀設定（`file` 種重讀檔案），不快取；讀不到、格式錯或連不上時，該次操作當 unreachable 處理（D4 的表）並 log 原因，不丟例外。
- `list`／`get`／`cooldown` 的語意與 D4 的表不變，只是傳輸從 `lgw.connect` 換成直連 HTTP；回應形狀沿用 `Result`（`ok`／`status`／`data`／`error`／`unreachable`），對應表不變，只拿掉 `invalid_window`（沒有 `window` 參數了）。`until` 被以 `until_out_of_range`／`invalid_until` 拒時改報「現在＋1 小時」重送一次（D4）；其他的 400 不重送。
- 測試：替身回和 `Result` 欄位完全相同的物件；另有一個打本地 loopback HTTP 的整合測試——不經 `lgw`、對著真的 LLMGateway 服務跑一次驗路徑與錯誤碼沒有漂移（預設 skip，放 `tests/integration/`）。

### D8 版本與健康檢查（MyLinuxPool 的 `install.sh --check` 用）

- `sidecar --version`：印出安裝時的 git sha（`uv tool install git+…@<sha>` 的套件中繼資料 `direct_url.json` 裡的 commit；從原始碼目錄跑時印套件版本並註明）。這是指令列的旗標，不是給 agent 的指示。
- 健康檢查不另加端點：`GET /agents`（status）有回應就是「在跑」，連不上就是「沒在跑」。對外仍只有四個動作。

## 部署與組織（本人 2026-10-12 裁定）

- **各台 sidecar 獨立**：一台機器一個 daemon、只聽 `127.0.0.1`（D1）；**不做 sidecar master**，沒有中央註冊或彙總——控制者直接對目標的 sidecar 下指令。
- **監看**：PMO 用 ssh 轉發（`ssh -L`）連到各台的 127.0.0.1 埠監看（`GET /agents`、`GET /jobs`）。
- **平常的溝通**：team 之間走 Angareion（信道）；sidecar 不做 agent 之間的訊息傳遞。
- **緊急與排程**：緊急打斷或排程由 PMO 對目標 sidecar 直接 `poke`（可帶 `interrupt`）或登錄 job（`POST /jobs`）；不經中央。
- **期 1 的機器**：Mac 不跑 sidecar（只當測試／開發用）；worker 由 MyLinuxPool 的 supervisor 拉起 `sidecar run`（D8、tasks 4.1）。

## PR 與合併順序（審查 L9）

- v5 的設計變更都在 `openspec/changes/sidecar-v0/` 這個 change 裡（沒有另開 `sidecar-v5` change）；`#31`（sidecar-v0）與 v5 設計 PR 的 diff 會落在同一批檔案上。
- `sidecar-v5` 以 `sidecar-v0` 的 branch 為 base 堆疊；合併順序（先 `sidecar-v0`／`#31` 再 v5，或先 rebase 到最新的 main）與 PM 對一下。

## Risks / Trade-offs

- [程式化模式沒有 TUI 可看] → 人看進度用 agora 的 Session 或 sidecar 的 log；之後若要，人可以另外用 herdr 管。
- [只綁本機、不認證] → 同一台機器上的任何行程（包含 agent）都能呼叫 sidecar；本人選的取捨。
- [狀態揮發] → sidecar 被強制殺掉時 job 清單會消失（自動重開 job 也是，waiting-quota 的 agent 要由控制者重新 open）；記憶在 agora 上。
- [agent 的 CLI 介面隨版本變動] → driver 是實作，版本和 sidecar 一起釘在 worker 映像裡。
- [opencode v2 的介面未定] → driver 改寫排在 spike（tasks 1.4）之後，不照 v1 猜；spike 用 `--standalone` 的私有 server，不干擾共用的背景服務；worker 映像升 v2 由 MyLinuxPool 開單。
- [現在＋1 小時的 fallback] → 對 5 小時的窗口＝每小時會自動重開一次、開起來又用完（本人接受的取捨；審查 M1）。
- [Claude 的 `rejected` 與 opencode 的額度類 reason 只觀察到少數值] → 未知值只 log 不換把；第一次真實用完時把原事件貼回 D6（tasks 3.4）。
- [`new` 模式不經 agora 行程] → 定期 import 之間若被強制殺掉，最多掉一個 `sync_period` 的對話（PM 已確認）；close 與換把一律先 import。
- [逐一 close 的時間] → `close` API 最壞 `close_idle_wait + close_timeout + 5`（`new` 模式再＋`close_timeout × 2` 的部分＝ `close_idle_wait + close_timeout × 2 + 5`）；sidecar 的 shutdown 路徑不變（一般 `close_timeout + 5`、`new` 65 秒）；supervisor 的 kill timeout 要大於 shutdown 路徑的加總（D2）。
- [`AgoraSessionStore` 以 `python -m agora.cli` 開 agora，`child_env` 的允許清單沒有 `PYTHONPATH`] → 用 `uv tool install` 裝的沒問題；從原始碼目錄開發時要用 `command` 設定指定 agora 的路徑（`docs/sidecar.md` 寫明；找不到模組時給明確的訊息）。
- [單一 scheduler 執行緒做 periodic import] → 多個 `new` agent 時延遲其他 agent 的刷新（D2，`docs/sidecar.md` 寫明）。
- [`sidecar run` 在 tasks 3.x 之前開不了任何 agent（沒有 driver、沒有 store）] → `docs/sidecar.md`（tasks 4.1）要寫明；`GET /agents` 仍會回應，所以健康檢查會過，但 `POST /agents` 回 400 `unsupported agent kind`。
