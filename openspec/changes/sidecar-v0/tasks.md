## 1. spike（PM，先做；本人 Q4）

- [x] 1.1 Claude Code 程式化模式：在指定 Session 上開（resume）、送第二則輸入、中斷這一輪、從事件看出 running／idle／結束、額度用完的訊號與重置時刻、`--settings` 關掉 autoContinueAtUsageLimit
- [x] 1.2 opencode server 模式：同上（開在指定 Session、送、中斷、狀態事件、額度用完的錯誤）
- [x] 1.3 agora 的接法二選一（design D6 的 a／b），結果寫回 design D6，必要時補 `session-continue` 的 delta 與 tasks 第 3 節
- [ ] 1.4 opencode v2 server API spike（本人裁定：v1 的細節作廢；Mac 上已是 2.0.26，用 `--standalone` 開私有 server，不動共用背景服務）：開 server、送訊息、中斷、狀態、額度、session id 的形狀；打斷的方法（TUI 的緊急插播是先 esc 兩次再送——server API 的對應）與確認「打斷後再送」不會只排進佇列；結果寫回 D6，並定 3.2 的改寫範圍

## 2. sidecar 本體（負責：agy，各自的 worktree）

- [ ] 2.1 HTTP 控制面（只綁 127.0.0.1）：`/agents` 的四個動作（含 D1 的錯誤碼、`workdir` 檢查、poke 的排隊與逾時）、`/jobs`（at／every／until、列出、刪除、`last_run`／`last_error`、動作與 `at` 的檢查）；狀態只在記憶體
- [ ] 2.2 內部介面：`AgentDriver`、`SessionStore`（沒有 `sync`）、`SubscriptionSource`（回結果或無法確認，不丟例外）、`Selector`（`PrioritySelector`，`exclude`、同 priority 以 id 排序；v0 只有 priority，替換靠改程式）；假實作給測試用
- [ ] 2.3 open／close／status／poke 的流程（design D1、D4、D5）；SIGTERM 時先 close 全部再結束（v5 的 session／id 改動屬 5.1；close 三段屬 5.3）
- [ ] 2.4 換把與自動重開 job（D4）：排除剛用完的一把、`at` 的算式、沒有候選與到時挑不到的處理、unreachable 的固定間隔、成功後送 `resume_prompt`（v5 的 `cooldown until`／現在＋1 小時與其重試屬 5.4）
- [ ] 2.5 `FileSubscriptionSource`（本機開發）與 `LLMGatewaySource` 的介面與錯誤對應（403／404／409／unreachable 照 D4 的表；每次操作重新讀設定，不快取）（v5 的直連本機 port 改寫屬 5.5）
- [ ] 2.6 測試：spec 的每個 Scenario（假實作，不開真的 agent、不連網）；環境變數不出現在命令列與 log；`file` 種的 subscription 檔被原子改寫後下一次操作用新的；每項在副本改壞確認會紅

## 3. agent driver 與 agora（spike 之後依 D6 的結論）

- [ ] 3.1 `ClaudeCodeDriver`：額度只認 `status == rejected`（未知值 log 原事件、當沒用完）、`rateLimitType` 的對應、api-key 類只靠輔助訊號、`credits_required`；`resets_at` 轉成 RFC 3339 UTC
- [ ] 3.2 `OpenCodeDriver` 改寫成 opencode v2（依 1.4 的 spike 結果）：開 server／送訊息／中斷／狀態來源／額度偵測／session id 的取得全部照 v2 重寫；額度類 reason 白名單（設定 `quota_reasons`）與 `next`（毫秒→`resets_at`）沿用概念、形狀以 spike 為準（沒有就不帶）；`launch` 的 key 與 baseURL 給法在 v2 上重驗（v1 的 `OPENCODE_CONFIG_CONTENT` 形狀作廢，沿用與否由 spike 定），結果寫回 D6；（5.2 的 `launch` 環境組裝與此一起做）
- [ ] 3.3 agora：`continue --headless`、`--headless-port`、`--add-tag`（`continue`／`import`）、stderr 的 `agora-session`／`agora-port`、SIGTERM 的溫和停下，與測試（fake agent 的 headless 模式）
- [ ] 3.4 PM 在 Mac 上用真的 agent 跑一次：open（接續 agora id 與不給 id 各一次；fork／merge 入口已拿掉、不再測）→ poke → poke＋中斷 → close；換把用 `FileSubscriptionSource` 模擬；**找一次真的額度用完，把原事件（去掉秘密）貼回 D6**，確認 Claude 的 `rejected`；opencode 的 reason 形狀以 1.4 的 spike 為準
- [ ] 3.5 `new` 模式（D5，PM 確認中）：不經 agora 行程，用 `agora import --add-tag` 定期與 close 時存進 agora

