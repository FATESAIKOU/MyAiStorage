# 住民容器（Mac 上的 opencode）

這個文件說明第 5 組的執行容器怎麼開、白名單是什麼、以及**能力邊界**在哪裡。
設計依據：`docs/impl/group5-7-modules.md` 第 1 節、design D3、ADR 0006、
技術驗證 1.7a〜1.7h、PM 決定 1／2（2026-09-28）。

---

## 1. 它是什麼

一個跑在 colima/docker 裡的 Ubuntu 24.04 容器，裡面有 opencode（住民 AI）、
rclone、同步器與 skill。**每個住民容器對應一個 profile**（期 1 是
`mac-opencode`），也就是一組簽章金鑰與一組授權。

```
Mac                                     容器
~/.config/aistorage/resident/<profile>/  →  /secrets/   （唯讀，白名單）
~/.local/share/aistorage/work/<容器名>/  →  /work       （可寫，HOME=/work）
                                        /opt/aistorage  （唯讀：套件、plugin、skill 說明）
```

同時可以開多個容器（每個分岔各自一個，design D3）。它們的 `/work` 與 opencode 的
本機資料**互不可見**；只有共用同一個 profile 的容器才共用同一組憑證。

---

## 2. 開一個容器

```bash
# 1. 準備 profile 的秘密目錄（只放白名單檔案）
mkdir -p ~/.config/aistorage/resident/mac-opencode
cp ~/.config/aistorage/rclone-worker.conf ~/.config/aistorage/resident/mac-opencode/
cp ~/.config/aistorage/sa-reader.json     ~/.config/aistorage/resident/mac-opencode/
cp ~/.config/aistorage/reader.json        ~/.config/aistorage/resident/mac-opencode/   # 非秘密
# signing.key 與 llm-<provider>.key 是這個 profile 專用的（見下）
chmod 600 ~/.config/aistorage/resident/mac-opencode/*

# 2. 看掛載計畫（不啟動容器；文件與測試都靠它）
resident/run.sh work-1 --profile mac-opencode --model opencode/space-bunny-free --print-plan

# 3. 建立 image（第一次要跑，約幾分鐘）
resident/build.sh

# 4. 啟動
resident/run.sh work-1 --profile mac-opencode --model opencode/space-bunny-free
```

啟動之後前景是 TUI，已經附在容器內既有的 `opencode serve` 上（PM 決定 1：
opencode 1.18.32 有 `opencode attach <url>`）。要從 **Mac 上**的 opencode 接同一個
session，加 `--publish-api 4096`（把容器的 4096 埠發布到宿主機的 loopback；
預設不發布，容器外看不到）。

不做互動介面（測試／CI）：`--no-tui`，此時前景只是容器裡的 `bash -l`。

---

## 3. 白名單（憑證）

**只有**下列檔名會以唯讀方式掛到 `/secrets/`：

| 檔名 | 用途 | 誰在用 |
|---|---|---|
| `rclone-worker.conf` | worker 的 Drive 憑證（`drive.file` scope，只碰得到自己的收件匣） | 同步器、skill |
| `sa-reader.json` | 讀取身分（SA，**沒有**儲存配額、不能建立檔案） | 同步器、skill 的讀取 |
| `signing.key` | 該 profile 的簽章私鑰（32 bytes Ed25519） | 同步器、skill 組項目 |
| `reader.json` | 讀取設定（manifest id 等，**不是秘密**） | 同步器、skill |
| `llm-<provider>.key` | LLM 金鑰（**只掛 `--model` 指定的那個 provider**） | opencode |
| `gh-pat-actions.txt` | 選用，5.3 觸發提交流程（`workflow_dispatch` 權限） | skill 的 `sync-and-commit` |

目錄裡出現**任何不在白名單的檔名就拒絕啟動**並列出檔名——不是默默忽略。這樣
「不小心把 `.env` 或 `anthropic.key` 放進去」會在啟動時就出錯，而不是靜靜地
多給了容器一份憑證。

權限過寬（group／other 可讀）也拒絕，並提示 `chmod 600`。

### 為什麼不掛 Claude 的憑證

