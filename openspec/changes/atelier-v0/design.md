## Context

期 1 的 AI team（worker 上的 AI 團隊）要有一份規矩才知道怎麼接指示、在哪開 PR、怎麼回報、哪些不能碰（MyAiStorage#32）。本人裁定 harness v0 以 Atelier 的設計為前提：先設計 Atelier，再照設計寫 v0（MyPMO#19 留言，2026-10-10：「Atelier 的「設計」已經提前到期 1」）。Atelier 的定位是「harness 的構成宣告本身，也就是職能；和 Agora（session 管理）是不同的東西」（MyPMO#19 內文）。

本設計的依據與材料：

- **本人裁定（MyPMO#19，2026-10-10）**：「每個團隊各有一份自己的 harness，包含五大要素（know／do／judge／dont＋能力需求）；分工機制和團隊內規範屬於 know」；「先把能力（GPU、瀏覽器、git、容器內模型等）做出來，一次全部給預設 profile；要限制時再說，先留擴張點」；「秘書 harness：不搬，留在 MyAiEntry App 裡」。（同上留言，第 1／2／4 點；v0 範圍見 #32 內文。）
- **本人裁定（MyPMO#19，2026-10-11）**：「A2A 信道（Angareion）就是第五要素。」這與 2026-10-10 的「五大要素＝know／do／judge／dont＋能力需求」在字面上對不起來——這是本設計的第一個疑點（D1），照工單要求整理成解讀與建議，交本人決定，不自行定案。
- **本人的長期需求（MyAiStorage#32 留言，2026-10-11）**：harness 的格式要有兩個欄位——可接受的底線模型（floor）與優先模型清單（preferred models，有順序）；worker 上的 sidecar 會照這兩個欄位向 LLMGateway 租用模型，盡量滿足，滿足不了就停下並通知；ModelBroker 已留擴充點。
- **既有的四實體 header 約定（`docs/design.md`）**：每個項目帶同一種 header（YAML front matter，OKF v0.2），實體之間只靠 header 互相指（第 39 行）；ref 語法是 `entity ":" locator [ "@" rev ] [ "#" fragment ]`（第 61-63 行），例子含 `atelier:reviewer@v3`（第 66 行）。Atelier 的項目因此用 `atelier:<名稱>@<版本>` 當識別。
- **sidecar 的現況（MyLinuxPool 的 `openspec/changes/sidecar-v5/`；文件 `modules/sidecar/docs/sidecar.md`）**：每台機器一個 daemon，對外四個動作（open／close／status／poke，只綁 `127.0.0.1`，不做認證；第 11-12 行）；狀態揮發（第 26 行）；`open` 的欄位有 `id`、`agent`（`claude-code`／`opencode`）、`workdir`、`session`（agora id 字串＝接續，省略＝新開）、`subscription`（選填）、`input`（選填；「第一則輸入；排進佇列等 agent 起來再送，**不會**讓 open 失敗」，第 103 行）。
- **舊 sidecar 設計已定的接法（MyAiStorage branch `aicompany-p1`，未合併、當依據）**：「Sidecar 與 Atelier 的對接 Seam 已固定：**Sidecar 僅負責在 agent 啟動 prompt 出現後，將 harness 檔案全文無損作為第一則輸入送入**，不對其內容進行解析、摘要或修改」（`aicompany-p1:docs/sidecar.md` 第 283 行）；設定欄位 `teams.<id>.harness_path`（第 115 行）；載入時機「agent 啟動並就緒（prompt 出現）後」（第 29 行）。這個 seam 在 v5 的對應長相是 `open` 的 `input`（排隊等 agent 起來再送）。
- **角色的依據（MyPMO handbook，private）**：`handbook/roles.md`（PM 一節：派工、一張單一個 branch／worktree、審查非作者、不自己寫程式、回報、安全界線）、`handbook/collaboration.md`（派工到合併、回報、安全界線）。本設計只用自己的話整理成通用規則，不原文貼上。

約束與邊界（本人指示，經工單）：

- **本單只做設計**；程式、sidecar、部署都不動（MyAiStorage#32 的 tasks 只是之後的步驟）。
- **不要定這兩件**：code agent 的正式啟動機制（沿用現況）、A2A 的正式設計（angareion-v5；原 MyPMO#23 已關，見 MyPMO#31）。只要把介面留好、不卡死。
- **公開 repo**：不寫帳號、額度、憑證名稱、模型輪換的具體表、第二大腦的內容。
- 設計與介面摘要由 PMO 拿給本人看，本人同意才實作。

## Goals / Non-Goals

**Goals：**

