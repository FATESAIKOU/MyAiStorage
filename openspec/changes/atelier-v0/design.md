## Context

本 change 是 #32 的設計，r2（2026-10-16）：本人看過 PR #81（r1）後給的方向修正，經 PMO 轉達。**仍然只做設計，不實作**。worktree 不變；公開 repo 不寫私人資訊。

**本人的原話與理解（經 PMO；本文件照這個理解改）**：

- **E1**：「Angareion 是 AiStorage 的要素，因為那邊也有儲存功能，儲存的東西是 adhoc 的 AI 之間的對話。」
  → 10-11 的「A2A 信道（Angareion）就是第五要素」講的是 **AiStorage 的要素**（Agora 存 Session；Angareion 存 AI 之間臨時的對話），**不是 harness 的要素**。harness 的五要素照 2026-10-10（know／do／judge／dont＋能力需求）；**Angareion 不是 harness 的章節**——capabilities 裡可以宣告要用 angareion，用法寫在 know。
- **E2**：「我以為 harness 就是一組虛擬資料夾，然後 AI 可以用某種方式讀 harness？（我考慮過直接實體擺資料夾，但感覺 AI 多起來會很混亂）」
  → harness 是 AiStorage 裡的一組**虛擬資料夾**（一個團隊／職務一組），AI 用一個**工具／指令**去讀（類似 agora／angareion）；**不是**把全文塞進第一則輸入、也**不是**在各台機器、每個 agent 的工作目錄擺實體資料夾。
- **E5**：「不用吧？應該說先思考前面兩個問題再決定載入時機跟載入方式？」
  → **暫不要求 sidecar 加 `harness` 欄位**；sidecar 盡量不改；載入方式在 r2 重新提選項（D8）。
- **E8**：同意（期 1 先由 PM 一個人做，並在回報標明）。

**r1 仍然有效的素材**：

- 四實體 header／ref 約定（`docs/design.md`）：每個項目帶 YAML front matter（OKF v0.2）、實體之間只靠 header 互相指（第 39 行）；ref 語法 `entity ":" locator [ "@" rev ] [ "#" fragment ]`（第 61-63 行），例子含 `atelier:reviewer@v3`（第 66 行）。
- **angareion 的讀法可以直接重用**（`openspec/changes/angareion-v5/design.md` §11）：「angareion 只讀本機複本（MyAiStorage 的 clone）……更新用 `git fetch`、讀檔用 `git show origin/main:<路徑>`——不依賴工作樹……**clone 與 fetch 走匿名 HTTPS**」；查無先 fetch、逾時與失敗沿用複本。Atelier 的讀取工具照這條路（同一份複本、同一種做法、零新憑證）。
- 本人 2026-10-11 的長期需求（MyAiStorage#32 留言）：harness 的格式要有兩個欄位——可接受的底線模型（floor）與優先模型清單（preferred models，有順序）。
- sidecar v5：`open` 的 `input`（選填）是第一則輸入、「排進佇列等 agent 起來再送」（MyLinuxPool `modules/sidecar/docs/sidecar.md` 第 103 行）；開場句走它，sidecar 不用改。
- PM 一般規矩（MyPMO handbook，private；用自己的話）。
- r1 的格式基礎（frontmatter 欄位、五章、`capabilities` 的 H3 子節、models 欄位）——r2 沿用，只換「放哪、怎麼讀、怎麼載入」。

**約束與邊界**：

- 本單只做設計；程式、sidecar、部署都不動。
- 不要定 code agent 的正式啟動機制（沿用現況）與 A2A 的正式設計（angareion-v5；原 MyPMO#23 已關，見 MyPMO#31）。
- 不碰 MyLinuxPool（只讀）。
- 公開 repo：不寫帳號、額度、憑證名稱、模型輪換的具體表、第二大腦的內容。

## Goals / Non-Goals

**Goals：**

- 定 harness 的**存放與定址**：AiStorage 的虛擬資料夾樹、`atelier:<team>-<role>@<vN>`、**版本怎麼管**、多個 AI 同時讀不會亂的理由（D3／D4／D6）。
- 定**讀取工具**的介面：指令形狀（對齊 angareion 風格）、讀到的形狀（整份／章節）、退出碼與錯誤訊號、權限與私密界線（D7／D10）。
- 定**載入方式**：固定開場句（與 MyPMO#33 的銜接）與選項（D8）；sidecar 盡量不改。
- 沿用並釘住**格式**（r1）：一份檔＝一個職務一個版本、frontmatter、五章、`capabilities` H3 子節、`models.floor`／`preferred`（D2／D9）。
- 寫出 **v0 職務草稿**（worker team PM）：PM 一般規矩＋worker 差異＋「先讀自己的 harness」（D11）。
- 定**測試與驗收**與**期 3 銜接**（D12／D13）。
- 把**要本人決定的事**編號列好（附建議）；E1／E2／E5／E8 標「本人已答覆（2026-10-16）」。

