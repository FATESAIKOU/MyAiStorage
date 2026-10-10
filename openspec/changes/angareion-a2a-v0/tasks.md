## 1. 訊息模型與後端介面

- [ ] 1.1 建 `src/angareion/` 套件與 `a2a` console script（`pyproject.toml` 的 packages 與 `[project.scripts]`），`a2a --help` 可用。
- [ ] 1.2 六欄位訊息模型：`from`／`to`／`channel`／`urgency`／`content`／`attachments`；front matter 序列化與解析、位址與附件驗證（`ref` 或 `inline` 恰好一個）、長度計算。單元測試：往返、缺欄位、內文含 `---`、群組位址、附件上限。
- [ ] 1.2a 設定與路徑：`~/.config/angareion/config.json`（`channel_repo`、`identity`、`token_file`）、`A2A_CONFIG`／`A2A_STATE_DIR`／`A2A_TOKEN_FILE` 覆寫；`~/.local/state/angareion/` 狀態目錄。
- [ ] 1.3 後端介面（Protocol：`create_channel`／`find_channel`／`post_message`／`list_messages(since, etag)`）與假後端（記憶體實作），給測試與 sidecar 的開發用。
- [ ] 1.4 GitHub 後端：stdlib HTTP client（transport 可注入假貨）、條件式請求與 ETag、分頁、repo 層 comments 端點、建立 issue、`--new` 檢查。單元測試：304、403＋retry-after、401、`id` 防重複。
- [ ] 1.5 本機狀態：`since`、`etags`、`acked`、`channels`；刪掉之後能從信道重建；單元測試。
- [ ] 1.6 token 讀取（`A2A_TOKEN_FILE` 或 `~/.config/angareion/token`，0600）；輸出與例外 MUST NOT 含 token；測試掃 stdout／stderr／log。

## 2. 指令

- [ ] 2.1 `a2a send`：`--to`／`--channel`／`--new`／`--urgency`／`--from`／`--id`／`--attach`／`--ref`／`--body`；stdout 只印 `id` 與 channel；重複 `id` 檢查；速率節流（80 次／分、500 次／時）與等待重試。
- [ ] 2.2 `a2a inbox`：未 ack、依 `at` 再由舊到新、`--json`、`--all`、`--identity`；游標與 ETag；沒有新訊息 exit 0。
- [ ] 2.3 `a2a ack`：多則、`--note`、已 ack 不重貼。
- [ ] 2.4 CLI 層測試：三個子指令的 exit code 0／1／2 與 stdout／stderr 分離；錯誤訊息不含秘密。

## 3. 文件與部署設定

- [ ] 3.1 `deploy/angareion/config.example.json`（不含秘密）與 `docs/angareion.md`：六欄位格式與範例、channel 與 issue 的對應、token 建立與輪替、狀態檔位置、群組位址在 v0 的行為。
- [ ] 3.2 本人的 smoke test 清單（建 private 信道 repo、設 PAT、兩台互送與 ack）；標明只有本人執行、隊員不碰。

## 4. 收尾

- [ ] 4.1 `uv run python -m compileall -q src` 與完整 unit 綠；conftest 把 `A2A_*` 指到 tmp，測試不連網、不碰真信道。
- [ ] 4.2 review（arch）與本人驗收；歸檔時把 spec 併進 `openspec/specs/angareion/`。
