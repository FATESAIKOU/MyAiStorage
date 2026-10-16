## ADDED Requirements

> 註（暫定）：以下「存放位置」「讀取工具」「載入方式」暫依 design D6／D7／D8 的建議（MyAiStorage 的 git 虛擬資料夾＋`atelier` 指令＋固定開場句），待 E9／E10／E11 由本人裁定後同步。本人 2026-10-16 已答覆：harness 五要素照 know／do／judge／dont＋能力需求；Angareion 是 AiStorage 的要素、**不是 harness 的章節**（在 `capabilities` 宣告、用法在 `know`）。

### Requirement: harness 檔的格式
一份 harness MUST 是一個職務一個版本的 UTF-8 Markdown 檔，第一個區塊是 YAML frontmatter（OKF v0.2 對齊；不認得的欄位保留）。frontmatter MUST 有 `type`（值 `Harness`）、`id`、`title`、`status`、`team`、`role`、`version`、`models`。`team` MUST 使用 `[a-z0-9][a-z0-9-]*`（英文小寫與 `-`）；`role` MUST 是 `pm`／`arch`／`impl`／`test` 之一（期 3 的 reviewer 對應 `arch`）；`status` MUST 是 `draft`／`stable`／`deprecated` 之一。本文 MUST 有 `know`、`do`、`judge`、`dont`、`capabilities` 五個 H2 章節；`capabilities` MUST 以能力名為 H3 子節；**v0 職務檔 MUST 有 `angareion` 子節，一般格式規則 SHOULD 宣告該職務真正需要的能力**；每個子節內 MUST 用一行 YAML 或清單寫參數，沒有參數時留空（用途放 `know`／`do`，不在子節內寫散文）。**MUST NOT 有 `angareion` 章節**（本人 2026-10-16）。內容 MUST NOT 含帳號、額度、憑證名稱、模型輪換表或第二大腦內容等私人資訊。

#### Scenario: 通過格式檢查
- **WHEN** 對一份照本格式寫的 harness 執行 MyAiStorage 的格式檢查
- **THEN** 通過（exit 0）

#### Scenario: 缺必填欄位
- **WHEN** 拿掉 `version`（或 `role`、`models`）後執行格式檢查
- **THEN** 失敗，訊息指出缺哪個欄位

#### Scenario: 章節不齊
- **WHEN** 拿掉 `dont` 章節、拿掉 v0 職務檔 `capabilities` 的 `angareion` 子節、把能力子節改成散文（沒有 H3），或把 `angareion` 另立一個 H2 章節
- **THEN** 失敗

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
harness MUST 構成「一個團隊／職務一組」的虛擬資料夾樹（依 D6 的建議＝MyAiStorage 的 git：`atelier/<team>/<role>/v<N>.md`；見頂部註）；MUST NOT 在各機器、各 agent 的工作目錄擺實體複本。寫入 MUST 只有一條路：對 MyAiStorage 開 PR（經審查與本人同意）；**agent 在執行工作時 MUST NOT 修改 harness**（要改 MUST 回報提議，由人或 PM 決定再開 PR）。多個 AI 同時讀 MUST 不會互相影響——理由是：讀取工具對 harness 唯讀（唯一的本機寫入是複本自身的 `git fetch`）、版本檔不可變、沒有共享可寫狀態、各讀各的本機複本。

#### Scenario: 多個讀者同時讀
- **WHEN** 兩個 agent 在不同機器上同時讀 `atelier:worker-team-pm@v0`
- **THEN** 兩邊讀到相同的位元組內容；沒有任何一方能改到 harness（工具沒有寫入介面）

#### Scenario: 工作時想改規矩
- **WHEN** agent 在執行工作時認為 harness 需要修改
- **THEN** 它回報提議（用 Angareion／回報格式），MUST NOT 直接改 harness 樹

### Requirement: 讀取工具（`atelier`）
repo MUST 附一個讀取工具（依 D7 的建議＝指令 `atelier`，與 agora／angareion 同一家族；見頂部註），對外 MUST 只有讀的操作：`atelier ls`（列職務與版本）與 `atelier show <id>[@<版本>] [--section know|do|judge|dont|capabilities] [--json]`。`show` 不給 `--section` 時 MUST 印整份檔原樣（含 frontmatter）；給 `--section` 時 MUST 只印該章節的內容。讀取 MUST 走本機複本（MyAiStorage 的 clone）＋`git fetch`（匿名 HTTPS；查無時先 fetch 一次；**同一複本路徑、同一把 flock 鎖與 20 秒逾時——沿用 angareion §11**），MUST NOT 依賴工作樹、MUST NOT 需要憑證。**MUST NOT 寫入 harness 樹、工作目錄或使用者檔案；唯一的本機寫入是複本自身的 `git fetch` 更新。**`git fetch` 失敗（離線、網路）時 MUST 在 stderr 警告一次並沿用目前的複本，MUST NOT 改變退出碼。退出碼：0 成功；1 用法錯或找不到（id／版本／章節）；2 讀取失敗（複本不存在、檔案壞、git 壞）；沒有其他碼。錯誤訊息 MUST NOT 含 harness 內文。

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