**Non-Goals：**

- 實作（工具、harness 樹、部署都只列步驟）。
- sidecar 的載入欄位（本人暫不要求；保留為未選選項）。
- A2A／Angareion 的正式設計（angareion-v5）；Atelier 本體與三職務（期 3）。
- code agent 的正式啟動機制、worker 上「誰開 team」的呼叫路徑（E6，現況缺口）。
- 模型輪換政策本身；harness 只留欄位（E4）。
- 秘書 harness（不搬，留在 MyAiEntry App）。

## Decisions

總覽：

| # | 決策 | 狀態 |
|---|---|---|
| D1 | 五要素＝know／do／judge／dont＋能力需求；Angareion 是 AiStorage 的要素、不是 harness 章節 | **本人已答覆（E1）** |
| D2 | 一份檔＝一個職務一個版本；frontmatter（OKF）＋五章；`capabilities` 是 H3 子節 | 定（r1 沿用） |
| D3 | 識別、命名與路徑：`atelier:<team>-<role>@<vN>` ↔ `atelier/<team>/<role>/v<N>.md` | 定（隊名 E7） |
| D4 | 版本：append-only 版本檔；舊版不可變、不刪；latest＝最大 N | 定（管法隨 E9） |
| D5 | 與 Agora 的關係：不混；harness 樹是 Atelier 自己的（不是 Session） | 定 |
| D6 | 存放位置：MyAiStorage 的 git（虛擬資料夾） | **要本人決定（E9）** |
| D7 | 讀取工具：新增 `atelier` 指令（`ls`／`show`） | **要本人決定（E10）** |
| D8 | 載入：固定開場句＋agent 用工具自讀 | **要本人決定（E11）** |
| D9 | 能力需求：宣告式 H3 子節＋`models.floor`／`preferred`；預設 profile | 定（值見 E4） |
| D10 | 讀取工具的錯誤情形：exit 0／1／2；fetch 失敗沿用複本 | 定（隨 D7） |
| D11 | v0 職務（worker team PM）的內容 | 定（草案；單人暫定已同意） |
| D12 | 測試與驗收：工具測試、格式檢查、期 1 端到端 | 定 |
| D13 | 期 3 銜接：哪些是 v0 暫定 | 定 |

### D1 五要素與 Angareion 的落點（本人已答覆，2026-10-16）

- harness 的五要素照 2026-10-10：`know`／`do`／`judge`／`dont`／`capabilities`（能力需求）。
- **Angareion 不是 harness 的章節**：它是 AiStorage 的要素（儲存 adhoc 的 AI 對話，與 Agora 存 Session 並列）。在 harness 裡，`capabilities` 宣告要用 `angareion`、用法（怎麼等、怎麼讀、怎麼回）寫在 `know`。
- r1 的三種解讀（甲／乙／丙）到此結束：**甲**（能力需求是第五）是結果；乙（Angareion 獨立成章）與丙（橫貫）不再考慮。

### D2 一份 harness 檔＝一個職務一個版本；檔案格式（r1 沿用）

- **一份檔、一個職務、一個版本**；一個團隊的 harness＝它那組職務的版本檔。
- **檔案**：UTF-8、Markdown；第一個區塊是 YAML frontmatter（OKF v0.2 對齊；不認得的欄位保留）。本文是五個 H2 章節：

| 章節 | 放什麼 |
|---|---|
| `know` | 要知道的事：你在哪、你是誰、怎麼讀這份 harness、工單與協作流程、Angareion 用法、名詞 |
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

- **寫到多細**：以「agent 照著做不會錯、人看得懂」為準；機器只讀 frontmatter（含 `models`；讀取工具不解析本文）；`capabilities` 在本文、v0 沒有機器消費它（環境不核銷；D9）。本文用繁中，指令、欄位名與識別用原文。
- **取捨**：單檔（frontmatter＋本文）vs 拆多檔——單檔好搬、好讀、好版本；期 3 Atelier 本體要拆再說。**採單檔。**

### D3 識別、命名與路徑

