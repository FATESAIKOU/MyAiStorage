# atelier Specification

## Purpose
Atelier 管 harness（各職務的指示檔）：格式、版本與安裝／讀取，並由啟動方按 agent 交付，不寫進使用者層級檔案。

## Requirements

### Requirement: harness 檔的格式
一份 harness MUST 是一個職務一個版本的 UTF-8 Markdown 檔，第一個區塊是 YAML frontmatter（OKF v0.2 對齊；不認得的欄位保留）。frontmatter MUST 有 `type`（值 `Harness`）、`id`、`title`、`status`、`team`、`role`、`version`、`models`。`team` MUST 使用 `[a-z0-9][a-z0-9-]*`（英文小寫與 `-`）；`role` MUST 是 `pm`／`arch`／`impl`／`test`／`rev` 之一（審查職務是 `rev`，不再借用 `arch`）；`status` MUST 是 `draft`／`stable`／`deprecated` 之一。本文 MUST 有 `know`、`do`、`judge`、`dont`、`capabilities` 五個 H2 章節；`capabilities` MUST 以能力名為 H3 子節；**v0 職務檔 MUST 有 `angareion` 子節，一般格式規則 SHOULD 宣告該職務真正需要的能力**；每個子節內 MUST 用一行 YAML 或清單寫參數，沒有參數時留空（用途放 `know`／`do`，不在子節內寫散文）。**MUST NOT 有 `angareion` 章節**（本人 2026-10-16）。內容 MUST NOT 含帳號、額度、憑證名稱、模型輪換表或第二大腦內容等私人資訊。**`v1` 起的版本檔**：`know`／`do`／`judge`／`dont` 的**正文** MUST NOT 寫死具體的工具名或模型名（只寫能力——工具與名字寫在各環境的說明），也 MUST NOT 含其他專案的名字、機器位址或**使用者家目錄路徑**（`/Users/…`、`~/…`）（本人 2026-10-18）；**通用系統路徑**（例如 `/tmp`）**不算**，`v1` 的職務要用它寫「工作檔不放 `/tmp`」。frontmatter 的產生者註記（`generated` 之類）不是正文，不算在內。harness 的 `know` MUST 明寫「專案自己的指示檔與工單規則優先」。**每一個 `pm` 職務檔（不分版本）**的 `judge` 或 `dont` MUST 含字串「審查一律非作者」——比對是**精確字串**（要的是這句話本身，同義改寫不算）。

#### Scenario: 通過格式檢查
- **WHEN** 對一份照本格式寫的 harness 執行 MyAiStorage 的格式檢查
- **THEN** 通過（exit 0）

#### Scenario: 缺必填欄位
- **WHEN** 拿掉 `version`（或 `role`、`models`）後執行格式檢查
- **THEN** 失敗，訊息指出缺哪個欄位

#### Scenario: 章節不齊
- **WHEN** 拿掉 `dont` 章節、拿掉 v0 職務檔 `capabilities` 的 `angareion` 子節、把能力子節改成散文（沒有 H3），或把 `angareion` 另立一個 H2 章節
- **THEN** 失敗

#### Scenario: 正文寫死了工具、模型或位址
- **WHEN** 一份 `v1` 起的 harness 在 `know`／`do`／`judge`／`dont` 寫出某一台環境才有的工具名、模型名、其他專案的名字、機器位址或使用者家目錄路徑（`/Users/…`、`~/…`）
- **THEN** 格式檢查失敗（只寫能力；工具、名字與位址放該環境的說明）（本人 2026-10-18）

#### Scenario: 通用系統路徑不算
- **WHEN** 同一份 harness 在 `know` 用 `/tmp` 寫「工作檔不放 `/tmp`」（通用系統路徑，不是家目錄）
- **THEN** 格式檢查通過（`/tmp` 不是私人資訊；`v1` 的職務要用它寫 B-9 那句）

### Requirement: 識別、版本與路徑
一份 harness 的識別 MUST 是 `atelier:<team>-<role>@<version>`，MUST 等於 frontmatter 的 `team`、`role`、`version` 組合；`<version>` MUST 是 `v` 加非負整數。識別 MUST 能直接用在四實體的 ref 語法（`docs/design.md` §3.2），例：`atelier:worker-team-pm@v0`。**版本檔 append-only**：一個版本一個檔；既有版本檔 MUST NOT 被修改、MUST NOT 被刪除；內容有變 MUST 新增下一個版本檔。`atelier show <id>`（不指定版本）MUST 解析為同一職務裡**版本號最大**的那一版。**版本判定 MUST 只比對版本字串（frontmatter 的 `version` 與檔名）；本文 MUST NOT 用來判斷版本。**

#### Scenario: id 與欄位一致
- **WHEN** 格式檢查一份 `id: atelier:worker-team-pm@v0`、`team: worker-team`、`role: pm`、`version: v0`、檔名 `v0.md` 的檔
- **THEN** 通過

#### Scenario: 版本不一致被擋
- **WHEN** `id` 的 `@` 版本、frontmatter 的 `version`、檔名三者有任一不一致（例 `@v1` 配 `v0.md`）
- **THEN** 格式檢查失敗

