## 交本人選的問題

sidecar 的範圍有三種切法，**請本人選定之後才實作**；下面是利弊對照與 arch 的建議，只是參考、不是替你選。

| | **A：通訊＋心跳** | **B：A＋啟動與關閉** | **C：B＋記憶** |
|---|---|---|---|
| 做什麼 | 只收 Angareion、投遞、agent 心跳與狀態 | 再加：用 `agora continue` 啟動、終止、匯出 agent，用 herdr 送字 | 再加：管 agora Session 的選擇、載入、存回、fork、壓縮與歸檔 |
| 緊急打斷 | 做不到（不控 process，只能等投遞點） | 做得到（sidecar 透過 herdr 送字） | 做得到 |
| team 誰啟動 | 人自己（attach 進 herdr 手動開）；sidecar 不管 | sidecar | sidecar |
| 對 #31 v0 的影響 | 與 v0 已定的「啟動、終止 team」衝突，要改 v0 | 就是 v0 | 超過 v0 |
| 誰拿 A2A token | 收發的人各自拿；要每個 agent 都給，收窄不了 | sidecar 獨拿；agent 經中繼回報 | 同 B |
| 誰拿模型憑證 | LLMGateway（sidecar 不碰） | LLMGateway 發准許，sidecar 照准許啟動、回報 | 同 B |
| 與 LLMGateway 怎麼溝通 | 幾乎無關（不啟動 agent） | sidecar 申請／回報；LLMGateway 記錄 worker、角色、coding agent、模型 | 同 B（記憶不進 LLMGateway） |
| 成本／風險 | 最小；流程要人手接、打斷做不到 | 中；herdr 操作與 agent 版本的差異、殭屍行程 | 大；與 agora 深度耦合，要先有 B 穩定 |
| arch 的建議 | | **建議先做這個**（等於 #31 的 v0；把 C 的介面留好） | 等 B 穩定後再擴 |

選 A 的話 #31 的 v0 要改寫；選 B 就是 issue 已定的 v0；選 C 會把期 1 拉長。以下設計以「B 的架構、A 的介面留白、C 的介面留白」寫成，讓三種選擇都接得上。

### 誰呼叫 agora、憑證在誰手上（A／B／C）

設計裡有一個先前沒寫明的假設要先講清楚：**B／C 是「只有 sidecar 呼叫 agora，agent 不碰」**。三種切法的答案不一樣：

| | A：通訊＋心跳 | B：A＋啟動關閉（建議） | C：B＋記憶 |
|---|---|---|---|
| 誰呼叫 agora（continue／import／search） | 人與 agent 自己；sidecar 不呼叫 | **只有 sidecar**：啟動、匯出、寫回都它做 | 同 B（再加它決定載入哪個 Session） |
| Drive 憑證（agora 的 `~/.config/agora/rclone.conf`） | worker 使用者的 HOME；要用的人／agent 自己要有 | 同一個 HOME；sidecar 用它。agent 是 sidecar 開的子程序、繼承同一個 HOME——**技術上讀得到，但設計的邊界是 agent 不碰**（v0 不做硬隔離） | 同 B |
| agent 想找舊 Session | agent 自己 `agora search`／`continue` | 經 sidecar 中繼（v0 只有「送訊息」的中繼；查詢介面留白；agent 自己跑 agora 不是支援路徑） | sidecar 依任務把記憶載進來，agent 不自己找 |
| agent 想自己存檔 | agent 自己 `agora import` | 不需要：`continue` 的定期寫回／結束寫回負責；agent 正常結束就好 | 同 B（sidecar 另外決定歸檔／壓縮） |
| 人 attach 進 worker、手動開 agent | 和 agent 一樣，自己有 rclone.conf 就能跑 agora | 那是「不是 sidecar 啟動的 agent」：v0 sidecar 不管；人自己用 agora（同一份 HOME 憑證），對話要人自己 `agora import`（continue 開的除外） | 同 B |