- **名稱**：`<team>-<role>`（`team` 見 D2 的字元規則；v0：`worker-team-pm`）。
- **識別**：`atelier:<名稱>@<版本>`，例 `atelier:worker-team-pm@v0`——與 `docs/design.md` 的 ref 語法（第 61-63 行）與例子 `atelier:reviewer@v3`（第 66 行）同一形狀；任何項目都能用 `refs` 指到它。
- **路徑（虛擬資料夾）**：`atelier/<team>/<role>/v<N>.md`（例 `atelier/worker-team/pm/v0.md`）；一個團隊一個資料夾、一個職務一個子資料夾、一個版本一個檔——就是本人說的「一組虛擬資料夾」。
- **與 agent 位址是兩件事**：agent 名（Angareion 位址）照 `angareion-v5` 的文法 `<repo 名>-<pm|arch|impl|test>[-<id>]`（`openspec/changes/angareion-v5/specs/angareion/spec.md` 第 142 行），由環境設定提供；harness 名稱不取代它。
- **取捨**：`<team>-<role>` vs 直接用 agent 名（會把 harness 綁死一個 repo）；資料夾樹（team／role）vs 扁平檔名（`worker-team-pm@v0.md`）——資料夾樹讓未來有團隊層共用檔（例 `atelier/worker-team/common/…`）的位置。**採 `<team>-<role>`＋三層路徑**（隊名 E7）。

### D4 版本怎麼管

- **版本檔 append-only**：一個版本一個檔（`v0.md`、`v1.md`…），MUST NOT 修改既有版本檔、MUST NOT 刪除；內容有變＝新增下一個版本檔。`version`、`id` 的 `@` 與檔名三者一致（D2／D3）。
- **latest＝同一個 role 資料夾裡最大的 N**；`atelier show <id>`（不給版本）讀 latest；`atelier show <id>@v0` 讀那一版。舊版永久可讀——這就是「不可變版本」。
- **不可變的理由**：多個 AI 同時讀不會亂（D6）；要追溯「某次工作照哪版做」時，版本還在（可搭配 Session 的 `refs`，D5）。
- **取捨**：append-only 版本檔 vs「單檔改內容＋git 歷史找舊版」。單檔：檔名固定、但工具要挖 `git log` 才拿得到舊版，且「v0 讀到的內容」依時間而變。append-only：檔案數量長（每個職務幾個小檔）、內容不變的保證是結構性的。**採 append-only。**
- 不做 semver；期 3 有需要再擴充。升級＝新增版本檔＋合併（PR）＋（可選）在開場句指定新版。

### D5 與 Agora Session 的關係

- harness 樹是 **Atelier 自己的**（虛擬資料夾）；**不是 Session**、不進 Agora 儲存、不經 Drive；Agora 不需要認識 Atelier（`docs/design.md` 第 39 行）。
- Session（可選）可以在 header 的 `refs` 記 `atelier:worker-team-pm@v0`，記「這顆 Session 在哪個 harness 的哪一版下跑」；那是 ref 字串，Agora 不必理解。**v0 不做自動寫入**；列為期 3 的作者提議。
- 讀取工具「只讀 harness、不碰 Session」；兩邊的設定與複本可以共用（D7），但語意不混。

### D6 存放位置（要本人決定，見 E9）

三個選項與取捨（本人要求的比較面向：版本、審查、公開性、離線、與 Agora／Angareion 的存取共用程度）：

- **甲（建議）：MyAiStorage 的 git**——虛擬資料夾樹 `atelier/…` 放這個 repo，讀取工具走本機 clone ＋ `git fetch`／`git show origin/main:`（與 angareion 讀 channel 設定同一種做法）。
  - 版本：git 歷史＋append-only 版本檔（D4）；審查：PR（與設計／程式同一套；本人同意才 merge）；公開性：內容本來就要公開（規則不放私人資訊）；離線：clone 一次、之後離線可讀（fetch 失敗沿用複本）；共用：**與 angareion 共用同一份本機複本／同一種匿名 HTTPS 讀法——零新憑證**；worker 端 angareion 能力已經需要這份複本。
  - 缺點：每次更新要 PR（但這正是審查要的）；Drive 派的人要用 git 看（既有習慣）。
