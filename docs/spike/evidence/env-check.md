# 容器環境核對（任務 A：spike/env 與 evidence/env-*.txt）

- 核對者：test。日期：2026-09-26。對照基準：`docs/spike/test-plan.md`（含四類判定與 1.2／1.5／1.7 前置）。
- 方法：只讀檔案——`spike/README.md`、`spike/env/{Dockerfile,build.sh,run.sh,verify.sh}`、`spike/evidence/env-{arm64,amd64}.txt`、`docs/spike/setup.md`、`docs/resources.md`、`~/.config/aistorage-spike/ids.env`（非秘密）。**不進容器、不碰 Drive／GitHub、不安裝任何東西。**
- 秘密檢查：對 `spike/` 與 `docs/spike/` 跑 test-plan 第三節的形狀掃描——只有 `test-plan.md` 自身的規範文字命中，兩份 evidence 乾淨（無 client secret、access／refresh token、PAT、私鑰形狀；只有 dummy 檔名與非秘密 id）。evidence 可提交。

## 結論（一句話）

arm64 容器**足以**作 1.2（arm64 半邊）、1.5、1.7 的前置，證據可信、無假綠燈；**amd64 的 qemu 環境不足以驗 git-annex**（segfault，已誠實標記），1.2 的 amd64 半邊與 1.8 仍待 GitHub Actions runner＋spike workflow（repo 裡還沒有這個 workflow）；另有 1.3 缺 `git-filter-repo`、1.5 缺 SA 的 rclone conf、證據路徑與慣例不一致。

## 一、逐項前置核對

### 1.2 git-remote-annex＋rclone 在 Drive（arm64／amd64）

- **arm64：足夠。** git-annex 10.20260717（≥10.20240531）、`/usr/local/bin/git-remote-annex` 存在且實際被用到、rclone 1.75.1（≥1.67）；`annex::` clone roundtrip 成功（檔案內容與 log 都有輸出）；colima sshfs 的 ownership 坑以 uid 501 解掉、git 的 safe.directory 與預設身分已設。root_folder_id 依 `setup.md`「追加」已補進 committer conf 並重寫 secret——1.2 步驟 2 的 `rclone lsf gdrive:` 存證**仍必須做**（本核對無法也不應驗 secret 現值）。
- **注意（避免誤引）**：roundtrip 用的是 **directory special remote（容器內 /tmp）**，不是 rclone special remote、不是 Drive。它證明的是 git-annex＋git-remote-annex＋sshfs 的機制；rclone／Drive 路徑、容器對外網路（Drive API）都**尚未**被這份證據驗證，是 1.2 自己的工作。
- **amd64：不足。** bundled git-annex 在 colima qemu 下 `git annex version` rc=139（signal 11）、roundtrip 同樣 rc=139 → amd64 的版本與 push／clone **尚未**被驗證。README 已正確導向「在真正的 amd64 runner 確認」，但連帶兩個待辦：
  1. **spike workflow 尚未進 repo**（`.github/workflows/` 目前只有 README 占位；test-plan 第二節標的「前置缺口」仍未解）。PAT 只有 Actions 權限、推不了 contents，需使用者用管理憑證放上去或明確授權代推。
  2. workflow 必須以與 Dockerfile **相同的來源與版本**（standalone tarball＋sha256、rclone zip）安裝，否則 1.2 的 arm64／amd64 版本可比性不成立。
- 提醒：不要把 Dockerfile 裡寫的版本當成「amd64 已驗版本」——binary 沒跑起來，版本未證明。

### 1.5 service account reader

- 已具備：`sa-reader.json`、SA 對 `aistorage-spike` 是 reader、arm64 容器可跑 git-annex。
- **缺**：`rclone-sa-reader.conf`（計畫自標的缺口；要 impl 建，`scope = drive`、`root_folder_id = SPIKE_FOLDER_ID`；H5）；以及 1.2 的 `agora-read/` repo（`encryption=none`、未 chunk）尚未存在。
- amd64 上讀取不受影響的假設同樣未驗（同 1.2 的 runner 問題）。

### 1.7 opencode export

- **機制層：足夠。** opencode 1.18.32 可執行；`ollama-cloud-key.txt` 可用 `run.sh` 只掛它（白名單機制已正負驗過）；能力邊界基線已量：env 只有 image／docker 變數、無 docker.sock、CapEff=0、`/proc/mounts` 只有 `/work` 與列出的 `/secrets/*`。
- 兩點補強在 1.7a 做：(1) 在**實際 1.7 的容器**重錄一次（本證據是基線，不是 1.7 的驗收）；(2) `/proc/mounts` 建議**不經 grep 過濾**整份 dump（env 證據只挑了 `/secrets|/work`，其他掛載可能看不到）；並可加驗 `/work` 以外不可達。
- 尚未驗（屬 1.7 本身）：容器內 opencode 對 ollama-cloud 的連線與模型可用性、export 全部子項。1.7j（時鐘漂移）需要的容器內網路＋外部 `Date` header 具備。

