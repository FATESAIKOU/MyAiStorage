# 回滾 runbook（6.2，只限本人，PM 決定 6）

回滾＝把某個舊版本恢復成新的快照（`via="rollback"`），在 AdminLock 內執行。
舊版本都能取回（`snapshots.jsonl`＋raw）；閱讀版在下一次發佈時重建。

## 流程（CLI 會照這個順序走）

1. 開鎖（見 `erase.md` 前兩步）。
2. 列出快照選擇：`rollback --config config/committer.json --session <id> --list`
   （沒有 `--repo-dir` 就在暫存目錄新 clone 一個；git／git-annex 一律在暫存目錄）。
3. 執行：`rollback --session <id> --to <snapshot_sha> --reason "…" --confirm <snapshot_sha>`。
   流程是 AdminLock → clone → 回滾 → commit → **重讀遠端 manifest**
   （`recheck-remote`：回滾不刪遠端檔，所以遠端 manifest 必須與開鎖時完全相同，
   不同就是有人在管理操作期間動了遠端，中止並保留鎖）
   → `git annex copy` ＋ `git push`
   → ls-remote／manifest 驗證 → 重建 pin → `readview_rebuild_epoch` 加 1。
   紀錄寫在真本 `_admin/rollbacks/<ULID>.json`（誰、何時、為什麼、session、
   新舊 sha），不含被還原的內容。
4. with 區塊正常結束才解鎖。中途失敗與收尾見 `erase.md` 的「中途失敗」
   （`swap-finish`、最後才 `unlock --confirm`）。

## 兩件一定要知道的事

1. **運作中的 Session，下一次同步會以來源端的內容成為新版本**：同步器比對
   sha，來源端比較新就上傳，回滾只維持到下一次同步為止。要永久移除內容請用
   抹除（`erase.md`），不要用回滾。已停止的 Session 沒有這個問題。
2. **單調性**：回滾快照的 `snapshot_at` 取執行時鐘，所以回滾之後「現在」一定
   大於所有舊快照。同步器下一份快照若帶的 `snapshot_at` 早於這次回滾，會被判成
   stale 而不採用（該次同步不動作）；下一次帶較新時間的同步才會蓋過回滾。
   也就是說回滾之後，第一次同步可能什麼都沒做——這是設計如此，不是故障。
