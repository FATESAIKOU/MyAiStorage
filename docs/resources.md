# 實體資源清單

AiStorage 用到的實體資源都記在這裡：Drive 資料夾、OAuth client、service account、token、repo。⚠️ **只記名稱、位置、用途、屬於哪個 profile、到期日；不記任何秘密的值。** 秘密放在 GitHub Actions secrets、MyLinuxPool 的 profile secrets，或（技術驗證期間）Mac 的 `~/.config/aistorage-spike/`。

## 期 1 正式身分（tasks 2.5，2026-09-27 建立）

秘密的值放在 Mac 的 `~/.config/aistorage/`（目錄 700、檔案 600）與 GitHub Actions secrets。

| 資源 | 種類 | 位置／識別 | 用途 | profile | 到期／輪替 |
|---|---|---|---|---|---|
| `aistorage-spike-1-260926` | GCP project | 專用帳號 | **提交流程專用**（只放 committer 一個 OAuth client，以及讀取用的 SA）；技術驗證時的 `other-project` client 已刪除 | — | 長期 |
| `aistorage-spike-2-260926` | GCP project | 專用帳號 | **worker 共用**（只放 worker 一個 OAuth client）；技術驗證時的三個 client 已刪除 | — | 長期 |
| `committer` | OAuth client（電腦版，正式版發布、未驗證） | project 1；`~/.config/aistorage/client-committer.json`、`rclone-committer.conf`（remote `gdrive`，scope `drive`，尚未設 `root_folder_id`） | 提交流程 | 提交流程 | 第 8 天複查 refresh token：2026-10-05 前後 |
| `worker` | OAuth client（同上） | project 2；`~/.config/aistorage/client-worker.json`、`rclone-worker.conf`（scope `drive.file`） | 所有 worker 共用的收件匣寫入 | 所有 worker | 同上 |
| `spike-reader@aistorage-spike-1-260926.iam.gserviceaccount.com` | service account | project 1；金鑰仍在 `~/.config/aistorage-spike/sa-reader.json` | 所有 worker 共用的讀取身分（正式的讀取視圖資料夾建立後再分享） | 所有 worker | — |
| `aistorage-test` | Drive 資料夾 | 專用帳號的我的雲端硬碟，由新的 committer 建立，id `1_MfiN1QT344hrp2884QbZkB8jJN7zYpa`（`~/.config/aistorage/ids.env`）；`rclone-committer-test.conf` 以它為根 | 第 3 組以後的整合測試（worker client 看不到它） | 提交流程（測試） | 測試用 |
| `FATESAIKOU/MyAiStorage-pin` | GitHub private repo | github.com | 釘選值（ADR 0008）；不放 workflow | 提交流程 | 長期 |
| `FATESAIKOU/MyAiStorage-pin-test` | GitHub private repo | github.com；deploy key `committer-test` 的私鑰 `~/.config/aistorage/pin-test.key` | 整合測試用的釘選值 | 提交流程（測試） | 測試用 |
| `committer`（deploy key） | SSH deploy key（read-write） | `MyAiStorage-pin`；私鑰 `~/.config/aistorage/pin-deploy-key`，並放在 `FATESAIKOU/MyAiStorage` 的 Actions secret `PIN_DEPLOY_KEY` | 提交流程寫入釘選值 | 提交流程 | 外洩時輪替 |

- 注意：技術驗證的 `aistorage-spike` 資料夾是舊的 committer（當時在 project 2）建的，所以 project 2 的 worker client **看得到也改得到它**。之後的整合測試要由新的 committer 另外建一個測試資料夾，不再用它。
- `~/.config/aistorage-spike/` 裡 project 2 的三份 rclone conf 與 `other-project` 的 conf 已經失效（client 已刪除）。
- 還沒做：正式的 Agora／讀取視圖資料夾、`RCLONE_CONF` secret、簽章金鑰與 `config/identity.json`（第 3、5 組建立時一起做）。ADR 0009 之後沒有 Foundry 的 repo／讀取視圖要建。

## 技術驗證（tasks 1.1〜1.9，2026-09-26 建立）

驗證結束後，標「驗證用」的資源全部撤銷或刪除，不沿用到正式環境。建立步驟見 `docs/spike/setup.md`。

