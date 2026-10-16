## 0. 前提（不是 PR）

- 本 change（設計 r3）經非作者審查、PMO 拿給本人看、本人同意後才動工。
- 需要留給本人裁定的：**E11（載入機制的選項）**；E7（隊名）低、不阻塞。沒有裁定前，實作照設計的建議（E11 選項 1：使用者層級指示檔＋開機 install），PR 說明要標「依建議暫定，若改裁定需同步」。
- 本人 2026-10-16 已答覆：E1／E2／E5／E8／E9（放 MyAiStorage 的 git）／E10（新增 `atelier` 指令）／E4（`models` 先留空）。
- **E6 由 MyPMO#33 處理**（PMO 經 ssh 轉發到 worker 8471 呼叫 open；PR #66）——本 change 不再處理，也不要有相依的實作。
- v0 的驗收是期 1 端到端（本檔第 4 節）；MyLinuxPool#45 的內容不要先做（那是驗收題目）。

## 1. MyAiStorage：`atelier` 指令（`ls`／`show`／`install`）（PR 1）

**範圍**：`src/atelier/`（新模組：`cli.py` 的 `ls`／`show`／`install`、讀取層 `git fetch`／`git show origin/main:<路徑>`、版本解析、安裝層（管理區塊與 per-agent 路徑）、設定）、`pyproject.toml`（entry point `atelier`、hatch packages）、`tests/unit/test_atelier_cli.py`。合併後：任何機器上（有本機複本）能用 `atelier ls`／`show` 讀 harness；`atelier install <id> --agent claude|opencode`（**可重複；每個值各裝一次**）把 harness 裝進各 agent 的使用者層級指示檔。**一步一個 PR。**

- [ ] 1.1 讀取層：**同一複本路徑（與 angareion 共用）、同一把 flock 鎖、20 秒逾時**（沿用 angareion §11；`openspec/changes/angareion-v5/design.md` 第 101 行）；`git fetch` 節流；fetch 失敗沿用複本＋stderr 警告；`git show origin/main:<路徑>` 讀檔；不依賴工作樹、不用憑證；**除複本自身的 fetch 外不寫任何檔案**（可抽共用或先小型複製，實作決定）
- [ ] 1.2 `atelier ls`：列職務與版本（`atelier:worker-team-pm@v0`、`@v1`…；`--json`）
- [ ] 1.3 `atelier show <id>[@<版本>] [--section …] [--json]`：預設整份原樣（byte-for-byte，含 frontmatter）；`--section` 只印該章節；不指定版本＝版本號最大（同 role 資料夾最大的 N）
- [ ] 1.4 `atelier install <id>[@<版本>] --agent <claude|opencode> [--agent …]`：**`--agent` 可重複**（每個值各裝一次、與順序無關、同值去重；**不給＝exit 1**）；**管理區塊**（固定開頭／結尾註解）或**獨立檔**（claude 的 `~/.claude/rules/atelier-<team>-<role>.md`）；**冪等**（重跑替換、不堆疊）；不指定版本＝最新；**MUST NOT 碰** `~/.claude/settings.json`、專案 `AGENTS.md`／`CLAUDE.md`、工作目錄
- [ ] 1.5 退出碼與錯誤：0／1（用法、查無——查無先 fetch 一次）／2（複本、git、寫入失敗）；訊息不含內文；**除複本自身的 fetch 與 install 的目標檔外沒有寫入路徑**
- [ ] 1.6 單元測試（假 git／假複本／假 HOME）：整份 byte-for-byte、`--section`、`--json`、latest／`@v0`、exit 1／2、fetch 失敗沿用複本；install 的冪等、保留原內容、per-agent 路徑、**重複 `--agent`（兩個都裝、順序無關、同值去重）、沒給 `--agent` exit 1**、寫不進去 exit 2、不碰 settings／專案檔
- [ ] 1.7 改壞驗證：拿掉版本解析（永遠讀最新／忽略 `@`）、`--section` 抓錯段、fetch 失敗回 2、把內容「整理」過再印、讓工具能寫 harness／工作目錄（不該有）、install 蓋掉整個檔、install 動到 settings.json、重複安裝產生兩份——各要讓 1.6 紅；逐項記在 PR 說明
- [ ] 1.8 文件：`docs/atelier/tool.md`（一頁：指令、退出碼、設定與複本、install 的目標與管理區塊、與 angareion 共用）

**測試**：`uv run pytest -q tests/unit/test_atelier_cli.py`（不需要網路、不碰憑證；現無 CI，PR 說明附原樣輸出）。

## 2. MyAiStorage：harness 樹正式化與格式檢查（PR 2；依存 PR 1 與 E9）

**範圍**：`atelier/worker-team/pm/v0.md`（新；內容＝`openspec/changes/atelier-v0/harness/worker-team-pm.md` 的定稿）、`tests/unit/test_atelier_harness.py`（新）。合併後：虛擬資料夾樹的第一份職務檔就位、格式檢查綠；change 歸檔時同步（刪掉草稿副本或留指路）。

