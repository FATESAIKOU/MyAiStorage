## ADDED Requirements

### Requirement: 未匯入頁讀本機 opencode 的儲存
未匯入頁 MUST 以唯讀方式讀本機 opencode 自己的儲存（`XDG_DATA_HOME`，預設 `~/.local/share` 底下的 `opencode/opencode.db`）列出所有專案的 Session（新到舊）、顯示最後一則 user／assistant 文字的預覽、並支援對話內文的搜尋。opencode 2.x 的資料表（`session_v2`、`session_message`）與 1.x 的（`session`、`message`／`part`）都 MUST 能讀；兩者都在時 MUST 以 2.x 的資料為準，但**只出現在 1.x 表的 Session MUST 仍被列出**（以 id 聯集，同一個 id 用 2.x 的列；升級沒有遷移舊 Session 不會把它們藏掉）。讀不到檔案、結構認不出來時 MUST 只警告一次並顯示空的清單／沒有預覽，MUST NOT 讓互動模式出錯。

- 預覽 MUST 只掃最近的幾則訊息並截斷長度（不把整份對話讀進來）；只算 user／assistant 的文字，附件不算。
- 搜尋 MUST 不分大小寫、正規化後比對，每一個命中都由程式確認；同一個 Session 只出現一次。
- 讀取單一 Session 的專案目錄失敗時（資料表不存在、查不到、目錄已經不見）MUST 回 None，MUST NOT 丟錯。

#### Scenario: v2 的資料庫列得出來
- **WHEN** 本機 opencode 2.x 的資料庫有兩個不同專案的 Session
- **THEN** 未匯入頁兩個都列出來，新到舊，專案目錄與標題正確

#### Scenario: 舊 Session 沒有被遷移
- **WHEN** 升級後 v2 的表沒有某個舊 Session，它只出現在 v1 的表
- **THEN** 未匯入頁照樣列出它；同一個 Session 同時在兩表時用 v2 的資料，清單不出現兩次

#### Scenario: v2 的預覽
- **WHEN** 在未匯入頁選一個 v2 的 Session
- **THEN** 預覽顯示它最後一則 user 或 assistant 的文字；`idle` 這種不是對話的列不顯示

#### Scenario: v2 的搜尋
- **WHEN** 在未匯入頁搜一個只出現在某個 v2 Session 內文的字
- **THEN** 只有那個 Session 命中；資料庫裡的附件不算

#### Scenario: 認不得的資料庫
- **WHEN** 資料庫的結構不是認得的版本
- **THEN** 清單是空的、stderr 只有一次警告；互動模式照常運作

#### Scenario: 缺表時取專案目錄
- **WHEN** 匯出失敗而 agora 要去資料庫查那個 Session 的專案目錄，但資料表不存在
- **THEN** 回 None（照原來的錯誤處理），不是丟出未賦值的錯誤
