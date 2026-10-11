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
- **Angareion 訊息處理**：使用 `angareion` 程式介面直接輪詢信道、條件式 ETag/304 查詢與 `since` 游標、遵循次級速率限制（Secondary Rate Limits）與訊息去重。
- **持久佇列**：收到的訊息在投遞前先原子落檔至本地優先級佇列。
- **依緊急度投遞**：
  - 高緊急度（`urgency >= 8`）：發送 Esc 中斷當前回合並立即送入輸入。
  - 一般/低緊急度（`urgency < 8`）：進入持久佇列，排隊於 agent 當前或下一次 hand-off 送入。
- **投遞後 Ack**：僅在訊息確實進入 agent 輸入後，才在信道上發送 Ack 確認留言。
- **啟動與終止 Team**：透過 herdr 管理終端視窗，執行 `agora continue session <home> --agent <agent> --fork --sync-period <N>`（`<N>` 單位為分鐘，預設 `5`；agora 讀的是分鐘，design D4／D10）；終止採三段式降級升級（SIGINT -> SIGTERM -> SIGKILL）。
- **狀態登錄與重啟恢復**：維護本地 `registry.json`，重啟後對照 herdr agent list 找回運行中的 agent。
- **Atelier Harness 載入**：在 agent 啟動並就緒（prompt 出現）後，將 harness 檔案全文無損作為第一則輸入送入，再把佇列中屬於該身分的低緊急度訊息依序送出（hand-off，D10 接線）。
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
    - Python 3.12+（`pyproject.toml` 的 `requires-python = ">=3.12"`）
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

    `sidecar run` 是常駐入口（D10）：一個迴圈接起三件事——每個身分的收信輪詢（照 Angareion 的節流，預設 30 秒一次）、回報中繼（`RelayServer`）、定期對照 herdr 找回 agent 並更新 registry 的 `updated_at`（心跳，預設 300 秒一次）；`Receiver` 在這裡建立，交給啟動 team 的流程用。後端報錯不會殺掉 daemon（速率限制按 `retry_after` 退避，其餘記 warning 下一輪重試）。常用旗標：`--poll-interval`、`--recover-interval`、`--channel`（回報預設頻道，預設 `general`）、`--sock`、`--once`（只跑一次輪詢＋recover 就退出，除錯用）。`channel_repo` 未設或 token 未設時直接 exit 1 並說明原因。


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
| `a2a_token_path` | 字串 | 選填/建議 | A2A 通訊用的 GitHub Fine-grained PAT 檔案路徑。權限 owner-only（`0600` 或 `0400`；`0440`／`0644` 等拒絕，規則見 angareion §5）。worker 上填 `/run/mlp-secrets/angareion-token`（`0400` 唯讀掛載）。token 檔由本人放置，隊員不碰。 |
| `channel_repo` | 字串 | 必填 | Angareion 信道 repo（例如 `"owner-org/a2a-channel"`）。中繼送出時頻道不存在直接回錯誤，不自動建（D10）。 |
| `identities` | 位址陣列 | 必填 | 此 worker 負責接收與監聽的身分清單；每個身分是完整的 Angareion 位址 `{repo, role, name}`（`repo`、`role` 必填，`name` 選填；收信匹配只比對 `repo`＋`role`）（D10）。 |
| `teams` | 物件 | 必填 | 各 team 的執行設定映射表（key 為 team 身分；D10 接線實作中，鍵的字面寫法待與實作 commit 對字後定稿）。中繼送出的 `from` 就是該 team 的完整位址，不再寫死（D10）。 |
| `teams.<id>.home_session` | 字串 | 必填 | 該 team 的長期記憶 agora home Session ID（任務會以 `--fork` 從此衍生）。 |
| `teams.<id>.agent` | 字串 | 選填 | 採用的 coding agent 種類（預設 `"opencode"`，可選 `"claude"`）。 |
| `teams.<id>.harness_path` | 字串 | 選填 | Atelier harness 檔案路徑，啟動時將全文作為第一則輸入送入。 |
| `teams.<id>.sync_period` | 整數 | 選填 | `agora continue` 定期寫回 Drive 的週期**分鐘**數（預設 `5`，與 agora `--sync-period` 同單位，組命令時原樣帶給 agora，不換算）（D10）。 |
| `teams.<id>.cwd` | 字串 | 選填 | Agent 啟動的工作目錄。 |
| `teams.<id>.env` | 物件 | 選填 | 啟動 agent 時額外注入的環境變數（白名單，見 §4；不帶時為空）。 |
| `llm_gateway` | 字串 | 選填 | LLMGateway 服務的 URL 位址（v0 階段暫由 fake broker 處理）。 |