PM 決定 2：住民的模型依隊員順序挑當下有額度的，**不使用 Claude**；模型與
provider 都是**設定值**，不寫死在 image 裡。image 裡沒有任何 Claude 的東西，
`entrypoint.sh` 啟動時還會主動拒絕：環境變數有 `ANTHROPIC_*`／`CLAUDE_*`，
或 `/work` 底下出現 `.claude*`、`~/.config/anthropic`、
`~/.config/opencode/auth.json`（opencode 自己寫出來的明文憑證）就**拒絕啟動**
並提示刪除。

### 為什麼 LLM 金鑰是「檔案引用」

`entrypoint.sh` 由 `opencode.base.json` 產生 `/tmp/aistorage/opencode.json`，
provider 的 `apiKey` 寫成 `{"file":"/secrets/llm-<provider>.key"}`。opencode 會
在啟動時讀那個檔案，所以它**不會**在 `/work` 寫出明文的 `auth.json`
（技術驗證 1.7a M4；已用 `opencode debug config` 與假的金鑰值驗過：`{file:...}`
會被解析成檔案內容，而且 `~/.local/share/opencode/` 下只有 `opencode.db` 與
lock 檔，沒有 `auth.json`）。

`rclone-worker.conf` 則是**複製**到 `/tmp/aistorage/rclone.conf`（600，可寫），
因為 rclone 要能刷新 access token 寫回去。

---

## 4. 能力邊界

啟動後在容器裡跑一次：

```bash
resident/verify-boundary.sh            # 人看的報告
resident/verify-boundary.sh --json     # 給存檔比對
```

它會檢查並印出：環境變數的**名稱**、`/proc/mounts` 裡的 `/secrets` 與 `/work`、
有沒有 `docker.sock`、`CapEff`、`/Users` 看不看得到、`/secrets` 是否真的不可寫、
**秘密的值有沒有出現在 `/work` 裡**（只輸出計數）、以及 Claude 相關
的環境變數／檔案。任一項不過就以非零碼結束。

### 「秘密沒有出現在 /work」這個檢查的兩個陷阱

這兩點是 5.2 的容器驗收才發現的，5.1 證據裡記的「0 命中」其實是**假通過**：

1. **不能用 `${SECRETS_DIR}/*` 列舉。** `/secrets` 是 `0711`（可穿越、不可列
   目錄），glob 不會展開 → 迴圈一個檔案都沒掃到 → 命中數永遠是 0。
   腳本改成照白名單指名，並輸出 `secret_leak_scanned`（實際掃過幾個檔案）；
   掃過 0 個就算失敗，不准回報通過。
2. **短行不能當 pattern。** `grep -F -f` 會把 pattern 檔的每一行都拿去比對，
   而 JSON／conf 裡有 `{`、`}`、`},` 這種 1～2 字元的行。實測 `sa-reader.json`
   有一行是單字元，於是 `/work` 底下 opencode 裝的 `node_modules` 裡幾千個
   JSON 全部「命中」。腳本只取長度 ≥ 12 的行當 pattern。

`reader.json` **不參與**這個掃描：它是刻意放在 `/secrets` 的非秘密設定
（manifest id、inbox folder id），內容都是通用 JSON 鍵，拿它比對會撞到
`/work/schemas/*.json`，只會製造假警報。真正要掃的是 `rclone-worker.conf`、
`sa-reader.json`、`signing.key`、`llm-<provider>.key`、`gh-pat-actions.txt`。

反向驗證（把 `signing.key` 複製到 `/work`）：掃描報 `signing.key=1` 並以非零碼
結束。

固定的邊界（Dockerfile 與 `run.sh` 一起保證）：

- 不掛 `docker.sock`
- 不加任何 capability（`CapEff` 為 0）
- 不 `--privileged`
- 以 Mac 的 `uid:gid` 執行（colima 的掛載是 sshfs，root 會讓 git-annex 的 chown 失敗）
- 只有 `/secrets`（唯讀）與 `/work` 兩個掛載點來自宿主機

### 信任範圍（D3 接受的）

同一個容器裡的 AI 可以直接用 bash 執行
`python -m aistorage.skill claim --session <別的 session id>`，而同一個 profile 的
持有者檢查**會通過**。也就是說：**同一個 profile 就是同一個信任範圍**。要更細的
邊界就要分不同的 profile（分不同容器、不同簽章金鑰、不同收件匣）。

---

## 5. 出問題時

