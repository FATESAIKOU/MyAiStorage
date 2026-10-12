## 1. spike（PM，先做；本人 Q4）

- [x] 1.1 Claude Code 程式化模式：在指定 Session 上開（resume）、送第二則輸入、中斷這一輪、從事件看出 running／idle／結束、額度用完的訊號與重置時刻、`--settings` 關掉 autoContinueAtUsageLimit
- [x] 1.2 opencode server 模式：同上（開在指定 Session、送、中斷、狀態事件、額度用完的錯誤）
- [x] 1.3 agora 的接法二選一（design D6 的 a／b），結果寫回 design D6，必要時補 `session-continue` 的 delta 與 tasks 第 3 節

## 2. sidecar 本體（負責：agy，各自的 worktree）

- [x] 2.1 HTTP 控制面（只綁 127.0.0.1）：`/agents` 的四個動作（含 D1 的錯誤碼、`workdir` 檢查、poke 的排隊與逾時）、`/jobs`（at／every／until、列出、刪除、`last_run`／`last_error`、動作與 `at` 的檢查）；狀態只在記憶體
- [x] 2.2 內部介面：`AgentDriver`、`SessionStore`（沒有 `sync`）、`SubscriptionSource`（回結果或無法確認，不丟例外）、`Selector`（`PrioritySelector`，`exclude`、同 priority 以 id 排序；v0 只有 priority，替換靠改程式）；假實作給測試用
- [x] 2.3 open／close／status／poke 的流程（design D1、D4、D5）；SIGTERM 時先 close 全部再結束
- [x] 2.4 換把與自動重開 job（D4）：排除剛用完的一把、`cooldown` 被拒絕時去掉 `resets_at` 重送、`at` 的算式、沒有候選與到時挑不到的處理、unreachable 的固定間隔、成功後送 `resume_prompt`
- [x] 2.5 `FileSubscriptionSource`（本機開發）與 `LLMGatewaySource`（extra `gateway` 依賴 `lgw` 的固定 tag；一次邏輯操作一個 `lgw.connect.connect`；每次操作重新讀 `gateway-connect.json`，不快取；403／404／409／unreachable 對應 D4 的表；見 design D7）
- [x] 2.6 測試：spec 的每個 Scenario（假實作，不開真的 agent、不連網）；環境變數不出現在命令列與 log；連線設定檔被原子改寫後下一次操作用新的；每項在副本改壞確認會紅

- [ ] 2.7 審查修正（架構師核對報告 `docs/review/sidecar-v0-core.md`；openspec 71247fc）
  - [x] 2.7.1 H1：`LLMGatewaySource` 改吃 lgw `Result`（ok／status／data／error／unreachable），對應表照 D7；`operation()` 接住進入 `with` 時的 `Unavailable`；假替身換成同欄位的 Result-like dataclass；加 `importorskip("lgw")` 的 loopback 整合測試
  - [x] 2.7.2 H2＋M4：只有 scheduler 執行緒改變 agent 狀態（`GET` 讀最後狀態）、每個 agent 同時只有一個狀態轉移（busy）
  - [x] 2.7.3 M1：`close` 遇到 `starting` 記 `closing`，open 完成後立刻 stop 並回報 exited
  - [x] 2.7.4 M2：`JobRegistry` 執行緒安全（1～2 秒併發測試）
  - [x] 2.7.5 M3：換把中途失敗 → waiting-quota＋`now + retry_interval` job＋log；一人的錯不影響別人
  - [x] 2.7.6 M5：shutdown 先進入關閉中（open／poke／新 job 503）、停 scheduler、再逐一 close
  - [x] 2.7.7 M6：補 U15（未部署不是重開候選）、U23（解除時刻已過 → `now + retry_interval`）測試
  - [x] 2.7.8 L1／L3／L4／L5
  - [x] 2.7.9 N1＋N4：close 遇到 `busy` 記 `closing`；`_swap` 在 stop 前與新 handle 寫回前看到 `closing` 就停新行程、標 exited；`_refresh_one` 在 ACTIVE 看到 `closing` 也停並標 exited（架構師探測成測試）
  - [x] 2.7.10 N2：cli `scheduler.join(timeout=close_timeout+5)`（逾時記 log 照樣 close）；driver／store 每個呼叫都有內部逾時（≤ close_timeout）＋逾時測試
  - [x] 2.7.11 N5：換把完成後 `busy` 有釋放、再觸發額度用完仍會換的測試
  - [x] 2.7.12 N6：413 測試改成讀到連線關閉（在 3.1 的 commit `c95d268` 內已改好）：driver 呼叫在鎖外、`every` 不補跑、本文 1 MB → 413、open 第一則輸入排隊不失敗

## 3. agent driver 與 agora（spike 之後依 D6 的結論）

