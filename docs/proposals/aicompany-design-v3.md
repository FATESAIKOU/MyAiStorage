# AiCompany 期 1 設計提案 v3：Angareion（#30）、sidecar（#31）與 LLMGateway 的接點

> 狀態：**提案**（2026-10-11 晚，MyAiStorage PM）。本人看完 v2 判 NG，依本人的回饋重寫。**還沒寫程式**，本人同意後才改 OpenSpec change 與派工。
> 照三種圖條列（實體圖、控制時序圖、介面圖），由 PMO 畫圖。

## 0. 本人的回饋（v2 → v3）

1. Angareion 與 sidecar **互不相依**：
   - Angareion：讓 agent、程式或人「上 Slack 留言、讀留言」的程式。
   - sidecar：透過 HTTP 接受指令、控制 agent 的程式。
   - 有緊急訊息時，由發信方（PMO、人或其他）用 sidecar 的「戳」，內容寫「用 Angareion 讀訊息」。讀寫 Angareion 完全交給各自的實體，和 sidecar 無關。
2. sidecar 的定位：登錄（開啟）時就用 id，並善用 agora 標頭的自訂 tags。
3. 相依只有 **sidecar → agora**、**sidecar → LLMGateway**；Angareion 獨立。
4. LLMGateway 只要三個介面：列出 subscription 一覽（含冷卻：現在能不能用）、取得某個 subscription 的詳細資料（共通 JSON：key、endpoint 等）、登錄冷卻（5 小時／1 週）。**sidecar 開 agent 直接吃這個共通 JSON。**
5. sidecar 怎麼連 LLMGateway：PMO 調研中，本提案不定。

---

## 1. 實體圖

### 1.1 實體

| 實體 | 是什麼 | 在哪裡 |
|---|---|---|
| 控制者 | PMO、人、排程、其他程式 | 任何地方 |
| sidecar | 控制 agent 的 daemon，對外只有 HTTP | 每台 worker 一個；Mac 也能跑 |
| agent | Claude Code、opencode 的行程 | 由 sidecar 開成子行程 |
| agora | Session 的保存處（Drive 上） | `agora` 指令 |
| LLMGateway | 保管 subscription（key、endpoint）與冷卻 | 常駐服務 |
| Angareion | 留言、讀留言的程式（指令＋函式庫）；agent 另有 skill 說明 | 每個要用的實體各自裝 |
| 信道 | Angareion 的後端，這次是 GitHub Issues | GitHub 的 private repo |

### 1.2 相依（箭頭＝「用到」）

- sidecar → agora：開 agent 時準備繼承的 Session、打標記；關閉與暫停時寫回。
- sidecar → LLMGateway：拿 subscription 的詳細資料；額度用完時登錄冷卻、看一覽挑下一個。
- sidecar → agent：開、送輸入、中斷、停、讀狀態（子行程，不經 herdr）。
- 控制者 → sidecar：HTTP 的五個動作；立即、定時、輪詢。
- agent → agora：`agora continue` 的定期寫回（記憶同步）。
- agent、控制者、人、程式 → Angareion → 信道：各自留言、讀留言。
- **沒有**：sidecar ↔ Angareion、Angareion → 任何其他元件、LLMGateway → sidecar（LLMGateway 是被呼叫的那一方）。

### 1.3 誰控制誰

| 誰 | 控制誰 | 怎麼控制 | 不做什麼 |
|---|---|---|---|
| 控制者 | sidecar | HTTP 的五個動作 | 不直接碰 agent 行程 |
| sidecar | 它開的 agent | 子行程 | 不讀寫 Angareion、不解讀 agent 的工作內容 |
| sidecar | agora 上的 Session | 準備、打 tag、寫回 | 不自己拼 Session 原始檔 |
| sidecar | LLMGateway 的冷卻紀錄 | 額度用完時登錄 | 不保存 key（只在記憶體，交給子行程環境） |
| 各實體 | 自己的 Angareion 收發 | `angareion` 指令 | — |

---

## 2. 介面圖（必要而且只有這些）

### 2.1 Angareion（獨立）

| 介面 | 參數 | 回傳 |
|---|---|---|
| `create_channel` | 名稱、成員（位址清單） | channel |
| `send` | channel、訊息（`from`／`to`／`urgency`／`content`／`attachments`） | 訊息 id |
| `wait` | 目標（某個 channel 或「寄給我的」）、游標、逾時（0＝不阻塞；N 秒；不設＝一直等） | 有沒有新訊息 |
| `read` | 目標、游標 | 訊息、新的游標 |

- 介面和實作分開：`ChannelBackend`（同樣四個動作）；這次的實作是 GitHub API＋Issues。
- 指令：`angareion channel create`、`angareion send`、`angareion wait`、`angareion read`。
- 沒有 ack、沒有限流；「讀到哪」是讀的人自己的游標。
- 設計裡不提 sidecar。

