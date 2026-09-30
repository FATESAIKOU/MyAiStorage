# 測試怎麼跑

三層：`unit/`（不碰外部）、`integration/`（真 Drive ＋ 真 pin-test repo）、
`e2e/`（真住民容器 ＋ 真提交流程）。

| 目錄 | 要什麼 | 怎麼跑 |
| --- | --- | --- |
| `tests/unit` | 只要 Python 依賴 | `uv run pytest tests/unit` |
| `tests/integration` | 真 Drive、真 git-annex、真 pin-test repo | `uv run python scripts/run_integration.py` |
| `tests/e2e` | 上面的 ＋ 住民 image ＋ e2e 環境 | `uv run python scripts/run_integration.py --include-e2e` |

`pyproject.toml` 的 `addopts = -m "not integration and not e2e"`，所以單純
`uv run pytest` 只會跑單元測試，不會因為缺設定而紅。

## 整合測試

### 需要的東西

`scripts/run_integration.py` 開跑前會做 preflight，四個檔缺任何一個、
`TEST_FOLDER_ID` 沒設、或外部程式找不到，就一口氣講清楚並以 exit 2 結束，
不會跑一半才失敗。

**憑證檔（值一律是路徑，內容不讀不印）**

| 檔案 | 預設位置 | 用途 |
| --- | --- | --- |
| `ids.env` | `~/.config/aistorage/ids.env` | 內含 `TEST_FOLDER_ID=`：Drive 上測試根資料夾的 id（不是秘密） |
| `rclone-committer-test.conf` | `~/.config/aistorage/rclone-committer-test.conf` | git-annex 透過 rclone 存取測試 Drive 的憑證 |
| `pin-test.key` | `~/.config/aistorage/pin-test.key` | `FATESAIKOU/MyAiStorage-pin-test` 的 deploy key 私鑰 |
| `github_known_hosts` | `config/github_known_hosts`（repo 內，非秘密） | SSH 主機指紋；嚴格模式擋掉中間人 |

pin repo 預設是 `git@github.com:FATESAIKOU/MyAiStorage-pin-test.git`。
**不要**把 pin key 指到正式的 `MyAiStorage-pin`：非 CI 環境寫正式 repo 會直接
被 `GitPinStore` 擋下（`PermissionError`）。

**外部程式**：`git`、`git-annex`、`rclone`、`git-filter-repo`。

**覆寫用的環境變數**（與 `tests/integration/conftest.py` 同一組，所以 CI 直接
寫檔＋設變數就能用同一份設定）：

```
AISTORAGE_TEST_IDS          # 預設 ~/.config/aistorage/ids.env
AISTORAGE_TEST_RCLONE_CONF  # 預設 ~/.config/aistorage/rclone-committer-test.conf
AISTORAGE_TEST_PIN_KEY      # 預設 ~/.config/aistorage/pin-test.key
AISTORAGE_TEST_KNOWN_HOSTS  # 預設 config/github_known_hosts
AISTORAGE_TEST_PIN_REPO     # 預設 git@github.com:FATESAIKOU/MyAiStorage-pin-test.git
```

`TEST_FOLDER_ID` 也可以直接用環境變數帶（優先於 `ids.env` 裡的值），
Actions 把它當 secret 寫進檔案、或直接當變數都可以。

### 跑

```bash
uv run python scripts/run_integration.py                    # tests/integration 全部
uv run python scripts/run_integration.py --only three_rounds  # 只跑 id 含此字串者
uv run python scripts/run_integration.py --include-e2e      # 再加跑 tests/e2e
```

其他選項：

- `--skip-leftovers`：不跑收尾殘留檢查（Drive 或 pin repo 連不上時）。
- `--strict-leftovers`：**這一輪**有殘留時 exit code 也算 1（CI 用；預設只列出來）。
  跑前就存在的舊東西不算——那不是這一輪的責任，算進去會讓每一場都紅。

行為：

