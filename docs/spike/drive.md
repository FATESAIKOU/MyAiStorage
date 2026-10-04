# Spike：Google Drive（V4）

2026-10-02，impl1。**rclone v1.69.3**（darwin arm64）。憑證只用路徑引用
`--config "$HOME/.config/agora/rclone.conf"`，**全程沒有讀過、沒有印過**那個檔
案；token 只用 `jq` 取 `expiry` 欄位。

測試範圍只有 `gdrive:agora-test/`（`scope = drive.file`，所以 remote 底下看得到
的就是自己建立的檔案）。測完 `rclone purge gdrive:agora-test` 收尾，
`rclone lsf gdrive:` 回到空。

腳本：`spike/drive/drive_probe.sh`（2026-10-04 清理時已從 repo 移除，要看請用 `git show fd3ba9f:<路徑>` 從歷史取回）（一步一節，每個 rclone 呼叫都包 `timeout 90`，
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
| 3b | **一次拿到所有 md5**（§4.3 指定的方式） | `rclone lsjson -R --fast-list --hash gdrive:agora-test/` | rc=0；**每個檔案都有 `Hashes.md5`**（另外還有 sha1／sha256），而且**與本機 `md5 -q` 完全相同**（見下表）；目錄項目也有 `ID` |
| 4 | 增量下載 | `rclone copy gdrive:…/sessions/<ULID> <mirror> --include session.md --include raw.json` | 第一次取回 2 檔 |
| 4b | 再下一次 | 同上（加上 `-v`） | `There was nothing to transfer`、`Checks: 3 / 3`、`Transferred: 0 B` ——**只拿新的** |
| 4c | 遠端加檔 | 遠端加一個檔，再 `copy --include raw2.json` | 只補那 1 個 |
| 5d | 覆寫 | `copyto` 覆寫既有的 `session.md` | 成功（同一個 Session 重新匯入的「最後寫的贏」，D2） |
| 6 | 遠端刪檔 | `deletefile gdrive:…/raw2.json` 後再看鏡像 | **鏡像裡的檔案還在**（鏡像不會自動少） |
| 7 | token | `rclone config dump \| jq '.gdrive.token\|fromjson\|{expiry}'` | access token 約 1 小時；到期後 rclone 自己刷新並改寫檔案（見下） |
| 8 | 收尾 | `rclone purge gdrive:agora-test` | rc=0；`lsf gdrive:` 空 |

## `lsjson --hash`：Drive 的 md5 能不能用（§4.3 的核心）

§4.3 要「列檔一律 `rclone lsjson -R --fast-list --hash`，一次拿到所有檔案的 md5；
用 md5 判斷要不要下載（不用時間）」。實測：

```bash
rclone lsjson -R --fast-list --hash gdrive:agora-test/
```

| 本機檔案 | 本機 `md5 -q` | Drive `Hashes.md5` | 一致 |
|---|---|---|---|
| `session.md`（12 bytes，`text/markdown`） | `7d8ad0147d0cc2a069b45da054cfa922` | 同左 | ✅ |
| `raw.json`（16 bytes，`application/json`） | `207999057af01556a81681700da2d6a6` | 同左 | ✅ |

`lsjson` 的每個項目欄位（v1.69.3）：`Path`、`Name`、`Size`、`MimeType`、`ModTime`、
`IsDir`、`Hashes{md5,sha1,sha256}`（檔案）或 `ID`（兩者都有，目錄也有 `ID`）。

→ **`--hash` 可行，Drive 端 md5 與本機 `md5 -q` 逐字相同**，`raw-<md5 前12>.json`
的命名與「用 md5 判斷要不要下載」都成立。兩個檔案一個是文字檔一個是 JSON，
MimeType 不同但都有 md5。

時間成本（3 層 4 個項目）：`-R` 1.81 秒、加 `--fast-list --hash` 1.82 秒——
**在這個規模下 `--fast-list` 沒有帶來可測的加速**。Agora 的 session 數變多之後
才值得重新量一次；先用設計上寫的那組旗標就好。

一個限制要知道：Drive 只對**二進位內容**提供 md5。從 Drive 網頁上傳的原生
Google 檔案（Docs/Sheets）沒有 md5，`Hashes` 會是空的——但 D5／`drive.file`
本來就看不到那些檔案，所以對 Agora 不構成問題。

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