#### Scenario: 舊版不可變
- **WHEN** 有人修改（或刪除）已發布的 `v0.md`，而不是新增 `v1.md`
- **THEN** 審查 MUST 擋下（append-only 規則；多讀者的不變性靠它）

### Requirement: harness 樹（虛擬資料夾）
harness MUST 構成「一個團隊／職務一組」的虛擬資料夾樹（依 D6 的建議＝MyAiStorage 的 git：`atelier/<team>/<role>/v<N>.md`；見頂部註）；MUST NOT 在各機器、各 agent 的工作目錄擺實體複本。寫入 MUST 只有一條路：對 MyAiStorage 開 PR（經審查與本人同意）；**agent 在執行工作時 MUST NOT 修改 harness**（要改 MUST 回報提議，由人或 PM 決定再開 PR）。多個 AI 同時讀 MUST 不會互相影響——理由是：讀取與安裝工具不寫 harness（唯一的本機寫入是複本自身的 `git fetch`）、版本檔不可變、沒有共享可寫狀態、同一台機器共用一份本機複本（各機器各自一份）。

#### Scenario: 多個讀者同時讀
- **WHEN** 兩個 agent 在不同機器上同時讀 `atelier:worker-team-pm@v0`
- **THEN** 兩邊讀到相同的位元組內容；沒有任何一方能改到 harness（工具沒有寫 harness 的介面）

#### Scenario: 工作時想改規矩
- **WHEN** agent 在執行工作時認為 harness 需要修改
- **THEN** 它回報提議（用 Angareion／回報格式），MUST NOT 直接改 harness 樹

### Requirement: 讀取工具（`atelier`）
repo MUST 附一個讀取工具（依 D7 的建議＝指令 `atelier`，與 agora／angareion 同一家族；見頂部註），**讀的操作** MUST 只有 `atelier ls`（列職務與版本）與 `atelier show <id>[@<版本>] [--section know|do|judge|dont|capabilities] [--json]`（寫的操作只有「產生 agent 指示檔（`render`）」與「安裝（`install`）」兩則：`render` 只寫 `--out`，`install` 只寫使用者層級指示檔）。`show` 不給 `--section` 時 MUST 印整份檔原樣（含 frontmatter）；給 `--section` 時 MUST 只印該章節的內容。讀取 MUST 走本機複本（MyAiStorage 的 clone）＋`git fetch`（匿名 HTTPS；查無時先 fetch 一次；**同一複本路徑、同一把 flock 鎖與 20 秒逾時——沿用 angareion §11**；那一次 fetch 受節流限制——距上次 fetch 不足 30 秒不再 fetch；週期 fetch 沿用 angareion 的 `config_pull_interval`——未設定時 900 秒、`0`＝不按週期拉；複本路徑以 `ATELIER_CONFIG_DIR` 優先，否則取 angareion `config.json` 的 `config_dir`），MUST NOT 依賴工作樹、MUST NOT 需要憑證。**本節的讀取操作 MUST NOT 寫入 harness 樹、工作目錄或使用者檔案；唯一的本機寫入是複本自身的 `git fetch` 更新**；寫的操作只有 `render`（下一則，只寫 `--out`）與 `install`（下下則，只寫使用者層級指示檔）。`git fetch` 失敗（離線、網路）時 MUST 在 stderr 警告一次並沿用目前的複本，MUST NOT 改變退出碼。退出碼：0 成功；1 用法錯或找不到（id／版本／章節）；2 讀取失敗（複本不存在、檔案壞、git 壞）；沒有其他碼。錯誤訊息 MUST NOT 含 harness 內文。

#### Scenario: 讀整份
- **WHEN** `atelier show atelier:worker-team-pm@v0`
- **THEN** 印出 `v0.md` 的整份內容（byte-for-byte），exit 0

#### Scenario: 讀一章節
- **WHEN** `atelier show atelier:worker-team-pm --section dont`
- **THEN** 只印 `dont` 章節的內容，exit 0

#### Scenario: 查無版本
- **WHEN** `atelier show atelier:worker-team-pm@v9`（沒有 v9）
- **THEN** 先 fetch 一次，仍查無則 exit 1，訊息說明版本不存在

#### Scenario: 複本不存在
- **WHEN** 本機沒有 MyAiStorage 的複本就執行 `atelier show`
- **THEN** exit 2，訊息說明要先做一次 clone（照 angareion 的訊息風格）；不嘗試連網下載整份 repo

#### Scenario: 離線
- **WHEN** fetch 失敗（離線），但本機複本還在
- **THEN** 用複本回答，stderr 警告一次，exit 0

