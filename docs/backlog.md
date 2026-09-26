# 待辦清單

期 1 之外的事項。期 1（驗證分裂、統合、相互參照，見 `openspec/changes/establish-aistorage-phase1/proposal.md`）之後不再分期規劃，要做時從這裡挑，挑的時候再跟使用者確認範圍與順序。沒有排序。

## 手機秘書（MyAiEntry）

- Session 同步器（排除 SSH 私鑰、LLM key、PAT）與手機 App 的轉換器（含內嵌圖片）；手機 App 的單一 Session 手動匯入。
- 裝置上的原生 OAuth（WebView 內禁止 OAuth）；手機 App profile 的憑證。
- 分裂、統合、參照與交接的動作：同步並寫交接單 → 一起提交 → 由接手的一方認領（以讀取介面確認）。
- 讀取介面的 TypeScript 用戶端（或共用同一份查詢規格），讀讀取視圖與搜尋索引。

## worker 與員工（MyLinuxPool）

- 見 `docs/tickets/mylinuxpool.md`：worker image 安裝 opencode、由 MyLinuxPool 發放 profile 憑證（證明 profile 歸屬）、profile 能力清單、刪除 worker 前同步完成、把手機 App 與 Mac opencode 登記成 profile。
- worker/default profile：`drive.file` 收件匣、service account reader、fine-grained token（MyBrain 與 Atelier 唯讀、MyAiStorage 只有 Actions 寫入）。
- 「秘書把 Session 交給員工」的交接情境端到端驗收；家裡離線情境。

## Atelier

- 基本設計與實作，等 MyLinuxPool 的 profile 重新設計之後一起考慮。起點是 `docs/atelier/`（需求設計、`role.md`、`external-copy.md`）與原 design D6：GitHub private repo、`roles/<職務>/{know,do,judge,dont}/`、`vendor/`、職務版本＝commit、修改經提交流程跑 judge。
- judge 驗證腳本在哪種執行環境跑、隔離到什麼程度、逾時多久、失敗原因寫在哪裡（原本是 REQ.md 實作語言提案的第四個問題）。
- 外部副本的上游比對（先手動，之後自動）。
- 秘書的 harness 移入 Atelier；Mac 本機與 LearnGhAgent 成為 Atelier 的消費者。

## MyBrain

- MyBrain PR：案件主題檔加不變的 `id`（欄位名與格式由這個 PR 定），更新 OKF 規範與 `validate.py`；更新 2026-09-06 那組已被推翻的判準筆記。
- 員工對 MyBrain 只讀；員工寫入 MyBrain（經提交流程開 PR，在免費方案下守住 AI 不能 merge）。

## Agora

- Mac 本機既有的 Claude Code、opencode、agy、codex Session 的自動同步（需要一個「Mac 同步程式」profile）。
- 摘要、收斂 AI、語意搜尋。

## Foundry

- 數 GB 的收容產出：寫入者直接上傳以內容雜湊命名的物件，提交流程只登錄雜湊（Drive 有 sha256Checksum 可以不下載就驗證）；連同原 tasks 1.7 的技術驗證（雜湊命名路徑、Actions runner 轉手 1 GB 與 3 GB 的耗時與磁碟上限）。
- 已知產出位置的自動收錄、死連結檢查；重要產出的快照。

## 共通

- 更高新鮮度的寫入機制（ADR 0007）：縮短定時間隔、上傳後自動觸發提交、合併多次觸發、自架 runner。
- 短效憑證。
- 提交流程的 clone 加速（Actions cache、定期重整 bundle），視技術驗證 1.8 的結果；注意它們會多出抹除要處理的副本。
- 內容層級的讀取授權：讀取視圖依可見範圍分資料夾（例如 LLMGateway 的隱私 tag）。
- 跨 repo 介面契約 `docs/contracts/`（收件匣上傳格式、觸發並等待提交流程的方式、能力清單格式、各 profile 的 secrets 名稱），等第一個外部消費者要開工時再寫。

## 技術驗證（2026-09-27）留下的待辦

- 偵測 opencode 的 fork：fork 沒有 parent 欄位、訊息 id 全部重生，用（時間＋內容雜湊）的前綴比對找出母 Session，建議建一條參考或接續 Link（技術驗證 1.7h）。
- 每個 profile 各自一個 Google 帳號：能從根源擋住注入（profile 的 token 建不進沒被分享的資料夾），做到的話 ADR 0008 可以撤回，偵測機制保留為縱深防禦。需要先補測「別的帳號的 `drive.file` client 建不進未分享的資料夾」。
- 容器時鐘在睡眠喚醒後的漂移（技術驗證 1.7j），與使用者一起補測。
- 1.3 誤刪事件的時間點與根本原因補進 `docs/spike/evidence/1.3-erase.md`（推測是 `rclone --drive-trashed-only` 的清單混入 live 資料夾）。
- 提交流程的釘選值寫回、清掃在 Actions cache 或自架 runner 下的行為（如果之後採用）。