| 症狀 | 處理 |
|---|---|
| `拒絕啟動：環境變數 ANTHROPIC_API_KEY 存在` | 從宿主機的環境移除（`unset`），或改用 `env -u` 啟動 |
| `拒絕啟動：…/auth.json 存在` | `docker exec` 不進去；用 `run.sh <名> --no-tui` 開一個沒互動的容器，進去 `rm` 後重啟 |
| `拒絕啟動：…有不在白名單的檔案` | 把不該在那裡的檔案移出 `~/.config/aistorage/resident/<profile>/` |
| `拒絕掛載權限過寬的秘密檔` | `chmod 600`（或設 `AISTORAGE_RESIDENT_ALLOW_LOOSE_PERMS=1`，不建議） |
| `已有同名容器存在` | 每個容器一份工作目錄；`docker rm -f <名>` 或換名字 |
| 同步器沒在跑 | 看容器內 `/tmp/aistorage/syncer.log`；`python -m aistorage.syncer opencode status` |
| 提交流程沒被觸發 | 5.3 的逾時訊息會提示跑健康檢查（6.3）；workflow 可能被停用 |

---

## 6. 環境上踩過的坑（2026-09-28 實測）

記在這裡是因為換一台 Mac 或重開 colima 很可能再遇到。完整錄影見
`docs/spike/evidence/5.1-resident-boundary.md`。

| 症狀 | 原因 | 處置 |
|---|---|---|
| `docker pull` 說 `no such host` | colima VM 的 systemd-resolved 指向失效的 IPv6 resolver（宿主機的系統 resolver 也壞，但 `8.8.8.8`、`192.168.0.1` 本身是好的） | **先照既有的原則 `colima restart`**（review-g5-6 L4：這是既有的處置原則）。如果重啟後仍然壞，才改 **VM 內**：`/etc/systemd/resolved.conf.d/aistorage-dns.conf` 寫 `DNS=8.8.8.8 1.0.0.8` 後 `systemctl restart systemd-resolved`。**這個 drop-in 是當時為了先建 image 加的，會留在 VM 裡，請 PM／使用者確認要不要保留**（它只影響 colima VM，不動宿主機） |
| image build 卡在 `TARGETARCH: parameter not set` | colima **沒有 buildx**，legacy builder 不會自動帶 BuildKit 的 `TARGETARCH` | `build.sh` 用 `--build-arg` 傳；Dockerfile 內部再以 `uname -m` 兜底 |
| `opencode: required file not found` | 本機 `ubuntu:24.04` 標籤曾快取成 **amd64**，arm64 的靜態執行檔在 amd64 容器裡跑不起來 | `build.sh` 帶 `--platform linux/arm64`（依 `uname -m`） |
| `cryptography` 匯入時 SIGILL | 47+ 的 aarch64 wheel 用了 colima VM 沒暴露的指令（VM 的 CPU Features 沒有 armv8.2+ 的 LSE/SHA 系列） | image 內固定 `cryptography>=42,<47`（46.0.3 實測可用）。Mac 上的開發環境不受影響 |
| entrypoint 說「缺少必要檔案」但檔案確實掛上去了 | `/secrets` 是 root 的 `0700`，容器以 Mac 的 uid（501）執行時連 `stat` 都做不到 | image 內 `/secrets` 改 `0711`（可穿越、不可列目錄） |
| entrypoint 卡住不動 | `opencode serve` 剛起來時第一次 `GET /session` 可能連得上卻不回應，沒有逾時的 `curl` 會卡死 | 探測加 `--connect-timeout 2 --max-time 5`，60 秒內沒就緒就明確失敗 |
| wheel 裝完 `import aistorage` 找不到 `schemas/*.json` | wheel 只含 `src/aistorage/`，`aistorage.schema` 執行期要找 `schemas/` | Dockerfile 把 `schemas/` 複製到 `/opt/aistorage/schemas`，entrypoint 啟動時再複製到 `/work/schemas`（CWD 是 `/work`）。**建議之後**在 `schema.py` 加一個套件內／環境變數的路徑（那是第 2 組的檔案，請 PM 決定誰改） |

## 7. 還沒做的

- 第 6 組的管理操作（`src/aistorage/admin/`；review-g5-6 的 H4〜H7 未修之前
  **不要對 Agora 的真實資料執行任何管理腳本**）
- TUI attach 的互動驗證（`opencode attach` 的存在已確認，但還沒有人用真人操作過；
  這一步需要真的開一個對話，屬於 5.4 的驗收）
