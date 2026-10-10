## 交本人選的問題

sidecar 的範圍有三種切法，**請本人選定之後才實作**；下面是利弊對照與 arch 的建議，只是參考、不是替你選。

| | **A：通訊＋心跳** | **B：A＋啟動與關閉** | **C：B＋記憶** |
|---|---|---|---|
| 做什麼 | 只收 Angareion、投遞、agent 心跳與狀態 | 再加：用 `agora continue` 啟動、終止、匯出 agent、pty 投遞 | 再加：管 agora Session 的選擇、載入、存回、fork、壓縮與歸檔 |
| 緊急打斷 | 做不到（不控 process，只能等投遞點） | 做得到（sidecar 自己握 agent 的終端機） | 做得到 |
| team 誰啟動 | 人／別的機制；worker 上沒有 herdr 的等價物 | sidecar | sidecar |
| 對 #31 v0 的影響 | 與 v0 已定的「啟動、終止 team」衝突，要改 v0 | 就是 v0 | 超過 v0 |
| 誰拿 A2A token | 收發的人各自拿；要每個 agent 都給，收窄不了 | sidecar 獨拿；agent 經中繼回報 | 同 B |
| 誰拿模型憑證 | LLMGateway（sidecar 不碰） | LLMGateway 發准許，sidecar 照准許啟動、回報 | 同 B |
| 與 LLMGateway 怎麼溝通 | 幾乎無關（不啟動 agent） | sidecar 申請／回報；LLMGateway 記錄 worker、角色、coding agent、模型 | 同 B（記憶不進 LLMGateway） |
| 成本／風險 | 最小；流程要人手接、打斷做不到 | 中；pty／tmux 與 agent 版本的差異、殭屍行程 | 大；與 agora 深度耦合，要先有 B 穩定 |
| arch 的建議 | | **建議先做這個**（等於 #31 的 v0；把 C 的介面留好） | 等 B 穩定後再擴 |

選 A 的話 #31 的 v0 要改寫；選 B 就是 issue 已定的 v0；選 C 會把期 1 拉長。以下設計以「B 的架構、A 的介面留白、C 的介面留白」寫成，讓三種選擇都接得上。

## Context

- 期 1 的流程：本人跟 PMO 說「做某張單」→ PMO 用 Angareion 發給 worker 上的 AI team → team（由 sidecar 啟動與控制）接手、開 PR、用 Angareion 回報 → PMO 轉告本人、本人 merge。sidecar 是 worker 端的接點。
- 已有的東西：agora（本機 Session 管理；`continue` 會把對話寫回原本的 Session，另有 `--fork`（#29）與 `--sync-period`（#28）在另一個分支、尚未 merge）；Angareion（#30，本期的 A2A 信道）；MyLinuxPool 的 shared-config 單元制度（pool-runtime 是可參考的例子）；Mac 上現在用 herdr 開 agent、看狀態。
- 尚不存在的東西：LLMGateway 的設計（MyPMO，本期同步進行）；Atelier 的 harness（#32）。這兩者的介面是本設計的外部依賴。
- 硬性限制：金鑰由本人自己設、不經 AI；這個 repo 是 public，設計不寫私人資訊；隊員的測試不碰真的 worker、真憑證。

## Goals / Non-Goals

**Goals**

- 定死 sidecar 在 worker 上的位置：它跟 agent 的關係、跟 agora 的關係、跟 Angareion 的關係、跟 LLMGateway 的關係。
- 定死 v0 的行為：收 Angareion → 依緊急度投遞；啟動、終止 team；重啟之後還活著。
- 定死分發方式：一個 shared-config 單元，不是一個 profile。
- 把三種範圍切法的介面留好，讓本人選完可以只加一層。

**Non-Goals**

- 本 change 不實作（設計經本人看過、選定範圍後才開工）。
- 不是它啟動的 agent 的發現與納管（v0 不做；介面留白）。
- 多 worker 的調度、跨 worker 的 team。
- 模型代理本身（LLMGateway 的事）。
- 期 4 的「監視 worker 內所有 agent」。
- 群組定址（Angareion 期 2）。

## Decisions

### D1 程式模型：一個 worker 一個常駐 process，systemd user unit

