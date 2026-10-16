## Context

本 change 是 #32 的設計，r3（2026-10-16）：本人看過 r2 後裁定了 E9／E10／E4 照建議，**E11 改方向**：不要靠「固定開場句叫 agent 自己去讀」，要用**agent 一定會載入的機制**；但不能蓋掉、也不能弄髒目標 repo 自己的 `AGENTS.md`／`CLAUDE.md`（本人原話：「我希望不要讓 AI 作業時還要特別打 prompt，用更加強制性的方式吧，比如放資料夾或者 AGENTS.md？（但要考慮專案覆蓋問題，所以你需要注意）」）。

**本人已答覆（2026-10-16，經 PMO）**：

- **E1**：harness 五要素照 2026-10-10（know／do／judge／dont＋能力需求）；「A2A 信道（Angareion）」是 **AiStorage 的要素**（存 adhoc 的 AI 對話）、**不是 harness 的章節**——在 `capabilities` 宣告、用法寫在 `know`。
- **E2**：harness 是 AiStorage 裡的一組**虛擬資料夾**（一個團隊／職務一組），AI 用**工具／指令**讀；不是把全文塞進第一則輸入、也不在各工作目錄擺實體資料夾。
- **E5**：**暫不要求 sidecar 加 `harness` 欄位**；sidecar 盡量不改。
- **E8**：同意期 1 先由 PM 一個人做並在回報標明。
- **E9（r2 建議採納）**：harness 存在 **MyAiStorage 的 git**：`atelier/<team>/<role>/v<N>.md`，版本只增不改。
- **E10（r2 建議採納）**：新增 **`atelier` 指令**（`ls`／`show`，只讀）。
- **E4（r2 建議採納）**：`models` 先留空、只留欄位。
- **E11（改方向）**：載入不靠開場句；用 agent 一定會載入的機制（D8）。

**r3 的查證：兩種 agent 的「一定會載入」機制**（2026-10-16；來源逐一標注）

> 版本註：**以 worker 實機釘的版本為準**。本機查證版本：claude 2.1.301、opencode 2.0.29；worker 目前釘 claude 2.1.296／opencode 2.0.26（同一個世代，但實作與驗收時要按 worker 的實際版本重驗）。

Claude Code（本機 2.1.301）：

- **使用者層級記憶檔 `~/.claude/CLAUDE.md`** 在每次 conversation 開始時載入；載入順序由廣到窄：managed（`/etc/claude-code/CLAUDE.md`）→ user（`~/.claude/CLAUDE.md`）→ project（`./CLAUDE.md` 或 `./.claude/CLAUDE.md`）→ local（`./CLAUDE.local.md`）；**全部串接、不是覆蓋**；衝突時模型可能任選一邊（來源：官方 memory 文件）。使用者層級另有 `~/.claude/rules/*.md`（在 project rules 之前載入，套用到每台機器上的每個專案）。（來源：https://code.claude.com/docs/en/memory。）
- 專案的 `AGENTS.md` 也會被當成 project instructions 讀（v2.1.277 以後；沒有 CLAUDE.md 時預設取代、可用設定改成兩者都讀或只讀 CLAUDE.md）——**這是 repo 自己的檔，不是我們要碰的**。（同上。）
- **`--append-system-prompt-file <path>` 存在**（本機 `claude --help` 有 `--append-system-prompt`；`--append-system-prompt-file` 不在 help 列表但可用——本機實測 `claude --append-system-prompt-file /tmp/nope.md -p hi` 回 `Error: Append system prompt file not found: /tmp/nope.md`，代表旗標存在）。**但預設的系統提示只在對話的第一次請求組一次並記錄在 session 裡，之後（含 `--resume`）沿用**——要每次重建得加 `--system-prompt-snapshot off`（官方 CLI reference「System prompt flags in resumed conversations」）。
- 環境變數 **`CLAUDE_CONFIG_DIR`** 存在（本機 binary 字串；官方 env-vars 文件：「For a variable that takes a path, such as `CLAUDE_CONFIG_DIR`, write the absolute path」；project／local settings 不能設它）。
- `agora continue` 起 claude 的指令：headless 是 `claude -p --input-format stream-json --output-format stream-json --verbose --resume <id> --settings '{"autoContinueAtUsageLimit": false}'`（`src/agora/agents/claude.py` 的 `start_native`）；互動是 `claude --resume <id>`。`AGORA_CLAUDE_CMD` 是正式介面（換執行檔；值當第一個 argv）。

opencode（本機 2.0.29；隔離環境實測——HOME／XDG 指到暫存、`opencode service set port` 改私有埠、假 upstream 只在本機 127.0.0.1、不碰 49374 與真資料）：

