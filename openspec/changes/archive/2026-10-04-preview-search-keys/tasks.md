## 1. 兩套按鍵（負責：impl3）

- [x] 1.1 `side()`（依焦點 widget 的 id：`#table`、`#filter` 是清單；`#right` 與 `#search` 是預覽）；`check_action` 改用它；`KEYS` 拆成清單、預覽兩張表，`paint_keys()` 依（分頁, 邊）查表；`on_descendant_focus` 重畫按鍵列（design「先有 side()」）
- [x] 1.2 App 層只留 `tab`、`shift+tab`（各自一個 action：Confirm 裡分別往下／往上，其他視窗什麼都不做，沒有視窗時切換兩邊；從篩選框切出去等同按 Enter，篩選框收起來）與 `q`；換頁改成 `[`、`]`（不設 priority）；ctrl+t 與其他清單的鍵只在清單；Enter 的 `primary` 維持「焦點是 `#table`」才放行（review R1）；`on_key` 的 Esc 只在 `#filterbar` 開著且 `side() == "list"` 時處理（review R6）（spec「按鍵」，design「按鍵與視窗」）
- [x] 1.3 測試（`run_test` 按鍵驅動）：Tab／shift+tab 切焦點、`]` 只在清單換頁、預覽區按 `d`／`m`／`a`／ctrl+t 沒有作用、按鍵列兩邊不同且滑鼠點也會換、Confirm 裡 Tab 往下 shift+tab 往上、AskText 裡 Tab 不動底下的畫面、從篩選框按 Tab 篩選照樣生效且篩選框收起來、在篩選框按 Enter 不會開接續或匯入。`test_tui.py` 裡 17 處用 `"tab"` 換頁的要改成 `]`

## 2. 預覽區的游標（負責：impl3，第 1 節 commit 之後）

- [x] 2.1 `pyproject.toml` 改用 `textual[syntax]`（更新 `uv.lock`，記在 commit）；預覽區改成 `PreviewText(TextArea)`，`id="right"`、唯讀、折行、焦點在時才整行高亮；`language="markdown"`，兩種退回不上色的情況（沒有 tree-sitter；有 tree-sitter 但沒有 markdown 語法時 try/except）；`## user`／`## assistant` 用 bold `#87afff`／bold `#d787ff`（`get_line()`；PM 10-04 決定、本人經 PMO 確認）（spec「預覽區的游標與捲動」，design「改用唯讀的 TextArea」）
- [x] 2.2 分段：補一段時記下 `scroll_y` 與折行後的高度、插入 `f"{step}\n"`、還原捲動、`history.clear()`；`k`／`↑`／PgUp 在第 0 行與捲到頂都會補；`g` 先連續 `step()` 再一次放進去；`Preview.text` 改用 list；`_no_header` 只在 `start == 0`；記下大小與 mtime；提示改成提到 `k`／`g`；換 Session 時游標在最後一行（design「分段載入」）
- [x] 2.3 測試：`j`／`k`／`↑`／`↓` 移動（短行）、`g` 全部載入且提示消失、`G`、補前一段不跳（review R4：捲到頂時原本最上面那一行的螢幕位置不變；按 `k` 時原本那一行剛好往下一列；驗的是那一行的螢幕位置，不只驗 `cursor_location`）、捲到頂與 PgUp 也會補、兩種退回不上色、焦點離開時不高亮；T6 原本的 Y1、Y2 驗的事要保留；150 ms 防抖與兩個分頁照舊

- [x] 2.4 review「第 2 節程式審查」的修正（第 3 節之前做）：S1 以 code block 結尾會掛掉（`_build_highlight_map` 跳過超出範圍的行，補測試並在 `run_test` 裡實際選到那一列）；S2 換 Session 只讀一段（`_suppress_scroll_load`，補測試）；S3 `k`／PgUp 補完後游標在折行後的上一列（測試用 `wrapped_document` 算位置、原本那一行 `== 1`）；W10、W11、提示文字各補斷言；`_build_highlight_map` 的覆寫要有測試守著；benchmark 門檻放寬到 5 秒；顏色改成 user `#87afff`、assistant `#d787ff`；Low：拿掉 `PreviewArea` 別名、BINDINGS 不展開、`step()` 不用空格當旗標

## 3. 預覽區搜尋（負責：impl3，第 2 節之後）

- [x] 3.1 搜尋框 `#search`：`/` 打開、Enter 送出並回到預覽區、Esc 關掉；同一個比對函式，在檔案上算 N 與 B（從檔頭結束處開始），已載入的在 TextArea 文字裡找；跳到第 k 個時 k ≤ B 就先一次載入；Enter 時往下最近、往下沒有就往上最近（review R2）；`n`／`N` 從游標往下／往上並繞回（spec「預覽區搜尋」，design「搜尋：計數」）
- [x] 3.2 標亮：目前那一個用 selection，其他用 `get_line()` 加前景色與粗體或底線，符合改變時清 `_line_cache` 再 `refresh()`；計數「第 k 個／共 N 個」；找不到；Esc 先關搜尋框、再清標亮；狀態的生命週期（以列的 key 判斷同一個 Session；換 Session 清掉；有搜尋時換成整份提示「已換成整份對話」、重讀提示「內容已更新」，都重搜；只有最後一則時標明；讀取失敗不卡在載入中；從搜尋框按 Tab 丟掉沒送出的字）（design「搜尋框與狀態」）
- [x] 3.3 測試：不變量（每個 `at`：B＋已載入＝N；內文涵蓋切在 `\n\n`、`\n## `、中文、`ß`、檔頭裡也有）、找到並跳過去、第一次搜尋找離尾端最近的（不載入前面）、`n`／`N` 繞回、按 Enter 跳到還沒載入的地方（自編的數 MB 內文）、檔頭不算、Esc 兩段、換 Session 清掉、最後一則換成整份後重搜、重讀後提示「內容已更新」、搜尋框裡打 `n`／`j`／`q` 是字

## 4. 收尾

- [x] 4.1 `docs/design.md` 5.9（按鍵表、預覽區）、README 的按鍵、`docs/acceptance.md` 互動模式那一段（負責：impl4，等第 1～3 節完成）
- [x] 4.2 review 審程式（第 1～3 節、2.4、T1～T3 都審過可以合，見 docs/review/preview-search-keys.md）；PM 用假資料在 pane 裡按一遍（10-04：g 1.40 秒、Enter 跳到最前面 1.33 秒、n／N 約 0.2 秒；Tab、shift+tab、[、]、G、N 在 kitty 關掉的預設狀態都正常；標題顏色正確），量 3 MB 的 Session 檔按 `g` 與跳到第一個符合（目標 1.5 秒；閱讀版 3 MB 約 1.5 秒可接受）；在 kitty 協定關掉的預設狀態（#23 合進來之後）實際按 Tab、shift+tab、`[`、`]`、`G`、`N`（review R7，單元測試測不到終端機的編碼）
- [x] 4.3 整合測試 31 passed（7bcf19b，29 分 43 秒，只用 `agora-test`；跑完真實的 agora 目錄沒有任何變動）；和 `ime-kitty-keyboard`、`guard-under-pytest` 一起開 PR，由使用者合併（合併與 issue #22 的收尾不在這個 change 裡追蹤）
