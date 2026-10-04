# Agora lite 效能量測

2026-10-02，impl2。腳本 `spike/perf/measure.py`（主）與 `spike/perf/probe_split.py`（拆解冷啟動）（2026-10-04 清理時已從 repo 移除，要看請用 `git show fd3ba9f:<路徑>` 從歷史取回）。
真的 Drive `agora-test`，`AGORA_CONFIG`／`AGORA_CACHE_DIR`／`AGORA_STATE_DIR` 都在暫存目錄，
`rclone.conf` 是 symlink。對話只有一段自編的 `claude -p`（`--disallowedTools` 放在 prompt 後面）。
每項跑 3 次取中位數。跑完 purge 掉這次建的 23＋5 個 `sessions/<ULID>`、刪掉自己那個 uuid 的
jsonl 與 `session-env/`，`agora-test` 最後是空的。

## 結果（中位數，秒）

| 項目 | 1 個 Session | 21 個 Session |
|---|---|---|
| sync：空鏡像（要下載全部 session.md） | 2.08 | **85.01** |
| sync：無變化（有節流的情況下仍強制跑） | 0.73 | 1.37 |
| import：export（讀 jsonl＋打包） | <0.01 | — |
| import：stage（寫 outbox 本機） | <0.01 | — |
| import：push（上傳＋驗證 md5） | **11.21** | — |
| import：verify（把 raw 抓回來比對） | <0.01 | — |
| search：有 sync | 1.15 | 1.33 |
| search：`--no-sync` | <0.01 | <0.01 |
| （對照）`claude -p` 一輪自編對話 | 6.92 | — |

冷啟動拆解（`probe_split.py`，5 個 Session，只量自己那幾個）：

| 階段 | 秒 |
|---|---|
| `lsjson -R --fast-list --hash`（一次列全部） | 1.12 |
| 每個 session.md 各下一次（`copyto`） | **3.46／個**（5 個共 17.29） |
| 每個 session 寫進索引（FTS5 trigram） | 0.01（5 個共 0.01） |

## 結論

**最慢的是「對 Drive 每個檔案各跑一次 rclone」。** 量到的單次往返約 3.5 秒，
`push_one` 一個 Session 要 3 次（raw、session.md、`lsjson` 驗證）所以 11.2 秒；
冷啟動 20 個 Session 是 1 次列檔 ＋ 20 次下載 ≈ 1.1 ＋ 69 ≈ 70 秒，加上其餘就是 85 秒。
列檔本身只要 1.1 秒，索引寫入幾乎不要時間（0.01 秒）——所以問題不在 Drive 的檔案多，
而在「呼叫次數」。

**使用者每天會感受到的等待**（用他自己的量級估算）：

| 動作 | 現在 | 說明 |
|---|---|---|
| `agora search session '表格'`（日常最常做） | **約 0.7～1.4 秒** | 有 sync／沒變化時；5 分鐘內重複 search 會被節流跳過，實際上常常是 0 秒 |
| `agora import --format opencode …` | 約 11 秒 | 使用者要站在那裡等 |
| `agora continue-session` 結束後存回 | 約 11 秒 | 而且是在關掉 agent 之後再多等 11 秒 |
| 換新機器／刪掉 `~/.cache/agora` 後第一次 sync | 1 個 Session 2 秒、20 個 Session 85 秒 | 一天只會遇到一次，但 85 秒很明顯 |

結論是「日常的 search 已經夠快（1 秒內），真正會卡住的是寫入（11 秒）」。

**值得改的地方（建議，沒動 src）**：

1. **寫入不要擋在前景**（影響最大）。現在 `push_one` 上傳完還要 `list_one` 驗 md5 才回來，
   那次驗證純粹是 paranoia：S1 的保證是「先傳 raw、最後傳 session.md」，
   讀的一方本來就會用 md5 判斷還沒寫完。可以改成上傳完就回頭，
   驗證交給下一次 `sync` 順手做（`list_sessions` 本來就會回 md5），
   11.2 秒 → 大約 7 秒（少一次往返）。
2. **冷啟動改成一次搬，不要 N 次**。`rclone copyto` 逐檔下載可以換成單一
   `rclone copy gdrive:sessions <mirror> --include '*/session.md' --checksum`
   （一個行程、平行傳輸、並用已列到的 md5 決定要不要抓）。
   20 個 Session 預期從 85 秒降到個位數秒；1 個 Session 也從 2 秒降到 1 次往返。
3. **`--fast-list` 已經在了，列檔不是瓶頸**，不用再優化列檔（1.12 秒，且與檔案數幾乎無關）。
4. 如果不做 1，改成把驗證改成「下一次 sync 再確認」也可以；兩者都做最省。

## 跑過的指令（形狀）

```bash
PERF_LEDGER=/tmp/agora-perf-work/ulids.txt python3 spike/perf/measure.py /tmp/agora-perf-work
PERF_LEDGER=/tmp/agora-perf-work/ulids.txt python3 spike/perf/probe_split.py /tmp/agora-perf-work
rclone --config ~/.config/agora/rclone.conf --drive-root-folder-id <agora-test id> \
  lsjson gdrive:sessions --dirs-only        # 只看有沒有自己的 ULID 留著
```

量到的形狀：`results.json`（13 個中位數）、`probe.json`（4 個階段秒數）、
Drive 上 `agora-test/sessions/` 最後 0 筆、自己那個 uuid 的 `projects/<編碼>/` 與
`session-env/<uuid>/` 都已刪。

## PM 的處理（2026-10-02）

- **冷啟動改成一次批次下載（已做）**：sync 把所有要更新的 `session.md` 用一次 `rclone copy --files-from … --no-traverse` 抓下來，失敗才退回逐一下載。真的 Drive 上 3 個 Session 的冷啟動從約 13 秒（依上表推算）變成 5.8 秒；Session 越多差越多。
- **寫入少一次往返（沒做，留給使用者決定）**：建議 1 會拿掉上傳後的 md5 確認，那是 outbox「確認 Drive 有了才移除」的保證（design 4.1），所以不拿掉。另一個做法是把 raw 與 session.md 用一次 `rclone copy --order-by name --transfers 1` 上傳（`raw-…` 排在 `session.md` 前面，順序仍然成立），可以省約 3.5 秒；但要改寫 fake 與幾個依賴 `copyto` 的測試，也要在真的 Drive 上確認順序，所以留到下一輪。