- **全域 `~/.config/opencode/AGENTS.md` 會被載入**：實測送給模型的 system message 末尾出現 `Instructions from: <XDG_CONFIG_HOME>/opencode/AGENTS.md` 加內容；**專案 `AGENTS.md` 也一起載入**（同一段後面再接 `Instructions from: <專案>/AGENTS.md`）——**兩者合併、不是取代**。（另有官方 v2 instructions 文件：載入順序＝全域檔→從 workspace 往專案根逐層；「OpenCode combines the files and does not resolve conflicts between them」。https://opencode.ai/v2/docs/instructions/。）
- **改版生效**：官方 v2 文件說，改全域或上層的 `AGENTS.md` 時，「Before the next model request, OpenCode detects the change and adds an instruction update」——同一個 session 的下一個請求前就會更新（nested 的檔案才要新 session）。（同上。）
- **v2 只認 `AGENTS.md`，不用 `CLAUDE.md` 當 fallback**（官方 v2 文件的 Callout；v1 文件還說有 Claude Code 相容，v2 沒有——本機實測也只載入 AGENTS.md）。
- `instructions`（config 欄位）：官方文件說會與 AGENTS.md 一起載入（相對路徑從 workspace 往專案根找、`~/` 從 home、絕對路徑照寫、http(s) 五秒逾時）。**本機 v2.0.29 實測沒有生效**：全域絕對路徑、全域相對路徑、專案相對路徑三種寫法都沒有出現在送給模型的任何訊息裡（`opencode debug config` 有列出那個 config 與 `instructions` 的值）。**設計不依賴它**；留作擴充點（之後版本修好再驗）。
- 環境變數（本機 binary 字串）有 `OPENCODE_CONFIG`、`OPENCODE_CONFIG_CONTENT`、`OPENCODE_CONFIG_DIR`、`OPENCODE_DISABLE_PROJECT_CONFIG` 等；`OPENCODE_CONFIG_DIR` 的官方說明是「custom config directory… searched for agents, commands, modes, and plugins」（不涵蓋全域 AGENTS.md）。
- `opencode reload`（本機 `--help` 有）＝叫執行中的 server 重讀設定。

**r1／r2 仍然有效的素材**：四實體 header／ref 約定（`docs/design.md` 第 39、61-66 行）；angareion §11 的讀法（本機複本＋`git fetch`／`git show origin/main:`、匿名 HTTPS、20 秒逾時、flock——`openspec/changes/angareion-v5/design.md` 第 101 行）；sidecar v5 的 `open.input`（MyLinuxPool `modules/sidecar/docs/sidecar.md` 第 103 行）；本人 2026-10-11 的 `models.floor`／`preferred` 兩欄（MyAiStorage#32 留言）；PM 一般規矩（MyPMO handbook，private；用自己的話）。

**約束**：本單只做設計、不實作；不碰 MyLinuxPool（只讀）；不碰真資料／49374／憑證（驗證只用隔離環境）；公開 repo 不寫私人資訊。

## Goals / Non-Goals

**Goals：**

- **載入（r3 的主題）**：agent 一定會載入 harness 的機制——由 `atelier install` 把 harness 裝進 agent 的**使用者層級**指示檔（不碰專案檔、不弄髒工作樹）；選項與建議（D8）、查證事實（Context）、期 1 的最小做法。
- 定 **`atelier install`** 的介面與行為（冪等、管理區塊、錯誤情形）（D8／D10）。
- 沿用 r2：**存放**（MyAiStorage 的 git、`atelier/<team>/<role>/v<N>.md`、append-only）、**識別與版本**、**讀取工具**（`ls`／`show`）、**格式**（frontmatter＋五章＋`capabilities` H3 子節）、**能力需求**（`models` 欄位）。
- **開場句**與 **MyPMO#33 的銜接**：第一則輸入不再負責載入 harness；寫出剩下的派工句與位置（「與 sidecar 的介面」）。
- 定**測試與驗收**（安裝與載入要看到什麼）與**期 3 銜接**（多職務）。
- 把**要本人決定的事**編號列好（附建議）；已答覆的標「本人已答覆（2026-10-16）」。

**Non-Goals：**

- 實作（工具、install、harness 樹、start.d 單位、部署都只列步驟）。
- sidecar 的載入欄位（本人暫不要求；保留為未選）。
- A2A／Angareion 的正式設計（angareion-v5；原 MyPMO#23 已關）；Atelier 本體與三職務（期 3）。
- code agent 的正式啟動機制；worker 上「誰開 team、怎麼呼叫 open」（**由 MyPMO#33 處理**，見 D14）。
- 模型輪換政策本身（E4：只留欄位）。
- 秘書 harness（不搬，留在 MyAiEntry App）。

## Decisions

總覽：

