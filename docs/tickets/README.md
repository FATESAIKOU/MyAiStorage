# 任務單

2026-10-03 起改用 OpenSpec（opsx）：每件事是一個 change，放在 `openspec/changes/<名稱>/`（proposal、design、specs、tasks），進度看 `tasks.md` 的勾選與 `openspec list`。這張表只當總覽。

最新的在最上面。狀態：待確認 → 進行中 → 待驗收 → 完成。

| 編號 | 標題 | 狀態 | 負責 | 備註 |
|---|---|---|---|---|
| T6 | 預覽只讀、只排需要的那一段（兩個分頁）→ [需求單](T6-lazy-preview.md) | 待做（T3 歸檔後）；行數額度待使用者決定 | — | 使用者 10-03 回報 Agora 分頁上下移動很慢；原因是每次移動都在主執行緒排整份 session.md（340 KB～3 MB） |
| T5 | 首次設定支援自己的 OAuth client、D5 文件改寫 → [需求單](T5-own-oauth-client.md) | **完成（10-03）**：fb920fb、3d2fb86、598981a；review T5、T5-sec1 確認任何讀不了的 client 檔都不會帶出 secret | impl1 | 10-03 換成自己的 client（issue #11 已關） |
| T4 | 精簡兩個轉接器、放寬行數目標 → change [`slim-adapters`](../../openspec/changes/archive/2026-10-03-slim-adapters/) | **完成（10-03 歸檔）**：−36 行；新的行數目標等使用者決定 | impl1 | 使用者 10-03 決定「先精簡，再放寬一點」 |
| T3 | 寫入先存本機、背景上傳、少連 Drive → change [`local-first-writes`](../../openspec/changes/local-first-writes/) | 收尾中：四節程式完成，review 多輪（T3-sec2～7、T3-final）都沒有 High；剩 T3-final 的 F1～F4、V3、V4、R7，低風險精簡（含 E1），X1／W4／W5，整合測試實跑，PM 假資料實跑 | impl2、impl1 | 使用者 10-03 決定：P1 改成先存本機；慢的問題選 B＋C；A 已完成（T5） |
| T2 | 互動模式：多選、全選、進度、中斷、按鍵整理 → change [`tui-batch-actions`](../../openspec/changes/archive/2026-10-03-tui-batch-actions/)（已歸檔，規格在 `openspec/specs/interactive-mode`） | **完成（10-03 歸檔）**，等本人用真實資料試（2-4） | impl1 | review 各輪都沒有 High；PM 試用 `T2-pm-run.md` |
| T1 | 指令模式：pull／push、批次動作的進度與續傳 → change [`command-batch-actions`](../../openspec/changes/archive/2026-10-03-command-batch-actions/)（已歸檔，規格在 `openspec/specs/batch-commands`、`session-sync`） | **完成（10-03 歸檔）**，等本人驗收（1-5）；P1 與行數額度等使用者決定 | impl1、impl2 | review T1-archive、T1-4.2d、final-checks 都沒有 High；PM 實跑 `T1-pm-run.md` |
| T0 | e2e 整合測試改成「continue 寫回原本的 Session」 | 完成 | impl1（3011d15）、impl2（745e5e8） | 兩邊都實跑過整合測試 |

## 2026-10-03 夜間的順序（使用者睡覺時；使用者說回來前不要停）

使用者驗收的兩步（T1 的 1-5、T2 的 2-4）往後挪，等使用者回來、在 3-1 之後做。其他照順序推進，**實作一律交給隊員**，PM 只派工、審、測、更新這張表與各 change 的 tasks.md。

| 順序 | 項目 | 負責 | 狀態 |
|---|---|---|---|
| 1 | T1 `command-batch-actions` 的 tasks 1、2、3 | impl2（1）、impl1（2、3） | 完成（F1 回歸已修、去重複已做） |
| 2 | T1 tasks 4.1 文件、4.2 整合測試實跑、4.3 review 審程式 | 隊員寫文件與跑測試，review 審，PM 看結果 | 完成（review T1-archive、PM 實跑）；剩 4.2d |
| 3 | T1 歸檔（`openspec archive`） | PM | 完成（10-03） |
| 4 | T2 轉成 change（需求已定，見 T2-tui-batch.md），review 看 specs | PM 寫、review 看 | 完成（提前做，和 T1 收尾並行） |
| 5 | T2 實作與測試 | 隊員 | 完成，剩 T2-final 的修正 |
| 6 | T2 PM 用假資料試、review 審、歸檔 | PM、review | 完成（10-03） |
| 7 | 3-1 更新 MyBrain（#151 已合，改開新的 PR） | PM | 完成：[MyBrain #155](https://github.com/FATESAIKOU/MyBrain/pull/155) 使用者 10-03 合了 |
| — | 等使用者回來：T1 驗收（1-5）、T2 真實資料試用（2-4）、合併 PR | 使用者 | 驗收步驟草稿在 `docs/review/acceptance-draft.md`（review 寫，T2 做完後更新「已知問題」再換掉 docs/acceptance.md） |

隊員的模型：impl1、impl2 用 opencode（Space Bunny Free → Muse Spark 1.3 Free → ollama-cloud DeepSeek V4.1 Flash max，用完或連不上就往下換；impl1 10-03 15:40 因 Space Bunny 連不上換成 Muse Spark）；impl3（`w2:p14`）、impl4（`w2:p1C`）是 agy，**同一個帳號、額度共用**。10-03 15:24 換上 Claude Opus 5.5 做了 R6、V6、K1、G4～G6，16:20 撞到**每週上限**（約 10-10 恢復）；Gemini 約 18:05 恢復。

**等使用者確認的事**（不擋進度，先照 PM 的判斷做）：
- T2 的「勾選但被篩選掉的列不算進動作」（review V4，PM 選了比較安全的做法）。
- T1 的「continue 進行中、原本的 Session 被別台刪掉」：另存成一個新的 Session（review T1-sec3 M1）。
- 6 個孤兒 `opencode run` 行程：使用者 10-03 11:30 停掉了。

- 行數目標：使用者 10-03 決定放寬到 **3,800**（寫進 `docs/design.md`）。T3 做完加上低風險精簡預估約 3,720。

**10-03 使用者已決定**：行數 → T4；P1（救不回來）→ T3「先存本機再上傳」；import／delete 太慢 → T3 的 B＋C，A 開 issue #11 觀察。

**使用者平常用的 agora**：10-03 11:20 起改成穩定版的一般安裝（9baa0e5，T2 歸檔那一版，位置 `~/.local/share/agora-stable/9baa0e5`），不再跟著這個工作目錄變；T3 做完、使用者驗收時再換回（`uv tool install --force --editable .`）。

規則：不合併任何 PR；不碰使用者的真實 session；需要使用者決定的事先停在那一項、寫進這裡，做其他不受影響的項目。

## 已完成（2026-10-02～03，沒有開單的部分）

- 互動模式改成 Textual、10 點試用回饋、全文快取（4dc53d0）、內文搜尋只掃沒快取的（a83795c）
- continue 寫回原本的 Session（109c150）、delete 一次多個（fc78921）、空白鍵不跳行（e1d4878）
- Drive 授權搬到 rclone 內建 client（5a07c16）
