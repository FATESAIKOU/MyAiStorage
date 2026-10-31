# Agora lite

找、合、接 coding agent 的 Session。Session 以一般檔案存在 Google Drive，opencode 與 Claude Code 都能接著做。設計見 [docs/design.md](docs/design.md)。

## 安裝

```bash
uv tool install --editable .     # 之後就有 agora 指令
```

需要：
- `rclone`；
- `~/.config/agora/rclone.conf`：remote 名稱是 `gdrive`，scope 是 `drive.file`。第一次打 `agora`（互動模式）時會引導你用瀏覽器授權；也可以自己跑 `rclone config create gdrive drive scope=drive.file --config ~/.config/agora/rclone.conf`；
- **選填**自己的 OAuth client（Google Cloud 的 **Desktop** client）。rclone 內建的 client 配額是所有 rclone 使用者共用的，被限流時一次 rclone 約 50 秒，用自己的約 0.6～0.8 秒。

  怎麼給：**在首次設定選第二個項，給它一個 client 設定檔的路徑**——把 Google 下載的 JSON 存成檔案、把路徑給它；或給一個兩行 `Client-ID=`／`SECRET=` 的文字檔。值由 agora 讀出來直接交給 rclone，不會印出來、不寫進 log、也不經過 shell。讀不到（檔案不在、不是 UTF-8、裡面沒有 `client_id`／`client_secret`）時會問你要不要重來一次，還是改用內建的 client。`web` 類型的 client 不收：rclone 對它用 `http://127.0.0.1:53682/` 當回呼，那種 client 授權一定會失敗。

  **不要把 secret 打在命令列上**（會留在 shell 的歷史紀錄裡）。真的要自己跑 `rclone config create`，就讓值從檔案來：

  ```bash
  CONF=~/.config/agora/rclone.conf
  JSON=~/Downloads/client_secret_xxx.json      # Google 下載的那個
  ID=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["installed"]["client_id"])' "$JSON")
  SECRET=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["installed"]["client_secret"])' "$JSON")
  rclone config create gdrive drive scope=drive.file client_id="$ID" client_secret="$SECRET" --config "$CONF"
  unset ID SECRET
  ```

  設完就把那個 JSON 移走或刪掉：值已經在 `rclone.conf`（權限 600）裡了。授權時同意畫面**要選正式版**：testing 模式的 refresh token 只有 7 天。換 client 之後以前建的檔案要自己搬（見 design D5）；換機器只要複製 `rclone.conf` 與 `config.json`。

- `opencode`、`claude`、`agy`（Antigravity CLI；用到哪個裝哪個）。

- **指定 agent 執行檔**（#77）：預設就是 PATH 上的 `opencode`／`claude`／`agy`。`AGORA_OPENCODE_CMD`／`AGORA_CLAUDE_CMD`／`AGORA_AGY_CMD` 可以把它們換成指定的執行檔（值是執行檔路徑或 PATH 上的名稱，例如包裝過、或繞過包裝的 `claude-real`）。所有要跑 agent CLI 的呼叫都經過它：continue 開 agent（互動與 `--headless` 的 `serve`／stream-json）、opencode 的 `session export`／`session import`／`session delete`、merge 的要約（summarize）；沒設定就是用預設名。claude 的 import／export 直接讀寫 `~/.claude` 的檔案，不叫 CLI；agy 的 import／export 直接讀它的 db，也不叫 CLI；agora 也不會為了版本去跑 `--version`（版本從 jsonl／匯出檔讀）。這是正式介面（MyLinuxPool 的 sidecar 用它指定 worker 上的執行檔），不是測試專用的旋鈕；細節見 [docs/design.md](docs/design.md)。

- **agy 的目錄**（#26）：`AGORA_AGY_HOME` 換 agy 的資料目錄，預設 `~/.gemini/antigravity-cli`；測試與隔離用。agora 的 agy 轉接器在 agy 的目錄裡只讀寫 `conversations/` 與 `conversation_summaries.db`（覆蓋對話前會另備份到 agora 自己的 `~/.local/state/agora/agy-backups/`），**不讀 agy 的憑證檔**。同 agent 的接續可用，會寫入你真的 `~/.gemini/antigravity-cli`；**建議使用前先備份**。範圍見下面「agy 的範圍與限制」。

第一次執行時，agora 會在 Drive 建一個 `agora/` 資料夾，並把它的 folder ID 記到 `~/.config/agora/config.json`。