| # | 決策 | 狀態 |
|---|---|---|
| D1 | 五要素＝know／do／judge／dont＋能力需求；Angareion 是 AiStorage 的要素、不是 harness 章節 | **本人已答覆（E1）** |
| D2 | 一份檔＝一個職務一個版本；frontmatter（OKF）＋五章；`capabilities` 是 H3 子節 | 定（r1） |
| D3 | 識別、命名與路徑：`atelier:<team>-<role>@<vN>` ↔ `atelier/<team>/<role>/v<N>.md` | 定（隊名 E7） |
| D4 | 版本：append-only 版本檔；舊版不可變、不刪；latest＝最大 N | 定 |
| D5 | 與 Agora 的關係：不混 | 定 |
| D6 | 存放位置：MyAiStorage 的 git | **本人已答覆（E9）** |
| D7 | 讀取工具：`atelier` 指令（`ls`／`show`） | **本人已答覆（E10）** |
| D8 | 載入：`atelier install` 寫進 agent 的使用者層級指示檔（一定會載入） | **要本人決定（E11 的新選項）** |
| D9 | 能力需求：宣告式 H3 子節＋`models.floor`／`preferred`（先留空） | 定 |
| D10 | 錯誤情形：讀取與安裝的退出碼 | 定 |
| D11 | v0 職務（worker team PM）的內容 | 定（草案） |
| D12 | 測試與驗收：install／載入、格式檢查、期 1 端到端 | 定 |
| D13 | 期 3 銜接：多職務的載入（每 agent 一份設定目錄） | 定 |
| D14 | E6：worker 上誰開 team、怎麼呼叫 `open` | **由 MyPMO#33 處理**（PR #66） |

### D1 五要素與 Angareion 的落點（本人已答覆，2026-10-16）

- harness 的五要素照 2026-10-10：`know`／`do`／`judge`／`dont`／`capabilities`。
- **Angareion 不是 harness 的章節**：在 `capabilities` 宣告要用它、用法（怎麼等、怎麼讀、怎麼回）寫在 `know`。
- r1 的三種解讀（甲／乙／丙）到此結束：**甲**是結果。

### D2 一份 harness 檔＝一個職務一個版本；檔案格式（r1 沿用）

- **一份檔、一個職務、一個版本**；一個團隊的 harness＝它那組職務的版本檔。
- **檔案**：UTF-8、Markdown；第一個區塊是 YAML frontmatter（OKF v0.2 對齊；不認得的欄位保留）。本文是五個 H2 章節：

| 章節 | 放什麼 |
|---|---|
| `know` | 要知道的事：你在哪、你是誰、這份 harness 會自動載入（怎麼重讀）、工單與協作流程、Angareion 用法、名詞 |
| `do` | 要做什麼：職責、動作、產出（含開 PR） |
| `judge` | 怎麼判斷：完成定義、什麼時候停下來問、品質與證據的要求 |
| `dont` | 不做什麼：安全界線、禁止事項、被擋不繞過 |
| `capabilities` | 能力需求（D9）：每個能力一個 H3 子節（子節名＝能力名；v0：`github`／`angareion`／`agora`），子節內用一行 YAML／清單寫參數、沒有參數就留空 |

- **frontmatter 欄位**：

| 欄位 | 必填 | 說明 |
|---|---|---|
| `type` | ✔ | `Harness`（OKF 必填欄位） |
| `id` | ✔ | `atelier:<team>-<role>@<version>`（D3） |
| `title`、`description` | ✔／建議 | OKF |
| `status` | ✔ | `draft`／`stable`／`deprecated`；未經本人驗收前沿用 `draft` |
| `team`、`role` | ✔ | `team` 用 `[a-z0-9][a-z0-9-]*`（英文小寫與 `-`）；`role ∈ {pm, arch, impl, test}`（期 3 的 reviewer 對應 `arch`） |
| `version` | ✔ | `v<非負整數>`；MUST 與 `id` 的 `@` 版本、檔名 `v<N>.md` 一致（D4） |
| `models` | ✔ | `floor`（模型識別碼或 `null`）＋`preferred`（有順序清單，可空）——本人的兩個欄位（D9） |
| `generated`、`refs` | 選填 | OKF；`generated` 記作者與時間，`refs` 可指其他實體 |

- **寫到多細**：以「agent 照著做不會錯、人看得懂」為準；**機器欄位只認 frontmatter**（含 `models`）；本文只在 `--section`／`--json` 時做章節切分，不做語意解析；`capabilities` 在本文、v0 沒有機器消費它（環境不核銷；D9）。本文用繁中，指令、欄位名與識別用原文。
- **取捨**：單檔 vs 拆多檔——單檔好搬、好讀、好版本；期 3 Atelier 本體要拆再說。**採單檔。**
- **衝突時的態度（r3 新增）**：這份 harness 只是 harness，**專案自己的指示檔（`AGENTS.md`／`CLAUDE.md`／工單規則）優先**——harness 的 `know` MUST 明寫這件事（見 D11），因為兩邊的載入順序（D8）不保證 harness 會被後讀。

### D3 識別、命名與路徑

