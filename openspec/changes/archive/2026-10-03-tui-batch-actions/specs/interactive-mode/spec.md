## Purpose

互動模式（只打 `agora`）一次處理多個 Session：勾選、全選、看進度、中斷，並看得出哪些 Session 雲端已經沒有；所有動作都經過指令模式，行為與指令模式相同。

## ADDED Requirements

### Requirement: 動作作用在勾選的列
merge MUST 作用在 Agora 頁勾選的列（至少兩個）。delete、pull、push MUST 作用在 Agora 頁勾選的列，沒有勾選時作用在游標那一列。未匯入頁的 import（Enter）與 pull（`p`）MUST 作用在勾選的列，沒有勾選時作用在游標那一列。continue（Enter）與 edit（`e`）MUST 只作用在游標那一列。「勾選的列」MUST 只算目前看得到（篩選之後）的列；被篩選掉但仍勾選著的列 MUST NOT 被送進動作，標題列 MUST 顯示「另有 N 個勾選被篩選掉」（review V4）。merge 的來源順序 MUST 是畫面上由上到下的順序。動作成功後勾選 MUST 清掉；失敗或中斷時 MUST 保留，方便重跑。

#### Scenario: 刪除勾選的
- **WHEN** 在 Agora 頁勾選三列後按 `d` 並確認
- **THEN** 這三個 Session 被送進一個 delete 指令

#### Scenario: 篩選掉的勾選不算
- **WHEN** 勾選三列後用篩選把其中一列藏起來，按 `d`
- **THEN** 只有看得到的兩列被刪，標題列提示有一個勾選被篩選掉

#### Scenario: 沒勾選時匯入游標那一列
- **WHEN** 未匯入頁沒有勾選任何列，按 Enter
- **THEN** 只有游標那一列被匯入

### Requirement: 全選切換
`a` MUST 切換目前看得到（篩選之後）的列：沒有全部勾選時全部勾選，已經全部勾選時全部取消。篩選掉的列的勾選狀態 MUST NOT 被改變。空白鍵勾選時游標 MUST NOT 移動。

#### Scenario: 切換兩次
- **WHEN** 清單有兩列、都沒勾，按 `a` 兩次
- **THEN** 第一次後兩列都勾選，第二次後都取消

### Requirement: 進度與中斷
import、merge、delete、pull、push MUST 在等待視窗裡執行，視窗 MUST 有進度條（依指令輸出的 `k/N` 更新）與最新的一行輸出。按 Esc MUST 中斷這個動作（包含它啟動的 agent，例如寫要約的 opencode）：先送 SIGINT，5 秒內沒停就送 SIGTERM，再 5 秒就 SIGKILL，都對整個 process group（review V2）。子程序 MUST NOT 讀得到使用者的鍵盤輸入（review V1）。進度 MUST 只從以 `[agora]` 開頭、含 `k/N` 的行讀（review V8）。結果視窗 MUST 依 exit code 說明：成功、部分失敗、已存進 outbox 等下次上傳、已中斷（review V6）。中斷後，結果視窗 MUST 說明「重跑同一個動作會接著做」，關掉之後回到清單（review Q4）。

#### Scenario: 看得到進度
- **WHEN** 匯入五個 session
- **THEN** 進度條顯示**已經做完**的個數：從 0/5 起，收到第 5 個 `5/5` 時是 4/5；指令正常結束（exit 0）時進度條才到 5/5，結果視窗說「完成」

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