#### Scenario: 唯讀
- **WHEN** 對 `atelier` 下任何寫入性質的參數（例 `atelier show --write`）
- **THEN** exit 1（未知參數）；除了複本自身的 `git fetch`，沒有任何檔案被改（尤其 harness 與工作目錄）

### Requirement: 載入（固定開場句）
啟動 team 的呼叫方 MUST 在 sidecar `open` 的 `input` **開頭**放固定開場句：先叫 agent 讀它的 harness（例 `atelier show atelier:worker-team-pm`）、再叫它讀信道的指示（Angareion `wait`／`read`）。agent MUST 先讀 harness、再讀指示，並以它讀到的那一版規矩做事。sidecar MUST NOT 需要 harness 概念（不新增欄位）；**v0 MUST NOT** 把 harness 全文塞進輸入（未選的 sidecar 載入選項保留在 design、不當成 v0 的預設）、MUST NOT 在各工作目錄放實體檔。開場句預設不指定版本（讀版本號最大的那版）；要固定時 MUST 可以寫 `@vN`。（未選的替代——sidecar 讀檔載入全文——保留在 design，MUST NOT 當成 v0 的預設。）

#### Scenario: 開場句在 input 開頭
- **WHEN** 以 `open` 啟動 team，`input` 是「你是 worker 上的 team PM。先讀你的 harness：`atelier show atelier:worker-team-pm`……」
- **THEN** agent 的第一個動作是讀 harness，之後才讀信道；sidecar 沒有因為 harness 而多收任何參數

#### Scenario: 規矩改版
- **WHEN** harness 發布了新版本 `@v1`，下一次 `open` 的開場句仍不指定版本
- **THEN** agent 讀到 `@v1`（版本號最大）；若開場句指定 `@v0`，agent 讀 `@v0`（舊版仍在、內容不變）

#### Scenario: sidecar 不知道 harness
- **WHEN** 檢查 sidecar 的請求欄位與設定
- **THEN** 沒有 harness 相關的欄位或檔案（本設計不改 sidecar）

### Requirement: 能力需求與模型欄位
`capabilities` 章節 MUST 以能力名為 H3 子節；子節名 SHOULD 用環境（MyLinuxPool）的 capability 名（v0：`github`、`angareion`、`agora`；`github` 的單位是 `shared-configs/gh`）；**v0 職務檔 MUST 含 `angareion` 子節**（一般職務 SHOULD 即可）；子節內 MUST 用一行 YAML 或清單寫參數，沒有參數時留空；同一份 harness 內的子節名 MUST NOT 重複。未知的能力名 MUST 只警告、MUST NOT 擋（能力清單要能長）。**模型不是能力子節**：frontmatter MUST 有 `models`，含 `floor`（模型識別碼或 `null`）與 `preferred`（有順序的清單，可空）。租用模型時 MUST 先照 `preferred` 的順序滿足，且 MUST NOT 低於 `floor`；滿足不了 MUST 停下並回報，MUST NOT 靜默降級。v0 的能力由 worker 的預設 profile 全部提供；harness MUST NOT 指定 profile 名（限制在期 2 由 worker profile 做）。sidecar MUST NOT 對宣告逐項核銷。

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

### Requirement: v0 職務（worker team PM）
識別 `atelier:worker-team-pm@v0` 的職務檔 MUST 以 PM 的一般規矩為底（派工與回報、一張單一個 branch／worktree、審查非作者、不自己寫程式、安全界線、不繞過被擋的動作），並加上 worker 上的差異：**開工前先讀自己的 harness（`atelier show`）、收到新指示前先重讀**；以 Angareion 收指示與回報（閒時 MUST 用阻塞的 `wait` 等，MUST NOT 在迴圈裡反覆查詢模型）；在 worker 上開 PR（MUST NOT 直接改 main）；MUST NOT 讀取或輸出憑證、MUST NOT 碰真機或別的 repo、MUST NOT 繞過被擋的動作、MUST NOT 在工作時改 harness。回報 MUST 含：做了什麼、產出（branch／PR）、測試原樣輸出、沒驗到的事、用的模型。期 1（v0）隊上只有 PM 時，實作與測試由 PM 自己做並在回報標明（本人 2026-10-16 同意）；此暫定由期 3 的三職務（PM／reviewer／impl）正式版取代。

#### Scenario: 收到指示
- **WHEN** PM 的 `wait` 醒來、`read` 讀到指示
- **THEN** 照工單做（一張單一個 branch／worktree）、開 PR、以 Angareion 回報，回報含測試原樣輸出

#### Scenario: 先讀 harness
- **WHEN** PM 被啟動、準備開工
- **THEN** 它先 `atelier show atelier:worker-team-pm` 讀規矩，再讀信道；之後每個新指示前也先重讀

#### Scenario: 被擋不繞過
- **WHEN** 一個動作被安全檢查或憑證擋下
- **THEN** PM 停下來回報，不換方式或換身分再試

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
