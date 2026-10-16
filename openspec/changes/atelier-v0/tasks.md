## 0. 前提（不是 PR）

- 本 change（設計）經非作者審查、PMO 拿給本人看、本人同意後才動工。
- 需要留給本人決定的先解掉：E1（五要素與 Angareion 落點）、E2（載入方式）、E5（sidecar 介面甲／乙／丙）。沒有裁定前，章節與 sidecar 欄位照設計的建議（甲）先寫，但 PR 說明要標「依建議暫定，若改裁定需同步」。
- v0 的驗收是期 1 端到端（本檔第 4 節）；MyLinuxPool#45 的內容不要先做（那是驗收題目）。

## 1. MyAiStorage：harness 檔正式化與格式檢查（PR 1）

**範圍**：`docs/atelier/harness/worker-team-pm.md`（新；內容＝`openspec/changes/atelier-v0/harness/worker-team-pm.md` 的定稿）、`tests/unit/test_atelier_harness.py`（新）、`docs/design.md` 的 Atelier 一段（若 E3 有裁定就補）。合併後：正式位置有一份通過格式檢查的 v0 職務檔；change 歸檔時同步（刪掉 change 內的草稿副本或留指路）。**一步一個 PR；本檔不做。**

- [ ] 1.1 把職務草稿放進 `docs/atelier/harness/worker-team-pm.md`；依 E1／E8 的裁定修正章節與內容（照 D1 的解讀調整 `capabilities` 章節——甲的 `angareion` 是子節，乙則獨立成章）
- [ ] 1.2 格式檢查測試（照 spec「harness 檔的格式」「識別與版本」「能力需求與模型欄位」）：frontmatter 必填（type／id／title／status／team／role／version／models）、`id`↔`version`↔`team`／`role` 一致、五個 H2 章節齊、`capabilities` 以 H3 子節列出能力（v0：`github`／`angareion`／`agora`；子節內一行 YAML 或留空）、`models.floor`／`preferred` 兩欄存在（值可空）
- [ ] 1.3 改壞驗證：拿掉一個必填欄位、把 `id` 的版本改歪、拿掉一個章節、拿掉 `models` 的一欄、拿掉 `angareion` 子節、把能力子節改成散文——各要讓 1.2 紅；逐項記在 PR 說明
- [ ] 1.4（選配，PM 決定）`docs/atelier/harness.md` 一頁格式說明（給之後寫職務的人）：欄位表、章節指引、版本規則
- [ ] 1.5（選配）把格式檢查接進 GitHub Actions workflow（本 repo 目前沒有跑 pytest 的 CI；要接再開）

**測試**：`uv run pytest -q tests/unit/test_atelier_harness.py`（不需要網路、不碰任何憑證；現無 CI，PR 說明附原樣輸出）。

## 2. MyLinuxPool：sidecar 的 harness 載入（PR 2；甲案；依存 PR 1 的檔與 E5 的裁定）

**範圍**：sidecar 的 `open` 與設定（`harness` 欄位、`harness_dir`）、載入路徑（讀檔→驗 frontmatter→全文作為第一則輸入）、三個錯誤碼、`docs/sidecar.md`（欄位與錯誤碼）、單元與整合測試。合併後：帶 `harness` 的 `open` 會把全文當第一則輸入；錯誤情形回 400 且什麼都沒開。

- [ ] 2.1 `open` 新增選填 `harness: {name, version?}`；設定新增 `harness_dir`（預設 `~/.config/sidecar/harness`）；不帶欄位時行為不變（`docs/sidecar.md` 第 74-104 行的欄位表與錯誤碼表跟著更新）
- [ ] 2.2 載入：讀 `<harness_dir>/<name>.md`、只解析 frontmatter（`type`／`id`／`version`）、版本比對（省略＝部署版）、把全文原樣排進第一則輸入（在 `input` 之前）；不改內容、不進 log／status（D6／D7／D10）
- [ ] 2.3 錯誤：`harness_not_found`／`harness_version_mismatch`／`harness_invalid`（400、什麼都沒開、訊息不含內文）
- [ ] 2.4 單元測試：假 harness 檔＋假 driver——第一則輸入 byte-for-byte 等於檔案；接續（`session`）也送；harness 先於一般 `input`；三種錯誤各回對應 code 且沒有 agent 被開；不帶欄位照舊
- [ ] 2.5 改壞驗證：拿掉載入（不送）、送出前「整理」內文（改一個字）、不比對版本、把錯誤碼改成 500、把內文寫進 log——各要讓 2.4 紅
- [ ] 2.6 整合（離線可跑）：真 agent binary 起一次（用假 provider／不需模型），確認第一個輸入是全文——比照現有整合測試的 skip 規則

**測試**：`python3 -m pytest modules/sidecar/tests`（單元）；整合照 sidecar 現有的環境變數規則。

## 3. MyLinuxPool：worker 部署 harness 檔（PR 3；依存 PR 1 與 2）

**範圍**：sidecar 單元（`shared-configs/sidecar/`）與映像——把 MyAiStorage 釘住的 sha 裡的 `docs/atelier/harness/worker-team-pm.md` 放進映像的 `harness_dir`；`install.sh --check` 驗檔案存在。合併後：worker 上的 sidecar 讀得到 v0 的職務檔，版本與 pin 一致。

- [ ] 3.1 從 pin 取檔放進映像（與 agora／sidecar 同一處 pin；build 時取、啟動不連網）
- [ ] 3.2 `install.sh --check`：檔案存在、frontmatter 的 `id` 與 pin 的預期版本一致（不存在回 1）
- [ ] 3.3 文件：`docs/sidecar.md` 或單元的 README 寫明檔從哪來、怎麼升版

**測試**：單元的 `--check` 三態；沒有真機時用沙箱目錄。

## 4. 期 1 端到端驗收（不是 PR；驗收題目 MyLinuxPool#45）

**範圍**：在 worker 上開 team（E6 的最簡做法）→ PMO 經 Angareion 派工 → PM 做完 MyLinuxPool#45 的內容 → 開 PR → 回報 → PMO 轉告本人 → 本人 merge。**這一步只做驗收，不先做 #45 的內容。**

- [ ] 4.1 開 team：`open` 帶 `harness: {"name": "worker-team-pm", "version": "v0"}`（或乙案：全文放 `input`）；留下第一則輸入是全文的證據
- [ ] 4.2 PMO `angareion send` 派工；PM `read`→做事→開 PR→`send` 回報
- [ ] 4.3 驗收 checklist（D12）：第一則輸入是全文；行為有 harness 的規矩（Angareion 收發、worker 上開 PR、回報格式）；沒有碰憑證／真機／main；回報含 PR 連結與測試原樣輸出
- [ ] 4.4 驗收紀錄（PMO 收；記進 MyPMO 期 1 的驗收）：通過／不通過、觀察到的偏差、要改 harness 的地方

## 5. 後續（不在本 change；列出來源）

- **期 3（MyAiStorage#17）**：Atelier 本體＝格式、版本、載入的正式版；三職務（PM／reviewer／impl）各一份；載入的正式機制（E2 的選項 3 或版本追蹤）。本 change 的格式是它的 subset。
- **期 2（MyLinuxPool#5）**：部門／團隊命名與 profile 的限制（能力按部門給）。
- **sidecar 的後續（MyLinuxPool#52／#130）**：worker 上誰啟動 team（E6）、遠端控制路徑；harness 欄位（甲案）落地後，乙案過渡路徑自然退場。
- **模型租用（LLMGateway／ModelBroker）**：`models.floor`／`preferred` 的消費端；值要不要公開見 E4。
