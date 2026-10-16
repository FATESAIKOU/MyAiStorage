## 0. 前提（不是 PR）

- 本 change（設計 r2）經非作者審查、PMO 拿給本人看、本人同意後才動工。
- 需要留給本人裁定的先解掉：E9（存放位置）、E10（讀取工具）、E11（載入選項）；仍開放的 E4（模型值）、E7（隊名）。沒有裁定前，實作照設計的建議（git 虛擬資料夾＋`atelier` 指令＋固定開場句），PR 說明要標「依建議暫定，若改裁定需同步」。
- 本人 2026-10-16 已答覆：E1（Angareion 不是 harness 章節）、E2（harness＝虛擬資料夾＋工具讀取）、E5（sidecar 暫不改）、E8（期 1 單人暫定同意）。
- v0 的驗收是期 1 端到端（本檔第 4 節）；MyLinuxPool#45 的內容不要先做（那是驗收題目）。

## 1. MyAiStorage：`atelier` 讀取工具（PR 1）

**範圍**：`src/atelier/`（新模組：`cli.py` 的 `ls`／`show`、讀取層 `git fetch`／`git show origin/main:<路徑>`、版本解析、設定）、`pyproject.toml`（entry point `atelier`、hatch packages）、`tests/unit/test_atelier_cli.py`。合併後：任何機器上（有本機複本）能用 `atelier ls`／`atelier show` 讀 harness，唯讀、匿名、離線可退。**一步一個 PR。**

- [ ] 1.1 讀取層：本機複本＋`git fetch` 節流（沿用 angareion §11 的讀法；fetch 失敗沿用複本＋stderr 警告）；`git show origin/main:<路徑>` 讀檔；不依賴工作樹、不用憑證（可抽共用或先小型複製，實作決定）
- [ ] 1.2 `atelier ls`：列職務與版本（`atelier:worker-team-pm@v0`、`@v1`…；`--json`）
- [ ] 1.3 `atelier show <id>[@<版本>] [--section …] [--json]`：預設整份原樣（byte-for-byte，含 frontmatter）；`--section` 只印該章節；不指定版本＝版本號最大
- [ ] 1.4 退出碼與錯誤：0／1（用法、查無——查無先 fetch 一次）／2（複本不存在、檔案壞、git 壞）；訊息不含內文；沒有寫入路徑
- [ ] 1.5 單元測試（假 git／假複本）：整份 byte-for-byte、`--section`、`--json`、latest／`@v0`、exit 1／2、fetch 失敗沿用複本（exit 不變）、唯讀
- [ ] 1.6 改壞驗證：拿掉版本解析（永遠讀最新／忽略 `@`）、`--section` 抓錯段、fetch 失敗回 2、把內容「整理」過再印、讓工具能寫——各要讓 1.5 紅；逐項記在 PR 說明
- [ ] 1.7 文件：`docs/atelier/tool.md`（一頁：指令、退出碼、設定與複本、與 angareion 共用）

**測試**：`uv run pytest -q tests/unit/test_atelier_cli.py`（不需要網路、不碰憑證；現無 CI，PR 說明附原樣輸出）。

## 2. MyAiStorage：harness 樹正式化與格式檢查（PR 2；依存 PR 1 與 E9）

**範圍**：`atelier/worker-team/pm/v0.md`（新；內容＝`openspec/changes/atelier-v0/harness/worker-team-pm.md` 的定稿）、`tests/unit/test_atelier_harness.py`（新）。合併後：虛擬資料夾樹的第一份職務檔就位、格式檢查綠；change 歸檔時同步（刪掉草稿副本或留指路）。

