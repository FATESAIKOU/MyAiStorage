## Context

v0（`openspec/specs/angareion/spec.md`）的介面帶著 GitHub 的形狀：`channel create --member`、位址 `{repo, role, name?}`、channel 名直接對應 issue 標題。本人 v5 決定：channel 是通訊軟體式的英文名，介面不再出現 repo／issue；收件人用共通 agent 命名。見 proposal.md 的 Why；動機不再重述。

## Goals / Non-Goals

**Goals：**
- 介面（參數、stdout、stderr、訊息格式）去 GitHub 化；`--member` 拿掉；`send --to` 選填。
- channel／backend 兩種設定檔進 MyAiStorage repo，走 PR 更新、clone＋pull 讀取。
- 列出 `docs/angareion.md` 與 `skills/angareion/SKILL.md` 的改寫點（本 change 不改那兩個檔）。

**Non-Goals：**
- 實作（只出 planning artifacts）。
- 嚴格的身分認證（v5 明說不做）。
- 群組位址（新模型裡沒有群組；`group=` 形狀視為格式錯誤）。
- 後端實作的更換（仍是 GitHub Issues，只是經設定檔指位）。

## Decisions

### 1. 位址＝agent 名字串
`from`、`to`、個人設定檔的 `identity` 一律是 `<repo 名>-<pm|arch|impl|test>[-<id>]`（例 `MyLinuxPool-impl-2`、`LLMGateway-pm`）。`messages.validate_address` 改為只認這個形狀；`cli.parse_address` 不再解析 `repo=`／`×`／`:` 形狀（也不再有 JSON 位址）；游標檔名（`identity_slug`）與顯示格式（`<repo×role>` → `<agent 名>`）跟著換。替代方案（保留舊形狀、另加對應表）被否決：v5 要求介面不出現 repo／issue 概念。

### 2. 無 `to`＝廣播，算進「寄給我的」
`send` 不給 `--to` 時訊息沒有 `to` 欄（front matter 省略）；`wait`／`read` 不給 `--channel` 時，「寄給我的」＝`to` 是自己 agent 名 **或沒有 `to`** 的訊息。理由：第一期 channel 全公開，廣播就是給所有隊員的；等信的 agent 不該漏掉它。替代方案（廣播只用 `--channel` 才看得到）見 Open Questions。

### 3. `--member` 整串拿掉
`channel create` 不再收 `--member`（未知參數 exit 1）；`ChannelBackend.create_channel` 簽名去掉 members；GitHub 後端不再寫／讀 issue 本文的 members（舊 issue 殘留的 members 欄讀取時忽略）；假後端同理。

### 4. channel 名只收英文名
`^[A-Za-z0-9_-]+$`（非空）；不合就 exit 1。`channel create` 的 stdout 只印名（編號內部化；「建 channel」情境已同步）。

### 5. 設定檔：位置、格式、讀寫分工
- 位置：`config/angareion/channels.json`、`config/angareion/backends.json`（新目錄；確切位置見 Open Questions）。
- 格式：JSON，`{"version": 1, "channels": {<名>: {"backend": <名>, "issue": <編號>}}, …}`；backend 檔 `{"version": 1, "backends": {<名>: {"type": "github", "repo": "owner/repo"}}}`。channel 條目不重寫 repo（repo 在 backend 設定裡）。
- 更新：對 MyAiStorage 開 PR，AiStorage PM 審查＋merge（d 等級；頻率約每個 a/b 專案一次）。
- 讀取：部署端 clone＋外部排程定期 pull（cron／systemd，建議至少每小時一次）；angareion 只讀本地複本，缺失或壞 JSON 就 exit 2。pull 失敗的處理是排程端的事：沿用上次可用的複本（angareion 不管）。
- 查無 channel 名一律視為 channel 不存在（exit 1），不再列舉後端。

### 6. v0→v5 的歷史訊息
讀寫都只認新形狀；舊 `{repo,role}` 形狀的留言視為非訊息忽略（與未知版本同理）。切換日前已讀過的不受影響（游標是後端位置）；要回頭看很久以前的人用 `--since` 會看到舊留言被跳過。切換日由各專案第一次設定檔 PR 決定。

### 7. 文件改寫點（本 change 不動這兩個檔）
`docs/angareion.md`：四個指令表（create 去 `--member`、send 的 `--to` 選填）、位址寫法整節、訊息格式範例（`from`／`to` 改字串、`to` 可省）、游標檔名規則、設定與 token 節（`channel_repo` 改為兩種設定檔＋clone／pull 流程）、smoke test 步驟、`--member` 字樣全清。
`skills/angareion/SKILL.md`：四個指令表、位址寫法（`repo=…,role=…` → agent 名）、「不給 `--channel`」的定義加廣播、回覆時 `channel`＋`--to` 的寫法範例。

## Risks / Trade-offs

- [設定檔是單點] → 複本缺失或壞 JSON 會讓全部指令 exit 2；緩解：排程 pull 沿用舊複本、PR 審查擋格式錯。
- [channel 改名要動 backend 實體] → 改名＝新 channel（舊 issue 留著）；緩解：設定檔 PR 裡同時說明，不做重新命名指令。
- [舊同名 issue] → 仍以後端最舊的為準（既有行為保留，介面不提編號）。
- [游標檔名改變] → 舊游標檔（`{repo}__{role}` 形）自然廢棄，新身分從頭讀一次；緩解：切換日時清掉或留著都無害（只佔一點空間）。
- [廣播進「寄給我的」] → 不給 `--channel` 的 `wait` 會被全頻道的廣播喚醒；緩解：v5 第一期本來就全公開，可接受（見 Open Questions）。

## Open Questions

1. `channel create` 留不留成指令（本人決定）：留＝保留「確保 backend 實體存在」的功能（不碰設定檔）；刪＝channel 只由設定檔 PR 建立，`channel` 子命令整組拿掉。spec 暫維持現狀。
2. 設定檔確切位置（本人決定）：提案 `config/angareion/*.json`；替代：repo 根 `angareion-channels.json` 等。
3. pull 週期與失敗處理（PM／部署決定）：提案至少每小時一次、失敗沿用舊複本；替代：angareion 每次執行自帶新鮮度檢查。
4. 廣播是否算「寄給我的」（本人確認）：提案含（Decision 2）；替代：只有指名才算，廣播只用 `--channel` 看。
