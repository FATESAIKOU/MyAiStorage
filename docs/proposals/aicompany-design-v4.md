# AiCompany 期 1 設計提案 v4：Angareion（#30）、sidecar（#31）與 LLMGateway 的接點

> 狀態：**提案**（2026-10-11 晚，MyAiStorage PM）。依本人對 v3 的回饋修改，**還沒寫程式**。照三種圖條列（實體圖、控制時序圖、介面圖），由 PMO 畫圖。
> v3 → v4 的差別在第 0 節；沒提到的部分和 v3 相同（`docs/proposals/aicompany-design-v3.md`）。

## 0. 本人對 v3 的回饋與 v4 的改動

1. **給 sidecar 的指示只有四件事，不能混在一起**：開啟、關閉、搜集狀態（只限它開的）、戳。「戳＋中斷」就等於暫停，所以**拿掉 pause**。開啟時的第一則輸入是**選填**。
2. **額度用完、全部冷卻時要自動重開**：等同掛一個 job，在最早解除冷卻的時刻用 `continue` 重開。
3. **不管重啟**：sidecar 的狀態全部是揮發的（只在記憶體），重啟就沒了；拿掉「重啟後找回」的設計。
4. （Angareion 的時序圖由 PMO 合併參與者，設計不變。）
5. **LLMGateway 不相依 MyLinuxPool**：LLMGateway 提供自己的「連線用程式」。sidecar 的 `SubscriptionSource` 實作改成用那個連線程式，不再假設池的隧道。細節等 LLMGateway 的 v4。

不變：Angareion 與 sidecar 互不相依；相依只有 sidecar → agora、sidecar → LLMGateway；共通 JSON 和 LLMGateway PM 一致（v3 第 2.4 節）。

---

## 1. 實體圖

### 1.1 實體

| 實體 | 是什麼 |
|---|---|
| 控制者 | PMO、人、排程、其他程式 |
| sidecar | 控制 agent 的 daemon，對外只有 HTTP；每台 worker 一個，Mac 也能跑；狀態只在記憶體 |
| agent | Claude Code、opencode 的行程，由 sidecar 開成子行程 |
| agora | Session 的保存處（Drive 上），`agora` 指令 |
| LLMGateway | 保管 subscription（key、endpoint）與冷卻；附一個連線用程式 |
| Angareion | 留言、讀留言的程式；agent 另有 skill 說明 |
| 信道 | Angareion 的後端，這次是 GitHub Issues |

### 1.2 相依（箭頭＝「用到」）

- sidecar → agora：開啟時準備繼承的 Session（並打上 `sidecar:<id>` 的 tag），關閉時寫回。
- sidecar → LLMGateway（經它的連線程式）：`get` 拿 key；額度用完時 `cooldown`、`list` 挑下一把。
- sidecar → agent：開、送輸入、中斷、停、讀狀態（子行程，不經 herdr）。
- 控制者 → sidecar：HTTP 的四個動作；立即、定時、輪詢。
- agent → agora：`agora continue` 的定期寫回。
- agent、控制者、人、程式 → Angareion → 信道。
- **沒有**：sidecar ↔ Angareion；LLMGateway → MyLinuxPool；任何東西 → sidecar 的本機狀態檔（沒有這種檔）。

### 1.3 誰控制誰

| 誰 | 控制誰 | 怎麼控制 | 不做什麼 |
|---|---|---|---|
| 控制者 | sidecar | HTTP 的四個動作 | 不直接碰 agent |
| sidecar | 它開的 agent | 子行程 | 不讀寫 Angareion |
| sidecar | agora 上的 Session | 準備、打 tag、寫回 | 不拼原始檔 |
| sidecar | LLMGateway 的冷卻紀錄 | 額度用完時登錄 | 不保存 key（只在記憶體，交給子行程環境） |
| 各實體 | 自己的 Angareion 收發 | `angareion` 指令 | — |

---

## 2. 介面圖（必要而且只有這些）

### 2.1 Angareion（和 v3 相同）

`create_channel`、`send`、`wait`（不阻塞或阻塞）、`read`；`ChannelBackend` 介面，實作是 GitHub API＋Issues。設計裡不提 sidecar。

### 2.2 sidecar（對外：四個動作，經 HTTP）

| 介面 | 參數 | 說明 |
|---|---|---|
| `open` | **id**（呼叫方給）、agent 種類、工作目錄、繼承的 Session（`new`／`continue <agora id>`／`fork <agora id>`／`merge <agora id…>`）、subscription（`{"id": …}`）、第一則輸入（**選填**） | 開 agent |
| `close` | id | 寫回 agora，結束 agent |
| `status` | id（可省＝全部） | 只列它開的：id、agent 種類、Session、subscription id、狀態（running／idle／waiting-quota（附預定重開的時刻）／exited）、開始時間；不含 key |
| `poke` | id、文字、中斷（選填，預設否） | 送一則輸入；`中斷`＝先打斷這一輪再送。要「暫停」就是戳一句「停下來等指示」並中斷 |

外部控制的三種形式（同一組 HTTP，動作仍只有四個）：

- 立即：`POST /agents`（open）、`POST /agents/{id}/close`、`GET /agents[/{id}]`（status）、`POST /agents/{id}/poke`。
- 定時：`POST /jobs {action, args, at}`。
- 輪詢：`POST /jobs {action, args, every, until?}`；另有 `GET /jobs`、`DELETE /jobs/{id}`。

狀態全部在記憶體：agent 清單、job 清單都不落檔；sidecar 重啟就從空的開始（子行程也隨之結束）。記憶已經由 `agora continue` 定期寫回在 agora 上，要接回就由控制者再 `open`（`continue` 那個 Session）。