## 指令

```
agora <動作> <型態> [session_id] [選項]
```

動作是 `search`、`import`、`merge`、`continue`、`delete`、`edit`、`show`、`pull`、`push`；
型態目前只有 `session`。另有 `agora migrate inventory`（搬遷盤點，唯讀；見 [docs/migration.md](docs/migration.md)），它不在這個 `<動作> <型態>` 的格式裡。

不帶參數執行 `agora` 進入互動模式。關掉 kitty 鍵盤協定是為了讓輸入法能選字：沒有設定時 agora 會自己設成 1（關掉）；自己設了就照你的設定：只有 1 會關掉，其他任何值（包括空字串）都會打開。

**互動模式的鍵分兩邊**，看焦點在清單還是預覽區；只有 `Tab`、`shift+tab`、`q` 共用。底部按鍵列只印**目前焦點那一邊**能用的鍵，焦點換了就跟著換。

⚠️ **`Tab` 不再換頁**：換頁改成 `[`、`]`，只在清單有焦點時有效；`Tab`／`shift+tab` 現在都是在清單與預覽區之間切換焦點，頁面不會變。習慣用 `shift+tab` 切焦點的人照舊——`Tab` 現在也一樣是切焦點。

**焦點在清單**（清單本身，或篩選框；篩選框裡的字母鍵都是打字）

| 鍵 | 動作 |
|---|---|
| `↑` `↓` | 移動游標 |
| `[` `]` | 換頁（Agora／未匯入） |
| `空白` | 勾選／取消這一列 |
| `a` | 全選切換 |
| `/` | 篩選（id、agent、標題、目錄） |
| `ctrl+t` | 篩選切換「標題／內文」 |
| `Enter` | 接續（Agora 頁：可選 agent 與是否 fork，顯示工作目錄，確認同步間隔）／匯入（未匯入頁）。篩選框裡的 Enter 只收掉篩選框 |
| `m` `e` `d` `p` `P` | 合併／改標頭／刪除／pull／push |
| `Esc` | 篩選框開著時：清掉篩選、收起篩選框 |
| `Tab` `shift+tab` | 切到另一邊（清單 ⇄ 預覽區；兩個鍵都一樣）。從篩選框切出去等同按 `Enter`：篩選框收起來，篩選照樣生效 |
| `q` | 離開 |

**焦點在預覽區**（預覽區本身，或它的搜尋框；搜尋框裡的字母鍵都是打字）

| 鍵 | 動作 |
|---|---|
| `j` `↓`／`k` `↑` | 游標往下／往上移一列（游標那一行整行高亮） |
| `PgUp` `PgDn`／`←` `→` `Home` `End` | 翻一頁／在行內移動 |
| `g` | 到最前面（**先把還沒載入的內容全部載入**） |
| `G` | 到最後一行 |
| `/` | 搜尋**整份** Session：不分大小寫、不含檔頭，顯示「第 k 個／共 N 個」，符合會標亮 |
| `n` `N` | 下一個／上一個符合，到頭時繞回另一端 |
| `Esc` | 搜尋框開著時關掉它；沒開時清掉標亮與計數 |
| `Tab` `shift+tab` | 切到另一邊（清單 ⇄ 預覽區；兩個鍵都一樣）。從搜尋框切出去會關掉搜尋框、還沒送出的字丟掉，已有的標亮留著 |
| `q` | 離開 |

預覽區是唯讀的 Markdown 原文（有語法上色）。為了順，平常只讀檔案**尾端**一段，游標往上（`k`、`↑`、PgUp，或滑鼠滾輪捲到頂）才補前面的內容，而且畫面不會跳；`g` 一次全部載入。清單的 `/`（篩選）與預覽區的 `/`（搜尋整份對話）是兩個不同的搜尋。

`import`、`delete`、`merge`、`pull`、`push` 一次可以處理多個，處理時在 stderr 逐行印
`k/N` 進度，stdout 只印 agora id（所以可以直接接管線）。被 Ctrl-C 中斷後，重跑同一個
指令會接著做：import 略過已匯入的、delete 略過自己刪過的、merge 沿用已寫好的要約、
pull 略過已經是新的。

