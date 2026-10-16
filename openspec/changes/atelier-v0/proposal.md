## Why

期 1 的 AI team（worker 上的 AI 團隊）要有一份規矩才知道怎麼接指示、在哪開 PR、怎麼回報、哪些不能碰（MyAiStorage#32）。本人裁定 harness v0 以 Atelier 的設計為前提（先設計 Atelier，再照設計寫 v0），並在 2026-10-16 看過 r2 後給定方向（經 PMO）：

- **E1**：Angareion 是 **AiStorage 的要素**（存 adhoc 的 AI 對話），**不是 harness 的章節**；harness 的五要素照 2026-10-10（know／do／judge／dont＋能力需求）。
- **E2**：harness 是 AiStorage 的一組**虛擬資料夾**，AI 用**工具**讀；不是把全文塞進第一則輸入、也不在各工作目錄擺實體資料夾。
- **E5**：暫不要求 sidecar 加欄位；sidecar 盡量不改。
- **E8**：同意期 1 先由 PM 一個人做並在回報標明。
- **E9／E10／E4（照建議採納）**：harness 放 MyAiStorage 的 git（`atelier/<team>/<role>/v<N>.md`、版本只增不改）；新增 `atelier` 指令（`ls`／`show`，只讀）；`models` 先留空、只留欄位。
- **E11（改方向）**：「我希望不要讓 AI 作業時還要特別打 prompt，用更加強制的方式吧……（但要考慮專案覆蓋問題）」——不要靠固定開場句；要用 **agent 一定會載入**的機制，但不能蓋掉或弄髒目標 repo 自己的 `AGENTS.md`／`CLAUDE.md`。

本 change 只做設計（r3），**不實作**。v0 範圍：只寫「worker team PM」一份職務，用來跑期 1 的驗收（MyLinuxPool#45 端到端）。

## What Changes

- **載入（r3 的主題）**：新增 **`atelier install <id> --agent claude|opencode`（可重複；每個值各裝一次；不給＝用法錯）**——把 harness 寫進 agent 的**使用者層級指示檔**（claude：`~/.claude/rules/atelier-*.md` 或 `CLAUDE.md` 的管理區塊；opencode：`~/.config/opencode/AGENTS.md` 的管理區塊），agent 啟動時自動載入（claude：新的 conversation 一定，接續由驗收確認）；管理區塊冪等、**不碰專案檔／工作目錄／`~/.claude/settings.json`**；期 1 由 MyLinuxPool#130 的開機掛點（start.d）跑 install；**sidecar 零改動**（D8）。選項與建議：使用者層級指示檔＋開機 install（v0）＋每 agent 一份設定目錄（期 3）。
- **五要素照 2026-10-10**：know／do／judge／dont＋能力需求；`capabilities` 宣告要用 `angareion`、用法寫在 `know`；**MUST NOT 有 `angareion` 章節**；harness MUST 明寫「專案自己的指示檔與工單規則優先」（D1／D2）。
- **存放與版本**：harness 樹＝MyAiStorage 的 git（`atelier/<team>/<role>/v<N>.md`）；版本檔 append-only、不可變；寫入只有 PR；agent 工作時 MUST NOT 改 harness（D3／D4／D6）。
- **讀取工具**：`atelier ls`／`show <id>[@版本] [--section …] [--json]`；只讀、匿名、讀本機複本＋`git fetch`（沿用 angareion 的同一複本路徑、flock、20 秒逾時）；exit 0／1／2（D7／D10）。
- **格式（r1 沿用）**：UTF-8 Markdown＋OKF frontmatter（`type/id/title/status/team/role/version/models`）；本文五章；`capabilities` 以能力名為 H3 子節；`models.floor`／`models.preferred` 先留空（D2／D9）。
- **第一則輸入**：不再負責載入 harness；保留 MyPMO#33 的派工句（「指示在 Angareion…」）即可，「先讀 harness」半句是可選的（「與 sidecar 的介面」）。
- **v0 職務草稿更新**：harness 自動載入、專案指示優先、要重讀時用 `atelier show`；其餘照 r1（worker 差異、單人暫定、公開性）（D11）。
- **測試與驗收**：install／載入、格式檢查、期 1 端到端（D12）。

## Capabilities

### New Capabilities

- `atelier`：harness 檔的格式、識別／版本／路徑、虛擬資料夾樹與不可變、讀取工具（`ls`／`show`）、**安裝（`atelier install`）**、v0 職務（worker team PM）的行為。

### Modified Capabilities

（無。）

## Impact

- 本 change 的產出（全部在 MyAiStorage，公開、無私人資訊）：`openspec/changes/atelier-v0/`（proposal／design／tasks／specs）、`harness/worker-team-pm.md`（職務草稿）、`docs/atelier/interface-summary.md`（一頁介面摘要）。
- 未來實作（不在本 change，見 tasks.md）：MyAiStorage＝`src/atelier/`（`ls`／`show`／`install`）＋`pyproject.toml` entry point＋`atelier/` 樹第一份職務檔＋`tests/`；MyLinuxPool＝開機掛點的 atelier 單位（跑 install）＋套件 pin 升級（既有流程；sidecar 不改）。
- 跨 repo（PMO 帶）：MyPMO#33 的第一則輸入可以去掉「先讀 harness」半句；E6（誰開 team、怎麼呼叫 open）由 #33 處理（PR #66）。
- 不含：程式碼變更；sidecar 的 `harness` 欄位（未選保留）；A2A／Angareion 的正式設計（angareion-v5；原 MyPMO#23 已關）；code agent 的正式啟動機制；期 3 的三職務與 Atelier 本體。
- 私人資訊：本 change 的內容 MUST NOT 含帳號、額度、憑證名稱、模型輪換表或第二大腦內容（公開 repo）。
