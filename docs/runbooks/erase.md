# 抹除 runbook（6.1，只限本人）

做法：design D2「改寫與抹除」＋技術驗證 1.3。AI 發現不該留存的內容時，
**只能提醒你抹除**（期 1 不提供改寫，舊版本一定還在）——想永久移除只有這一條路。

> CLI 狀態：`python -m aistorage.admin erase` 已接上（AdminLock → clone →
> 改寫 → 刪遠端 → push → 驗證 → 重建 pin → 後置條件）。沒有管理憑證時
> 會明確報 `not_wired`，不會假裝成功。

## 流程（全程在 AdminLock 內）

1. 以管理憑證準備：`AISTORAGE_RCLONE_CONF`（專用管理帳號）、pin repo 的管理
   寫入、`gh` 已登入。git／git-annex 一律在暫存目錄（`AISTORAGE_ALLOWED_WORKDIR`
   只有在真的要指向長期存在的 clone 時才設）。
2. 開鎖：`AdminLock`（pin 的 `.pin/<repo>.maintenance` 旗標＋停用 workflow＋
   等 run 跑完＋預檢遠端 manifest 等於正式 pin）。
   `python -m aistorage.admin lock-status --pin-repo <url>` 查看旗標。
   停用的是**設定檔 `committer_workflow` 指的那一個** workflow（預設
   `.github/workflows/committer.yml`）；名字寫錯時 `gh workflow disable`
   會直接失敗，所以不要在指令列上另外指定。
3. 計畫：`erase` 預設 dry-run，只列各類別的檔案數與計畫雜湊，並在**計畫階段**
   就逐一確認每個檔案的 parent 屬於它自己的類別（真本前綴／讀取視圖／
   收件匣／隔離區）——不符就整個拒絕，不會刪到一半才失敗。記下 `--confirm` 值。
4. 執行：`erase --confirm <plan-hash> --canary <canary> --why <原因>`。
   canary 必填：一段只存在於要抹除內容裡的字串，後置條件靠它確認真的找不到了。
5. 執行順序（`admin.remote.swap_remote`）：
   本機改寫（filter-repo／redact／annex drop／gc／remap／抹除紀錄 → **commit**）
   → 依 file id 永久刪除遠端 → **重讀遠端 manifest**（`recheck-remote`：確認
   遠端沒有在管理操作期間被別人動過；有刪檔時主 manifest 已由本輪刪掉，
   「不存在」是預期結果）
   → `git annex copy` ＋ `git push --force`
   → ls-remote 與 manifest 驗證 → 以觀測到的遠端狀態重建正式 pin
   → `readview_rebuild_epoch` 加 1（下一輪完整重建讀取視圖）
   → 後置條件（Drive／bundle／git 歷史／git 物件／annex 物件掃描 canary）。
6. segment 抹除會同步改寫 `snapshots.jsonl`、`handoffs`、`links` 的雜湊；
   接續點訊息本身被抹除的交接單標 `erased`（看得到交接單，看不到內容）。
7. with 區塊正常結束才會解鎖（旗標刪除、workflow 重開）。

## 中途失敗（重要）

`git push --force` 不會刪掉遠端舊的 GITBUNDLE（1.3 實測），所以抹除一定是
「永久刪除＋重推」。若在刪除之後、pin 重建之前失敗：

- **鎖會保留**、workflow 維持停用（H5），提交流程不會回來碰到不一致的遠端；
- 例外訊息會印出「已完成到哪一步」與「下一步指令」：
  - 卡在 delete／push → 保留旗標，在管理 clone 重新
    `git annex copy --to=origin; git push --force origin main git-annex`，
    再 `python -m aistorage.admin swap-finish --repo-dir <clone> --force-push`；
  - 卡在 verify → 先確認遠端 bundle／manifest，再 swap-finish；
  - 卡在 recheck-remote → 遠端在管理操作期間被動過（可能有另一輪提交流程或
    另一個管理操作跑過）：先確認遠端現況與釘選值誰才對，必要時重建基準，
    再 swap-finish；
  - 卡在 pin → 遠端已一致，只是 pin 舊了：
    `python -m aistorage.committer init-pin --confirm`，再 swap-finish。
- 全部處理完才 `python -m aistorage.admin unlock --confirm`（沒有 `--confirm`
  就不會解鎖）。

## 已知副本

抹除會破壞 annex 物件的所有副本（1.3 實測）。若你有用 clone 當非正式備份，
抹除後那些 clone 仍是擴散點——`erase` 會把 `known_clones` 列在計畫裡提醒。
