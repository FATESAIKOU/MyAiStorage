## Context

現況（`src/agora/cli.py`、`src/agora/store.py`）：

- `store.continuing(paths, ulid)`（`store.py`）只回 bool：開 `pending/<ULID>.json` 試 `flock LOCK_EX|LOCK_NB`，拿得到＝只是留下來的記錄（沒人接續），拿不到＝有人正在接續。
- `cmd_delete` 在收集階段遇到 `continuing` 就 `raise InputError`（exit 1），整批不刪，訊息只有「正在接續，等它結束再刪」。
- `cmd_continue` 的提早檢查（`store.continuing`）與 `_write_pending` 的兩個鎖檢查都走 `_locked(agora_id)`；`main` 先印固定行 `agora-locked <agora id>`，再印中文說明，exit 4（#54 的呼叫方契約）。
- pending 記錄由 `_write_pending` 寫入，欄位有 `agora_id`、`agent`、`agent_session_id`、`dir`、`parent`、`title`、`before_count`、`started_at`、`header_updates`、`add_tags`、`saved_md5`。記錄的鎖交給 agent 子行程持有（`C1`），所以另一個行程刪不掉、也不該動它。
- `cmd_search` 的每一行：`agora:<ULID>  <store.sort_date 的前 10 碼>  <agent 或 relation>  <摘要>`，再加 `(未上傳)`／`(雲端沒有)`；排序由 `Index.search` 決定（主鍵是 `sort_date`，`updated_at` 已在次鍵）。
- TUI 的清單「更新」欄已經用 `agora.updated_at`（`tui.py` 的 `_when`，退回 `sort_date`），指令模式的 search 是唯一還只看對話日期的清單。

約束：鎖的機制、`continue` 的固定行與 exit 4、`--fork` 的行為都不動；測試只用假資料與隔離環境；repo 是 public（文件不寫真 id／本機路徑）。

## Goals / Non-Goals

**Goals：**

- `delete` 批次裡混著被鎖與沒鎖時，沒鎖的照刪、被鎖的留著並說得出是誰在用（agent／目錄／開始時間）。
- 全部被鎖時有明確結果與 exit code，且不與 4（`continue` 被鎖的專用訊號）與 5（agent 死掉）衝突。
- `search` 一眼看得出「最近動過」與「本機正在用」，不再把正在跑的 Session 看成舊的。
- `--force` 是明確、看得懂後果的選擇，不是繞過確認的後門。

**Non-Goals：**

- 不改鎖本身（`continuing` 的判準、`_write_pending` 的兩道鎖、agent 子行程持有鎖）。
- 不改 `continue` 的固定行 `agora-locked <agora id>` 與 exit 4、`--fork` 的行為。
- 不改 `pull --not-exist-delete` 的「正在接續，不能刪」（另一個指令；要同一套說法可以之後再用同一個 helper，這次不動它）。
- 不在 TUI 加 `--force`（TUI 的 `d` 走指令模式，會沿用新的跳過行為；要不要在 TUI 也給 force 是之後的事）。
- 不改 search 的排序（見 Decisions 3）。
- 不印 pending 記錄裡其他欄位（`agent_session_id`、`title`、`header_updates`…）。

## Decisions

### 1. 跳過與 exit code

