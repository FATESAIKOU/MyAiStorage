# 任務單

2026-10-03 起改用 OpenSpec（opsx）：每件事是一個 change，放在 `openspec/changes/<名稱>/`（proposal、design、specs、tasks），進度看 `tasks.md` 的勾選與 `openspec list`。這張表只當總覽。

最新的在最上面。狀態：待確認 → 進行中 → 待驗收 → 完成。

| 編號 | 標題 | 狀態 | 負責 | 備註 |
|---|---|---|---|---|
| T2 | [互動模式：多選、全選、進度、中斷、按鍵整理](T2-tui-batch.md) | 等 T1 | 待派 | T1 完成後轉成 change |
| T1 | 指令模式：pull／push、批次動作的進度與續傳 → change [`command-batch-actions`](../../openspec/changes/command-batch-actions/) | 進行中 | impl1（tasks 2、3）、impl2（tasks 1） | review Q1～Q12 已併入 |
| T0 | e2e 整合測試改成「continue 寫回原本的 Session」 | 完成 | impl1（3011d15）、impl2（745e5e8） | 兩邊都實跑過整合測試 |

## 已完成（2026-10-02～03，沒有開單的部分）

- 互動模式改成 Textual、10 點試用回饋、全文快取（4dc53d0）、內文搜尋只掃沒快取的（a83795c）
- continue 寫回原本的 Session（109c150）、delete 一次多個（fc78921）、空白鍵不跳行（e1d4878）
- Drive 授權搬到 rclone 內建 client（5a07c16）
