## 0. 前提（不是 PR）

- 本 change（設計）先經非作者審查、PMO 拿給本人看、定案（第 1 節）之後才動工；**v1 的內容**已經在 `harness/pm-v1.md` 的草稿裡，實作＝把它定稿進樹＋補檢查。
- 來源：PMO 的「PM 共通的工作方式」草稿（第 1、2 節）＋本人 2026-10-18 對附錄 B 的 17 條決定（B-1～B-17）＋現行 `atelier/worker-team/pm/v0.md`。**草稿的附錄 A（各專案特有的做法）不進 harness**（B-17）。
- **已定**（PMO 2026-10-18）：D3 選項 1、D5 選項 A、D6 選項 I；**載入＝按 agent 交付**（D4，機制建議選項 A）。
- **不動**：v0 檔（append-only）；**PR 1 不動 `src/`**（只動 `atelier/` 樹、`tests/`、`docs/`）；PR 3 會動 `src/atelier/install.py`、`src/agora/cli.py`、`src/agora/agents/*.py`；`~/.claude/settings.json`、專案自己的 `AGENTS.md` 一律不碰。
- 公開 repo：實作與文件的內容照 design「私人資訊」的約束（不寫帳號、額度、憑證名稱、其他專案的名字、機器位址、輪換表、第二大腦）。

## 1. 定案（要 PM／PMO）

- [x] 1.1 **D4 的交付機制**：建議選項 A（opencode `instructions` 每 process 設定；claude `--append-system-prompt-file` ＋ `--system-prompt-snapshot off`）；未選 B／C／D（見 design D4 的實測）
  - 出處：PR #95 設計經 PMO 代 merge（本人 2026-10-18 授權）；實作 `src/agora/agents/claude.py:1088-1090`、`src/agora/agents/opencode.py:1603-1604`；MyLinuxPool#235
- [x] 1.2 **Mac 的 PM 由誰交付**：開 pane 的指令（本人／PM 的腳本）＋`agora continue`；腳本誰維護
  - 出處：不適用：Mac 也改用各環境的隊員控制服務（sidecar）交付，由 MyPMO#77 取代（另見 MyLinuxPool#320）
- [x] 1.3 `atelier render` 的介面（`--append`／`--out`、退出碼）與 `install` 的去留（建議：留著給 v0／全域，v1 不用）
  - 出處：PR #101（render）、PR #102（install 守門：`v>=1` 的 pm 沒給 `--global` 就 exit 1）；`tests/unit/test_atelier_cli.py:1380,1390`

## 1b. 部署前置（H1：**PR 1 之前或同一個部署批次**；跨 repo，PMO 排）

worker 的開機單位跑的是**不指定版本**的 `atelier install`（＝latest）；PR 1 一合併，latest 就是 v1，下一次重啟會把 v1 寫進使用者層級、所有 agent 都拿到。所以這一節 MUST 比 PR 1 早（或同一批次）。

- [x] 1b.1 **（建議）把開機單位 pin 到 `@v0`**：`atelier install atelier:worker-team-pm@v0 --agent claude --agent opencode`（一行改動；重啟只裝 v0，不會把 v1 裝進使用者層級）
  - 出處：MyLinuxPool#219「Pin the atelier install to @v0 at boot」，2026-10-18 merge，早於 PR #98
- [x] 1b.2 **（交付機制就緒後）移除 install**：開機單位只做驗證（或整個刪掉）；遇到 `install` exit 1 **忽略＋記一行警告、不擋開機**（照 MyLinuxPool#130 的契約）
  - 出處：MyLinuxPool#282（`start.d/20-atelier` 不再 install，只剩唯讀的 `atelier show`）；MyLinuxPool#216 的 2026-10-23 實測
- [x] 1b.3 備案：1b.1 做不到時，PR 1 不得合併；PR 1 合併後到 1b.1 完成前 **worker 不得重啟**（不建議依賴人記——M0 的教訓）
  - 出處：不適用：前提是「1b.1 做不到」，但 1b.1 在 PR #98 合併前就完成了
- [x] 1b.4 部署檢查清單：合併 PR 1 前確認（1）開機單位的版本已 pin 或 install 已移除、（2）worker 不會在窗口內重啟、（3）install 失敗的處理是「忽略＋警告」
  - 出處：PR #98 合併前 PMO 留言已確認 #219 merge、沒有「沒釘版本又還在跑」的 worker

## 2. MyAiStorage：v1 職務檔與格式檢查（PR 1；**在 1b 之後或同一批次**）