### 1.3 抹除

- **前置不足（小缺口）**：image 沒有 `git filter-repo`（apt 只裝 ca-certificates、curl、git、jq、python3、sqlite3、unzip、xz-utils；也沒有 pip）。1.3 要用它改寫歷史，需加進 Dockerfile 或執行時安裝（需容器網路）。
- 其餘（git、python3、rclone conf 讀取、canary 流程）具備。

### 1.4／1.6／1.8

- 1.4：不依賴此容器（Drive API 探測腳本可在 Mac 跑）；1.4f／1.4g／1.4h 需要的 1.2 建 repo 腳本與 Drive repo 尚未存在。
- 1.6：不依賴此容器（Mac 的 gh）。
- 1.8：**不足**（同 G1）：spike workflow 不存在；workflow 需在 runner 重現工具安裝、並帶收件匣資料夾 id（N2）；收件匣本身由 1.4i 建立，尚未存在。

## 二、證據可信度審計（假綠燈檢查）

1. **秘密**：全程用 dummy-a/b/c，非真憑證；「未列出的檔不入容器」有正負對照；「唯讀」是實際 `Read-only file system` 錯誤。可信。
2. **第 8 節 roundtrip**：有實際 clone、檔案內容、log 輸出，不是只看 exit code；是誠實的**部分**驗證，但驗的是 directory remote——報告與 1.9 引用時不得當成 rclone／Drive 的證據。
3. **amd64 rc=139**：標成失敗並寫明 qemu 研判，沒有粉飾；風險反而是「誤把 Dockerfile 宣稱當已驗版本」（見上）。
4. **FTS5 trigram**：有實際命中輸出（`人工智慧儲存層`），非空跑。
5. **能力邊界**：env／CapEff／docker.sock／裝置都有實際輸出；`/proc/mounts` 以 grep 過濾（見 1.7a 補強）。`:/Users/fatesaikou` 的 sshfs 來源顯示是 colima 的呈現方式——容器內 `ls /Users` 不存在、可達樹只有 `/work` 子目錄，不是破口。
6. **計時、uid、rc** 都有紀錄；`verify.sh` 以 `!! ` 偵測失敗並回非零（amd64 那次確實回非零）。無「工作樹裡 grep canary 命中自己」之類的假紅燈風險（本任務無 canary）。

## 三、缺口清單（依阻塞程度）

| # | 缺口 | 阻塞 | 建議 |
|---|---|---|---|
| G1 | spike workflow 不在 repo（`.github/workflows/` 只有 README） | 1.2 amd64、1.8 | 先解 test-plan 第二節前置缺口（使用者放或授權代推）；workflow 用與 Dockerfile 相同版本安裝 |
| G2 | amd64 git-annex 在 qemu segfault、版本未驗 | 1.2 amd64 | 在 Actions runner 上以同一 tarball＋sha256 安裝並記版本 |
| G3 | image 無 `git-filter-repo` | 1.3 | 加進 Dockerfile 或執行時安裝（需網路） |
| G4 | `rclone-sa-reader.conf` 未建 | 1.5 | impl 照計畫建（`scope = drive`、`root_folder_id`） |
| G5 | 證據在 `spike/evidence/`，test-plan 慣例是 `docs/spike/evidence/` | 1.9 彙整 | 複製到慣例路徑或在 1.9 引用時註明路徑 |
| G6 | 1.7a 的能力邊界要在實際容器重錄、mounts 不濾 | 1.7a | 容器內整份 `/proc/mounts`＋env 名稱清單 |
| G7 | 收件匣資料夾不存在（1.4i 產物） | 1.8 空跑 | 1.4i 完成後把 id 進 workflow 設定（N2） |

非阻塞已知事實：colima sshfs＋root 的 ownership 坑已解（uid 501）；shorthand URL 少 `directory=` 是呈現問題、已在 roundtrip 中用完整參數繞過。

## 四、判決

- **環境（arm64）足以作 1.2（arm64 半邊）、1.5、1.7 的前置，證據可信。** 但 1.2 的 amd64 半邊與 1.8 在 G1 解決前不具開工條件；1.3 在 G3 解決前不具開工條件；1.5 在 G4 前不完整。
- test 不動環境；上述交 impl／PM。
