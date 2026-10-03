# T1 PM 驗收（tasks 4.3）：用假資料實跑

2026-10-03 07:50～08:10，PM。HEAD `fb34c16`。

環境：全部在 `/tmp/agora-tui`，離線——rclone 是 `tests/fakes/fake_rclone.py`（遠端是 `/tmp/agora-tui/remote/agora-test`），claude 的 session 是 `tests/fixtures/claude/cl-basic.jsonl` 改了 id 與第一句的六份副本，`claude -p`（寫要約）是睡 4 秒後回固定 JSON 的替身，opencode 指到不存在的路徑。沒有碰 Drive、沒有讀任何真實的 Session、沒有執行不帶參數的 `agora`。腳本在 PM 的 scratchpad（`demo/setup.py`、`demo/t1-run.sh`），照 `docs/review/acceptance-draft.md` 第一段改成 claude 的版本。

## 結果

| 節 | 結果 |
|---|---|
| 1 import 一次多個 | ✅ 三行 id、`匯入 k/3`；重跑同樣的 id；一個打錯 → 其他照做，exit 2；進度只在 stderr |
| 2 pull／push | ✅ 不給 id → exit 1；拿下原始檔；`claude:` 寫進閱讀版快取；沒前綴的 uuid → 拒絕；edit 後 push 覆蓋 Drive；push 不吃 agent id |
| 3 continue 寫回原本的 | ✅ 印出同一個 id，show 看得到新的那句 |
| 4 delete 多個與重跑 | ✅ 沒 `--yes` → 列出、exit 1；`刪除 k/2`；重跑「已經不在了，略過 2 個」exit 0；打錯的 id → 找不到 exit 1 |
| 5 merge 中斷後沿用 | ✅ 在第 2 個來源寫要約時送 SIGINT → exit 130；重跑時第 1 個「沿用上次寫好的」，只為第 2 個叫一次 claude；標頭 `status: draft` |
| 6 雲端沒有 | ✅ 標記、`--filter cloud=no`、continue 拒絕（exit 1、兩個選擇）、merge 拒絕、`pull --not-exist-delete` 刪掉本機副本 |

（第 5 節第一次沒中斷成功，是 PM 的腳本問題：非互動 bash 的背景工作會忽略 SIGINT。改用 Python 的 `send_signal` 之後正常。）

## 發現

| # | 嚴重度 | 內容 |
|---|---|---|
| P1 | 要使用者決定 | **在這台 import 的 Session，本機只有 `session.md`，沒有原始檔**（原始檔只在 pull 時才拿下來）。所以別台刪掉之後，`push --not-exist-upload` 會拒絕（「標頭指到的 raw-….json 本機沒有，不傳半套」），而 pull 也拿不到了——實際上**救不回來**，只能從 agent 重新 import 成新的 Session。符合 spec（「本機沒有那個原始檔時拒絕這一個」），但「保留並標記」的用意是讓使用者還能選擇傳回去。驗收草稿 6.10 會過，是因為 A 在 2.2 被 pull 過。 |
| P2 | Low | merge 的來源裡有雲端沒有的，會先為前面的來源寫要約（花一次 AI），輪到那一個才拒絕。應該在開始前就檢查全部的來源。 |
| P3 | Low | pull 已經是新的、略過時，結尾還是說「拉下 1 個」（略過也算進 done）。 |
| P4 | Low | merge 被中斷時印的是「沒存完的接續會在下一個 agora 指令自動補存」，那是 continue 的說法；merge 應該說「重跑會沿用已寫好的要約」。 |

P1 的選項：(a) import、continue、merge 寫完之後把剛上傳的原始檔留在本機鏡像（多佔一份磁碟，大小和 agent 那邊的 session 差不多）；(b) 維持現狀，但拒絕訊息改成提示「從 agent 重新 import（會是新的 Session）」；(c) 維持現狀。PM 建議 (a)：使用者選了「保留並標記」，就是要能救回來；(a) 也讓 continue 不必再下載一次原始檔。
