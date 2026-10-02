# 任務單

2026-10-03 起改用 OpenSpec（opsx）：每件事是一個 change，放在 `openspec/changes/<名稱>/`（proposal、design、specs、tasks），進度看 `tasks.md` 的勾選與 `openspec list`。這張表只當總覽。

最新的在最上面。狀態：待確認 → 進行中 → 待驗收 → 完成。

| 編號 | 標題 | 狀態 | 負責 | 備註 |
|---|---|---|---|---|
| T2 | 互動模式：多選、全選、進度、中斷、按鍵整理 → change [`tui-batch-actions`](../../openspec/changes/tui-batch-actions/) | 待歸檔：功能、4.1b、T2-final 的 W1～W3／S3 都修完；PM 在 pane 用假資料試過（`T2-pm-run.md`，全部的按鍵路徑都正常，Q1～Q4 小修正排成 4.2b）；review 確認修正中（→ `docs/review/T2-archive.md`） | impl1 | review T2-sec1～3 的意見都已修 |
| T1 | 指令模式：pull／push、批次動作的進度與續傳 → change [`command-batch-actions`](../../openspec/changes/command-batch-actions/) | 待歸檔：review T1-archive 沒有 High，D3～D7 文字已改（2d37ba2）；PM 用假資料實跑第 1～6 節都過（`T1-pm-run.md`）；P2～P4 小修正（4.2d）impl2 做中；P1 等使用者決定（不擋歸檔） | impl1、impl2 | review Q1～Q12 已併入 |
| T0 | e2e 整合測試改成「continue 寫回原本的 Session」 | 完成 | impl1（3011d15）、impl2（745e5e8） | 兩邊都實跑過整合測試 |

## 2026-10-03 夜間的順序（使用者睡覺時；使用者說回來前不要停）

使用者驗收的兩步（T1 的 1-5、T2 的 2-4）往後挪，等使用者回來、在 3-1 之後做。其他照順序推進，**實作一律交給隊員**，PM 只派工、審、測、更新這張表與各 change 的 tasks.md。

| 順序 | 項目 | 負責 | 狀態 |
|---|---|---|---|
| 1 | T1 `command-batch-actions` 的 tasks 1、2、3 | impl2（1）、impl1（2、3） | 完成（F1 回歸已修、去重複已做） |
| 2 | T1 tasks 4.1 文件、4.2 整合測試實跑、4.3 review 審程式 | 隊員寫文件與跑測試，review 審，PM 看結果 | 完成（review T1-archive、PM 實跑）；剩 4.2d |
| 3 | T1 歸檔（`openspec archive`） | PM | 等 2 |
| 4 | T2 轉成 change（需求已定，見 T2-tui-batch.md），review 看 specs | PM 寫、review 看 | 完成（提前做，和 T1 收尾並行） |
| 5 | T2 實作與測試 | 隊員 | 完成，剩 T2-final 的修正 |
| 6 | T2 PM 用假資料試、review 審、歸檔 | PM、review | PM 試過、review 審完；剩 4.2b 與修正確認 |
| 7 | 3-1 更新 MyBrain PR #151 | PM | 等 6 |
| — | 等使用者回來：T1 驗收（1-5）、T2 真實資料試用（2-4）、合併 PR | 使用者 | 驗收步驟草稿在 `docs/review/acceptance-draft.md`（review 寫，T2 做完後更新「已知問題」再換掉 docs/acceptance.md） |

隊員的模型：impl1、impl2 用 opencode（Space Bunny Free → Muse Spark 1.3 Free → ollama-cloud DeepSeek V4.1 Flash max，用完往下換）；impl3（`w2:p14`）、impl4（`w2:p15`）是 agy，額度 0%：Claude Opus 約 10-03 10:00 恢復、Gemini 約 10-03 18:05 恢復，恢復就換上去接主要的實作。

**等使用者確認的事**（不擋進度，先照 PM 的判斷做）：
- T2 的「勾選但被篩選掉的列不算進動作」（review V4，PM 選了比較安全的做法）。
- **行數額度（要使用者決定）**：04:30 的 HEAD 是 **3,413 行**（T2 的功能都做完了，只剩測試；去重複已做）。T1 剛完成時是 3,121 行，超過 2,900 行的目標 221 行（review `docs/review/T1-size.md`）。去重複可省約 75 行（impl2 在做），T2 還會再加一些，做完預估約 3,050～3,150 行。兩個轉接器還能再省約 85 行（`docs/review/adapters-size.md`，不拿掉 export 的重試是 65 行）。兩項都做完約 2,960 行，仍超過約 60 行，T2 還會再加。選項：(a) 放寬額度到 3,300；(b) 去重複＋精簡轉接器，再放寬一點；(c) 接受現在的大小。PM 建議 (b)。在使用者決定前，只做去重複（不改行為），轉接器的精簡先不動。
- **T1 PM 驗收 P1**：在這台 import 的 Session 本機只有 `session.md`，別台刪掉之後 `push --not-exist-upload` 會因為本機沒有原始檔而拒絕，實際上救不回來（`docs/tickets/T1-pm-run.md`）。review 的估算（`docs/review/P1-options.md`）：這台 import、continue、merge 出來的 Session 都救不回來，只有別台寫、這台 pull 過的救得回來；另外鏡像裡被取代的舊原始檔從來不會被清掉。選項 (a) 寫完後把原始檔留在本機鏡像（約 8～13 行）；(b) 只改拒絕訊息（約 4～6 行）；(c) 維持。PM 建議 (a)，順手清掉鏡像裡被取代的舊原始檔。決定之後開成一個新的 change，不擋 T1 歸檔。
- T1 的「continue 進行中、原本的 Session 被別台刪掉」：agent 結束時不寫回、改存成一個新的 Session，避免丟掉這次的對話（review T1-sec3 M1）。
- **6 個孤兒 `opencode run` 行程**（parent 是 1，10-02 00:17～03:19 開始，提問是整合測試的那幾句，例如 `ses_C2PROJ2DIRTEST01`）：impl2 回報、應是之前幾輪整合測試沒收乾淨的。PM 要停掉時被 auto mode 擋下（停掉行程算「干擾工作負載」），留給使用者決定；隊員也不要清。看的方式：`pgrep -fl "opencode run"`。

規則：不合併任何 PR；不碰使用者的真實 session；需要使用者決定的事先停在那一項、寫進這裡，做其他不受影響的項目。

## 已完成（2026-10-02～03，沒有開單的部分）

- 互動模式改成 Textual、10 點試用回饋、全文快取（4dc53d0）、內文搜尋只掃沒快取的（a83795c）
- continue 寫回原本的 Session（109c150）、delete 一次多個（fc78921）、空白鍵不跳行（e1d4878）
- Drive 授權搬到 rclone 內建 client（5a07c16）
