## MODIFIED Requirements

### Requirement: 同一台機器上不能同時接續同一個 Session
一個 Session 正在接續中（這台機器上有另一個 `agora continue` 正在寫回它）時，再對它 `agora continue`（不含 `--fork`）MUST 被拒絕，exit 4，並在中文說明之前先印一行固定格式 `agora-locked <agora id>`（呼叫方不必解析中文；#54）。被鎖的訊號 MUST 和一般輸入錯誤的 exit 1 分開。對它 `--fork` 不受影響。

拒絕 MUST 涵蓋兩條路徑：開頭的提早檢查（`store.continuing`），以及真的拿 `pending/<ULID>.json` 的鎖時。兩個行程幾乎同時接續時，開頭的檢查看不到後到的鎖；輸的那個 MUST NOT 覆蓋贏的那個的 pending 記錄、MUST NOT 啟動 agent，而且 MUST 給同一個訊號。

#### Scenario: 同一台機器再接續一次
- **WHEN** A 正在接續中，在另一個終端機 `agora continue session A --agent claude`
- **THEN** 被拒絕：exit 4，`agora-locked agora:A` 這一行在說明之前，說明 A 正在接續中；第一個接續不受影響

#### Scenario: 幾乎同時接續同一個 Session
- **WHEN** 兩個 `agora continue` 幾乎同時接續 A，其中一個先拿到 `pending/<ULID>.json` 的鎖
- **THEN** 另一個在拿鎖時被拒絕：exit 4，同樣先印 `agora-locked agora:A`；沒有啟動 agent，先到的那個 pending 記錄沒有被覆蓋

#### Scenario: 不是被鎖的輸入錯誤
- **WHEN** `agora continue session agora:…`（不存在的 id）`--agent claude`
- **THEN** 照舊 exit 1，不是 4；被鎖的訊號只有同機接續中會給

#### Scenario: 被鎖時 fork 照常
- **WHEN** A 正在接續中，`agora continue session A --agent claude --fork`
- **THEN** 照常 fork 並接續新的 Session，exit 0
