# 抹除 runbook（6.1，只限本人）

做法：design D2「改寫與抹除」＋技術驗證 1.3。AI 發現不該留存的內容時，
只能改寫遮蔽並提醒（舊版本仍在）；抹除只有你本人能執行。

## 流程（全程在 AdminLock 內）

1. 以管理憑證準備：`~/.config/aistorage/rclone-committer.conf`（專用帳號）、
   pin repo 的管理寫入（你的帳號）、`gh` 已登入。
2. 開鎖：`AdminLock`（pin 的 `.pin/<repo>.maintenance` 旗標＋停用 workflow＋
   等 run 跑完＋預檢遠端 manifest 等於正式 pin）。
   `python -m aistorage.admin lock-status --pin-repo <url>` 可查看旗標。
3. 計畫：`erase` 先 dry-run，只看 id、計數與雜湊。
   記下 `plan-hash`。
4. 執行：`erase --confirm <plan-hash>`（雜湊不符就拒絕）。
   內容順序：clone → filter-repo（整個 Session 刪目錄；segment 改寫 raw）→
   annex key 標 dead 並 forget／drop → 依 file id 永久刪除遠端 bundle、
   manifest、`.bak`、key（刪前 get 確認 parents）→ push → 重建正式 pin →
   `readview_rebuild_epoch` 加 1 完整重發讀取視圖 → 清收件匣與隔離區 →
   `gh run delete`（如有需要）→ `verify_remote` 後置條件。
5. segment 抹除會同步改寫 `snapshots.jsonl`、`handoffs`、`links` 的雜湊
   （`snapshot_remap` 只記雜湊對應）；接續點訊息本身被抹除的交接單標
   `erased`（看得到交接單，看不到內容）。
6. 解鎖（刪旗標、重開 workflow）。已知 clone 與快取另外提醒處理。

## 後置條件（`verify_remote`，只輸出計數）

- 目前版本、git 歷史、bundle、Drive 舊 revision 與垃圾桶、讀取視圖與索引、
  收件匣、隔離資料夾、Actions log 都找不到 canary（測試）或目標內容。
- remote 上沒有不在 manifest 裡的 bundle；`git cat-file` 失敗即判失敗。
- 垃圾桶裡沒有任何一代 uuid 的 bundle；被抹除的 key 取不到。
- 抹除紀錄在真本 `_admin/erasures/<ULID>.json`（誰、何時、為什麼、id、雜湊對應），
  不含任何被抹除的內容。

## 禁止

- Mac opencode 的憑證（worker 的 conf）執行抹除：必須得到 403 或 404。
- 打錯前綴：目標一律以 file id 指定，先 dry-run。
