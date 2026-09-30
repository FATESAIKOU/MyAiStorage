# 技術驗證的憑證準備（2026-09-26 實際執行紀錄）

技術驗證（tasks 1.1〜1.8）用的憑證。**秘密的值只存在 Mac 的 `~/.config/aistorage-spike/`（權限 600），不進 repo、prompt、log 或報告。** 隊員只以檔案路徑引用；非秘密的識別（project id、資料夾 id、SA email）在同目錄的 `ids.env`。資源清單見 `docs/resources.md`。

## 檔案

| 檔案 | 內容 | 用在 |
|---|---|---|
| `client-committer.json` | OAuth client（電腦版），project `aistorage-spike-2-260926` | 提交流程 |
| `client-mac-opencode.json` | 同上 | Mac opencode profile |
| `client-test-profile.json` | 同上 | 測試用 profile |
| `client-other-project.json` | OAuth client（電腦版），project `aistorage-spike-1-260926` | 1.4 不同 project 的隔離 |
| `rclone-committer.conf` | rclone remote `gdrive`，scope `drive`（完整） | 提交流程；也放在測試 repo 的 Actions secret `RCLONE_CONF` |
| `rclone-mac-opencode.conf` | rclone remote `gdrive`，scope `drive.file` | Mac opencode profile 的收件匣 |
| `rclone-test-profile.conf` | rclone remote `gdrive`，scope `drive.file` | 測試用 profile |
| `rclone-other-project.conf` | rclone remote `gdrive`，scope `drive.file` | 1.4 |
| `sa-reader.json` | service account `spike-reader` 的金鑰 | 讀取身分（1.5） |
| `gh-pat-actions.txt` | fine-grained token，只有 `aistorage-spike` 的 Actions: Read and write，30 天 | 1.6、1.8 |
| `ollama-cloud-key.txt` | ollama-cloud API key | 容器裡的 opencode（1.7） |
| `ids.env` | 非秘密的 id | 全部 |

## 建立步驟（誰做的）

1. **使用者**：專用帳號登入 gcloud（另開 configuration `aistorage-spike`，不動原本的 default）；在 Cloud Console 同意服務條款。
2. **PM（gcloud）**：建 project `aistorage-spike-1-260926`、`aistorage-spike-2-260926`，兩邊啟用 Drive API；在 project 1 建 service account `spike-reader` 與 JSON 金鑰。剛建好的 SA 要等幾秒才能建金鑰（第一次 NOT_FOUND，重試成功）。
3. **使用者（Console，只能在網頁上做）**：兩個 project 的 Google Auth Platform 都做「開始」→「品牌」→「發布應用程式」。發布成**正式版**前，「品牌」頁必須填應用程式名稱、支援信箱、首頁網址、隱私權政策網址（填了測試 repo 的 GitHub 網址即可，不送驗證）。接著建電腦版 OAuth client 並下載 JSON：`committer`、`mac-opencode`、`test-profile` 建在 project 2，`other-project` 建在 project 1。用戶端密碼只有建立當下能下載。
4. **PM（rclone）**：用四個 client 各建一份 rclone 設定（`rclone config create … --config <檔案> >/dev/null`，不在畫面上印出內容）；**使用者**在跳出的瀏覽器用專用帳號授權（未驗證的應用程式要按「進階」→「前往」）。
5. **PM（rclone、Drive API）**：以 committer 建資料夾 `aistorage-spike`，用 Drive API 把它分享給 `spike-reader`（reader、不寄通知）。
6. **PM（gh）**：建 private repo `FATESAIKOU/aistorage-spike`，把 `rclone-committer.conf` 放進 Actions secret `RCLONE_CONF`。
7. **使用者（GitHub 網頁）**：建 fine-grained token；複製 ollama-cloud key。兩者由 PM 搬進 `~/.config/aistorage-spike/`，確認 token 對 `actions/workflows` 回 HTTP 200。

## 注意

- ⚠️ 不要執行 `rclone config show` 或任何會印出設定檔內容的指令。
- zsh 不會把字串變數拆成多個參數（`C="--config x"; rclone ls $C` 會失敗），指定設定檔請用 `RCLONE_CONFIG=<檔案>` 環境變數或直接寫 `--config <檔案>`。
- 驗證結束後，這些憑證依 `docs/resources.md` 撤銷或輪替，不沿用到正式環境。

## 追加（review-4 H6）

- **PM**：`rclone-committer.conf` 補上 `root_folder_id = SPIKE_FOLDER_ID`（`rclone config update gdrive root_folder_id=… --config …`），再重新寫入 Actions secret `RCLONE_CONF`。確認 `rclone lsf gdrive:` 列出的是 `aistorage-spike` 資料夾的內容（當時是空的）。之後 committer 的路徑一律相對於 `aistorage-spike`；`drive.file` client 建在根目錄的收件匣，以資料夾 id 存取。