- 定 harness 的**格式**：檔案形式、frontmatter 欄位、五要素章節、寫到多細；一份 harness 的**識別與命名**；與 Agora Session 的關係。
- 定**版本**規則：`atelier:<名稱>@<版本>` 怎麼對上 `docs/design.md` 的 ref；升降版與一致性。
- 定**載入**：誰提供檔、放哪、怎麼交給 agent（claude-code 與 opencode 都要能吃）、什麼時候載入（open／continue）、版本固定與升級；載入的接法沿用已定的 seam（全文無損、第一則輸入），把它對到 v5 的欄位。
- 定**與 sidecar 的介面**（獨立一節）：`open` 要不要新增欄位、設定檔、錯誤情形；逐條列出要 MyLinuxPool 做的事。
- 寫出 **v0 職務草稿**（worker team PM）：PM 一般規矩為底＋worker 差異；公開、無私人資訊。
- 定**能力需求**的表示（含本人的 `models.floor`／`models.preferred` 兩欄）與預設 profile 的角色；誰負責滿足。
- 定**測試與驗收方式**（期 1 驗收要看到什麼才算 harness v0 有效；harness 檔本身怎麼驗），以及**與期 3 的銜接**。
- 把**要本人決定的事**編號列好（附建議），不要自己定案。

**Non-Goals：**

- 實作（程式、sidecar 欄位、映像部署、文件改寫都只列步驟）。
- A2A／Angareion 的正式設計與 sidecar 的 team 概念（angareion-v5；原 MyPMO#23 已關，見 MyPMO#31；sidecar 的 team 概念是 v5 的後續）。
- code agent 的正式啟動機制（本人未定案；本設計對「誰開 team、怎麼開」只留介面，列為未決 E6）。
- 三職務（PM／reviewer／impl）與 Atelier 本體（期 3，MyAiStorage#17）。
- 模型輪換政策本身（哪個模型給哪個角色）；harness 只留欄位（見 E4）。
- 秘書 harness（不搬，留在 MyAiEntry App）。

## Decisions

總覽：

| # | 決策 | 狀態 |
|---|---|---|
| D1 | 五要素與 Angareion 的落點：能力需求＝第五、Angareion 在其中（建議） | **要本人決定（E1）** |
| D2 | 一份 harness 檔＝一個職務；Markdown＋frontmatter（OKF）；五章 | 定 |
| D3 | 識別與命名：`atelier:<team>-<role>@<版本>`；檔名 `<名稱>.md` | 定（隊名待 E7） |
| D4 | 版本規則：`v<整數>`、單調遞增、`id` 與 `version` 一致 | 定 |
| D5 | 與 Agora Session 的關係：不混；Session 可選用 refs 註記 | 定 |
| D6 | 載入方式：全文原樣、作為第一則輸入（建議） | **要本人決定（E2）** |
| D7 | 載入時機與版本固定：每次 open（含接續）送一次；部署版為準 | 定（隨 D6） |
| D8 | 檔案來源與位置：MyAiStorage 版控；映像部署進 worker；`harness_dir` | 定（來源位置見 E3） |
| D9 | 能力需求：本文 H3 子節（子節名＝能力名、v0：`github`／`angareion`／`agora`）＋frontmatter 的 `models.floor`／`preferred`；預設 profile | 定（值見 E4） |
| D10 | 錯誤情形：`harness_not_found`／`harness_version_mismatch`／`harness_invalid`；什麼都沒開 | 定（隨 sidecar 介面 E5） |
| D11 | v0 職務（worker team PM）的內容 | 定（草案；單人暫定見 E8） |
| D12 | 測試與驗收：格式檢查、載入測試、期 1 端到端 | 定 |
| D13 | 期 3 銜接：哪些是 v0 暫定 | 定 |

### D1 五要素與 Angareion 的落點（要本人決定，見 E1）

兩句裁定的原文：

- MyPMO#19 留言（2026-10-10，第 3 點）：「**第一份職務**：要寫，這就是 Atelier 的一部分。每個團隊各有一份自己的 harness，包含五大要素（know／do／judge／dont＋能力需求）；分工機制和團隊內規範屬於 know。」
- MyPMO#19 留言（2026-10-11）：「裁定（本人 2026-10-11）：A2A 信道（Angareion）就是第五要素。」

**解讀甲（建議）：四要素＋能力需求是五要素；Angareion 是能力需求裡的第一個具名子節。**
harness 的五個章節是 `know`／`do`／`judge`／`dont`／`capabilities`；`capabilities` MUST 以能力名為 H3 子節（表示法見 D9），MUST 有 `angareion` 子節（信道、身分、誰提供 token 的部署邊界），Angareion 的**用法**（怎麼讀、怎麼回、閒時怎麼等）放 `know`。
後果：與 2026-10-10 的列舉完全一致；sidecar／worker 只需要一條「能力」處理路徑（環境提供、v0 不核銷）；10-11 那句被讀成「worker 溝通的答案是 Angareion，它屬於第五要素的內容」——如果本人的本意是「信道本身要獨立成一個要素」，這個讀法就打了折。風險：誤解本人。