- [ ] 2.1 把職務草稿放進 `atelier/worker-team/pm/v0.md`；依 D11 修正內容（含「先讀自己的 harness、新指示前重讀」）
- [ ] 2.2 格式檢查測試（照 spec「harness 檔的格式」「識別、版本與路徑」「能力需求與模型欄位」）：frontmatter 必填、`id`↔`version`↔檔名一致、五個 H2 章節齊、`capabilities` 以 H3 子節列出能力（v0：`github`／`angareion`／`agora`；子節內一行 YAML 或留空）、`models.floor`／`preferred` 存在（值可空）、沒有 `angareion` 章節
- [ ] 2.3 append-only 檢查：與 `origin/main` 的 merge-base 比對——既有版本檔（`v<N>.md`）的 blob MUST 不變、MUST 不被刪除；新版本 MUST 是新檔（改壞驗證：拿掉這條檢查要紅）
- [ ] 2.4 改壞驗證：拿掉必填欄位、把版本改歪、拿掉章節、拿掉 `models` 一欄、拿掉 `angareion` 子節、把子節改成散文、加一個 `angareion` H2——各要讓 2.2 紅
- [ ] 2.5（選配，PM 決定）`docs/atelier/harness.md` 一頁格式說明（欄位表、章節指引、版本規則）
- [ ] 2.6（選配）把格式檢查接進 GitHub Actions workflow（本 repo 目前沒有跑 pytest 的 CI）

**測試**：`uv run pytest -q tests/unit/test_atelier_harness.py`。

## 3. MyLinuxPool：worker 套件 pin 升到含 `atelier`（PR 3；既有流程、最小）

**範圍**：worker 的 `agora` 套件 pin（`shared-configs/agora/tool-pins` 的既有做法）升到含 `atelier` 指令的 sha。合併後：worker 上 `atelier` 可執行、讀得到 harness 樹（與 angareion 共用同一份本機複本）。**預設不做別的**（不改 sidecar、不新增能力、不新增憑證）。

- [ ] 3.1 pin 升級＋`install.sh --check` 或既有檢查（`atelier --help` 可跑、讀得到複本）
- [ ] 3.2（可選）worker 啟動的固定開場句寫進 MyLinuxPool 的文件（字面照設計「與 sidecar 的介面」）

**測試**：單元的 `--check` 三態；沒有真機時用沙箱目錄。

## 4. 期 1 端到端驗收（不是 PR；驗收題目 MyLinuxPool#45）

**範圍**：在 worker 上開 team（E6 的最簡做法）→ `open` 的 `input` 開頭是固定開場句 → PM 讀 harness、讀信道 → PMO 經 Angareion 派工 → PM 做完 MyLinuxPool#45 的內容 → 開 PR → 回報 → PMO 轉告本人 → 本人 merge。**這一步只做驗收，不先做 #45 的內容。**

- [ ] 4.1 開 team：`open` 帶固定開場句（先讀 harness、再讀信道）；留下「PM 第一個動作是 `atelier show`」與讀到版本的證據
- [ ] 4.2 PMO `angareion send` 派工；PM `read`→做事→開 PR→`send` 回報
- [ ] 4.3 驗收 checklist（D12）：開場句在 `input` 開頭；PM 先讀 harness 再讀信道；harness 版本核對（開場句 pin `@v0` 更好核）；行為有 harness 的規矩；沒有碰憑證／真機／main；回報含 PR 連結與測試原樣輸出
- [ ] 4.4 驗收紀錄（PMO 收；記進 MyPMO 期 1 的驗收）：通過／不通過、觀察到的偏差、要改 harness 的地方

## 5. 後續（不在本 change；列出來源）

- **期 3（MyAiStorage#17）**：Atelier 本體＝格式、版本、發布與載入的正式版；三職務（PM／reviewer／impl）；載入可評估未選的 sidecar 載入（D8 丙）或版本追蹤。
- **期 2（MyLinuxPool#5）**：部門／團隊命名與 profile 的限制。
- **sidecar 的後續（MyLinuxPool#52／#130）**：worker 上誰啟動 team（E6）、遠端控制路徑；若日後選 sidecar 全文載入，照「與 sidecar 的介面」保留的清單做。
- **模型租用（LLMGateway／ModelBroker）**：`models.floor`／`preferred` 的消費端；值要不要公開見 E4。
- **MyPMO#33**：PMO 派工與收回報的流程文件採用固定開場句（跨 repo 的文案同步）。