### Requirement: 安裝（`atelier install`）——agent 一定會載入的機制
`atelier install <id>[@<版本>] --agent <claude|opencode> [--agent …] [--global]` MUST 把 harness 寫進**每一個指定 agent** 的**使用者層級指示檔**（`--agent` **可以重複**；每個值各裝一次、與順序無關；同一個值重複＝只裝一次；**不給 `--agent` ＝用法錯、exit 1**），讓 agent **一定會載入**（claude：新的 conversation 一定，`--resume` 的接續要在驗收確認——官方沒有明文；opencode：下一個模型請求前就會更新），MUST NOT 依賴第一則輸入、MUST NOT 要求 sidecar 的任何欄位。寫入方式：**管理區塊**（以固定開頭與結尾的註解包住；同一個目標檔內一個 harness 一個區塊）或**獨立檔**（claude 的 `~/.claude/rules/atelier-<team>-<role>.md` 這種一職務一檔）——重複執行 MUST 冪等（同樣的輸入產生同樣的結果，不重複堆疊）。安裝 MUST NOT：碰到目標 repo 自己的 `AGENTS.md`／`CLAUDE.md`、工作目錄的任何檔案、或 `~/.claude/settings.json`（尤其 MUST NOT 產生 `apiKeyHelper`）。不指定版本時 MUST 裝版本號最大的那一版。**`v1` 起：對 `role: pm` 的 harness，`install` MUST 預設拒絕（exit 1，訊息指向 `render`／由啟動方交付）；只有明寫 `--global` 時才 MUST 安裝，並 MUST 在 stderr 印一行警告「同一個使用者的每個 agent 都會拿到這份 PM harness」**（本人 2026-10-18；M4）。退出碼同讀取工具：0 成功；**1 用法錯、找不到 id／版本、或政策拒絕（`v1` 的 pm harness 沒有 `--global`）**；2 複本或檔案問題（含寫不進去）。

#### Scenario: 裝進 opencode 的使用者層級指示檔
- **WHEN** `atelier install atelier:worker-team-pm@v0 --agent opencode`（pin `@v0`：`v1` 的 pm harness 沒有 `--global` 會被拒絕）
- **THEN** `~/.config/opencode/AGENTS.md` 裡出現包住 harness 的管理區塊（或等價的獨立檔）；下一次 `opencode` 啟動時載入

#### Scenario: 裝進 claude 的使用者層級指示
- **WHEN** `atelier install atelier:worker-team-pm@v0 --agent claude`
- **THEN** harness 進入 `~/.claude` 底下的使用者層級指示（rules 檔或 `CLAUDE.md` 的管理區塊）；下一次 `claude` 啟動時載入

#### Scenario: 冪等
- **WHEN** `atelier install atelier:worker-team-pm@v0 --agent claude` 跑兩次
- **THEN** 目標檔的內容只多一份 harness（管理區塊被替換，不是再貼一次）

#### Scenario: 重複 --agent
- **WHEN** `atelier install atelier:worker-team-pm@v0 --agent claude --agent opencode`
- **THEN** claude 與 opencode 的使用者層級指示檔各出現一份 harness（只裝一次、與 `--agent` 的順序無關）

#### Scenario: 沒給 --agent
- **WHEN** `atelier install atelier:worker-team-pm@v0`（只有 id，沒有 `--agent`）
- **THEN** exit 1（用法錯）；什麼都沒裝

#### Scenario: 不動專案檔與 settings
- **WHEN** 目標 repo 有 `AGENTS.md`／`CLAUDE.md`，而使用者有 `~/.claude/settings.json`
- **THEN** 安裝完成後兩者內容逐字不變；工作目錄沒有新檔案

#### Scenario: 專案指示優先
- **WHEN** harness 與目標 repo 自己的指示檔對同一件事給不同說法
- **THEN** harness 的 `know` 明寫「專案的指示優先」，MUST NOT 要求 harness 覆蓋專案

#### Scenario: v1 的 PM harness 沒給 --global
- **WHEN** `atelier install atelier:worker-team-pm@v1 --agent claude`（沒有 `--global`）
- **THEN** exit 1，訊息說改用 `render`／由啟動方交付；目標檔一個字都沒動

#### Scenario: 明寫 --global 才裝
- **WHEN** `atelier install atelier:worker-team-pm@v1 --agent claude --global`
- **THEN** 安裝成功（exit 0），stderr 有一行警告說同一個使用者的每個 agent 都會拿到這份 PM harness

### Requirement: 能力需求與模型欄位
`capabilities` 章節 MUST 以能力名為 H3 子節；子節名 SHOULD 用環境的 capability 名（v0：`github`、`angareion`、`agora`）；**一份給多個環境共用的職務（`v1` 起的 PM 這種）MAY 用中性名**——回報通道、隊員控制、監看這類在兩邊不一樣的能力（對應哪一個環境的單位提供，寫在該環境的說明），環境自己就有的能力 SHOULD 照環境的名字；**v0 職務檔 MUST 含 `angareion` 子節**（一般職務 SHOULD 即可）；子節內 MUST 用一行 YAML 或清單寫參數，沒有參數時留空；同一份 harness 內的子節名 MUST NOT 重複。未知的能力名 MUST 只警告、MUST NOT 擋（能力清單要能長）。**模型不是能力子節**：frontmatter MUST 有 `models`，含 `floor`（模型識別碼或 `null`）與 `preferred`（有順序的清單，可空）；**v0 的值留空**（本人 2026-10-16）。租用模型時 MUST 先照 `preferred` 的順序滿足，且 MUST NOT 低於 `floor`；滿足不了 MUST 停下並回報，MUST NOT 靜默降級。v0 的能力由 worker 的預設 profile 全部提供；harness MUST NOT 指定 profile 名（限制在期 2 由 worker profile 做）。sidecar MUST NOT 對宣告逐項核銷。