**解讀乙：四要素＋Angareion 是五要素；能力需求併入 `do`（或當附錄）。**
五章是 `know`／`do`／`judge`／`dont`／`angareion`（通信規矩自成一個要素）；能力需求（GPU、瀏覽器、git、模型）寫進 `do` 章節的「你需要什麼才能做事」小節，或檔尾附錄。
後果：與 2026-10-11 的字面一致；通信（身分、channel、urgency、等待、回報）有一個顯眼的位置；但 2026-10-10 明確列出的「能力需求」被降級成不是要素，機器的能力清單要靠 frontmatter（`models` 等欄位），文件上兩套詞彙（要素 vs 欄位）容易混。風險：與 10-10 的列舉對不起來，要本人明確推翻一次。

**解讀丙：四要素＋能力需求是五要素；Angareion 不佔章節，橫貫 `know`（用法）與 `capabilities`（能力）。**
五章同甲，但「A2A 信道是第五要素」的兌現方式是：`capabilities` 裡 `angareion` 是 MUST，且 `know` 必須有一節講通信紀律；文件中把第五要素寫成「能力需求（通信＝Angareion）」。
後果：與甲幾乎相同（檔案結構一樣），差在措辭與「`angareion` 子節必填」是不是明寫；最少新增結構；但如果本人要的是「信道獨立成要素」，這個讀法會被認為是折衷而非照辦。風險：兩邊都不滿足。

**建議：甲。** 理由：（1）2026-10-10 的列舉精確，2026-10-11 的那句是在回答 MyPMO#19 待決 5「worker 之間怎麼溝通」（這是推論：該留言的語境是「Atelier（harness 的構成宣告）」那條待決清單的第 5 項），像是「溝通的答案是 Angareion」的補充；（2）A2A 信道要 token、身分、端點，與 GPU／git／模型同一軸（環境要提供的能力），放 `capabilities` 讓 sidecar／worker 的介面只有一條路徑；（3）實作上 Angareion 的「用法規矩」放 `know`、「能力宣告」放 `capabilities`，兩個位置都自然。**這一項不論選甲／乙／丙，格式的其他部分（frontmatter、版本、載入）都不變，只動章節名與 `angareion` 的落點。**

### D2 一份 harness 檔＝一個職務；檔案格式

- **一份檔一個職務**（例「worker team PM」）；一個團隊的 harness＝它那組職務檔（期 3 三份）。「每個團隊各有一份自己的 harness」讀成「每個團隊各有自己的一組職務檔」，不是所有團隊共用一份。
- **檔案**：UTF-8、Markdown；第一個區塊是 YAML frontmatter（OKF v0.2 對齊；不認得的欄位保留——`docs/design.md` 第 43 行）。本文是五個 H2 章節（D1 的結論；建議甲）：

| 章節 | 放什麼 |
|---|---|
| `know` | 要知道的事：你在哪、你是誰、工單與協作流程、Angareion 用法、名詞 |
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
| `team`、`role` | ✔ | 職務所屬；`role ∈ {pm, arch, impl, test}` |
| `version` | ✔ | `v<非負整數>`；MUST 與 `id` 的 `@` 版本一致（D4） |
| `models` | ✔ | `floor`（模型識別碼或 `null`）＋`preferred`（有順序清單，可空）——本人的兩個欄位（D9） |
| `generated`、`refs` | 選填 | OKF；`generated` 記作者與時間，`refs` 可指其他實體 |

- **寫到多細**：以「agent 照著做不會錯、人看得懂」為準；機器只讀 frontmatter（含 `models`；載入器不解析本文；D6）；`capabilities` 在本文、v0 沒有機器消費它（環境不核銷；D9）。本文用繁中，指令、欄位名與識別用原文。
- **取捨**：單檔（frontmatter＋本文）vs 拆多檔（frontmatter／本文分開）。單檔：好搬、全文送入 agent 時不用拼接、編輯直覺；多檔的好處（各章節獨立版控、機器可選讀）v0 不需要——期 3 Atelier 本體要拆再說。**採單檔。**

### D3 識別、命名與檔案命名

