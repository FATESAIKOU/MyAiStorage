# 非作者審查報告：tui-continue-fork (#36)

- **審查對象**：commit `fbd4598`（作者：agy1）
- **審查者**：agy3 (Gemini 3.8 Flash)
- **審查日期**：2026-10-11
- **對照規格**：
  - `openspec/changes/tui-continue-fork/`（`proposal.md`、`design.md`、`specs/interactive-mode/spec.md`、`tasks.md`）
  - `openspec/specs/session-continue/spec.md`（「continue --fork」、「同一台機器上不能同時接續同一個 Session」）
  - `openspec/specs/interactive-mode/spec.md`（「按鍵」、「接續前確認同步間隔」）

---

## 結論

**原則上可以合併（建議請 agy1 修復 1 項 Medium 測試斷言漏洞後合入）。**

- **業務與互動邏輯完全符合 Spec / Design**：
  - 選 agent 視窗（`Confirm`）預設不勾 fork；空白鍵勾選後切換提示，送出帶 `--fork`。
  - 正在接續中且未勾 fork 時，在問工作目錄前提早擋下並提示可勾 fork；勾了 fork 則順利放行。
  - Esc 正確取消（`Confirm` 回傳 `(None, False)`，`ans is None or ans[0] is None` 判斷無誤）。
  - 雲端沒有的 Session 仍由 `_refuse` 最先擋下，不開任何視窗。
- **後端狀態與 ID 判斷一致**：
  - 與指令模式使用相同的 `store.continuing(self.paths, ulid)`，並透過 `cli._ulid_of(row.key)` 正確剝除 `agora:` 前綴。
- **Title 讀取安全防禦周全**：
  - `self.index.header(ulid)` 正確使用 bare ULID 查詢 SQLite；回傳空或無 title 時依序 fallback 至 `row.cells[1]` 與 `row.key`，完全不會拋出例外。
- **文件齊全可重現**：
  - `README.md`、`docs/design.md` 5.9 與 `docs/acceptance.md` 12.18 / 12.19 均與實作同步且可依步驟驗收。
- **需要修改之處（Medium）**：
  - `test_continue_confirm_tab_and_shift_tab_do_not_switch_app_tab` 的斷言過弱（僅檢查 `assert app.tab == "agora"`）。在突變測試中，若刻意讓 Tab 冒泡並切換底層畫面焦點，該測試依然綠燈通過，未能有效防護 spec「視窗裡 Tab／shift+tab 不動底下的畫面」。需加強底層畫面焦點斷言。

---

## 詳細審查項目（依嚴重等級分列）

### High (0 項)
無阻擋性缺陷。核心狀態管理、防重複接續與 CLI 介面參數均準確無誤。

---

### Medium (1 項)

#### M1. `test_continue_confirm_tab_and_shift_tab_do_not_switch_app_tab` 斷言過弱，無法抓出 Tab 冒泡導致底層畫面焦點變動的突變

- **問題說明**：
  - 在 `tests/unit/test_tui.py` 中，agy1 新增了 `test_continue_confirm_tab_and_shift_tab_do_not_switch_app_tab`，意圖驗證 spec「視窗裡 Tab／shift+tab 不動底下的畫面」。
  - 測試中的斷言為：
    ```python
    await pilot.press("tab")
    assert screen.focused == screen.query_one("#extra")
    assert app.tab == "agora"  # did not switch to import
    ```
  - 但在 `AgoraApp` 中，`Tab` 鍵綁定的是 `action_tab`（在清單與預覽區之間切換焦點 `toggle_focus`），而切換分頁（`app.tab`）是由 `[` 與 `]` 觸發（`action_prev_tab` / `action_next_tab`）。換言之，`Tab` 本來就永遠不會改變 `app.tab`。
  - 若 `Confirm` 視窗內的 Tab 處理未良好隔離，冒泡到 App 並觸發了 `self.toggle_focus()`，使底層畫面焦點從清單（`#table`）切換到預覽區（`#right`），該測試依然會 PASS！
- **實測方法（突變驗證）**：
  - 在獨立副本中修改 `src/agora/tui.py` 的 `action_tab`，刻意讓 Confirm 的 Tab 冒泡觸發底層焦點切換：
    ```python
    if isinstance(self.screen, Confirm):
        self.screen.action_focus_next()
        self.toggle_focus()  # 刻意改壞：切換底層畫面焦點
        return
    ```
  - 執行測試：
    ```bash
    PYTHONPATH=src uv run pytest tests/unit/test_tui.py -k test_continue_confirm_tab_and_shift_tab_do_not_switch_app_tab
    ```
  - **實測結果**：測試 **1 passed**（綠燈，漏抓！）。