- 一句話總結：**A 憑證四散、人自己接；B／C 的憑證集中在 sidecar，agent 只經中繼說話**；三種切法的 rclone.conf 都在 worker 使用者的 HOME，本質上是同一個信任邊界，硬隔離（不同 user／容器）留到期後。
- A2A token 的歸屬見上面那張表（A 是收發的人各自拿、B／C 只給 sidecar）；它和 agora 的 Drive 憑證是兩種不同的憑證，不要混在一起。

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

### D2 agent 宿主：herdr（本人選定）

- agent 不是 sidecar 的直接子程序，而是跑在 **herdr** 裡（MyLinuxPool#47 把 herdr 裝進 worker 映像；和 Mac 上用同一套）。sidecar 用 herdr 的指令啟動、送字、讀狀態：
  - **開**：`herdr tab create --cwd <工作目錄>`（需要獨立 checkout 的任務先用 `herdr worktree create`），再 `herdr pane run <pane> "agora continue …"` 把 agent 跑在那個 pane；偵測到 agent 後用 `herdr agent rename <pane> <name>` 命名（名字規則 `[a-z][a-z0-9_-]{0,31}`，agent 結束時清掉）。
  - **送字**：`herdr agent prompt <name> <text>`（原子地送字＋Enter，會處理 bracketed paste）。
  - **讀狀態／畫面**：`herdr agent get`（idle／working／blocked／done／unknown）與 `herdr agent read <name> --source recent-unwrapped --lines N`；等輸出用 `herdr pane wait-output`。
- 這樣做的理由：
  - **重啟接得回來**：herdr server 常駐；sidecar 重啟後用 `herdr agent list`／`herdr pane list` 對照 registry 找回還在跑的 agent——in-process 的 pty 做不到這件事。
  - **和 Mac 同一套**：命令與人在 Mac 上的操作一樣；人要去看就用 `herdr session attach`，不必另學一個工具。
  - **送字是 herdr 的本業**，不必自己寫 pty 的重連與視窗大小處理。
- 替代方案（不採）：in-process pty（簡單，但重啟即失去控制）；tmux（本人選了 herdr，worker 映像裝的也是 herdr）；不控 process、只讀 log（做不到投遞）。

### D2a herdr 在 worker 上的注意事項

- **server 怎麼起、誰擁有**：worker 上跑一個 headless 的 herdr server（`herdr server`，detached daemon；`herdr status server --json` 可查狀態與 socket），以 worker 使用者身分、由 systemd user unit 拉起（與 MyLinuxPool#47 對齊誰負責）；sidecar 只是同一個 session 的用戶端。人在 Mac 上用 `herdr --remote <worker>`／`herdr session attach` 接同一個 server 看。
- **不能在 herdr 裡再開 herdr（巢狀）**：sidecar 由 systemd 起、不在任何 pane 裡；環境 MUST NOT 帶 `HERDR_ENV`／pane 變數（偵測到就清掉或拒絕啟動），也 MUST NOT 執行不帶子命令的 `herdr`（那會開／接 TUI）。所有操作都走子命令、讀 JSON。
- **重啟後找回 agent**：registry 記 pane id 與 agent 名；sidecar 重啟後用 `herdr agent list` 與 `herdr pane get`／`pane process-info` 對照——名字還在就接回，名字被清掉（agent 結束、被取代或釋放）就記 exited。
- **權限旗標由 sidecar 決定**：opencode 的 `OPENCODE_PERMISSION`、claude 的權限模式（permission mode／allowlist）由 sidecar 依 team 的設定與 harness 的能力需求在啟動時給定（`herdr tab create --env` 或 `pane run` 的命令環境），agent MUST NOT 自己改；v0 預設最小權限，實際值與 Atelier（#32）對齊。
- **herdr 的 agent 狀態對 opencode 不可靠**：`agent get` 的 idle／working 只是參考；判斷 opencode 還在跑要看畫面最下面一行有沒有「esc interrupt」（`agent read`）。claude 的狀態較可靠，但仍以畫面為最後依據。
- **`agent prompt` 的等待語意**：從非 working 狀態送出後 5 秒內沒觀察到狀態變化會回 `agent_prompt_stalled`；投遞不使用 `--wait`（那是「送了等它做完」的用途），等狀態另外用 `agent wait`／`pane wait-output`。
- **測試接縫**：sidecar 把 herdr 包成一個薄 client（開 agent／送字／讀狀態／收拾 pane），單元測試注入假 client；真的 herdr 只在 worker 的端到端測試跑。