**範圍**：`atelier/worker-team/pm/v1.md`（新；內容＝本 change 的 `harness/pm-v1.md` 定稿）、`tests/unit/test_atelier_harness.py`（補強）、`docs/atelier/harness.md`（一頁格式說明）。合併後：`atelier show atelier:worker-team-pm` 讀到 v1；格式檢查綠；`@v0` 仍可讀。

- [x] 2.1 把草稿搬進 `atelier/worker-team/pm/v1.md`（frontmatter 的 `id`／`team`／`version` 照 D3 的定案＝選項 1；正文照草稿）
  - 出處：PR #98（commit `666133c`）；`atelier/worker-team/pm/v1.md` 與本 change 的 `harness/pm-v1.md` 內容相同
- [x] 2.2 格式檢查補強（design D7 的清單）：更新已知能力名清單（D6 定案的 `github`／`channel`／`agora`／`team`／`monitor`）、「審查一律非作者」擴到所有 pm 版本、v1 的關鍵句（`不是權限`、`照 PMO 的模型輪換表`、`不指定頻道`、`長時間的指令`——**四段聯集**：`know`＋`do`＋`judge`＋`dont` 合起來各出現一次，不要求每段都有）、工具名／模型名／其他專案名／機器位址／家目錄路徑（`/Users/`、`~/`）的黑名單（只驗 `v>=1` 的正文，不含 `capabilities` 子節名與 frontmatter；比對前先遮掉 `CLAUDE.md`／`AGENTS.md` 這兩個檔名；`/tmp` 這種通用系統路徑不算）、同一職務共用一份的檢查（**只警告、不擋**）
  - 出處：PR #98；`tests/unit/test_atelier_harness.py:75`（`KNOWN_CAPABILITIES`）、`:88`（`PM_KEY_SENTENCES`）、`:611`（`test_every_pm_harness_keeps_reviews_by_non_authors`）
- [x] 2.3 改壞驗證：2.2 的每一條各一個注入（拿掉關鍵句、塞工具名、塞模型名、塞 IPv4、塞 `~/` 路徑、動到 v0 的 bytes）都要紅；**共用一份那條是警告**：注入後 assert 有 warning、不是 error；逐項記在 PR 說明
  - 出處：PR #98；`tests/unit/test_atelier_harness.py:563`（`V1_MUTATIONS`）配 `:596`（`test_the_checker_catches_each_v1_violation`）
- [x] 2.4 文件：`docs/atelier/harness.md` 補 v1 的能力名、「按 agent 交付」「v0 已被 v1 取代」；`src/atelier/harness.py` 的註解（若有提到已知能力名）跟著更新
  - 出處：PR #98；`docs/atelier/harness.md` 已寫 v1 的能力名、按 agent 交付、`v0` 被 `v1` 取代
- [x] 2.5 測試：`uv run pytest -q tests/unit/test_atelier_harness.py`；`openspec validate atelier-pm-harness-v1 --strict --no-interactive`
  - 出處：PR #98 內文記了 `tests/unit/test_atelier_harness.py` 與 validate 的結果；歸檔前 #241 重跑
- [x] 2.6 歸檔：`openspec archive atelier-pm-harness-v1`（把 delta 併進主 spec）——照例在實作 PR 合併之後
  - 出處：#241（本次歸檔；先校正 delta 再 archive）

## 3. MyAiStorage：`atelier render`（PR 2）

- [x] 3.1 `atelier render <id>[@v<N>] [--append <檔>] --out <檔>`：讀 harness（不指定版本＝latest）、可選附加環境說明、**原子寫入**；exit 0／1（用法、查無、**`--append` 檔不存在**）／2（複本、讀取、寫入失敗）；訊息不含內文
- [x] 3.2 測試：整份內容、`--append` 的順序與分隔、latest／`@v<n>`、查無 id → 1、`--append` 檔不存在 → 1 且 `--out` 沒建立、寫不進去 → 2、不碰使用者層級檔案與 harness 樹
- [x] 3.3 改壞驗證：拿掉版本解析、`--append` 內容漏掉、寫入不是原子、把內容整理過——各要紅

## 3b. MyAiStorage：`install` 的守門與 `agora continue` 的交付（PR 3）

