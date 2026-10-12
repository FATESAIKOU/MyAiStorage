## ADDED Requirements

### Requirement: opencode 的匯出格式（v1／v2）
adapter MUST 自己認出 opencode 的匯出格式：v2 是扁平（`messages[]` 的每一列有 `type`），v1 是巢狀（`messages[]` 的每一列有 `info`）；使用者 MUST NOT 需要指定版本，也 MUST NOT 用 `opencode --version` 猜格式（存下來的 raw 可能是別版產生的）。看不懂的形狀 MUST 是明確的錯誤（exit 非 0），MUST NOT 變成空的閱讀版。

- 閱讀版（`turns()`）對兩版都 MUST 成立：user 的文字、assistant 的內容（文字與工具呼叫各照閱讀版的規則）；`idle` 這種不是對話的列 MUST NOT 產生段落；工具呼叫 MUST 是一行 `[tool] …`。
- v2 的必填欄位與 id／參照的結構位置 MUST 以真實 v2 session 的匯出為準（不是用猜的）。

#### Scenario: v2 的 Session 匯入後閱讀版完整
- **WHEN** 用 `agora import session --external-session-id <id> --agent opencode` 匯入一個 opencode 2.x 的多輪 Session（含工具呼叫）
- **THEN** Session 的 body 有對應的 user／assistant 段落，工具呼叫有一行 `[tool] …`：不是空的

#### Scenario: 看不懂的匯出格式
- **WHEN** 匯出檔的訊息既沒有 `type` 也沒有 `info`
- **THEN** 匯入失敗並說明格式認不出來；沒有存出任何 Session

### Requirement: opencode v2 的接續與 fork
本機的 opencode 是 2.x 時，`agora continue session <id> --agent opencode`（含 `--headless`）、`--fork`、以及 merge 出來的 Session 的接續 MUST 照常運作：接續時載入的是 agent 自己的格式（工具呼叫等不經轉換）、載入後 MUST 回讀驗證（訊息數與 part 數）、原本的 agent session MUST NOT 被改動。

- 同 agent 的 fork MUST 是原 Session 的 raw 加上新訊息（原樣載入）。
- 送給 opencode 的每一個自動化呼叫（匯出、匯入、刪除、寫要約）MUST 帶明確的 server 上下文（`--standalone` 或 `--server`），MUST NOT 讓 opencode 去找或啟動使用者的 managed 背景服務。

#### Scenario: 接續 v2 的 Session
- **WHEN** 對一個 opencode 2.x 匯入的 Session 執行 `agora continue session <id> --agent opencode`，在 agent 裡說了一句話後離開
- **THEN** agent 看得到完整前文（含工具呼叫）；結束後寫回同一個 Session；來源的 agent session 不變；閱讀版完整

#### Scenario: fork v2 的 Session
- **WHEN** `agora continue session <id> --agent opencode --fork`
- **THEN** 新 Session 有原樣的前文加上 fork 的兩則；原 Session 不變

#### Scenario: 接續 merge 出來的 Session
- **WHEN** `agora merge session <A> <B>` 之後，接續 merge 印出的那個 Session
- **THEN** 用 v2 的 payload 載入（不是被 v2 拒收的 v1 巢狀）；agent 看得到要約與來源清單；寫回的是那個 Session

#### Scenario: 不叫 managed 服務
- **WHEN** 匯入、接續、寫要約或刪除時 agora 呼叫 opencode
- **THEN** 呼叫帶明確的 server 上下文（`--standalone` 或 `--server`）：不會轉去尋找或啟動 managed 背景服務（例如預設埠 49374 的共用服務）

### Requirement: 舊版 opencode raw 的同 agent 接續
Store 裡在 opencode 2.x 之前匯入的 Session，raw 是 v1 巢狀格式。用同一個 agent 接續或 fork 這種 Session 時，agora MUST 在開 agent 之前以明確的訊息拒絕（exit 非 0），訊息 MUST 說明重新匯入的方法（`agora import session --external-session-id <agent session id> --agent opencode`，來源還在時原地更新）；MUST NOT 把 v1 的 payload 送給 v2 的 `session import`（那會留下半個 Session）。

跨 agent 接續舊 raw MUST 照常（閱讀版轉換後重建，本來就重寫）；被拒絕時原本的 Session 與它的 raw MUST NOT 被改動。

#### Scenario: 舊格式的 raw 用同一個 agent 接續
- **WHEN** 對一個 opencode 1.x 時代匯入的 Session（store 裡的 raw 是 v1 巢狀）執行 `agora continue session <id> --agent opencode`
- **THEN** 不開 agent，exit 非 0，訊息說明要重新匯入那個 Session；原 Session 與 raw 都不變

#### Scenario: 舊格式的 raw 跨 agent 接續
- **WHEN** 同上，但 `--agent claude`
- **THEN** 用閱讀版轉換照常接續；不因來源是 v1 格式而拒絕
