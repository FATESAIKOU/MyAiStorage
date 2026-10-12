## 1. 設定檔與讀取（先做；格式定下來後面才能動）

- [x] 1.1 `config/angareion/channels.json`／`backends.json` 的 JSON schema（`version`）與範例檔；`config/angareion/` 進 repo（d 等級 PR）
- [x] 1.2 本地複本的讀取（`config_dir`）、缺失／壞 JSON 的 exit 2；單元測試
- [x] 1.3 讀取與更新：`git fetch`＋`git show origin/main:<路徑>`（不依賴工作樹）、`FETCH_HEAD` mtime 當新鮮度、`config_pull_interval`（預設 900 秒；0＝不按週期）、flock 鎖、逾時 20 秒、失敗沿用舊複本＋stderr 警告、`wait` 輪詢中不 fetch；查無 channel 先 fetch 一次（最短間隔 30 秒）、create 輪詢 fetch（上限 2 分鐘）；單元測試
- [x] 1.4 註冊簿 issue（MyAiStorage；label `層級:d`＋`狀態:進行中`、長期存在不關閉；置頂留言寫格式與規則）

## 2. 位址與訊息格式

- [x] 2.1 `messages.validate_address` 改用 agent 名文法（`^[A-Za-z0-9][A-Za-z0-9._-]*-(pm|arch|impl|test)(-[A-Za-z0-9_.]+)?$`；驗證與由右而左的解析是兩步）；`parse_message`／`serialize_message` 的 `to` 選填
- [x] 2.2 `cli.parse_address` 拿掉 `repo=`／`×`／`:`／JSON 位址；`--to` 選填、`--member` 拿掉
- [x] 2.3 `state.identity_slug`、顯示格式改 agent 名；舊游標檔自然廢棄的測試

## 3. 後端與指令

- [x] 3.1 `ChannelBackend` 去 members；GitHub 後端經兩個設定檔解析（查無→`ChannelNotFoundError`）；實體開（create）與關（delete，不刪）的動作；不再寫／讀 members（舊殘留忽略）
- [ ] 3.2 `channel create` 三步：後端開實體 → 用戶端驗實體（存在且標題等於 channel 名；不符 exit 2）→ 送註冊（在註冊簿留一行 JSON 留言；短間隔重試 2 次、最多 3 次嘗試）→ 輪詢 fetch 等對應出現（上限 2 分鐘）；同名 exit 1、開失敗 exit 2；`--backend`（多個 backend 時必給）（design §6）
- [ ] 3.3 `channel delete`：找對應 → 關實體（404 視同已關）→ 在註冊簿留 unregister 留言（design §7）
- [x] 3.4 留言送出（註冊簿；`Issues: Read and write`）與假的註冊端（測試用）
- [ ] 3.5 註冊簿重建 workflow（例 `.github/workflows/angareion-registry.yml`）＋更新腳本：`on: issue_comment`（`if` 擋 Bot）＋每天一次 `schedule`、授權（只採計 `OWNER`／`MEMBER`／`COLLABORATOR`；非授權不回報）、從整串留言重建（`per_page=100`、翻完所有頁）、只驗留言本身（格式、channel 名、backend 名；不查 backend 的 issue）、回報（純文字）、commit main（無變化不 commit；`permissions:` 至少 `contents: write`＋`issues: write`）、`concurrency`＋`cancel-in-progress`、sha 樂觀鎖重試（上限 5）（design §9／§10；腳本以可注入的檔案 API 單元測試）
- [ ] 3.6 `send` 無 `to`＝廣播；`wait`／`read` 的「寄給我的」含廣播（design §2）
- [ ] 3.7 假後端同步；`docs/angareion.md` 與 `skills/angareion/SKILL.md` 按 design §13 改寫

## 4. 測試與收尾

- [ ] 4.1 spec 每個 Scenario 的單元測試（假後端／假註冊端；workflow 腳本的重建、同名、取消後重建用可注入的檔案 API 測）
- [ ] 4.2 每項在副本改壞確認會紅；完整 unit 綠
- [ ] 4.3 非作者審查後開 PR
- [ ] 4.4 `angareion-v5` push 後開 PR：以 `angareion-v0` 為 base 堆疊（#41 已用 merge commit `95a7641` 合併、branch 保留）

## 5. 開工前確認（design §10）

- [ ] 5.1 workflow 授權：只採計 `author_association` 為 `OWNER`／`MEMBER`／`COLLABORATOR` 的留言；觸發層擋 Bot、非授權不回報（design §10）
- [ ] 5.2 信道 token 加選 `MyAiStorage`（`Issues: Read and write`；細粒度 PAT）
- [ ] 5.3 MyAiStorage 的 main 允許 workflow 用 `GITHUB_TOKEN` 直推（分支保護／bypass）