- **序列化執行**（沒有 `-n`，不開 xdist）。pin-test repo 是所有線共用的，
  同時跑會互相覆蓋釘選值。每一支測試用唯一的釘選值條目名
  （`sandbox.pin_repo_name()` 產生 `it-<ULID>`），避免撞名。
- **不開 `-l`／rich traceback**（用 `--tb=short`）：本機區域變數可能含秘密或
  未過濾路徑，不能印進 Actions log（`docs/impl/group3-modules.md` 8.3 的 N8）。
- **殘留檢查**：跑前拍一張快照（Drive 測試根底下的直接子資料夾名 ＋ pin repo
  `.pin/` 條目名），跑完再拍一次。摘要分成兩段：
  **這一輪的殘留**（快照比對出來的新名稱）與**跑前就存在的舊東西**
  （只報數量，指向清理腳本）。沒有自動刪——可能是別條線的。
- **摘要與 exit code**：結束印各階段與合計的通過／失敗／略過數與總時間；
  任一階段失敗 → exit 1，preflight 沒過 → exit 2，全部通過 → 0。

### 殘留長什麼樣

Drive 底下留下 `it-<ULID>/` 或 `it-erase-<ULID>/` 前綴（連同同名的
`-inbox`／`-quarantine`／`-readview`）、pin repo 留下 `.pin/it-*`，就是有東西
沒清掉。常見原因是測試被中斷（Ctrl-C、機器睡著）。

conftest 的三道清理都只認「自己建過的 file id」：`sandbox` teardown 逐一刪掉
它建的前綴與收件匣、`sweep_session_leftovers` 在整場結束時補掃一次、
`cleanup_pin_entries` 用 pin store 自己的 `GIT_SSH_COMMAND` 推一筆刪除 commit。
被硬殺就沒用——那些就是下面這支腳本要清的。

清理失敗不再靜默：conftest 會用 `warnings.warn` 說出是什麼、哪個 id，
整場結束再印一次彙總（否則「Drive 上留了東西」完全沒有線索）。

### 清掉舊殘留

```bash
uv run python scripts/cleanup_integration_leftovers.py            # dry-run（預設）
uv run python scripts/cleanup_integration_leftovers.py --confirm  # 真的刪
```

範圍只有整合測試自己的東西：Drive `TEST_FOLDER_ID` 底下符合 `it-<ULID>`
（含 `it-erase-`）的前綴與其 `-inbox`／`-quarantine`／`-readview`，以及 pin-test
repo 的 `.pin/it-*`。**`e2e-*` 一律不碰**（e2e 環境是單例，impl3 在用；要清
e2e 的東西走 `scripts/e2e_setup.py --sweep-orphans`）。

沒有 `--confirm` 就是 dry-run，會把「會刪什麼」與「不碰什麼」都列出來。
`--confirm` 時每個資料夾刪之前重新 `get()` 確認 parents 與名字都對得上，
對不上就跳過並說明——快照是上一個行程拍的，中間別人可能動過。

### e2e 額外前置

`--include-e2e` 還需要 `scripts/e2e_setup.py` 先 setup 好（Agora repo、
讀取視圖、測試收件匣、測試 profile 的祕密目錄）、`resident/build.sh` 建好 image。
細節見 `tests/e2e/README.md`。e2e 環境是單例，有 `.e2e-env.lock.json` 鎖；
整合測試本身不吃那把鎖（它用自己的 `it-<ULID>` 前綴與釘選值名）。

## 搬到 GitHub Actions

**這個檔只是示意，repo 裡沒有真的 workflow 檔**（Actions 額度用完）。
搬過去的關鍵只有三件事：

1. 把 secret 從 Actions secrets **寫成檔案**（測試的設定一律以路徑引用，
   這支腳本只檢查檔案在不在，不會讀內容）；
2. 設那幾個 `AISTORAGE_TEST_*` 環境變數指向剛寫出來的檔案；
3. 呼叫同一個指令。整合測試本來就是序列化跑的，不需要 GitHub Actions 的
   matrix 來分攤，也不需要 service container。