- 程式在 MyAiStorage：`src/sidecar/`、console script `sidecar`（跟 agora、angareion 同一個 repo、同一種安裝方式）。
- 一個 worker 跑一個 sidecar process（systemd user unit `agent-sidecar.service`），設定裡列它服務的身分（team／角色）清單；不為每個 team 開一個 process（一個 worker 的狀態集中一處，重啟一次全部恢復）。
- A2A 收發用 angareion 的**程式介面**（同 repo 直接 import），不是 subprocess 呼叫 `a2a`（少一層解析、錯誤處理一致）；`a2a` 指令照舊給人用。
- 設定 `~/.config/sidecar/config.json`（`A2A` 的 token 路徑、身分清單、每隊的 agent 種類與 harness 路徑、LLMGateway 位址）；狀態 `~/.local/state/sidecar/`。

### D2 agent 宿主：sidecar 專屬的 tmux server

- agent 不是 sidecar 的直接子程序，而是跑在 **sidecar 專屬的 tmux server** 裡（`tmux -L sidecar`，和使用者自己的 tmux 隔離）：每個 agent 一個 session（`agent-<role>-<ulid>`），pane 跑 `agora continue …`。
- 這樣做的理由：
  - **重啟接得回來**：sidecar 重啟後用 `tmux list-sessions` 重新找到還在跑的 agent——in-process 的 pty 做不到這件事，而「sidecar 當掉就失去對 agent 的控制」是監督程式不合格的表現。
  - **送字有現成、被大量驗證過的機制**（`tmux send-keys`），不必自己寫 pty 的重連與視窗大小處理。
  - **人看得到**：必要時人可以 `tmux -L sidecar attach` 看 agent 在做什麼（今天在 Mac 上用 herdr 看，等價物在 worker 上就是這個）。
- 替代方案（不採）：in-process pty（簡單，但重啟即失去控制）；不控 process、只讀 log（做不到投遞）。

### D3 投遞政策：urgency 兩段、ack 在投遞後

- `urgency` ≥ 8：**立刻**送進 agent 的輸入（`tmux send-keys -l <text>` 加 Enter，成為它下一輪的 user turn）。agent 正在跑一輪時，先把訊息送進去讓它排隊；**若實測 agent 不排隊而是吃掉/丟掉**，退回「先送一次 Ctrl-C 讓它收掉這一輪、回到提示再送」——這條用 spike 在 opencode 與 claude 各驗一次，結果寫回 design（見 tasks 2.4）。
- `urgency` < 8：進持久佇列；在下一次 hand-off 送出。v0 保證的 hand-off ＝ agent 程序結束後、下一次啟動時。做法：佇列裡的訊息排序好，agent 一啟動（tmux session 起來、prompt 出現）就用 send-keys 依序送出。
- 同一批依 `urgency` 高到低、同 `urgency` 依 `at` 舊到新。
- **投遞成功才 ack**：訊息進了 agent 的輸入才 `a2a ack`。語意是「已交付」，不是「已完成」；完成的訊號是 team 後來的回報訊息。
- at-least-once：sidecar 在投遞與 ack 之間當掉，訊息會再投一次；用訊息 `id` 去重，MUST NOT 重複送出。

### D4 啟動與終止（B 的核心）

- **啟動**：每個 team 有一個 agora 的 **home Session**（團隊的長期記憶，例如「<team> 的收件匣」）。一個任務 `agora continue session <home> --agent <agent> --fork --sync-period <N>`（#29、#28 的介面；依賴它們 merge）：
  - `--fork`：任務之間隔離，每個任務一個新 Session，home 不被改動；
  - `--sync-period`：agent 跑很久時 Drive 上的版本看得到進度（預設 5 分鐘）。
- **harness 載入**（#32 的 seam）：agent 起來、prompt 出現後，sidecar 把 harness 全文當第一則輸入送進去（或依 #32 最後定的載入點）；sidecar 不解讀內容。
- **終止**：對 agent 的 process group 依序 SIGINT →（等 5 秒）SIGTERM →（等 5 秒）SIGKILL，和互動模式的 escalate 同一套；pid 由 tmux pane 取得。
- **匯出**：`agora continue` 啟動的對話由 continue 自己寫回；如果是別的方式啟動的 agent（例如人手開的，A 的世界），結束後用 `agora import session --external-session-id <id> --agent <agent>` 匯出。sidecar MUST NOT 自己拼 agent 的原始檔。
- **暫停／恢復**（設計裡保留、v0 不必做）：SIGSTOP／SIGCONT 對 process group；注意停在工具呼叫中間可能讓 TUI 顯示錯亂，文件要寫。