- **乙：Google Drive**（與 Agora 同一套 rclone 存取；Atelier 自己的資料夾樹）。
  - 版本：檔案版本＋檔名 `vN`；審查：**沒有 PR**（只有檔案，誰改的都難追）；公開性：Drive 是私人儲存（內容公開無妨，但沒有好處）；離線：可借 Agora 的鏡像做法；共用：與 Agora 共用 rclone／憑證與快取機制。
  - 缺點：**沒有 PR 審查**（本設計的前提是設計經審查）；版本的可定址性弱；每台要讀的機器都要有 Drive 憑證與 rclone（Mac 與其他裝置要各自設定）。
- **丙：實體複本**（映像／工作目錄各放一份）——本人已排除（「AI 多起來會很混亂」）；工作樹被污染、版本會漂移、每台各一份。**不建議**，列出當對照。

**多個 AI 同時讀不會亂的理由**（選甲時全部成立；選乙時除「共用 clone」外同理）：

1. **唯讀工具**：`atelier` 沒有寫入介面（D7）；讀者碰不到狀態。
2. **版本不可變**：append-only（D4）；`v0` 讀到的位元組永遠一樣。
3. **沒有共享可寫狀態**：不像工作目錄（會被 commit、改檔、切 branch）；harness 樹沒有人邊讀邊改。
4. **各讀各的**：每個 agent 讀自己機器上的本機複本；同名 id 的內容由 git 保證一致。
5. **不在工作目錄放實體複本**：工作樹乾淨；AI 再多也只是多幾個讀者。

**建議：甲**（與本人「AiStorage 的虛擬資料夾」的說法一致；審查與版本都最強；共用 angareion 的讀法＝零新憑證）。**要本人決定（E9）。**

### D7 讀取工具介面（要本人決定，見 E10）

選 `atelier` 工具放哪、長什麼樣（風格對齊 angareion）：

- **甲（建議）：新增 `atelier` 指令**（與 agora／angareion 同一個 distribution：`pyproject.toml` 加一個 entry point、新模組 `src/atelier/`）。
  - `atelier ls [--json]`：列職務與版本（例 `atelier:worker-team-pm@v0`、`@v1`）。
  - `atelier show <id>[@<版本>] [--section know|do|judge|dont|capabilities] [--json]`：印 harness。**預設整份原樣**（含 frontmatter——版本與識別在裡面）；`--section` 只印該章節的內容（給不想整份進 context 的 agent）；`--json` 給程式（frontmatter＋各章節）。
  - **讀法**：本機複本（MyAiStorage 的 clone）＋ `git fetch` 節流更新（沿用 angareion §11：`git show origin/main:<路徑>`、匿名 HTTPS、查無先 fetch、fetch 失敗 stderr 警告一次並沿用複本）；不依賴工作樹。
  - **退出碼**（對齊 angareion／agora 的簡化版）：0 成功；1 用法錯或找不到（id／版本／章節）；2 讀取失敗（複本不存在、檔案壞、git 壞）。沒有 3（outbox）／4（鎖）／5（agent）——它是唯讀的。
  - 權限與私密界線：工具**只讀**；寫入只有一條路——對 MyAiStorage 開 PR（審查＋本人同意）；**agent 執行工作時 MUST NOT 改 harness**（要改就回報提議，由人或 PM 決定再開 PR）；harness 的內容照公開規則（不放私人資訊）。
  - 取捨：多一個指令（多一個入口），但語意正確、與 angareion 同一家族、之後 Atelier 本體就從這裡長。
- **乙：併進 `agora`**（例 `agora atelier show …`）：不新增指令；但 agora 是 Session 工具，介面與退出碼表都是 Session 的，把 Atelier 塞進去會讓兩個實體糾纏（r1 D5：不混）。
- **丙：把 harness 當特殊 Session** 用 `agora show` 讀：重用 Agora 全部機制（含 Drive）；但 harness 不是 Session（要偽造 header／kind）、沒有 PR 審查、選 Drive 才有意義；不建議。

**建議：甲。** 工具放 MyAiStorage（與 agora／angareion 同 repo；worker 的套件來源同一個 pin）。**要本人決定（E10）。**

### D8 載入時機與方式（要本人決定，見 E11）

選項（都不寫進工作目錄）：