#### Scenario: 欄位存在、值可空
- **WHEN** 一份 harness 的 `models.floor` 是 `null`、`preferred` 是空清單
- **THEN** 格式檢查通過（＝未指定，由環境決定）

#### Scenario: 能力的子節
- **WHEN** `capabilities` 有三個 H3 子節 `github`／`angareion`／`agora`，參數都留空
- **THEN** 格式檢查通過

#### Scenario: 子節名重複或沒有子節
- **WHEN** `capabilities` 有兩個同名子節，或整章沒有 H3 子節
- **THEN** 格式檢查失敗

#### Scenario: 未知名稱只警告
- **WHEN** `capabilities` 有一個環境不認得的能力名
- **THEN** 格式檢查警告、不擋（能力清單要能長）

#### Scenario: 達不到底線
- **WHEN** 租用時 `preferred` 都不可用、可用的模型又低於 `floor`
- **THEN** 停下並回報（不是改用更低階的模型繼續跑）

#### Scenario: 跨環境共用的職務用中性名
- **WHEN** 一份給 Mac 與 worker 共用的職務檔，`capabilities` 用了中性名（回報通道、隊員控制、監看），而那些名字不是某一個環境的單位名
- **THEN** 格式檢查通過（未知的能力名只警告），由各環境的說明交代誰提供

### Requirement: v0 職務（worker team PM）
識別 `atelier:worker-team-pm@v0` 的職務檔 MUST 以 PM 的一般規矩為底（派工與回報、一張單一個 branch／worktree、審查非作者、不自己寫程式、安全界線、不繞過被擋的動作），並加上 worker 上的差異：**這份 harness 由使用者層級指示檔自動載入**（不用自己去讀；`know` MUST 說明要重讀或確認版本時用 `atelier show`）；**專案自己的指示檔與工單規則優先**；以 Angareion 收指示與回報（閒時 MUST 用阻塞的 `wait` 等，MUST NOT 在迴圈裡反覆查詢模型）；在 worker 上開 PR（MUST NOT 直接改 main）；MUST NOT 讀取或輸出憑證、MUST NOT 碰真機或別的 repo、MUST NOT 繞過被擋的動作、MUST NOT 在工作時改 harness；**沒有明確任務脈絡時 MUST 停下、回報一句「沒有未完成的工作」（MUST NOT 自行探索 HOME、讀設定檔或狀態檔、寫記憶檔）**（MyLinuxPool 實機驗收，2026-10-16）。回報 MUST 含：做了什麼、產出（branch／PR）、測試原樣輸出、沒驗到的事、用的模型。期 1（v0）隊上只有 PM 時，實作與測試由 PM 自己做並在回報標明（本人 2026-10-16 同意）；**這個單人暫定自 `v1`（有隊員的 PM）起結束**——`v1` 起 PM 不自己寫程式、一律非作者審查，期 3 再由 Atelier 本體的正式版接手。

#### Scenario: 收到指示
- **WHEN** PM 的 `wait` 醒來、`read` 讀到指示
- **THEN** 照工單做（一張單一個 branch／worktree）、開 PR、以 Angareion 回報，回報含測試原樣輸出

#### Scenario: harness 自動載入
- **WHEN** PM 被 sidecar 啟動（`open` 的 input 只說「去 Angareion 讀指示」）
- **THEN** 它已經帶著 harness 的規矩（使用者層級指示檔載入的），MUST NOT 需要第一則輸入叫它去讀 harness

#### Scenario: 被擋不繞過
- **WHEN** 一個動作被安全檢查或憑證擋下
- **THEN** PM 停下來回報，不換方式或換身分再試

#### Scenario: 沒有明確的任務脈絡
- **WHEN** PM 收到「接著剛才的工作」這類提示，但 Session 裡沒有未完成的工作
- **THEN** 它回報一句「沒有未完成的工作」就停下；MUST NOT 自行探索 HOME、讀 `~/.claude.json`／`~/.mylinuxpool`／`~/.config/*` 這類設定檔或狀態檔、或寫記憶檔（MyLinuxPool 實機驗收，2026-10-16）

#### Scenario: 單人暫定要標明
- **WHEN** 期 1 的 PM 自己完成實作與測試
- **THEN** 回報中標明是 v0 的單人暫定（自己實作、沒有非作者審查），並留給 PMO／本人決定下一步

### Requirement: 與 Agora Session 的關係
harness 樹是 Atelier 自己的（不是 Session、不是 Agora 的項目）；MUST NOT 存進 Agora、MUST NOT 要求 Agora 認識 Atelier。Session 的 header MAY 在 `refs` 記 harness 的識別（例 `atelier:worker-team-pm@v0`）作為註記；那是選填、MUST NOT 影響載入或讀取，v0 MUST NOT 自動寫入。

#### Scenario: 讀 harness 不需要 Agora
- **WHEN** 在一台沒有配置任何 Agora 儲存的地方用 `atelier` 讀 harness
- **THEN** 正常讀到（Atelier 與 Agora 不相依）

#### Scenario: refs 只是註記
- **WHEN** 一個 Session 的 `refs` 含 `atelier:worker-team-pm@v0`
- **THEN** 讀寫該 Session 的行為不變（`refs` 就只是一個字串）