| 資源 | 種類 | 位置／識別 | 用途 | profile | 到期／輪替 |
|---|---|---|---|---|---|
| AiStorage 專用帳號 | Google 帳號（Google One 家庭共用成員，共用 5TB 配額） | 由使用者管理 | AiStorage 所有 Drive 資料的擁有者 | 你（管理）、提交流程 | 長期 |
| `aistorage-spike-1-260926` | GCP project | 專用帳號 | service account；1.4 的另一個 project（`other-project` client） | — | 驗證用 |
| `aistorage-spike-2-260926` | GCP project | 專用帳號 | 主要 OAuth client（committer、mac-opencode、test-profile） | — | 驗證用 |
| `committer` | OAuth client（電腦版，正式版發布、未驗證） | project 2 | 提交流程，scope `drive` | 提交流程 | 驗證用 |
| `mac-opencode` | OAuth client（同上） | project 2 | 收件匣，scope `drive.file` | Mac opencode | 驗證用 |
| `test-profile` | OAuth client（同上） | project 2 | 收件匣，scope `drive.file` | 測試用 profile | 驗證用 |
| `other-project` | OAuth client（同上） | project 1 | 1.4 不同 project 的隔離，scope `drive.file` | 測試 | 驗證用 |
| `spike-reader@aistorage-spike-1-260926.iam.gserviceaccount.com` | service account（JSON 金鑰） | project 1 | 讀取身分；對 `aistorage-spike` 資料夾是 reader | 讀取（驗證用共用一個） | 驗證用 |
| `aistorage-spike` | Drive 資料夾 | 專用帳號的我的雲端硬碟，id `1Obn3Rj1Quyg1l_2YW0GhXE39FpETeyLj` | 所有驗證都只在這裡進行 | — | 驗證用 |
| `FATESAIKOU/aistorage-spike` | GitHub private repo | github.com | 1.6、1.8 的 workflow 驗證 | — | 驗證用 |
| `RCLONE_CONF` | Actions secret | `FATESAIKOU/aistorage-spike` | 提交流程的 rclone 設定（committer） | 提交流程 | 驗證用 |
| `aistorage-spike-actions` | fine-grained token | 使用者的 GitHub 帳號；只有 `aistorage-spike` 的 Actions: Read and write | 觸發 workflow_dispatch | Mac opencode | 2026-10-26 到期 |
| ollama-cloud API key | API key | 使用者的 ollama-cloud 帳號 | 容器裡 opencode 的 LLM | Mac opencode（白名單） | 驗證後撤銷 |
| spike workflows（`spike-git-annex`、`spike-commit-pipeline`、`spike-empty-check`、`pin-writeback-impl2`、`h4-sha-guard-test`、`sweep-cost-measure`） | GitHub Actions workflow | `FATESAIKOU/aistorage-spike`；副本在本 repo 的 `spike/workflows/` | 1.2、1.6、1.8、1.4f 的驗證 | — | 2026-09-27 全部 disable；其中三個帶 `RCLONE_CONF`，正式資料進入專用帳號前刪除 |
| `pin-state` | git 分支 | `FATESAIKOU/aistorage-spike` | 1.4f 兩階段釘選寫回的驗證（內含 `.github/`，正式環境要做成 orphan 分支） | — | 驗證用 |
| `agora-basic/`、`agora-erase/`、`agora-read/`、`agora-load/`、`agora-erase2/`、`agora-1.2m2/`、`agora-m2fg/`、`agora-1.4f2/`、`agora-1.4f3/`、`agora-1.4f3multi/` | git-annex repo 前綴（Drive 資料夾） | `aistorage-spike` 資料夾底下 | 1.2〜1.8 各項驗證（各項一個前綴，不共用） | 提交流程 | 驗證用 |
| `quarantine-1.4f2` 等隔離資料夾 | Drive 資料夾 | `aistorage-spike` 資料夾底下 | 1.4f 對策移出的注入物（約 29 筆） | 提交流程 | 驗證用 |
| `aistorage-spike-inbox-*`、`aistorage-spike-*` | Drive 資料夾 | 專用帳號「我的雲端硬碟」根目錄（`drive.file` client 建不進 `aistorage-spike`，只能以資料夾 id 追蹤） | 1.4、1.8 的收件匣 | Mac opencode／測試 | 驗證用 |
| 容器工作目錄 | 本機目錄 | Mac 的 `~/.local/share/aistorage-spike/work/`（含 opencode 產生的明文 `auth.json`） | 各項的一次性容器與 clone | — | 驗證用 |

### 清理（2026-09-27 完成，只清測試資料）

使用者決定只清測試資料、保留帳號設定，供第 2 組的整合測試使用。已刪除：`aistorage-spike` 資料夾底下的所有子項（各 `agora-*` 前綴、隔離資料夾）、根目錄的 `aistorage-spike-*` 資料夾與收件匣、整個專用帳號的垃圾桶（`trashed=true` 查詢為 0）、測試 repo 的所有 workflow、`RCLONE_CONF` secret、`pin-state` 分支與全部 run、Mac 上的容器工作目錄（含 `auth.json`）。**保留**：專用帳號、兩個 GCP project、四個 OAuth client、`spike-reader` SA、`aistorage-spike` 資料夾本身、測試 repo（只剩 main）、PAT、ollama-cloud key、`~/.config/aistorage-spike/`、docker image `aistorage-spike-env`。紀錄見 `docs/spike/evidence/1.9-cleanup.md`。上表中已刪除的列保留作為紀錄。

提交流程的 client 要搬到獨立的 GCP project（tasks 2.5），完成後舊的 committer client 撤銷，本表隨之更新。
