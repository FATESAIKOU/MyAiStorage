## ADDED Requirements

### Requirement: 接續前確認同步間隔
在 Agora 頁接續（Enter）時，選完 agent 與工作目錄之後，MUST 用輸入框問自動同步的間隔（分鐘，0 ＝ 不自動同步），預設值是 5。

- Enter 確認：值是不小於 0 的數字時，MUST 用這個間隔接續（等同指令模式的 `--sync-period <值>`）；不是的話 MUST 留在輸入框並說明要填數字。
- Esc MUST 取消整個接續，不開 agent。
- 輸入框照「按鍵」的規則：Tab／shift+tab 不動底下的畫面。

#### Scenario: 用預設值
- **WHEN** 在 Agora 頁按 Enter，選了 agent 與工作目錄，輸入框直接按 Enter
- **THEN** 以 `--sync-period 5` 接續

#### Scenario: 改成不自動同步
- **WHEN** 輸入框改成 0 再按 Enter
- **THEN** 以 `--sync-period 0` 接續

#### Scenario: 填錯
- **WHEN** 輸入框填 `abc` 按 Enter
- **THEN** 輸入框還在，說明要填不小於 0 的數字；沒有開 agent

#### Scenario: 取消
- **WHEN** 輸入框按 Esc
- **THEN** 不接續，回到清單