用 `lsjson --dirs-only` 就能拿到 folder ID（`ID` 欄位）——§2 D5 決定要把 folder ID
寫進 `config.toml`，這個 ID 就是這樣來的。

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
測試開始時  expiry = 2026-10-02T00:51:06.318512+09:00   （= 2026-10-01T15:51:06Z）
到期後      expiry = 2026-10-02T01:54:27.788543+09:00   （= 2026-10-01T16:54:27Z）
token_type = Bearer
```

（`~/.config/agora/client-worker.json` 也看過欄位名：**只有** `client_id`、
`client_secret`、`project_id`、三個 URL、`redirect_uris`——是 OAuth *client* 的
描述，**沒有 token 也沒有 expiry**。所以整個 `~/.config/agora/` 裡唯一的
expiry 就是 rclone.conf 的那一個。）

觀察到的是 **access token 約 1 小時壽命**（不是 7 天；`refresh_token` 才是長期的，
它也存在同一個檔案裡、沒有 expiry 欄位）。§2 D5 講的「refresh token 7 天失效」
是**另一件事**（consent screen 沒設 In production 的後果），別把兩個數字混在
一起：rclone.conf 裡看得到的 `expiry` 是 1 小時級距的 access token。

**自動刷新實測過**（這是 agora 能不能無人值守的關鍵）：在 access token 過期
（15:51:06Z）之後跑第一個 rclone 指令（`lsf gdrive:`），rclone 自己用
refresh_token 換了新的 access token，**並把新的 expiry 寫回 rclone.conf**
（15:54:29Z 讀到 16:54:27Z）。整個過程沒有任何錯誤訊息、沒有互動、rc=0。

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
| 列檔（給索引與 md5 比對） | `rclone lsjson -R --fast-list --hash <agora 根>` | `--config`；`--hash` 才有 `Hashes.md5` |
| 增量下載 | `rclone copy <agora 根>/sessions <鏡像>/sessions` | `--config`；鏡像目錄不存在時 rclone 會自己建 |
| 刪一個 session | `rclone purge gdrive:agora/sessions/<ULID>` | `--config` |
| 刪整個 agora | `rclone purge gdrive:agora` | `--config` |
| 用 folder ID 當根（§2 D5 已決定要用） | `rclone --drive-root-folder-id <ID> lsjson -R --fast-list --hash gdrive:` | `--config`、`--drive-root-folder-id`；**目標一定要寫 `gdrive:`** |

建議再加的：
- `--drive-root-folder-id <ID>`：**要**（§2 D5 已決定）。實測可行：列出與讀檔都
  正常，路徑相對於該資料夾。**但目標必須是 remote**（`gdrive:` 開頭），寫成 `.`
  會變成列本機目錄（陷阱 2）。ID 從 `lsjson --dirs-only` 的 `ID` 欄位拿。
- `--transfers`／`--checkers`：Drive 的 API 配額有限，**不要**開太大。實測
  單次 `lsjson`／`copy` 在 0.5～2 秒內回來，預設值就夠。
- `--drive-...` 的其他參數（`--drive-pacer-min-sleep` 等）**不需要**。
- 刪除 Drive 上的檔案要靠 `rclone deletefile`／`purge`，**沒有**「刪本機鏡像裡
  對應檔案」的旗標；鏡像與 Drive 不同步（§4.3 那條）要靠 Agora 自己處理：
  `sync` 時把「Drive 上不存在但鏡像有」視為刪除。

## 對 design.md 的修改建議

design.md 已經是第 2 版，所以以下是「**要補的**」而不是改寫既有決定：

1. **§5.2／§4.1 的上傳指令要寫成 `copyto`。** §4.1 說「上傳 raw」「上傳
   session.md」但沒寫指令；`rclone copy` 給檔名當目的地會**靜默建成空資料夾**，
   這是本次最容易踩到的錯。寫成 `rclone copyto <本機> <遠端完整檔名>`。
2. **§2 D5 的 folder ID 存取**：可行，但要在該節寫一句「目標路徑必須是
   `gdrive:` 開頭；寫本機路徑會靜默讀本機目錄」。另外 ID 從
   `lsjson --dirs-only` 的 `ID` 欄位取（實測）。
3. **§4.3 的 `lsjson -R --fast-list --hash` 確認可用**：Drive 的 `Hashes.md5` 與
   本機 `md5 -q` 逐字相同，`raw-<md5 前12>.json` 的命名與「用 md5 判斷要不要
   下載」都成立。補一個限制說明：Google 原生檔案沒有 md5（`drive.file` 看不到，
   所以不構成問題）。
4. **§4.3／§6 的「Drive 上刪掉的，鏡像跟著刪」**：`rclone copy` **不會**刪本機
   檔案（實測遠端 `deletefile` 後鏡像裡還在）。這半邊必須由 Agora 自己做，
   建議寫成「用 `lsjson --hash` 的結果與鏡像比對，Drive 沒有的就刪」——正好
   md5 已經拿到了，不用額外呼叫。
5. **§2 D5 的 token**：實測 `expiry` 是 **access token，約 1 小時**（見上面
   「Token 過期」），不是 7 天；7 天是 **consent screen 沒設 In production** 時
   refresh token 的失效期，這兩件事要分開寫，否則實作的人會去讀 rclone.conf 的
   `expiry` 當成 7 天。要補的是：rclone 會自己刷新並**改寫這個檔**，所以
   `~/.config/agora/rclone.conf` 不能設成唯讀；Agora 不要自己讀 token。
6. **§4.5 索引**：可以存 Drive 的**檔案／資料夾 ID**（`lsjson` 的 `ID` 欄位），
   刪除不用靠名字比對。既然 §2 D5 已經決定用 folder ID 存根目錄，把每個檔案的
   ID 一起存下來是同一個成本。

## 清理

`rclone purge gdrive:agora-test` 後 `rclone lsf gdrive:` 沒有輸出。Drive 上沒有
留下本次測試的任何資料，也沒有碰 `agora-test/` 以外的東西。