### 2.2 sidecar（對外：五個動作，經 HTTP）

| 介面 | 參數 | 說明 |
|---|---|---|
| `open` | **id**（呼叫方給）、agent 種類、工作目錄、繼承的 Session（`new`／`continue <agora id>`／`fork <agora id>`／`merge <agora id…>`）、**subscription**（LLMGateway 的共通 JSON，見 2.4）、第一則輸入（可省） | 開 agent；Session 打上 tag `sidecar:<id>` |
| `pause` | id | 暫停（語意見第 4 節 Q3） |
| `close` | id | 寫回 agora，結束 agent |
| `status` | id（可省＝全部） | 只列它開的：id、agent 種類、Session、subscription 名、狀態（running／idle／paused／waiting-quota／exited）、開始時間；不含 key |
| `poke` | id、文字、是否中斷 | 送一則輸入；`中斷`＝先打斷這一輪再送 |

外部控制的三種形式：

- 立即：直接呼叫上面五個（`POST /agents`、`POST /agents/{id}/pause|close|poke`、`GET /agents[/{id}]`）。
- 定時：`POST /jobs {action, args, at}`。
- 輪詢：`POST /jobs {action, args, every, until?}`；另有 `GET /jobs`、`DELETE /jobs/{id}`。job 只是「何時執行五個動作中的哪一個」。

定位：id 由呼叫方在 `open` 時給，之後所有動作都用它。sidecar 在 agora 的 Session 標頭 `tags` 加上 `sidecar:<id>`；sidecar 或機器重啟後，靠這個 tag 在 agora 上找回「這個 id 對應哪個 Session」。

### 2.3 sidecar 內部（介面和實作分開）

| 內部介面 | 動作 | 這次的實作 |
|---|---|---|
| `AgentDriver` | `env_for(subscription) → env／unset`、`launch(env, session)`、`send(text)`、`interrupt()`、`state() → running／idle／exited／額度用完（含重置時刻、是哪個窗：5h／1w）`、`stop(溫和／強制)` | `ClaudeCodeDriver`、`OpenCodeDriver` |
| `SessionStore` | `prepare(模式, ids, tag) → agent 用的 session`、`sync(session)`、`find(tag)` | `AgoraSessionStore`（`agora` 指令） |
| `SubscriptionSource` | `list() → 一覽（不含 key）`、`get(name) → 共通 JSON`、`cooldown(name, 窗, 到何時)` | `LLMGatewaySource`（連法等 PMO 調研）；本機開發可用「讀一個 JSON 檔」的實作 |

- 「這個 subscription 要變成 agent 的哪些環境變數」是 `AgentDriver` 的知識（例如 Claude Code 用訂閱 token 或 API key、opencode 用它的 provider 設定）。LLMGateway 不必知道 agent 種類。
- 「怎麼看出額度用完」也是 `AgentDriver` 的知識。
- 環境變數用子行程的環境給，不上命令列，不寫進 log、registry 或檔案。

### 2.4 LLMGateway（sidecar 用到的三個）

| 介面 | 回傳 |
|---|---|
| `list` | 每個 subscription 的共通 JSON，**不含 `key`**：含 `agents`、`priority`、**`available`（現在能不能用）**、各窗（5h、1w）冷卻到何時 |
| `get(id, agent)` | 共通 JSON（見下）；含 key。`agent` 不在該筆的 `agents` 裡就拒絕 |
| `cooldown(id, window: 5h／1w, resets_at)` | 登錄這一把在這個窗冷卻到何時（過了自動解除；過去或超過 8 天的時間拒收） |

共通 JSON（**和 LLMGateway PM 一起定**；以它 10-11 晚的草案為底，拿掉 `launch`）：

```
{
  "schema": 1,
  "id": "claude-personal",
  "provider": "anthropic | openrouter | ollama-cloud | opencode-zen",
  "kind": "claude-subscription | api-key",
  "agents": ["claude-code"],
  "priority": 10,
  "models": {"allow": ["*"], "default": null},
  "endpoint": null,
  "key": "<只在 get 回>",
  "cooldown": {"5h": {"until": "…"} | null, "1w": {"until": "…"} | null},
  "available": false
}
```

- `list` 回同樣的形狀但沒有 `key`，誰都能看；`get` 才有 `key`。
- `agents`：這把可以給哪些 agent 種類（條款限制，例如 Claude 訂閱只給 claude-code；`get` 也只在請求的 agent 種類符合時才回 key）。
- `priority`：預設的挑選順序（小的先）。`available`＝兩個窗都沒在冷卻、而且 key 有部署。
- **和 LLMGateway PM 一致（10-11 晚）**：schema 沒有 `launch`。「這把 key 要變成 agent 的哪些環境變數」放在 sidecar 的 `AgentDriver.env_for`，因為環境變數的名字跟著 agent 的版本變，而 agent 的版本和 sidecar 一起釘在 worker 映像裡。LLMGateway 只回共通欄位。
- 條款守門：`get` 必須帶 `agent=claude-code|opencode`，不在該筆的 `agents` 裡就回 403 `agent_not_allowed`（不是新介面，只是 `get` 的必填參數）；sidecar 呼叫時帶自己要開的種類。
- sidecar 的 `open` 收這個物件；只給 `{"id": …}` 時，sidecar 自己向 LLMGateway `get` 補齊（見第 4 節 Q1）。

