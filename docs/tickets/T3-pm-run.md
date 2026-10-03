# T3 PM 實跑（tasks 5.3）：用假資料量前景時間與 Drive 呼叫

2026-10-03 13:19～13:25，PM。HEAD `5faac91` 的副本（`/tmp/agora-pm-run`，自己的 venv，不吃工作目錄裡未 commit 的改動）。

環境和 T1、T2 的實跑相同（`T1-pm-run.md`）：全部在 `/tmp/agora-tui`，離線的 rclone 替身、claude fixture 的副本、`claude -p` 的替身。**不設 `AGORA_UPLOAD`**，所以用的是真的背景程序。腳本：PM scratchpad 的 `demo/t3-run.py`。

## 指令模式

| 動作 | 前景時間 | 前景的 rclone | 背景的 rclone | 結果 |
|---|---|---|---|---|
| import 3 個（5 分鐘內同步過） | **0.12 秒** | 0 次 | 3 次：`copy`（原始檔）、`copy`（session.md）、`lsjson`（驗 md5） | Drive 上 3 個都有；本機鏡像每個都有 session.md 和原始檔 |
| delete 2 個 | **0.11 秒** | 2 次 `lsjson` | 2 次 `purge` | 指令結束時清單已經沒有那兩個；背景 0.23 秒後 Drive 上也沒有了 |
| 在這台 import 的，被「另一台」刪掉（直接刪假 Drive 上的資料夾），同步後 `push --not-exist-upload` | 0.22 秒 | — | — | 先被標成雲端沒有；不用先 pull 就傳回去了（P1 解決） |

假 Drive 每次呼叫幾乎不花時間。真的 Drive（自己的 client）每次約 0.6～0.8 秒，import、continue、merge、edit 的這些呼叫都在背景，使用者不用等。delete 前景的 2 次列檔還在前景（spec 沒有要求，記下來）。

## 互動模式（herdr 分頁 `pm-t3`）

| 項目 | 結果 |
|---|---|
| 未匯入頁勾兩列 Enter | ✅ 結果視窗「✓ 匯入 2 個（claude）／已經存在本機，背景上傳中」 |
| Agora 頁的雲端欄 | ✅ 回到清單時已是 ✓（假 Drive 太快，看不到「未上傳」的那一瞬間；單元測試有守） |
| 刪一個 ✓ 的列 | ✅「已從本機刪除，背景移到 Drive 垃圾桶」 |
| 刪一個 ✗ 的列（W5） | ✅「已從本機刪除」，輸出說「雲端沒有，只刪本機這份」，沒有說背景移到垃圾桶 |

## 發現

| # | 嚴重度 | 內容 |
|---|---|---|
| Q1 | Low | delete 的前景還有 2 次列檔：開頭的同步一次，加上 delete 自己確認一次。真的 Drive 上約 1.5 秒，可以接受；要再省，可以讓 delete 沿用同步的列檔結果 |
| Q2 | 觀察 | 救回來的那一個（`--not-exist-upload`），雲端欄要等下一次完整同步才從 ✗ 變回 ✓。這是 T2 就記下的 S4，行為沒變 |
