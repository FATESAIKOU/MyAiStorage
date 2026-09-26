# spike/：技術驗證

這裡是 AiStorage 期 1 技術驗證（`openspec/changes/establish-aistorage-phase1/tasks.md` 第 1 組）用的一次性程式與環境，留作證據。驗證通過與否由使用者判定；`docs/spike/` 是給使用者的憑證準備說明。

## 目錄

| 路徑 | 內容 |
|---|---|
| `env/Dockerfile` | 技術驗證用的 Ubuntu 24.04 容器 image（git、git-annex、rclone、opencode、sqlite3、jq、python3） |
| `env/build.sh` | build image（預設 colima 的架構；`ARCH=amd64` 可建 amd64 版） |
| `env/run.sh` | 啟動容器：只掛白名單秘密（唯讀）與該容器專屬的工作目錄 |
| `env/verify.sh` | 驗證以上環境並產出證據到 `evidence/env-<arch>.txt`（含能力邊界的五項前置檢查） |
| `workflows/` | 推到測試 repo `FATESAIKOU/aistorage-spike` 的 spike workflow 來源（1.2 amd64、1.8）|
| `probe/` | 依 file id 操作的 Drive API 探測腳本（1.4 用） |
| `evidence/` | 任務 A（容器環境）的驗證輸出；**其他實測證據一律放 `docs/spike/evidence/`** |

## 怎麼用

```bash
# 1. build（在 Mac 上；colima 要先跑）
spike/env/build.sh

# 2. 進到容器（不掛任何秘密）
spike/env/run.sh s1

# 3. 只掛指定的秘密，並執行指令
spike/env/run.sh s1 rclone-mac-opencode.conf -- bash -lc 'rclone version'

# 4. 跑環境驗證、產生證據
spike/env/verify.sh
```

### `run.sh` 的規則

- `<容器名>` 決定 docker `--name` 與工作目錄；容器名只允許 `[A-Za-z0-9_.-]`。
- 只用**檔名**引用 `~/.config/aistorage-spike/` 下的秘密（可多個）；沒有列出的檔案不會進到容器。
- 秘密一律**唯讀**掛到 `/secrets/`；工作目錄掛到 `/work`（預設 `~/.local/share/aistorage-spike/work/<容器名>/`，每個容器各自一個）。
- 不掛任何其他 home 目錄；容器內 `$HOME=/work`、以 Mac 的 `uid:gid` 執行。
- 容器名重複、檔名含路徑、秘密檔權限過寬（group/other 可讀）都會被拒絕。

環境變數（特殊情況才用）：

| 變數 | 預設 | 用途 |
|---|---|---|
| `AISTORAGE_SPIKE_SECRETS` | `~/.config/aistorage-spike` | 秘密目錄 |
| `AISTORAGE_SPIKE_WORK_ROOT` | `~/.local/share/aistorage-spike/work` | 工作目錄根 |
| `AISTORAGE_SPIKE_IMAGE` | `aistorage-spike-env:latest` | image 名稱 |
| `AISTORAGE_SPIKE_USER` | Mac 的 `uid:gid` | 容器內執行的使用者 |
| `AISTORAGE_SPIKE_ALLOW_LOOSE_PERMS=1` | 未設 | 略過秘密檔權限檢查 |

## 工具的安裝方式與版本（1.2 要跟 amd64 的 Actions runner 比對）

全都從官方來源下載、以 sha256 鎖定版本（`Dockerfile` 的 ARG）；apt 只裝系統工具。

| 工具 | 版本 | 安裝方式 |
|---|---|---|
| Ubuntu | 24.04 | 基底 image |
| git | 2.43.0 | apt（Ubuntu 24.04） |
| **git-annex** | **10.20260717** | **官方 standalone tarball**（`downloads.kitenet.net/git-annex/linux/current/`，arm64/amd64 各一包） |
| rclone | 1.75.1 | 官方 zip（`downloads.rclone.org`） |
| opencode | 1.18.32 | GitHub release（`anomalyco/opencode`，`opencode-linux-{arm64,x64}.tar.gz`） |
| sqlite3 | 3.45.1 | apt（Ubuntu 24.04，`3.45.1-1ubuntu2.8`；FTS5 含 trigram） |
| jq | 1.7（套件 `1.7.1-3ubuntu0.24.04.2`） | apt |
| python3 | 3.12.3 | apt |

**為什麼 git-annex 不用 apt：** Ubuntu 24.04 的 `git-annex` 是 10.20240129，**沒有附 `git-remote-annex`**（實測 `command -v git-remote-annex` 找不到），而那是 Agora / Foundry 存進 Drive 的關鍵。standalone tarball 有完整的 `git-remote-annex`。tarball 的 `runshell` 會被改成 `GIT_ANNEX_PACKAGE_INSTALL=1`（image build 時以 `sed` 改並檢查），這樣它不會在使用者 home 裡裝 ssh 的 shim。

## 環境的坑（驗證時發現，已處理或已記錄）

1. **colima 的 sshfs 掛載 + root：** git-annex 需要保留檔案 ownership（chown），root 在 sshfs 上做不到，push 會失敗（`failed to preserve ownership` / `Failed to upload manifest.`）。`run.sh` 因此預設以 Mac 的 `uid:gid` 執行容器。
2. **git 的 dubious ownership：** 容器內 uid 與掛載檔案的 uid 不同，image 內以 `git config --system --add safe.directory '*'` 解掉。
3. **`git clone annex::` 需要 git 身分：** git-remote-annex 內部會跑 `git commit-tree`，沒有 `user.name` / `user.email` 會失敗。image 內已設預設身分。
4. **shorthand URL 不含 complete URL：** `git clone annex::` 印出的 full URL 對 directory remote 少 `directory=`，clone 時要自己帶完整參數（`annex::<uuid>?...&directory=...`）。這是 remote 設定的呈現方式問題，不是環境問題。
5. **amd64 只在 colima 的 qemu 上測：** amd64 image 建得起來、rclone / opencode / sqlite3 / jq / python3 / git 都能跑，但 bundled git-annex 在 qemu 下 segfault（arm64 原生與 apt 的 amd64 git-annex 都正常，研判是 qemu 對 Haskell runtime 的模擬問題）。1.2 要在真正的 amd64 runner（GitHub Actions）上確認。

## 能力邊界（`evidence/env-*.txt` 第 9 節）

`run.sh` 的容器預設：非 privileged、沒有 docker.sock、CapEff 為 0（只用得到 unprivileged 的一般能力）、env 只含 image 與 docker 注入的變數、`/proc/mounts` 只看得到 `/work` 與被列出的 `/secrets/*`。這是 1.7 / 5.1 判「容器內拿不到白名單以外憑證」的起點；完整判定仍依 `docs/spike/test-plan.md` 第一節最後一條。