---

## 3. 控制時序圖

### 3.1 派一件工作給某個 team

1. 控制者 → Angareion：`send`（工作內容，寫在 channel X）。
2. 控制者 → sidecar：`open`（id=`team-a-0042`、claude-code、fork 自 team 的 home Session、subscription=`{"id": "claude-personal"}`、第一則輸入「用 angareion 讀 channel X」）。
3. sidecar → LLMGateway：`get(claude-personal, agent=claude-code)` → 共通 JSON。
4. sidecar → agora：fork home Session，打 tag `sidecar:team-a-0042` → agent 用的 session。
5. sidecar → AgentDriver：`env_for` → `launch`（子行程，env 在環境裡）。
6. sidecar → agent：送第一則輸入。
7. agent → Angareion：`read` → 工作 → `send`（回報）→ 閒著時 `wait`（阻塞地等，不耗模型額度）。
8. agent → agora：定期寫回。

### 3.2 緊急

1. 發信方 → Angareion：`send`（urgency 9）。
2. 發信方 → sidecar：`poke`（id、「立刻用 angareion 讀 channel X」、中斷）。
3. sidecar → agent：打斷這一輪，再送那句話。
4. agent → Angareion：`read`。

### 3.3 額度用完、換一把（全部在 sidecar 內部，不是新的對外介面）

1. AgentDriver → sidecar：`state` ＝ 額度用完（窗：5h 或 1w、重置時刻）。
2. sidecar → LLMGateway：`cooldown(這一把, 窗, 重置時刻)`。
3. sidecar → LLMGateway：`list` → 挑 `available` 而且 `agents` 包含這個 agent 種類的，照 `priority` 取第一個（第 4 節 Q2）。
4. 有的話：sidecar → `get(那一把, agent=這個種類)` → AgentDriver `stop`（溫和）→ agora 寫回 → agora `continue` 同一個 Session → `launch`（新的 env）。id 不變、tag 不變。
5. 沒有的話：`stop` → 寫回；狀態記成 `waiting-quota`，附最早解除冷卻的時刻。要不要到時間自動重開，見第 4 節 Q2。

### 3.4 定時與輪詢

- 定時：`POST /jobs {action: open, args: …, at: "2026-10-12T09:00+09:00"}`。
- 輪詢：`POST /jobs {action: poke, args: {id, text}, every: 1800}`，用在「定期做某件事」；收信不需要輪詢，agent 自己 `angareion wait`。

### 3.5 sidecar 或機器重啟

1. sidecar 起來，子行程已經沒了。
2. `status` 用 agora 上的 tag `sidecar:<id>` 列出它開過的 id 與對應的 Session（以 agora 為準）。
3. 要不要接回由控制者決定：`open`（同一個 id、`continue` 那個 Session）。

---

## 4. 要本人決定的事（附建議）

- **Q1 誰去拿 key**：(a) 控制者只給 subscription 的 id，sidecar 自己向 LLMGateway `get`（**建議**：key 不經過控制者、不出現在 HTTP 請求裡）；(b) 控制者拿完整的共通 JSON 傳給 `open`。兩種都接受同一個 schema，差別只在 key 經過誰。
- **Q2 換把時誰挑下一把、怎麼挑**：**建議** sidecar 挑：在 `list` 裡找 `available` 且 `agents` 含這個 agent 種類的，照 `priority` 取第一個（之後可加 harness 的「優先模型＋底線」）。全部冷卻時：只停下並在 `status` 標 `waiting-quota`，**或**自動排一個「到最早解除冷卻的時刻」的 job 重開（請選）。
- **Q3「暫停」的意思**（v2 Q2）：建議「寫回 agora 後停掉行程，之後用 `open`（同 id、continue）接回」。
- **Q4 agent 的開法**（v2 Q3）：建議用程式化模式（Claude Code stream-json、opencode server），不開 TUI 讀畫面；要先 spike。
- **Q5 誰可以呼叫 sidecar 的 HTTP**（v2 Q4）：建議只綁本機，用控制 token 認證，不給 agent。遠端控制者怎麼連，和「sidecar 怎麼連 LLMGateway」一起看 PMO 的調研。
- **Q6 agent 用 Angareion 的憑證**（v2 Q1）：agent 自己留言、讀留言，所以它讀得到信道 token（範圍只有信道 repo 的 Issues）。建議接受。這和 sidecar 無關，是 Angareion 的部署問題。
