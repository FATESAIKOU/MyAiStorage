## 1. 實作（負責：agy1，Gemini 3.8 Flash）

- [x] 1.1 選 agent 的視窗加「fork」選項（預設不勾），沿用 `Confirm`（design「視窗沿用 Confirm」，注意 Esc 回傳 `(None, False)`）；`argv_for("continue", …, fork=…)` 勾了就加 `--fork`
- [x] 1.2 正在接續中的提早檢查：沒勾 fork 時用 `store.continuing` 判斷，是的話說明並停下，不問目錄、不開 agent；勾了照常（design「正在接續中的提早檢查」）
- [x] 1.3 測試（`run_test` 按鍵驅動，假的 cli）：預設不勾 → argv 沒有 `--fork`；勾了 → 有 `--fork`；正在接續中＋沒勾 → 有說明、沒問目錄、cli 沒被呼叫；正在接續中＋勾了 → 照常；Esc → 什麼都沒做；視窗裡 Tab／shift+tab 不動底下的畫面；雲端沒有的照舊被擋。每一項在副本裡改壞驗證
- [x] 1.4 文件：`docs/design.md` 5.9、README 的互動模式、`docs/acceptance.md` 互動模式（加 fork 的手動驗收步驟）

## 2. 收尾

- [x] 2.1 非作者審查（agy3，Gemini 3.8 Flash，`docs/review/tui-continue-fork.md`，e788bac）：可以合；M1（視窗裡 Tab 的測試抓不到冒泡）與 L1、L2 由 agy1 在 4afc542 修掉，PM 在副本重做 M1 的改壞，確認測試會紅
- [x] 2.2 PM 用假資料在 pane 裡按一遍（隔離的試用環境、假 rclone、`tests/fakes/fake_claude.py`，fbd4598）：勾 fork 多一個 `<title>-fork`（relation fork、parent 指向原本那個），原本的不動；不勾照常接續原本那個、不多出 Session；Esc 什麼都不做；說明文字隨勾選改變
- [x] 2.3 完整 unit（4afc542）：835 passed、1 failed——失敗的是 10-03 起就有的計時測試 `test_the_bar_really_reaches_n_n_before_the_window_goes`（機器忙時偶爾失敗，10-04 的審查記錄過；這次的改動沒有碰到它），單獨重跑 5 次都通過。整合測試沒有跑：這次只改 TUI，交給指令模式的 argv 和 `continue --fork` 相同，指令模式的 fork 已有整合測試，PM 的 pane 實跑也走完了整條流程
- [x] 2.4 開 PR，由使用者授權後才合（PMO 會先問本人；合併與 issue #36 的收尾不在這個 change 裡追蹤）
