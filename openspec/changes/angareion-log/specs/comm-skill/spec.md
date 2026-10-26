## RENAMED Requirements

- FROM: `### Requirement: 對外只有 send 與 inbox 兩個指令`
- TO: `### Requirement: 對外只有 send、inbox、log 三個指令`

## MODIFIED Requirements

### Requirement: 對外只有 send、inbox、log 三個指令
skill MUST 對外只提供 `send`、`inbox` 與 `log` 三個子指令（另有 `--version`／`--help`）。skill MUST NOT 提供 channel 建刪、開關 agent、單獨戳 agent、狀態查詢或 ack 等指令。

#### Scenario: 不認得的子指令
- **WHEN** 呼叫 `aicomm poke` 或 `aicomm status`
- **THEN** exit 1，stderr 說明只有 `send`、`inbox` 與 `log`

## ADDED Requirements

### Requirement: log 薄包 angareion log，原樣轉發、不叫醒
`aicomm log` MUST 以 Angareion 的 `log` 指令查詢：

- 除 `--to`／`--mentioned` 的注入規則外，MUST NOT 自行解析或驗證 `log` 的旗標——`log` 之後的引數原樣交給 `angareion log`（subprocess），stdout／stderr 原樣轉發，結束碼跟著 Angareion（成功 0、用法錯 1、後端或設定錯 2）；錯誤由 Angareion 報，skill 不重複實作旗標語意。
- **預設 `--to me`**：使用者沒有給 `--to` 也沒有給 `--mentioned` 時，MUST 自動補 `--to me`；使用者給了任一個，MUST NOT 注入（以使用者的為準）。「使用者給了」的判斷 MUST 認得等價寫法（不只是 `--to` 空格寫法）：對每個使用者引數取 `=` 之前的字面、去掉 `--`，若恰是 `to` 或 `mentioned` 的**前綴**，就算使用者已指定收件人（涵蓋 `--to <值>`、`--to=<值>`、`--mentioned`，與 argparse 會接受的未歧義縮寫如 `--t`、`--m`、`--ment`）；注入 MUST 放在使用者引數**之前**（argparse 同名旗標重複時「後給的贏」，放前面保證使用者值即使漏判也贖得回）。補的是字面 `me`，skill MUST NOT 自己解析身分（skill 不讀 Angareion 的設定檔；`me` 由 angareion 解析為「寄給我的」＝`to` 是我、或廣播、或 `mentions` 有我）。
- **不叫醒**：`log` MUST NOT 對 sidecar 發出任何請求（查 agent 或戳都不發）；叫醒是 `send` 的副作用。
- `log` MUST NOT 提供 `--wait`／`--interrupt`／`--no-wake`（沒有等待與叫醒語意）；引數裡出現 Angareion 不認得的旗標時照 Angareion 的回應（exit 1）轉發。
- `--json` MUST 原樣轉發 Angareion 的 JSON 輸出。
- `log` 是 inbox 之後的補查工具：skill 的說明 MUST 寫明「被叫醒先 `aicomm inbox`；要前後文或補讀用 `aicomm log`，不要用 `angareion read --since` 反覆撈」與「`log` 不動游標」。

#### Scenario: 預設補 --to me
- **WHEN** `aicomm log --since 2026-10-25T00:00:00Z`
- **THEN** 對 angareion 實際送出 `log --since 2026-10-25T00:00:00Z --to me`；輸出與結束碼原樣

#### Scenario: 使用者給了 --to 就不注入
- **WHEN** `aicomm log --to mypmo-pm-m1 --grep blocker`
- **THEN** 對 angareion 送出 `log --to mypmo-pm-m1 --grep blocker`（原樣，沒有補 `--to me`）

#### Scenario: 等號的 --to=<值> 也不注入（不會被靜默蓋掉）
- **WHEN** `aicomm log --to=mypmo-pm-m1 --grep blocker`
- **THEN** 對 angareion 送出 `log --to=mypmo-pm-m1 --grep blocker`（原樣，沒有補 `--to me`——argparse 合法的等號寫法不會被掃描漏掉）

#### Scenario: 縮寫的 --mentioned 也不注入
- **WHEN** `aicomm log --m --grep blocker`（argparse 的未歧義縮寫＝`--mentioned`）
- **THEN** 不注入；對 angareion 轉出 `log --m --grep blocker`（原樣），`--to me` 沒有悄悄與它並存

#### Scenario: 使用者給了 --mentioned 就不注入
- **WHEN** `aicomm log --mentioned --channel team-alpha`
- **THEN** 對 angareion 送出 `log --mentioned --channel team-alpha`（原樣，沒有補 `--to me`）

#### Scenario: 原樣轉發結束碼與輸出
- **WHEN** Angareion 對 `log` 回 exit 1（例如旗標用法錯）或 exit 2（後端錯）
- **THEN** `aicomm log` 以同一結束碼結束，stdout／stderr 內容與 Angareion 相同（aicomm 不加提示，除了原本就可能有的 sidecar 導向輸出不會出現在 log）

#### Scenario: log 不碰 sidecar
- **WHEN** 對假 sidecar 檢查 `aicomm log` 期間的所有請求
- **THEN** 沒有任何請求（GET／POST 都沒有），無論收件人身分為何

#### Scenario: SKILL.md 補 log 的分工句
- **WHEN** 檢查 `skills/aicomm/SKILL.md`
- **THEN** 有「被叫醒先 inbox；要前後文或補讀用 log」與「不要用 `read --since` 反覆撈」的明文，並把 `log` 列進指令清單（三個指令）