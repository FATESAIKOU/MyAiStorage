## 1. 設定檔與讀取（先做；格式定下來後面才能動）

- [ ] 1.1 `config/angareion/channels.json`／`backends.json` 的 JSON schema（`version`）與範例檔；`config/angareion/` 進 repo（d 等級 PR）
- [ ] 1.2 本地複本的讀取（`config_dir`）、缺失／壞 JSON 的 exit 2；單元測試
- [ ] 1.3 讀取與更新：`git fetch`＋`git show origin/main:<路徑>`（不依賴工作樹）、`FETCH_HEAD` mtime 當新鮮度、`config_pull_interval`（預設 900 秒；0＝不按週期）、flock 鎖、逾時 20 秒、失敗沿用舊複本＋stderr 警告、`wait` 輪詢中不 fetch；查無 channel 先 fetch 一次（最短間隔 30 秒）、create 輪詢 fetch（上限 2 分鐘）；單元測試

## 2. 位址與訊息格式

- [ ] 2.1 `messages.validate_address` 改用 agent 名文法（`^[A-Za-z0-9][A-Za-z0-9._]*-(pm|arch|impl|test)(-[A-Za-z0-9_.]+)?$`、由右而左解析）；`parse_message`／`serialize_message` 的 `to` 選填
- [ ] 2.2 `cli.parse_address` 拿掉 `repo=`／`×`／`:`／JSON 位址；`--to` 選填、`--member` 拿掉
- [ ] 2.3 `state.identity_slug`、顯示格式改 agent 名；舊游標檔自然廢棄的測試

## 3. 後端與指令

- [ ] 3.1 `ChannelBackend` 去 members；GitHub 後端經兩個設定檔解析（查無→`ChannelNotFoundError`）；實體開（create）與關（delete，不刪）的動作；不再寫／讀 members（舊殘留忽略）
- [ ] 3.2 `channel create` 三步：後端開實體 → 送註冊（短間隔重試 2 次、最多 3 次嘗試）→ 輪詢 fetch 等對應出現（上限 2 分鐘）；同名 exit 1、開失敗 exit 2；`--backend`（多個 backend 時必給）（design §6）
- [ ] 3.3 `channel delete`：找對應 → 關實體（404 視同已關）→ 送註銷（design §7）
- [ ] 3.4 註冊／註銷請求的送出（依 design §10 的裁定；**待本人確認後才開工**）與假的註冊端（測試用）
- [ ] 3.5 `.github/workflows/register-channel.yml`／`unregister-channel.yml`＋更新腳本：授權（sender 名單；非授權不動檔）、輸入只經環境變數（不插 `${{ }}`；嚴格 JSON）、驗 backend／issue 存在且標題相符、drain（每次 run 掃未完成的請求）＋`schedule` sweep、concurrency group、sha 樂觀鎖重試（上限 5）、同名不覆蓋、完成記號（`angareion-wf: done`／`rejected`；確定後關閉請求 issue）、留言與關閉（design §9；腳本以可注入的檔案 API 單元測試）
- [ ] 3.6 `send` 無 `to`＝廣播；`wait`／`read` 的「寄給我的」含廣播（design §2）
- [ ] 3.7 假後端同步；`docs/angareion.md` 與 `skills/angareion/SKILL.md` 按 design §13 改寫

## 4. 測試與收尾

- [ ] 4.1 spec 每個 Scenario 的單元測試（假後端／假註冊端；workflow 腳本的併發與同名用可注入的檔案 API 測）
- [ ] 4.2 每項在副本改壞確認會紅；完整 unit 綠
- [ ] 4.3 非作者審查後開 PR
- [ ] 4.4 `angareion-v5` push 後開 PR：以 `angareion-v0` 為 base 堆疊（#41 已用 merge commit `95a7641` 合併、branch 保留）

## 5. 開工前要本人確認（design §10）

- [ ] 5.1 workflow 觸發方式：A `workflow_dispatch`／B `repository_dispatch`／C issue 事件（建議 C）
- [ ] 5.2 選 C 時：現有 token 的 repo 涵蓋 MyAiStorage（Issues: write）
- [ ] 5.3 MyAiStorage 的 main 允許 workflow 用 `GITHUB_TOKEN` 直推（分支保護／bypass）
- [ ] 5.4 選 A／B 時：新 token 與 MyLinuxPool 的 secret 佈署
- [ ] 5.5 選 C 時：註冊落點（M6）——MyAiStorage 的註冊 issue／registry repo／單一註冊簿 issue＋`issue_comment`
