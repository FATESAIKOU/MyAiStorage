# Agora lite

找、合、接 coding agent 的 Session。Session 以一般檔案存在 Google Drive，opencode 與 Claude Code 都能接著做。設計見 [docs/design.md](docs/design.md)。

## 安裝

```bash
uv tool install --editable .     # 之後就有 agora 指令
```

需要：
- `rclone`；
- `~/.config/agora/rclone.conf`：remote 名稱是 `gdrive`，scope 是 `drive.file`，用 rclone 內建的 client。第一次打 `agora`（互動模式）時會引導你用瀏覽器授權；也可以自己跑 `rclone config create gdrive drive scope=drive.file --config ~/.config/agora/rclone.conf`；
- `opencode`、`claude`（用到哪個裝哪個）。

第一次執行時，agora 會在 Drive 建一個 `agora/` 資料夾，並把它的 folder ID 記到 `~/.config/agora/config.json`。

## 指令

```
agora <動作> <型態> [session_id] [選項]
```

動作是 `search`、`import`、`merge`、`continue`、`delete`、`edit`、`show`、`pull`、`push`；
型態目前只有 `session`。

`import`、`delete`、`merge`、`pull`、`push` 一次可以處理多個，處理時在 stderr 逐行印
`k/N` 進度，stdout 只印 agora id（所以可以直接接管線）。被 Ctrl-C 中斷後，重跑同一個
指令會接著做：import 略過已匯入的、delete 略過自己刪過的、merge 沿用已寫好的要約、
pull 略過已經是新的。

```bash
# 初次引入（只上傳指定的那一個 Session）；--external-session-id 可以給多次或用逗號分隔
agora import session --external-session-id ses_xxxx --agent opencode --header 'title=CSV 規劃'
agora import session --external-session-id 0f1e…-uuid --agent claude
agora import session --external-session-id ses_a,ses_b --external-session-id ses_c --agent opencode

# 找：--filter KEY=VALUE 是全等，KEY~=TEXT 是包含；text 是全文
agora search session --filter text~=表格
agora search session --filter agent=claude --filter tags=csv
agora search session --filter generated.by~=opencode
agora search session                          # 沒有 filter＝列出全部
agora search session --filter text~=CSV --no-sync   # 不連 Drive，只查本機索引

# 合併成一個新的 Session（空白或逗號分隔都可以）
agora merge session agora:01K6…, agora:01K7…, agora:01K8…

# 接著做（agent 結束時自動存回，印出新的 agora id）
agora continue session agora:01K6… --agent opencode
agora continue session agora:01K8… --agent claude --dir ~/proj

# 改標頭：給 --header／--header-file 就直接疊加；都不給就用 $EDITOR 打開
agora edit session agora:01K6… --header 'tags=[csv, 表格]' --header 'status=stable'
agora edit session agora:01K6…

# 刪除：移到 Drive 垃圾桶（30 天內可以在 Drive 網頁還原），一定要加 --yes
# 有找不到的 id 時整批都不刪（exit 1）；自己刪過的 id 重跑時會略過
agora delete session agora:01K6… --yes
agora delete session agora:01K6…, agora:01K7… --yes

# pull／push 只吃給的 id（沒有 --all；id 的前綴決定意思，不從形狀猜）
# agora:X → agora 的 Session；opencode:<id>／claude:<id> → 這台機器上的 agent session
agora pull session agora:01K6…                    # 拿下 session.md 與它指到的原始檔
agora pull session claude:0f1e…-uuid              # 把那個 session 的全文寫進本機快取

# 把給的 agora Session 寫回 Drive：只傳 session.md 與它標頭指到的那一個原始檔
agora push session agora:01K6… agora:01K7…

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
| 1 | 做不到：找不到 id、沒有訊息可匯入、merge 少於兩個、delete 沒加 `--yes`、有子 Session |
| 2 | 錯誤：標頭、Drive、agent 或非預期的錯誤，以及指令用法錯誤 |
| 3 | 已存在本機 outbox，還沒上傳到 Drive（之後的指令會自動再送） |

不用自己同步：每個指令開頭都會自動把沒上傳成功的送出去，需要時也會從 Drive 拉新的內容。

**壓縮：** 用 agent 內建的指令，opencode 是 `/compact`，Claude Code 是 `/compact`。壓縮後的內容會在 agent 結束時存回 Agora。

## 檔案放哪

| 位置 | 內容 |
|---|---|
| Drive `agora/sessions/<ULID>/` | `session.md`（header＋閱讀版）、`raw-<md5>.json`（原始匯出） |
| `~/.config/agora/` | `rclone.conf`、`config.json` |
| `~/.cache/agora/` | 鏡像與搜尋索引，刪掉也會重建 |
| `~/.local/state/agora/` | `outbox/`（還沒上傳成功的）、`pending/`（接續中的）、`*/.bad/`（讀不了的壞檔，每個指令都會提示）——**不要刪** |

## 測試

```bash
uv run pytest -q                                   # 單元測試：不碰 Drive、不跑真的 agent
# 整合測試：真的 Drive（只用 agora-test/，AGORA_FOLDER_NAME=agora-test）、真的 opencode 與
# claude（自編短對話，claude -p 約 6 次），要跑好幾分鐘，所以丟到背景：
nohup uv run pytest -q -m integration tests/integration > it.log 2>&1 &
```
