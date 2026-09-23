## Purpose

定義 Session 從來源應用進入 Agora 的方式：每個來源應用一個同步器，平時定期同步、接續前再同步一次，讓接續點有明確的定義，也不讓用完就丟的 worker 帶走工作。

## ADDED Requirements

### Requirement: 每個來源應用一個同步器
每個來源應用 MUST 透過自己的同步器把 Session 送進 Agora；新增一個來源應用 MUST 只需要新增一個同步器，不需要修改 Agora 或其他同步器。期 1 MUST 提供手機 App 與 opencode 兩個同步器。

#### Scenario: 之後加入 Claude Code
- **WHEN** 期 2 要讓 Mac 上的 Claude Code Session 進入 Agora
- **THEN** 只需要新增一個 Claude Code 同步器

### Requirement: 定期同步
同步器 MUST 定期把運作中 Session 的最新內容同步進 Agora。同步 MUST 可以重複執行：同一內容同步兩次，不會在 Agora 產生重複或新版本。

#### Scenario: 重複同步
- **WHEN** 同步器對一個沒有新內容的 Session 再同步一次
- **THEN** Agora 中該 Session 沒有任何變化

### Requirement: 接續前同步
發起接續的一方 MUST 在建立接續之前，把被接續的 Session 同步一次；接續點 SHALL 是這次同步到的位置。

#### Scenario: 秘書交接
- **WHEN** 你在手機上剛講完三句話就要秘書把 S1 交給員工
- **THEN** 秘書先同步 S1，接續點包含那三句話，員工讀得到它們

### Requirement: 參考讀取最新已同步版本
參考 MUST 讀取 Agora 中當下最新已同步的版本，SHALL NOT 要求來源應用即時同步。

#### Scenario: 參考一個運作中的 Session
- **WHEN** 員工參考一個仍在手機上進行的 Session
- **THEN** 它讀到的是最後一次同步的內容，手機 App 不會被要求做任何事

### Requirement: worker 刪除前同步完成
員工在 worker 上產生的 Session MUST 在該 worker 被刪除之前同步完成。同步沒有完成時，刪除流程 MUST 讓你知道有尚未同步的 Session。

#### Scenario: 刪除還有未同步內容的 worker
- **WHEN** 你在手機上刪除一個 worker，而它上面的 opencode Session 還有未同步的內容
- **THEN** 系統先完成同步；同步失敗時，刪除流程明確告知有哪些 Session 未同步

### Requirement: 同步不以另一邊存在為前提
手機 App 的同步 MUST NOT 依賴任何 AiContainer 機器在線；AiContainer 的同步 MUST NOT 依賴手機在線。

#### Scenario: 家裡斷網
- **WHEN** 家裡的網路斷了、所有 worker 都離線
- **THEN** 手機 App 仍然能把 Session 同步進 Agora