- **名稱**：`<team>-<role>`（`team` 見 D2 的字元規則；v0：`worker-team-pm`）。
- **識別**：`atelier:<名稱>@<版本>`（例 `atelier:worker-team-pm@v0`）——與 `docs/design.md` 的 ref 語法與例子 `atelier:reviewer@v3` 同一形狀。
- **路徑（虛擬資料夾）**：`atelier/<team>/<role>/v<N>.md`；一個團隊一個資料夾、一個職務一個子資料夾、一個版本一個檔。
- **與 agent 位址是兩件事**：agent 名（Angareion 位址）照 `angareion-v5` 的文法 `<repo 名>-<pm|arch|impl|test>[-<id>]`，由環境設定提供；harness 名稱不取代它。
- **取捨**：`<team>-<role>` vs 直接用 agent 名（會把 harness 綁死一個 repo）；資料夾樹 vs 扁平檔名——樹讓未來有團隊層共用檔的位置。**採 `<team>-<role>`＋三層路徑**（隊名 E7）。

### D4 版本怎麼管

- **版本檔 append-only**：一個版本一個檔（`v0.md`、`v1.md`…），MUST NOT 修改既有版本檔、MUST NOT 刪除；內容有變＝新增下一個版本檔。`version`、`id` 的 `@` 與檔名三者一致。
- **latest＝同一個 role 資料夾裡最大的 N**；`atelier show <id>` 讀 latest；`atelier show <id>@v0` 讀那一版；舊版永久可讀。
- **不可變的理由**：多個 AI 同時讀不會亂（D6）；要追溯「某次工作照哪版做」時版本還在。
- **取捨**：append-only vs「單檔改內容＋git 歷史找舊版」。**採 append-only**（結構性的不可變）。不做 semver；期 3 有需要再擴充。
- 升級＝新增版本檔＋合併（PR）→ 重跑 `atelier install`（D8）→ 下一個 session 生效。

### D5 與 Agora Session 的關係

- harness 樹是 **Atelier 自己的**；不是 Session、不進 Agora 儲存、不經 Drive；Agora 不需要認識 Atelier（`docs/design.md` 第 39 行）。
- Session（可選）可以在 header 的 `refs` 記 harness 的識別；**v0 不做自動寫入**（期 3 的作者提議）。

### D6 存放位置（本人已答覆，E9）

**MyAiStorage 的 git**：虛擬資料夾樹 `atelier/…` 放這個 repo；讀取與安裝都走本機 clone ＋ `git fetch`／`git show origin/main:`（與 angareion 讀 channel 設定同一種做法、同一複本路徑、同一把 flock 鎖、20 秒逾時——`openspec/changes/angareion-v5/design.md` 第 101 行）；匿名 HTTPS、零新憑證。多個 AI 同時讀不會亂的理由：讀取與安裝工具不寫 harness（唯一寫入是複本自身的 fetch）、版本檔不可變、沒有共享可寫狀態、同一台機器共用一份複本（各機器各自一份）。

### D7 讀取工具（本人已答覆，E10）

**新增 `atelier` 指令**（與 agora／angareion 同一個 distribution：`pyproject.toml` 加 entry point、新模組 `src/atelier/`）：

- `atelier ls [--json]`：列職務與版本。
- `atelier show <id>[@<版本>] [--section know|do|judge|dont|capabilities] [--json]`：印 harness。預設整份原樣（含 frontmatter）；`--section` 只印該章節；`--json` 給程式。
- **只讀**（唯一的本機寫入是複本自身的 `git fetch`）；不依賴工作樹、不用憑證；exit 0／1／2（D10）。

### D8 載入（本人已答覆 E11 改方向；**新的選項要本人決定**）

**方向**：不靠開場句；用 agent 一定會載入的機制；不能蓋掉或弄髒專案自己的 `AGENTS.md`／`CLAUDE.md`。做法＝`atelier install <id> --agent <claude|opencode> [--agent …]`（**可重複；每個值各裝一次、與順序無關；不給＝用法錯 exit 1**）把 harness 裝進每一個指定 agent 的**使用者層級指示檔**（每台機器一份、不進 repo）。

選項與取捨：

- **選項 1（v0 建議）：使用者層級全域指示檔＋開機安裝。**
  - **claude**：寫進 `~/.claude/rules/atelier-<team>-<role>.md`（使用者層級 rules，每台機器每個專案都會載入、在 project rules 之前；一個職務一個檔，不碰別人的檔案）。備選：`~/.claude/CLAUDE.md` 的**管理區塊**（`<!-- atelier:… -->`…`<!-- /atelier -->`，install 冪等替換）。**不碰** `~/.claude/settings.json`（不得有 `apiKeyHelper`；#33 D6 已列檢查）。
  - **opencode**：寫進 `~/.config/opencode/AGENTS.md` 的同款**管理區塊**（實測這個檔會被載入；v2 不讀 `~/.claude/CLAUDE.md`）。
  - **誰在什麼時候跑 install**：期 1 由 MyLinuxPool#130 的**開機掛點（start.d）**加一個 atelier 單位，開機時跑 `atelier install`。`atelier`／harness 檔在 worker 的套件與複本裡（**建置時不跑**——那時還沒有 angareion 準備好的本機複本）。**改版後**：worker 重啟時自動重跑；長期不重啟的 worker 要人工跑一次（驗收記在 tasks 4.4）。
  - 影響：**sidecar 零改動**（agent 啟動時自己載入）。期 3 多職務：同一台機器上多職務會共用同一份全域指示檔 → **撐不住**（要走選項 2）。
  - 生效：claude 在每個 conversation 開始時載入（resume 的重載要在驗收確認）；opencode 依官方文件在下一次模型請求前就會更新（同一 session 也吃）。
