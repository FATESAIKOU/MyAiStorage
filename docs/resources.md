# 實體資源清單

AiStorage 用到的實體資源都記在這裡：Drive 資料夾、OAuth client、service account、token、repo。⚠️ **只記名稱、位置、用途、屬於哪個 profile、到期日；不記任何秘密的值。** 秘密放在 GitHub Actions secrets、MyLinuxPool 的 profile secrets，或（技術驗證期間）Mac 的 `~/.config/aistorage-spike/`。

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

### 清理

依 `docs/spike/report.md` 第六節，在使用者判定 go／no-go 之後進行：刪除上面所有驗證用資源、清空專用帳號的垃圾桶並以 `trashed=true` 查詢確認為 0、撤銷 refresh token、PAT 與 SA 金鑰、刪除兩個 GCP project、從測試 repo 移除 `RCLONE_CONF`。完成後更新本表，並把紀錄存到 `docs/spike/evidence/1.9-cleanup.md`。
