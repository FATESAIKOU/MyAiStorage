# Agent Sidecar

Agent Sidecar 是執行在 worker 機器上的 AI agent 監督程序（Supervisor daemon），以 systemd user unit 形式常駐執行。它負責為指定的團隊身分輪詢 Angareion 訊息、依緊急度投遞至執行中的 agent、透過 herdr 管理 agent 生命週期，並集中保管通訊憑證與提供安全回報中繼。

---

## 1. 簡介與範圍

### 定位與工作流程
在期 1 的一人公司工作流程中：
1. **本人與 PMO**：本人向 PMO 提出工單與決策。
2. **PMO 發信**：PMO 透過 Angareion 信道（A2A）發送任務訊息至 worker 上的 AI 團隊。
3. **Sidecar 接手**：Sidecar 作為 worker 端的常駐接點，拉取訊息、排入佇列、依緊急度投遞或啟動對應 team。
4. **Team 執行與回報**：Team 在獨立 herdr pane 內透過 `agora continue` 執行任務，完成後經由 Sidecar 的本地 UNIX socket（`sidecar say`）安全回報。
5. **結案**：PMO 收到回報並轉告本人，由本人進行審查或合併。

### 本人 10-11 裁定：範圍 B（通訊＋心跳＋啟動與關閉）
依據 2026-10-11 本人裁定（MyPMO#47，見 `openspec/changes/agent-sidecar-design/design.md`），Sidecar 採用 **範圍 B** 架構：

#### 做什麼（v0 範圍）
- **Angareion 訊息處理**：使用 `angareion` 程式介面直接輪詢信道、條件式 ETag/304 查詢、遵循次級速率限制（Secondary Rate Limits）與訊息去重。
- **持久佇列**：收到的訊息在投遞前先原子落檔至本地優先級佇列。
- **依緊急度投遞**：
  - 高緊急度（`urgency >= 8`）：發送 Esc 中斷當前回合並立即送入輸入。
  - 一般/低緊急度（`urgency < 8`）：進入持久佇列，排隊於 agent 當前或下一次 hand-off 送入。
- **投遞後 Ack**：僅在訊息確實進入 agent 輸入後，才在信道上發送 Ack 確認留言。
- **啟動與終止 Team**：透過 herdr 管理終端視窗，執行 `agora continue session <home> --agent <agent> --fork --sync-period <N>`；終止採三段式降級升級（SIGINT -> SIGTERM -> SIGKILL）。
- **狀態登錄與重啟恢復**：維護本地 `registry.json`，重啟後對照 herdr agent list 找回運行中的 agent。
- **Atelier Harness 載入**：在 agent 啟動並就緒後，將 harness 檔案全文無損作為第一則輸入送入。
- **安全回報中繼**：透過本地 UNIX domain socket 與 `SO_PEERCRED` 行程樹驗證，提供 `sidecar say` 指令供 agent 安全發信。
- **模型租約介面**：透過 `ModelBroker` 介面申請與釋放模型租約（v0 使用假 broker）。

#### 不做什麼（Non-Goals / D9）
- **外來 Agent 納管**：v0 僅納管由 Sidecar 自行啟動的 agent；手動開啟或外部既有的 agent 介面留白，不納入管理。
- **暫停與恢復**：v0 不實作 process group 的 SIGSTOP/SIGCONT，避免 TUI 顯示與工具調用狀態混亂。
- **範圍 C 的深度記憶管理**：不介入 agora Session 的挑選、壓縮與歸檔，由 agora `--fork` 與寫回機制負責。
- **跨機器 Presence 心跳**：v0 不發送 A2A 訊息作為心跳以節省 API 額度，心跳僅限本地 registry 的 `updated_at` 與 `sidecar status`。
- **多 Worker 分布式調度**：不處理跨機器任務分配。
- **模型代理本身**：模型請求轉發由 LLMGateway 處理，Sidecar 不做 proxy。
- **群組定址**：群組展開為 Angareion 期 2 規格，v0 僅支援角色位址。

---

## 2. 安裝與部署

### v0 安裝方式（從 MyAiStorage 安裝）
Sidecar 位於 `MyAiStorage` repo 內的 `src/sidecar/`，提供 console script `sidecar`。