- **選項 2（期 3 建議的擴充）：每個 agent 一份設定目錄。** claude 用 `CLAUDE_CONFIG_DIR`（每個職務一個 `~/.claude` 等價目錄，各自一份 `CLAUDE.md`／rules）；opencode 每個職務一個 `XDG_CONFIG_HOME`（各自一份 `~/.config/opencode/AGENTS.md`）。**sidecar 在 `open` 時要多設一個環境變數**（要改 sidecar，小）；多職務撐得住；不碰 repo。`OPENCODE_CONFIG_DIR`／`instructions` 也可以研究，但本機 v2.0.29 的 `instructions` 實測沒生效（Context），先不要押。
- **選項 3（未選保留）：agent 啟動參數注入**——claude 用 `--append-system-prompt-file <harness>`（本機實測旗標存在）；opencode 沒有等價旗標。要改 agora（`start_native` 的 argv）或 sidecar 的 driver，而且 claude 的系統提示在對話中會被記錄、旗標文字要等壓縮或新對話才生效（官方文件）；不建議當 v0 的機制，保留。

**建議：選項 1（v0）＋選項 2（期 3）**；選項 3 與 r1／r2 的方案都保留為未選。**要本人決定（E11）。**

**未選（保留、不刪）**：

- **r2 的固定開場句方案**：sidecar `open` 的 `input` 開頭放「先讀你的 harness：`atelier show …`」+「再讀信道」。r3 之後不必靠它（harness 會自動載入）。
- **r1 的 sidecar 全文載入**：`open.harness` 欄位＋`harness_dir`＋全文原樣作為第一則輸入（錯誤碼見「與 sidecar 的介面」）。

### D9 能力需求與預設 profile（r1 沿用）

- **表示**：`capabilities` 在本文，一個能力一個 H3 子節（子節名＝能力名、用環境的 capability 名）；子節內用一行 YAML／清單寫參數，沒有參數就留空。**frontmatter 不放 `capabilities`**。v0 的能力集合：`github`（MyLinuxPool 單位 `gh` 的 capability 名）、`angareion`、`agora`（與 `shared-configs/*/unit.json` 一致——只讀確認）。**模型不是能力子節**：它是 frontmatter 的 `models`（`llmgateway` 是環境的能力單位、sidecar 用）。
- **angareion 的落點**：能力在 `capabilities` 宣告、用法在 `know`；MUST NOT 自成章節（D1）。
- **語意**：能力由環境提供；v0 不逐項核銷。誰負責滿足：環境能力＝MyLinuxPool；模型＝LLMGateway（未來 ModelBroker 依 `models` 欄位租用）；sidecar 只把憑證交給 agent。
- **本人的兩個模型欄位**：

  ```yaml
  models:
    floor: <模型識別碼或 null>        # 可接受的底線模型
    preferred: [<模型識別碼>, ...]    # 優先清單，有順序；可空
  ```

  語意：租用先照 `preferred` 順序；不得低於 `floor`；達不到就停下並通知。**本人已答覆（E4）：v0 先留空**（`floor: null`、`preferred: []`）；值之後由本人決定怎麼填。
- **預設 profile 的表示**：檔案不寫 profile 名；期 2 的按部門限制由 worker profile 做（MyLinuxPool#5）。
- **擴張點**：能力名開放（未知名稱＝格式檢查警告、不擋）；參數 free-form。

### D10 錯誤情形

**讀取（`ls`／`show`）與安裝（`install`）共用**：

| 情形 | 退出碼 | 行為 |
|---|---|---|
| 正常 | 0 | 印結果／裝好 |
| 用法錯、找不到 id／版本 | 1 | 說明；查無時先 fetch 一次（同 angareion） |
| 複本不存在、檔案壞、git 壞、安裝目標寫不進去 | 2 | 說明明確原因（複本不存在→提示 clone；權限→提示使用者／路徑） |
| fetch 失敗（離線、網路） | 0（不變） | stderr 警告一次、沿用複本 |

