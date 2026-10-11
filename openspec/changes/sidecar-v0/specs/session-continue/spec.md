## ADDED Requirements

### Requirement: 程式化開法
`agora continue session <id> --agent … [--fork] [--sync-period N] --headless` MUST 照常準備 Session（continue 或 fork）、上鎖、定期寫回、在 agent 結束時寫回，只是用 agent 的程式化模式開它，而不是互動的 TUI：Claude Code 用 stream-json 的輸入與輸出；opencode 用它的 server 模式。agent 跑著時，agora MUST NOT 寫 stdout（agent 的 stdin／stdout 直接交給呼叫方），agora 的說明一律寫 stderr。

#### Scenario: Claude Code 以 stream-json 接續
- **WHEN** `agora continue session agora:A --agent claude --headless`，呼叫方從 stdin 送一則 stream-json 的 user 訊息，之後關掉 stdin
- **THEN** 呼叫方從 stdout 讀到 agent 的事件與 `result`；agent 結束後 A 的對話多了那一輪

#### Scenario: fork 加上程式化開法
- **WHEN** `agora continue session agora:A --agent claude --fork --headless`
- **THEN** 和互動的 fork 一樣多一個 `-fork` 的 Session，agent 以程式化模式開在新的 Session 上；A 不變