- **修復建議**：
  - 參考 `test_ask_text_modal_tab_does_not_affect_underlying_screen`，在關閉 modal 後驗證底層畫面焦點未受影響：
    ```python
    await pilot.press("escape")
    await pilot.pause()
    assert app.focused is app.query_one("#table")
    ```
  - 在副本中補上此斷言後重跑突變，測試精準噴出 `AssertionError: assert PreviewText(id='right') is DataTable(id='table')` 轉紅。

---

### Low (2 項)

#### L1. `docs/acceptance.md` 12.18 可補充切換其他 agent 時的操作說明

- **問題說明**：
  - 驗收步驟 12.18 寫道：「跳出選 agent 視窗，下方有 `[ ] fork：...`；按 `Tab` 移到勾選框，按 `空白` 勾選... 按 `Enter` 確定」。
  - 在 `Confirm` 中，焦點在 `#extra` 勾選框時按 Enter 會觸發 `action_confirm`，以當前高亮的選項（預設第 1 個 `opencode`）送出。
  - 若驗收者想選第 2 個 agent（`claude`），應在按 Tab 移至勾選框前先按 `↓`，或在勾選框按 `shift+tab` 回到選項清單按 `↓`。建議在手動步驟備註簡要提示，避免驗收人員以為 Tab 是在 agent 選項與勾選框之間循環。

#### L2. 函式內 `_ulid_of` 之循環引用防護可補註解

- **問題說明**：
  - `src/agora/tui.py` 在 `action_primary` 內部執行 `from agora.cli import _ulid_of`。
  - 這是為了避免 `agora.cli`（其 `cmd_tui` 內有 `from agora import tui`）與 `agora.tui` 在模組載入時產生循環引用（circular import）。
  - 寫法合理且符合設計，但若加上一行註解（如 `# local import avoids circular import with cli`）能讓後續維護者更清楚原因。

---

## 突變測試（Mutation Testing）紀錄

在獨立副本（`git archive HEAD | tar -x -C /tmp/agora_review_replica`）中執行之驗證結果：

| 突變測試項目 | 修改內容 | 預期反應 | 實測結果 | 評價 |
|---|---|---|---|---|
| **1. 沒勾也加 `--fork`** | `argv_for` 呼叫時強制傳入 `fork=True` | `test_continue_default_unticked_does_not_pass_fork` 應失敗 | **FAIL (紅燈)**：`AssertionError: assert '--fork' not in argv` | 測試有效防護 |
| **2. continuing 檢查移至 `_refuse` 之前** | 將 `_refuse` 移到 Confirm 與 continuing 檢查之後 | 雲端遺失之 Session 測試應報錯 | **FAIL (紅燈)**：`test_continuing_a_session_the_cloud_lost_is_refused_on_screen` 因彈出 Confirm 而掛掉（`Confirm has no attribute lines`） | 測試有效防護 |
| **3. Confirm Tab 冒泡切換底層焦點** | `action_tab` 在處理 Confirm 時額外呼叫 `self.toggle_focus()` | 應抓出底層焦點被改變 | **PASS (綠燈，漏抓！)**：因斷言僅檢查無關的 `app.tab == "agora"` | 發現漏洞，列為 M1 |
| **4. 完整單元測試套件驗證** | 未修改之正常版本執行完整測試 | 836 項測試全數通過 | **836 passed in 417.85s (0:06:57)** | 既有功能無回歸 |

---

## 給 PM 的清單

1. **請 PM 轉交 agy1 強化測試（M1）**：
   - 修改 `tests/unit/test_tui.py` 的 `test_continue_confirm_tab_and_shift_tab_do_not_switch_app_tab`，在 `await pilot.press("escape")` 後加入：
     ```python
     await pilot.pause()
     assert app.focused is app.query_one("#table")
     ```
     確保底層畫面焦點維持在表格上。
2. **手動驗收（Task 2.2）**：
   - PM 可依 `docs/acceptance.md` 12.18 / 12.19 在 pane 中進行實機手動驗收（驗證勾 fork 多出 `-fork` Session，未勾 fork 在正在接續中時提早提示擋下）。
3. **驗收與 PR 結論**：
   - 除 M1 測試斷言待補強外，功能程式碼與文件皆已達標，可於 M1 補正後進行 PR 合併。