- 錯誤訊息不含 harness 內文；工具 MUST NOT 寫入 harness 樹、工作目錄或使用者檔案，**唯一的例外**：複本自身的 `git fetch`，以及 `install` 對**使用者層級指示檔**的管理區塊（D8）。
- install MUST NOT 觸碰 `~/.claude/settings.json`、專案的 `AGENTS.md`／`CLAUDE.md`、工作目錄的任何檔案。
- **（未選）sidecar 全文載入的錯誤表**（`harness_not_found`／`harness_version_mismatch`／`harness_invalid`；400、什麼都沒開）保留在「與 sidecar 的介面」。

### D11 v0 職務（worker team PM）的內容

以 PM 的一般規矩為底（MyPMO handbook，private；用自己的話）：派工與回報、一張單一個 branch／worktree、審查非作者、不自己寫程式、安全界線、不繞過被擋的動作。加上 worker 上的差異：

- **這份 harness 會自動載入**（D8）：不用自己去讀；要重讀或確認版本時用 `atelier show atelier:worker-team-pm`。**專案自己的指示檔與工單規則優先**，harness 只管職務與界線。
- 用 Angareion 收指示與回報；閒著用阻塞的 `wait` 等；urgency 7 以上先處理；訊息不貼私人資訊。
- 在 worker 上開 PR（憑證由環境提供；不直接改 main；不碰別的 repo、不碰真機、不 ssh、不 dispatch）。
- 憑證不讀不印不寫進訊息／log／commit；被擋就回報，不換方式繞過。
- 回報格式：做了什麼、產出（branch／PR）、**測試原樣輸出**、沒驗到的、用的模型（可加「讀到的 harness 版本」）、要決定的（附建議）。
- **期 1 v0 的暫定（本人已同意）**：隊上只有 PM 一個職務——收到的工作由 PM 自己做（含實作與測試），並在回報標明；期 3 有三職務後改回「不自己寫程式、非作者審查」。
- 公開性：不寫帳號、額度、憑證名、輪換表、MyBrain 內容。

草稿在 `harness/worker-team-pm.md`。

### D12 測試與驗收

- **工具測試（MyAiStorage）**：單元＝假的 git／假複本／假 HOME——`ls`、`show`（整份 byte-for-byte、`--section`、`--json`）、版本解析（latest／`@v0`）、exit 1／2、fetch 失敗沿用複本；**install**：管理區塊的**冪等**（跑兩次結果一樣）、保留使用者原本的內容、只動目標檔、`--agent claude`／`opencode` 各自的路徑與格式、**重複 `--agent`（兩個都裝、順序無關、同值去重）、沒給 `--agent` exit 1**、寫不進去時 exit 2；改壞驗證（拿掉版本解析、`--section` 抓錯段、install 蓋掉整個檔、install 動到 settings.json、重複安裝產生兩份——各要紅）。
- **載入驗證（隔離環境）**：claude／opencode 各起一次（opencode 用假 upstream 的本機流程；claude 用官方文件的行為＋期 1 實機驗收看得到）——確認 harness 的內容出現在 agent 的指示裡、且專案的同名檔沒有被動。
- **格式檢查（MyAiStorage；本 repo 目前沒有跑 pytest 的 CI）**：pytest 讀 `atelier/**/v<N>.md`——frontmatter 必填、`id`↔`version`↔檔名一致、五章齊、`capabilities` 以 H3 子節列出能力、`models` 兩欄存在（值可空）、MUST NOT 有 `angareion` 章節；**加上 append-only 檢查**（與 `origin/main` 的 merge-base 比對：既有版本檔的 blob 不得改變、不得刪除）。改壞驗證逐項記在 PR 說明；要不要接 CI 見 tasks。
- **期 1 驗收（MyLinuxPool#45 端到端；MyPMO#31）要看到什麼才算 harness v0 有效**：
  1. worker 開機（或映像建置）後，agent 的**使用者層級指示檔裡有這份 harness**（`atelier install` 的產物；版本 `atelier:worker-team-pm@v0`）；
  2. PM 起來後的第一個動作是去**讀 Angareion**（harness 是自動載入的，不需要第一則輸入叫它讀）；
  3. PM 的行為有 harness 的規矩（Angareion 收發、worker 上開 PR、回報格式）；沒有碰憑證／真機／主分支；
  4. 專案自己的 `AGENTS.md`／`CLAUDE.md`（如果 repo 有）**沒有被改**、工作樹乾淨；
  5. 指示由 PMO 經 Angareion 發出（#33 的派工句）、回報由 PMO 收到；本人 merge PR 收尾。
- **harness 檔本身怎麼驗**：格式檢查＋內容審查（非作者）＋期 1 驗收時看行為。

### D13 期 3 銜接（v0 暫定 → 被取代）

