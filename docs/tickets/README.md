# 任務單

2026-10-03 起改用 OpenSpec（opsx）：每件事是一個 change，放在 `openspec/changes/<名稱>/`（proposal、design、specs、tasks），進度看 `tasks.md` 的勾選與 `openspec list`。這張表只當總覽。

最新的在最上面。狀態：待確認 → 進行中 → 待驗收 → 完成。

| 編號 | 標題 | 狀態 | 負責 | 備註 |
|---|---|---|---|---|
| T8 | 測試的小工具在沒有隔離時拒絕執行 → [需求單](T8-test-isolation-guard.md) | **完成（10-04）**：b99ff85；PM 實測沒有隔離時 import 測試檔會被擋下、正式目錄沒被碰 | impl2 | 10-04 00:28 impl1 的診斷讀到正式索引：約 8 筆真實 Session 的標題與內文開頭進了外部模型；假 Session 寫進正式鏡像（PM 已清）。要告訴使用者 |
| T7 | 驗收時記下的四個小問題 → [需求單](T7-acceptance-findings.md) | **完成（10-04）**：F3 078dd07＋f019167＋feb7bed、F1／F2／F4 93bab3b；review T7 確認，Q3～Q5 等 Low 留到之後 | impl2、impl1 | 使用者 10-04 驗收通過 |
| T6 | 預覽只讀、只排需要的那一段（兩個分頁）→ [需求單](T6-lazy-preview.md) | **完成（10-03）**：0ef57f4、9c74220、07ed920；review T6 確認；PM 量到 Agora 分頁每移動一格約 3 秒 → 約 0.1 秒 | impl2 | 使用者 10-03 回報 Agora 分頁上下移動很慢 |
| T5 | 首次設定支援自己的 OAuth client、D5 文件改寫 → [需求單](T5-own-oauth-client.md) | **完成（10-03）**：fb920fb、3d2fb86、598981a；review T5、T5-sec1 確認任何讀不了的 client 檔都不會帶出 secret | impl1 | 10-03 換成自己的 client（issue #11 已關） |
| T4 | 精簡兩個轉接器、放寬行數目標 → change [`slim-adapters`](../../openspec/changes/archive/2026-10-03-slim-adapters/) | **完成（10-03 歸檔）**：−36 行；新的行數目標等使用者決定 | impl1 | 使用者 10-03 決定「先精簡，再放寬一點」 |
| T3 | 寫入先存本機、背景上傳、少連 Drive → change [`local-first-writes`](../../openspec/changes/archive/2026-10-03-local-first-writes/)（已歸檔，規格在 `openspec/specs/local-first-writes`，`batch-commands` 的 import 已更新） | **完成（10-03 歸檔）**，等本人驗收；PM 實跑 `T3-pm-run.md`；整合測試第七輪通過 | impl2、impl1、impl3 | 使用者 10-03 決定：P1 改成先存本機；慢的問題選 B＋C；A 已完成（T5） |
| T2 | 互動模式：多選、全選、進度、中斷、按鍵整理 → change [`tui-batch-actions`](../../openspec/changes/archive/2026-10-03-tui-batch-actions/)（已歸檔，規格在 `openspec/specs/interactive-mode`） | **完成（10-03 歸檔）**，等本人用真實資料試（2-4） | impl1 | review 各輪都沒有 High；PM 試用 `T2-pm-run.md` |
| T1 | 指令模式：pull／push、批次動作的進度與續傳 → change [`command-batch-actions`](../../openspec/changes/archive/2026-10-03-command-batch-actions/)（已歸檔，規格在 `openspec/specs/batch-commands`、`session-sync`） | **完成（10-03 歸檔）**，等本人驗收（1-5）；P1 與行數額度等使用者決定 | impl1、impl2 | review T1-archive、T1-4.2d、final-checks 都沒有 High；PM 實跑 `T1-pm-run.md` |
| T0 | e2e 整合測試改成「continue 寫回原本的 Session」 | 完成 | impl1（3011d15）、impl2（745e5e8） | 兩邊都實跑過整合測試 |

## 2026-10-04 夜間（使用者睡覺時）

使用者合了 PR #10（07005a2）與 MyBrain #156，並交代：「把剩下的小問題修掉 你跑 e2e, 然後直接開 PR merge 進入 main, 然後在我這個 mac 裝新的 agora」「幫我直接清掉不需要的密碼跟設定」。