- **逐個判定**（沿用今天的順序）：`store.continuing` 為真→這次不刪（記下來，附原因）；為假→照現行規則（`deleted` 記錄裡的就略過、找不到的整批拒絕、有子 Session 的規則是另一段，不變）。只是留下來的記錄（鎖沒人拿）不算接續中，照現行規則刪並照舊警告。
- **exit code：有任何一個被鎖而跳過 → 1**；全部被鎖 → 一樣 1，stderr 明確說「沒有刪除任何東西」（兩個以上說「N 個都在接續中：…」，只有一個說「`agora:X` 正在接續中」——單數時不通順，審查 L3 改掉）。這是今天單一個被鎖的碼（`InputError` → 1），所以不算新碼；重跑（等 agent 結束後）就會刪掉，符合既有的「重跑接著做」。**背景上傳器起不來時仍是 3**（`EXIT_IN_OUTBOX` 的既有優先序：那半已經排進佇列但現在沒人做，比「有跳過」更需要說；審查 L1）。
- **不用 4**：4 是 `continue` 的專用訊號——固定行 `agora-locked <agora id>`，呼叫方（sidecar）靠它回 409。delete 的答案是一個批次結果（部分刪了、部分沒有），回 4 會讓呼叫方以為是「continue 被拒絕」。**不用 5**：那是 agent 子行程死掉。需要分辨「因為被鎖而沒刪」的呼叫方讀 stderr 的那一段（或等將來真的有需求再加碼）。
- 混批次（刪了 2 個、跳過 1 個）也是 1——與「有子 Session 被拒絕時回 1、其他照刪」的現行語意一致；0 留給「要刪的都不在了／全刪掉」。
- 邊界：同一個 id 給兩次**這次新加的去重**（`dict.fromkeys`）只印一次（#90 沒要求，是順手的小整理；`headers`／`skipped` 本來就是 dict，重複 id 的可觀察行為幾乎不變，審查 L6）；被跳過的父 Session 不進刪除清單，但**同一次要刪的子 Session 照刪**（子自己的刪除不需要父先不見；子不在這次裡的照樣擋住父，規則不變）。

### 2. 誰在接續：資料與說法

- 新增 `store.continuing_details(paths, ulid) -> dict | None`：只在這把鎖有人拿時回那份 pending 記錄（**讀不到／壞掉回 `{}`**——「有人在接續、細節不明」，和實作與 tasks 2.1 一致；審查 M1。`None` 只有一個意思：沒有人在接續）；`continuing()` 的 bool 契約不變（`cache`／TUI／`sync` 的呼叫端不動）。
- 說法在 cli 裡集中一個 helper（`_who_note`），四處共用：`continue` 的說明（提早檢查與 `_write_pending` 的兩個鎖）、`delete` 的跳過訊息、`delete --force` 的警告、**TUI 清單上 Enter 的接續拒絕**（審查 M2：同一個症狀、同一個說法）。形狀：`正在接續中（agent claude、目錄 ~/proj、從 2026-10-18T05:01:00Z 開始）`；缺哪個欄位就少哪一段（舊記錄沒有 `started_at` 時不編造；壞掉的記錄只剩「正在接續中」）。目錄把 `$HOME` 開頭縮成 `~`（TUI 的 `_home` 本來就這樣，指令模式的 `_short_dir` 這次一起），其他欄位一律不印。
- `continue` 的說明因此變成 `agora:X 正在接續中（…），等它結束再接續（要分岔請用 --fork）`：`agora-locked <agora id>` 固定行、`正在接續中`、`要分岔請用 --fork`、exit 4 都不變（既有測試的斷言仍成立）。
- `_write_pending` 的 `*.json.tmp` 競合窗口：另一個 `_write_pending` 還在飛，記錄不一定存在——拿不到細節時維持原本的說明，不編。
- `delete` 的跳過訊息（每個一行）與 `--force` 的警告（每個刪除之前一行）都用同一個 helper 組出來；只在 stderr。

### 3. search 的格式

- 行：`agora:<ULID>  <對話日期>[（更新：YYYY-MM-DD）]  <agent 或 relation>  <摘要>  <標記>…`。
- **對話日期（現行的 `store.sort_date`）留在原位**：清單的排序主鍵就是它，顯示跟著排序走才不會騙人。**`agora.updated_at` 與它不同時補上 `（更新：YYYY-MM-DD）`**；`updated_at` 沒有時（舊標頭）就不補。這樣「8 月的對話、今天還在動」一眼看得出來，而清單順序不變。
- 為什麼不把主日期換成更新日：那樣列的日期與排序不一致（清單看起來像壞掉），而且要嘛動排序（TUI 的清單順序也跟著變）、要嘛畫面自相矛盾——兩者都超過這張工單的範圍。要改排序是另一個決定（見 Open Questions）。
- 標記順序：`(使用中)`、`(未上傳)`、`(雲端沒有)`；「使用中」＝`store.continuing`（本機的鎖）。只是留下來的記錄不算。日期欄不放空白，維持每行空格分欄（id 是第一欄，管線靠它）。
- 限制寫進 README 與 spec：**只判斷本機的鎖**；別的機器上的接續這台看不到，沒有共用的鎖可以查。清單不假裝看得到。