| v0 暫定 | 期 3 |
|---|---|
| 只有 PM 一份職務；PM 自己實作與測試（已同意） | PM／reviewer／實作隊員三職務；非作者審查、PM 不自己寫程式 |
| 載入＝使用者層級指示檔＋開機 install（單職務） | 每個 agent 一份設定目錄（`CLAUDE_CONFIG_DIR`／per-role XDG_CONFIG_HOME）——sidecar 要設環境變數（D8 選項 2） |
| 版本檔 append-only＋git | 由 Atelier 本體管理（多團隊、審查、發布、install 的正式版） |
| Session 的 refs 不自動記 harness | 可選：自動註記「照哪版做」（作者提議） |
| 隊名 `worker-team`（單一） | 部門／團隊命名（MyLinuxPool#5，期 2） |

### D14 worker 上誰開 team、怎麼呼叫 `open`（由 MyPMO#33 處理）

- PMO 用 `mlp fwd` 把 worker 的 `127.0.0.1:8471` 轉到 Mac 的本機埠，再用 `dispatch.py open` 呼叫 `POST /agents`（MyPMO#33 的設計 r4，PR #66）；名字三處一致（PMO 的 `teams.yml`、worker 的 `MLP_AGENT_NAME`、angareion 位址）。
- 本設計不處理這件事，也不要有相依的實作；harness 的載入（D8）與 `open` 的呼叫路徑無關。

## 與 sidecar 的介面

**結論：sidecar 不改（v0 零改動；比 r2 更少）。** harness 不再經過 `open` 的任何欄位、也不靠 `input`。

**第一則輸入（與 MyPMO#33 的銜接）**：

- #33（PMO 改用 Angareion 派工與收回報；PR #66 的設計 r4）的第一則輸入目前是「先讀你的 harness：`atelier show atelier:worker-team-pm`，照它的規矩工作。指示在 Angareion：…」。**r3 之後「先讀 harness」那半句是多餘的**（harness 由使用者層級指示檔自動載入）；**「指示在 Angareion」那半句還是要的**（那是派工，不是載入）。
- 建議給 #33 的簡化版（PMO 決定；跨 repo 對齊）：
  > 你是 worker 上的 team PM（<team PM 名>）。指示在 Angareion：用 `angareion read --channel <ch> --since <派工時刻> --json` 讀，只處理工單號是 <repo>#<n> 的那幾則……
  （其餘照 #33 §2 的字面：收到先回「收到」、讀不到就退避重試、做完用 `angareion send` 回報。）
- **位置**：`open` 的 `input`（選填）第一段；sidecar 照舊排隊等 agent 起來再送。**留著 #33 原句也無害**（只是多一次 `atelier show`）；要精簡由 #33 決定。
- 也可以完全不放 `input`：harness 還是會載入，但沒有人告訴 agent 去哪裡讀指示——**不建議**，v0 保留最小派工句。

**未選選項（保留；本人暫不要求）**：

- **r1 甲：sidecar 新增 `harness` 欄位**——`open` 選填 `harness: {name, version?}`；設定 `harness_dir`；載入＝讀檔、驗 frontmatter、全文原樣作為第一則輸入（在 `input` 之前）；錯誤 400 `harness_not_found`／`harness_version_mismatch`／`harness_invalid`；不改內容、不進 log。附帶 `docs/sidecar.md` 更新與 `install.sh --check`。
- **r1 乙：呼叫方把全文放 `input`**；**r2 的開場句方案**。都保留。

### 要 MyLinuxPool 做的事

1. **`atelier` 單位（開機掛點；期 1 的安裝機制）**——在 worker 的 `shared-configs/` 加一個單位（`unit.json` 的 `start`＋`files/start`，照 MyLinuxPool#130 的契約）：開機時跑 `atelier install atelier:worker-team-pm --agent claude --agent opencode`（改版後重啟會重跑；長跑不重啟的 worker 要人工跑一次）；失敗只記 log、不擋開機（照 #130 的規則）。**sidecar 不改。**
2. **套件 pin 升級（既有流程）**：worker 的 `agora` 套件 pin 升到含 `atelier` 的 sha。
3. **不做**：`harness_dir`／`open` 欄位／sidecar 的載入（未選）；harness 的內容驗證（在 MyAiStorage 的 CI）。
4. 其餘環境能力（映像、憑證、LLMGateway 等）照 #52 的既有範圍。
5. **（跨 repo，PMO 帶）**：#33 的第一則輸入可以去掉「先讀 harness」半句（D8 的「與 sidecar 的介面」）；worker 的 team PM 名稱、channel 等照 #33。

## 作者加的項目（本人未指示；帶給本人看）

