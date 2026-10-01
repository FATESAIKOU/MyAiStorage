# Agora lite

找、合、接 coding agent 的 Session。Session 以一般檔案存在 Google Drive，opencode 與 Claude Code 都能接著做。設計見 [docs/design.md](docs/design.md)。

## 安裝

```bash
uv tool install --editable .     # 之後就有 agora 指令
```

需要：
- `rclone`；
- `~/.config/agora/rclone.conf`：remote 名稱是 `gdrive`，用 worker OAuth client、scope 是 `drive.file`；
- `opencode`、`claude`（用到哪個裝哪個）。

第一次執行時，agora 會在 Drive 建一個 `agora/` 資料夾，並把它的 folder ID 記到 `~/.config/agora/config.json`。

## 指令

```bash
# 初次引入（只上傳指定的那一個 Session）
agora import --format opencode --session-id ses_xxxx --header 'title=CSV 規劃' --header '先做讀取'
agora import --format claude   --session-id 0f1e…-uuid

# 找
agora search session '表格'
agora search session 'CSV' --header agent=claude
agora search session                 # 省略關鍵字＝列出全部
agora search session 'CSV' --no-sync # 不連 Drive，只查本機索引

# 合併成一個新的 Session（空白或逗號分隔都可以）
agora merge-session agora:01K6…, agora:01K7…, agora:01K8…

# 接著做（agent 結束時自動存回，印出新的 agora id）
agora continue-session agora:01K6… --agent opencode
agora continue-session agora:01K8… --agent claude --dir ~/proj

# 其他
agora show agora:01K6…        # header ＋ 閱讀版
agora show agora:01K6… --raw  # 原始匯出
agora sync                     # 推 outbox、拉 Drive、重建索引
```

`--header` 的寫法：
- `key=value`，可以設定 `title`、`case`、`refs`、`tags`、`note`。
- 不含 `=` 的文字整段當成 note。
- `search` 的 `--header` 可以用 `agent`、`relation`、`case`、`tag`、`ref`、`title`。

**exit code：**

| code | 意思 |
|---|---|
| 0 | 成功 |
| 1 | 做不到：找不到 id、沒有訊息可匯入、merge 少於兩個 |
| 2 | 錯誤：header、Drive、agent 或非預期的錯誤，以及指令用法錯誤 |
| 3 | 已存在本機 outbox，還沒上傳到 Drive（下次 `sync` 會再送） |

**壓縮：** 用 agent 內建的指令，opencode 是 `/compact`，Claude Code 是 `/compact`。壓縮後的內容會在 agent 結束時存回 Agora。

## 檔案放哪

| 位置 | 內容 |
|---|---|
| Drive `agora/sessions/<ULID>/` | `session.md`（header＋閱讀版）、`raw-<md5>.json`（原始匯出） |
| `~/.config/agora/` | `rclone.conf`、`config.json` |
| `~/.cache/agora/` | 鏡像與搜尋索引，刪掉也會重建 |
| `~/.local/state/agora/` | `outbox/`（還沒上傳成功的）、`pending/`（接續中的）、`*/.bad/`（讀不了的壞檔，每個指令都會提示）、`reading/`（注入用的閱讀版，接續結束就刪）——**不要刪** |

## 測試

```bash
uv run pytest -q                                   # 單元測試：不碰 Drive、不跑真的 agent
# 整合測試：真的 Drive（只用 agora-test/，AGORA_FOLDER_NAME=agora-test）、真的 opencode 與
# claude（自編短對話，claude -p 約 6 次），要跑好幾分鐘，所以丟到背景：
nohup uv run pytest -q -m integration tests/integration > it.log 2>&1 &
```