- [ ] 2.1 把職務草稿放進 `atelier/worker-team/pm/v0.md`；依 D11 修正內容（harness 自動載入、專案指示優先、要重讀時用 `atelier show`）
- [ ] 2.2 格式檢查測試（照 spec「harness 檔的格式」「識別、版本與路徑」「能力需求與模型欄位」）：frontmatter 必填、`id`↔`version`↔檔名一致、五個 H2 章節齊、`capabilities` 以 H3 子節列出能力（v0：`github`／`angareion`／`agora`；子節內一行 YAML 或留空）、`models.floor`／`preferred` 存在（值可空）、沒有 `angareion` 章節、`know` 有「專案指示優先」
- [ ] 2.3 append-only 檢查：與 `origin/main` 的 merge-base 比對——既有版本檔（`v<N>.md`）的 blob MUST 不變、MUST 不被刪除；新版本 MUST 是新檔（改壞驗證：拿掉這條檢查要紅）
- [ ] 2.4 改壞驗證：拿掉必填欄位、把版本改歪、拿掉章節、拿掉 `models` 一欄、拿掉 `angareion` 子節、把子節改成散文、加一個 `angareion` H2、拿掉「專案指示優先」——各要讓 2.2 紅
- [ ] 2.5（選配，PM 決定）`docs/atelier/harness.md` 一頁格式說明（欄位表、章節指引、版本規則）
- [ ] 2.6（選配）把格式檢查接進 GitHub Actions workflow（本 repo 目前沒有跑 pytest 的 CI）

**測試**：`uv run pytest -q tests/unit/test_atelier_harness.py`。

## 3. MyLinuxPool：`atelier` 單位（開機安裝）（PR 3；依存 PR 1；與 #130 的契約）

**範圍**：worker 的 `shared-configs/` 新單位（`unit.json` 的 `start`＋`files/start`，照 MyLinuxPool#130 的契約）與套件 pin。合併後：worker 開機時跑 `atelier install`，把 harness 裝進 agent 的使用者層級指示檔；sidecar 完全不改。

- [ ] 3.1 worker 的 `agora` 套件 pin 升到含 `atelier` 指令的 sha（既有流程；與 agora／angareion 同一個 pin）
- [ ] 3.2 新單位：開機時跑 `atelier install atelier:worker-team-pm --agent claude --agent opencode`；失敗只記 log、不擋開機（照 #130 的規則）；`install.sh --check` **兩邊都要驗**：claude 的 `~/.claude/rules/atelier-*.md` 存在（或 `CLAUDE.md` 裡有管理區塊）**且** opencode 的 `~/.config/opencode/AGENTS.md` 裡有管理區塊（任一沒有回 1）
- [ ] 3.3（可選）worker 啟動文件的固定開場句改成 r3 的簡化版（去掉「先讀 harness」半句；見設計「與 sidecar 的介面」）
- [ ] 3.4 若 E11 最後選了「每個 agent 一份設定目錄」（期 3）：sidecar 在 `open` 時多設 `CLAUDE_CONFIG_DIR`／per-role `XDG_CONFIG_HOME`——那是**期 3**的獨立單，不在本 PR。

**測試**：單元的 `--check` 三態；沒有真機時用沙箱 HOME／XDG；**不碰任何真檔**。

## 4. 期 1 端到端驗收（不是 PR；驗收題目 MyLinuxPool#45）

**範圍**：worker 開機（或映像建置）→ `atelier install` 把 harness 裝進使用者層級指示檔 → PMO 依 MyPMO#33 派工（`send`＋`open`，`input` 只說去 Angareion 讀）→ PM 讀到指示、做事 → 開 PR → 回報 → PMO 轉告本人 → 本人 merge。**這一步只做驗收，不先做 #45 的內容。**

- [ ] 4.1 安裝證據：agent 的使用者層級指示檔裡有 harness（版本 `@v0`）；專案自己的 `AGENTS.md`／`CLAUDE.md` 逐字不變；工作樹乾淨
- [ ] 4.2 PM 起來後**不需要**第一則輸入叫它讀 harness（`input` 只有派工句）——留下 harness 已載入的證據（claude：session 的 memory 清單／`/context` 之類；opencode：系統提示的 `Instructions from:`）。**按 worker 實機釘的版本重驗**（設計時本機：claude 2.1.301／opencode 2.0.29；worker 釘 2.1.296／2.0.26）
- [ ] 4.3 照 D12 的 checklist：Angareion 收發、worker 上開 PR、回報含 PR 連結與測試原樣輸出、沒有碰憑證／真機／main
- [ ] 4.4 規矩改版：新增 `v1` 後**重跑 install**（worker 重啟會自動重跑；不重啟就人工跑一次）→ 在下一次啟動生效——記下實際觀察（claude 的 resume 行為、opencode 的下一個請求，都在這裡確認）
- [ ] 4.5 驗收紀錄（PMO 收；記進 MyPMO 期 1 的驗收）：通過／不通過、觀察到的偏差、要改 harness 的地方

## 5. 後續（不在本 change；列出來源）

- **期 3（MyAiStorage#17）**：Atelier 本體＝格式、版本、發布與載入（install）的正式版；三職務（PM／reviewer／impl）；多職務的載入＝每個 agent 一份設定目錄（D8 選項 2；sidecar 要設環境變數）。
- **期 2（MyLinuxPool#5）**：部門／團隊命名與 profile 的限制。
- **MyPMO#33**：派工與收回報的流程（第一則輸入可以去掉「先讀 harness」半句）；E6 已轉過去。
- **模型租用（LLMGateway／ModelBroker）**：`models.floor`／`preferred` 的消費端；值要不要公開見 E4（本人已答覆先留空）。
- **未選（保留）**：r1 的 sidecar 全文載入、r2 的固定開場句、r3 的啟動參數注入（`--append-system-prompt-file`）。
