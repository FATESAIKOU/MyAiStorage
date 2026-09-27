# 回滾 runbook（6.2，只限本人，PM 決定 6：管理者直接操作）

回滾＝把某個舊版本恢復成新的快照（`via="rollback"`），在 AdminLock 內執行。
舊版本都能取回（`snapshots.jsonl`＋raw）；閱讀版在下一次發佈時重建。

## 流程

1. 開鎖（見 `erase.md` 前兩步）。
2. 列出快照選擇：`python -m aistorage.admin rollback --repo-dir <clone> --session <id> --list`。
3. 執行：`rollback --session <id> --to <snapshot_sha> --reason "…" --confirm <snapshot_sha>`。
   紀錄寫在真本 `_admin/rollbacks/<ULID>.json`（誰、何時、為什麼、session、新舊 sha）。
4. 解鎖。下一次提交流程會把新快照照常收進並發佈。

## 重要限制（一定要先理解）

**運作中的 Session，下一次同步會以來源端的內容成為新版本**：同步器比對
 sha，來源端比較新就上傳，回滾只維持到下一次同步為止。
要永久移除內容請用抹除（`erase.md`），不要用回滾。
已停止的 Session 沒有這個問題。