```bash
# 初次引入（只上傳指定的那一個 Session）；--external-session-id 可以給多次或用逗號分隔
agora import session --external-session-id ses_xxxx --agent opencode --header 'title=CSV 規劃'
agora import session --external-session-id 0f1e…-uuid --agent claude
agora import session --external-session-id 0f1e…-uuid --agent agy   # agy 的 id 是 conversations/<id>.db 的檔名
agora import session --external-session-id ses_a,ses_b --external-session-id ses_c --agent opencode
agora import session --external-session-id 0f1e…-uuid --agent claude --as-new   # 搬遷：忽略同來源的舊格式 Session，重匯成新的
agora migrate inventory --json --must-keep ~/keep.txt   # 搬遷盤點（唯讀）：哪些舊格式 Session 可以丟；流程見 docs/migration.md

# 找：--filter KEY=VALUE 是全等，KEY~=TEXT 是包含；text 是全文
agora search session --filter text~=表格
agora search session --filter agent=claude --filter tags=csv
agora search session --filter generated.by~=opencode
agora search session                          # 沒有 filter＝列出全部
agora search session --filter text~=CSV --no-sync   # 不連 Drive，只查本機索引
agora search session --filter tags=ai:claude-work --json   # 機器可讀：一個 JSON 陣列（欄位見 docs/design.md 5.1）
agora search session --sort updated --limit 10   # 依標頭最後被動過的時間（edit --add-tag 會刷新），只取前 10 筆
# 每行：agora id、對話日期、agent、標題或摘要；最後更新日與對話日期不同時補
# 「（更新：YYYY-MM-DD）」。清單順序預設仍是對話日期（新到舊）；--sort updated 改依標頭最後更新時間。
# --limit N 取排序與去重之後的前 N 筆（整數 ≥ 1）；--sort／--limit 只有 search 吃。
# 本機正在接續的標 (使用中)；只看得到這台機器的鎖，別台機器上的接續看不到。

# 合併成一個新的 Session（空白或逗號分隔都可以）
agora merge session agora:01K6…, agora:01K7…, agora:01K8…

# 接著做（agent 結束時自動存回，印出新的 agora id）
agora continue session agora:01K6… --agent opencode
agora continue session agora:01K8… --agent claude --dir ~/proj

# 接續期間每幾分鐘自動寫回（預設 5 分鐘；0 ＝ 只在結束時寫回；小數可）
agora continue session agora:01K6… --agent opencode --sync-period 1

# 從同一點分岔：先複製出新 Session 再開 agent，原本那個不動，寫回都寫到新的那個
agora continue session agora:01K6… --agent opencode --fork

# 啟動方交付 harness（單一 agent 生效，不改使用者層級檔案）
agora continue session agora:01K6… --agent opencode --harness-file /path/to/harness.md

# 把自己的參數原樣轉給 claude（只有 claude 吃；opencode／agy 收到會報錯）
# --agent-arg 只收 = 形式；-- 之後的一整串原樣接在 agora 自己的旗標後面（-- 放最後）
# 短旗標分開寫（-cp、-r<id> 這種合併／黏值會被拒）；多值旗標用 --flag=值，每個值一個
agora continue session agora:01K8… --agent claude --agent-arg=--effort --agent-arg=medium
agora continue session agora:01K8… --agent claude -- --permission-mode bypassPermissions --effort medium

# 改標頭：給 --header／--header-file／--add-tag 就直接疊加；都不給就用 $EDITOR 打開
# --add-tag 附加 tag（不覆蓋原有的、不重複，可重複給）；和 --header 併用時先套 --header 再附加
# --add-tag 只有 import、continue、edit 吃，其他指令收到會報錯（exit 1）
agora edit session agora:01K6… --header 'tags=[csv, 表格]' --header 'status=stable'
agora edit session agora:01K6… --add-tag csv --add-tag 表格
agora edit session agora:01K6…

# 刪除：本機立刻不見（鏡像、索引、outbox 那一筆），Drive 那份由背景移到垃圾桶
# （30 天內可以在 Drive 網頁還原）。指令不等背景。一定要加 --yes
# 有找不到的 id 時整批都不刪（exit 1）；自己刪過的 id 重跑時會略過
# 正在接續的（本機有人在寫回它）跳過不刪，並寫出是誰在用（agent、目錄、開始時間）；
# 其他照刪，exit 1；全部都被鎖住時什麼都不刪並明說。等它結束再重跑就會刪掉
# 要連使用中的一起刪：加 --force（--force 也要 --yes）。刪掉之後那個 agent 的寫回
# 會另存成新的 Session（原本那個 id 不會再有更新），刪之前會逐個警告
agora delete session agora:01K6… --yes
agora delete session agora:01K6…, agora:01K7… --yes
agora delete session agora:01K6… --force --yes    # 連正在接續的一起刪（會先警告）

# pull／push 只吃給的 id（沒有 --all；id 的前綴決定意思，不從形狀猜）
# agora:X → agora 的 Session；opencode:<id>／claude:<id>／agy:<id> → 這台機器上的 agent session
agora pull session agora:01K6…                    # 拿下 session.md 與它指到的原始檔
agora pull session claude:0f1e…-uuid              # 把那個 session 的全文寫進本機快取
agora pull session agy:0f1e…-uuid                 # 把那個 agy 對話的閱讀版寫進本機快取

# 把給的 agora Session 寫回 Drive：只傳 session.md 與它標頭指到的那一個原始檔
agora push session agora:01K6… agora:01K7…

# 雲端沒有的 Session（被另一台機器刪掉的）：本機的副本與索引都留著，標成「雲端沒有」，
# 同步**不會**自己清掉——要不要刪、要不要傳回去，是你的決定，要明確說。
agora search session --filter cloud=no        # 只列出雲端沒有的（每行最後標「(雲端沒有)」）
agora search session --filter cloud=yes       # 列出雲端還有的
agora pull session agora:01K6… --not-exist-delete   # 雲端沒有 → 刪掉本機這份
agora push session agora:01K6… --not-exist-upload   # 雲端沒有 → 把它傳回去（撤銷刪除）
# 兩個 flag 都不加時只印一行提醒、不動。還在 outbox（未上傳）或正在接續的不算「雲端沒有」，
# 也不會被 --not-exist-delete 刪掉。continue／edit 遇到雲端沒有的會拒絕（exit 1），
# delete 雲端沒有的只刪本機這份。對 agent 的 id，--not-exist-delete 是「agent 那邊已經沒有了，
# 就刪掉它的全文快取」；對 agora 的 id，Drive 上沒有 sessions/（資料夾 id 或 token 的問題）
# 時會拒絕那一個，不會當成刪除。

# 看標頭＋閱讀版／原始匯出
agora show session agora:01K6…
agora show session agora:01K6… --raw
```

