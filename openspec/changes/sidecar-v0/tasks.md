## 1. spike（PM，先做；本人 Q4）

- [x] 1.1 Claude Code 程式化模式：在指定 Session 上開（resume）、送第二則輸入、中斷這一輪、從事件看出 running／idle／結束、額度用完的訊號與重置時刻、`--settings` 關掉 autoContinueAtUsageLimit
- [x] 1.2 opencode server 模式：同上（開在指定 Session、送、中斷、狀態事件、額度用完的錯誤）
- [x] 1.3 agora 的接法二選一（design D6 的 a／b），結果寫回 design D6，必要時補 `session-continue` 的 delta 與 tasks 第 3 節

## 2. sidecar 本體（負責：agy，各自的 worktree）

- [ ] 2.1 HTTP 控制面（只綁 127.0.0.1）：`/agents` 的四個動作（含 D1 的錯誤碼、`workdir` 檢查、poke 的排隊與逾時）、`/jobs`（at／every／until、列出、刪除、`last_run`／`last_error`、動作與 `at` 的檢查）；狀態只在記憶體
- [ ] 2.2 內部介面：`AgentDriver`、`SessionStore`（沒有 `sync`）、`SubscriptionSource`（回結果或無法確認，不丟例外）、`Selector`（`PrioritySelector`，`exclude`、同 priority 以 id 排序；v0 只有 priority，替換靠改程式）；假實作給測試用
- [ ] 2.3 open／close／status／poke 的流程（design D1、D4、D5）；SIGTERM 時先 close 全部再結束
- [ ] 2.4 換把與自動重開 job（D4）：排除剛用完的一把、`cooldown` 只送 `until`（拿不到恢復時間報現在＋1 小時；`until` 被拒時改報現在＋1 小時重送）、`at` 的算式、沒有候選與到時挑不到的處理、unreachable 的固定間隔、成功後送 `resume_prompt`
- [ ] 2.5 `FileSubscriptionSource`（本機開發）與 `LLMGatewaySource`（v5：不用 `lgw` extra，直連本機 port 的 HTTP；`cooldown(id, until)`；每次操作重新讀設定，不快取；403／404／409／unreachable 對應 D4 的表；見 design D7）
- [ ] 2.6 測試：spec 的每個 Scenario（假實作，不開真的 agent、不連網）；環境變數不出現在命令列與 log；連線設定檔被原子改寫後下一次操作用新的；每項在副本改壞確認會紅

## 3. agent driver 與 agora（spike 之後依 D6 的結論）

- [ ] 3.1 `ClaudeCodeDriver`：額度只認 `status == rejected`（未知值 log 原事件、當沒用完）、`rateLimitType` 的對應、api-key 類只靠輔助訊號、`credits_required`；`resets_at` 轉成 RFC 3339 UTC
- [ ] 3.2 `OpenCodeDriver`：額度類 reason 白名單（設定 `quota_reasons`）、`next`（毫秒）轉成 `resets_at`（沒有就不帶；v5 不再分窗）；**在 opencode 1.18.35 上驗證 key 與 baseURL 的給法**（例如 `OPENCODE_CONFIG_CONTENT`），結果寫回 D6
- [ ] 3.3 agora：`continue --headless`、`--headless-port`、`--add-tag`（`continue`／`import`）、stderr 的 `agora-session`／`agora-port`、SIGTERM 的溫和停下，與測試（fake agent 的 headless 模式）
- [ ] 3.4 PM 在 Mac 上用真的 agent 跑一次：open（fork）→ poke → poke＋中斷 → close；換把用 `FileSubscriptionSource` 模擬；**找一次真的額度用完，把原事件（去掉秘密）貼回 D6**，確認 `rejected` 與 opencode 的 reason
- [ ] 3.5 `new` 模式（D5，PM 確認中）：不經 agora 行程，用 `agora import --add-tag` 定期與 close 時存進 agora

## 4. 文件與收尾

- [ ] 4.1 `docs/sidecar.md`：HTTP 介面、設定、連線設定檔（欄位、原子改寫、worker 上由 MyLinuxPool 的單元從 `gateway.json` 產生、known_hosts 放 state 目錄 0600）、在 Mac 與 worker 上怎麼跑（worker 由 MyLinuxPool 的 supervisor 拉起 `sidecar run`）
- [ ] 4.2 `sidecar --version` 印安裝時的 git sha（design D8）；從 branch `aicompany-p1` 可沿用的只有三段終止的做法，其餘（herdr、收信迴圈、中繼、佇列、check）不移植
- [ ] 4.3 完整 unit 綠；非作者審查（Muse Spark 1.3 xhigh）
- [ ] 4.4 開 PR（b，PMO 先跟本人說明再 merge）

## 5. v5 改動（本人 2026-10-12 中午定案；要改的程式工作）

- [ ] 5.1 `open`：`session` 改為選填的 agora session id（接續用；不給＝新開）；`id` 用共通命名（`parse_session`、`valid_id`、fork／merge／new 入口去留待本人確認，見 `docs/review/v5-impact.md`）
- [ ] 5.2 `AgentDriver` 拿掉 `env_for`：`launch(session, workdir, credentials, env)` 直接拿憑證與環境變數；兩 driver 的 `launch` 內組子行程環境（含模型選擇、`no_model`、遮罩沿用只遮真秘密）
- [ ] 5.3 `poke` 立刻讀：帶 `interrupt` 先打斷再送（已是現況，補測試鎖住）；`stop` 溫和＝閒了才退、強制＝立刻，退出結束整個 agent 程序
- [ ] 5.4 `state`／`cooldown`：拿不到恢復時間報「現在＋1 小時」；`cooldown(id, until)`（拿掉 `window`；`WINDOWS`、`invalid_window`、`_earliest_lift` 重寫；driver 的 5h／1w 解析保留做內部用）
- [ ] 5.5 `SubscriptionSource` 改 curl 本機 port（拿掉 `lgw connect` 與 gateway extra）：`LLMGatewaySource` 重寫（`base_url` 設定、逾時、Result 對應沿用）；`FileSubscriptionSource` 不變；`pyproject.toml`、`wiring.py`、`config.py`（拿掉 `gateway_connect_file`）同步
- [ ] 5.6 `/jobs` 維持現狀（待本人決定去留；本期不動程式，只在 design 標註）
- [ ] 5.7 `docs/sidecar.md` 同步 v5（session 參數、命名、`cooldown until`、curl 來源、kill timeout 公式不變）；完整 unit 綠、每項副本改壞確認會紅
