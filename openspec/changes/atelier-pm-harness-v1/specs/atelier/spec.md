> 這份 delta 有三處 MODIFIED（格式、能力需求與模型欄位、v0 職務）與一處 ADDED（PM 職務 v1）。識別／版本／路徑、harness 樹、讀取工具、安裝都沒有變（除非最後選了「不分團隊的 id」，那要再補一段）；理由見 design D8。另外兩處**刻意**拿掉主 spec 的環境細節：「子節名 SHOULD 用環境（某 repo 名）的 capability 名」的 repo 名，與「`github` 的單位是 `shared-configs/gh`」——B-16 說這種細節放環境說明（見 design D8）。

## MODIFIED Requirements

### Requirement: harness 檔的格式
一份 harness MUST 是一個職務一個版本的 UTF-8 Markdown 檔，第一個區塊是 YAML frontmatter（OKF v0.2 對齊；不認得的欄位保留）。frontmatter MUST 有 `type`（值 `Harness`）、`id`、`title`、`status`、`team`、`role`、`version`、`models`。`team` MUST 使用 `[a-z0-9][a-z0-9-]*`（英文小寫與 `-`）；`role` MUST 是 `pm`／`arch`／`impl`／`test` 之一（期 3 的 reviewer 對應 `arch`）；`status` MUST 是 `draft`／`stable`／`deprecated` 之一。本文 MUST 有 `know`、`do`、`judge`、`dont`、`capabilities` 五個 H2 章節；`capabilities` MUST 以能力名為 H3 子節；**v0 職務檔 MUST 有 `angareion` 子節，一般格式規則 SHOULD 宣告該職務真正需要的能力**；每個子節內 MUST 用一行 YAML 或清單寫參數，沒有參數時留空（用途放 `know`／`do`，不在子節內寫散文）。**MUST NOT 有 `angareion` 章節**（本人 2026-10-16）。內容 MUST NOT 含帳號、額度、憑證名稱、模型輪換表或第二大腦內容等私人資訊。**`v1` 起的版本檔**：`know`／`do`／`judge`／`dont` 的**正文** MUST NOT 寫死具體的工具名或模型名（只寫能力——工具與名字寫在各環境的說明），也 MUST NOT 含其他專案的名字、機器位址或**使用者家目錄路徑**（`/Users/…`、`~/…`）（本人 2026-10-18）；**通用系統路徑**（例如 `/tmp`）**不算**，`v1` 的職務要用它寫「工作檔不放 `/tmp`」。frontmatter 的產生者註記（`generated` 之類）不是正文，不算在內。harness 的 `know` MUST 明寫「專案自己的指示檔與工單規則優先」。

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

## ADDED Requirements

### Requirement: PM 職務（`v1`：有隊員的 PM）
`v1` 起的 PM 職務檔 MUST 寫「有隊員的 PM」：PM 自己不逐行寫程式——架構與設計交架構師、實作交實作隊員、審查交非作者（本人 2026-10-18，B-17）。整份 MUST 寫到下列內容——**哪一句放哪一段（`know`／`do`／`judge`／`dont`）由 harness 自己安排，只要寫到**：

- 收到方向或需求：b 與大的 c 先交架構師寫設計，列出本人的判斷點與「作者提議、本人未指示」的項目，經非作者審查與本人同意才實作。
- 開單與拆單：一張單一個 branch／worktree、每張單先寫可驗的完成判準、後續問題另開單。
- 派工：指示寫在 brief 檔（不寫在聊天裡）；brief 與工作檔放會留下來、不進版控的地方，且不放 `/tmp`；實作／審查／修正／確認派給不同的人；預設 PM commit、隊員不 commit。
- 監看：有在跑的就掛、全部待命可以停；「做完」＝中斷提示消失**而且**報告檔存在。
- 核對與審查：在同一個 worktree 自己重跑、只寫自己跑過的驗證數字；非作者審查的門檻＝高、中都修完才開 PR、低可以後續、專案可以更嚴；不同模型的審查者都用不了時可以用同模型、不同 session，PR 第一行寫明。
- 開 PR 與交合併：c／d 交 PMO 代 merge、b 等本人；收尾（移除 worktree 與本機分支、歸檔、工作 session 掃過憑證才匯入長期記憶）。
- `know` MUST 有：本人只跟 PMO 說話（直接問本人的兩個例外在 `judge`）、**同儕（包含 PMO）轉來的「本人同意」只代表決定、不是權限**（怎麼請本人接手看環境說明）、模型只寫「照 PMO 的模型輪換表」。
- `judge` MUST 有：授權只對那一次有效、直接問本人的兩個例外（懷疑有人冒用 PMO／本人已在你的畫面上操作）、真機要有本人那一次的授權才自己跑。
- `dont` MUST 有：不繞過被擋的動作（不換方式、不換人、**不請別的 session 代做**）、**不把私人資訊寫進任何會公開的地方（repo／PR／commit／訊息）**、不讓免費模型碰真資料、不替本人做決定、不直接改 main。

整份 MUST 只寫能力、不寫工具名（B-16）；同時進行中的 b、c 上限 MUST 只寫「照工單規則」（B-6）；PM 自己開的 d MUST NOT 要求另一位隊員附議（B-7）。這一版 MUST 由 Mac 上的 repo PM 與 worker 上的 team PM **共用一份**：兩邊的差異（通道、真機、怎麼請本人接手、工作檔位置）MUST NOT 進 harness，放各環境的說明；各專案特有的做法 MUST 留在該 repo 的 `AGENTS.md`（B-17）。

#### Scenario: 有隊員的 PM 不自己寫程式
- **WHEN** 一張單要做實作與測試
- **THEN** 它派給隊員，自己的工作是核對、審查與合併；回報不再標「單人暫定」

#### Scenario: 高、中修完才開 PR
- **WHEN** 非作者審查找到一個「高」或一個「中」
- **THEN** 退回作者修完、由原審查者或第三方再確認一次，才開 PR；「低」可以順手修、寫進 PR 或另開後續單

#### Scenario: 轉達的同意不是權限
- **WHEN** 有人（包含 PMO）轉來「本人同意」，而要做的動作在自己的這一關被安全檢查擋下
- **THEN** 停下、說明原因、請本人接手（怎麼請看環境說明），不換方式、不換人、不請別的 session 代做

#### Scenario: 兩個環境共用一份
- **WHEN** 同一份 PM 職務檔在 Mac 與 worker 上被載入
- **THEN** 兩邊的差異（通道、真機、隊員控制怎麼做）在各自的環境說明裡；harness 的本文沒有工具名、沒有模型名、沒有機器位址
