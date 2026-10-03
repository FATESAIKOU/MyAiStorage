## MODIFIED Requirements

### Requirement: import 一次多個
`agora import session --external-session-id <id>[,<id>…]` MUST 接受多個 agent session id（`--external-session-id` 可以給多次，也可以用逗號分隔）。MUST 先同步一次（受 5 分鐘節流：5 分鐘內同步過就沿用本機索引），再逐一匯入；某一個失敗 MUST 照樣做下一個，最後 exit code 是第一個非零的（全部成功是 0；存進本機、交給背景上傳算成功，見 `local-first-writes`）。只給一個 id 時，行為與 exit code 和給多個時相同。

#### Scenario: 其中一個失敗
- **WHEN** 給三個 id，其中一個不存在
- **THEN** 另外兩個被匯入並印出 agora id，stderr 說明失敗的那個，exit 非零

#### Scenario: 中斷後重跑
- **WHEN** 匯入三個時在第二個被中斷，再執行同一個指令
- **THEN** 第一個因為內容沒變而略過，只做剩下的
