## 0. 前提（不是 PR）

- 本 change（設計）先經非作者審查、PMO 拿給本人看、定案（第 1 節）之後才動工；**v1 的內容**已經在 `harness/pm-v1.md` 的草稿裡，實作＝把它定稿進樹＋補檢查。
- 來源：PMO 的「PM 共通的工作方式」草稿（第 1、2 節）＋本人 2026-10-18 對附錄 B 的 17 條決定（B-1～B-17）＋現行 `atelier/worker-team/pm/v0.md`。**草稿的附錄 A（各專案特有的做法）不進 harness**（B-17）。
- **已定**（PMO 2026-10-18）：D3 選項 1、D5 選項 A、D6 選項 I；**載入＝按 agent 交付**（D4，機制建議選項 A）。
- **不動**：v0 檔（append-only）；**PR 1 不動 `src/`**（只動 `atelier/` 樹、`tests/`、`docs/`）；PR 3 會動 `src/atelier/install.py`、`src/agora/cli.py`、`src/agora/agents/*.py`；`~/.claude/settings.json`、專案自己的 `AGENTS.md` 一律不碰。
- 公開 repo：實作與文件的內容照 design「私人資訊」的約束（不寫帳號、額度、憑證名稱、其他專案的名字、機器位址、輪換表、第二大腦）。

## 1. 定案（要 PM／PMO）

- [ ] 1.1 **D4 的交付機制**：建議選項 A（opencode `instructions` 每 process 設定；claude `--append-system-prompt-file` ＋ `--system-prompt-snapshot off`）；未選 B／C／D（見 design D4 的實測）
- [ ] 1.2 **Mac 的 PM 由誰交付**：開 pane 的指令（本人／PM 的腳本）＋`agora continue`；腳本誰維護
- [ ] 1.3 `atelier render` 的介面（`--append`／`--out`、退出碼）與 `install` 的去留（建議：留著給 v0／全域，v1 不用）

## 1b. 部署前置（H1：**PR 1 之前或同一個部署批次**；跨 repo，PMO 排）

worker 的開機單位跑的是**不指定版本**的 `atelier install`（＝latest）；PR 1 一合併，latest 就是 v1，下一次重啟會把 v1 寫進使用者層級、所有 agent 都拿到。所以這一節 MUST 比 PR 1 早（或同一批次）。

- [ ] 1b.1 **（建議）把開機單位 pin 到 `@v0`**：`atelier install atelier:worker-team-pm@v0 --agent claude --agent opencode`（一行改動；重啟只裝 v0，不會把 v1 裝進使用者層級）
- [ ] 1b.2 **（交付機制就緒後）移除 install**：開機單位只做驗證（或整個刪掉）；遇到 `install` exit 1 **忽略＋記一行警告、不擋開機**（照 MyLinuxPool#130 的契約）
- [ ] 1b.3 備案：1b.1 做不到時，PR 1 不得合併；PR 1 合併後到 1b.1 完成前 **worker 不得重啟**（不建議依賴人記——M0 的教訓）
- [ ] 1b.4 部署檢查清單：合併 PR 1 前確認（1）開機單位的版本已 pin 或 install 已移除、（2）worker 不會在窗口內重啟、（3）install 失敗的處理是「忽略＋警告」

## 2. MyAiStorage：v1 職務檔與格式檢查（PR 1；**在 1b 之後或同一批次**）

**範圍**：`atelier/worker-team/pm/v1.md`（新；內容＝本 change 的 `harness/pm-v1.md` 定稿）、`tests/unit/test_atelier_harness.py`（補強）、`docs/atelier/harness.md`（一頁格式說明）。合併後：`atelier show atelier:worker-team-pm` 讀到 v1；格式檢查綠；`@v0` 仍可讀。

