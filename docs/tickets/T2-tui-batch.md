# T2 互動模式：多選、全選、進度、中斷、按鍵整理

- 狀態：**已轉成 OpenSpec change `tui-batch-actions`**（2026-10-03）。之後以 `openspec/changes/tui-batch-actions/` 為準，這份只留作歷史。
- 來源：使用者 2026-10-03 的回饋與決定
- 負責：T1 驗收後再派（預定 impl2，熟 Textual 版的部分由 PM 審）

## 需求（使用者決定）

| # | 需求 | 決定 |
|---|---|---|
| U1 | 動作都可以多選 | merge、delete（Agora 頁）、import（未匯入頁）都作用在勾選的列；沒有勾選時，delete、import 作用在游標那一列。continue、edit 一次一個。 |
| U2 | 全選／全不選 | **`a` 一個鍵切換**：目前看得到的列（篩選後）沒有全勾就全勾，已經全勾就全部取消。 |
| U3 | 按鍵整理 | 見下表；兩頁同一個鍵做同一類的事。 |
| U4 | 進度 | 等待視窗有進度條，從指令輸出的 `k/N` 讀；下面一行顯示最新的輸出。 |
| U5 | 中斷 | 等待視窗按 **Esc** 中斷：對指令的子程序（整個 process group）送 SIGINT。之後重跑同一個動作會接著做（T1 的 R4），所以不需要特別處理。 |
| U6 | 空白鍵勾選後游標不動 | 已做（e1d4878） |
| U7 | 一覽要顯示雲端有沒有 | Agora 頁加一欄「雲端」：有 ✓、沒有 ✗（T1 R6 的標記）。`p`／`P` 作用在勾選的列，否則游標那一列；pull 與 push 的確認視窗各有一個勾選項：「雲端沒有的就刪掉本機的」（pull）、「雲端沒有的就傳回去」（push），預設不勾。 |

## 按鍵（使用者確認）

| 類別 | 鍵 | Agora 頁 | 未匯入頁 |
|---|---|---|---|
| 移動 | ↑↓ ／ Tab ／ shift+tab | 移動／換頁／左右切換焦點 | 同左 |
| 選取 | 空白 ／ `a` | 勾選／切換全選與全不選 | 同左 |
| 找 | `/` ／ ctrl+t | 篩選／切換標題與內文 | 同左 |
| 主要動作 | Enter | 接續（一個） | 匯入（勾選的，否則游標那一個） |
| 其他動作 | `m` ／ `e` ／ `d` | 合併／改標頭／刪除 | — |
| 本機與 Drive | `p` ／ `P` | pull／push 勾選的（push 要確認） | `p`：把勾選的 agent session 全文寫進快取 |
| 執行中 | Esc | 中斷 | 同左 |
| 離開 | `q` | 離開 | 同左 |

## 做法

- 不需要整個終端機的動作（import、merge、delete、pull、push）一律**用子程序跑同一個 `agora` 指令**（`python -m agora.cli …`，自己一個 process group），從它的輸出讀進度；不要再在執行緒裡 `redirect_stdout`（review K3）。
- continue、edit 照舊用 `App.suspend()` 把終端機交出去。
- import 多選時，同一個 agent 的列合成一個指令（`--external-session-id a,b,c`）。

## 驗收條件

- 用 Textual 的 `run_test` 測：`a` 切換全選、delete／import 用勾選的列、Esc 會送中斷、進度條跟著 `k/N` 動。
- PM 在 pane 用假資料操作一遍；最後由使用者用真實資料試。
