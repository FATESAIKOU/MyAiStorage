# 任務單

2026-10-03 起改用 OpenSpec（opsx）：每件事是一個 change，放在 `openspec/changes/<名稱>/`（proposal、design、specs、tasks），進度看 `tasks.md` 的勾選與 `openspec list`。這張表只當總覽。

最新的在最上面。狀態：待確認 → 進行中 → 待驗收 → 完成。

| 編號 | 標題 | 狀態 | 負責 | 備註 |
|---|---|---|---|---|
| T2 | 互動模式：多選、全選、進度、中斷、按鍵整理 → change [`tui-batch-actions`](../../openspec/changes/tui-batch-actions/) | 規劃完成，等 T1 | impl3（Opus 恢復後）或 impl2 | review T2.md 的 V1～V8 已併入 |
| T1 | 指令模式：pull／push、批次動作的進度與續傳 → change [`command-batch-actions`](../../openspec/changes/command-batch-actions/) | 進行中 | impl1（tasks 2、3）、impl2（tasks 1） | review Q1～Q12 已併入 |
| T0 | e2e 整合測試改成「continue 寫回原本的 Session」 | 完成 | impl1（3011d15）、impl2（745e5e8） | 兩邊都實跑過整合測試 |

## 2026-10-03 夜間的順序（使用者睡覺時；使用者說回來前不要停）

使用者驗收的兩步（T1 的 1-5、T2 的 2-4）往後挪，等使用者回來、在 3-1 之後做。其他照順序推進，**實作一律交給隊員**，PM 只派工、審、測、更新這張表與各 change 的 tasks.md。

| 順序 | 項目 | 負責 | 狀態 |
|---|---|---|---|
| 1 | T1 `command-batch-actions` 的 tasks 1、2、3 | impl2（1）、impl1（2、3） | 進行中。第 1～3 節與 review 的 S1、S2 修正都完成（tasks.md 打勾）；4.1、4.2 完成；第 3 節的 review 修正（M1～M3）帶進一個回歸 F1（High：離線時 continue 會讓別台刪掉的 Session 復活），impl2 修正中；之後是去重複、PM 驗證 |
| 2 | T1 tasks 4.1 文件、4.2 整合測試實跑、4.3 review 審程式 | 隊員寫文件與跑測試，review 審，PM 看結果 | 等 1 |
| 3 | T1 歸檔（`openspec archive`） | PM | 等 2 |
| 4 | T2 轉成 change（需求已定，見 T2-tui-batch.md），review 看 specs | PM 寫、review 看 | 等 3 |
| 5 | T2 實作與測試 | 隊員 | 等 4 |
| 6 | T2 PM 用假資料試、review 審、歸檔 | PM、review | 等 5 |
| 7 | 3-1 更新 MyBrain PR #151 | PM | 等 6 |
| — | 等使用者回來：T1 驗收（1-5）、T2 真實資料試用（2-4）、合併 PR | 使用者 | |

隊員的模型：impl1、impl2 用 opencode（Space Bunny Free → Muse Spark 1.3 Free → ollama-cloud DeepSeek V4.1 Flash max，用完往下換）；impl3（`w2:p14`）、impl4（`w2:p15`）是 agy，額度 0%：Claude Opus 約 10-03 10:00 恢復、Gemini 約 10-03 18:05 恢復，恢復就換上去接主要的實作。

**等使用者確認的事**（不擋進度，先照 PM 的判斷做）：
- T2 的「勾選但被篩選掉的列不算進動作」（review V4，PM 選了比較安全的做法）。
- **行數額度（要使用者決定）**：03:10 的 HEAD 是 **3,274 行**（含 F1～F7 修正與 T2 進行中的部分；去重複已做）。T1 剛完成時是 3,121 行，超過 2,900 行的目標 221 行（review `docs/review/T1-size.md`）。去重複可省約 75 行（impl2 在做），T2 還會再加一些，做完預估約 3,050～3,150 行。兩個轉接器還能再省約 85 行（`docs/review/adapters-size.md`，不拿掉 export 的重試是 65 行）。兩項都做完約 2,960 行，仍超過約 60 行，T2 還會再加。選項：(a) 放寬額度到 3,300；(b) 去重複＋精簡轉接器，再放寬一點；(c) 接受現在的大小。PM 建議 (b)。在使用者決定前，只做去重複（不改行為），轉接器的精簡先不動。
- T1 的「continue 進行中、原本的 Session 被別台刪掉」：agent 結束時不寫回、改存成一個新的 Session，避免丟掉這次的對話（review T1-sec3 M1）。

規則：不合併任何 PR；不碰使用者的真實 session；需要使用者決定的事先停在那一項、寫進這裡，做其他不受影響的項目。

## 已完成（2026-10-02～03，沒有開單的部分）

- 互動模式改成 Textual、10 點試用回饋、全文快取（4dc53d0）、內文搜尋只掃沒快取的（a83795c）
- continue 寫回原本的 Session（109c150）、delete 一次多個（fc78921）、空白鍵不跳行（e1d4878）
- Drive 授權搬到 rclone 內建 client（5a07c16）
