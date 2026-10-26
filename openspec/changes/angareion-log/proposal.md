## Why

工單 MyAiStorage#254（c 級，設計先審、本人同意才實作）。現在讀信只有 `angareion read`（`--channel`／`--mentioned`／`--since`）與 `aicomm inbox`，兩者都綁游標：沒有時間上界、沒有寄件人／收件人過濾、沒有前後文、沒有搜尋。要回頭找一則信只能用 `read --since` 反覆撈——`--since` 含邊界、同一秒的訊息要靠 `id` 去重，而多行訊息在視窗邊緣容易只看到第一行，容易漏。本人 2026-10-26 提議（經 PMO 轉達）：讀信介面要讓 AI 一次讀到全部相關信件，並能往前往後追溯到任意位置。PMO 同日在工單補充留言加上 aicomm 層的要求：`aicomm log` 薄包 `angareion log`（同參數、預設 `--to me`）；a2a 的 SKILL.md（#212 那份）補「被叫醒先 inbox；要前後文或補讀用 log，不要用 `read --since` 反覆撈」；`aicomm peek` 只列後續（MyLinuxPool#366），不在本單。

本 change 只做設計（proposal／design／specs delta／tasks），**不寫程式**。

**TS 端與協定的判斷**：不動。`log` 是純讀取的查詢，讀的是現有留言，訊息格式與協定（`angareion: 1`）零變動，MyAiEntry 不需要任何改動（理由見 design D6），所以**不建議升 b**。

## What Changes

- `angareion` 新增**唯讀查詢指令 `log`**（第六個介面）：
  - 過濾：`--channel <ch>`（可重複＝多頻道；不給＝全部）、`--from <AI 名>`（含 `me`，作者提議）、`--to <AI 名|me>`、`--mentioned`；
  - 時間上下界：`--since`／`--until`（都是後端記錄的時刻 `received_at`，皆含邊界）；
  - 內文搜尋：`--grep <字串>`（字面、不分大小寫、只比對內文，作者提議待裁定）；
  - 前後文：`--around <訊息 id> [--before N] [--after N]`（以到達位置數訊息）；
  - 串：`--thread <訊息 id>`（沿 `in_reply_to` 取整串）；
  - `--limit N`（保留最新 N 則）、`--json`。
  - 輸出固定由舊到新、內文完整不截斷（多行完整印）；**永遠不動游標**。
- 為了效能新增 **`log` 專用的本機快取**：每身分每 channel 一檔（state 目錄、0700／0600），增量同步（`since`＋到達位置雙條件）；安靜的 channel 走 ETag 條件式查詢（304 不吃額度，沿用 #96 的可記錄規則）。`read`／`wait` 的游標與 ETag 檔不變；快取壞掉＝整檔視為空重建，永不漏信。
- `aicomm` 新增第三個指令 `log`：薄包 `angareion log`，同參數原樣轉發，**使用者沒給 `--to`／`--mentioned` 時預設補 `--to me`**；不叫醒任何人、不碰 sidecar。對外指令從兩個變三個：send／inbox／log。
- 說明與 docs 更新：`skills/aicomm/SKILL.md`、`docs/aicomm.md` 的補讀說明改指向 `log`（擬改原文見 design D4／D5；#219 的補救句保留不動）、`skills/angareion/SKILL.md`、`docs/angareion.md` 補 `log`。
- 不做：`aicomm peek`（後續）、regex 搜尋、即時跟隨、分頁 offset 等（理由見 design D1「不做」）。

## Capabilities

### Modified Capabilities

- `angareion`：「五個介面」改名為「六個介面」（新增唯讀查詢 `log`）；「輸出與結束碼」補 `log` 的 stdout；「後端可以替換」的後端動作新增一個唯讀列舉；「給 agent 的說明」補 `log` 用法。新增兩條 requirement：唯讀查詢 `log`（介面語意）、`log` 的本機快取（同步、正確性、權限）。
- `comm-skill`：「對外只有 send 與 inbox 兩個指令」改名為「對外只有 send、inbox、log 三個指令」；新增 requirement：`log` 薄包 Angareion 的 log（預設 `--to me`、原樣轉發、不叫醒）。

（無新增 capability。）

## Impact

- 未來實作（本 change 只列，不在本 change 做）：`src/angareion/`（cli／client／backend／github 的查詢與列舉、新的快取模組）、`tests/unit/`；`src/aicomm/cli.py`（`log` 子指令與空信箱提示句）、`skills/aicomm/SKILL.md`、`docs/aicomm.md`、`tests/unit/test_aicomm_*.py`；`skills/angareion/SKILL.md`、`docs/angareion.md`。
- 不動：Angareion 協定（`angareion: 1`）、TS 端 MyAiEntry、sidecar、MyPMO 的程式。
- 效能本質：回頭查詢的成本從「每次全量逐頁列出來現場過濾」（每次重付）變成「每 channel 一次增量同步（安靜時是 304 條件式查詢）＋本機過濾」；代價是快取佔本機磁碟（每身分一份，design D3 有估算）。
- 公開 repo：內容不寫帳號、機器位址、憑證、真的 session id。