### 2.3 sidecar 內部（介面和實作分開）

| 內部介面 | 動作 | 這次的實作 |
|---|---|---|
| `AgentDriver` | `env_for(subscription)`、`launch(env, session)`、`send(text)`、`interrupt()`、`state()`（running／idle／exited／額度用完＋窗＋重置時刻）、`stop(溫和／強制)` | `ClaudeCodeDriver`、`OpenCodeDriver` |
| `SessionStore` | `prepare(模式, ids, tag) → agent 用的 session`、`sync(session)` | `AgoraSessionStore` |
| `SubscriptionSource` | `list()`、`get(id, agent)`、`cooldown(id, 窗, 重置時刻)` | `LLMGatewaySource`（經 LLMGateway 的連線程式；細節等它的 v4）；本機開發可用讀 JSON 檔的實作 |

- v3 的 `SessionStore.find`（重啟後靠 tag 找回）拿掉。
- agora 的 tag `sidecar:<id>` **建議保留**：不增加任何 sidecar 介面，只是 `prepare` 的一個參數；用途是人或程式在 agora 上查得到「哪個 Session 是哪個 id 開的」（`agora search --filter`）。不需要就連這個參數一起拿掉（見第 4 節 Q7）。

### 2.4 LLMGateway（sidecar 用到的三個；和 v3 相同）

`list`（不含 key）、`get(id, agent)`（含 key；`agent` 不在該筆 `agents` 裡就拒絕）、`cooldown(id, 窗, 重置時刻)`。共通 JSON 見 v3 第 2.4 節（和 LLMGateway PM 一致）。

---

## 3. 控制時序圖

### 3.1 派一件工作（和 v3 相同，只差第一則輸入選填）

1. 控制者 → Angareion：`send`（工作內容）。
2. 控制者 → sidecar：`open`（id、agent 種類、fork 自 home Session、subscription `{"id"}`、第一則輸入「用 angareion 讀 channel X」）。
3. sidecar → LLMGateway：`get(id, agent)`。
4. sidecar → agora：`prepare`（fork，打 tag）。
5. sidecar → AgentDriver：`env_for` → `launch`；有第一則輸入就送。
6. agent → Angareion：`read` → 工作 → `send` → 閒著時 `wait`。
7. agent → agora：定期寫回。

### 3.2 緊急（和 v3 相同）

發信方 → Angareion `send`（urgency 9）→ 發信方 → sidecar `poke`（id、「立刻用 angareion 讀 channel X」、中斷）→ sidecar 打斷這一輪再送 → agent `read`。

### 3.3 「暫停」＝戳＋中斷

控制者 → sidecar `poke`（id、「停下手上的事，等下一則指示」、中斷）→ agent 停在提示處。要繼續就再 `poke` 一句新的指示。

### 3.4 額度用完、換一把（sidecar 內部，不是對外介面）

1. AgentDriver → sidecar：額度用完（窗、重置時刻）。
2. sidecar → LLMGateway：`cooldown(這一把, 窗, 重置時刻)`。
3. sidecar → LLMGateway：`list` → 挑 `available` 且 `agents` 含這個種類的，照 `priority` 取第一個。
4. 有的話：`get(那一把, agent)` → `stop`（溫和）→ agora 寫回 → `prepare`（continue 同一個 Session）→ `launch`。id 不變。
5. **全部冷卻**：`stop` → 寫回；狀態 `waiting-quota`，同時**自動掛一個 job**：`at ＝ 這個種類能用的 subscription 最早解除冷卻的時刻`，動作是 `open`（同一個 id、`continue` 那個 Session、subscription 由 sidecar 屆時重新挑）。這個 job 和外部掛的 job 是同一種東西，`GET /jobs` 看得到、可以刪。

### 3.5 定時與輪詢（和 v3 相同）

### 3.6 重啟

不處理。sidecar 重啟就從空的開始；需要接回的工作由控制者再 `open`（`continue`）。

---

## 4. 要本人決定的事

v3 留下的 Q1（sidecar 依 id 自己 `get`）、Q4（agent 用程式化模式開）、Q5（HTTP 只綁本機＋控制 token；**本人的回答是不做認證，見下一節**）、Q6（agent 讀得到信道 token）照舊，由 PMO 一起問。v4 新增：

- **Q7 agora 的 tag `sidecar:<id>` 留不留**：建議留（不增加介面；人或程式能在 agora 上查到 Session 是哪個 id 開的）。不需要就拿掉。
- **Q8 自動重開時挑哪一把**：建議屆時重新 `list`、照 `priority` 挑（冷卻期間別的 subscription 可能恢復得更早）。

---

## 5. 本人的回答（10-11）

| 題 | 本人的回答 |
|---|---|
| Q1 誰去拿 key | sidecar 依 subscription 的 id 自己 `get`；key 不經過控制者 |
| Q4 agent 的開法 | 程式化模式（已 spike：Claude Code stream-json、opencode server） |
| Q5 誰可以呼叫 sidecar | HTTP 只綁本機，**不做認證** |
| Q6 agent 用信道的憑證 | Angareion 只定 token 檔的位置與讀法；token 由 MyLinuxPool 的能力分發 |
| Q7 agora 的 tag | 留 `sidecar:<id>` |
| Q8 挑哪一把 | `Selector` 可替換，預設照 `priority` |

2026-10-12 的實作文件（`openspec/changes/sidecar-v0/`）已照這些回答與交叉核對（`docs/review/v4-consistency.md`）修訂；和上面各節不同之處以 change 為準。
