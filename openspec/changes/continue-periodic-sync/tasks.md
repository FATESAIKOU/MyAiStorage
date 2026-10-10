## 1. 實作（在 continue-fork 合進這個分支之後）

- [x] 1.1 量測（PM 10-10，`tests/integration/test_sync_measure.py`，結果寫在 design「讀到一半不能寫壞」）（真的 opencode 與 claude，只用 `agora-test`，短間隔）：`opencode export` 的一致性、閒置時兩次 export 是否 byte 相同、和正在跑的 TUI 同時 export 的副作用；Claude 閒置時會不會 append 非訊息的行；典型的 raw 大小。結果寫進 design 的「讀到一半不能寫壞」與 Risks，再定「沒有新內容」的判斷
- [ ] 1.2 `cmd_continue`：`Popen`＋`wait(timeout)` 迴圈；`--sync-period <分鐘>`（預設 5、0 ＝ 不自動同步、小數可、負數或非數字在開 agent 前 exit 1、換算後下限 0.1 秒；不另設環境變數）；agent 還活著時迴圈不離開；SIGINT 行為不變；一開頭同機正在接續中就拒絕（fork 除外）
- [ ] 1.3 把 `_finish` 拆成「收」與「寫」；`_write_target`（三步，定期 `check_drive=False`、最後一次與補存 `check_drive=True`）；update 照 `same_id` 加 `never_confirmed`；md5 只在 `_save` 成功後記；Claude 的大小／mtime 預檢；`previous_sources` 去重
- [ ] 1.4 最後一次：沒有新內容仍確認目標、被刪就另存；沒寫時仍踢上傳並照規則給 0／3；印出寫到的 id、另存說明、失敗次數；寫回還在跑時先印「正在寫回…」；`unsaved-launches` 規則
- [ ] 1.5 靜音：包裝過的 writer（時間、Session id）寫 `<state>/continue.log`（`LOG_MAX`）；`Drive._run` 與 `opencode export` 加 `stdin=DEVNULL`，rclone 加 `--ask-password=false`；任何例外吞掉、記 log、計數（**store 部分已完成**：`Drive._run` 已有 `stdin=DEVNULL` 與 `--ask-password=false`，測試在 test_store.py；**`opencode export` 的 stdin 已完成**：`stdin=subprocess.DEVNULL`，測試在 test_agent_opencode.py；writer、吞例外與計數待 cli.py）
- [ ] 1.6 pending：目標換到另存的 Session 時 rename pending（鎖跟著 inode）；結束時 unlink 目前的路徑；`recover_pending` 走 `_write_target`
- [ ] 1.7 store：`stage` 在 tmp 資料夾裡先寫好 `.update`；定期寫回的 stage 與 pending 做 fsync（**stage 部分已完成**：`stage(..., update=, kind=)` 在 tmp 裡先寫好 `.update` 再整個換進去，stage 寫的檔案與資料夾都 fsync；`mark_update` 保留給事後才決定的呼叫端；pending 的寫入在 cli.py，待 cli 那邊；測試在 test_store.py）
- [x] 1.8 fakes：`fake_claude.py`／`fake_opencode.py` 加 scripted 模式（說一句 → 等一輪寫回 → 寫半行 → 再說一句 → 離開），「等一輪寫回」輪詢 continue.log 或假 remote，設逾時；新的 `AGORA_TEST_FAULT` 點 `after-periodic-write`（scripted 模式已完成：`FAKE_AGENT_SCRIPT=append:…,wait-sync,partial,…,exit`，`wait-sync` 輪詢 `FAKE_SYNC_SIGNAL` 指向的檔案、逾時 `FAKE_SYNC_TIMEOUT`（預設 10 秒）就 exit 2，開始／結束各印 `FAKE-AGENT-START`／`FAKE-AGENT-END`；`partial` 在 claude 是寫到一半的行、在 opencode 是還在跑的 tool part；測試在 tests/unit/test_fake_script.py。**`AGORA_TEST_FAULT` 的 `after-periodic-write` 沒做**：那是 store／cli 的接縫，不在這次改的檔案裡）
- [ ] 1.9 單元測試：spec 的八個 Scenario；fork 出來的 Session 在離線時定期寫回；`.done-B` 正在比對時又寫一次；間隔下限；fd 1／2 的測試（子程序跑 `python -m agora.cli continue …`，`AGORA_CLAUDE_CMD` 指向假 agent，stdout 與 stderr 導到同一個 O_APPEND 檔，假 agent 在開始與結束各印一個標記，斷言兩個標記之間沒有 agora 的輸出）；從互動模式接續時的 redirect
- [x] 1.11 互動模式：Agora 頁接續時，選完 agent 與工作目錄後用 `AskText` 問同步間隔（預設 5），驗證後把 `--sync-period` 加進 `argv_for("continue", …)`；填錯留在輸入框、Esc 取消；測試照 interactive-mode delta 的四個 Scenario（`run_test` 按鍵驅動）（已完成：`AskText` 加了選用的 `check`，填錯時視窗留著、顯示 `⚠ 請填不小於 0 的數字…`；沒有 `check` 的呼叫端（工作目錄、client 設定檔）多一個 `Static` 都沒有，行為照舊。`argv_for(..., sync_period=None)` 不給就不加旗標，給 0 也照加。測試在 tests/unit/test_tui.py，13 個 mutation 都抓得到。指令模式那側是 impl1 的）
- [ ] 1.10 整合測試補一個（只用 `agora-test`；短間隔，假 agent＋真的 Drive，或真的 agent）

## 2. 收尾

- [ ] 2.1 `docs/design.md` 5.4（含測試接縫清單、跨機器同時接續是後寫的贏、header 會被蓋回）、5.6（正在接續中的 Session）、README
- [ ] 2.2 review 審
- [ ] 2.3 PM 用假資料實跑（短間隔）
- [ ] 2.4 和 `continue-fork` 一起開 PR（本人 merge）