**標頭：** 和 MyBrain 筆記一樣是 OKF frontmatter（`type`、`title`、`description`、`tags`、`status`、`generated`、`verified`、`sources`、`stale_after`…），另外有四個實體共通的 `id`、`refs`、`case`，以及系統欄位 `agora`。
- `import`／`merge`／`continue` 會自動填好需要的欄位，再依序疊上：
  1. `--header-file <yaml>`；
  2. 每一個 `--header KEY=VALUE`，同一個 key 後面的蓋掉前面的。
- KEY 可以用點路徑，例如 `generated.by=human:fatesaikou`。
- VALUE 用 YAML 解析，例如 `tags=[a, b]`、`sources=[{id: x, title: y}]`；清單欄位只給一個值時，會變成只有那一個值的清單。
- `id` 和 `agora` 區塊是系統欄位，不能改。

**exit code：**

| code | 意思 |
|---|---|
| 0 | 成功 |
| 1 | 做不到：找不到 id、沒有訊息可匯入、merge 少於兩個、delete 沒加 `--yes`、有子 Session、delete 有正在接續的而跳過（全部都被鎖住也是，訊息說明沒有刪除任何東西；背景上傳器起不來時仍是 3） |
| 2 | 錯誤：標頭、Drive、agent 匯出失敗或非預期的錯誤，以及指令用法錯誤 |
| 3 | 還在 outbox（背景上傳器起不來，或 `AGORA_UPLOAD=inline` 時這一輪沒送完） |
| 4 | 這個 Session 正在接續中（同一台機器有另一個 `continue` 在寫回它）：說明之前先印一行 `agora-locked <agora id>`，呼叫方用它分辨（409）；`--fork` 不受影響 |
| 5 | `continue` 的 agent 子行程死掉（`returncode != 0`，130／143／-2／-15 除外，見下）：寫回照做，stderr 印一行 `agora-agent-failed rc=<原碼>`；寫回成功回 5，寫回失敗回 3（同樣印那一行） |