- [x] 3b.1 `install`：`v>=1` 的 `role: pm` harness 沒給 `--global` → exit 1（訊息指向 `render`／啟動方交付）、目標檔不動；給 `--global` → 成功且 stderr 有警告；`render` 查無 id／版本 → exit 3（讓呼叫方分辨查無與用法錯）
- [x] 3b.2 測試釘住 3b.1 的兩條路徑（含「什麼都沒寫」與「警告有印」）以及 render 查無 id／版本 exit 3
- [x] 3b.3 `Launch.env: dict`（預設空）；`src/agora/cli.py` spawn 時 `{**os.environ, **launch.env, "PWD": …}`；**不改 `os.environ`**
- [x] 3b.4 `agora continue --harness-file <檔>`：claude → argv 加 `--append-system-prompt-file` ＋ `--system-prompt-snapshot off`；opencode → `Launch.env` 的 `OPENCODE_CONFIG_CONTENT` 只帶 `instructions`（**與檔案設定合併**，實測；不要把整份設定重貼、把憑證帶進環境）
  - 觀察：`src/agora/agents/opencode.py:1604` 會覆蓋呼叫方原本帶進來的 `OPENCODE_CONFIG_CONTENT`，不是合併；現在是覆蓋，#242 決定要不要改成合併（delta 的 session-continue 寫現在的行為：整份取代）
- [x] 3b.5 測試：只有帶 `--harness-file` 的 process 吃得到；同一個 CLI、不帶的下一個 session 吃不到；檔案設定仍在（合併）由人工實測、無自動測試；自動測試只釘 CONTENT 只含 instructions 與路徑為絕對路徑
- [x] 3b.6 改壞驗證：把 env 改成改 `os.environ`（應紅）、CONTENT 重貼整份設定（應紅）、沒帶 `--harness-file` 卻注入（應紅）、install 守門拿掉（應紅）、`--global` 不印警告（應紅）、claude 少一個旗標（應紅）、render 查無改回 1（應紅）

## 4. 交付機制（跨 repo；PMO 排，MyPMO#86 會用到）

- [x] 4.1 worker：sidecar 的 `open`／`continue --headless` 注入（**欄位形狀已定：PMO 2026-10-18（PR #95 留言，與 MyPMO#80 命名定案對齊；細節 MyLinuxPool#216）——`open` 不收 `role` 欄位，角色由 AI 名字推出；預設 harness＝`atelier:<harness_series>-<角色>`；選填 `harness`（形狀 `{id, env_note}`）可覆寫；推出來的 harness 不存在 → 不注入、不算錯；秘書不由 sidecar 開、回 400** → `atelier render` → opencode `OPENCODE_CONFIG_CONTENT` 只帶 `instructions`（實測合併）／claude `--append-system-prompt-file` ＋ `--system-prompt-snapshot off`）；`render` 失敗**不開 agent**、回 4xx／5xx；`continue` 用 `open` 時的 session metadata，沒有就警告
  - 出處：MyLinuxPool#235、#237、#239 已 merge；MyLinuxPool#216 於 2026-10-23 結案（真機項目通過）
- [x] 4.1b 測試（sidecar 端，跨 repo）：`CONTENT` 只帶 `instructions` 時**檔案設定保留**（provider 還在、檔案的 `instructions` 還在）——釘住 M1 的實測
  - 出處：跨 repo，由 MyLinuxPool#332 收。sidecar 那一半有測試：MyLinuxPool `modules/sidecar/tests/test_sidecar_harness_delivery.py:98-103,133-135`（`OPENCODE_CONFIG_CONTENT` 加了 `instructions` 之後，provider／apiKey／permission 不變）；opencode 自己合併設定檔那一半只有 M1 實測、沒有測試，已併進 MyLinuxPool#332
- [x] 4.2 worker：開機單位在階段 A 已 pin `@v0`／移除 install（1b）；這個階段確認它只做驗證、`install` exit 1 時忽略＋警告、不擋開機
  - 出處：MyLinuxPool#282；MyLinuxPool#216 的 2026-10-23 實測
- [x] 4.3 Mac：開 pane 的指令與 `agora continue`（`src/agora/agents/*.py` 的 `start_native`＋`Launch.env`）帶交付；PMO／隊員不帶
  - 出處：不適用：Mac 改走 sidecar（MyPMO#77），「開 pane 的指令」這條路不存在；本 repo 的部分（`start_native`＋`Launch.env`、`agora continue --harness-file`）由 PR #102 完成，sidecar 用它的實作是 MyLinuxPool#239
- [x] 4.4 版本前提：opencode 的 `instructions` 在 2.0.29 不生效（v0 的實測）、2.0.32 生效（本輪實測）——worker 的 pin 若比 2.0.32 舊，要先升級並重驗；claude 的旗標在 worker 釘的版本上要重驗
  - 出處：MyLinuxPool#238（worker 的 opencode pin 到 2.0.32）；claude 的旗標在 worker 上 2026-10-23 實測生效（MyLinuxPool#216）；版本升級後的重驗交給 MyLinuxPool#358