- **名稱**：`<team>-<role>`；`team` 用 `[a-z0-9][a-z0-9-]*`（英文小寫與 `-`）、`role` 用小寫職務字（`pm`／`arch`／`impl`／`test`）。**期 3 的 reviewer 職務對應 agent 角色字 `arch`（沿用現行命名）；impl＝實作隊員**。v0：`worker-team-pm`。
- **識別**：`atelier:<名稱>@<版本>`，例 `atelier:worker-team-pm@v0`——與 `docs/design.md` 的 ref 語法（第 61-63 行）與例子 `atelier:reviewer@v3`（第 66 行）同一形狀，任何項目都能用 `refs` 指到它。
- **檔名**：`<名稱>.md`（例 `worker-team-pm.md`）；版本只寫在 frontmatter 與識別裡，不放檔名（舊版靠 git 歷史）。
- **與 agent 位址是兩件事**：agent 名（Angareion 位址）照 `angareion-v5` 的文法 `<repo 名>-<pm|arch|impl|test>[-<id>]`（`openspec/changes/angareion-v5/specs/angareion/spec.md` 第 142 行），由環境（個人設定）給；harness 名稱不取代它。職務檔會寫「你的 agent 名由環境提供，收發一律用它」。
- 取捨：`<team>-<role>` vs 直接用 agent 名（`mylinuxpool-pm`）。前者把「職務」與「哪個 repo 的哪個 agent」分開，同一份職務可以給多個 repo 的團隊用；後者與 sidecar 的 id 慣例一致、但 harness 會綁死一個 repo。**採 `<team>-<role>`**（隊名 E7）。

### D4 版本規則

- `v<非負整數>`（v0、v1、v2…），單調遞增；**內容有變 MUST 換版本**（同版本內容不同＝錯誤）。不做 semver；期 3 有需要再擴充（`vN.M`）。
- `id` 的 `@` 版本、frontmatter 的 `version`、檔名三者由格式檢查保證一致（D12）。
- 讀者（sidecar）只**比對版本字串**，不驗內容；內容驗證在 MyAiStorage 的格式檢查（D12）——分工：Atelier 管內容，sidecar 管載入。
- 升級：改檔＋改 version（一個 PR，格式檢查綠）→ 部署（D8）→ 下一次 `open` 生效。正在跑的 team 不受影響（載入的文字已在對話裡；D7）。
- 降級／回滾：git 回退檔案＋重新部署；識別含版本，所以「回去指 v0」在介面上說得清楚。
- 取捨：整數版（v0／v1…）vs semver。整數：規則小、不會被讀成相容矩陣；semver：語意多但 v0 用不到。**採整數**，期 3 有需要再擴充。

### D5 與 Agora Session 的關係

- harness **不是** Session：不進 Agora 儲存、不經 Drive；Agora 不需要認識 Atelier（`docs/design.md` 第 39 行：實體之間只靠 header 互相指，不共用程式或儲存）。
- Session（可選）可以在 header 的 `refs` 記 `atelier:worker-team-pm@v0`，記「這顆 Session 在哪個 harness 下跑」；那是一個 ref 字串，Agora 不必理解它。**v0 不做自動寫入**（Agora 目前沒有這個動作）；列為期 3 的作者提議（見「作者加的項目」）。
- 載入的 harness 文字會成為對話的一部分、照常被 Agora 存——這是特性（記憶完整），不是混淆：檔本身仍是唯一權威來源，改規矩改檔不改對話。

### D6 載入方式（要本人決定，見 E2）

候選與取捨（兩種 agent＝claude-code 與 opencode 都要能吃）：

1. **寫進工作目錄的 `AGENTS.md`／`CLAUDE.md`（或副檔＋在檔案裡指一行）**：agent 原生就會讀。缺點：弄髒工作樹（PM 之後要開 PR，`git status` 多檔案；repo 可能已有同名檔要合併；接續時可能被改回）；opencode 讀 `AGENTS.md`、claude-code 讀 `CLAUDE.md`，要寫兩份或 symlink。
2. **第一則輸入（sidecar 讀檔→送全文）**：兩種 agent 行為一致；不碰工作樹；與 v5 的 `input`（第 103 行）直接對上；**舊設計的 Seam 就是這樣定的**（`aicompany-p1:docs/sidecar.md` 第 283 行：「將 harness 檔案全文無損作為第一則輸入送入…不對其內容進行解析、摘要或修改」）。缺點：佔對話；被壓縮後可能被稀釋；每次 `open` 重送會重複。
3. **agent 的 system prompt／設定參數**（claude-code 的字串旗標、opencode 的 `instructions`／`OPENCODE_CONFIG_CONTENT`）：語意最接近「規矩」（system 層）、不佔對話。缺點：兩種 agent 各自的旗標與注入方式不同（版本耦合，agent 改版就要跟）；字串可能經 argv（`ps` 可見）；sidecar 要改 driver。
4. **混合**：system prompt 放「載入指示」、內容留檔案（agent 自己讀）。

**建議：2（第一則輸入全文）**，理由：跨 agent 一致、不動工作樹、與已定的 seam 相同、v5 已有 `input` 欄位可用（最小介面）；把 3 列為期 3 候選（若稀釋問題真的出現）。**要本人決定（E2）。**

### D7 載入時機、版本固定與升級