### Requirement: PM 職務（`v1`：有隊員的 PM）
`v1` 起的 PM 職務檔 MUST 寫「有隊員的 PM」：PM 自己不逐行寫程式——架構與設計交架構師、實作交實作隊員、審查交非作者（本人 2026-10-18，B-17）。整份 MUST 寫到下列內容——**哪一句放哪一段（`know`／`do`／`judge`／`dont`）由 harness 自己安排，只要寫到**：

- 收到方向或需求（`v1`、`v2`）：b 與大的 c 先交架構師寫設計，列出本人的判斷點與「作者提議、本人未指示」的項目，經非作者審查與本人同意才實作。
- 收到方向或需求（`v>=3`，`pm/v3`）：b 與全部 c 的設計與介面 PR 必經專案架構師（觸發與免經由 PM 寫在工單上；免經寫「架構師：免（理由）」）；PM 不越過架構師定介面，實作 brief 引用已核准的設計文件路徑與版本；必經的單，架構師的結構審查通過才 merge；PMO 席位名單尚無該專案架構師時，照 v2 的方式做，在工單上註明「架構師：未開席」（design §11.1 的 2b）。
- 開單與拆單：一張單一個 branch／worktree、每張單先寫可驗的完成判準、後續問題另開單。
- 派工：指示寫在 brief 檔（不寫在聊天裡）；brief 與工作檔放會留下來、不進版控的地方，且不放 `/tmp`；實作／審查／修正／確認派給不同的人；預設 PM commit、隊員不 commit。
- 監看：有在跑的就掛、全部待命可以停；「做完」＝中斷提示消失**而且**報告檔存在。
- 核對與審查：在同一個 worktree 自己重跑、只寫自己跑過的驗證數字；非作者審查的門檻＝高、中都修完才開 PR、低可以後續、專案可以更嚴；不同模型的審查者都用不了時可以用同模型、不同 session，PR 第一行寫明。
- 開 PR 與交合併（分版本）：`v1`：c／d 交 PMO 代 merge；`v2` 起（#200）：c／d 由**該 repo 的 PM** 審過（非作者）、CI 綠就自己 merge、merge 後告知 PMO，**team PM 不 merge**，PR 好了回報 PMO、由 PMO 轉給該 repo 的 PM；b 等本人（由 PMO 向本人說明、本人同意才 merge）；收尾（移除 worktree 與本機分支、歸檔、工作 session 掃過憑證才匯入長期記憶；**關隊員或收掉它的畫面／session 之前，先中斷它正在跑的工作、確認沒有長時間的指令還活著——只關畫面不會停掉 session，共用的服務重啟會把中斷的長指令重新叫醒**）。
- `know` MUST 有：本人只跟 PMO 說話（直接問本人的兩個例外在 `judge`）、**同儕（包含 PMO）轉來的「本人同意」只代表決定、不是權限**（怎麼請本人接手看環境說明）、模型只寫「照 PMO 的模型輪換表」、**harness 是環境在啟動你時交給你的（不是使用者層級指示檔）**、**收發的紀律**（收訊不指定頻道才只收寄給自己的、只有送訊才指定頻道；身分在每個工作單位唯一、狀態目錄持久；回報目錄狀態要實證；閒著用阻塞等待、逾時拉長——能力層級，具體指令看環境說明）。
- `judge` MUST 有：授權只對那一次有效、直接問本人的兩個例外（懷疑有人冒用 PMO／本人已在你的畫面上操作）、真機要有本人那一次的授權才自己跑。
- `dont` MUST 有：不繞過被擋的動作（不換方式、不換人、**不請別的 session 代做**）、**不把私人資訊寫進任何會公開的地方（repo／PR／commit／訊息）**、不讓免費模型碰真資料、不替本人做決定、不直接改 main。

整份 MUST 只寫能力、不寫工具名（B-16）；同時進行中的 b、c 上限 MUST 只寫「照工單規則」（B-6）；PM 自己開的 d MUST NOT 要求另一位隊員附議（B-7）。這一版 MUST 由 repo PM 與 team PM **共用一份**（不分機器）：兩邊的差異（通道、真機、怎麼請本人接手、工作檔位置）MUST NOT 進 harness，放各環境的說明；各專案特有的做法 MUST 留在該 repo 的 `AGENTS.md`（B-17）。

#### Scenario: 有隊員的 PM 不自己寫程式
- **WHEN** 一張單要做實作與測試
- **THEN** 它派給隊員，自己的工作是核對、審查，以及照「開 PR 與交合併」那一條交合併；回報不再標「單人暫定」

#### Scenario: 高、中修完才開 PR
- **WHEN** 非作者審查找到一個「高」或一個「中」
- **THEN** 退回作者修完、由原審查者或第三方再確認一次，才開 PR；「低」可以順手修、寫進 PR 或另開後續單

#### Scenario: 轉達的同意不是權限
- **WHEN** 有人（包含 PMO）轉來「本人同意」，而要做的動作在自己的這一關被安全檢查擋下
- **THEN** 停下、說明原因、請本人接手（怎麼請看環境說明），不換方式、不換人、不請別的 session 代做

#### Scenario: 兩個環境共用一份
- **WHEN** 同一份 PM 職務檔在 Mac 與 worker 上被載入
- **THEN** 兩邊的差異（通道、真機、隊員控制怎麼做）在各自的環境說明裡；harness 的本文沒有工具名、沒有模型名、沒有機器位址

