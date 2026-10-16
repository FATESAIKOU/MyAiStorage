## ADDED Requirements

> 註（暫定）：以下「能力需求」的落點暫依 design D1 的建議甲（能力需求為第五要素；`angareion` 是 `capabilities` 的子節）與 D9 的表示法（H3 子節）。待 E1 由本人裁定後同步；若改乙，`angareion` 成為獨立的 H2 章節。

### Requirement: harness 檔的格式
一份 harness MUST 是一個職務一份 UTF-8 的 Markdown 檔，第一個區塊是 YAML frontmatter（OKF v0.2 對齊；不認得的欄位保留）。frontmatter MUST 有 `type`（值 `Harness`）、`id`、`title`、`status`、`team`、`role`、`version`、`models`。`team` MUST 使用 `[a-z0-9][a-z0-9-]*`（英文小寫與 `-`）；`role` MUST 是 `pm`／`arch`／`impl`／`test` 之一（期 3 的 reviewer 對應 `arch`）；`status` MUST 是 `draft`／`stable`／`deprecated` 之一。本文 MUST 有 `know`、`do`、`judge`、`dont`、`capabilities` 五個 H2 章節；`capabilities` MUST 以能力名為 H3 子節，MUST 有 `angareion` 子節；每個子節內 MUST 用一行 YAML 或清單寫參數，沒有參數時留空（用途放 `know`／`do`，不在子節內寫散文）。內容 MUST NOT 含帳號、額度、憑證名稱、模型輪換表或第二大腦內容等私人資訊。

#### Scenario: 通過格式檢查
- **WHEN** 對一份照本格式寫的 harness 執行 MyAiStorage 的格式檢查
- **THEN** 通過（exit 0）

#### Scenario: 缺必填欄位
- **WHEN** 拿掉 `version`（或 `role`、`models`）後執行格式檢查
- **THEN** 失敗，訊息指出缺哪個欄位

#### Scenario: 章節不齊
- **WHEN** 拿掉 `dont` 章節、拿掉 `capabilities` 的 `angareion` 子節，或把能力子節改成散文（沒有 H3）後執行格式檢查
- **THEN** 失敗

### Requirement: 識別與版本
一份 harness 的識別 MUST 是 `atelier:<team>-<role>@<version>`，MUST 等於 frontmatter 的 `team`、`role`、`version` 組合；`<version>` MUST 是 `v` 加非負整數。識別 MUST 能直接用在四實體的 ref 語法（`docs/design.md` §3.2），例：`atelier:worker-team-pm@v0`。內容有變時 MUST 換版本；同一個版本 MUST NOT 對應不同內容。讀取方（sidecar）只比對版本字串，MUST NOT 解讀本文。

#### Scenario: id 與欄位一致
- **WHEN** 格式檢查一份 `id: atelier:worker-team-pm@v0`、`team: worker-team`、`role: pm`、`version: v0` 的檔
- **THEN** 通過

#### Scenario: 版本不一致被擋
- **WHEN** `id` 的 `@` 版本與 frontmatter 的 `version` 不同（例 `@v1` 配 `v0`）
- **THEN** 格式檢查失敗

### Requirement: 能力需求與模型欄位
`capabilities` 章節 MUST 以能力名為 H3 子節；子節名 SHOULD 用環境（MyLinuxPool）的 capability 名（v0：`github`、`angareion`、`agora`；`github` 的單位是 `shared-configs/gh`），MUST 含 `angareion` 子節；子節內 MUST 用一行 YAML 或清單寫參數，沒有參數時留空；同一份 harness 內的子節名 MUST NOT 重複。未知的能力名 MUST 只警告、MUST NOT 擋（能力清單要能長）。**模型不是能力子節**：frontmatter MUST 有 `models`，含 `floor`（模型識別碼或 `null`）與 `preferred`（有順序的清單，可空）。租用模型時 MUST 先照 `preferred` 的順序滿足，且 MUST NOT 低於 `floor`；滿足不了 MUST 停下並回報，MUST NOT 靜默降級。v0 的能力由 worker 的預設 profile 全部提供；harness MUST NOT 指定 profile 名（限制在期 2 由 worker profile 做）。sidecar MUST NOT 對宣告逐項核銷。

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

### Requirement: 載入
sidecar 啟動 team 時，MUST 依呼叫方指定的 harness（名稱，版本可省）讀取 harness 檔，並 MUST 把**檔案全文原樣**作為 agent 的第一則輸入送出——MUST NOT 摘要、改寫、截斷或調整格式；MUST NOT 解析本文（只讀 frontmatter 的 `type`／`id`／`version`）；MUST 把 harness 排在一般 `input` 之前。載入 MUST NOT 依賴 agent 種類（claude-code 與 opencode 行為相同）、MUST NOT 修改工作目錄的任何檔案、MUST NOT 把內文寫進 log 或 status。呼叫方省略版本時 MUST 用部署中的版本。每次 `open`（新開或接續）MUST 都載入一次。

