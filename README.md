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

# 合併成一個新的 Session
agora merge-session agora:01K6… agora:01K7…

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

**壓縮：** 用 agent 內建的指令，opencode 是 `/compact`，Claude Code 是 `/compact`。壓縮後的內容會在 agent 結束時存回 Agora。

## 檔案放哪

| 位置 | 內容 |
|---|---|
| Drive `agora/sessions/<ULID>/` | `session.md`（header＋閱讀版）、`raw-<md5>.json`（原始匯出） |
| `~/.config/agora/` | `rclone.conf`、`config.json` |
| `~/.cache/agora/` | 鏡像與搜尋索引，刪掉也會重建 |
| `~/.local/state/agora/` | outbox（還沒上傳成功的）與 pending（接續中的）——**不要刪** |

## 測試

```bash
uv run pytest -q                                   # 單元測試：不碰 Drive、不跑真的 agent
uv run pytest -q -m integration tests/integration  # 整合測試：只用 Drive 的 agora-test/、自編短對話
```
