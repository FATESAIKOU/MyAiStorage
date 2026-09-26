# env-check 缺口處理（G1、G3、G4、G7）

- 執行時間：2026-09-26 07:38〜07:46 UTC
- 執行者：impl1
- 來源：`docs/spike/evidence/env-check.md`（test 的核對報告）第三節缺口清單

## G3 image 補 git-filter-repo（1.3 要用）

- `spike/env/Dockerfile` 增加 git-filter-repo v2.47.0（上游單檔，`raw.githubusercontent.com/newren/git-filter-repo/v2.47.0`，sha256 `67447413e273fc76809289111748870b6f6072f08b17efe94863a92d810b7d94`，build 時 `sha256sum -c` 驗證）。
- 重新 build arm64 image 後實測：

```
$ docker run --rm aistorage-spike-env:latest bash -lc 'command -v git-filter-repo && git filter-repo --version && python3 --version'
/usr/local/bin/git-filter-repo
a40bce548d2c
Python 3.12.3
```

→ **G3 解決**（image 內建，執行時不需網路、不需 pip）。版本：`--version` 顯示 `a40bce548d2c`（v2.47.0 的內建 revision）。amd64 image 需重建才會帶到（1.2 的 runner 不經 image，用 Dockerfile 相同版本安裝；1.3 在 arm64 容器執行）。

## G4 SA 用的 rclone conf

- 新增 `~/.config/aistorage-spike/rclone-sa-reader.conf`（600 權限），內容為：
  - `type = drive`
  - `scope = drive`（H5：完整 scope，證明的是 Drive ACL 只有 reader，不是 token scope）
  - `service_account_file` 指向 `~/.config/aistorage-spike/sa-reader.json`
  - `root_folder_id = SPIKE_FOLDER_ID`（H6）
- 功能測試（不印出任何 conf 內容）：

```
$ RCLONE_CONFIG=~/.config/aistorage-spike/rclone-sa-reader.conf rclone lsf gdrive:
committer-owned.txt
inbox-host/
rc=0
$ RCLONE_CONFIG=... rclone about gdrive:
Used:    0 B
rc=0
```

→ **G4 解決**：SA 以 reader 身分看得到 `aistorage-spike/` 的內容（當時有 committer 建的 `committer-owned.txt`、`inbox-host/`）。寫入／刪除的負向驗證屬 1.5。

## G1 spike workflow 進測試 repo `FATESAIKOU/aistorage-spike`（不是本 repo）

- 檔案來源保留在 `spike/workflows/`（本 repo，證據用）；以 `gh` 認證的 git 推到 `FATESAIKOU/aistorage-spike`：
  - `.github/workflows/spike-git-annex.yml`（1.2 amd64 半邊、1.8 runner 量測）：amd64 安裝 git-annex standalone tarball（**與 `spike/env/Dockerfile` 相同版本與 sha256**）＋ rclone 1.75.1（同 sha256）；clone 與 `annex get` 分開計時；`commit-push` 選項可量 push；repo URL 由 variable `SPIKE_REPO_URL` 提供（1.2 建 repo 後設定）；conf 由 secret `RCLONE_CONF` 寫入、不印出。
  - `.github/workflows/spike-empty-check.yml`（1.8 空收件匣）：以資料夾 id（variable `SPIKE_INBOX_ID`）列舉收件匣，**在安裝任何工具、clone 任何 repo 之前**判斷；空的就結束。
  - `spike/inbox_probe.py`：純 Python 標準庫實作（只讀 conf 到記憶體換 token，輸出只有檔數與檔名）。
- 設定與驗證：

```
$ gh variable set SPIKE_INBOX_ID --repo FATESAIKOU/aistorage-spike --body "173O-NBiv3iFygsV4IDQj7Zjcba5A4_K7"
$ gh workflow list --repo FATESAIKOU/aistorage-spike
spike-empty-check	active	367559034
spike-git-annex	active	367559035
```

- 空收件匣實測（手動觸發）：

```
$ gh workflow run spike-empty-check --repo FATESAIKOU/aistorage-spike
$ gh run view 36227666548 --json jobs
Set up job: success
Checkout（只為了拿到 inbox_probe.py，不 clone Drive repo）: success
計時起點: success
以資料夾 id 列出收件匣（純 Python，不安裝工具）: success
空收件匣 → 不 clone、不裝工具: success
```

- 時間：run 建立 07:43:52Z、更新 07:44:05Z，job 步驟 07:43:57〜07:44:00Z（空跑約 1 計費分鐘以內）。
- **過程留痕（誠實記錄）**：第一次觸發（run `36227630906`）FAIL——workflow 忘了 `actions/checkout`，`spike/inbox_probe.py` 不存在。補上 checkout 步驟後 run `36227666548` 成功；失敗與修正都保留在 repo 的 run 歷史。
- `spike-empty-check` 另帶 `schedule: '0 */6 * * *'`（1.8 要觀察 schedule 的實際間隔與延遲；不需要時可 `gh workflow disable`）。**注意：schedule 會持續產生 run**，1.8 完成後應停用（記在這裡避免忘記）。

## G7 收件匣建好（1.8 前置）

- 依 D3「收件匣由該 profile 自己的 client 建立」：用 `mac-opencode`（A）的 conf 建 `aistorage-spike-inbox-mac-opencode`（My Drive 根目錄、前綴 `aistorage-spike-`）：

```
$ rclone mkdir gdrive:aistorage-spike-inbox-mac-opencode --config <A conf>   # rc=0
$ rclone lsjson gdrive: --dirs-only ... | jq
{"Name":"aistorage-spike-inbox-mac-opencode","ID":"173O-NBiv3iFygsV4IDQj7Zjcba5A4_K7"}
$ rclone lsf gdrive:aistorage-spike-inbox-mac-opencode/ ...                  # 空
```

- 收件匣 id 已設為測試 repo 的 variable `SPIKE_INBOX_ID`（見 G1），供 1.8 以 id 列舉。
- **待辦（1.9 清理清單與 resources.md）**：`aistorage-spike-inbox-mac-opencode`（id `173O-NBiv3iFygsV4IDQj7Zjcba5A4_K7`）。

## 與 G5 有關的說明

環境證據（`spike/evidence/env-arm64.txt`、`env-amd64.txt`）依 PM 指示留在 `spike/evidence/`，已在 `spike/README.md` 註明；本檔與今後的實測證據一律放 `docs/spike/evidence/`。
