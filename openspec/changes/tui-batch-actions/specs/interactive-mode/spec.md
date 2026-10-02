## Purpose

互動模式（只打 `agora`）一次處理多個 Session：勾選、全選、看進度、中斷，並看得出哪些 Session 雲端已經沒有；所有動作都經過指令模式，行為與指令模式相同。

## ADDED Requirements

### Requirement: 動作作用在勾選的列
merge MUST 作用在 Agora 頁勾選的列（至少兩個）。delete、pull、push MUST 作用在 Agora 頁勾選的列，沒有勾選時作用在游標那一列。未匯入頁的 import（Enter）與 pull（`p`）MUST 作用在勾選的列，沒有勾選時作用在游標那一列。continue（Enter）與 edit（`e`）MUST 只作用在游標那一列。

#### Scenario: 刪除勾選的
- **WHEN** 在 Agora 頁勾選三列後按 `d` 並確認
- **THEN** 這三個 Session 被送進一個 delete 指令

#### Scenario: 沒勾選時匯入游標那一列
- **WHEN** 未匯入頁沒有勾選任何列，按 Enter
- **THEN** 只有游標那一列被匯入

### Requirement: 全選切換
`a` MUST 切換目前看得到（篩選之後）的列：沒有全部勾選時全部勾選，已經全部勾選時全部取消。篩選掉的列的勾選狀態 MUST NOT 被改變。空白鍵勾選時游標 MUST NOT 移動。

#### Scenario: 切換兩次
- **WHEN** 清單有兩列、都沒勾，按 `a` 兩次
- **THEN** 第一次後兩列都勾選，第二次後都取消

### Requirement: 進度與中斷
import、merge、delete、pull、push MUST 在等待視窗裡執行，視窗 MUST 有進度條（依指令輸出的 `k/N` 更新）與最新的一行輸出。按 Esc MUST 中斷這個動作（包含它啟動的 agent，例如寫要約的 opencode）。中斷後畫面 MUST 回到清單並說明「重跑同一個動作會接著做」。

#### Scenario: 看得到進度
- **WHEN** 匯入五個 session
- **THEN** 進度條從 1/5 走到 5/5

#### Scenario: 中斷 merge
- **WHEN** merge 寫第二個來源的要約時按 Esc
- **THEN** 動作停止、回到清單；再對同樣的列執行 merge 時，第一個來源的要約被沿用

### Requirement: 雲端欄與 pull／push 的選項
Agora 頁 MUST 有「雲端」欄，雲端有的顯示 ✓、雲端沒有的顯示 ✗、還沒上傳的顯示「未上傳」。pull 的確認視窗 MUST 有預設不勾的「雲端沒有的就刪掉本機的」，勾了等同 `--not-exist-delete`；push 的確認視窗 MUST 有預設不勾的「雲端沒有的就傳回去」，勾了等同 `--not-exist-upload`。

#### Scenario: 看出被別台刪掉的
- **WHEN** 某個 Session 被另一台機器刪掉，這台同步之後
- **THEN** 它的「雲端」欄顯示 ✗

#### Scenario: 對雲端沒有的接續
- **WHEN** 對雲端欄是 ✗ 的列按 Enter 接續
- **THEN** 不開 agent，畫面顯示指令模式的拒絕訊息與兩個選擇

### Requirement: 按鍵
按鍵 MUST 是：↑↓ 移動、Tab 換頁、shift+tab 左右切換焦點、空白勾選、`a` 全選切換、`/` 篩選、ctrl+t 切換標題與內文、Enter（Agora 頁接續、未匯入頁匯入）、`m` 合併、`e` 改標頭、`d` 刪除、`p` pull、`P` push、`q` 離開；等待視窗裡 Esc 中斷。按鍵列 MUST 只顯示目前能用的鍵。

#### Scenario: 未匯入頁的按鍵列
- **WHEN** 在未匯入頁
- **THEN** 按鍵列沒有 `m`、`e`、`d`、`P`
