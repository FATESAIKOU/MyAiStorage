## ADDED Requirements

### Requirement: 輸入法
互動模式 MUST 能在篩選框（與預覽區的搜尋框）用中文等輸入法打字、選字。選字時按的 Enter MUST NOT 讓剛選好的字消失。

為了做到這一點，互動模式啟動時 MUST 不啟用 kitty keyboard protocol：載入 Textual 之前，若環境變數 `TEXTUAL_DISABLE_KITTY_KEY` 沒有設定，就設成 `1`；已經設定時（不論值是什麼）MUST 照使用者的設定。指令模式不受影響。

#### Scenario: 預設關掉 kitty 鍵盤協定
- **WHEN** 環境變數裡沒有 `TEXTUAL_DISABLE_KITTY_KEY`，執行 `agora` 進入互動模式
- **THEN** Textual 不啟用 kitty keyboard protocol（`textual.constants.DISABLE_KITTY_KEY` 為真）

#### Scenario: 尊重使用者的設定
- **WHEN** 使用者設了 `TEXTUAL_DISABLE_KITTY_KEY=0` 再執行 `agora`
- **THEN** 照他的設定，啟用 kitty keyboard protocol

#### Scenario: 用輸入法選字
- **WHEN** 在 herdr 的 pane 裡打開 `agora`，按 `/`，用中文輸入法打「表格」並選字
- **THEN** 篩選框裡是「表格」，選字用的 Enter 沒有讓字消失
