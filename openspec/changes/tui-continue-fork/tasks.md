## 1. 實作（負責：agy1，Gemini 3.8 Flash）

- [ ] 1.1 選 agent 的視窗加「fork」選項（預設不勾），沿用 `Confirm`（design「視窗沿用 Confirm」，注意 Esc 回傳 `(None, False)`）；`argv_for("continue", …, fork=…)` 勾了就加 `--fork`
- [ ] 1.2 正在接續中的提早檢查：沒勾 fork 時用 `store.continuing` 判斷，是的話說明並停下，不問目錄、不開 agent；勾了照常（design「正在接續中的提早檢查」）
- [ ] 1.3 測試（`run_test` 按鍵驅動，假的 cli）：預設不勾 → argv 沒有 `--fork`；勾了 → 有 `--fork`；正在接續中＋沒勾 → 有說明、沒問目錄、cli 沒被呼叫；正在接續中＋勾了 → 照常；Esc → 什麼都沒做；視窗裡 Tab／shift+tab 不動底下的畫面；雲端沒有的照舊被擋。每一項在副本裡改壞驗證
- [ ] 1.4 文件：`docs/design.md` 5.9、README 的互動模式、`docs/acceptance.md` 互動模式（加 fork 的手動驗收步驟）

## 2. 收尾

- [ ] 2.1 非作者審查（agy3，Gemini 3.8 Flash）：對照 spec／design，改壞驗證
- [ ] 2.2 PM 用假資料在 pane 裡按一遍（試用環境，fake agent），確認勾 fork 會多一個 `-fork` 的 Session、原本的不動
- [ ] 2.3 完整 unit 全綠；整合測試只在需要時跑，只用 `agora-test`（這次只改 TUI，指令模式的 fork 已有整合測試）
- [ ] 2.4 開 PR（本人授權後才合；PMO 會先問本人）
