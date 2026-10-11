## ADDED Requirements

### Requirement: 接續時選擇要不要 fork
在 Agora 頁按 Enter 接續時，選 agent 的視窗 MUST 同時有一個「fork」選項，預設不勾。

- 不勾：MUST 照原本的方式接續原本那個 Session，MUST NOT fork。
- 勾了：MUST 等同指令模式的 `agora continue session <id> --agent <agent> --fork`：先複製成新的 Session（title 加 `-fork`）再接續，原本那個不動。之後的工作目錄、同步間隔照常問，答案照常帶給指令模式。
- 原本那個 Session 在這台機器上正在接續中時：
  - 不勾 fork：MUST 在問工作目錄之前就說明它正在接續中、要分岔可以勾 fork，然後停下，不開 agent；
  - 勾了 fork：照常接續。
- 視窗照「按鍵」的規則：Tab／shift+tab 只在視窗裡移動；Esc MUST 取消整個接續。
- 雲端沒有的 Session 照舊不能接續，也不能從這裡 fork。

#### Scenario: 預設不 fork
- **WHEN** 在 Agora 頁對 A 按 Enter，選了 claude，沒有勾 fork，工作目錄與同步間隔都直接按 Enter
- **THEN** 以 `continue session agora:A --agent claude --dir … --sync-period 5` 接續，沒有 `--fork`

#### Scenario: 勾 fork
- **WHEN** 同上，但在選 agent 的視窗勾了 fork
- **THEN** 以 `continue session agora:A --agent claude --dir … --sync-period 5 --fork` 接續

#### Scenario: 正在接續中、沒勾 fork
- **WHEN** A 在這台機器上正在接續中，對 A 按 Enter，沒有勾 fork 就確定
- **THEN** 畫面說明 A 正在接續中、要分岔可以勾 fork；沒有問工作目錄，沒有開 agent

#### Scenario: 正在接續中、勾了 fork
- **WHEN** A 正在接續中，對 A 按 Enter，勾了 fork 再確定
- **THEN** 照常問工作目錄與同步間隔，以 `--fork` 接續

#### Scenario: Esc
- **WHEN** 選 agent 的視窗按 Esc
- **THEN** 不接續，回到清單