### D3 投遞政策：urgency 兩段、ack 在投遞後

- `urgency` ≥ 8：**立刻**送進 agent 的輸入（`herdr agent prompt <name> <text>`，成為它下一輪的 user turn）。agent 正在跑一輪時，先把訊息送進去讓它排隊；**若實測 agent 不排隊而是吃掉/丟掉**，退回「先用 `herdr agent send-keys <name> ctrl+c` 讓它收掉這一輪、回到提示再送」——這條用 spike 在 opencode 與 claude 各驗一次，結果寫回 design（見 tasks 2.4）。
- `urgency` < 8：進持久佇列；在下一次 hand-off 送出。v0 保證的 hand-off ＝ agent 程序結束後、下一次啟動時。做法：佇列裡的訊息排序好，agent 一啟動（herdr 偵測到、prompt 出現）就用 `herdr agent prompt` 依序送出。
- 同一批依 `urgency` 高到低、同 `urgency` 依 `at` 舊到新。
- **投遞成功才 ack**：訊息進了 agent 的輸入才 `a2a ack`。語意是「已交付」，不是「已完成」；完成的訊號是 team 後來的回報訊息。
- at-least-once：sidecar 在投遞與 ack 之間當掉，訊息會再投一次；用訊息 `id` 去重，MUST NOT 重複送出。

### D4 啟動與終止（B 的核心）

- **啟動**：每個 team 有一個 agora 的 **home Session**（團隊的長期記憶，例如「<team> 的收件匣」）。一個任務 `agora continue session <home> --agent <agent> --fork --sync-period <N>`（#29、#28 的介面；依賴它們 merge）：
  - `--fork`：任務之間隔離，每個任務一個新 Session，home 不被改動；
  - `--sync-period`：agent 跑很久時 Drive 上的版本看得到進度（預設 5 分鐘）。
- **harness 載入**（#32 的 seam）：agent 起來（用 `pane wait-output`／`agent wait` 判斷 prompt 出現）後，sidecar 把 harness 全文當第一則輸入送進去（`agent prompt`；或依 #32 最後定的載入點）；sidecar 不解讀內容。
- **終止**：先 `herdr agent send-keys <name> ctrl+c`（讓 agent 收尾，等同互動模式的 SIGINT）；等 5 秒還在，對它的 process group 送 SIGTERM、再 5 秒 SIGKILL（pid 由 `herdr pane process-info` 取得）；最後手段才 `herdr pane close`（pane 是 sidecar 開的）。收尾都記進 registry 與 log。
- **匯出**：`agora continue` 啟動的對話由 continue 自己寫回；如果是別的方式啟動的 agent（例如人手開的，A 的世界），結束後用 `agora import session --external-session-id <id> --agent <agent>` 匯出。sidecar MUST NOT 自己拼 agent 的原始檔。
- **暫停／恢復**（設計裡保留、v0 不必做）：SIGSTOP／SIGCONT 對 process group；注意停在工具呼叫中間可能讓 TUI 顯示錯亂，文件要寫。

### D5 登錄與發現

- v0：**只認自己啟動的 agent**。registry 檔（`~/.local/state/sidecar/registry.json`）記：agent id、角色、agora Session、agent 種類、herdr tab／pane id、agent 名、啟動時間、狀態（starting／running／exited／terminated）、log 位置。
- sidecar 重啟：讀 registry，用 `herdr agent list` 與 `herdr pane get`／`pane process-info` 對照，還在的標 running、不在的標 exited（並把結束的證據記下來）。
- **發現外來 agent**（v0 不做，介面留白）：未來用一個 register 介面（unix socket，agent 自己報到）或掃 process；設計上把「agent 清單」抽成一個 provider，registry 只是其中一種來源。
- **心跳**：v0 的心跳是本機 registry 的 `updated_at`＋`sidecar status --json`；**不為心跳發 A2A 訊息**（省額度）。要跨機器看狀態是期 2 的 presence，或人去看 log／請 team 回報。