- **時機：每次 `open` 都送一次**（新開與接續都送；送在一般 `input` 之前，見「與 sidecar 的介面」）。理由：v5 的狀態揮發（`sidecar.md` 第 26 行），sidecar 不記得「這個 agent 載過沒」；重送保證接續（例如額度換把重開）後規矩仍在最新上下文；文字不長，重複可接受。
- **替代（期 3）**：在 Session 或 sidecar 記「已載入的版本」，只在換版時送——要 sidecar 或 agora 有對應能力（現在沒有）。
- **版本固定**：`open` 請求帶 `harness: {name, version?}`；省略版本＝用部署中的版本；帶了版本而與檔案不符＝錯誤（D10）。**一次 open 載入的版本就是那次 run 的版本**；升級＝部署新檔＋下一次 `open`（或先 close 再開）。
- 取捨：接續時重送整份 vs 送「規矩未變」一句。整份重送：無狀態、保證內容；簡短提醒：省對話但依賴「之前真的載過」這個假設。**v0 送整份。**

### D8 檔案從哪來、放哪

- **內容來源（canonical）**：MyAiStorage 的 `docs/atelier/harness/<名稱>.md`（本 change 的草稿在 `openspec/changes/atelier-v0/harness/`，實作 PR 放進正式位置）。公開、git 版本控、與 Atelier 的設計同處。取捨見 E3（與「MyAiStorage 要不要承載 Atelier」的裁定相連：先放 `docs/atelier/`，不建新頂層目錄）。
- **worker 上**：映像建置時從釘住的 MyAiStorage sha（與 agora／sidecar 同一處 pin，見 `shared-configs/agora/tool-pins` 的既有做法；MyLinuxPool#52 留言）取檔，放進映像的固定路徑（建議預設 `~/.config/sidecar/harness/`，由單元決定）；容器啟動不需要連網（同留言）。sidecar 以設定的 `harness_dir` 讀（預設同路徑）。
- **另一條路**：呼叫方把全文直接放 `input`（sidecar 零改動；檔案不必在 worker）。當過渡（見「與 sidecar 的介面」的乙案）。
- 為何不讓 sidecar 上網抓：worker 啟動不連網（既有約定）；檔案要與映像一起被釘住版本、可離線審計。

### D9 能力需求與預設 profile

- **表示（定死一種）**：`capabilities` 在**本文**：一個能力一個 **H3 子節**，子節名＝能力名（用環境的 capability 名）；子節內用**一行 YAML（或清單）**寫參數，沒有參數就留空（不寫假參數、不寫散文——用途放 `know`／`do`，不在此重複）。**frontmatter 不放 `capabilities`**。v0 的能力集合：`github`（MyLinuxPool 單位 `gh` 的 capability 名；gh 本體與 git credential）、`angareion`、`agora`——與 MyLinuxPool 的 `shared-configs/*/unit.json` 一致（只讀確認）。**模型不是能力子節**：它是 frontmatter 的 `models`（消費端是 sidecar 的 ModelBroker 與 LLMGateway；`llmgateway` 是環境的能力單位、sidecar 用，agent 不直接用）。v0 的三個子節都沒有參數（值由環境決定）；參數語意等有需求再定。
- **語意**：能力由環境提供（worker 的 profile＋憑證）；**v0 不逐項核銷**（本人：先全給預設 profile、要限制時再說）——harness 宣告是「需求清單」，不是開關。**誰負責滿足**：環境能力＝MyLinuxPool（profile、映像、憑證）；模型＝LLMGateway（未來 ModelBroker 依 harness 的兩個模型欄位租用）；sidecar 只把環境給的憑證交給 agent（既有機制），不自己核銷。
- **本人的兩個模型欄位**（#32 留言）：

  ```yaml
  models:
    floor: <模型識別碼或 null>        # 可接受的底線模型
    preferred: [<模型識別碼>, ...]    # 優先清單，有順序；可空
  ```

  語意：租用時先照 `preferred` 的順序滿足；都不可用時，不得低於 `floor`；達不到就**停下並通知**（本人裁定）。識別碼的詞彙＝LLMGateway 的模型識別碼（等 ModelBroker 落地時定案——這是推論：現在沒有既定的詞彙表）。
- **預設 profile 的表示**：檔案**不寫** profile 名；「預設 profile」是 worker 端的部署概念（MyPMO#19 第 2 點）。期 2 的按部門限制由 worker profile 做（MyLinuxPool#5），harness 不動。
- **擴張點**：能力名開放（未知名稱＝格式檢查警告、不擋，讓能力清單能長）；參數 free-form；`models.floor`／`preferred` 是固定欄位。值放哪見 E4。

### D10 錯誤情形（載入時）

照 v5 的錯誤風格（400＋`{"error": {"code", "message"}}`；`sidecar.md` 第 44 行、第 177 行）：

