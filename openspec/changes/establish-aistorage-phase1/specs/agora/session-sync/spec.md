## Purpose

定義 Session 從來源應用進入 Agora 的方式：每個來源應用一個同步器，平時定期同步、接續前再同步一次，讓接續點有明確的定義；並定義寫入端如何維持新鮮度，讓讀取端不必知道寫入是怎麼做的。

## ADDED Requirements

### Requirement: 每個來源應用一個同步器
每個來源應用 MUST 透過自己的同步器把 Session 送進 Agora；新增一個來源應用 MUST 只需要新增一個同步器，不需要修改 Agora 或其他同步器。期 1 MUST 提供 opencode 同步器（在 Mac 上執行）。

#### Scenario: 之後加入手機 App
- **WHEN** 之後要讓手機 App 的 Session 進入 Agora
- **THEN** 只需要新增一個手機 App 同步器（與它的轉換器）

### Requirement: 定期同步
同步器 MUST 定期把運作中 Session 的最新內容同步進 Agora，並附上這次擷取的快照時間。同步 MUST 可以重複執行：同一內容同步兩次，不會在 Agora 產生重複或新版本。

#### Scenario: 重複同步
- **WHEN** 同步器對一個沒有新內容的 Session 再同步一次
- **THEN** Agora 中該 Session 沒有任何變化

### Requirement: 接續前同步
被接續 Session 的持有者 MUST 在寫交接單時同步該 Session，並與交接單一起提交；接續點 SHALL 是已經提交、或與交接單同一批放進收件匣的位置。提交流程 MUST 在同一次執行裡先收原始紀錄、再收交接單，並拒絕接續點超過已收進真本位置的交接單。接續 MUST NOT 要求持有者以外的任何一方觸發同步。

#### Scenario: 分裂前同步
- **WHEN** 你在 Mac 的 opencode Session S1 剛講完三句話，就要它分裂成兩份工作
- **THEN** S1 同步後與兩張交接單一起提交（只需要一次提交流程），接續點都包含那三句話，認領它們的 Session 讀得到那三句

### Requirement: 寫入端維持新鮮度
一個 Session 在 Agora 能維持多新，SHALL 由寫入機制（同步頻率、提交流程的觸發方式）決定。寫入機制 MAY 依成本選擇並逐步追加；追加寫入機制 MUST NOT 改變讀取介面。期 1 MUST 提供最便宜的一組：定期同步、定時提交，以及寫入者主動「同步並提交」（寫交接單前必用；想讓其他 Session 讀到進度時也可以用）。「同步並提交」的完成 MUST 以讀取介面看得到這次放進收件匣的每一個項目（快照、交接單、認領、參考 Link、產出登錄等）或它的拒絕原因為準，不以某一次提交流程的執行結果為準。

#### Scenario: 主動發佈進度
- **WHEN** 並行中的 S2 完成一段別的 Session 需要的工作，主動同步並提交
- **THEN** 提交流程完成之後，其他 Session 經由讀取介面讀到的 S2 快照時間是這次同步擷取的時間（不早於 S2 發起同步的時刻）

### Requirement: 同步不以其他執行體存在為前提
一個來源應用的同步 MUST NOT 依賴其他來源應用或任何 AiContainer 機器在線。（期 1 的同步器只在 Mac 上、完全不碰 AiContainer，這條由架構保證，第 9 組不另外驗收。）

#### Scenario: 家裡斷網
- **WHEN** 家裡的網路斷了、所有 AiContainer 機器都離線，而 Mac 在外面連得上網路
- **THEN** Mac 上的 opencode 同步器仍然能把 Session 同步進 Agora
