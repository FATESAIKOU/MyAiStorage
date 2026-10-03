## 1. 骨架（負責：impl2，先做，做完 impl1 才開始第 4 節）

- [x] 1.1 `src/agora/background.py`：獨立入口（不經過 `cli.main`）；flock `<state>/upload.lock`、處理到 outbox 與刪除佇列都空、放開鎖後再檢查一次；輸出寫 `<state>/upload.log`（超過 1 MB 只留尾巴）。留一個空的 `process_trash_queue(paths, drive)` 給 impl1（design「背景程序」「一把鎖」）（`_take` 拿不到鎖就結束；`run` 放開鎖後再看一次。mutation 誠實回報見下）
- [x] 1.2 啟動背景的 helper：`start_new_session`、`stdin=DEVNULL`、`close_fds=True`、env 原樣、stdout／stderr 到記錄檔；`AGORA_UPLOAD=inline` 時改在前景同步跑（上傳與刪除佇列）；回傳「已啟動／已在前景傳完／失敗」（N1）
- [x] 1.3 測試：真的啟動一次背景，確認沒接到呼叫端的 stdout、沒繼承 pending 鎖、結束後鎖會放開；inline 開關
- [x] 1.4 依 review T3-sec1 的 N1、N3 調整骨架：啟動 helper 回傳三種結果（已啟動／已在前景傳完／失敗），inline 時上傳失敗回 exit 3（既有測試不變）、刪除佇列也在前景處理；指令開頭的 sync **不在前景上傳**，改成 outbox 或刪除佇列不空就啟動背景，提醒「背景上傳中，N 筆」（L5）。單元測試預設 `AGORA_UPLOAD=inline`（`tests/conftest.py`），整合測試維持真的背景程序。共用判斷放在 `store.queued_for_trash(paths)`（N10）
- [x] 1.5 mutation（副本裡做）：N1 的 inline 失敗、N3 的 sync 前景上傳、M1 的不拿鎖、日誌不截斷——四個都抓得到。**兩個抓不到，誠實記下**：(a) `close_fds=False` 時 pending 鎖測試仍然綠——Python 的 fd 本來就是 non-inheritable（PEP 446），子程序拿不到那個鎖，真正守住的是這件事而不是那個 flag（flag 本身由啟動參數的測試守著）；(b) 拿掉「放開鎖後再看一次」沒有測試會紅——迴圈內的 `waiting != seen` 就會抓到晚到的 Session，spec 也說兩條路徑任一條成立即可，那一行是窄窗期的第二道防線

## 2. 本機完整的一份與批次上傳（負責：impl2）

- [ ] 2.1 `remember` 把原始檔也放進鏡像（同名同大小不複製、原子寫入），清掉被取代的 `raw-*`（spec「本機保留完整的一份」）
- [ ] 2.2 假 rclone 補上「本機 → Drive 的 `copy --files-from`」與 `delete --files-from`
- [ ] 2.3 批次上傳：（有更新既有 id 時先列檔）→ 原始檔一次（失敗就整輪結束）→ `session.md` 一次 → 列檔驗 md5 → 刪舊原始檔一次；`.done-` 改名再比對，只刪自己驗過的版本（H1）；前景寫 `.update` 記號（N4）；有記號但 Drive 上沒有的，另存成新的 Session（L7、N5）；`.done-` 的交錯（N6、N7）。`push_outbox` 與 `push session` 都用它；指令開頭的 sync 不在前景上傳、改成啟動背景（N3）；push 等到它的 id 離開 outbox（N2）；`store.queued_for_trash`、push 拒絕佇列裡的、main 開頭的佇列提醒（N10）（spec「一批只連固定幾次 Drive」「只有自己驗過的版本離開 outbox」「背景上傳」）
- [ ] 2.4 sync 跳過刪除佇列裡的 ULID：不放進索引、不標記；`stage`／`outbox_ulids` 處理 `.done-`
- [ ] 2.5 import、continue、merge、edit（與 `recover_pending` 的收尾）存完本機就啟動背景並結束，exit 0；啟動失敗 exit 3。import 開頭的同步改成受節流（batch-commands 的 MODIFIED delta）
- [ ] 2.6 測試：rclone 的呼叫次數與順序；上傳中途 stage 新版本不會遺失（H1）；原始檔那一次失敗時不傳 session.md（M4）；別台在上傳前刪掉的另存成新的 Session、不會被傳回去（L7、N5）；背景失敗之後下一個指令補傳；在這台寫的被刪掉之後救得回來

## 3. 背景刪除（負責：impl1，在第 1 節完成之後）

- [ ] 3.1 `delete` 的前景：既有檢查、`forget_local`、墓碑、拿掉 outbox 裡的那一筆、加進 `<state>/trash-queue/`、啟動背景；雲端沒有的只刪本機、不進佇列（spec「delete 先在本機」）
- [ ] 3.2 填 `process_trash_queue`：每個 ULID 一次 purge，沿用 S1-4／S1-4b
- [ ] 3.3 `pull` 拒絕佇列裡的，說「正在刪除」（push 的拒絕與開頭的提醒在 2.3，N10）
- [ ] 3.4 測試：刪完馬上從搜尋消失；排隊時同步不加回、不標記、pull 拒絕；改完馬上刪不會先傳上去；背景刪除失敗之後下一個指令再試

## 4. 互動模式（負責：impl1）

- [ ] 4.1 結果視窗依 design「exit code」：exit 0 的 import、merge 說「已經存在本機，背景上傳中」，delete 說「已從本機刪除，背景移到 Drive 垃圾桶」；只有 exit 3 說「已存進 outbox，之後的指令會再送」；剛匯入的雲端欄是「未上傳」（spec「互動模式的說明」）
- [ ] 4.2 測試：等待視窗在子程序結束時就結束，不等背景上傳

## 5. 收尾

- [ ] 5.1 `docs/design.md` 4.1、5.6、5.10 與 README 更新
- [ ] 5.2 整合測試（只用 `agora-test`，`wait_uploaded()`）：import 多個 → Drive 上都有；delete → Drive 上沒有；在這台寫的被刪掉之後救回來
- [ ] 5.3 review 審程式；PM 用假資料實跑（量 import 3 個、delete 2 個的前景時間、rclone 次數，以及 `-v --stats` 的 API 請求數）；記下行數