| 情形 | HTTP／code | 行為 |
|---|---|---|
| `harness_dir` 下沒有該名稱的檔 | 400 `harness_not_found` | 什麼都沒開 |
| 請求的版本 ≠ 檔案的 `version` | 400 `harness_version_mismatch` | 什麼都沒開 |
| frontmatter 解析失敗、或缺 `type`／`id`／`version` | 400 `harness_invalid` | 什麼都沒開 |

- 錯誤訊息**不含 harness 內文**（比照「key 不進 log」的風格；`sidecar.md` 第 38-40 行）。
- 載入後 agent 沒照規矩做（模型忽略）：不是 sidecar 的錯誤，靠驗收看行為（D12）。
- 「沒有給 `harness` 欄位」＝不載入、照舊開（向後相容；v0 的介面是選填）。

### D11 v0 職務（worker team PM）的內容

以 PM 的一般規矩為底（依據：MyPMO handbook，private；用自己的話）：派工與回報、一張單一個 branch／worktree、審查非作者、不自己寫程式、工單與層級、安全界線、不繞過被擋的動作。加上 worker 上的差異：

- 用 Angareion 收指示與回報；閒著用阻塞的 `wait` 等（不要輪詢模型）；urgency 7 以上先處理；訊息不貼私人資訊。
- 在 worker 上開 PR（憑證由環境提供；不直接改 main；不碰別的 repo、不碰真機、不 ssh、不 dispatch）。
- 憑證不讀不印不寫進訊息／log／commit；被擋就回報，不換方式繞過。
- 回報格式：做了什麼、產出（branch／PR）、**測試原樣輸出**、沒驗到的、用的模型、要決定的（附建議）。
- **期 1 v0 的暫定**：隊上目前只有 PM 一個職務——收到的工作由 PM 自己做（含實作與測試），並在回報標明；期 3 有三職務後改回「不自己寫程式、非作者審查」（E8）。
- 取捨：以「PM 的一般規矩＋worker 差異」為底（同一套詞彙、期 3 直接長出三職務）vs 另寫一份 worker 專用的規矩（簡短但之後要合併兩份文件）。**採前者。**
- 公開性：不寫帳號、額度、憑證名、輪換表、MyBrain 內容。

草稿在 `harness/worker-team-pm.md`。

### D12 測試與驗收

- **格式檢查（MyAiStorage；本 repo 目前沒有跑 pytest 的 CI）**：pytest 讀 `docs/atelier/harness/*.md`——frontmatter 必填欄位、`id`↔`version` 一致、五章齊、`capabilities` 以 H3 子節列出能力、`models` 兩欄存在（值可空）；拿掉任一項要紅。改壞驗證逐項記在 PR 說明；要不要接 CI 見 tasks 1.5。
- **載入測試（MyLinuxPool）**：單元＝假 harness 檔＋假 driver——`open` 帶 `harness` → 第一則輸入是**全文（byte-for-byte）**；找不到／版本不符／壞檔 → 400 且**沒有**開 agent；接續（`session`）也送；改壞驗證（拿掉載入、改寫內文、不比對版本 → 測試紅）。整合＝真 agent 起一次，看第一個輸入（可用 fake／離線）。
- **期 1 驗收（MyLinuxPool#45 端到端；MyPMO#31）要看到什麼才算 harness v0 有效**：
  1. PM 的第一則輸入是 harness 全文（版本 `atelier:worker-team-pm@v0`）；
  2. PM 的行為出現 harness 的規矩：用 Angareion `read`／`wait`／`send`、在 worker 上開 PR、回報含 PR 連結與測試原樣輸出、沒有碰憑證／真機／主分支；
  3. 指示由 PMO 經 Angareion 發出、回報由 PMO 收到（整條路走通）；本人 merge PR 收尾。
- **harness 檔本身怎麼驗**：格式檢查（本節第 1 點）＋內容審查（非作者；比照設計審查）＋期 1 驗收時看行為。

### D13 期 3 銜接（v0 暫定 → 被取代）

| v0 暫定 | 期 3 |
|---|---|
| 只有 PM 一份職務；PM 自己實作與測試 | PM／reviewer／實作隊員三職務；非作者審查、PM 不自己寫程式 |
| 載入＝每次 open 送全文 | 載入的正式版（Atelier 本體；評估 system prompt／版本追蹤，E2） |
| `harness_dir`＋映像部署 | 由 sidecar／Atelier 的正式機制管理（多團隊、多版本） |
| Session 的 refs 不自動記 harness | 可選：自動註記（作者提議） |
| 隊名 `worker-team`（單一） | 部門／團隊命名（MyLinuxPool#5，期 2） |

## 與 sidecar 的介面

**現況（v5）**：`POST /agents` 的欄位＝`id`、`agent`、`workdir`、`session`（agora id 字串＝接續；省略＝新開）、`subscription`（選填）、`input`（選填，第一則輸入；排隊等 agent 起來再送；`sidecar.md` 第 74-104 行）。沒有 team 概念、沒有 harness 概念；狀態揮發。

