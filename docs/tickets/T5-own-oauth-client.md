# T5：首次設定支援自己的 OAuth client、文件改寫

2026-10-03，PM。狀態：待做（小件，不開 OpenSpec change，直接用這張單）。

## 背景

使用者 10-03 把 Drive 存取從 rclone 內建 client 換成自己的 OAuth client（issue #11，已關閉）：

- 專案 `aistorage-spike-2-260926`、Desktop client、`drive.file`；
- 被共用配額擋住時一次 rclone 約 50 秒，換了之後每次 0.6～0.8 秒。

agora 的程式沒有改：rclone 的 `[gdrive]` 有 `client_id`／`client_secret` 就用它，沒有就用內建的。但是有兩處還停在舊的說法。

## 要做的

| # | 內容 |
|---|---|
| 1 | 互動模式的首次設定（`tui.py` 裡跑 `rclone config create gdrive drive scope=drive.file` 的那一段）：多一個選填步驟，讓使用者給一個 client 設定檔的**路徑**，例如 Google 下載的 JSON，或 `Client-ID=`／`SECRET=` 兩行的文字檔。給了就帶 `client_id`／`client_secret`，沒給就照舊用內建的。程式只讀那個路徑，**不把值印出來、不寫進 log**；傳給 rclone 時也不經過 shell。 |
| 2 | `docs/design.md` D5 與它的注意事項改寫：現在用自己的 client，以及為什麼（共用配額、量測數字）；同意畫面要是正式版（否則 refresh token 7 天失效）；換 client 要搬家（10-03 的搬法：舊的下載 → 新的上傳 → 逐檔核對 md5 → 切換 `config.json` 的 folder ID → 舊的移到垃圾桶）；換新機器時複製 `rclone.conf` 與 `config.json` 即可 |
| 3 | README 的安裝／首次設定段落同步 |
| 4 | 測試：首次設定給了路徑時，rclone 的參數裡有 client_id；路徑不存在或格式不對時，提示並退回內建的；參數裡的 secret 不出現在任何畫面文字裡 |

## 不做

- 用 rclone 的設定檔加密（每次都要密碼，對 agora 不方便）。