- `continue` 的 agent 失敗（#76）：舊行為是只看寫回結果，agent 死掉又沒有新內容時回 0；第一版改成回 agent 原碼，但原碼會與 agora 自己的 1／2／3／4 撞碼。新行為是寫回仍然照做（死前的內容不丟），但 agent 以 `returncode != 0` 結束（排除 130／143、-2／-15，以及轉送過 SIGTERM／SIGHUP 之後的結束（不論 agent 回什麼碼）；-9／-11 都算失敗）時，stderr 印固定一行 `agora-agent-failed rc=<原碼>`（負值照印，如 `rc=-9`），寫回成功回固定的 5，寫回失敗維持 3。正常結束（0）、使用者離開、被 SIGTERM 溫和停下（exit 0；#107 之後 headless 與互動版都一樣，互動版連 SIGHUP 也當同一個訊號）不變。0／1／2／3／4 的原語意完全不變：絕不把 agent 原碼當 exit code，sidecar 靠 exit 4＝locked 判斷不受影響。

**`agora continue` exit code 新舊對照（#76）：**

| code | 意思 | 舊行為 | 新行為 | stderr 訊號 |
|---|---|---|---|---|
| 0 | 成功：寫回成功且 agent 正常（0、130／143、-2／-15、轉送的結束） | 回 0 | 不變 | 無固定行 |
| 1 | 做不到：找不到 id、雲端沒有、`--sync-period` 非法等 | 回 1 | 不變 | 中文說明，無固定行 |
| 2 | 錯誤：標頭、Drive、agent 匯出失敗或非預期錯誤 | 回 2 | 不變 | 中文說明，無固定行 |
| 3 | 還在 outbox：寫回已存本機但上傳沒送完 | 回 3 | 不變；agent 同時死時仍回 3，但多印 agent 那一行 | 上傳失敗說明；agent 同死時加 `agora-agent-failed rc=<原碼>` |
| 4 | 被鎖：同機另一個 `continue` 正在寫回它 | 回 4 | 不變（絕不被 agent 原碼污染） | `agora-locked <agora id>` 在說明之前 |
| 5 | agent 死掉：`returncode != 0`（排除 130／143、-2／-15、轉送過 SIGTERM／SIGHUP 之後的結束；-9／-11 算） | 回 0（失敗被吞掉） | 新增：寫回照做，寫回成功回 5，寫回失敗回 3（都印行） | `agora-agent-failed rc=<原碼>`（如 `rc=3`、`rc=-9`） |

**先存本機，Drive 的部分交給背景。** import、continue、merge、edit 寫完本機（鏡像裡有完整的一份，含原始檔）就回 exit 0，背景上傳器慢慢送；`delete` 也是本機先不見，Drive 那份排進 `<state>/trash-queue/`，背景逐一 purge。所以指令的離開時間不再由 Drive 決定。

- 背景輸出寫在 `~/.local/state/agora/upload.log`（超過 1 MB 時，下一次背景啟動會把它清空重寫），鎖是同目錄的 `upload.lock`；它的 stdout 不接到你的終端機，所以管線還是讀得到 EOF。
- 每個指令開頭若發現還在等的東西，會啟動背景並提示「outbox 有 N 筆未上傳」／「有 N 個等著移到 Drive 垃圾桶」（背景正在跑時不提示），不會在前台慢慢送。
- **只有 `push` 會等**：它的契約是「回來時那幾筆在 Drive 上」，所以等到自己剛寫的 id 離開 outbox 才算成功；等不到就繼續等（每 10 秒說一次為什麼），Ctrl-C 隨時可退。
- 還在 outbox 或正等著移到垃圾桶的 Session，`pull`／`push` 會拒絕並說「正在刪除」；雲端沒有的（別台刪掉的）Session 在那之前照樣搜得到、標成「雲端沒有」。

不用自己同步：每個指令開頭都會把 outbox 與刪除佇列交給背景上傳器，需要時也會從 Drive 拉新的內容。

**壓縮：** 用 agent 內建的指令，opencode 是 `/compact`，Claude Code 是 `/compact`。壓縮後的內容會在 agent 結束時存回 Agora。

**已知限制（接續進 opencode 用 Zen 免費模型）：** `continue` 開出來的 opencode session——
跨 agent 接續、或 opencode 接續自己，都一樣——**不能用 Zen（`opencode` provider）的免費模型**，
第一個請求就被擋（HTTP 403 `FreeTierError`：`free tier can only be used from within OpenCode`）。
要接續請改用 **Ollama Cloud 的 DeepSeek**（`ollama-cloud/deepseek-v4.1-flash`）——它不在 Zen 的免費層，
這個專案實際就是這樣接續的。（Zen 的**付費**模型是否也擋匯入進來的 session，這次沒有實測。）

