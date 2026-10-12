## 1. 設定檔（先做；格式定下來後面才能動）

- [ ] 1.1 channel／backend 設定檔的 JSON schema（含 `version`）與範例檔
- [ ] 1.2 本地複本的讀取、缺失／壞 JSON 的 exit 2，與單元測試
- [ ] 1.3 MyAiStorage repo 建 `config/angareion/`（或本人定的位置）並放第一版兩個檔（d 等級 PR）

## 2. 位址與訊息格式

- [ ] 2.1 `messages.validate_address` 改認 agent 名；`parse_message`／`serialize_message` 的 `to` 選填
- [ ] 2.2 `cli.parse_address` 拿掉 `repo=`／`×`／`:`／JSON 位址；`--to` 選填、`--member` 拿掉
- [ ] 2.3 `state.identity_slug`、顯示格式改 agent 名；舊游標檔自然廢棄的測試

## 3. 後端與指令

- [ ] 3.1 `ChannelBackend.create_channel` 去 members；GitHub 後端經設定檔解析 channel（查無→`ChannelNotFoundError`）、不再寫／讀 members
- [ ] 3.2 `send` 無 `to`＝廣播；`wait`／`read` 的「寄給我的」含廣播（design Decision 2）
- [ ] 3.3 假後端同步；`channel create` 照本人決定留（輸出只印名）或刪
- [ ] 3.4 個人設定檔的 `identity` 改 agent 名、`channel_repo` 改為設定檔複本位置；`docs/angareion.md` 與 `skills/angareion/SKILL.md` 按 design §7 改寫

## 4. 測試與收尾

- [ ] 4.1 spec 每個 Scenario 的單元測試（假後端；`channel create` 留刪兩案各一測，先寫留案）
- [ ] 4.2 每項在副本改壞確認會紅；完整 unit 綠
- [ ] 4.3 非作者審查後開 PR