### D5 登錄與發現

- v0：**只認自己啟動的 agent**。registry 檔（`~/.local/state/sidecar/registry.json`）記：agent id、角色、agora Session、agent 種類、pid、tmux session 名、啟動時間、狀態（starting／running／exited／terminated）、log 位置。
- sidecar 重啟：讀 registry，用 `tmux has-session` 與 pid 對照，還在的標 running、不在的標 exited（並把 exit 狀態記下來）。
- **發現外來 agent**（v0 不做，介面留白）：未來用一個 register 介面（unix socket，agent 自己報到）或掃 process；設計上把「agent 清單」抽成一個 provider，registry 只是其中一種來源。
- **心跳**：v0 的心跳是本機 registry 的 `updated_at`＋`sidecar status --json`；**不為心跳發 A2A 訊息**（省額度）。要跨機器看狀態是期 2 的 presence，或人去看 log／請 team 回報。

### D6 憑證邊界（與 LLMGateway、本人）

| 憑證 | 誰設定 | 誰持有 | 備註 |
|---|---|---|---|
| A2A token（#30） | 本人（capability） | **只有 sidecar** | 0600；agent 環境與檔案裡沒有；agent 回報經中繼 |
| 模型存取 | LLMGateway（本人設定的上游） | LLMGateway；agent 拿「這次准許」對應的存取方式 | sidecar 不持有、不讀、不寫模型金鑰 |
| worker 的 git 憑證（開 PR） | 本人（capability，worker 既有） | agent 的環境（照 worker 的既有方式） | sidecar 不管、不複製、不記 log；v0 不動它 |
| Angareion 的 channel repo | 本人 | 只有 sidecar（同上） | worker 上的 agent 沒有 GitHub 存取 |

- **sidecar ↔ LLMGateway 的溝通**（B／C）：啟動前 sidecar POST 一個「開 agent」申請（worker、角色、coding agent、模型）→ gateway 回准許（含 agent 要用的模型存取方式與效期）或拒絕 → sidecar 照回覆設定 agent 環境並啟動 → 結束／終止後回報。協定細節等 MyPMO 的 LLMGateway 設計出來對齊（見 Open Questions）；sidecar 這一側先做一個 `ModelBroker` 介面（`request_lease`／`release`），開發與測試用假 broker。
- **agent 回報的中繼**：agent 不能自己連 Angareion（沒有 token）。sidecar 在 `$XDG_RUNTIME_DIR/sidecar.sock` 開一個 unix socket，提供一個 `send` 操作；用 SO_PEERCRED 拿呼叫方的 pid，pid 的 process tree 裡有 registry 的 agent 才接受；sidecar 用該 team 的身分把訊息送進 Angareion。agent 端用一個薄指令（`sidecar say …`）呼叫它。這樣 token 不離開 sidecar，而 team 的回報仍然是它自己的身分。

### D7 分發：MyLinuxPool 的一個 shared-config 單元

- `shared-configs/agent-sidecar/`（MyLinuxPool 的規則）：
  - `unit.json`：描述、`provides`；**不是一個 profile**——任何 profile（worker、provider、gateway）都可以把它列進 shared-configs，profile 仍是部門。
  - `install.sh`：把 sidecar 裝進 `~/.local/share/agent-sidecar/`（自帶 venv，從 MyAiStorage 的 pinned ref 安裝，不碰系統 Python）；systemd user unit 放 `~/.config/systemd/user/`；`--check` 驗：單元檔案與安裝結果一致、依賴（tmux、Python）在、設定可讀、A2A token 能讀信道（打一次 `GET`；200／304 算通，401／403 算不通，5xx 回「無法確認」）。冪等、重跑一樣。
  - `files/`：systemd unit、範例設定（不含秘密）。
  - `tests/`：照 MyLinuxPool 的測試慣例。
- **依賴**：tmux 執行檔（dotfiles 目前只帶 `tmux.conf`，worker 上的 tmux 由哪個 unit 保證要跟 MyLinuxPool 對齊）；Python 由單元自帶 venv 解決。
- 替代方案（不採）：新增一個 `agent-worker` profile——profile 是部門，sidecar 是工具，工具不該變成部門。