## 4. 文件與收尾

- [ ] 4.1 `docs/sidecar.md`：HTTP 介面、設定（`subscription_source` 直連本機 port；沒有連線設定檔）、在 Mac 與 worker 上怎麼跑（worker 由 MyLinuxPool 的 supervisor 拉起 `sidecar run`；v5 的同步內容見 5.7、部署與組織見 design）
- [ ] 4.2 `sidecar --version` 印安裝時的 git sha（design D8）；從 branch `aicompany-p1` 可沿用的只有三段終止的做法，其餘（herdr、收信迴圈、中繼、佇列、check）不移植
- [ ] 4.3 完整 unit 綠；非作者審查（Muse Spark 1.3 xhigh）
- [ ] 4.4 開 PR（b，PMO 先跟本人說明再 merge）

## 5. v5 改動（本人 2026-10-12 中午定案；要改的程式工作）

- [ ] 5.1 `open`：`session` 改為選填的 agora session id（接續用；不給＝新開）；`id` 的共通命名是慣例（sidecar 不驗證格式；`parse_session` 只認 id 或省略、`valid_id` 的基本檢查不變）；**fork／merge 的入口拿掉（本人裁定：由 PMO／PM 先在 agora 做好 Session 再交 id；`merge_not_allowed_in_job` 與 job 的 merge 規則刪除；core 的對外路徑收斂，`SessionStore` 的 fork／merge 能力保留）**
- [ ] 5.2 `AgentDriver` 拿掉 `env_for`：`launch(session, workdir, credentials, env)` 直接拿憑證與環境變數；`ClaudeCodeDriver` 的 `launch` 內組子行程環境（含模型選擇、`no_model`、遮罩沿用只遮真秘密）；opencode 的部分併入 3.2（v2 重做）。（去掉 `env_for` 後的環境放置有審查 H1 待本人確認，見 design D3。）
- [ ] 5.3 `poke` 立刻讀：帶 `interrupt` 先打斷再送（已是現況，補測試鎖住）；close／`stop` 三段（等 `idle` 上限 `close_idle_wait`（預設 300 秒、可設定）→ SIGTERM 到 `close_timeout` → SIGKILL；shutdown 路徑跳過等閒、換把不等閒；強制＝立刻），退出結束整個 agent 程序
- [ ] 5.4 `state`／`cooldown`：拿不到恢復時間報「現在＋1 小時」；`cooldown(id, until)`（拿掉 `window`；`WINDOWS`、`invalid_window`、`_earliest_lift` 重寫；driver 的 5h／1w 解析保留做內部用）
- [ ] 5.5 `SubscriptionSource` 改 curl 本機 port（拿掉 `lgw connect` 與 gateway extra）：`LLMGatewaySource` 重寫（`base_url` 設定、逾時、Result 對應沿用）；`FileSubscriptionSource` 不變；`pyproject.toml`、`wiring.py`、`config.py`（拿掉 `gateway_connect_file`）同步
- [ ] 5.6 `/jobs`（本人 2026-10-12 定案保留、不加功能）：程式完全不動；design D1 的文字已改為定案
- [ ] 5.7 `docs/sidecar.md` 同步 v5 內容（session 參數、命名、`cooldown until`、curl 來源、close 三段與 kill timeout 公式）——與 4.1 同一份文件、一起寫；完整 unit 綠與副本改壞確認見 2.6／4.3
- [ ] 5.8 worker 映像的 opencode 升到 v2（MyLinuxPool 開單；sidecar／driver 以 v2 為準，版本一起釘）