1. **先決條件**：
   - Python 3.10+
   - `uv` 套件管理工具
   - `herdr`（由 worker 系統或映像提供）

2. **建立專屬虛擬環境並安裝**：
   ```bash
   # 建立專屬 venv 目錄
   uv venv ~/.local/share/agent-sidecar/venv
   source ~/.local/share/agent-sidecar/venv/bin/activate

   # 安裝 MyAiStorage（包含 sidecar、agora、angareion）
   cd /path/to/MyAiStorage
   uv pip install -e .
   ```

3. **配置 systemd user unit**：
   建立 `~/.config/systemd/user/agent-sidecar.service`：
   ```ini
   [Unit]
   Description=Agent Sidecar Supervisor
   After=network.target

   [Service]
   Type=simple
   ExecStart=%h/.local/share/agent-sidecar/venv/bin/python -m sidecar.cli run
   Restart=on-failure
   RestartSec=5s
   Environment=PYTHONUNBUFFERED=1

   [Install]
   WantedBy=default.target
   ```

4. **啟動並啟用開機常駐**：
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now agent-sidecar.service
   ```

### 待辦事項：MyLinuxPool shared-config 單元
未來將依循 MyLinuxPool 規範，將 Sidecar 包裝為 `shared-configs/agent-sidecar/` 單元（包含 `unit.json`、自帶獨立 venv 的 `install.sh`、`--check` 三態健康檢查、`files/` 與 `tests/`），由各 worker profile 依需宣告引用，不設為獨立 profile。此部分另案實作。

---

## 3. 設定檔說明

### 設定檔路徑
- 預設路徑：`~/.config/sidecar/config.json`
- 可透過環境變數 `SIDECAR_CONFIG` 或命令列 `--config <path>` 覆寫。

### 設定欄位
對照 `src/sidecar/config.py` 中的 `SidecarConfig`：

| 欄位名稱 | 型態 | 必要性 | 說明 |
|---|---|---|---|
| `state_dir` | 字串 | 選填 | 狀態與日誌根目錄。預設為 `~/.local/state/sidecar`（可由 `SIDECAR_STATE_DIR` 覆寫）。 |
| `a2a_token_path` | 字串 | 選填/建議 | A2A 通訊用的 GitHub Fine-grained PAT 檔案路徑。必須具備 POSIX `0600` 權限。 |
| `identities` | 字串陣列 | 必填 | 此 worker 負責接收與監聽的角色身分清單（例如 `["team-alpha", "team-beta"]`）。 |
| `teams` | 物件 | 必填 | 各 team 的執行設定映射表（key 為 identity）。 |
| `teams.<id>.home_session` | 字串 | 必填 | 該 team 的長期記憶 agora home Session ID（任務會以 `--fork` 從此衍生）。 |
| `teams.<id>.agent` | 字串 | 選填 | 採用的 coding agent 種類（預設 `"opencode"`，可選 `"claude"`）。 |
| `teams.<id>.harness_path` | 字串 | 選填 | Atelier harness 檔案路徑，啟動時將全文作為第一則輸入送入。 |
| `teams.<id>.sync_period` | 整數 | 選填 | `agora continue` 定期寫回 Drive 的週期秒數（預設 `300`）。 |
| `teams.<id>.cwd` | 字串 | 選填 | Agent 啟動的工作目錄。 |
| `teams.<id>.env` | 物件 | 選填 | 啟動 agent 時額外注入的環境變數。 |
| `llm_gateway` | 字串 | 選填 | LLMGateway 服務的 URL 位址（v0 階段暫由 fake broker 處理）。 |

### 範例設定（不含秘密）
`~/.config/sidecar/config.json`：
```json
{
  "state_dir": "/home/worker/.local/state/sidecar",
  "a2a_token_path": "/home/worker/.config/sidecar/token",
  "identities": [
    "owner-org/worker-team-1"
  ],
  "teams": {
    "owner-org/worker-team-1": {
      "home_session": "01JABCDEF0123456789ABCDEF0",
      "agent": "opencode",
      "harness_path": "/home/worker/.config/sidecar/harness/team1.md",
      "sync_period": 300,
      "cwd": "/home/worker/workspace/project-repo",
      "env": {
        "PROJECT_ENV": "production"
      }
    }
  },
  "llm_gateway": "http://127.0.0.1:8080"
}
```

---

## 4. 憑證與安全邊界

### A2A Token
- **集中持有原則**：A2A Token 僅由 Sidecar 本身持有並讀取。
- **儲存與權限**：存放在 `~/.config/sidecar/token`（或設定指定的路徑）。檔案權限**嚴格要求 `0600`**（僅擁有者可讀寫，禁止 group/other 存取）。`SidecarConfig.verify_token_permissions()` 會進行權限校驗。
- **隔離防護**：
  - Sidecar 透過 herdr 啟動 agent 時，傳遞的環境變數清單嚴格受到白名單保護，**絕不包含 A2A Token**。
  - Agent 不得具備信道 repo 的讀寫權限；Agent 在其工作目錄與檔案系統中無法讀取 A2A Token。
- **Token 輪替步驟**：
  1. 本人於 GitHub 產生新的 Fine-grained PAT（僅授權信道 repo 之 `Issues: Read and write` 權限）。
  2. 覆寫本機 Token 檔案並確保權限：
     ```bash
     echo -n "github_pat_NEW_TOKEN_VALUE" > ~/.config/sidecar/token
     chmod 0600 ~/.config/sidecar/token
     ```
  3. 重啟 Sidecar 服務（`systemctl --user restart agent-sidecar`）或等待下一個請求自動載入。
  4. 確認 `sidecar status` 顯示 `Token: 0600 ok` 且能正常收信。
  5. 撤銷 GitHub 上的舊 PAT。

### 模型憑證邊界
- Sidecar 與 Agent **完全不持有、不讀取、不寫入任何模型供應商金鑰**（如 `ANTHROPIC_API_KEY`、`OPENAI_API_KEY`）。
- 模型存取一律經由 LLMGateway 核發臨時租約與准許憑據。

### Agent 回報中繼（`sidecar say`）
- **機制**：Agent 無法直接連線 Angareion 信道發文。Sidecar 在本地開啟 UNIX domain socket（預設 `$XDG_RUNTIME_DIR/sidecar.sock` 或 `~/.local/state/sidecar/sidecar.sock`）。
- **鑑別驗證**：
  - 伺服端使用 `SO_PEERCRED`（Linux）或 `LOCAL_PEERPID`（macOS）取得發起連線的行程 PID。
  - 遞迴比對 PID 及其父行程樹（Ancestor PIDs），確認該連線行程隸屬於本地 `registry.json` 中處於 `running` 狀態的 agent。
- **身分綁定**：通過驗證後，Sidecar 自動以該 Agent 註冊的團隊身分（`from`）代為在 Angareion 信道發布訊息。
- **命令列呼叫**：Agent 可直接在終端執行薄指令回報：
  ```bash
  sidecar say "已完成任務 42，PR #108 已建立" --to pmo
  ```

---

## 5. 狀態、佇列與日誌

### 狀態目錄結構（`~/.local/state/sidecar/`）
- `registry.json`：登錄檔案，記錄所有經由 Sidecar 啟動的 agent 資訊（agent ID、身分、agora home session、agent kind、herdr tab/pane ID、agent name、啟動時間、狀態、log 路徑等）。
- `queue/<identity>.json`：各身分的持久訊息佇列。訊息按 `(-urgency, at)` 優先級排序。
- `seen.json`：已接收訊息 ID 集合，跨重啟防止重複投遞。
- `agents/<id>/log`：個別 agent pane 的終端輸出日誌，日誌上限比照 agora 規範。

### 原子落檔機制
所有狀態更新（`Registry.save()`、`MessageQueue._save_raw()`、`SeenStore.save()`）均採用 POSIX 原子寫入紀律：
1. 先寫入同目錄下的隱藏暫存檔 `.tmp-...`。
2. 呼叫 `flush()` 與 `os.fsync()` 強制落盤。
3. 透過 `os.replace()` 原子置換目標檔案。

### 壞檔處理機制（Corrupt State Handling）
若 `registry.json` 或 `queue/<identity>.json` 因磁碟錯誤或格式損壞導致無法解析 JSON：
- **禁止靜默重設為空並覆寫**，以防原有排程與狀態資料遭到永久性銷毀。
- **防護行為**：系統會自動將損壞檔案備份為 `.corrupt-<時間戳記>`（例如 `registry.json.corrupt-20261011T160000Z`），並在日誌記錄嚴重錯誤。
- *備註：此項備份行為已在審查（M2）中定案由 agy4 修復，寫完後請與 PM 確認最終行為細節。*

---

## 6. 訊息投遞政策、Ack 與去重

### 投遞政策（依 2026-10-11 herdr spike 實測結果）
依據在 herdr 上的實測（OpenCode 1.18.35 與 Claude Code），執行中透過 `herdr agent prompt` 送字會自動在 agent 端排隊，不會遺失。因此投遞機制劃分為兩段：

#### 高緊急度（`urgency >= 8`）
- **立刻打斷並送入**：
  - 若 agent 當前正在執行回合（`is_working`）：
    - **不得使用 Ctrl+C 打斷**（Ctrl+C 會結束 agent 行程）。
    - 依 spike 結果發送 **Esc** 鍵中斷當前回合：
      - **Claude Code**：送出 1 次 `esc`（畫面提示 `Interrupted · What should Claude do instead?`）。
      - **OpenCode**：送出 2 次 `esc`（第 1 次僅顯示 `esc again to interrupt`）。
    - 等待終端工作提示（Claude 的 `esc to interrupt`、OpenCode 的 `esc interrupt`）消失，確認回到 prompt 後，立即以 `herdr agent prompt` 送入。
  - 若 agent 當前為 idle（未在執行回合）：直接以 `herdr agent prompt` 送入。

#### 一般/低緊急度（`urgency < 8`）
- **排隊投遞**：
  - 若 agent 正在執行：寫入持久佇列；若 agent 正在執行，可直接送入輸入暫存排隊（於下一輪執行）。
  - 若 agent 尚未啟動：保留在持久佇列中，待下次啟動（hand-off）時依序送出。

#### 訊息排序
同一批投遞的訊息，優先以 `urgency` 由高到低排序；相同 `urgency` 則依時間 `at` 由舊到新排序。

### Ack 時機與去重
- **Ack 語意**：代表「**已成功交付給 agent 輸入**」，非「任務已完成」。
- **觸發點**：僅當訊息透過 `herdr agent prompt` 成功送達 agent 後，Sidecar 才會在信道發布 `ack <id>` 留言。
- **去重（At-least-once）**：若 Sidecar 在投遞成功與發布 Ack 之間當機，下次重啟會再次讀取該訊息。Sidecar 藉由 `seen.json` 記錄之 ID 進行比對，已投遞之訊息絕不重複送入 agent。

---

## 7. 啟動、終止與 Herdr 注意事項

### 啟動流程
1. 透過 herdr 建立獨立 tab/pane（支援工作目錄與環境隔離）。
2. 在 pane 內執行啟動指令：
   ```bash
   agora continue session <home_session> --agent <agent_kind> --fork --sync-period <sync_period>
   ```
   - `--fork`：確保為該任務建立全新的衍生 Session，團隊的 `home_session` 保持純淨不受污染。
   - `--sync-period`：設定定期背景寫回秒數（預設 300 秒），確保長期任務進度定時同步回 Drive。
3. 透過 `herdr agent rename` 為該 agent 命名（格式為英數小寫，32 字元以內）。
4. 偵測 agent 啟動並就緒後，將對應之 harness 全文送入作為第一則提示。

### 終止的三段式降級升級（Escalation Termination）
當需要終止運行中的 agent 時，Sidecar 依序執行三段式處理：
1. **階段 1 (SIGINT)**：透過 `herdr agent send-keys <name> ctrl+c` 發送中斷，給予 agent 5 秒時間收尾並觸發 agora 結束寫回。
2. **階段 2 (SIGTERM)**：若 5 秒後該 pane 前景行程群組（process group）仍未結束，向其 PGID 發送 `SIGTERM`，再次等待 5 秒。
3. **階段 3 (SIGKILL & 收尾)**：若仍未結束，向其 PGID 發送 `SIGKILL` 強制結束行程。最後關閉 herdr pane（`herdr pane close`），並將終止狀態記錄於 `registry.json` 與日誌。

### Herdr 使用注意事項
- **嚴禁巢狀執行（No Nesting）**：
  - Sidecar 本身由 systemd 啟動，必須在獨立的守護進程環境運行，**嚴禁在 herdr 的任何 pane 內部執行 Sidecar**。
  - Sidecar 啟動時會主動檢查並清理 `HERDR_ENV`、pane 相關環境變數；若環境被污染則拒絕啟動。
  - 嚴禁執行不帶子命令的 `herdr` 指令（避免誤觸 TUI）。
- **OpenCode 狀態判讀**：
  - `herdr agent get` 提供的狀態在 OpenCode 不夠精準。
  - 判斷 OpenCode 是否正在執行回合，必須透過 `herdr agent read` 檢查終端畫面末端是否包含 `esc interrupt`。

---

## 8. 與外部系統邊界

### 與 LLMGateway（ModelBroker）的邊界
- Sidecar 不直接管理模型供應商的 API 金鑰。
- 架構上定義了 `ModelBroker` Protocol：
  - `request_lease(worker, role, coding_agent, model)`：在啟動 agent 前申請模型租約。
  - `release(lease_id)`：在 agent 結束或終止時釋放租約。
- 在 MyPMO 的 LLMGateway 協定對齊前，Sidecar 內建 `FakeModelBroker` 用於本機開發與單元測試。

### 與 Atelier（#32）的邊界
- Atelier 負責定義各 team 的任務目標與 harness。
- 目前 #32 暫停中；Sidecar 與 Atelier 的對接 Seam 已固定：**Sidecar 僅負責在 agent 啟動 prompt 出現後，將 harness 檔案全文無損作為第一則輸入送入**，不對其內容進行解析、摘要或修改。

---

## 9. 本人端到端驗收（Task 5.1）草稿指南

> [!CAUTION]
> **以下步驟涉及真實 GitHub 信道、真實 Worker 環境與憑證，僅供本人（專案擁有者）親自執行。隊員（AI）嚴禁操作。**

### 準備工作
1. 在目標 Worker 機器上，確認 `herdr server` 正在運行（`herdr status server --json` 回傳正常）。
2. 安裝 Sidecar 並配置 `~/.config/sidecar/config.json`，將 `identities` 設為測試團隊身分（例如 `owner-org/worker-team`）。
3. 放置有效的 Fine-grained PAT 於 `~/.config/sidecar/token`，並設定 `chmod 0600 ~/.config/sidecar/token`。
4. 啟動 Sidecar 服務：
   ```bash
   systemctl --user start agent-sidecar
   sidecar status
   ```
   確認輸出 `Token: 0600 ok`，狀態正常。

### 驗收步驟
1. **[PMO 端] 發送任務訊息**：
   在控制端執行：
   ```bash
   a2a send --to "repo=owner-org/worker-team,role=worker" --channel test-e2e --body "開始執行端到端驗證任務"
   ```
2. **[Worker 端] 驗證 Sidecar 收信與啟動 Team**：
   - 觀察 `sidecar status`，確認訊息入列並啟動 agent。
   - 執行 `herdr agent list`，確認 agent 正常啟動且名稱符合規範。
   - 可用 `herdr session attach` 觀察 agent 視窗，確認 harness 已作為第一則輸入載入。
3. **[PMO 端] 驗證 Ack 交付確認**：
   - 檢查信道上的 issue comments，確認出現包含 `ack <id>` 的確認留言。
4. **[Worker 端] 驗證 Agent 中繼回報**：
   - 在 agent pane 內或透過 herdr 執行：
     ```bash
     sidecar say "任務執行中，已完成階段 1" --to pmo
     ```
   - 確認 PMO 端信箱（`a2a inbox`）收到來自 `owner-org/worker-team` 的回報訊息。
5. **[Worker 端] 驗證終止流程**：
   - 透過 Sidecar 觸發終止 team，觀察是否依序發送 Ctrl+C，等待 5 秒後結束，pane 正常關閉。
   - 檢查 agora 是否已自動將對話寫回 Drive。
6. **[Worker 端] 驗證 Sidecar 重啟恢復**：
   - 啟動一個 long-running agent，手動重啟 Sidecar 服務（`systemctl --user restart agent-sidecar`）。
   - 執行 `sidecar status`，確認重新辨識出正在運行的 agent，未丟失追蹤。
