## 1. spike（PM，先做；本人 Q4）

- [x] 1.1 Claude Code 程式化模式：在指定 Session 上開（resume）、送第二則輸入、中斷這一輪、從事件看出 running／idle／結束、額度用完的訊號與重置時刻、`--settings` 關掉 autoContinueAtUsageLimit
- [x] 1.2 opencode server 模式：同上（開在指定 Session、送、中斷、狀態事件、額度用完的錯誤）
- [x] 1.3 agora 的接法二選一（design D6 的 a／b），結果寫回 design D6，必要時補 `session-continue` 的 delta 與 tasks 第 3 節

## 2. sidecar 本體（負責：agy，各自的 worktree）

- [x] 2.1 HTTP 控制面（只綁 127.0.0.1）：`/agents` 的四個動作、`/jobs`（at／every／until、列出、刪除）；狀態只在記憶體
- [x] 2.2 內部介面：`AgentDriver`、`SessionStore`、`SubscriptionSource`、`Selector`（`PrioritySelector`）；假實作給測試用
- [x] 2.3 open／close／status／poke 的流程（design D1、D4、D5）；結束時先 close 全部
- [x] 2.4 換把與自動重開 job（D4）
- [x] 2.5 `FileSubscriptionSource`（本機開發）與 `LLMGatewaySource`（經 `lgw connect`，和 LLMGateway PM 對齊）
- [x] 2.6 測試：spec 的每個 Scenario（假實作，不開真的 agent、不連網）；環境變數不出現在命令列與 log；每項在副本改壞確認會紅

## 3. agent driver 與 agora（spike 之後依 D6 的結論）

- [ ] 3.1 `ClaudeCodeDriver`
- [ ] 3.2 `OpenCodeDriver`
- [ ] 3.3 agora：`continue --headless`（D6 的 a）與測試（fake agent 的 headless 模式）
- [ ] 3.4 PM 在 Mac 上用真的 agent 跑一次：open（fork）→ poke → poke＋中斷 → close；換把用 `FileSubscriptionSource` 模擬

## 4. 文件與收尾

- [ ] 4.1 `docs/sidecar.md`：HTTP 介面、設定、在 Mac 與 worker 上怎麼跑（worker 由 MyLinuxPool 的 supervisor 拉起 `sidecar run`）
- [ ] 4.2 `sidecar --version` 印安裝時的 git sha（design D8）；從 branch `aicompany-p1` 可沿用的只有三段終止的做法，其餘（herdr、收信迴圈、中繼、佇列、check）不移植
- [ ] 4.3 完整 unit 綠；非作者審查（Muse Spark 1.3 xhigh）
- [ ] 4.4 開 PR（b，PMO 先跟本人說明再 merge）