- [x] 3.0 `AgoraSessionStore`（sidecar 側，PM 新開）：continue／fork／merge 經 `agora continue … --headless`（merge 先 `agora merge --agent <by>`）、`--add-tag sidecar:<id>`、依前綴讀 stderr 的 `agora-session`／`agora-port`／`agora-agent-session`、close 用 SIGTERM 溫和停並等寫回；env 不出現在命令列；所有子行程呼叫有逾時
- [x] 3.1 `ClaudeCodeDriver`：額度只認 `status == rejected`（未知值 log 原事件、當沒用完）、`rateLimitType` 的對應、api-key 類只靠輔助訊號、`credits_required`；`resets_at` 轉成 RFC 3339 UTC
- [x] 3.2 `OpenCodeDriver`：額度類 reason 白名單（設定 `quota_reasons`）、`next`（毫秒）的轉換與窗口的單一規則（> 5 小時＋5 分鐘 → `1w`，否則 `5h`）；**在 opencode 1.18.35 上驗證 key 與 baseURL 的給法**（例如 `OPENCODE_CONFIG_CONTENT`），結果寫回 D6
- [x] 3.3 agora：`continue --headless`、`--headless-port`、`--add-tag`（`continue`／`import`）、stderr 的 `agora-session`／`agora-port`、SIGTERM 的溫和停下，與測試（fake agent 的 headless 模式）
- [ ] 3.6 `env_for` 與 driver 錯誤的 spike 修正（架構師的 drivers spike；PM 派）
  - [x] 3.6.1 `OpenCodeDriver.env_for`：provider id 對應表；`model`／`small_model` 必設（無來源丟 `no_model`）；宣告 `provider.<id>.models`；`OPENCODE_DISABLE_AUTOUPDATE`／`OPENCODE_DISABLE_MODELS_FETCH`；首次回應核對 providerID／modelID
  - [x] 3.6.2 `ClaudeCodeDriver.env_for`：非 anthropic 的 api-key 用 `ANTHROPIC_AUTH_TOKEN`＋`ANTHROPIC_API_KEY=""`；`models.default` 設 `ANTHROPIC_MODEL`＋`ANTHROPIC_SMALL_FAST_MODEL`；`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`／`DISABLE_AUTOUPDATER`
  - [x] 3.6.3 driver 的錯誤訊息帶子行程 stderr 末幾行（遮掉 key 與 `env_for` 的值）
  - [x] 3.6.4 打真 binary 的 loopback 測試放 `tests/integration/`（`SIDECAR_REAL_AGENT_TESTS=1` 才跑，預設 skip）
- [ ] 3.4 PM 在 Mac 上用真的 agent 跑一次：open（fork）→ poke → poke＋中斷 → close；換把用 `FileSubscriptionSource` 模擬；**找一次真的額度用完，把原事件（去掉秘密）貼回 D6**，確認 `rejected` 與 opencode 的 reason
- [x] 3.5 `new` 模式（D5，PM 確認中）：不經 agora 行程，用 `agora import --add-tag` 定期與 close 時存進 agora
- [ ] 3.7 審查修正（架構師第 3 節審查；docs/review/sidecar-v0-section3.md）
  - [x] 3.7.1 H1：close 在同一個鎖內設 busy，stop 做完才釋放；_refresh_one 略過 busy
  - [x] 3.7.2 M1：Claude 輔助訊號限 api-key、api_retry 等重試用盡或 is_error、文字要求 is_error
  - [x] 3.7.3 M3：只遮真正的秘密（key 與 config 內 apiKey），不遮 1／模型名／endpoint
  - [x] 3.7.4 M5：AgoraProcess 持有 pump deque 或 callable 取最新末幾行
  - [x] 3.7.5 L1：poke 時 driver 例外轉 409 agent_not_running 並標 exited
  - [x] 3.7.6 L3：agora 3.3 SIGTERM handler 在 Popen 前抵達記旗標，Popen 後轉送
  - [ ] 3.7.7 H2：new 模式換把前 import 失敗也 stop、標 waiting-quota、scheduler 重試 import 後掛 job
  - [ ] 3.7.8 M2：OpenCodeDriver 長逾時、狀態以背景送出在跑為準、錯誤寫進 exit.reason
  - [ ] 3.7.9 M4：import 逾時 ≤ close_timeout、job open 禁 merge、docs/sidecar.md 修正公式與 L2/L4

## 4. 文件與收尾

- [x] 4.1 `docs/sidecar.md`：HTTP 介面、設定、連線設定檔（欄位、原子改寫、worker 上由 MyLinuxPool 的單元從 `gateway.json` 產生、known_hosts 放 state 目錄 0600）、在 Mac 與 worker 上怎麼跑（worker 由 MyLinuxPool 的 supervisor 拉起 `sidecar run`）
- [x] 4.2 `sidecar --version` 印安裝時的 git sha（design D8）；從 branch `aicompany-p1` 可沿用的只有三段終止的做法，其餘（herdr、收信迴圈、中繼、佇列、check）不移植
- [ ] 4.3 完整 unit 綠；非作者審查（Muse Spark 1.3 xhigh）
- [ ] 4.4 開 PR（b，PMO 先跟本人說明再 merge）
