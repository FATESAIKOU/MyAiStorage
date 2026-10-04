## Context

互動模式在 `src/agora/tui.py`（Textual 8.2.8）。

現在的預覽區：
- 是 `PreviewArea(VerticalScroll)`，裡面放 `Static(Markdown(...))`；
- T6 讓它只讀、只排檔案的最後一段（`Preview.step`、`read_tail`），捲到頂再補前一段；
- 游標移動有 150 ms 的防抖。

現在的按鍵：
- `AgoraApp.BINDINGS` 一套給全部，`check_action` 依焦點把一部分關掉；
- `Tab` 換頁、`shift+tab` 切焦點；
- `Confirm` 視窗裡的 `Tab` 由 `action_next_tab` 轉交（review S1）。

## Goals / Non-Goals

**Goals：**
- 預覽區有游標與整行高亮，可以搜尋整份 Session；
- 左右兩套按鍵，只有 `Tab`（含 `shift+tab`）與 `q` 共用；
- 保留 T6 的分段讀取：大 Session 也不讀整份、不排整份。

**Non-Goals：**
- 修中文輸入法選字（issue #23 另外處理）；
- 預覽區的選取、複製、編輯；
- 跨 Session 搜尋（清單的 `/` 篩選照舊）。

## Decisions

### 預覽區改用唯讀的 TextArea
- 用 `PreviewText(TextArea)`，設定 `read_only=True`、`soft_wrap=True`、`show_line_numbers=False`、`highlight_cursor_line=True`。
- 游標、整行高亮、捲到游標，都是 TextArea 現成的。
- 顯示原始的 Markdown，加上語法上色（使用者選的）：`language="markdown"` 需要 tree-sitter，所以 `pyproject.toml` 改成 `textual[syntax]`。裝不起來時退回不上色，不要讓預覽區壞掉。
- 替代方案「在排過版的 Markdown 上自己畫游標」：程式多、風險高，使用者選了不要。
- 預覽區上方原本「pinned」的那一行（dir、tags）與「↑ 往上捲載入更早的內容」提示，維持在 TextArea 之外的 `Static`。

### 分段載入接在 TextArea 上
- 補前一段：在 `(0, 0)` 插入新的文字，游標的列數加上插入的行數，所以游標停在原本那一行（spec「不跳」）。
- 觸發條件：游標在第 0 行又往上（`k`／`↑`），或捲到頂（沿用 `watch_scroll_y` 的做法）。
- `g`：一直補到 `Preview.more()` 為假，再到第 0 行。
- `G`：到最後一行。

### 搜尋在檔案上做
- 按 Enter 時讀**整個檔案的位元組**（2 MB 約 2 ms，只讀不排），用 casefold 找出所有符合的位元組位移。
- 沒有檔案的預覽（`Preview(None)`，例如只有最後一則）就在記憶體的文字裡找。
- 位移換成 TextArea 的位置，用已載入那一段的起點 `Preview.at`：
  - 位移大於等於 `at`：在已載入的範圍內，數 `at` 到位移之間的換行數就是第幾行；
  - `at == 0` 時要扣掉 `_no_header` 拿掉的標頭長度；
  - 位移小於 `at`：先 `step()` 到 `at` 小於等於位移，再換算。
- 標亮：目前那一個用 TextArea 的 selection。其他已載入的符合，在 `PreviewText` 覆寫逐行繪製（render line），把那一行裡落在符合範圍的字套上標亮樣式。只處理畫面上的行，不重算整份。
- 計數「第 k 個／共 N 個」顯示在預覽區下方的狀態行。

### 搜尋框
- 預覽區下方的一行 `Input`，`/` 打開並取得焦點。
- Enter 送出並把焦點還給 `PreviewText`；Esc 關掉搜尋框。
- 搜尋框開著時，字母鍵是打字，不是 `n`／`j` 等快捷鍵。
- 和清單的 `/` 篩選是兩個不同的 Input，互不影響。

### 兩套按鍵
- App 層只留共用的 `tab`、`shift+tab`（`toggle_focus`，priority）和 `q`（非 priority，搜尋框、篩選框裡打 q 照常是字）。
- `Confirm` 視窗裡的 Tab 轉交照舊（S1）。
- 清單的鍵放在 `DataTable` 的子類別，或由 `check_action` 只在 `focused` 是清單（或清單的篩選框）時放行。
- 預覽區的鍵（`j`、`k`、`g`、`G`、`/`、`n`、`N`、`escape`）放在 `PreviewText.BINDINGS`。
- 換頁：`[`、`]`，只在清單。
- 按鍵列（`Footer`）依焦點顯示，`refresh_bindings()` 在切焦點時呼叫。

## Risks / Trade-offs

- [`textual[syntax]` 多裝 tree-sitter 與語言套件] → 只在安裝時多幾 MB；裝不起來就不上色。
- [TextArea 每次 `load_text` 會重算語法樹] → 一段約 30 KB，可以接受；補前一段用 insert，不要整份重設。
- [把整個檔案讀進來找位置] → 只讀不排，2 MB 約 2 ms；檔案很大（例如 50 MB）時要量一次，必要時改成分塊讀。
- [使用者習慣 `Tab` 換頁] → 這是使用者自己選的改法；README、`docs/acceptance.md`、按鍵列都要更新。
- [搜尋框打中文] → 受 issue #23 影響，修好前可以貼上。