這是 Zen 對「不是 opencode 自己開的對話」的限制，不是 agora 送出的東西造成的：匯入一個
**0 則訊息**的 session 也被拒，與對話大小無關（兩千到二十萬字元第一個請求就被擋），
而那個 session 送出的 request 與 opencode 自開 session 的第一輪逐欄位相同（body 一樣，
只有 session id 不同）。完整的實測、排除過的假設與證據表在 [docs/review/agora-free-85.md](docs/review/agora-free-85.md)。

## agy 的範圍與限制

agy 的對話是一個 SQLite 檔（`~/.gemini/antigravity-cli/conversations/<id>.db`），清單與標題在旁邊的 `conversation_summaries.db`。agora 直接讀這兩個地方，不叫 agy 的 CLI（接續除外）。

- **可以**：`agora import session --external-session-id <agy 對話 id> --agent agy`；互動模式的「未匯入」頁會列出來；搜尋標題與 preview；預覽；**同 agent 的互動接續**。import、列表、搜尋、預覽都是唯讀。
- **搜尋的範圍**：agy 自己的摘要表只有標題與 preview，所以「未匯入」頁的內文搜尋只看得到這兩欄，**不搜對話內文**（切到內文模式時狀態列會說明）。匯入之後，閱讀版就在 agora 的索引裡，照一般規則搜。
- **接續會寫入真的 agy 目錄**：`agora continue session <agora id> --agent agy` 會寫入你真的 `~/.gemini/antigravity-cli`（還原 `conversations/` 的對話檔，缺的話補 `conversation_summaries.db` 的一列）。真 agy 載入還原的對話、正常退出後不留 `-wal`／`-shm`、以及它如何改寫 agora 補的那一列，都在 2026-10-21 驗過（docs/agy-storage.md §12）。**建議使用前先備份 `~/.gemini/antigravity-cli`**。來源必須是 agy 匯入的 Session。

  ```bash
  # 先備份 ~/.gemini/antigravity-cli
  agora continue session agora:01K6… --agent agy --dir ~/proj
  ```

- **不支援**：`--fork`、`--headless`、跨 agent 的接續（agy → opencode／claude 或反過來）、以及 merge（`--agent agy`，或來源是 agy 匯入的 Session）；都在讀 Session 內容或叫 agent 之前拒絕，exit 1。
- **一輪定期寫回的成本**：agy 的對話是整份 SQLite，有新的內容時，每一輪寫回都整份匯出、base64 後整份上傳，沒有差分；對話大了會慢。差分上傳是另案。

探查與理由見 [docs/agy-storage.md](docs/agy-storage.md)（§12 真機驗證、§13 曾經的預設閘門）。

## 檔案放哪

| 位置 | 內容 |
|---|---|
| Drive `agora/sessions/<ULID>/` | `session.md`（header＋閱讀版；header 的 `agora.raw.segments` 是 raw 的清單）、`seg-<世代>-<序>-<md5 前 12 碼>.part`（raw 的一段，接起來就是整份原始匯出；沒有整份 `raw-*.json`）。升級前上傳的舊格式 Session 還有 `raw-<md5>.json`：閱讀版可讀、raw 不可用、不要刪、沒有原地轉存，見 [docs/migration.md](docs/migration.md) |
| `~/.config/agora/` | `rclone.conf`、`config.json` |
| `~/.cache/agora/` | 鏡像與搜尋索引（含整份原始檔），刪掉也會重建 |
| `~/.local/state/agora/` | `outbox/`（等著上傳的）、`trash-queue/`（等著移到 Drive 垃圾桶的）、`pending/`（接續中的）、`upload.log`（背景的記錄）、`*/.bad/`（讀不了的壞檔，每個指令都會提示）——**不要刪** |

## 測試

```bash
uv run pytest -q                                   # 單元測試：不碰 Drive、不跑真的 agent
# 整合測試：真的 Drive（只用 agora-test/，AGORA_FOLDER_NAME=agora-test）、真的 opencode 與
# claude（自編短對話，claude -p 約 6 次），要跑好幾分鐘，所以丟到背景：
nohup uv run pytest -q -m integration tests/integration > it.log 2>&1 &
```

## 授權

[MIT](LICENSE)。第三方內容的授權聲明見 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。
