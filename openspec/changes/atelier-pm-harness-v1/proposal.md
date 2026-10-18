## Why

期 2 M1（MyAiStorage#94）：本人 2026-10-18 對「PM 共通草稿」的 17 條待決定事項做了決定（真機由 PM 在授權下自己跑、開 PR 前高中修完、隊員選模型的順序、同模型不同 session 的審查退路、轉達的同意不算權限、只寫能力不寫工具名、模型細節放輪換表、Mac 與 worker 共用一份、專案特有的留在 `AGENTS.md`……）。

這些決定要收成 **Atelier 的 PM harness v1**：取代期 1 的單人暫定版 `atelier:worker-team-pm@v0`（已在 worker 上載入、實機驗收通過），並改成 **Mac 上的 repo PM 與 worker 上的 team PM 共用一份**（期 2 之後兩邊都是有隊員的 PM，B-17）。v0 的 `do` 是「單人暫定、PM 自己實作」，v1 起 PM 不自己寫程式、一律非作者審查。

決定裡的 B-15／B-16 同時改了 harness 的**寫法**：模型只寫「照 PMO 的輪換表」、通道與隊員管理只寫**能力**不寫工具名——所以 harness 檔案的格式規則（`capabilities` 子節名、內容限制）要跟著更新，這一件也落在本 change。

本 change 只做設計（proposal／design／specs／tasks／`harness/pm-v1.md` 草稿），**不實作**：v1 檔進樹、格式檢查、`atelier render`、sidecar／pane 的交付、開機單位的清理都只列步驟。

## What Changes

- **v1 職務檔的內容**（草稿 `harness/pm-v1.md`，同 v0 的做法先放 change 內）：以 v0 為底，改成「有隊員的 PM」；五章照「PM 共通草稿」第 2 節重整（`know`：你是誰／怎麼載入／接手／工單與協作／模型／回報與通道；`do`：收到需求／開單拆單／派工／監看／核對與審查／開 PR 與合併／收尾；`judge`；`dont`；`capabilities`）。
- **共用一份**：`team` 沿用 `worker-team`（新檔 `atelier/worker-team/pm/v1.md`，id `atelier:worker-team-pm@v1`）；Mac 與 worker 的差異（通道、真機、怎麼請本人接手、工作檔在哪）放**環境說明**，各一份；各專案特有的做法留在該 repo 的 `AGENTS.md`。
- **17 條決定的落點**逐條寫在 design（harness 哪一章／環境說明／各 repo 的 `AGENTS.md`）。
- **寫法與格式**（B-15／B-16）：harness 只寫能力（回報 PMO、開關隊員、監看隊員……），不寫工具名；模型只寫「照 PMO 的模型輪換表」；同時進行中的 b／c 上限只寫「照工單規則」。格式檢查（`tests/unit/test_atelier_harness.py`）跟著補：已知能力名清單、v1 的關鍵句、工具／模型／位址的黑名單。
- **載入改成按 agent**（PMO 抓到的高）：v0 把 harness 寫進**使用者層級**指示檔，同一台機器、同一個使用者的每個 agent 都會載入（worker 的隊員、Mac 的 PMO／別 PM／隊員都被套上 PM 規矩）。v1 起改由**開 agent 的一方在啟動那一個 agent 時交付**（opencode：`instructions` 每 process 設定；claude：`--append-system-prompt-file`），只有 PM 角色拿 PM harness；使用者層級檔案不再用來交付 v1。v0 在 worker 上的殘留要停 install 並清理（過渡步驟）。
- **交付與工具的配套**（第二輪審查 M2～M4）：`atelier render`（把 harness＋環境說明交給啟動方，**只寫 `--out`**）；`install` 對 `v1` 的 pm harness **預設拒絕、要 `--global`**（不然手動 install 仍可把 PM 規則裝回使用者層級，擋不住舊習慣）；`agora continue` 的 `--harness-file`＋`Launch.env`（opencode 的 per-process 設定只能走環境變數，`Launch` 現在沒有這個通道）。
- **M0 的協定教訓**（PMO 2026-10-18 指示寫入）：**收發的紀律**（收訊不指定頻道、只有送訊才指定頻道；身分在每個工作單位唯一、狀態目錄持久；回報目錄狀態要實證；閒著用阻塞等待、逾時拉長）寫進 `know`「回報與通道」；**關隊員前的確認**（先中斷、確認沒有長時間的指令還活著；只關畫面不會停掉 session，共用服務重啟會叫醒；來源是 PM-HANDOFF 2026-10-18 下午的教訓）寫進「收尾」；能用工具擋的列成「**要 Angareion 配合**」清單（`--channel` 警告與 ETag 由 #96 涵蓋；新的狀態目錄警告、`wait` 的預設逾時另列）。
- **已定（PMO 2026-10-18）**：id／team 沿用 `worker-team`（選項 1）；`capabilities` 用中性名 `channel`／`team`／`monitor`（選項 I）；環境說明放環境那一側（選項 A）。

## Capabilities

### New Capabilities

（無。）

### Modified Capabilities

- `atelier`：harness 檔的格式（內容限制：只寫能力、不寫工具名；模型只寫「照輪換表」）、能力需求（跨環境共用的職務可以用中性能力名）、v1 職務（PM，有隊員的 PM；Mac 與 worker 共用）、**按 agent 交付 harness（不是使用者層級；PM harness 不給非 PM 角色）**、**`render`（產生 agent 指示檔）**、**`install` 對 v1 的 pm harness 預設拒絕（要 `--global`）**——delta 在 `specs/atelier/spec.md`。

## Impact

- 本 change 的產出（MyAiStorage，公開、無私人資訊）：`openspec/changes/atelier-pm-harness-v1/`（proposal／design／tasks／specs）、`harness/pm-v1.md`（v1 草稿）。
- 未來實作（不在本 change，見 tasks.md）：`atelier/worker-team/pm/v1.md`（v1 定稿）、`tests/unit/test_atelier_harness.py`（格式檢查補強）、`docs/atelier/harness.md`（一頁格式說明補 v1）、**`atelier render`**（把 harness＋環境說明交給啟動方）、**`install` 的守門**（v1 的 pm harness 預設拒絕、要 `--global`，見安裝 requirement）、**`agora continue --harness-file`＋`Launch.env`**；交付＝啟動方（worker 的 sidecar、Mac 的開 pane 指令／`agora continue`）帶參數——**預設不用 install、不寫使用者層級檔案**（`--global` 是唯一的全域例外）。
- 跨 repo（不在本 change；列出來源）：各 repo 的 `AGENTS.md` 收「附錄 A 的專案特有內容」（各自的 PM 帶）；PMO 那一側的工單規則（PM 開的 d 不用附議的措辭）與模型輪換表（B-14／B-15 的細節）由 PMO 排。
- 不含：程式碼變更；v0 檔的修改（append-only）；期 3 的 Atelier 本體與多職務載入；模型輪換政策本身。
- 私人資訊：本 change 與 harness MUST NOT 含帳號、額度、憑證名稱、其他專案的名字、機器位址、模型輪換表、第二大腦內容（公開 repo）。