- **甲（建議）：固定開場句＋agent 自讀＋新指示前重讀。** 啟動 team 的呼叫方在 `open` 的 `input` 開頭放固定開場句（見「與 sidecar 的介面」）；agent 先 `atelier show` 讀 harness、再 `angareion` 讀指示；harness 的 `know` 要求「收到每個新指示前先重讀一次 harness」。sidecar 影響：**零**（用現有 `input` 欄位）；MyLinuxPool 影響：**零**（除了把 worker 的套件 pin 升到含 `atelier` 的 sha——既有流程）。
- **乙：固定開場句＋只在開頭讀一次。** 同甲，但不要求之後重讀；規矩改版後要等下次 `open`（新開或接續）才會被讀到。sidecar 影響：零。
- **丙（未選，保留）：sidecar 全文載入**（r1 的甲案：`open.harness` 欄位＋`harness_dir`＋全文作為第一則輸入）。本人 2026-10-16 暫不要求；清單與錯誤碼保留在「與 sidecar 的介面」，日後要選再照做。sidecar 影響：要改。

**建議：甲**（多一次便宜的指令、規矩改版即時生效、sidecar 不動）。**要本人決定（E11）。**

### D9 能力需求與預設 profile（r1 沿用）

- **表示**：`capabilities` 在**本文**：一個能力一個 **H3 子節**，子節名＝能力名（用環境的 capability 名）；子節內用一行 YAML（或清單）寫參數，沒有參數就留空（用途放 `know`／`do`）。**frontmatter 不放 `capabilities`**。v0 的能力集合：`github`（MyLinuxPool 單位 `gh` 的 capability 名；gh 本體與 git credential）、`angareion`、`agora`——與 MyLinuxPool 的 `shared-configs/*/unit.json` 一致（只讀確認）。**模型不是能力子節**：它是 frontmatter 的 `models`（消費端是 sidecar 的 ModelBroker 與 LLMGateway；`llmgateway` 是環境的能力單位、sidecar 用，agent 不直接用）。v0 的三個子節都沒有參數（值由環境決定）。
- **angareion 的落點（本人 2026-10-16）**：能力在 `capabilities` 宣告，用法（`wait`／`read`／`send`）在 `know`；**MUST NOT 自成章節**（D1）。
- **語意**：能力由環境提供（worker 的 profile＋憑證）；**v0 不逐項核銷**——harness 宣告是「需求清單」，不是開關。**誰負責滿足**：環境能力＝MyLinuxPool（profile、映像、憑證）；模型＝LLMGateway（未來 ModelBroker 依 harness 的兩個模型欄位租用）；sidecar 只把環境給的憑證交給 agent（既有機制）。
- **本人的兩個模型欄位**（#32 留言）：

  ```yaml
  models:
    floor: <模型識別碼或 null>        # 可接受的底線模型
    preferred: [<模型識別碼>, ...]    # 優先清單，有順序；可空
  ```

  語意：租用時先照 `preferred` 的順序滿足；都不可用時，不得低於 `floor`；達不到就**停下並通知**。識別碼的詞彙＝LLMGateway 的模型識別碼（等 ModelBroker 落地時定案——這是推論：現在沒有既定的詞彙表）。
- **預設 profile 的表示**：檔案**不寫** profile 名；「預設 profile」是 worker 端的部署概念（MyPMO#19 第 2 點）。期 2 的按部門限制由 worker profile 做（MyLinuxPool#5），harness 不動。
- **擴張點**：能力名開放（未知名稱＝格式檢查警告、不擋）；參數 free-form；`models.floor`／`preferred` 是固定欄位。值放哪見 E4。

### D10 讀取工具的錯誤情形

| 情形 | 退出碼 | 行為 |
|---|---|---|
| 正常 | 0 | 印 harness（或清單） |
| 用法錯（參數、章節名） | 1 | 只說明，不做任何事 |
| 找不到 id／版本 | 1 | 說明查無；查無時先 fetch 一次（同 angareion 的規則） |
| 本機複本不存在、檔案壞、git 壞 | 2 | 說明明確原因（複本不存在→提示 clone 的步驟，照 angareion 的訊息風格） |
| fetch 失敗（離線、網路） | 0（不變） | stderr 警告一次、沿用複本；**MUST NOT** 因此改變退出碼 |

- 錯誤訊息**不含 harness 內文**（只講 id／版本／路徑）。
- 工具 MUST NOT 有寫入動作、MUST NOT 需要憑證（匿名 HTTPS）。
- **（未選）sidecar 全文載入的錯誤表**（`harness_not_found`／`harness_version_mismatch`／`harness_invalid`；400、什麼都沒開）保留在「與 sidecar 的介面」，日後選丙再照做。

### D11 v0 職務（worker team PM）的內容

以 PM 的一般規矩為底（依據：MyPMO handbook，private；用自己的話）：派工與回報、一張單一個 branch／worktree、審查非作者、不自己寫程式、工單與層級、安全界線、不繞過被擋的動作。加上 worker 上的差異：

