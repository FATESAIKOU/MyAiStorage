## Why

期 1 的 AI team（worker 上的 AI 團隊）要有一份規矩才知道怎麼接指示、在哪開 PR、怎麼回報、哪些不能碰（MyAiStorage#32）。本人裁定 harness v0 以 Atelier 的設計為前提：先設計 Atelier，再照設計寫 v0。r1（PR #81）之後，本人 2026-10-16 給了方向修正（經 PMO）：

- **E1**：「Angareion 是 AiStorage 的要素，因為那邊也有儲存功能，儲存的東西是 adhoc 的 AI 之間的對話。」→ 10-11 的「第五要素」講的是 **AiStorage 的要素**；harness 的五要素照 2026-10-10（know／do／judge／dont＋能力需求），**Angareion 不是 harness 的章節**。
- **E2**：harness 是 AiStorage 的一組**虛擬資料夾**，AI 用**工具**讀（類似 agora／angareion）；不是把全文塞進第一則輸入、也不在各工作目錄擺實體資料夾。
- **E5**：**暫不要求 sidecar 加欄位**；sidecar 盡量不改；載入方式重新提選項。
- **E8**：同意期 1 先由 PM 一個人做並在回報標明。

本 change 只做設計（r2），**不實作**。v0 範圍：只寫「worker team PM」一份職務，用來跑期 1 的驗收（MyLinuxPool#45 端到端）。

## What Changes

- **五要素照 2026-10-10**：know／do／judge／dont＋能力需求；`capabilities` 宣告要用 `angareion`、用法寫在 `know`；**MUST NOT 有 `angareion` 章節**（D1）。
- **存放**：harness 樹＝AiStorage 的虛擬資料夾（建議：MyAiStorage 的 git，`atelier/<team>/<role>/v<N>.md`）；**版本檔 append-only、不可變**；寫入只有 PR（審查＋本人同意）；agent 工作時 MUST NOT 改 harness（D3／D4／D6）。
- **定址**：`atelier:<team>-<role>@<vN>`（對上 `docs/design.md` 的 ref 寫法）；識別↔路徑↔frontmatter 三者一致（D3）。
- **讀取工具**：新增 `atelier` 指令（與 agora／angareion 同一個 distribution）——`atelier ls`、`atelier show <id>[@版本] [--section …] [--json]`；只讀、匿名、讀本機複本＋`git fetch`（沿用 angareion 的讀法）、exit 0／1／2（D7／D10）。
- **載入**：固定開場句放在 sidecar `open` 的 `input` 開頭——先讀 harness、再讀信道（與 MyPMO#33 的派工流程合成一句）；**sidecar 零改動**；r1 的「sidecar 載入全文＋`open.harness` 欄位」降為未選選項（D8；「與 sidecar 的介面」）。
- **格式（r1 沿用）**：UTF-8 Markdown＋OKF frontmatter（`type/id/title/status/team/role/version/models`）；本文五章；`capabilities` 以能力名為 H3 子節；本人的 `models.floor`／`models.preferred` 兩個欄位（D2／D9）。
- **v0 職務草稿更新**：加「先讀自己的 harness、收到新指示前重讀」；其餘照 r1（worker 差異、單人暫定、公開性）（D11）。
- **測試與驗收**：工具測試、格式檢查（`atelier/**`）、期 1 端到端要看什麼（D12）；**與期 3 銜接**（D13）。

## Capabilities

### New Capabilities

- `atelier`：harness 檔的格式、識別／版本／路徑、虛擬資料夾樹與不可變、讀取工具（`atelier`）的行為、載入（固定開場句）、v0 職務（worker team PM）的行為。

### Modified Capabilities

（無。）

## Impact

- 本 change 的產出（全部在 MyAiStorage，公開、無私人資訊）：`openspec/changes/atelier-v0/`（proposal／design／tasks／specs）、`harness/worker-team-pm.md`（職務草稿）、`docs/atelier/interface-summary.md`（一頁介面摘要）。
- 未來實作（不在本 change，見 tasks.md）：MyAiStorage＝`src/atelier/` 讀取工具＋`pyproject.toml` entry point＋`atelier/` 樹第一份職務檔＋`tests/`；MyLinuxPool＝只把 worker 的 `agora` 套件 pin 升到含 `atelier` 的 sha（既有流程；不新增能力、不改 sidecar）。
- 不含：程式碼變更；sidecar 的 `harness` 欄位（本人暫不要求；保留為未選選項）；A2A／Angareion 的正式設計（以 `openspec/changes/angareion-v5/` 為準；原 MyPMO#23 已關，見 MyPMO#31）；code agent 的正式啟動機制（沿用現況，本人未定案）；期 3 的三職務與 Atelier 本體。
- 私人資訊：本 change 的內容 MUST NOT 含帳號、額度、憑證名稱、模型輪換表或第二大腦內容（公開 repo）。