- [ ] 2.1 把草稿搬進 `atelier/worker-team/pm/v1.md`（frontmatter 的 `id`／`team`／`version` 照 D3 的定案＝選項 1；正文照草稿）
- [ ] 2.2 格式檢查補強（design D7 的清單）：更新已知能力名清單（D6 定案的 `github`／`channel`／`agora`／`team`／`monitor`）、「審查一律非作者」擴到所有 pm 版本、v1 的關鍵句（`不是權限`、`照 PMO 的模型輪換表`、`不指定頻道`、`長時間的指令`——**四段聯集**：`know`＋`do`＋`judge`＋`dont` 合起來各出現一次，不要求每段都有）、工具名／模型名／其他專案名／機器位址／家目錄路徑（`/Users/`、`~/`）的黑名單（只驗 `v>=1` 的正文，不含 `capabilities` 子節名與 frontmatter；比對前先遮掉 `CLAUDE.md`／`AGENTS.md` 這兩個檔名；`/tmp` 這種通用系統路徑不算）、同一職務共用一份的檢查（**只警告、不擋**）
- [ ] 2.3 改壞驗證：2.2 的每一條各一個注入（拿掉關鍵句、塞工具名、塞模型名、塞 IPv4、塞 `~/` 路徑、動到 v0 的 bytes）都要紅；**共用一份那條是警告**：注入後 assert 有 warning、不是 error；逐項記在 PR 說明
- [ ] 2.4 文件：`docs/atelier/harness.md` 補 v1 的能力名、「按 agent 交付」「v0 已被 v1 取代」；`src/atelier/harness.py` 的註解（若有提到已知能力名）跟著更新
- [ ] 2.5 測試：`uv run pytest -q tests/unit/test_atelier_harness.py`；`openspec validate atelier-pm-harness-v1 --strict --no-interactive`
- [ ] 2.6 歸檔：`openspec archive atelier-pm-harness-v1`（把 delta 併進主 spec）——照例在實作 PR 合併之後

## 3. MyAiStorage：`atelier render`（PR 2）

- [ ] 3.1 `atelier render <id>[@v<N>] [--append <檔>] --out <檔>`：讀 harness（不指定版本＝latest）、可選附加環境說明、**原子寫入**；exit 0／1（用法、查無、**`--append` 檔不存在**）／2（複本、讀取、寫入失敗）；訊息不含內文
- [ ] 3.2 測試：整份內容、`--append` 的順序與分隔、latest／`@v<n>`、查無 id → 1、`--append` 檔不存在 → 1 且 `--out` 沒建立、寫不進去 → 2、不碰使用者層級檔案與 harness 樹
- [ ] 3.3 改壞驗證：拿掉版本解析、`--append` 內容漏掉、寫入不是原子、把內容整理過——各要紅

## 3b. MyAiStorage：`install` 的守門與 `agora continue` 的交付（PR 3）

- [ ] 3b.1 `install`：`v>=1` 的 `role: pm` harness 沒給 `--global` → exit 1（訊息指向 `render`／啟動方交付）、目標檔不動；給 `--global` → 成功且 stderr 有警告
- [ ] 3b.2 測試釘住 3b.1 的兩條路徑（含「什麼都沒寫」與「警告有印」）
- [ ] 3b.3 `Launch.env: dict`（預設空）；`src/agora/cli.py` spawn 時 `{**os.environ, **launch.env, "PWD": …}`；**不改 `os.environ`**
- [ ] 3b.4 `agora continue --harness-file <檔>`：claude → argv 加 `--append-system-prompt-file` ＋ `--system-prompt-snapshot off`；opencode → `Launch.env` 的 `OPENCODE_CONFIG_CONTENT` 只帶 `instructions`（**與檔案設定合併**，實測；不要把整份設定重貼、把憑證帶進環境）
- [ ] 3b.5 測試：只有帶 `--harness-file` 的 process 吃得到；同一個 CLI、不帶的下一個 session 吃不到；檔案設定（provider、檔案自己的 `instructions`）都還在
- [ ] 3b.6 改壞驗證：把 env 改成改 `os.environ`（應紅）、CONTENT 重貼整份設定（應紅）、沒帶 `--harness-file` 卻注入（應紅）