- **載入機制的選項**（D8）：使用者層級指示檔＋開機 install（選項 1）、每 agent 一份設定目錄（選項 2）、啟動參數注入（選項 3，未選）。
- **`atelier install` 子指令**：管理區塊（`<!-- atelier:… -->`）、冪等、per-agent 路徑（claude rules 檔／opencode AGENTS.md）；由 MyLinuxPool#130 的 start.d 在開機時跑。
- **install 的目標選擇**：claude 用 `~/.claude/rules/atelier-<team>-<role>.md`（備選：`CLAUDE.md` 的管理區塊）；opencode 用 `~/.config/opencode/AGENTS.md` 的管理區塊。
- **harness 的 `know` 明寫「專案指示檔優先」**（D2／D11）。
- **`atelier` 指令、`ls`／`show`、`--section`／`--json`、exit 0／1／2、共用 angareion 的 clone 與 fetch 節流（E10 已答覆）**。
- **儲存與版本**：`atelier/<team>/<role>/v<N>.md`、append-only、latest＝同 role 最大 N（E9 已答覆）。
- 版本只放檔名與 frontmatter（`v<整數>`、不做 semver）。
- `models` 放 frontmatter 頂層；v0 的值留空（E4 已答覆）。
- `role ∈ {pm, arch, impl, test}` 與 `team` 的字元規則（D2）。
- harness 名稱與 agent 位址是兩件事（D3）。
- 隊名 `worker-team`（D3；E7）。
- v0 檔的 `status: draft`；`generated` 欄位記設計者。
- 期 1 單人暫定（本人已同意）。
- 回報可加記「讀到的 harness 版本」。
- Session `refs` 記 harness id（期 3；v0 不做）。
- 格式檢查放 MyAiStorage 的 pytest；tasks 的可選文件與可選 CI。
- （未選，保留）r1 的 sidecar 欄位與錯誤碼、r2 的開場句。

## Risks / Trade-offs

- [全域指示檔是「使用者層級」的] → 同一台機器上多職務會互相蓋到（期 1 只有一個職務，接受；期 3 走 D8 選項 2）。install 用管理區塊＋獨立檔名，至少不破壞別的內容。
- [專案指示檔 vs harness 的優先順序不保證] → harness 明寫「專案優先」；harness 只寫職務與界線，不重複專案規則（D2／D11）。
- [claude 在 `--resume` 時是否重讀 `CLAUDE.md` 沒明文] → 期 1 驗收列入確認項（D12）；規矩改版時最保險是 close 後重開（新 conversation 一定載入）。
- [opencode 的 `--standalone` 與指令更新] → 官方文件說全域 AGENTS.md 的改動在下一個請求前生效；`opencode reload` 也還在。
- [install 弄壞使用者環境] → 管理區塊冪等、只動目標檔、不碰 `settings.json`／專案檔／工作樹；測試有改壞驗證。
- [harness 檔在公開 repo] → 內容照 D11 的公開規則；模型的值留空（E4）。
- [未選方案日後若要回頭用] → r1／r2 的清單都保留；r1 的 sidecar 錯誤碼仍在。
- [「開 team」的路徑] → **由 MyPMO#33 處理**（PMO 經 ssh 轉發到 worker 8471 呼叫 open；PR #66）。
- [「一定會載入」≠「一定會遵守」] → 指示檔只是**注入 context**；官方 memory 文件說 CLAUDE.md 是 context、不是 enforced configuration（「Claude treats them as context, not enforced configuration」）。規矩有沒有被遵守用**行為驗收**看（D12：Angareion 收發、PR、回報格式、不碰禁區）；不遵守就改寫法或加別的檢查，不要假設它會被強制執行。

## Open Questions（未決事項；編號；附建議）

**本人已答覆（2026-10-16，經 PMO）**：

1. **E1**：五要素照 2026-10-10；Angareion 是 AiStorage 的要素、不是 harness 章節（D1）。
2. **E2**：harness＝AiStorage 的虛擬資料夾（一組），AI 用工具讀（D6／D7）。
3. **E3（原「harness 檔的正式位置」）**：已併入 E9；不再單獨列。
4. **E5**：暫不要求 sidecar 加欄位；sidecar 盡量不改；載入方式改在 r2／r3 提選項（D8）。
5. **E8**：同意期 1 單人暫定（PM 自己做並在回報標明）。
6. **E9**：harness 存 MyAiStorage 的 git（`atelier/<team>/<role>/v<N>.md`、append-only）（D6）。
7. **E10**：新增 `atelier` 指令（`ls`／`show`，只讀）（D7）。
8. **E4**：`models` 先留空、只留欄位（D9）。

**仍開放（要本人決定）**：

9. **E11（新；要本人決定）：載入機制的選項**——選項 1 使用者層級指示檔＋開機 install（**建議**；sidecar 零改動；v0 單職務）；選項 2 每 agent 一份設定目錄（期 3 多職務；sidecar 要設環境變數）；選項 3 啟動參數注入（未選）。見 D8。
10. **E7（低）：隊名**——v0 用 `worker-team`；期 2 部門命名（MyLinuxPool#5）再定。**建議：不阻塞，v0 就用。**

**已轉出去（不是本單的未決）**：

11. **E6：worker 上誰開 team、怎麼呼叫 `open`**——**由 MyPMO#33 處理**（PR #66 的設計：PMO 用 `mlp fwd` 轉發到 worker 的 8471，`dispatch.py open`；r1／r2 的「人在 worker 上呼叫」建議作廢）。
