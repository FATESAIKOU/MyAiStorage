## 1. 實作（在 continue-fork 合進這個分支之後）

- [ ] 1.1 量測（真的 opencode 與 claude，只用 `agora-test`，短間隔）：`opencode export` 的一致性、閒置時兩次 export 是否 byte 相同、和正在跑的 TUI 同時 export 的副作用；Claude 閒置時會不會 append 非訊息的行；典型的 raw 大小。結果寫進 design 的「讀到一半不能寫壞」與 Risks，再定「沒有新內容」的判斷
- [ ] 1.2 `cmd_continue`：`Popen`＋`wait(timeout)` 迴圈；間隔 300 秒，`AGORA_CONTINUE_SYNC_SECONDS`（小數、下限 0.1）；agent 還活著時迴圈不離開；SIGINT 行為不變；一開頭同機正在接續中就拒絕（fork 除外）
- [ ] 1.3 把 `_finish` 拆成「收」與「寫」；`_write_target`（三步，定期 `check_drive=False`、最後一次與補存 `check_drive=True`）；update 照 `same_id` 加 `never_confirmed`；md5 只在 `_save` 成功後記；Claude 的大小／mtime 預檢；`previous_sources` 去重
- [ ] 1.4 最後一次：沒有新內容仍確認目標、被刪就另存；沒寫時仍踢上傳並照規則給 0／3；印出寫到的 id、另存說明、失敗次數；寫回還在跑時先印「正在寫回…」；`unsaved-launches` 規則
- [ ] 1.5 靜音：包裝過的 writer（時間、Session id）寫 `<state>/continue.log`（`LOG_MAX`）；`Drive._run` 與 `opencode export` 加 `stdin=DEVNULL`，rclone 加 `--ask-password=false`；任何例外吞掉、記 log、計數
- [ ] 1.6 pending：目標換到另存的 Session 時 rename pending（鎖跟著 inode）；結束時 unlink 目前的路徑；`recover_pending` 走 `_write_target`
- [ ] 1.7 store：`stage` 在 tmp 資料夾裡先寫好 `.update`；定期寫回的 stage 與 pending 做 fsync
- [ ] 1.8 fakes：`fake_claude.py`／`fake_opencode.py` 加 scripted 模式（說一句 → 等一輪寫回 → 寫半行 → 再說一句 → 離開），「等一輪寫回」輪詢 continue.log 或假 remote，設逾時；新的 `AGORA_TEST_FAULT` 點 `after-periodic-write`
- [ ] 1.9 單元測試：spec 的八個 Scenario；fork 出來的 Session 在離線時定期寫回；`.done-B` 正在比對時又寫一次；間隔下限；fd 1／2 的測試（子程序跑 `python -m agora.cli continue …`，`AGORA_CLAUDE_CMD` 指向假 agent，stdout 與 stderr 導到同一個 O_APPEND 檔，假 agent 在開始與結束各印一個標記，斷言兩個標記之間沒有 agora 的輸出）；從互動模式接續時的 redirect
- [ ] 1.10 整合測試補一個（只用 `agora-test`；短間隔，假 agent＋真的 Drive，或真的 agent）

## 2. 收尾

- [ ] 2.1 `docs/design.md` 5.4（含測試接縫清單、跨機器同時接續是後寫的贏、header 會被蓋回）、5.6（正在接續中的 Session）、README
- [ ] 2.2 review 審
- [ ] 2.3 PM 用假資料實跑（短間隔）
- [ ] 2.4 和 `continue-fork` 一起開 PR（本人 merge）