## 4. 交付機制（跨 repo；PMO 排，MyPMO#86 會用到）

- [ ] 4.1 worker：sidecar 的 `open`／`continue --headless` 注入（`harness: {id, env_note}` 為準、`role` 是別名、衝突拒絕 → `atelier render` → opencode `OPENCODE_CONFIG_CONTENT` 只帶 `instructions`（實測合併）／claude `--append-system-prompt-file` ＋ `--system-prompt-snapshot off`）；`render` 失敗**不開 agent**、回 4xx／5xx；`continue` 用 `open` 時的 session metadata，沒有就警告
- [ ] 4.1b 測試（sidecar 端，跨 repo）：`CONTENT` 只帶 `instructions` 時**檔案設定保留**（provider 還在、檔案的 `instructions` 還在）——釘住 M1 的實測
- [ ] 4.2 worker：開機單位在階段 A 已 pin `@v0`／移除 install（1b）；這個階段確認它只做驗證、`install` exit 1 時忽略＋警告、不擋開機
- [ ] 4.3 Mac：開 pane 的指令與 `agora continue`（`src/agora/agents/*.py` 的 `start_native`＋`Launch.env`）帶交付；PMO／隊員不帶
- [ ] 4.4 版本前提：opencode 的 `instructions` 在 2.0.29 不生效（v0 的實測）、2.0.32 生效（本輪實測）——worker 的 pin 若比 2.0.32 舊，要先升級並重驗；claude 的旗標在 worker 釘的版本上要重驗
- [ ] 4.5 環境說明（worker／Mac 各一份，照 D5 的清單）：通道指令與逾時、關隊員的步驟、**開或接回多個 agent／session 一個一個來、中間隔幾秒（共用服務的 session 資料庫會 `database is locked`，2026-10-18 實際發生）**、真機與請本人接手、工作檔位置
- [ ] 4.6 交付前的預檢（啟動方）：一次只起一個 agent／session、前一個起來後隔幾秒再起下一個；失敗時人工重啟共用服務的步驟寫進環境說明

## 5. 過渡清理（在 4.1 可用之後）

- [ ] 5.1 清掉 worker 使用者層級的 v0 殘留（claude：刪 `~/.claude/rules/atelier-worker-team-pm.md`；opencode：刪 `AGENTS.md` 的 `<!-- atelier:worker-team-pm -->` 區塊）——人工、經 PM 授權；**順序在交付可用之後**，否則 PM 也失去 harness
- [ ] 5.2 在清理前，隊員被 v0 污染的暫時緩解：brief 明寫角色與範圍、PM 盯行為（**不是解法**）

## 6. 驗收（行為，不是測試）

- [ ] 6.1 **在 5.1 清掉使用者層級殘留之後**，同一台機器同時開 PM 與一個隊員：**PM 的請求裡有 harness、隊員的沒有**（opencode 看假 upstream 的請求、claude 看啟動的 argv）；`~/.claude/rules/` 與 `~/.config/opencode/AGENTS.md` 逐字不變
- [ ] 6.2 PM 起來後的行為照 v1：收指示、開單、派工、監看、非作者審查、PR 與回報的格式都對得上；沒有碰憑證／真機／main
- [ ] 6.3 沒驗到的、觀察到的偏差，記回工單與 PMO

## 7. 跨 repo 收尾（不在本 PR；列出來源）

- [ ] 7.1 各 repo 的 `AGENTS.md`：收草稿附錄 A 的專案特有內容（本 repo 的隔離規則、測試與工具怪癖、真機規矩……）；各自的 PM 帶
- [ ] 7.2 PMO 的 repo：工單規則補「PM 開的 d 不用另一位附議」（B-7）；模型輪換表收 B-14／B-15 的細節（選模型的順序、哪些只能短任務、審查不用哪些）
- [ ] 7.3 隊員工具例外的說法（B-13）：不留個別例外；該團隊的文件與輪換表照同一套
- [ ] 7.4 工單規則／repo 摘要裡「進行中的 b＋c 上限」的舊數字（B-6）一起更正