**誰提供檔、放哪**：見 D8——MyAiStorage 版控；worker 映像在固定路徑；sidecar 讀 `harness_dir`。

**sidecar 怎麼交給 agent**：見 D6——全文原樣、作為第一則輸入；順序：**harness 先、呼叫方的 `input` 後**（呼叫方的 input 通常是「用 angareion 讀 channel X」這種工作提示）。sidecar MUST NOT 摘要、改寫、截斷或把內文寫進 log／status；只讀 frontmatter 的 `type`／`id`／`version` 做版本比對。

**`open` 要不要新增欄位（選項與取捨）**：

- **甲（建議）**：新增選填 `harness: {"name": "worker-team-pm", "version": "v0"}`（`version` 可省）。sidecar 讀 `<harness_dir>/<name>.md`、驗版本、載入。向後相容（不帶＝照舊）；錯誤情形有位置回（D10）；呼叫方不必自己拿檔案。
- **乙（過渡備援）**：不改 sidecar——呼叫方把 harness **全文放 `input`**（sidecar 不知道 harness 的存在）。零改動、MyLinuxPool 進度不卡期 1 驗收；但沒有版本驗證、沒有專屬錯誤碼、呼叫方要自己拿到檔案、全文要進 HTTP 請求。
- **丙**：改用設定檔——`~/.config/sidecar/config.json` 加 `teams`／`identities` 對 harness 的對應（每身分一個 `harness`；`harness_dir` 共用）。最接近「sidecar 啟動 team 時載入」的構想（檔一次設定、open 不用帶），但 v5 沒有 team／身分概念，要動的比甲多（設定形狀、與 open 的 id 對應）。

**建議**：v0 採**甲**（最小而完整）；**乙當過渡**——MyLinuxPool 還沒做甲之前，期 1 驗收先用乙把全文放 `input`（設計與 harness 檔不變，只有「誰把文字交給 sidecar」不同）。**要本人決定（E5）。**

**錯誤情形**：見 D10 表（`harness_not_found`／`harness_version_mismatch`／`harness_invalid`；400；什麼都沒開；訊息不含內文）。

**版本不符的定義**：請求帶了 `version` 而檔案 frontmatter 的 `version` 不同。請求省略版本＝用部署版（不報錯）。

**現況缺口（不是本單能解，列入未決 E6）**：sidecar 只綁 `127.0.0.1`——**誰**在 worker 上／怎麼呼叫 `open` 把 team 開起來還沒定（與 #52、#130 的後續相關；v5 沒有遠端入口，遠端控制的做法也還沒定）。本設計的介面在「人用本機 CLI 呼叫」與「之後有正式的啟動流程」兩種情形下都成立。

### 要 MyLinuxPool 做的事（逐條）

1. **sidecar：新增載入介面（甲案）**——`open` 選填 `harness {name, version?}`；新設定 `harness_dir`（預設 `~/.config/sidecar/harness`）；載入＝讀 `<harness_dir>/<name>.md`、驗 frontmatter（`type`／`id`／`version`）、把**全文原樣**作為第一則輸入（在 `input` 之前）；錯誤碼三個（D10）；不改內容、不進 log／status；單元測試＋改壞驗證（D12）。
2. **sidecar：更新 `docs/sidecar.md`**——欄位、`harness_dir`、載入順序、錯誤碼。
3. **worker 部署**——把 MyAiStorage 釘住的 sha 裡的 `docs/atelier/harness/worker-team-pm.md` 放進映像的 `harness_dir`（與 agora／sidecar 同一處 pin）；`install.sh --check` 驗檔案存在（不存在回 1）。
4. **不做／不要做**——harness 的內容驗證（在 MyAiStorage 的 CI）；team 概念；Angareion 整合（v5 的 sidecar 不含 Angareion；agent 自己用它讀信道）；把 harness 寫進工作目錄。
5. **（期 1 驗收前置）**——worker 上「開 team」的呼叫路徑要有人或有程式做（E6）；若採乙案過渡，期 1 用「把全文放 `input`」即可，不必等 1。
6. **其餘不變**——其餘環境能力（映像、憑證、LLMGateway 等）照 #52 的既有範圍，本設計不新增要求。

## 作者加的項目（本人未指示；帶給本人看）

以下不是本人的指示，是設計時作者提的；都保留，說明時要讓本人知道：