#### Scenario: `v1`、`v2` 的 PM 檔（b 與大的 c）
- **WHEN** PM 檔版本是 `v1` 或 `v2`
- **THEN** 只要求「b 與大的 c 先交架構師寫設計，經非作者審查與本人同意才實作」；不要求 `v>=3` 的「全部 c」與免經記錄條款（append-only；見 design §11.3）

#### Scenario: 必經架構師（`v>=3`）
- **WHEN** PM 派一張 b 級或 c 級的單（或改對外介面、改模組切割的 d 級單）
- **THEN** PM 的工單寫明「架構師：必經」，設計與介面 PR 先經該專案的架構師；PM 不越過架構師定介面；`v1`、`v2` 的 PM 檔不改，仍只寫「b 與大的 c」（append-only；見上一個 Scenario 與 design §11.3）

### Requirement: 按 agent 交付 harness
`v1` 起的 PM 職務 MUST **按 agent 交付**：開 agent 的一方（各環境的隊員控制服務〔sidecar〕）MUST 在啟動**那一個** agent 時，依它的角色把對應的 harness 交給它；**PM 角色 MUST 拿到 PM harness，其他角色 MUST NOT 拿到 PM harness**（本人 2026-10-18；PMO 抓到的高）。這裡的「PM harness」指**該環境 series 的 `pm`**（例如 `atelier:worker-team-pm`）；PMO 自己的職務檔 `atelier:pmo-pm`（#226）是另一份，按 PMO 的角色交付，不算這裡的「PM harness」，也不因此交給 PM 或隊員。交付 MUST 只對那一個 agent 的 process 生效：同一個使用者的其他 agent（PMO、別的 PM、隊員、別專案的 agent）MUST NOT 因為有人啟動了 PM 而改變指示或載入。交付 MUST NOT 依賴使用者層級指示檔（`~/.claude/rules/` 的職務檔、`~/.config/opencode/AGENTS.md` 的職務區塊）；`atelier install` 留著給 `v0` 與全域用途，**`v1` 的 PM harness MUST NOT 用 `install` 交付，唯一例外是明寫 `--global` 的全域安裝**（單一 agent 的機器；安裝時 MUST 印警告，見「安裝」）。交付方 MUST 先確認 harness 檔存在且可讀（`opencode` 對不存在的 `instructions` 是靜默忽略），產生失敗時 MUST NOT 啟動 agent，MUST 回報原因。

#### Scenario: 同一台機器的隊員拿不到 PM harness
- **WHEN** worker 上同時開了 team PM 與一個隊員（同一個使用者、同一台機器）**，而且使用者層級的 v0 殘留已經清掉**（見 `openspec/changes/archive/2026-10-25-atelier-pm-harness-v1/design.md` 的過渡步驟）
- **THEN** 只有 PM 的 process 的指示裡有 PM harness；隊員的沒有

#### Scenario: Mac 上的多重角色互不干擾
- **WHEN** Mac 的 sidecar 為同一個使用者開了 PMO、兩個 PM 與一個隊員
- **THEN** 只有兩個 PM 的 process 拿到 team 的 PM harness；PMO 拿的是自己的 `atelier:pmo-pm`、隊員拿的是自己的職務檔，兩者都沒有 PM harness

#### Scenario: 使用者層級檔案不變
- **WHEN** 啟動 PM（交付成功）之後檢查 `~/.claude/rules/` 與 `~/.config/opencode/AGENTS.md`
- **THEN** 內容逐字不變（交付是啟動參數／每 process 設定，不是寫檔）

#### Scenario: 產生失敗就不開
- **WHEN** `atelier render`（或等價的交付準備）回 1／2／3、或產生的檔不存在
- **THEN** 啟動方不開 agent，回報原因（不開一個沒有規矩的 PM）

#### Scenario: --global 的全域安裝是唯一例外
- **WHEN** 在只有一個 agent 的機器上 `atelier install atelier:worker-team-pm@v1 --agent claude --global`
- **THEN** 安裝成功、stderr 有警告（同一個使用者的每個 agent 都會拿到）；同一個指令沒有 `--global` 時 exit 1——這是「v1 的 PM harness 不用 install 交付」的唯一例外

### Requirement: 產生 agent 指示檔（`atelier render`）
`atelier render <id>[@<版本>] [--append <檔>] --out <檔>` MUST 讀 harness（不指定版本＝版本號最大）、可選把 `--append` 的檔案（環境說明）接在後面、**原子寫入** `--out`（先寫同目錄的暫存檔再 rename；中斷時不留半份）。`--append` 的檔 MUST 存在（不存在 → exit 1）。harness 格式壞（frontmatter 缺或無法解析、`type` 不是 `Harness`）MUST 拒絕（exit 2，`--out` 不建立）。`render` 是**只寫 `--out`** 的寫入操作：MUST NOT 寫 `--out` 以外的任何檔案（harness 樹、使用者層級指示檔、工作目錄都一樣）。退出碼：0 成功；1 用法錯（參數錯、壞 id 格式、缺 `--out`、`--out ''`、`--append` 檔不存在含 `--append ''`）；2 讀取失敗（複本不存在、壞 harness）或寫不進去；3 harness 查無 id／版本（讓呼叫方分辨「查無」與「用法錯」）。錯誤訊息 MUST NOT 含 harness 內文。

