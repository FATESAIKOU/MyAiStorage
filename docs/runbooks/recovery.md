# 復原 runbook（6.4，只限本人）

Drive 上的 repo 被刪除時，從任一個 git clone 重建。全部在 AdminLock 內執行。

## 前置記錄（遺失時先補齊）

- 每個 repo 的完整 clone URL（`annex::<uuid>?type=rclone&…&rcloneprefix=…`）。
- pin repo 的位置、`config/committer.json` 的備份。
- manifest 的 file id。

> CLI 狀態：`recover` 目前只做**唯讀偵測與列步驟**（`--config config/committer.json
> --new-prefix <id>`）；實際的「刪檔重建＋重推＋重建 pin」是整合測試與
> `swap-finish` 的範圍，沒有管理憑證時會報 `not_wired`。

## 三種模式（`python -m aistorage.admin recover --check …` 先判定）

1. **主 manifest 還在**：不需要復原，先跑健康檢查找真正的原因。
2. **只剩 `.bak`**：比對 `.bak` 內容雜湊等於正式 pin 的 manifest；
   相符就放著，下一輪提交流程走 BAK_RECOVERY（丟棄待定、照常往下）。
   不相符就改走 from-clone。
3. **主 manifest 與 `.bak` 都不在**：只能 from-clone：
   用任一個 clone 推到新前綴（git push＋上傳 bundle／manifest／annex 物件）；
   以觀測到的遠端狀態重建正式 pin（`init-pin --confirm`，只在 Mac；它會照
   6.5 自己上鎖——沒有維護旗標時停用 workflow、等沒有執行中的 run、重讀遠端
   manifest，已經有旗標時就是沿用中止處理中既有的鎖）；
   更新 `config/committer.json`；`readview_rebuild_epoch` 加 1
   完整重建讀取視圖（等全部上傳完才切換 manifest）；
   跑一輪完整提交流程（含清掃）確認正常，把步驟與耗時記在下面。

## 演練紀錄

（在 `TEST_FOLDER_ID` 底下刪掉測試 repo、用 clone 復原、跑一輪完整提交流程後，
把日期、步驟與耗時寫在這裡。）