### D8 狀態與失敗處理

- 目錄：`~/.local/state/sidecar/`：`registry.json`（原子寫）、`queue/<identity>.json`（持久佇列，原子寫）、`agents/<id>/log`（pane 的輸出，大小上限比照 agora 的 log 慣例）、`seen.json`（已投遞的訊息 id，去重用）。
- 先寫佇列、再 ack；registry 的狀態轉移都要落檔（當掉重來能接上）。
- agent 意外死亡：waitpid 或 tmux 對照發現 → 記 exited＋exit code＋log → 佇列裡屬於它的訊息留著，下一次啟動時送。
- sidecar 自己死亡：systemd 拉起；啟動流程：讀 registry → tmux 對照 → 繼續輪詢與投遞。

### D9 v0 的界線

v0 做：收 Angareion（含輪詢與節流，用 angareion）、佇列、依 urgency 投遞、ack、啟動／終止 team、registry／status、harness 載入、分發單元。
v0 不做：外來 agent 發現、暫停／恢復、C 的記憶管理（session 選擇、壓縮、歸檔）、presence、multi-worker。

## Risks / Trade-offs

- [agent 的 TUI 行為版本相依：執行中送字可能被吃掉或需要 Ctrl-C] → 實作前先在 opencode 與 claude 各做一次 spike（tasks 2.4）；不成立時把高緊急度的機制換成「Ctrl-C 後再送」，或把 v0 的投遞降為 hand-off-only 並回報本人。
- [tmux 在 worker 上不保證存在] → 單元的 `--check` 把它當硬依賴；與 MyLinuxPool 對齊由哪個 unit 帶 tmux；不存在就不假裝健康。
- [send-keys 打到正在跑的 TUI，可能弄壞 agent 的畫面或誤觸] → 只送完整訊息加 Enter（不送控制鍵，Ctrl-C 例外且只在高緊急度、spike 後）；送出前用 `capture-pane` 做 best-effort 的狀態檢查。
- [sidecar 與 agent 的對話可能不同步（佇列已送、agent 沒收到）] → 訊息 id 去重＋at-least-once；log 留下每次送出的證據。
- [LLMGateway 的介面未定，可能改 sidecar 的啟動流程] → `ModelBroker` 介面隔離；假 broker 讓 v0 可獨立開發；對齊後只換實作。
- [A2A token 在 worker 上的保管] → 0600、只有 sidecar 讀；不進 agent 環境；`--check` 驗可讀；輪替步驟寫文件。
- [harness 依賴 #32] → seam 只要求「原文帶進第一則輸入」；#32 定案後對齊載入點，sidecar 不改寫內容。
- [殭屍行程與 tmux session 累積] → 終止流程一定關 session；啟動前清掉已 exited 的 session；`status` 看得出殘留。

## Migration Plan

1. 等本人選定範圍（A／B／C）與 LLMGateway／Atelier 對齊。
2. 先在**一台** worker 上 canary：裝單元 → 放 token → 跑一次 echo 任務（PMO 送一則 → sidecar 投遞 → team 回報）。
3. 擴大：worker profile 的 shared-configs 清單加上這個單元（改 profile 是設定，不是新 profile）。
4. 回滾：`systemctl --user disable --now agent-sidecar`；單元移除即可，不動其他單元；佇列與 registry 留著（再裝回來接得上）。

## Open Questions

- **LLMGateway 的租用協定**（申請／准許的欄位、憑證型態與效期、回報時機）：等 MyPMO 的設計出來對齊；在此之前用假 broker，不影響本 change 的 spec（spec 只要求「不持有模型金鑰、只照准許啟動」）。
- **agent 執行中送字的實測行為**（排隊／丟掉／要 Ctrl-C）：不改變 spec 的語意（≥8 立刻、<8 hand-off），但決定實作機制；用 spike 回答（tasks 2.4）。
- **tmux 執行檔由哪個 MyLinuxPool unit 保證**：與 MyLinuxPool 對齊即可，不影響本 change 的設計形狀。
- **C 的記憶介面**（session 選擇、壓縮、歸檔的具體形狀）：本人選 C 之後再展開；D4 的 home Session ＋ `--fork` 已經把介面留在 agora 這一側。
