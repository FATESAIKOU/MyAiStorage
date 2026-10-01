# Spike：Google Drive（V4）

2026-10-02，impl1。**rclone v1.69.3**（darwin arm64）。憑證只用路徑引用
`--config "$HOME/.config/agora/rclone.conf"`，**全程沒有讀過、沒有印過**那個檔
案；token 只用 `jq` 取 `expiry` 欄位。

測試範圍只有 `gdrive:agora-test/`（`scope = drive.file`，所以 remote 底下看得到
的就是自己建立的檔案）。測完 `rclone purge gdrive:agora-test` 收尾，
`rclone lsf gdrive:` 回到空。

腳本：`spike/drive/drive_probe.sh`（一步一節，每個 rclone 呼叫都包 `timeout 90`，
結尾一定 purge）。

## 結論

**可行。** 建資料夾、上傳、列出、增量下載、刪除在 `drive.file` 下全部可行，
需要的旗標只有 `--config`。但有**四個會靜默做出錯的事的陷阱**，其中兩個會直接
讓 Agora 的資料形狀不對（另外兩個是不會報錯、要靠比對才發現的行為）。

## 逐步結果

| # | 做什麼 | 指令 | 結果 |
|---|---|---|---|
| 0 | 確認 remote | `rclone --config … listremotes` ／ `lsd gdrive:` | `gdrive:` 存在；根目錄空的（rc=0，無輸出） |
| 1 | 建目錄 | `rclone mkdir gdrive:agora-test/sessions/<ULID>` | rc=0，**不需**事先建父層 |
| 2 | 上傳 | `rclone copyto <本機檔> gdrive:…/session.md` | rc=0；`lsjson` 顯示 `MimeType=text/markdown`、`Size` 正確、`IsDir=false` |
| 3 | 列出 | `lsf … --format pst` ／ `lsjson` | `raw.json;48;2026-10-02 00:28:25`（路徑;大小;時間）；`lsjson` 另有 `Name/ID/ModTime/IsDir/MimeType` |
| 4 | 增量下載 | `rclone copy gdrive:…/sessions/<ULID> <mirror> --include session.md --include raw.json` | 第一次取回 2 檔 |
| 4b | 再下一次 | 同上（加上 `-v`） | `There was nothing to transfer`、`Checks: 3 / 3`、`Transferred: 0 B` ——**只拿新的** |
| 4c | 遠端加檔 | 遠端加一個檔，再 `copy --include raw2.json` | 只補那 1 個 |
| 5d | 覆寫 | `copyto` 覆寫既有的 `session.md` | 成功（同一個 Session 重新匯入的「最後寫的贏」，D2） |
| 6 | 遠端刪檔 | `deletefile gdrive:…/raw2.json` 後再看鏡像 | **鏡像裡的檔案還在**（鏡像不會自動少） |
| 7 | token | `rclone config dump \| jq '.gdrive.token\|fromjson\|{expiry}'` | 見下 |
| 8 | 收尾 | `rclone purge gdrive:agora-test` | rc=0；`lsf gdrive:` 空 |

## 四個陷阱

### 陷阱 1：`copy` 給「檔名」當目的地，會建成**資料夾**

```bash
rclone copy  $WORK/session.md gdrive:agora-test/sessions/<ULID>/trap.md
#   rc=0  ← 沒有報錯
rclone lsf   gdrive:agora-test/sessions/<ULID> --format p --dirs-only
#   trap.md/                      ← 資料夾，不是檔案
rclone lsjson gdrive:agora-test/sessions/<ULID>
#   {"Name":"trap.md","Size":0,"MimeType":"inode/directory","IsDir":true, …}
```

`copy` 的目的地**永遠被當成目錄**（「把來源複製進這個目錄」）。要單檔對單檔必須
用 **`copyto`**（或把目的地寫成 `…/<ULID>/`，讓它保留原檔名）。兩者都 rc=0，
所以這是**靜默的資料形狀錯誤**：Drive 上看起來有檔案，實際上是空資料夾，
`rclone copy` 下載回來也只會得到一個空目錄。

