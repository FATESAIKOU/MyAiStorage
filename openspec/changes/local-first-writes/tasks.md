## 1. 本機完整的一份（負責：impl2）

- [ ] 1.1 `remember` 把標頭指到的原始檔也放進鏡像，並清掉同一個 ULID 底下被取代的 `raw-*`（spec「本機保留完整的一份」）
- [ ] 1.2 測試：在這台 import → 別台刪掉 → `push --not-exist-upload` 成功；continue 之後鏡像裡只剩新的原始檔

## 2. 一批的上傳與背景程序（負責：impl2）

- [ ] 2.1 假 rclone 補上「本機 → Drive 的 `copy --files-from`」與 `delete --files-from`
- [ ] 2.2 批次上傳：原始檔一次、`session.md` 一次、列檔驗 md5 一次、刪舊原始檔一次；md5 不符的留在 outbox（spec「一批只連固定幾次 Drive」）。`push_outbox` 與 `push session` 都用它
- [ ] 2.3 內部入口 `_upload`：flock、處理到 outbox 與刪除佇列都空了才結束、輸出寫 `<state>/upload.log`；啟動時 `start_new_session`、`stdin=DEVNULL`、stdout／stderr 不接呼叫端（spec「背景上傳」）
- [ ] 2.4 import、continue、merge 存完本機就啟動背景上傳並結束；import 開頭的同步改成受節流
- [ ] 2.5 測試：rclone 的呼叫次數與順序；真的啟動一次 `_upload`，確認沒有接到呼叫端的 stdout，結束後鎖會放開；背景失敗之後，下一個指令補傳

## 3. 背景刪除（負責：impl1）

- [ ] 3.1 `delete` 在前景做檢查、`forget_local`、寫墓碑、加進 `<state>/trash-queue/`，然後啟動背景程序並結束；`_upload` 處理佇列，沿用 S1-4／S1-4b（spec「delete 先在本機」）
- [ ] 3.2 `sync` 跳過 trash-queue 裡的 ULID：不放進索引、不標記
- [ ] 3.3 測試：刪完馬上從搜尋消失；排隊時同步不會加回、也不會被標記；背景刪除失敗之後，下一個指令再試

## 4. 互動模式（負責：impl1）

- [ ] 4.1 結果視窗：import、merge 說「已經存在本機，背景上傳中」，delete 說「已從本機刪除，背景移到 Drive 垃圾桶」；剛匯入的雲端欄是「未上傳」（spec「互動模式的說明」）
- [ ] 4.2 測試：等待視窗在子程序結束時就結束，不等背景上傳

## 5. 收尾

- [ ] 5.1 `docs/design.md` 4.1、5.6、5.10 與 README 更新
- [ ] 5.2 整合測試（只用 `agora-test`）：import 多個 → 背景上傳完成 → Drive 上都有；delete → 背景完成 → Drive 上沒有；在這台寫的被刪掉之後救回來
- [ ] 5.3 review 審程式；PM 用假資料實跑（量 import 3 個、delete 2 個的前景時間與 rclone 次數）；記下行數
