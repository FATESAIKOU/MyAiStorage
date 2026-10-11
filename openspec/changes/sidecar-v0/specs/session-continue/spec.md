## ADDED Requirements

### Requirement: 程式化開法
`agora continue session <id> --agent … [--fork] [--sync-period N] [--dir <dir>] --headless [--headless-port <埠>]` MUST 照常準備 Session（continue 或 fork）、上鎖、定期寫回、在 agent 結束時寫回，只是用 agent 的程式化模式開它，而不是互動的 TUI：Claude Code 用 stream-json 的輸入與輸出，並固定帶 `--settings '{"autoContinueAtUsageLimit": false}'`；opencode 用它的 server 模式，埠由 `--headless-port` 指定。agent 跑著時，agora MUST NOT 寫 stdout（agent 的 stdin／stdout 直接交給呼叫方），agora 的說明一律寫 stderr。

Session 存好、agent 開起來之前，agora MUST 在 stderr 印一行 `agora-session <id>`（id 是接續的那個，`--fork` 時是新的那個；這一行是固定格式，其餘說明不是）。opencode 時再印一行 `agora-port <埠>`。

#### Scenario: Claude Code 以 stream-json 接續
- **WHEN** `agora continue session agora:A --agent claude --headless`，呼叫方從 stdin 送一則 stream-json 的 user 訊息，之後關掉 stdin
- **THEN** 呼叫方從 stdout 讀到 agent 的事件與 `result`；agent 結束後 A 的對話多了那一輪

#### Scenario: fork 加上程式化開法
- **WHEN** `agora continue session agora:A --agent claude --fork --headless`
- **THEN** 和互動的 fork 一樣多一個 `-fork` 的 Session，agent 以程式化模式開在新的 Session 上；A 不變；stderr 有 `agora-session <新的 id>`，stdout 沒有 agora 的任何輸出

#### Scenario: opencode 以 server 模式接續
- **WHEN** `agora continue session agora:A --agent opencode --headless --headless-port 8123`
- **THEN** opencode 在 127.0.0.1:8123 聽著，接在 A 的對話上；stderr 有 `agora-port 8123`；agent 結束後 A 多了那一輪

#### Scenario: 溫和停下
- **WHEN** headless 的 agora 行程收到 SIGTERM
- **THEN** agora 轉給 agent、等它結束、做最後一次寫回、放掉同一個 Session 的鎖之後退出，exit 0

#### Scenario: 被強制殺掉
- **WHEN** headless 的 agora 行程被 SIGKILL
- **THEN** 不做最後一次寫回（最多掉一個 `--sync-period` 的對話）；鎖是行程持有的（`flock`），行程沒了就釋放，下一次 `continue` 這個 Session MUST 不被擋

### Requirement: 附加 tag
`agora continue`（含 `--fork`、`--headless`）與 `agora import` MUST 接受可重複的 `--add-tag <tag>`：把 tag 附加到寫回的那個 Session 標頭的 `tags`，原有的 tags MUST 保留（和 `--header tags=…` 的取代不同）；已經有的相同 tag MUST NOT 重複。不 fork 的 `continue` 把 tag 加在原 Session 上。

#### Scenario: fork 時附加
- **WHEN** A 的 tags 是 `[ui, tui]`，`agora continue session agora:A --agent claude --fork --headless --add-tag sidecar:t1`
- **THEN** 新 Session 的 tags 是 `[ui, tui, sidecar:t1]`；A 的 tags 不變

#### Scenario: 不 fork 時附加
- **WHEN** `agora continue session agora:A --agent claude --headless --add-tag sidecar:t1`
- **THEN** A 的 tags 是原有的加上 `sidecar:t1`

#### Scenario: 重複的 tag
- **WHEN** A 已經有 `sidecar:t1`，再 `--add-tag sidecar:t1`
- **THEN** tags 裡只有一個 `sidecar:t1`

#### Scenario: import 時附加
- **WHEN** `agora import session --external-session-id X --agent claude --add-tag sidecar:t1`
- **THEN** 匯入的 Session 的 tags 含 `sidecar:t1`
