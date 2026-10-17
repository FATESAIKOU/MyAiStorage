## 0. 前提（不是 PR）

- 本 change（設計）先經非作者審查、PMO 拿給本人看、定案（第 1 節）之後才動工；**v1 的內容**已經在 `harness/pm-v1.md` 的草稿裡，實作＝把它定稿進樹＋補檢查。
- 來源：PMO 的「PM 共通的工作方式」草稿（第 1、2 節）＋本人 2026-10-18 對附錄 B 的 17 條決定（B-1～B-17）＋現行 `atelier/worker-team/pm/v0.md`。**草稿的附錄 A（各專案特有的做法）不進 harness**（B-17）。
- **不動**：v0 檔（append-only）、`src/` 的程式（`atelier` 的 id／路徑／install 行為都不改）、`~/.claude/settings.json`、專案自己的 `AGENTS.md`。
- 公開 repo：實作與文件的內容照 design「私人資訊」的約束（不寫帳號、額度、憑證名稱、其他專案的名字、機器位址、輪換表、第二大腦）。

## 1. 定案（要 PM／本人；沒有定案不動工）

- [ ] 1.1 D3 識別與路徑：建議選項 1（沿用 `worker-team`，新增 `atelier/worker-team/pm/v1.md`）；若選 2／3，先讀 design 的代價（清理舊 install 產物／改文法）
- [ ] 1.2 D6 能力名：建議選項 I（`github`／`channel`／`agora`／`team`／`monitor`）
- [ ] 1.3 D5 環境說明：建議選項 A（放環境那一側、由環境的主人維護；worker 那一份是跨 repo 的後續）
- [ ] 1.4 Mac 上誰跑 `atelier install`、什麼時候跑（共用一份的前提；本設計的提議：本人在上線時手動跑一次，之後每次改版重跑）
- [ ] 1.5 `harness/pm-v1.md` 草稿的措辭（標題／`description`／內文）與 `status: draft` 的處置

## 2. MyAiStorage：v1 職務檔與格式檢查（PR 1）

**範圍**：`atelier/<team>/pm/v1.md`（新；內容＝本 change 的 `harness/pm-v1.md` 定稿）、`tests/unit/test_atelier_harness.py`（補強）、`docs/atelier/harness.md`（一頁格式說明）。合併後：`atelier show atelier:worker-team-pm` 讀到 v1；格式檢查綠；`@v0` 仍可讀。

- [ ] 2.1 把草稿搬進 `atelier/worker-team/pm/v1.md`（frontmatter 的 `id`／`team`／`version` 照 1.1 的定案；正文照 1.5）
- [ ] 2.2 格式檢查補強（design D7 的清單）：更新已知能力名清單（照 1.2）、「審查一律非作者」擴到所有 pm 版本、v1 的關鍵句（`不是權限`、`照 PMO 的模型輪換表`——**三段聯集**：`know`＋`judge`＋`dont` 合起來各出現一次，不要求每段都有）、工具名／模型名／其他專案名／機器位址／家目錄路徑（`/Users/`、`~/`）的黑名單（只驗 `v>=1` 的正文，不含 `capabilities` 子節名與 frontmatter；比對前先遮掉 `CLAUDE.md`／`AGENTS.md` 這兩個檔名；`/tmp` 這種通用系統路徑不算）、同一職務共用一份的檢查（**只警告、不擋**；清單跟 1.2 的定案走）
- [ ] 2.3 改壞驗證：2.2 的每一條各一個注入（拿掉關鍵句、塞工具名、塞模型名、塞 IPv4、塞 `~/` 路徑、動到 v0 的 bytes）都要紅；**共用一份那條是警告**：注入後 assert 有 warning、不是 error；逐項記在 PR 說明
- [ ] 2.4 文件：`docs/atelier/harness.md` 補 v1 的能力名與「環境說明」「v0 已被 v1 取代」；`src/atelier/harness.py` 的註解（若有提到已知能力名）跟著更新
- [ ] 2.5 測試：`uv run pytest -q tests/unit/test_atelier_harness.py`；`openspec validate atelier-pm-harness-v1 --strict --no-interactive`
- [ ] 2.6 歸檔：`openspec archive atelier-pm-harness-v1`（把 delta 併進主 spec）——照例在實作 PR 合併之後

## 3. 載入與升級（上線）

- [ ] 3.1 worker：合併後重啟（開機單位跑的是不帶版本的 `atelier install`，會拿 latest＝v1），或人工跑一次 `atelier install atelier:worker-team-pm`；記下 install 的輸出
- [ ] 3.2 Mac：跑一次 `atelier install atelier:worker-team-pm --agent claude`（第一個職務檔會在 `~/.claude/rules/` 出現）
- [ ] 3.3 生效的確認：Mac 開**新的 conversation**、worker 重開（或等下一次開機）→ 確認新的 context 裡是 v1
- [ ] 3.4 沒有兩份：確認 claude 的 rules 只有一個這個職務的檔、opencode 的 `AGENTS.md` 只有一個這個職務的區塊（同名升級＝取代）
- [ ] 3.5 Rollback 演練（可選）：`atelier install atelier:worker-team-pm@v0` 會覆蓋回 v0

## 4. 環境說明（照 1.3 的定案）

- [ ] 4.1 Mac 環境說明：你在哪、通道收發用什麼、能力由誰提供、工作檔放哪、真機怎麼動、被擋時怎麼請本人接手、`~/.claude/rules/` 怎麼放；誰維護（本人／PM）
- [ ] 4.2 worker 環境說明：同上＋開關隊員與監看的工具、通道的指令用法（怎麼等、怎麼讀、怎麼回）、工作目錄與記憶的寫回
- [ ] 4.3 worker 那一份的落地＝**跨 repo**（開機單位或它的檔案；標記用自己的一組，不要用 `<!-- atelier:… -->`）——另一個 PR，由 PMO 排

## 5. 跨 repo 收尾（不在本 PR；列出來源）

- [ ] 5.1 各 repo 的 `AGENTS.md`：收草稿附錄 A 的專案特有內容（本 repo 的隔離規則、測試與工具怪癖、真機規矩……）；各自的 PM 帶
- [ ] 5.2 PMO 的 repo：工單規則補「PM 開的 d 不用另一位附議」（B-7）；模型輪換表收 B-14／B-15 的細節（選模型的順序、哪些只能短任務、審查不用哪些）
- [ ] 5.3 隊員工具例外的說法（B-13）：不留個別例外；該團隊的文件與輪換表照同一套
- [ ] 5.4 工單規則／repo 摘要裡「進行中的 b＋c 上限」的舊數字（B-6）一起更正

## 6. 驗收（行為，不是測試）

- [ ] 6.1 兩個環境各一個新 session：指示檔裡是 `atelier:worker-team-pm@v1`（`atelier show --section know` 對得上）；專案自己的 `AGENTS.md`／`CLAUDE.md` 逐字不變
- [ ] 6.2 PM 起來後的行為照 v1：收指示、開單、派工、監看、非作者審查、PR 與回報的格式都對得上；沒有碰憑證／真機／main
- [ ] 6.3 沒驗到的、觀察到的偏差，記回工單與 PMO
