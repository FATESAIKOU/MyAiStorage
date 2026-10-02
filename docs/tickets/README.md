# 任務單

最新的在最上面。狀態：待確認 → 進行中 → 待驗收 → 完成。

| 編號 | 標題 | 狀態 | 負責 | 備註 |
|---|---|---|---|---|
| T2 | [互動模式：多選、全選、進度、中斷、按鍵整理](T2-tui-batch.md) | 等 T1 | 待派 | |
| T1 | [指令模式：pull／push、批次動作的進度與續傳](T1-command-batch.md) | 已確認，待派工 | impl1、impl2 | 等 T0 做完就派 |
| T0 | e2e 整合測試改成「continue 寫回原本的 Session」 | 進行中 | impl1（opencode）、impl2（claude） | 109c150 的後續 |

## 已完成（2026-10-02～03，沒有開單的部分）

- 互動模式改成 Textual、10 點試用回饋、全文快取（4dc53d0）、內文搜尋只掃沒快取的（a83795c）
- continue 寫回原本的 Session（109c150）、delete 一次多個（fc78921）、空白鍵不跳行（e1d4878）
- Drive 授權搬到 rclone 內建 client（5a07c16）
