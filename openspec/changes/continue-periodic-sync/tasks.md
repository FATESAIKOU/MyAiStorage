## 1. 實作（在 continue-fork 合進這個分支之後）

- [ ] 1.1 `cmd_continue`：`subprocess.run` 換成 `Popen`＋`wait(timeout)` 迴圈；間隔 300 秒，`AGORA_CONTINUE_SYNC_SECONDS` 給測試；SIGINT 行為不變
- [ ] 1.2 把 `_finish` 拆成「收」與「寫」；記上一次寫回的 raw md5，沒有新內容不寫；最後一次和最後的定期寫回相同就不寫；只有整段都沒寫過才記 `unsaved-launches`
- [ ] 1.3 定期寫回靜音：Python 輸出轉到 `<state>/continue.log`；確認外部程式（opencode export、背景上傳）的輸出不會寫到終端機；任何例外吞掉、記 log、下一輪再試
- [ ] 1.4 途中被刪：第一次另存後更新記憶中的 record 與 pending 檔（持有鎖的 fd 改寫），之後都寫到新的 id；最後補印另存的說明
- [ ] 1.5 單元測試（假 agent、很短的間隔、隔離環境）：spec 的五個 Scenario；fork 出來的 Session 也定期寫回；`recover_pending` 在定期寫回過之後寫到正確的 id；定期寫回期間 fd 1／2 沒有輸出
- [ ] 1.6 整合測試補一個（只用 `agora-test`；用很短的間隔跑真的 agent 或假 agent＋真的 Drive）

## 2. 收尾

- [ ] 2.1 `docs/design.md` 5.4（含測試接縫清單）、README
- [ ] 2.2 review 審
- [ ] 2.3 PM 用假資料實跑（短間隔）
- [ ] 2.4 開 PR（本人 merge）