- **先讀自己的 harness**（新增）：用 `atelier show atelier:worker-team-pm` 讀；收到新指示前先重讀（D8 甲）；規矩以你讀到的那一版為準。
- 用 Angareion 收指示與回報；閒著用阻塞的 `wait` 等（不要輪詢模型）；urgency 7 以上先處理；訊息不貼私人資訊。
- 在 worker 上開 PR（憑證由環境提供；不直接改 main；不碰別的 repo、不碰真機、不 ssh、不 dispatch）。
- 憑證不讀不印不寫進訊息／log／commit；被擋就回報，不換方式繞過。
- 回報格式：做了什麼、產出（branch／PR）、**測試原樣輸出**、沒驗到的、用的模型、要決定的（附建議）。
- **期 1 v0 的暫定（本人已同意）**：隊上目前只有 PM 一個職務——收到的工作由 PM 自己做（含實作與測試），並在回報標明；期 3 有三職務後改回「不自己寫程式、非作者審查」。
- 公開性：不寫帳號、額度、憑證名、輪換表、MyBrain 內容。

草稿在 `harness/worker-team-pm.md`。

### D12 測試與驗收

- **讀取工具測試（MyAiStorage）**：單元＝假的 git／假複本——`ls`、`show`（整份 byte-for-byte、`--section`、`--json`）、版本解析（latest／`@v0`）、exit 1（用法、查無）／2（複本不存在）、fetch 失敗沿用複本且 exit 不變、**唯讀**（沒有任何寫入路徑）；改壞驗證（拿掉版本解析、`--section` 抓錯段、fetch 失敗回 2、把內容「整理」過再印——各要紅）。
- **格式檢查（MyAiStorage；本 repo 目前沒有跑 pytest 的 CI）**：pytest 讀 `atelier/**/v<N>.md`——frontmatter 必填欄位、`id`↔`version`↔檔名一致、五章齊、`capabilities` 以 H3 子節列出能力、`models` 兩欄存在（值可空）；**加上 append-only 檢查**（與 `origin/main` 的 merge-base 比對：既有版本檔的 blob 不得改變、不得刪除）。拿掉任一項要紅。改壞驗證逐項記在 PR 說明；要不要接 CI 見 tasks 1.5。
- **期 1 驗收（MyLinuxPool#45 端到端；MyPMO#31）要看到什麼才算 harness v0 有效**：
  1. `open` 的 `input` 開頭是固定開場句（讀 harness → 讀信道）；
  2. PM 的第一個動作是 `atelier show atelier:worker-team-pm`（拿得到全文、版本一致）；
  3. PM 用 Angareion `read`／`wait`／`send`；在 worker 上開 PR；回報含 PR 連結與測試原樣輸出；沒有碰憑證／真機／主分支；
  4. 指示由 PMO 經 Angareion 發出、回報由 PMO 收到；本人 merge PR 收尾。
  （MyLinuxPool 端的前置：worker 的套件 pin 含 `atelier` 指令；讀得到 harness 樹——與 angareion 共用同一份複本。）
- **harness 檔本身怎麼驗**：格式檢查（本節第 2 點）＋內容審查（非作者）＋期 1 驗收時看行為。

### D13 期 3 銜接（v0 暫定 → 被取代）

| v0 暫定 | 期 3 |
|---|---|
| 只有 PM 一份職務；PM 自己實作與測試（已同意） | PM／reviewer／實作隊員三職務；非作者審查、PM 不自己寫程式 |
| 載入＝固定開場句＋agent 自讀（E11） | 載入的正式版（Atelier 本體；可評估未選的 sidecar 載入／版本追蹤） |
| 版本檔 append-only＋git | 由 Atelier 本體管理（多團隊、審查、發布） |
| Session 的 refs 不自動記 harness | 可選：自動註記「照哪版做」（作者提議） |
| 隊名 `worker-team`（單一） | 部門／團隊命名（MyLinuxPool#5，期 2） |

## 與 sidecar 的介面

**結論：sidecar 盡量不改（v0 零改動）。** 本節保留 r1 的未選選項，供日後需要時照做。

**sidecar 現在的介面（v5；不改）**：`POST /agents` 的欄位＝`id`、`agent`、`workdir`、`session`（接續或新開）、`subscription`（選填）、`input`（選填；第一則輸入，排隊等 agent 起來再送；`sidecar.md` 第 74-104 行）。**沒有 harness 概念**——本設計不再要求它。