#### Scenario: 第一則輸入是全文
- **WHEN** 呼叫 `open` 並指定 `harness: {"name": "worker-team-pm", "version": "v0"}`（agent 可用、工作目錄存在）
- **THEN** agent 起來後收到的第一則輸入與 harness 檔的位元組內容一致（原樣全文），之後才輪到呼叫方的一般 `input`

#### Scenario: 接續也載入
- **WHEN** `open` 帶 `session`（agora id）接續既有 Session，並指定 harness
- **THEN** harness 全文照樣作為這次的第一則輸入送出

#### Scenario: 不碰工作目錄
- **WHEN** 完成一次帶 harness 的 `open`
- **THEN** 工作目錄沒有任何新檔案或改動（`git status` 不變）

### Requirement: 載入的錯誤情形
找不到 harness 檔 MUST 以 400 `harness_not_found` 拒絕；請求版本與檔案不符 MUST 以 400 `harness_version_mismatch` 拒絕；frontmatter 無法解析或缺 `type`／`id`／`version` MUST 以 400 `harness_invalid` 拒絕。三種情形 MUST NOT 開 agent、MUST NOT 留下任何部分狀態；錯誤訊息 MUST NOT 含 harness 內文。沒有帶 harness 欄位時 MUST 照舊開 agent（不載入）。

#### Scenario: 找不到
- **WHEN** `harness_dir` 下沒有 `worker-team-pm.md` 而 `open` 指定它
- **THEN** 回 400 `harness_not_found`；沒有 agent 被開、沒有 Session 被動

#### Scenario: 版本不符
- **WHEN** `open` 指定 `version: v1` 而部署的檔案是 `v0`
- **THEN** 回 400 `harness_version_mismatch`；什麼都沒開

#### Scenario: 壞檔
- **WHEN** 檔案存在但 frontmatter 解析失敗（或沒有 `id`）
- **THEN** 回 400 `harness_invalid`；什麼都沒開

#### Scenario: 不帶欄位照舊
- **WHEN** `open` 不帶 harness 欄位
- **THEN** 行為與沒有這個功能時相同（照舊開 agent）

### Requirement: v0 職務（worker team PM）
識別 `atelier:worker-team-pm@v0` 的職務檔 MUST 以 PM 的一般規矩為底（派工與回報、一張單一個 branch／worktree、審查非作者、不自己寫程式、安全界線、不繞過被擋的動作），並加上 worker 上的差異：以 Angareion 收指示與回報（閒時 MUST 用阻塞的 `wait` 等，MUST NOT 在迴圈裡反覆查詢模型）、在 worker 上開 PR（MUST NOT 直接改 main）、MUST NOT 讀取或輸出憑證、MUST NOT 碰真機或別的 repo、MUST NOT 繞過被擋的動作。回報 MUST 含：做了什麼、產出（branch／PR）、測試原樣輸出、沒驗到的事、用的模型。期 1（v0）隊上只有 PM 時，實作與測試由 PM 自己做並在回報標明；此暫定由期 3 的三職務（PM／reviewer／impl）正式版取代。

#### Scenario: 收到指示
- **WHEN** PM 的 `wait` 醒來、`read` 讀到指示
- **THEN** 照工單做（一張單一個 branch／worktree）、開 PR、以 Angareion 回報，回報含測試原樣輸出

#### Scenario: 被擋不繞過
- **WHEN** 一個動作被安全檢查或憑證擋下
- **THEN** PM 停下來回報，不換方式或換身分再試

#### Scenario: 單人暫定要標明
- **WHEN** 期 1 的 PM 自己完成實作與測試
- **THEN** 回報中標明是 v0 的單人暫定（自己實作、沒有非作者審查），並留給 PMO／本人決定下一步

### Requirement: 與 Agora Session 的關係
harness MUST NOT 存進 Agora（它不是 Session，也不是項目）；MUST NOT 要求 Agora 認識 Atelier。Session 的 header MAY 在 `refs` 記 harness 的識別（例 `atelier:worker-team-pm@v0`）作為註記；那是選填、MUST NOT 影響載入，v0 MUST NOT 自動寫入。

#### Scenario: 載入不需要 Agora
- **WHEN** 在一台沒有配置任何 Agora 儲存的地方解析與檢查 harness 檔
- **THEN** 格式檢查照常通過（harness 與 Agora 不相依）

#### Scenario: refs 只是註記
- **WHEN** 一個 Session 的 `refs` 含 `atelier:worker-team-pm@v0`
- **THEN** 讀寫該 Session 的行為不變（`refs` 就只是一個字串）