- [x] 4.5 環境說明（worker／Mac 各一份，照 D5 的清單）：通道指令與逾時、關隊員的步驟、**開或接回多個 agent／session 一個一個來、中間隔幾秒（共用服務的 session 資料庫會 `database is locked`，2026-10-18 實際發生）**、真機與請本人接手、工作檔位置
  - 出處：worker：MyLinuxPool#241；Mac：MyLinuxPool#320；Mac 環境說明的合併權描述由 MyLinuxPool#401 收
- [x] 4.6 交付前的預檢（啟動方）：一次只起一個 agent／session、前一個起來後隔幾秒再起下一個；失敗時人工重啟共用服務的步驟寫進環境說明
  - 出處：跨 repo，由 MyLinuxPool#420 收。三份環境說明都有「循序啟動、間隔 ≥10 秒」；人工重啟共用服務的步驟還沒有，由 MyLinuxPool#420 補

## 5. 過渡清理（在 4.1 可用之後）

- [x] 5.1 清掉 worker 使用者層級的 v0 殘留（claude：刪 `~/.claude/rules/atelier-worker-team-pm.md`；opencode：刪 `AGENTS.md` 的 `<!-- atelier:worker-team-pm -->` 區塊）——人工、經 PM 授權；**順序在交付可用之後**，否則 PM 也失去 harness
  - 出處：worker 由 MyLinuxPool#216 驗過；Mac 由 PM 2026-10-25 唯讀查過（只看檔名）：Mac 上使用者層級的 rules 目錄沒有 atelier 的檔、opencode 的 `AGENTS.md` 不存在 → 沒有 v0 殘留
- [x] 5.2 在清理前，隊員被 v0 污染的暫時緩解：brief 明寫角色與範圍、PM 盯行為（**不是解法**）
  - 出處：不適用：前提（v0 殘留還在）已不存在（見 5.1）

## 6. 驗收（行為，不是測試）

- [x] 6.1 **在 5.1 清掉使用者層級殘留之後**，同一台機器同時開 PM 與一個隊員：**PM 的請求裡有 harness、隊員的沒有**（opencode 看假 upstream 的請求、claude 看啟動的 argv）；`~/.claude/rules/` 與 `~/.config/opencode/AGENTS.md` 逐字不變
  - 出處：替代驗證；嚴格版在 MyLinuxPool#332。worker：MyLinuxPool#216（2026-10-23，PM 與隊員的 `harness` 分別是 `atelier:worker-team-pm@v1`、`atelier:worker-team-impl@v1`）。Mac：team PM 在 sidecar 上實際用了一整天，算通過；PMO 的 pmo-pm 等 MyPMO#192 merge 後一起驗（不擋）
- [x] 6.2 PM 起來後的行為照 v1：收指示、開單、派工、監看、非作者審查、PR 與回報的格式都對得上；沒有碰憑證／真機／main
  - 出處：替代驗證：MyPMO#86（worker 上的 team PM 經 sidecar 開隊員、帶隊員做完一張單，2026-10-21 結案）；逐項比對 v1 未做
- [x] 6.3 沒驗到的、觀察到的偏差，記回工單與 PMO
  - 出處：MyLinuxPool#216 的留言（2026-10-20 的發現；10-23 沒驗到的項目轉 MyLinuxPool#332）

## 7. 跨 repo 收尾（不在本 PR；列出來源）

- [x] 7.1 各 repo 的 `AGENTS.md`：收草稿附錄 A 的專案特有內容（本 repo 的隔離規則、測試與工具怪癖、真機規矩……）；各自的 PM 帶
  - 出處：搬到 #243
- [x] 7.2 PMO 的 repo：工單規則補「PM 開的 d 不用另一位附議」（B-7）；模型輪換表收 B-14／B-15 的細節（選模型的順序、哪些只能短任務、審查不用哪些）
  - 出處：MyPMO `工單規則.md:41`（d 的核准）；`handbook/model-rotation.md`（審查不用 agy、短任務模型、依工作類型挑模型）；MyPMO#150
- [x] 7.3 隊員工具例外的說法（B-13）：不留個別例外；該團隊的文件與輪換表照同一套
  - 出處：照 arch 報告的建議：MyPMO#150（開發隊員全 Claude）重寫模型規矩時一起處理；逐字落點：`FATESAIKOU/MyPMO handbook/model-rotation.md:18`（「各團隊都照同一套，沒有個別例外。」）；PMO 不另外定
- [x] 7.4 工單規則／repo 摘要裡「進行中的 b＋c 上限」的舊數字（B-6）一起更正
  - 出處：MyPMO `工單規則.md:131`（上限 2 張）；本 repo `AGENTS.md` 已改（commit `e679943`）