#### Scenario: 產生一份給啟動方
- **WHEN** `atelier render atelier:worker-team-pm --out <agent 的 state 目錄>/instructions.md`
- **THEN** 檔案內容是 harness 的整份原樣（含 frontmatter），exit 0

#### Scenario: 附加環境說明
- **WHEN** `atelier render atelier:worker-team-pm --append <環境說明> --out <agent 的 state 目錄>/instructions.md`
- **THEN** 檔案是 harness 後面接環境說明（順序固定），exit 0

#### Scenario: --append 的檔不存在
- **WHEN** `atelier render atelier:worker-team-pm --append <不存在的檔> --out <agent 的 state 目錄>/instructions.md`
- **THEN** exit 1，訊息說明檔案不存在；`--out` 沒有被建立

#### Scenario: 查無版本
- **WHEN** `atelier render atelier:worker-team-pm@v9 --out <檔>`（沒有 v9）
- **THEN** exit 3，訊息說明版本不存在；`--out` 沒有被建立

#### Scenario: 查無 id
- **WHEN** `atelier render atelier:worker-team-test --out <檔>`（沒有這個職務的 harness）
- **THEN** exit 3，訊息說明找不到 harness；`--out` 沒有被建立

#### Scenario: 壞 harness
- **WHEN** `atelier render` 指定的 harness 沒有 frontmatter、frontmatter 無法解析或 `type` 不是 `Harness`
- **THEN** exit 2，訊息說明原因（不含 harness 內文）；`--out` 沒有被建立

#### Scenario: 寫不進去
- **WHEN** `--out` 指向一個寫不進去的路徑
- **THEN** exit 2，說明原因；`--out` 以外的任何檔案都沒有變（harness 樹、使用者層級指示檔、工作目錄）

### Requirement: 架構師職務（arch，worker-team）
worker-team 的架構師職務檔（`atelier:worker-team-arch@v1`，`role: arch`）MUST 寫明：架構師**每個專案一位**（本人 2026-10-31 裁定，見 design 決定表第 1 點）、按需開（需要才開，用完關）、與 PM 同級；只產設計與介面、審模組切割與 SOLID、可退回設計，**MUST NOT 寫實作**；同一張單 **MUST NOT 兼任設計與實作**（架構師審觸發單的實作是否符合它批准的設計，**不算**兼任實作）。

觸發的單（b、c、改對外介面或模組切割的 d 級單）的設計與介面 PR **MUST 經該專案的架構師**，有兩種模式，兩者都算經過架構師：**核准者模式**（預設）由隊員或 PM 派的作者寫設計、架構師做結構審查給「可以／退回設計」；**作者模式**（跨模組或改總圖的 b，定義見 design §2.1）由架構師親寫，結構面的非作者審查改由另一個模型的審查者照**架構清單**（design §3.1 的定義，harness 與本 spec 共用）審，並由 PMO 向本人說明（本人 2026-10-31 裁定核准者／作者模式並存、預設核准者，作者模式限跨模組或改總圖的 b，見 design 決定表第 2 點；**作者模式由 PM 在工單上指定，並寫明審查者與架構清單**）。沒有架構師的「可以」MUST NOT merge 設計、MUST NOT 派實作。作者模式例外：架構師親寫的設計，以非作者審查者照架構清單的「可以」加 PMO 向本人說明為核准；架構師對自己設計的「可以」不算數。觸發與免經的判定由 PM 寫在工單上。

產物歸屬：proposal 與 specs delta 歸 PM（PM 或 PM 派的作者寫、PM 核准），架構師只審行為能否用一致的介面表達；design 的模組切割、介面契約、依賴方向、SOLID 與測試策略歸架構師核准；tasks 的 PR 切分歸架構師，排序歸 PM。

衝突依問題種類裁決（本人 2026-10-31 裁定，見 design 決定表第 3 點與 §1 矩陣）：語意與需求歸 PM；形狀與一致性（模組切割、介面形狀、SOLID、命名、依賴方向）歸架構師；測試契約的層級與替身歸架構師、驗收門檻歸 PM；非功能需求的目標與門檻歸 PM、達成的結構歸架構師；兩邊僵持交 PMO，PMO 不能決定時再交本人。

架構師 MUST 有交接檔、接手時先讀交接檔的最新一節；排隊的優先序由 PM 定。架構師的設計 MUST 經非作者審查才算數，架構師 MUST NOT 審自己的設計。架構師 MUST 能宣告跨單一致性的阻塞，PM MUST NOT 用排序繞過它。觸發的單沒有架構師的「可以」，設計不派實作、實作不 merge。

架構師席位不可用（額度用完、結束或出錯、離線）時，觸發單的設計與介面 PR MUST 暫停，PM MUST NOT 代定介面；PMO 可以開代理架構師（同一份 harness 與交接檔，接手同一個席位，名字照 PMO 的席位名單；代理使用的前提與交回規矩見 design §2.3），連代理都開不了時 PMO 可以書面例外（記在工單）。