### D6 憑證邊界（與 LLMGateway、本人）

| 憑證 | 誰設定 | 誰持有 | 備註 |
|---|---|---|---|
| A2A token（#30） | 本人（capability） | **只有 sidecar** | 0600；agent 環境與檔案裡沒有；agent 回報經中繼 |
| 模型存取 | LLMGateway（本人設定的上游） | LLMGateway；agent 拿「這次准許」對應的存取方式 | sidecar 不持有、不讀、不寫模型金鑰 |
| worker 的 git 憑證（開 PR） | 本人（capability，worker 既有） | agent 的環境（照 worker 的既有方式） | sidecar 不管、不複製、不記 log；v0 不動它 |
| Angareion 的信道 repo | 本人 | 只有 sidecar（同上） | agent MUST NOT 有**信道 repo** 的存取；它開 PR 用的是 worker 既有的 git 憑證（上一列），兩種憑證互不相干 |

- **sidecar ↔ LLMGateway 的溝通**（B／C）：啟動前 sidecar POST 一個「開 agent」申請（worker、角色、coding agent、模型）→ gateway 回准許（含 agent 要用的模型存取方式與效期）或拒絕 → sidecar 照回覆設定 agent 環境並啟動 → 結束／終止後回報。協定細節等 MyPMO 的 LLMGateway 設計出來對齊（見 Open Questions）；sidecar 這一側先做一個 `ModelBroker` 介面（`request_lease`／`release`），開發與測試用假 broker。
- **agent 回報的中繼**：agent 不能自己連 Angareion（沒有 token）。sidecar 在 `$XDG_RUNTIME_DIR/sidecar.sock` 開一個 unix socket，提供一個 `send` 操作；用 SO_PEERCRED 拿呼叫方的 pid，pid 的 process tree 裡有 registry 的 agent 才接受；sidecar 用該 team 的身分把訊息送進 Angareion。agent 端用一個薄指令（`sidecar say …`）呼叫它。這樣 token 不離開 sidecar，而 team 的回報仍然是它自己的身分。

### D7 分發：MyLinuxPool 的一個 shared-config 單元

- `shared-configs/agent-sidecar/`（MyLinuxPool 的規則）：
  - `unit.json`：描述、`provides`；**不是一個 profile**——任何 profile（worker、provider、gateway）都可以把它列進 shared-configs，profile 仍是部門。
  - `install.sh`：把 sidecar 裝進 `~/.local/share/agent-sidecar/`（自帶 venv，從 MyAiStorage 的 pinned ref 安裝，不碰系統 Python）；systemd user unit 放 `~/.config/systemd/user/`；`--check` 驗：單元檔案與安裝結果一致、依賴（herdr、Python）在、herdr server 可達（`herdr status server --json`）、設定可讀、A2A token 能讀信道（打一次 `GET`；200／304 算通，401／403 算不通，5xx 回「無法確認」）。冪等、重跑一樣。
  - `files/`：systemd unit、範例設定（不含秘密）。
  - `tests/`：照 MyLinuxPool 的測試慣例。
- **依賴**：herdr 由 worker 映像提供（MyLinuxPool#47），單元不自己裝；herdr server 由哪個 unit 拉起、用哪個 session 名，與 MyLinuxPool#47 對齊；Python 由單元自帶 venv 解決。
- 替代方案（不採）：新增一個 `agent-worker` profile——profile 是部門，sidecar 是工具，工具不該變成部門。

### D8 狀態與失敗處理