| 順序 | 項目 | 狀態 |
|---|---|---|
| 1 | 清掉本機用不到的密碼與設定：`client-secret.txt`、`rclone-builtin.conf`、`rclone-own.conf`、`config.json` 的 `previous_folders` | 完成 |
| 2 | T7（F1～F4）修好、review 審 | 完成 |
| 3 | 完整的整合測試（e2e） | 完成：第九輪 31 passed（20 分 27 秒）；單元 560 passed |
| 4 | 開 PR（agora-lite → main），**這一次使用者授權 PM 自己用 merge commit 合** | 完成：[PR #20](https://github.com/FATESAIKOU/MyAiStorage/pull/20) 合進 main（b174024）。注意：PR #10 當時合進去的只是 10-03 04:30 的版本，之後的工作都是這次才進 main |
| 5 | 用合進 main 的版本在使用者的 Mac 裝新的 agora（固定一份，`~/.local/share/agora-stable/<commit>`） | 完成：`~/.local/share/agora-stable/b174024` |
| 6 | （進行中）收掉這個 worktree：PM 用 agora 把**這個 Claude Code session**（`55edd374-…`）匯入正式的 `agora/`，再用 `agora continue session <id> --agent claude --dir ~/testAI/MyAiStorage` 在原路徑接續（使用者 10-04 選的）；新的那個 session 確認 agora-lite 全部合進 main、worktree 乾淨後，移除 worktree、刪掉已合併的 agora-lite 分支（本機與 GitHub）；原路徑的其他檔案不碰 | 等 5 |

只有使用者能做的（已告知）：Google Cloud 刪舊 secret `****Of6a`；Google 帳號移除「rclone」的存取權；撤銷 GitHub fine-grained token 與 ollama-cloud API key。

## 2026-10-04：使用者驗收通過

使用者照 `docs/acceptance.md` 走完第 2～16 節，全部通過（`docs/acceptance-result-2026-10-03.md`）。驗收資料已清（`agora-test` 8 個、opencode 測試對話 13 個依 id 一個一個刪、`/tmp/agora-acc`）。使用者平常用的 agora 換成新的穩定版 **0871e1c**（`~/.local/share/agora-stable/0871e1c`）。剩下：合 PR #10（只能用 merge commit，使用者做）、MyBrain #156、T7。

## 2026-10-03 晚上：全部完成，等使用者驗收

T1～T6 都完成。HEAD `4bf652d`：單元測試 549 passed；整合測試第八輪（07ed920，真 Drive 的 `agora-test`，自己的 OAuth client）**31 passed，18 分 50 秒**（之前 40～50 分鐘，清掉 `agora-test` 裡 45 個舊測試殘骸後變快）。驗收步驟：`docs/acceptance.md`。使用者現在裝的是穩定版 9baa0e5，驗收前換成這個工作目錄的版本（`uv tool install --force --editable .`）。

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
| 8 | MyBrain 補 10-03 下午（換 client、T3） | PM | 完成：[MyBrain #156](https://github.com/FATESAIKOU/MyBrain/pull/156)，等使用者合 |
| — | 等使用者回來：T1 驗收（1-5）、T2 真實資料試用（2-4）、合併 PR | 使用者 | 驗收步驟草稿在 `docs/review/acceptance-draft.md`（review 寫，T2 做完後更新「已知問題」再換掉 docs/acceptance.md） |

隊員的模型：impl1、impl2 用 opencode（Space Bunny Free → Muse Spark 1.3 Free → ollama-cloud DeepSeek V4.1 Flash max，用完或連不上就往下換；impl1 10-03 15:40 因 Space Bunny 連不上換成 Muse Spark）；impl3（`w2:p14`）、impl4（`w2:p1C`）是 agy，**同一個帳號、額度共用**。10-03 15:24 換上 Claude Opus 5.5 做了 R6、V6、K1、G4～G6，16:20 撞到**每週上限**（約 10-10 恢復）；Gemini 約 18:05 恢復。

**等使用者確認的事**（不擋進度，先照 PM 的判斷做）：
- T2 的「勾選但被篩選掉的列不算進動作」（review V4，PM 選了比較安全的做法）。
- T1 的「continue 進行中、原本的 Session 被別台刪掉」：另存成一個新的 Session（review T1-sec3 M1）。
- 6 個孤兒 `opencode run` 行程：使用者 10-03 11:30 停掉了。

- 行數：使用者 10-03 先定 3,800，之後說「基本上都放寬 品質我之後會統一開 issue 處理」——行數只記錄、不擋進度（`docs/design.md` 已改）。T3 收尾時 3,795。

**10-03 使用者已決定**：行數 → T4；P1（救不回來）→ T3「先存本機再上傳」；import／delete 太慢 → T3 的 B＋C，A 開 issue #11 觀察。

**使用者平常用的 agora**：10-03 11:20 起改成穩定版的一般安裝（9baa0e5，T2 歸檔那一版，位置 `~/.local/share/agora-stable/9baa0e5`），不再跟著這個工作目錄變；T3 做完、使用者驗收時再換回（`uv tool install --force --editable .`）。

規則：不合併任何 PR；不碰使用者的真實 session；需要使用者決定的事先停在那一項、寫進這裡，做其他不受影響的項目。

## 已完成（2026-10-02～03，沒有開單的部分）

- 互動模式改成 Textual、10 點試用回饋、全文快取（4dc53d0）、內文搜尋只掃沒快取的（a83795c）
- continue 寫回原本的 Session（109c150）、delete 一次多個（fc78921）、空白鍵不跳行（e1d4878）
- Drive 授權搬到 rclone 內建 client（5a07c16）
