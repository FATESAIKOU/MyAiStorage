## ADDED Requirements

### Requirement: search 每一行顯示最後更新時間
`agora search session` 的每一行 MUST 顯示該 Session 的最後更新時間（標頭的 `agora.updated_at`），不再只有建立日／對話日期。行的形狀：

`agora:<ULID>  <對話日期>[（更新：YYYY-MM-DD）]  <agent 或 relation>  <標題或摘要>  <標記>…`

- 對話日期是現行的 `store.sort_date`（來源對話、`generated.at`、匯入時間）；**清單的排序 MUST 維持現行**（對話日期新到舊，`updated_at` 已是次鍵），不因這個改動改變。
- `agora.updated_at` 與對話日期**不同**時，MUST 緊接在對話日期後面補上 `（更新：YYYY-MM-DD）`（只到日、不含空白，維持每行的空格分欄）；相同時 MUST 省略。`agora.updated_at` 沒有時（舊標頭）MUST 只顯示對話日期。

#### Scenario: 很久以前對話、今天被寫回
- **WHEN** 一個 Session 的對話日期是 2026-08-01、今天（2026-10-18）被寫回
- **THEN** 那一行是 `agora:…  2026-08-01（更新：2026-10-18）  …`，不會看起來像 8 月就沒動過

#### Scenario: 剛匯入、來源對話是更早的
- **WHEN** 今天匯入一個來源對話日期是 2026-10-01 的 Session
- **THEN** 那一行是 `agora:…  2026-10-01（更新：2026-10-18）  …`（兩個日期不同就都看得到）

#### Scenario: 舊標頭沒有 updated_at
- **WHEN** 一個 Session 的標頭沒有 `agora.updated_at`
- **THEN** 那一行只顯示對話日期，沒有「（更新：…）」；指令不因此失敗

### Requirement: search 標出本機使用中
本機正在接續中的 Session（`pending/<ULID>.json` 的鎖有人拿著；`store.continuing`，見 `session-continue`）MUST 在該行標成 `(使用中)`，與 `(未上傳)`、`(雲端沒有)` 並存。只是留下來的記錄（鎖沒人拿）MUST NOT 算使用中。

「使用中」只判斷**這台機器**的鎖：別台機器上的接續從這裡看不到（沒有共用的鎖），MUST NOT 假裝看得到；這個限制 MUST 寫在使用者文件（README）裡。

#### Scenario: 本機正在接續
- **WHEN** A 正在這台機器上被接續，`agora search session`
- **THEN** A 那一行有 `(使用中)`

#### Scenario: 收不了尾的記錄
- **WHEN** `pending/<ULID>.json` 留著但沒有任何人拿它的鎖
- **THEN** 那一行不標 `(使用中)`（而且下一次執行指令會把它補存或警告）

#### Scenario: 別台機器上的接續
- **WHEN** B 正在別台機器上被接續，這台執行 `agora search session`
- **THEN** 這台的清單不標 B 使用中；這是已知限制，README 寫明
