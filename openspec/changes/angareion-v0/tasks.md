## 1. 移植與改名（負責：agy，各自的 worktree）

- [x] 1.1 從 branch `aicompany-p1` 移植可以沿用的：`src/angareion/messages.py`（訊息模型）、`github.py`（HTTP transport、ETag、分頁、`mask_token`）、`config.py` 的 token 讀取（只限擁有者權限）；拿掉 ack、`RateLimiter`、`rebuild_from_backend`、`a2a` 指令
- [x] 1.2 改名：console script `angareion`、環境變數 `ANGAREION_CONFIG`／`ANGAREION_STATE_DIR`／`ANGAREION_TOKEN_FILE`、設定與狀態目錄 `angareion`；`tests/conftest.py` 把 `ANGAREION_*` 指到暫存目錄

## 2. 四個介面

- [x] 2.1 `ChannelBackend`（create_channel／send／check／read）與記憶體假後端；`GitHubIssuesBackend`（channel＝issue、訊息＝留言、「寄給我的」用 repo 層留言列表、`check` 用 ETag 條件式請求）
- [x] 2.2 函式庫層的 `wait`（在 `check` 上輪詢：timeout 0 呼叫一次，否則 15 秒間隔直到有新的或逾時）與 `read`（游標）；游標檔（D4）
- [x] 2.3 四個子命令與結束碼（D1）；stdout／stderr 分離
- [x] 2.4 測試：spec 的每個 Scenario（假後端、假 transport、不連網）；token 不出現在任何輸出；每項在副本改壞確認會紅

## 3. 文件

- [x] 3.1 `docs/angareion.md`：格式、四個指令、身分、token 的放置與輪替（只讀 `ANGAREION_TOKEN_FILE`；worker 上由 MyLinuxPool 的 Angareion 能力設定，檔名照池的規則）、本人的 smoke test（建 private 信道 repo、建 PAT、兩邊互送、wait、read）
- [x] 3.2 `skills/angareion/SKILL.md`（D6）；確認 opencode 讀說明檔的位置，寫進 `docs/angareion.md`（opencode 位置未確認，docs 留 TODO 給 PM）

## 3.3 審查修正（第一批：和游標無關）

- [x] M2 `DefaultHttpTransport` 加 30 秒 socket 逾時；逾時轉 `BackendError`（exit 2）
- [x] L1 後端非 JSON 回應轉 `BackendError`（exit 2）；CLI 不再接一般 `ValueError`
- [x] L2 `read` 先輸出（flush 成功）再存游標；`BrokenPipeError` 時游標不前進
- [x] L3 Link 的下一頁網址須以 `base_url` 開頭，否則後端錯誤；不跟隨轉址
- [x] L5 `send` 只接受 `--body TEXT` 或 `--body -`；沒給 `--body` exit 1（更新 docs 與 SKILL）
- [x] L6 游標暫存檔用 `tempfile` 同目錄唯一檔名再 `os.replace`
- [ ] L4 同名 channel 最舊者勝出；`create_channel` 建完再查一次（競態）
- [ ] L7 YAML 解析成 datetime/date 的 `at` 轉回 ISO 字串再驗證
- [ ] 補漏抓測試：token 讀取順序；請求 headers（Authorization／Accept／X-GitHub-Api-Version）
- [ ] 文件：opencode 讀 skill 位置的 spike 結論；SKILL.md 補 exit 2 與回覆位址兩行

## 4. 收尾

- [ ] 4.1 完整 unit 綠；非作者審查（Muse Spark 1.3 xhigh）
- [ ] 4.2 開 PR（b，PMO 先跟本人說明再 merge）；本人 smoke test 驗收