→ **Agora 上傳 `session.md`／`raw.json` 必須用 `copyto`；若要用 `copy`，目的地
必須以 `/` 結尾。** 建議實作上乾脆全部用 `copyto`（檔名固定兩個，沒有歧義）。

### 陷阱 2：`--drive-root-folder-id` 的目標必須寫成 remote

```bash
ID=<lsjson 拿到的 ID>
rclone --config … --drive-root-folder-id "$ID" lsf gdrive: --format pst
#   session.md;89;…            ← 正確：相對於那個資料夾
rclone --config … --drive-root-folder-id "$ID" cat gdrive:session.md
#   --- / entity: agora / …    ← 正確
rclone --config … --drive-root-folder-id "$ID" lsf .
#   .git / .gitignore / docs/ / spike/   ← **本機** cwd 的內容，完全沒碰到 Drive
```

用 folder ID 當根是**可行的**（列出與讀檔都正常，路徑要相對於該資料夾），
但目標不能寫 `.`／本機路徑，否則 rclone 會把它當本機目錄、靜默列出本機檔案。
這在 Agora 裡不會發生（我們永遠寫 `gdrive:` 開頭），但值得記一筆。

用 `lsjson --dirs-only` 就能拿到 folder ID（`ID` 欄位）——這代表**索引可以存
Drive 的檔案 ID**，之後要改名／移動／刪除都不必再靠名字比對。

### 陷阱 3：同一層的同名資料夾 vs 同名檔案

| 動作 | 結果 |
|---|---|
| `mkdir gdrive:agora-test/sessions/dup` 連跑兩次 | 兩次 **rc=0**，**冪等**，沒有生出第二個 `dup/`（列出只有一個） |
| `copyto <檔> gdrive:agora-test/sessions/dup`（`dup` 已是資料夾） | rc=1，重試 3 次後 `Failed to copyto: is a directory not a file` |
| `mkdir gdrive:agora-test/session.md`（`session.md` 已是**檔案**） | rc≠0，`CRITICAL: Failed to create file system for "…": is a file not a directory` |

→ 同一層「兩個同名資料夾」rclone 會**復用既有的那個**，不會報錯也不會產生第二個。
Drive 本身容得下同名資料夾，但 rclone 這條路看不出來——也就是說**撞名在這裡是
不可見的**。因為 Agora 的路徑是 `agora/sessions/<ULID>/`，ULID 不會撞，這不是風險，
但 `agora search` 若用「同名資料夾」當識別鍵就會錯，**要用 ULID**。
（反方向「檔名撞到既有資料夾」「資料夾名撞到既有檔案」都會**明確報錯**，這是好的：
撞名要讓它報錯，不要吞掉。）

### 陷阱 4：zsh 的 `$FLAGS` 不會分字

`drive_probe.sh` 第一次寫成 `rclone $FLAGS lsd gdrive:`，在 **zsh** 下（`$FLAGS`
不自動分字）整個 `--config /Users/…/rclone.conf` 變成**一個**引數，rclone 於是
把後面的路徑當成子命令：

```
Error: unknown command "gdrive:agora-test" for "rclone"
```

`bash` 會分字所以看不出來。→ Agora 的 shell 呼叫要用 `subprocess.run([...])` 的
引數陣列，或在 zsh 裡把每個引數寫死／用 `${=FLAGS}`。**這條對 Python subprocess
不適用**（本來就是分字的），但同一支程式若哪天包一層 shell 就要注意。

## Token 過期

`rclone config dump` 的 `.gdrive` 欄位：
`type=drive`、`scope=drive.file`、`team_drive=""`、`token`（JSON 字串，內含
`access_token` / `expiry` / `refresh_token` / `token_type`）。

```
expiry = 2026-10-02T00:51:06.318512+09:00      （= 2026-10-01T15:51:06Z）
token_type = Bearer
```

