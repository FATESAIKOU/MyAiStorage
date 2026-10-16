## Why

期 1 的 AI team（worker 上的 AI 團隊）要有一份規矩才知道怎麼接指示、在哪開 PR、怎麼回報、哪些不能碰（MyAiStorage#32）。本人裁定（MyPMO#19）：harness v0 以 Atelier 的設計為前提——先設計 Atelier，再照設計寫 v0；Atelier 是 harness 的構成宣告本身，也就是職能，和 Agora（Session 管理）是不同的東西。本 change 只做設計，**不實作**。

v0 範圍：只寫「worker team PM」一份職務，用來跑期 1 的驗收（MyLinuxPool#45 端到端）。本單的格式要讓期 3（PM／reviewer／實作隊員三職務與 Atelier 本體，MyAiStorage#17）能接上。

## What Changes

- **Harness 的結構與格式**：一份 harness 檔＝一個職務；UTF-8 Markdown ＋ YAML frontmatter（與 `docs/design.md` 的四實體 header／ref 語法對齊）；五要素寫成五個章節（know／do／judge／dont／capabilities），其中第五要素與 Angareion 的落點有三種讀法（甲／乙／丙），整理成解讀與建議交本人決定（design D1）。
- **識別與版本**：`atelier:<team>-<role>@<version>`（例 `atelier:worker-team-pm@v0`）；版本的遞增、一致性與升級規則（design D3／D4）。
- **載入**：sidecar 啟動 team 時，把 harness 檔**全文原樣**作為 agent 的第一則輸入送入；claude-code 與 opencode 行為一致；不改工作目錄；每次 `open`（含接續）載入一次（design D6／D7）。
- **與 sidecar 的介面**（獨立一節）：`open` 新增選填 `harness` 欄位與 `harness_dir` 的提案、錯誤碼、備援做法（呼叫方把全文放 `input`，sidecar 零改動）；逐條列出**要 MyLinuxPool 做的事**（design「與 sidecar 的介面」）。
- **能力需求**：本文 `capabilities` 章節（一個能力一個 H3 子節，宣告式；子節內一行 YAML／留空）＋ frontmatter 的兩個模型欄位 `models.floor`（可接受的底線模型）與 `models.preferred`（優先清單，有順序）；v0 由 worker 的預設 profile 全給（design D9）。
- **v0 職務草稿**：`harness/worker-team-pm.md`——PM 的一般規矩（派工、一張單一個 branch／worktree、審查非作者、不自己寫程式、回報、安全界線、不繞過被擋的動作）＋ worker 上的差異（用 Angareion 收指示與回報、在 worker 上開 PR、不碰憑證與真機）。
- **測試與驗收**（design D12）與**期 3 銜接**（design D13）。

## Capabilities

### New Capabilities

- `atelier`：harness 檔的格式、識別與版本、能力需求、載入、錯誤情形、v0 職務（worker team PM）的行為。

### Modified Capabilities

（無。）

## Impact

- 本 change 的產出（全部在 MyAiStorage，公開、無私人資訊）：`openspec/changes/atelier-v0/`（proposal／design／tasks／specs）、`harness/worker-team-pm.md`（職務草稿）、`docs/atelier/interface-summary.md`（一頁介面摘要，給 PMO 向本人說明）。
- 未來實作（不在本 change，見 tasks.md）：MyAiStorage＝harness 檔的正式位置與格式檢查測試；MyLinuxPool＝sidecar 的 `harness` 欄位／`harness_dir`／載入與錯誤碼、harness 檔隨映像部署、`docs/sidecar.md` 更新。
- 不含：程式碼變更；A2A／Angareion 的正式設計（以 `openspec/changes/angareion-v5/` 為準；原 MyPMO#23 已關，見 MyPMO#31）；code agent 的正式啟動機制（沿用現況，本人未定案）；期 3 的三職務與 Atelier 本體。
- 私人資訊：本 change 的內容 MUST NOT 含帳號、額度、憑證名稱、模型輪換表或第二大腦內容（公開 repo）。
