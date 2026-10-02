# batch-commands Specification

## Purpose
import、delete、merge 一次處理多個 Session，逐一顯示進度，被中斷之後重跑同一個指令會接著做，不重做已經完成的部分。
## Requirements
### Requirement: import 一次多個
`agora import session --external-session-id <id>[,<id>…]` MUST 接受多個 agent session id（`--external-session-id` 可以給多次，也可以用逗號分隔）。MUST 先同步一次，再逐一匯入；某一個失敗 MUST 照樣做下一個，最後 exit code 是第一個非零的（全部成功是 0）。只給一個 id 時，行為與 exit code 和以前相同。

#### Scenario: 其中一個失敗
- **WHEN** 給三個 id，其中一個不存在
- **THEN** 另外兩個被匯入並印出 agora id，stderr 說明失敗的那個，exit 非零

#### Scenario: 中斷後重跑
- **WHEN** 匯入三個時在第二個被中斷，再執行同一個指令
- **THEN** 第一個因為內容沒變而略過，只做剩下的

### Requirement: 進度
import、delete、merge、pull、push MUST 在處理多個項目時，逐一在 stderr 印出一行含 `k/N` 的進度。
格式是 `[agora]`、一個空格、動作名、空白、`k/N`、兩個空格、該項的 id（沒有冒號），例如 `[agora] 匯入 2/5  ses_…`、`[agora] 來源 1/3  agora:01K6…`。stdout 只放結果（agora id），可以接到管線。

#### Scenario: 管線不被進度弄髒
- **WHEN** 執行 `agora delete session A B --yes | wc -l`
- **THEN** 結果是 2；進度只出現在 stderr

### Requirement: delete 重跑略過已刪除
`agora delete session <id>… --yes` 中，agora 自己刪過的 id（記錄在本機）MUST 被略過並印出「已經不在了」，不算失敗。從來不存在的 id（例如打錯）MUST 照樣報找不到。

#### Scenario: 中斷後重跑
- **WHEN** 刪三個時在第二個之後被中斷，再執行同一個指令
- **THEN** 已刪的兩個被略過，第三個被刪，exit 0

#### Scenario: 打錯的 id
- **WHEN** 給一個從來不存在的 id
- **THEN** exit 1，訊息說找不到

### Requirement: merge 沿用已寫好的要約
merge 每寫好一個來源的要約，MUST 先存在本機；重跑時，同一個 agent、同一份提示詞、同一個模型設定、同一段實際送出的文字所寫的要約 MUST 被沿用（沿用前 MUST 再用 schema 驗證一次），只寫還沒寫的。任何一項不同就重寫。

#### Scenario: 在第二個來源被中斷
- **WHEN** 合併兩個來源，寫完第一個的要約後被中斷，再執行同一個指令
- **THEN** 只為第二個來源叫一次 AI，第一個沿用，merge 成功

#### Scenario: 來源改過
- **WHEN** 第一個來源的內容在重跑前被改過
- **THEN** 第一個來源的要約重寫

