**沒有 High。** 3.1 的雲端欄是對的：它讀的是 sync 留在索引裡的標記和 outbox，列表本身不會去問 Drive；「未上傳」也是獨立的一種狀態，不會被當成 ✗。只有幾個 Low（R1～R4）。

# Review：change tui-batch-actions 第 3 節

## 3.1 雲端欄（`416647a`）

2026-10-03，review。對照 `specs/interactive-mode/spec.md` 的「雲端欄與 pull／push 的選項」，以及 `docs/review/T2.md` V6 的「雲端」欄那一列。只提意見，沒有改程式。

在 `git archive 416647a` 取出的副本跑 `test_tui.py`：**48 passed**。沒有跑整合測試，沒有碰 Drive，沒有讀任何真實的 Session，也沒有執行不帶參數的 `agora`。

| spec | 實作 | 測試 | 結果 |
|---|---|---|---|
| Agora 頁有「雲端」欄 | `COLUMNS["agora"]` 加了「雲端」 | 斷言了欄位名稱 | ✅ |
| 雲端有 → ✓，沒有 → ✗，還沒上傳 → 「未上傳」 | `agora_rows`：在 outbox → 未上傳；否則 `index.cloud_has()` → ✓／✗ | `test_the_cloud_column_says_where_each_session_is`（三種都有，而且也查了表格裡實際畫出來的那一格） | ✅ |
| 「未上傳」優先於 ✗ | 先判斷是不是在 outbox | 同上 | ✅（和 session-sync 的「還在 outbox 的不算雲端沒有」一致） |
| 列表不去問 Drive（T6） | 只讀索引和 outbox | — | ✅ |
| Scenario「被另一台機器刪掉，這台同步之後 → ✗」 | 標記由 sync 寫入；TUI 啟動時做一次節流的同步；import、delete 這些子程序也會同步，做完之後 `reload` 會讀到新的標記 | 測試是直接呼叫 `mark_missing`，**沒有**走過「Drive 上刪掉 → 同步 → 重讀」 | ✅\*（R2） |

### 其他

| # | 嚴重度 | 問題 | 建議 |
|---|---|---|---|
| R1 | Low | `agora_rows(index, filters, paths=None)`：沒有傳 `paths` 的時候，`staged` 就是空的集合，outbox 裡的 Session 會**默默地**顯示成 ✓（因為它們沒有標記）。現在唯一的呼叫者（`reload`）有傳，但測試 `tui.agora_rows(index, [])` 沒有傳，以後的呼叫者也很容易漏掉 | 把 `paths` 改成必填 |
| R2 | Low | spec 的 Scenario 是「同步之後」顯示 ✗，但測試是直接寫標記。互動模式裡已經沒有手動同步的鍵了（`r`、`s` 都拿掉了），啟動時的同步又是節流的（5 分鐘內同步過就跳過），所以「別台剛刪掉、這台馬上打開互動模式」的時候，✗ 可能要等到下一次 import／delete 或重新啟動才會出現 | 補一個走完整條路的測試：fake remote 上 `rmtree` → `store.sync` → `reload` → ✗。另外在 design 5.9 寫明「雲端欄反映的是上一次完整同步的結果」（V6 的建議） |
| R3 | Low（給 3.2） | push `--not-exist-upload` 成功之後，標記要等**下一次完整同步**才會清掉（T1-sec3 L5），所以 3.2 做完之後，`P` 加上「雲端沒有的就傳回去」，那一列還是會顯示 ✗，一直到下次同步 | 3.2 的時候一起處理：push 成功的那些 id，直接從 `cloud_missing` 拿掉；或者讓 TUI 在 push 之後做一次不節流的同步 |
| R4 | Low | 每一次 `reload` 都會呼叫 `outbox_ulids()`，而它有一個副作用：會把 `.old-*` 還原回去。有 `.tmp-*` 在的時候它會跳過（stage 正在進行中），所以和同時在跑的子程序不會衝突。只是要知道：互動模式現在也會碰到 outbox 的這個復原邏輯了 | 不用改；在註解裡記一句就好 |