整份 MUST 只寫能力、不寫工具名、模型名、其他專案的名字與機器位址（與既有的 `v1` 內容限制相同）。`pm` 的 `v>=3` 與「PM 不越過架構師定介面」的義務見下方 MODIFIED 的 `PM 職務`；`pmo` 的 `v>=4` 只由 harness 的關鍵句測試守住，主 spec 目前沒有 PMO 職務的 requirement（見 design §11.2、§13 D17）。

#### Scenario: 每專案一位
- **WHEN** 同一個專案要登記第二位架構師
- **THEN** 第二位不會成為第二個席位：唯一性由 PMO 側規則保證，以專案為鍵（本人 2026-10-31 裁定每專案一位，見 design 決定表第 1 點）；「席位名單」的機制本 repo 沒有規格，待定義（design D13）；代理架構師接手同一席位，不算第二位。若本人改定為專案×team，本 scenario 改為以「專案×team」為鍵

#### Scenario: 必經的單
- **WHEN** PM 判定一張 b 或 c 級的單（或改對外介面、改模組切割的 d 級單）
- **THEN** 它的設計與介面 PR 經該專案的架構師；工單上寫明「架構師：必經」；實作 brief 引用已核准的設計文件路徑與版本

#### Scenario: 核准者模式（預設）
- **WHEN** 隊員或 PM 派的作者寫好一張必經單的設計文件
- **THEN** 架構師做結構審查，給「可以」或「要退回設計」；沒有它的「可以」，設計不 merge、不派實作

#### Scenario: 作者模式
- **WHEN** 一張 b 級單跨模組或改總圖，由 PM 在工單上指定作者模式（並寫明審查者與架構清單），架構師親寫設計
- **THEN** 結構面的非作者審查改由另一個模型的審查者照架構清單審；它的「可以」加上 PMO 向本人說明、本人同意才進實作（本人 2026-10-31 裁定核准者／作者模式並存、預設核准者，見 design 決定表第 2 點）；架構師對自己設計的「可以」不算數。模式由 PM 依定義判定，判不出來用核准者模式

#### Scenario: 免經要記錄
- **WHEN** PM 判定一張單免經架構師（純文件、純測試補強、不改介面的小 d）
- **THEN** 工單上寫「架構師：免（理由）」；爭議交 PMO 裁定；架構師定期掃過新開的單，碰到介面或模組界線的免經單提出，PM 改判定；免經的 PR merge 後通知架構師（知會，不是閘門）

#### Scenario: 同一張單不兼任實作
- **WHEN** 架構師設計過某一張單
- **THEN** 該單的實作派給別的隊員；架構師不寫該單的實作碼

#### Scenario: 架構師審觸發單的實作結構
- **WHEN** 觸發單的實作 PR 出來
- **THEN** 架構師審它的介面契約與依賴方向是否符合它批准的設計（對照契約編號表，不逐行讀碼）；正確性與測試由審查隊員審；審查隊員遇到結構疑點只回報並標「交架構師」、不裁決；兩邊都通過才 merge

#### Scenario: 架構師不審自己的設計
- **WHEN** 架構師寫完一份設計文件（作者模式）
- **THEN** 它要經另一個模型的審查者審查才成立；架構師自己不算審過

#### Scenario: 退回設計
- **WHEN** 非作者審查或 PM 認為設計違反總圖、SOLID 或介面一致性
- **THEN** 設計退回，回到設計階段修；設計退回與實作退回分開計數：同一張單的設計第 3 次退回時停下，交 PM 判斷是否改範圍，PM 仍無解交 PMO；同一張單的實作第 3 次退回時停下，交架構師重看契約；PM 要求退的、或 PM 與架構師僵持，交 PMO

#### Scenario: 交接
- **WHEN** 架構師的 session 重開或換模型
- **THEN** 它先讀交接檔的最新一節，並用一兩句跟 PM 確認現況；舊 context 一律當作過期

#### Scenario: 排隊由 PM 定
- **WHEN** 多張單同時要設計
- **THEN** 優先序由 PM 定；架構師回報容量，不自行改順序；兩張單的介面衝突等跨單問題，架構師可以宣告阻塞，PM 不用排序繞過

#### Scenario: 結構與需求衝突
- **WHEN** 架構師的判斷與 PM 的判斷衝突
- **THEN** 依問題種類裁決：語意與需求歸 PM，形狀與一致性歸架構師，測試契約的層級與替身歸架構師、驗收門檻歸 PM，非功能的目標與門檻歸 PM、達成的結構歸架構師；兩邊堅持不下交 PMO，PMO 不能決定時再交本人（本人 2026-10-31 裁定，見 design D9；PR-A 已 merge）

#### Scenario: 架構師不可用
- **WHEN** 架構師席位額度用完、結束或出錯、或離線
- **THEN** 回報 PM 與 PMO；觸發單的設計與介面 PR 暫停，不由 PM 代定介面；PMO 可以開代理架構師（同一份 harness 與交接檔，接手同一個席位）；代理只在原席位不可用期間存在，原席位已確定停止或宣告不可用才開，與原席位不同時執行；原席位回來時代理先寫完交接檔最新一節再交回；連代理都開不了時，PMO 可以書面例外（記在工單）；事後發現誤判免經的單，補審或退回（design §2.3，**作者提議**）