### 4. `--force`：警告與確認

- 旗標：`agora delete session <id>… [--force] --yes`。`--force` **不取代** `--yes`；沒有 `--yes` 時什麼都不刪（現行預覽照舊、exit 1），只是預覽會把使用中的那幾個標出來、說明要 `--force` 加 `--yes` 才會連它們一起刪。
- 有 `--force --yes` 時：被鎖的 Session 照一般刪除（本機鏡像、索引、outbox、`trash-queue`、`deleted` 記錄），但**每一個刪之前**先在 stderr 印：
  `[agora] --force：agora:X 正在接續中（agent …、目錄 …、從 …開始）；那個 agent 之後的寫回會另存成一個新的 Session，這個 id 不會再有它的更新`
  後果是 `_final_write` → `_write_target` 的既有規則：目標沒有 header 了→`is_new`→`_save_continue` 把對話另存成新 Session（`parents` 指向被刪的這個）。這是已經被「另一台機器刪掉再接續」測試鎖住的路。
- **pending 記錄與它的鎖 MUST NOT 被動**：它屬於另一個行程（刪了鎖會讓那個 run 走錯路）。`--force` 刪的是鏡像／索引／Drive 佇列，記錄留給那個 run 自己收尾。
- 為什麼不是互動確認：指令模式可以接管線（stdin 不一定是終端機），問答會卡住；兩個旗標（`--yes` 表確定、`--force` 表連使用中的一起）就是明確的兩段確認，而且警告一律印在刪除之前、不可能被跳過。`-h` 的文字要寫明後果。

### 5. 測試與改壞驗證

- 單元測試（假資料、`tests/` 的隔離 fixture）：
  - 混批次：被鎖的留著＋沒鎖的刪了＋stderr 有 agent／目錄／開始時間＋跳過摘要＋exit 1；
  - 全部被鎖：什麼都沒刪、訊息明確、exit 1；agent 結束（鎖放掉）後重跑 → 刪掉、exit 0；
  - `--force --yes`：被鎖的也刪掉、警告在刪除之前、pending 記錄還在、之後 `_finish`（用既有的假 agent 流程）另存成新 Session；
  - `--force` 沒 `--yes`：什麼都沒刪、exit 1；
  - `search`：`(使用中)` 只在鎖有人拿時出現、留在原地沒人拿的記錄不算、更新日與對話日不同時出現、`(雲端沒有)` 仍並存；
  - `continue`：固定行＋exit 4＋「要分岔請用 --fork」不變（既有測試），說明多了誰在接續。
- 改壞驗證在 `git archive` 的副本做（假資料、隔離環境）：拿掉跳過邏輯（回到整批拒絕）、拿掉 `--force` 的警告、把使用中判斷弄反（只認留下來的記錄／鎖著的不算）——各要紅，結果記進回報與 commit。
- 不跑整合測試；不碰真 session、真 Drive、`~/.cache/agora`、`~/.local/state/agora`。

## Open Questions

- `search` 的**排序**要不要也改成「最近更新優先」：這次不動（顯示與排序一致）。要改的話是另一個決定（索引排序主鍵、TUI 清單順序一起）。
- `pull --not-exist-delete` 的「正在接續，不能刪」要不要補上同一套誰在接續的說明：這次不動（不是這張工單的驗收）。
- TUI 要不要給 `--force` 或在使用中的列上標示：這次只在指令模式。