**固定開場句（與 MyPMO#33 的銜接）**：

- MyPMO#33（PMO 改用 Angareion 派工與收回報）的啟動開場本來就是「去 Angareion 讀指示」；加上「先讀 harness」，合成一句：
  > 你是 worker 上的 team PM。先讀你的 harness：`atelier show atelier:worker-team-pm`（照它做；收到新指示前也先重讀）。再用 `angareion wait`（或 `read`）讀信道上的指示。
- **位置**：放在 `open` 的 `input` 的**開頭第一段**；其他工作提示接在後面。（`input` 是選填欄位；**team 啟動的流程 MUST 帶這一句**——由啟動 team 的呼叫方帶，不放進 sidecar。）
- 版本：句子預設**不指定版本**（讀 latest）；要固定時寫 `atelier show atelier:worker-team-pm@v0`（驗收時用 pin 好核對）。
- 這一句是**部署／流程的一部分**（worker 啟動或 PMO 派工的固定文案），不是 sidecar 的一部分。

**未選選項（r1 保留；本人暫不要求）**：

- **r1 甲：sidecar 新增 `harness` 欄位**——`open` 選填 `harness: {name, version?}`；設定 `harness_dir`；載入＝讀檔、驗 frontmatter、把全文原樣作為第一則輸入（在 `input` 之前）；錯誤 400 `harness_not_found`／`harness_version_mismatch`／`harness_invalid`（什麼都沒開、訊息不含內文）；不改內容、不進 log。附帶的 `docs/sidecar.md` 更新與 `install.sh --check` 驗檔。
- **r1 乙：呼叫方把全文放 `input`**——零改動的過渡（與 D8 甲的差別：甲只放指令、乙放全文）。
- 若日後改選 sidecar 載入，上面的欄位名、錯誤碼與檢查照舊可用（介面已留好）。

### 要 MyLinuxPool 做的事（預設：零）

1. **v0：零**。開場句走現有 `input`；`atelier` 是 MyAiStorage 的指令，不需要新能力、不新增憑證、不改 sidecar。
2. **唯一相關（既有流程）**：worker 的 `agora` 套件 pin 升到含 `atelier` 指令的 sha（與 agora／angareion 同一個 pin；`shared-configs/agora/tool-pins` 的既有做法）。讀 harness 用的本機複本與 angareion 共用（angareion 能力已經需要它）。
3. **文件（可選）**：worker 啟動的固定開場句如果要寫進 MyLinuxPool 的文件，照本節的字面。
4. **未選**：若本人日後選 sidecar 全文載入，再照上面的未選清單（欄位、`harness_dir`、錯誤碼、部署與 `--check`）。
5. 其餘環境能力（映像、憑證、LLMGateway 等）照 #52 的既有範圍，本設計不新增要求。

## 作者加的項目（本人未指示；帶給本人看）

以下不是本人的指示，是設計時作者提的；都保留，說明時要讓本人知道：

- **儲存建議**：`atelier/<team>/<role>/v<N>.md` 的三層路徑、append-only 版本檔（E9）。
- **讀取工具建議**：`atelier` 指令、`ls`／`show`、`--section`／`--json`、exit 0／1／2、共用 angareion 的 clone 與 fetch 節流（E10）。
- **固定開場句的字面與位置**（`input` 開頭第一段；版本預設不 pin）（E11）。
- **harness 的 `know` 要求「收到新指示前重讀一次」**（D8 甲的一部分）。
- **能力表示法**（`capabilities` 的 H3 子節、一行 YAML 或留空）與 v0 能力集合 `github`／`angareion`／`agora`（D9）。
- 版本只放檔名與 frontmatter（`v<整數>`、不做 semver）。
- `models` 放 frontmatter 頂層；v0 的值留空。
- `role ∈ {pm, arch, impl, test}` 與 `team` 的字元規則（D2）。
- harness 名稱與 agent 位址是兩件事（D3）。
- 隊名 `worker-team`（D3；E7）。
- v0 檔的 `status: draft`；`generated` 欄位記設計者。
- 期 1 單人暫定（本人已同意）。
- Session `refs` 記 harness id（期 3；v0 不做）。
- 格式檢查放 MyAiStorage 的 pytest；tasks 1.7／2.5／2.6 的可選文件與可選 CI。
- （未選，r1 遗留）`harness_dir`、`harness` 欄位形狀、三個錯誤碼、載入順序——保留在「與 sidecar 的介面」。

## Risks / Trade-offs