- 目錄：`~/.local/state/sidecar/`：`registry.json`（原子寫）、`queue/<identity>.json`（持久佇列，原子寫）、`agents/<id>/log`（pane 的輸出，大小上限比照 agora 的 log 慣例）、`seen.json`（已投遞的訊息 id，去重用）。
- 先寫佇列、再 ack；registry 的狀態轉移都要落檔（當掉重來能接上）。
- agent 意外死亡：用 `herdr agent list`（名字消失）與 `herdr pane get` 對照發現 → 記 exited＋log → 佇列裡屬於它的訊息留著，下一次啟動時送。
- sidecar 自己死亡：systemd 拉起；啟動流程：讀 registry → `herdr agent list` 對照 → 繼續輪詢與投遞。

### D9 v0 的界線

v0 做：收 Angareion（含輪詢與節流，用 angareion）、佇列、依 urgency 投遞、ack、啟動／終止 team、registry／status、harness 載入、分發單元。
v0 不做：外來 agent 發現、暫停／恢復、C 的記憶管理（session 選擇、壓縮、歸檔）、presence、multi-worker。

## Risks / Trade-offs

- [agent 的 TUI 行為版本相依：執行中送字可能被吃掉或需要 Ctrl-C] → 實作前先在 opencode 與 claude 各做一次 spike（tasks 2.4）；不成立時把高緊急度的機制換成「Ctrl-C 後再送」，或把 v0 的投遞降為 hand-off-only 並回報本人。
- [worker 上的 herdr server 起不來或版本不合] → 單元的 `--check` 驗 herdr 在、`herdr status server --json` 可達；server 由誰拉起與 MyLinuxPool#47 對齊；不健康就不啟動服務。
- [送字打到正在跑的 TUI，可能弄壞 agent 的畫面或誤觸] → 用 `herdr agent prompt`（原子送字＋Enter，不送控制鍵；ctrl+c 例外、只在高緊急度且 spike 後）；送出前用 `agent get`／`agent read` 的畫面做 best-effort 狀態檢查（opencode 看 esc interrupt）。
- [sidecar 與 agent 的對話可能不同步（佇列已送、agent 沒收到）] → 訊息 id 去重＋at-least-once；log 留下每次送出的證據。
- [LLMGateway 的介面未定，可能改 sidecar 的啟動流程] → `ModelBroker` 介面隔離；假 broker 讓 v0 可獨立開發；對齊後只換實作。
- [A2A token 在 worker 上的保管] → 0600、只有 sidecar 讀；不進 agent 環境；`--check` 驗可讀；輪替步驟寫文件。
- [harness 依賴 #32] → seam 只要求「原文帶進第一則輸入」；#32 定案後對齊載入點，sidecar 不改寫內容。
- [殭屍行程與開著的 tab／pane 累積] → 終止流程一定收拾 pane（必要時 `herdr pane close`）；啟動前清掉已 exited 的 tab；`status` 看得出殘留。

## Migration Plan

1. 等本人選定範圍（A／B／C）與 LLMGateway／Atelier 對齊。
2. 先在**一台** worker 上 canary：裝單元 → 放 token → 跑一次 echo 任務（PMO 送一則 → sidecar 投遞 → team 回報）。
3. 擴大：worker profile 的 shared-configs 清單加上這個單元（改 profile 是設定，不是新 profile）。
4. 回滾：`systemctl --user disable --now agent-sidecar`；單元移除即可，不動其他單元；佇列與 registry 留著（再裝回來接得上）。

## Open Questions

- **LLMGateway 的租用協定**（申請／准許的欄位、憑證型態與效期、回報時機）：等 MyPMO 的設計出來對齊；在此之前用假 broker，不影響本 change 的 spec（spec 只要求「不持有模型金鑰、只照准許啟動」）。
- **agent 執行中送字的實測行為**（排隊／丟掉／要 Ctrl-C）：不改變 spec 的語意（≥8 立刻、<8 hand-off），但決定實作機制；用 spike 回答（tasks 2.4）。
- **worker 上 herdr server 由誰拉起、session 怎麼命名、herdr 版本怎麼釘**：與 MyLinuxPool#47 對齊即可，不影響本 change 的設計形狀。
- **C 的記憶介面**（session 選擇、壓縮、歸檔的具體形狀）：本人選 C 之後再展開；D4 的 home Session ＋ `--fork` 已經把介面留在 agora 這一側。