觀察到的是 **access token 約 1 小時壽命**（不是 7 天；`refresh_token` 才是長期的，
它也存在同一個檔案裡）。整個測試期間（跨過多次 rclone 呼叫）`expiry` 欄位**沒有
變過**，因為當時 access token 還沒到期。

**這代表：**
- Agora **不能**自己讀／解析 token（那會把憑證值帶進程式）；一律讓 rclone 自己去
  刷新，`agora` 只在呼叫 rclone 時用 `--config` 帶路徑。
- 憑證檔會被 rclone 自己改寫（刷新後 `expiry` 變成新的時間），所以
  `~/.config/agora/rclone.conf` 的權限只需要給**同一個使用者**，不需要凍結寫入。
- 憑證失效（refresh token 被撤銷）時的行為還沒測；`rclone config reconnect
  gdrive` 是互動式的，需要人在終端機上走一次 —— 這件事現在就該決定要怎麼處理
  （見下面建議）。

## Agora 需要的 rclone 旗標

| 用途 | 指令 | 需要的旗標 |
|---|---|---|
| 建 session 目錄 | `rclone mkdir gdrive:agora/sessions/<ULID>` | `--config <路徑>` |
| 上傳兩個檔 | `rclone copyto <本機> gdrive:…/<檔名>` | `--config <路徑>`；**必須 copyto** |
| 列出（給索引） | `rclone lsjson gdrive:agora/sessions --recursive` | `--config`（要 `--recursive` 才走深層） |
| 增量下載 | `rclone copy gdrive:agora/sessions <鏡像>/sessions` | `--config`；鏡像目錄不存在時 rclone 會自己建 |
| 刪一個 session | `rclone purge gdrive:agora/sessions/<ULID>` | `--config` |
| 刪整個 agora | `rclone purge gdrive:agora` | `--config` |

建議再加的：
- `--drive-root-folder-id <ID>`：**可以不要**。好處是 agora 的根資料夾改名或移動時
  不會壞（索引存 ID 就能繼續）；壞處是多一個要保存的欄位。初期建議**不要**，
  等真的被改名再說。
- `--transfers`／`--checkers`：Drive 的 API 配額有限，**不要**開太大。實測
  單次 `lsjson`／`copy` 在 0.5～2 秒內回來，預設值就夠。
- `--drive-...` 的其他參數（`--drive-pacer-min-sleep` 等）**不需要**。
- 刪除 Drive 上的檔案要靠 `rclone deletefile`／`purge`，**沒有**「刪本機鏡像裡
  對應檔案」的旗標；鏡像與 Drive 不同步（陷阱 3 那條）要靠 Agora 自己處理：
  `sync` 時把「Drive 上不存在但鏡像有」視為刪除。

## 對 design.md 的修改建議

1. §4 的路徑補一句：**建目錄不需事先建父層**（`rclone mkdir` 會連建），但
   **上傳檔案要用 `copyto`**——§5.2／§4 寫「上傳 session.md 與 raw.json」時
   應該把指令列明確寫出來，這是本次最容易踩到的錯。
2. §5.1 `sync` 的「增量」要寫清楚是 `rclone copy`（只取新的）**加上**「遠端刪掉
   的，本機鏡像要跟著刪」這半邊；`rclone copy` 本身**不會**刪本機的檔案。
3. §2 D5／§4 的 `~/.config/agora/rclone.conf`：補一句 **access token 約 1 小時
   會過期，rclone 會自己用 refresh token 換並改寫這個檔**，Agora 不要自己碰
   token；另外要決定 refresh token 失效時怎麼辦（現在只能
   `rclone config reconnect gdrive` 互動重跑）。
4. §4 的索引可以存 Drive 的**檔案／資料夾 ID**（`lsjson` 的 `ID` 欄位就有），
   這樣刪除不用靠名字比對，也為將來「改名或移動根資料夾」留出路。

## 清理

`rclone purge gdrive:agora-test` 後 `rclone lsf gdrive:` 沒有輸出。Drive 上沒有
留下本次測試的任何資料，也沒有碰 `agora-test/` 以外的東西。