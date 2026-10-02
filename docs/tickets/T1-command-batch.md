# T1 指令模式：pull／push、批次動作的進度與續傳

- 狀態：**待確認**（K1 等使用者決定）→ 確認後派工
- 來源：使用者 2026-10-03 的回饋與決定（對話中的 AskUserQuestion）；review `docs/review/cache.md`
- 負責：impl1（pull／push 與快取）、impl2（import／delete／merge）；review 看完再合
- 先做 T1（指令模式），T1 驗收後才做 T2（TUI）。TUI 的所有動作都呼叫這裡的指令，所以行為以 T1 為準。

## 需求（使用者決定）

| # | 需求 | 決定 |
|---|---|---|
| R1 | 指令合併 | `cache agora`＋`cache local` → **`agora pull`**；`sync` → **`agora push`**。其他指令不變，共 9 個。 |
| R2 | import 一次多個 | `--external-session-id` 可以給多次，或用逗號分隔。先同步一次，再逐一匯入；某一個失敗照樣做下一個。 |
| R3 | 進度 | import、delete、merge、pull、push 都逐一印出 `k/N`（stderr 或 stdout 都可以，但要一行一個，格式是 `… k/N …`）。 |
| R4 | 中斷後重跑會接著做 | 被 Ctrl-C（之後 TUI 的 Esc 也是送 SIGINT）中斷後，重跑同一個指令要自然接著做，不重做已經完成的部分。 |

R4 各指令的現況與要補的：

| 指令 | 中斷後重跑 | 要做的 |
|---|---|---|
| import | 已經匯入、內容沒變的會略過 | 不用改，加測試鎖住 |
| pull | 已經快取、沒過時的會略過 | 不用改，加測試鎖住 |
| push | 重新覆蓋一次，結果一樣 | 見 K1 |
| delete | 已經刪掉的會報「找不到」，整批失敗 | **改成「已經不在了，略過」**，exit 0 |
| merge | 什麼都沒存，重跑會重寫全部要約 | **每寫好一個來源的要約就先存在本機**（`<state>/merge-sections/`，鍵是 agent＋提示詞版本＋來源 id＋那個來源的內文）；重跑時沿用，只寫還沒寫的 |

## review 的意見（`docs/review/cache.md`）

| # | 嚴重度 | 內容 | 處理 |
|---|---|---|---|
| K1 | **High** | push（原 sync）不先拉，會把**別台機器刪掉的 Session 傳回去而復活**；也會傳上舊的 raw、`*.partial`、`.DS_Store` | **待使用者決定**（建議：只寫回 Drive 上還在的 Session，而且每個只傳 `session.md` 和它標頭指到的那一個 raw） |
| K2 | Medium | 排除清單漏了 `*.partial`、`.*`、`*.tmp` | 照 K1 的建議做就不需要排除清單 |
| K4 | Medium | `local_reading` 的暫存檔名固定，兩個執行緒同時寫同一個 session 會失敗 | 暫存檔名加唯一碼 |
| K5 | Medium | `refresh_agora` 只接 `StoreError`，其他例外會讓整個 pull 停掉 | 每一個 Session 的例外都接住，記為失敗，照樣做下一個 |
| K3 | Medium | Busy 在執行緒裡換掉整個程式的 stdout | 屬於 T2（TUI 改成用子程序跑指令後就沒有這個問題） |
| K6～K11 | Low | 見 review | impl 判斷，順手能改就改 |

## 指令規格（做完之後）

| 指令 | 格式 | 多個 | 進度 | 中斷後重跑 |
|---|---|---|---|---|
| search | `agora search session [--filter …]` | — | — | — |
| show | `agora show session <id> [--raw]` | — | — | — |
| import | `agora import session --external-session-id <id>[,<id>…] [--external-session-id …] --agent <a>` | ✅ | `匯入 k/N` | 略過已匯入 |
| merge | `agora merge session <id> <id>… --agent <a>` | ✅ | `來源 k/N` | 沿用已寫好的要約 |
| continue | `agora continue session <id> --agent <a> [--dir]` | ✗ | — | 下次補存（已有） |
| edit | `agora edit session <id> [--header …]` | ✗ | — | — |
| delete | `agora delete session <id> <id>… --yes` | ✅ | `刪除 k/N` | 略過已刪除 |
| pull | `agora pull` | — | `k/N` | 略過已快取 |
| push | `agora push` | — | `push k/N` | 重新覆蓋（K1 的範圍內） |

## 驗收條件

- 單元測試涵蓋：多個 import（含一個失敗）、delete 重跑、merge 在第二個來源被中斷後重跑只寫第二個、pull／push 的 k/N、K1／K4／K5。
- 整合測試（只用 `agora-test`、自編短對話）：pull、push 各跑一次；import 多個。
- `docs/design.md` 5.10、5.6、5.3、5.2 與 README 同步更新；舊的 `cache`／`sync` 指令拿掉。
- 程式碼行數仍在 2,900 行內。

## 參考

`docs/tickets/T1-pm-draft-reference.txt`：PM 之前自己寫到一半、已經撤回的草稿，**只是摘要格式，不能直接套用**，看看就好。
