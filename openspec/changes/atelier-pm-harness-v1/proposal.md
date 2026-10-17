## Why

期 2 M1（MyAiStorage#94）：本人 2026-10-18 對「PM 共通草稿」的 17 條待決定事項做了決定（真機由 PM 在授權下自己跑、開 PR 前高中修完、隊員選模型的順序、同模型不同 session 的審查退路、轉達的同意不算權限、只寫能力不寫工具名、模型細節放輪換表、Mac 與 worker 共用一份、專案特有的留在 `AGENTS.md`……）。

這些決定要收成 **Atelier 的 PM harness v1**：取代期 1 的單人暫定版 `atelier:worker-team-pm@v0`（已在 worker 上載入、實機驗收通過），並改成 **Mac 上的 repo PM 與 worker 上的 team PM 共用一份**（期 2 之後兩邊都是有隊員的 PM，B-17）。v0 的 `do` 是「單人暫定、PM 自己實作」，v1 起 PM 不自己寫程式、一律非作者審查。

決定裡的 B-15／B-16 同時改了 harness 的**寫法**：模型只寫「照 PMO 的輪換表」、通道與隊員管理只寫**能力**不寫工具名——所以 harness 檔案的格式規則（`capabilities` 子節名、內容限制）要跟著更新，這一件也落在本 change。

本 change 只做設計（proposal／design／specs／tasks／`harness/pm-v1.md` 草稿），**不實作**：v1 檔進樹、格式檢查、環境說明、install 與開機單位都只列步驟。

## What Changes

- **v1 職務檔的內容**（草稿 `harness/pm-v1.md`，同 v0 的做法先放 change 內）：以 v0 為底，改成「有隊員的 PM」；五章照「PM 共通草稿」第 2 節重整（`know`：你是誰／怎麼載入／接手／工單與協作／模型／回報與通道；`do`：收到需求／開單拆單／派工／監看／核對與審查／開 PR 與合併／收尾；`judge`；`dont`；`capabilities`）。
- **共用一份**：`team` 沿用 `worker-team`（新檔 `atelier/worker-team/pm/v1.md`，id `atelier:worker-team-pm@v1`）；Mac 與 worker 的差異（通道、真機、怎麼請本人接手、工作檔在哪）放**環境說明**，各一份；各專案特有的做法留在該 repo 的 `AGENTS.md`。
- **17 條決定的落點**逐條寫在 design（harness 哪一章／環境說明／各 repo 的 `AGENTS.md`）。
- **寫法與格式**（B-15／B-16）：harness 只寫能力（回報 PMO、開關隊員、監看隊員……），不寫工具名；模型只寫「照 PMO 的模型輪換表」；同時進行中的 b／c 上限只寫「照工單規則」。格式檢查（`tests/unit/test_atelier_harness.py`）跟著補：已知能力名清單、v1 的關鍵句、工具／模型／位址的黑名單。
- **載入與升級**：v0 已在 worker 上；v1 以同一個 name 升級（`atelier install atelier:worker-team-pm`＝latest），重跑 install 即取代，不會兩份同時載入；Mac 要自己跑一次 install；改版後 worker 要重啟（或人工跑一次）才生效。v0 檔**不動**（append-only）。
- **未定、要 PM／本人決定的**（附選項與建議）：id／team 名、`capabilities` 的能力名、環境說明放哪與誰維護。

## Capabilities

### New Capabilities

（無。）

### Modified Capabilities

- `atelier`：harness 檔的格式（內容限制：只寫能力、不寫工具名；模型只寫「照輪換表」）、能力需求（跨環境共用的職務可以用中性能力名）、v1 職務（PM，有隊員的 PM；Mac 與 worker 共用）——delta 在 `specs/atelier/spec.md`。

## Impact

- 本 change 的產出（MyAiStorage，公開、無私人資訊）：`openspec/changes/atelier-pm-harness-v1/`（proposal／design／tasks／specs）、`harness/pm-v1.md`（v1 草稿）。
- 未來實作（不在本 change，見 tasks.md）：`atelier/worker-team/pm/v1.md`（v1 定稿）、`tests/unit/test_atelier_harness.py`（格式檢查補強）、`docs/atelier/harness.md`（一頁格式說明補 v1）；載入＝Mac 跑一次 `atelier install`、worker 重啟（`start.d/20-atelier` 已裝 latest）——**程式不改**。
- 跨 repo（不在本 change；列出來源）：各 repo 的 `AGENTS.md` 收「附錄 A 的專案特有內容」（各自的 PM 帶）；PMO 那一側的工單規則（PM 開的 d 不用附議的措辭）與模型輪換表（B-14／B-15 的細節）由 PMO 排。
- 不含：程式碼變更；v0 檔的修改（append-only）；期 3 的 Atelier 本體與多職務載入；模型輪換政策本身。
- 私人資訊：本 change 與 harness MUST NOT 含帳號、額度、憑證名稱、其他專案的名字、機器位址、模型輪換表、第二大腦內容（公開 repo）。