- [規矩改版後、跑著的 agent 還照舊版做] → 開場句每次 `open` 都會再給一次（新開與接續都走 `open`）；harness 要求每個新指示前重讀（D8 甲）；期 3 做版本追蹤（可搭配 Session refs）。
- [讀 latest 讓「不同時間讀到的版本不同」] → 要固定就在開場句 pin `@vN`（驗收時 pin）；append-only 讓舊版永遠在（D4）。
- [append-only 讓 repo 檔案變多] → harness 檔小（KB 級）；換來不可變與可追溯。
- [公開 repo 的內容限制] → harness 內容照 D11 的公開規則；設計審查把關；模型的值留空（E4）。
- [讀取要本機複本] → 與 angareion 共用同一份（angareion 能力已需要）；複本不存在＝exit 2、照 angareion 的訊息提示 clone；離線時沿用複本。
- [Drive 選項（若本人改選 E9 乙）] → 沒有 PR 審查、版本可定址性弱、每台都要 Drive 憑證；本設計的格式（append-only 版本檔）仍然可用，但審查要另想辦法。
- [未選的 sidecar 載入若日後選用] → 欄位、錯誤碼與部署清單已保留；屆時 sidecar 改動要重排（可能撞期 3 的 Atelier 本體）。
- [worker 上「開 team」路徑未定（E6）] → 期 1 驗收前必須有答案；本設計的開場句在「人手呼叫」與「正式啟動」下都成立。
- [「harness 讀到哪一版」在回報裡說不清] → 回報格式要求寫「用的模型」，也可加「讀到的 harness 版本」；期 3 用 refs 自動化（作者提議）。

## Open Questions（未決事項；編號；附建議）

**本人已答覆（2026-10-16，經 PMO）**：

1. **E1 五要素與 Angareion 的落點**——已答覆：harness 五要素照 2026-10-10（know／do／judge／dont＋能力需求）；**Angareion 是 AiStorage 的要素**（存 adhoc 的 AI 對話）、**不是 harness 的章節**；`capabilities` 宣告、用法在 `know`（D1）。
2. **E2 載入方式（r1 版）**——已答覆：harness 是 AiStorage 的**虛擬資料夾**（一個團隊／職務一組），AI 用**工具**讀；不是把全文塞進第一則輸入、也不在各工作目錄擺實體檔（本文件的 D6／D7／D8 照此重做）。
3. **E5 sidecar 介面（r1 版）**——已答覆：暫不要求 sidecar 加欄位；sidecar 盡量不改；載入方式改在 r2 提選項（D8、「與 sidecar 的介面」）。
4. **E8 期 1 單人暫定**——已確認：同意（PM 自己做並在回報標明）。

**仍開放（要本人決定）**：

5. **E4（要本人決定）：`models.floor`／`preferred` 的值放哪、可不可以公開**——公開 repo 不寫模型輪換表。**建議：格式留欄位、v0 的值留空（`floor: null`、`preferred: []`＝未指定）；期 3 ModelBroker 落地前，由本人決定哪些值可以寫進公開檔（模型名本身不是憑證，但「哪個角色用哪個模型」的順序可能不想公開）。**
6. **E7（低）：隊名**——v0 用 `worker-team`；期 2 部門命名（MyLinuxPool#5）再定。**建議：不阻塞，v0 就用。**
7. **E9（新；要本人決定）：harness 放哪**——甲 MyAiStorage 的 git（建議；虛擬資料夾樹、PR 審查、與 angareion 共用複本）／乙 Google Drive（與 Agora 同套；沒有 PR 審查）／丙 實體複本（本人已排除）。見 D6。
8. **E10（新；要本人決定）：讀取工具**——甲 新增 `atelier` 指令（建議；`ls`／`show`）／乙 併進 `agora`／丙 當特殊 Session。見 D7。
9. **E11（新；要本人決定）：載入選項**——甲 固定開場句＋自讀＋新指示前重讀（建議）／乙 只在開頭讀一次／丙（未選）sidecar 全文載入。見 D8。

**現況缺口（不是本人的裁定）**：

10. **E6：worker 上「誰開 team、怎麼呼叫 `open`」**——sidecar 只綁 `127.0.0.1`；與 #52／#130 的後續相關。**建議：期 1 先用最簡做法（人在 worker 上或以本機 CLI／腳本呼叫 `open`），正式做法留給 #52 的後續；本設計不因它改介面。**
11. **E3（原「harness 檔的正式位置」）**——已併入 E9（存放位置），不再單獨列。