### 範例設定（不含秘密）
`~/.config/sidecar/config.json`：
```json
{
  "state_dir": "/home/worker/.local/state/sidecar",
  "a2a_token_path": "/home/worker/.config/sidecar/token",
  "channel_repo": "owner-org/a2a-channel",
  "identities": [
    {"repo": "owner-org", "role": "worker", "name": "team-1"}
  ],
  "teams": {
    "owner-org/worker-team-1": {
      "home_session": "01JABCDEF0123456789ABCDEF0",
      "agent": "opencode",
      "harness_path": "/home/worker/.config/sidecar/harness/team1.md",
      "sync_period": 5,
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
- **集中持有原則**：A2A Token 僅由 Sidecar 本身持有並讀取。token 檔由本人親自放置與輪替，隊員不碰、不讀（本 repo 是 public，憑證不進版控）。
- **儲存與權限**：存放在 `~/.config/sidecar/token`（或 `a2a_token_path` 指定的路徑）。檔案權限要求 owner-only（`0600` 或 `0400`；`0440`／`0644` 等拒絕，規則見 angareion §5）。`SidecarConfig.verify_token_permissions()` 會進行權限校驗。worker 上的憑證是平台唯讀掛載的 `/run/mlp-secrets/angareion-token`（`0400`），`a2a_token_path` 指過去即可，不手動寫入或 chmod。
- **隔離防護**：
  - Sidecar 透過 herdr 啟動 agent 時，傳給 pane 的環境只來自 `teams.<id>.env`（白名單）；不帶 `env` 時給空的附加環境，**絕不把 Sidecar 自己的整個行程環境（含 A2A Token）傳給 agent**（D10）。
  - token 只放檔案，**不 export 到登入環境**：herdr server 繼承來的環境變數 Sidecar 擋不住，export 了等於每個 pane 都有（D10）。
  - Agent 不得具備信道 repo 的讀寫權限；Sidecar 不把 token 放進 agent 的環境或工作目錄。
  - 誠實版邊界：v0 不做 OS 層級硬隔離——Sidecar 與 agent 跑在同一個 worker user 下，技術上 agent 行程讀得到 token 檔；設計的邊界是 agent 不碰（中繼是唯一出口），靠「不給、不用、log 可查」維持（design「誰呼叫 agora、憑證在誰手上」）。
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
   （worker 上 `/run/mlp-secrets/angareion-token` 由平台掛載提供，輪替走平台流程，不手動覆寫。）

### 模型憑證邊界
- Sidecar 與 Agent **完全不持有、不讀取、不寫入任何模型供應商金鑰**（如 `ANTHROPIC_API_KEY`、`OPENAI_API_KEY`）。
- 模型存取一律經由 LLMGateway 核發臨時租約與准許憑據。

### Agent 回報中繼（`sidecar say`）
- **機制**：Agent 無法直接連線 Angareion 信道發文。Sidecar 在本地開啟 UNIX domain socket（預設 `$XDG_RUNTIME_DIR/sidecar.sock` 或 `~/.local/state/sidecar/sidecar.sock`）。
- **鑑別驗證**：
  - 伺服端使用 `SO_PEERCRED`（Linux）或 `LOCAL_PEERPID`（macOS）取得發起連線的行程 PID。
  - 遞迴比對 PID 及其父行程樹（Ancestor PIDs），確認該連線行程隸屬於本地 `registry.json` 中處於 `running` 狀態的 agent。
- **身分綁定**：通過驗證後，Sidecar 自動以該 Agent 所屬 team 的完整位址 `{repo, role, name}`（`from`）代為在 Angareion 信道發布訊息，不寫死（D10）。
- **urgency 範圍**：`sidecar say` 與中繼只收 `0`～`9`（Angareion spec），超出範圍回錯誤，不靜默丟掉（D10）。
- **頻道必須已存在**：回報送往預設 channel `general`（`sidecar run --channel` 可改）；該 channel 須事先建好（`a2a send --channel general --new …`），不存在時中繼回錯誤，**不自動建頻道**（D10）。
- **命令列呼叫**：Agent 可直接在終端執行薄指令回報：
  ```bash
  sidecar say "已完成任務 42，PR #108 已建立" --to pmo
  ```

---

## 5. 狀態、佇列與日誌

### 狀態目錄結構（`~/.local/state/sidecar/`）
- `registry.json`：登錄檔案，記錄所有經由 Sidecar 啟動的 agent 資訊（agent ID、身分、agora home session、agent kind、herdr tab/pane ID、agent name、啟動時間、狀態、log 路徑等）。
- `queue/<identity>.json`：各身分的持久訊息佇列。訊息按 `(-urgency, at)` 優先級排序。`<identity>` 經 percent-encoding（例如身分 `owner-org/worker-team` 對應 `owner-org%2Fworker-team.json`）。
- `seen.json`：已接收訊息 ID 集合，跨重啟防止重複投遞。
- `agents/<id>/log`：個別 agent pane 的終端輸出日誌，日誌上限比照 agora 規範。

### 原子落檔機制
所有狀態更新（`Registry.save()`、`MessageQueue._save_raw()`、`SeenStore.save()`）均採用 POSIX 原子寫入紀律：
1. 先寫入同目錄下的隱藏暫存檔 `.tmp-...`。
2. 呼叫 `flush()` 與 `os.fsync()` 強制落盤。
3. 透過 `os.replace()` 原子置換目標檔案。

### 壞檔處理機制（Corrupt State Handling）
若 `registry.json`、`queue/<identity>.json` 或 `seen.json` 因磁碟錯誤或格式損壞導致無法解析 JSON：
- **禁止靜默重設為空並覆寫**，以防原有排程與狀態資料遭到永久性銷毀。
- **防護行為**：系統會自動將損壞檔案備份為 `.corrupt-<時間戳記>`（時間戳記為 `strftime("%Y%m%d%H%M%S")`，例如 `registry.json.corrupt-20261011160000`，無 `T` 無 `Z`），並在日誌記錄嚴重錯誤。
- *備註：此項備份行為已在第一批審查（M2）定案並由 `22fcfed` 實作完成（registry／queue／seen 三者皆備份）。*

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
    - `--sync-period`：定期背景寫回的**分鐘**數（預設 **5** 分鐘），原樣帶給 agora，不換算，確保長期任務進度定時同步回 Drive。注意 agora 讀的是分鐘，舊文件曾誤寫為「秒／300」。
3. 透過 `herdr agent rename` 為該 agent 命名（格式為英數小寫，32 字元以內）。
4. 等 agent 的 prompt 出現（`pane wait-output`，預設等 60 秒）後，將對應之 harness 全文送入作為第一則提示；等不到就記 registry（狀態 `starting`、原因在 `exit_reason`）並報錯，不靜默送空。

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

## 9. 本人端到端驗收（Task 5.1）

> [!CAUTION]
> **以下步驟涉及真實 GitHub 信道、真實 Worker 環境與真實憑證，僅供本人（專案擁有者）親自執行。隊員（AI）嚴禁執行、嚴禁接觸真實 token。標 `[本人]` 的步驟含憑證操作，只能本人做。**

驗收路線（tasks 5.1）：PMO 送一則 → sidecar 收 → 啟動 team → team 用 `sidecar say` 回報 → PMO 收到；另外各做一次終止與 sidecar 重啟。每一步都是「做什麼、預期看到什麼、失敗時看哪個 log」。

### 準備工作（Worker 端）

1. **[本人] 確認 `herdr server` 在跑**：
   ```bash
   herdr status server --json
   ```
   - 預期：回傳 server 正常。若不正常，先依 MyLinuxPool#47 把 server 拉起再繼續（sidecar 起不來多半是它）。

2. **安裝 Sidecar 並寫設定**（§2、§3）：
   - `~/.config/sidecar/config.json`：`identities` 放測試 team 的完整位址（例如 `{"repo": "owner-org", "role": "worker", "name": "team-1"}`），`channel_repo` 指信道 repo，`teams` 內給 `home_session`（事先存在的 agora home session）、`harness_path`（檔案須存在）、`sync_period`（分鐘，`5`）、`cwd`。
   - 預期：`python -m sidecar.cli status` 不報設定錯誤（服務還沒起沒關係）。

3. **[本人] 放置 token 並鎖權限**：
   worker 上 `a2a_token_path` 指 `/run/mlp-secrets/angareion-token`（`0400` 唯讀掛載，見 §4），只確認存在，不手動寫入或 chmod：
   ```bash
   ls -l /run/mlp-secrets/angareion-token
   ```
   自管檔案才走寫入流程：
   ```bash
   install -m 0600 /dev/null ~/.config/sidecar/token
   cat > ~/.config/sidecar/token   # 貼上 Fine-grained PAT（僅信道 repo 的 Issues: Read and write），Ctrl-D 結束
   chmod 0600 ~/.config/sidecar/token
   ls -l ~/.config/sidecar/token
   ```
   - 預期：掛載路徑看到 `-r--------`，自管檔案看到 `-rw-------`（Linux/macOS 通用）。token 只放檔案、不 export（§4）。
   - 失敗：`sidecar status` 的 `Token:` 不是 `0600 ok` → 權限或路徑錯（`0440`／`0644` 等會被拒）；先修好再往下。

4. **[本人] 預建回報 channel**（中繼不自動建頻道，不存在會回錯誤）：
   ```bash
   a2a send --to "repo=owner-org,role=worker,name=team-1" --channel general --new --body "建回報 channel，本則可忽略"
   ```
   - 預期：stdout 一行 `<id> general`，exit 0；信道 repo 出現 Issue `general`。

5. **啟動 Sidecar 並確認健康**：
   ```bash
   sidecar run --once   # 先跑一次輪詢＋recover 就退出：設定、token、herdr 有問題會直接 exit 1 並說明
   systemctl --user enable --now agent-sidecar.service
   sidecar status
   ```
   - 預期：`--once` exit 0；`sidecar status` 顯示 `Token: 0600 ok`，`Agents: 0 running`，`Queues:` 各身分皆 `0 queued`。
   - 失敗：`--once` 的 stderr（`channel_repo` 未設、`a2a_token_path` 未設、token 讀失敗、herdr 連不上都會明說）；常駐後的問題看 `journalctl --user -u agent-sidecar.service`（sidecar 行程輸出都在這裡）。

### 步驟 1［PMO 端］送一則任務

```bash
a2a send --to "repo=owner-org,role=worker,name=team-1" --channel sidecar-e2e --new --urgency 5 --body "開始執行端到端驗證任務"
```

- 預期：stdout 一行 `<id> sidecar-e2e`，exit 0；記下這個 `<id>`（後面核對 ack 用）。信道 repo 出現 Issue `sidecar-e2e`，底下有一則含 front matter 的留言（`to` 是 team 的完整位址）。
- 失敗：exit 1 是參數或 channel 問題（看 stderr，不含秘密）；exit 2 是後端或 token 問題（stderr 已遮罩 token；回頭檢查 token 檔、`channel_repo` 拼字、PAT 權限）。

### 步驟 2［Worker 端］sidecar 收到並入列

等下一個收信輪詢週期後執行：

```bash
sidecar status
```

- 預期：`Queues:` 裡該身分計數變 `1 queued`；`~/.local/state/sidecar/queue/<percent-encoded 身分>.json` 裡有步驟 1 的那個 `id`。
- 失敗：計數一直是 0 → 先核對訊息的 `to`（`repo`＋`role`）是否等於設定裡的身分、再看 `journalctl --user -u agent-sidecar.service` 有無 poll 報錯；懷疑信道端時直接去 GitHub 看 Issue 底下到底有沒有那則留言。

### 步驟 3［Worker 端］team 啟動，harness 先進

- 預期（不需手動啟動，`sidecar run` 迴圈看到無 running agent 的身分有信就會啟動）：
  1. `herdr agent list` 多出一個 agent（名稱英數小寫 32 字內）。
  2. `sidecar status` 的 `Agents:` 變 `1 running`，且 `~/.local/state/sidecar/registry.json` 有該 agent 的 `running` 記錄。
  3. attach 進 pane 看到 harness 全文是第一則輸入；佇列計數回到 `0 queued`（hand-off 已送）。
- 失敗：agent 沒起來 → `journalctl` 看 `start_team` 報錯（常見：`home_session` 沒設、`harness_path` 檔案不存在、等不到 prompt 超過 60 秒——此時 registry 該筆狀態為 `starting` 並記原因）；pane 起了但 agora 沒跑 → 看 `~/.local/state/sidecar/agents/<id>/log` 的 pane 輸出。

### 步驟 4［PMO 端］ack 已交付

- 預期：`sidecar-e2e` 的 Issue 底下出現 `ack <步驟1的id>` 的確認留言（ack 語意是「已交付 agent 輸入」，不是任務完成）。
- 失敗：一直沒 ack → 回 Worker 看佇列是否還躺著（`sidecar status`／queue 檔）＋`journalctl`；訊息若還在佇列，表示投遞沒成功，不要直接 ack 了事。

### 步驟 5［Worker 端］team 用中繼回報

在 agent 的 pane 內執行（或經 herdr 送進該 pane）：

```bash
sidecar say "任務執行中，已完成階段 1" --to pmo
```

- 預期：stdout `Sent message to pmo (from …， id=…)`，exit 0；`from` 是該 team 的完整位址 `{repo, role, name}`（可用 `a2a inbox --all` 或 Issue 頁核對）。`urgency` 只用 `0`～`9`，超出範圍會回錯誤。
- 失敗：`Sidecar relay socket not found` → 中繼沒在跑（`systemctl --user status agent-sidecar`＋`journalctl`；`say` 與 `run` 的 `--sock`／`SIDECAR_SOCK` 須一致，預設相同）；`Unauthorized: pid …` → 送話的 pane 不是 sidecar 登記的 running agent（`sidecar status` 對 `registry.json`）；其餘錯誤看 stderr 字串＋`journalctl`。

### 步驟 6［PMO 端］收到回報

```bash
a2a inbox --identity "repo=pmo,role=pmo"
```

（收件匹配只比 `repo`＋`role`，`name` 不影響；不帶 `--identity` 時用自己 config 的身分。）

- 預期：列出步驟 5 的回報（`from` 是 team 的完整位址，channel 為 `general`）；或直接開 `general` 的 Issue 頁看到該留言。
- 失敗：inbox 空的 → 先確認 `--identity` 與中繼送出的 `to`（`repo=pmo,role=pmo`）對得上，再去 `general` Issue 頁看留言到底有沒有（有留言但 inbox 空＝身分不對；連留言都沒有＝步驟 5 沒送出去）。

### 步驟 7［Worker 端］終止一次

觸發 sidecar 終止該身分的 team（終止操作入口隨 D10 接線落地，屆時以實作的指令操作；三段式流程見 §7），然後觀察：

- 預期：
  1. 先送 Ctrl+C（pane 內 agent 開始收尾），5 秒後未結束才升級 SIGTERM，再 5 秒才 SIGKILL。
  2. 結束後 herdr 的 pane 關閉（`herdr agent list` 看不到它），`sidecar status`／`registry.json` 記為 terminated。
  3. agora 把這次對話寫回 Drive（`--fork` 出來的新 Session；home Session 保持純淨）。
- 失敗：pane 還活著 → `journalctl` 看終止停在哪一段；registry 沒更新 → 看 `journalctl`＋`registry.json` 是否被壞檔備份（`.corrupt-` 檔名，見 §5）；對話沒上 Drive → 看 pane log 的寫回輸出。

### 步驟 8［Worker 端］sidecar 重啟一次

1. 先讓一個 team 處於 running（重做步驟 1～3，或留著步驟 3 的 agent 不要終止）。
2. 重啟服務：
   ```bash
   systemctl --user restart agent-sidecar.service
   sidecar status
   ```
- 預期：同一個 agent（同 agent id、同 pane）仍列為 running；Queues 計數不變（沒投遞的訊息還在）；`registry.json` 的 `updated_at` 有更新（心跳每輪 recover 更新，預設 300 秒）。再從 PMO 送一則低緊急度訊息，確認重啟後投遞正常。
- 失敗：agent 被標成 exited → `journalctl` 看重啟時的 recover 段，並用 `herdr agent list` 對照它是不是真的死了；若 herdr 剛好連不上，sidecar 不該改 registry（fail-closed），把 `journalctl` 的 warning 貼給 PM。
