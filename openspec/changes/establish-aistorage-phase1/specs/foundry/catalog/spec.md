## Purpose

定義 Foundry 的產出目錄：管理集中、儲存分散。所有產出都登錄在同一份目錄裡，但只有收容產出的真本放在 Foundry，讓人與 AI 從同一個入口找回任何產出。

## ADDED Requirements

### Requirement: 產出目錄登錄所有產出
Foundry MUST 以一份產出目錄登錄產出，每件一筆；每筆 MUST 具備共通 metadata，並 MUST 記錄產出它的 Session。

#### Scenario: 從 Session 找產出
- **WHEN** 你想知道統合 Session S4 產出了哪些東西
- **THEN** 以該 Session id 查產出目錄，列出它的所有產出

### Requirement: 原處產出只登錄出處
真本在自己專案 repo 的產出，MUST 只在產出目錄中登錄出處，Foundry MUST NOT 保存它的副本。期 1 由產生它的 AI 在產出後明確登錄。

#### Scenario: opencode 改了 FinDashboard 的報告
- **WHEN** opencode 在 FinDashboard repo 產出一份新報告並登錄
- **THEN** 產出目錄多一筆指向該報告的連結型項目，報告本身仍只在 FinDashboard repo

### Requirement: 收容產出的真本放 Foundry
沒有自己家的產出（簡報、圖片、影片、一次性報告），真本 MUST 存放在 Foundry 的儲存實體中。期 1 MUST 支援經提交流程轉手、在 design D7 所訂上限以內的單一檔案；數 GB 單一檔案的路徑在待辦清單。超過上限的檔案 MUST 被明確拒收並說明原因，不得悄悄截斷或漏收。

#### Scenario: 一次性報告
- **WHEN** 統合 Session S4 在 Mac 的容器裡產出一份 PDF 報告並登錄
- **THEN** 報告被存進 Foundry 並登錄；那個容器刪除之後，報告仍然取得到

#### Scenario: 超過上限的檔案
- **WHEN** 某個 Session 登錄一個超過期 1 上限的影片
- **THEN** 提交流程拒收它並把拒絕原因發佈在讀取視圖，產出目錄與 Foundry 真本都不變

### Requirement: 從目錄找得到並拿得到
產出目錄 MUST 經由 Foundry 自己的讀取介面查詢，支援以型態、所屬案件、產生者、時間、產出它的 Session 查詢。查詢結果 MUST 足以讓有授權的呼叫者取得產出本身，並 MUST 依與 Agora 讀取介面相同的新鮮度規則附上快照時間。

#### Scenario: 找上個月的報告
- **WHEN** opencode 要找上個月 FinDashboard 那份報告
- **THEN** 它經由 Foundry 的讀取介面查產出目錄，得到能直接取得那份報告的連結

### Requirement: 永久保存
Foundry MUST 預設永久保存收容產出與目錄項目，不得有自動過期。

#### Scenario: 一年前的簡報
- **WHEN** 你要找一年前某個 Session 產出的簡報
- **THEN** 產出目錄仍有該筆，簡報本身仍可取得