```yaml
# .github/workflows/integration.yml（示意，目前不建立）
name: integration
on:
  workflow_dispatch:
  schedule:
    - cron: "17 3 * * *"   # 避開整點，別跟別人撞

jobs:
  integration:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    env:
      # 秘密從 Actions secrets 寫成檔案；內容不會被印出來。
      HOME: /home/runner
    steps:
      - uses: actions/checkout@v4

      - name: Install git-annex
        run: |
          sudo apt-get update
          sudo apt-get install -y git-annex
          curl -sSLO https://raw.githubusercontent.com/newren/git-filter-repo/main/git-filter-repo
          sudo install -m755 git-filter-repo /usr/local/bin/
          # rclone
          curl https://rclone.org/install.sh | sudo bash

      - name: Write credentials from secrets
        env:
          RCLONE_CONF_B64: ${{ secrets.IT_RCLONE_CONF_B64 }}
          PIN_TEST_KEY: ${{ secrets.IT_PIN_TEST_KEY }}
          KNOWN_HOSTS: ${{ secrets.IT_KNOWN_HOSTS }}
          TEST_FOLDER_ID: ${{ secrets.IT_TEST_FOLDER_ID }}
        run: |
          set -euo pipefail
          mkdir -p ~/.config/aistorage
          echo "$RCLONE_CONF_B64" | base64 -d > ~/.config/aistorage/rclone-committer-test.conf
          printf '%s\n' "$PIN_TEST_KEY" > ~/.config/aistorage/pin-test.key
          chmod 600 ~/.config/aistorage/pin-test.key ~/.config/aistorage/rclone-committer-test.conf
          printf '%s\n' "$KNOWN_HOSTS" > ~/.config/github_known_hosts
          printf 'TEST_FOLDER_ID=%s\n' "$TEST_FOLDER_ID" > ~/.config/aistorage/ids.env
          export AISTORAGE_TEST_KNOWN_HOSTS=$HOME/.config/github_known_hosts
          echo "::add-mask::$PIN_TEST_KEY"

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Run integration tests
        run: |
          uv sync --extra dev
          uv run python scripts/run_integration.py --strict-leftovers
```

Actions 上唯一要注意的環境差異：

- pin repo 的 deploy key 是唯讀以外都要開；`GitPinStore` 用
  `GIT_SSH_COMMAND`（`-F /dev/null`、`IdentitiesOnly`、
  `StrictHostKeyChecking=yes`、`UserKnownHostsFile`）隔離 runner 的個人身分，
  所以 runner 不需要也不該有 `SSH_AUTH_SOCK`。
- 摘要最後一段就是給人看的（過／失敗／略過 + 總時間 + 結果），失敗時
  直接從 Actions log 讀就好，不必再產 artifact。
- 官方 `actions/checkout` 設的 `GITHUB_ACTIONS=true` 會影響 `GitPinStore`
  對正式 pin repo 的寫入保護——CI 上理論上可以寫 `MyAiStorage-pin`，但
  `AISTORAGE_TEST_PIN_REPO` 仍應指向 `-test` repo。

## 單元測試對這支腳本的覆蓋

`tests/unit/test_run_integration_smoke.py`：preflight 缺東西時的每一種訊息
（缺檔案、`ids.env` 沒有 `TEST_FOLDER_ID`、找不到外部程式、外部程式執行失敗）、
`resolve_settings` 的環境變數覆寫、`diff_names`、`parse_junit`、
`pytest_argv`（不開 `-l`）、`format_issues`／`format_summary` 的內容、
`Summary.exit_code` 的組合。

`tests/unit/test_cleanup_leftovers_smoke.py`：清理腳本的名字邊界
（`it-<ULID>`／`it-erase-<ULID>`／三種附屬資料夾算，`e2e-*`／`syncer-*`／
不像 ULID 的一律不算）、pin 條目的四種副檔名收斂成同一個條目名、
`partition_leftovers` 把這一輪與更早的分開、`--strict-leftovers` 只對這一輪
生效，以及 **CLI 預設絕不刪**（用假的 `confirm_deletions` /
`confirm_pin_deletions` 斷言它們沒被呼叫）。