- `harness_dir` 與檔名 `<name>.md` 的形狀；`harness` 欄位的形狀（`{name, version?}`）與「不帶＝照舊」。
- 乙案（呼叫方把全文放 `input`）當過渡備援（「與 sidecar 的介面」）。
- `capabilities` 的表示法（H3 子節、子節內一行 YAML 或留空）與 v0 的能力集合 `github`／`angareion`／`agora`（D9）。
- 三個錯誤碼與 400 的選擇。
- 載入順序（harness 先於一般 `input`）與「每次 open 都送」。
- 版本只放 frontmatter（`v<整數>`、不做 semver）。
- `models` 放 frontmatter 頂層（而不是 `capabilities` 裡）；v0 的值留空。
- `role ∈ {pm, arch, impl, test}` 與 `team` 的字元規則（D3）。
- harness 名稱與 agent 位址是兩件事（D3）。
- 隊名 `worker-team`（D3；E7）。
- v0 檔的 `status: draft`；`generated` 欄位記設計者。
- 期 1 單人暫定（PM 自己做實作與測試並標明）。
- Session `refs` 記 harness id（期 3；v0 不做）。
- 格式檢查放 MyAiStorage 的 pytest、載入測試放 MyLinuxPool；tasks.md 1.4 的可選格式文件（`docs/atelier/harness.md`）。
- 內容來源先放 `docs/atelier/`（不建新頂層目錄）。

## Risks / Trade-offs

- [第一則輸入被壓縮稀釋] → 每 open 重送；文字保持短；期 3 評估 system prompt（E2）。
- [harness 檔在公開 repo] → 內容必須一般化（D11）；模型的值留空（E4）；寫作時逐節檢查，設計審查把關。
- [sidecar 依賴新欄位（甲案）] → 允許乙案過渡：期 1 驗收不被 MyLinuxPool 的進度卡死；兩案共用同一份 harness 檔與規矩。
- [版本固定靠部署] → 忘了更新部署＝舊版一直用：`install.sh --check` 驗存在；期 3 Atelier 本體管理；期 1 用單一團隊、影響小。
- [「第五要素」解讀未定（E1）] → 若最終選乙，章節名與 `angareion` 的位置要改（frontmatter、版本、載入不動）；格式檢查跟著改，成本小；先照甲寫。
- [worker 上「開 team」路徑未定（E6）] → 期 1 驗收前必須有答案；本設計的介面在「人手呼叫」與「正式啟動」下都成立。
- [sidecar 只綁 127.0.0.1] → 沒有遠端入口；遠端控制（PMO 從 Mac）現況只能靠隧道或本機代理，屬 #52／後續；不影響 harness 的設計。
- [每次 open 重送全文] → 對話重複、Agora 會存多次：可接受（文字短；公開內容）；期 3 用版本追蹤減少。
- [模型欄位先留空] → ModelBroker 落地前沒有租用依據（現況本來就是 LLMGateway 的 priority 排序）：期 1 不受影響；填值前先給本人看（E4）。

## Open Questions（未決事項；編號；附建議）

1. **E1（要本人決定）：五要素與 Angareion 的落點**——甲／乙／丙三種解讀見 D1。**建議甲**（能力需求＝第五；Angareion 是其中的通信能力，`know` 寫用法）。
2. **E2（要本人決定）：載入方式**——寫工作目錄的 AGENTS.md／CLAUDE.md、第一則輸入、system prompt 三條路見 D6。**建議：第一則輸入全文**（跨 agent 一致、不動工作樹、與舊設計的 seam 相同）。
3. **E3（要本人決定，低）：harness 檔的正式位置**——`docs/atelier/harness/`（建議；與「MyAiStorage 要不要承載 Atelier」相連，先不建新頂層目錄）vs 等承載裁定後放 `atelier/` vs 放 MyLinuxPool。**建議：先放 `docs/atelier/harness/`**，承載裁定後再搬（識別不變，搬移不影響 ref）。
4. **E4（要本人決定）：`models.floor`／`preferred` 的值放哪、可不可以公開**——公開 repo 不寫模型輪換表。**建議：格式留欄位、v0 的值留空（`floor: null`、`preferred: []`＝未指定）；期 3 ModelBroker 落地前，由本人決定哪些值可以寫進公開檔（模型名本身不是憑證，但「哪個角色用哪個模型」的順序可能不想公開）。**
5. **E5（要本人決定）：sidecar 介面採甲／乙／丙**——見「與 sidecar 的介面」。**建議甲（新增選填欄位）＋乙當過渡**。
6. **E6（要 PMO／PM 協調，不是本人的裁定）：worker 上「誰開 team、怎麼呼叫 open」**——現況缺口（sidecar 綁 127.0.0.1；#52／#130 的後續）。**建議：期 1 先用最簡做法（人在 worker 上或以本機 CLI／腳本呼叫 `open`），把正式做法留給 #52 的後續；本設計不因它改介面。**
7. **E7（低）：隊名**——v0 用 `worker-team`；期 2 部門命名（MyLinuxPool#5）再定。**建議：不阻塞，v0 就用。**
8. **E8（要本人確認）：期 1 單人暫定**——隊上只有 PM 時，PM 自己做實作與測試並在回報標明（見 D11）。**建議：同意（否則驗收做不了）；期 3 三職務取代。**
