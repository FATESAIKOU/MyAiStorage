執行方式：PM 負責拆解與驗收；每個任務由實作與測試兩位隊員分開完成，再經 QA 初步審查。第 1 組是閘門：技術驗證報告經使用者確認 go 之前，不開始第 2 組之後的任務。第 8 組是其他 repo 的工單，照各自專案的流程實作。

## 1. 技術驗證（閘門）

- [ ] 1.1 建立自己的 Google OAuth client，設成已發佈（個人使用例外），記錄建立步驟與 token 期限行為
- [ ] 1.2 在使用者的 Drive 上用 git-annex 內建的 git-remote-annex 加 rclone special remote 建一個測試 repo，從另一台機器 clone，確認 commit / push / log / diff / revert 可行
- [ ] 1.3 驗證抹除：`git annex drop --force`、`git annex forget`、改寫歷史並重推 bundle 之後，被抹除的內容在目前版本、舊版本、bundle 裡都找不到
- [ ] 1.4 驗證 `drive.file` 的隔離：client A 看不到、也刪不到 client B 建的檔案；分別測同一個 GCP project 與不同 project 的情況
- [ ] 1.5 驗證 service account 被分享成 reader 時，能讀取該資料夾（含 `git clone annex::…`），不能寫入或刪除
- [ ] 1.6 驗證只有「contents 唯讀＋Actions 寫入」的 fine-grained token：能觸發 workflow_dispatch，不能推 contents，不能修改 workflow 檔案
- [ ] 1.7 驗證大檔路徑：寫入者上傳以內容雜湊命名的物件、提交流程只登錄雜湊而不轉手內容；同時量測 Actions runner 直接轉手 1 GB 與 3 GB 檔案的耗時與磁碟上限
- [ ] 1.8 量測一次提交流程的耗時，確認 concurrency group 會讓第二次觸發排隊而不是並行
- [ ] 1.9 撰寫技術驗證報告（逐項 pass / fail，附證據），交使用者判斷 go / no-go

## 2. 共通 schema

- [ ] 2.1 定義共通 metadata 的 JSON Schema：六個必填與可空欄位、擴充欄位的規則；附驗證器與測試（缺少必填欄位時拒絕、不認得的欄位忽略）
- [ ] 2.2 定義收件匣項目的格式：原始紀錄＋sidecar、改寫提案、Session Link 與交接單、職務修改提案、Foundry 登錄項目；附格式驗證器
- [ ] 2.3 定義「收件匣 ↔ profile」的對照設定，以及期 1 的三種 profile（worker/default、手機 App、Mac 同步程式）

## 3. 提交流程：Agora 路徑

- [ ] 3.1 建立提交流程的 workflow 骨架：定時（預設每 3 小時）、workflow_dispatch、concurrency group、secrets 的讀取方式；確認被任意觸發時也安全
- [ ] 3.2 掃描收件匣、驗證項目；不合格的項目寫回一份拒絕原因，不進真本
- [ ] 3.3 依來源收件匣蓋產生者章，忽略寫入者自填的產生者
- [ ] 3.4 以內容雜湊判斷重複；同一內容重複上傳不產生新 commit
- [ ] 3.5 opencode 轉換器：由原始紀錄產生閱讀版
- [ ] 3.6 手機 App 轉換器：由原始紀錄（含內嵌圖片）產生閱讀版
- [ ] 3.7 處理 Session Link：接續與參考、接續點、交接單；接續 Link 缺少交接單或職務時拒絕
- [ ] 3.8 更新 Session 狀態（運作中／停止中）
- [ ] 3.9 處理改寫提案：做成新 commit，並驗證沒有改變任何內容的位置（接續點仍然有效）
- [ ] 3.10 單一 Session 手動匯入工具：把現存 Session 包成收件匣項目

## 4. 讀取視圖與搜尋

- [ ] 4.1 每次提交後發佈讀取視圖：metadata、閱讀版、產出目錄
- [ ] 4.2 建立 SQLite FTS5 索引（trigram 分詞），用中日文關鍵字驗證命中
- [ ] 4.3 搜尋用戶端（函式庫與 CLI）：條件篩選、全文搜尋、兩者組合；沒有讀取權的身分會被拒絕，而不是回傳空結果
- [ ] 4.4 重建指令：從原始紀錄重建全部閱讀版與索引，並比對重建前後的結果一致

## 5. 管理操作

- [ ] 5.1 抹除腳本（只限管理憑證），附驗證測試：抹除的內容在目前版本、舊版本、讀取視圖都找不到，並留下抹除紀錄
- [ ] 5.2 回滾腳本：把某個 Session 退回指定版本，閱讀版隨之重建
- [ ] 5.3 健康檢查：OAuth refresh token 是否有效、Actions 分鐘數用量報告

## 6. Atelier

- [ ] 6.1 建立 Atelier private repo，目錄為 `roles/<職務>/{know,do,judge,dont}/` 與 `vendor/`，並定義能力需求宣告的格式
- [ ] 6.2 建立第一個可用的職務，judge 附上可執行的驗證腳本
- [ ] 6.3 提交流程處理職務修改提案：在隔離目錄跑 judge，通過才推進 main，失敗就記下原因、main 不動
- [ ] 6.4 員工用的職務載入器：取得指定 commit、比對能力需求與 profile 能力清單（不足就拒絕並說明）、只啟用宣告的能力、把「職務＠commit」寫進 Session metadata
- [ ] 6.5 把職務用到的第三方 skill 與 `mybrain-*` 複製進 `vendor/`，記錄出處與版本
- [ ] 6.6 上游比對 workflow（手動觸發）：以內容比對分成一致、有落差、上游已消失三類，有落差的產生更新提案，更新要過 judge

## 7. Foundry

- [ ] 7.1 在 Drive 上建立 Foundry 的 git-annex repo（與 Agora 分開）
- [ ] 7.2 產出目錄的項目格式，以及透過收件匣登錄（原處產出是連結型項目，記錄產出它的 Session）
- [ ] 7.3 收容產出入庫：小檔經提交流程；大檔依 1.7 的結論走雜湊命名路徑
- [ ] 7.4 產出目錄進讀取視圖，可以依型態、所屬案件、產生者、時間、產出它的 Session 查詢

## 8. 外部專案工單

- [ ] 8.1 MyLinuxPool：worker image 安裝 opencode；profile 宣告能力清單；用 profile secrets 注入該 profile 的憑證（Drive 收件匣 client、service account reader、GitHub token）
- [ ] 8.2 MyLinuxPool：worker 內的 opencode 上傳器定期執行；刪除 worker 前先跑最後一次同步並等提交流程完成，沒同步完就明確告知
- [ ] 8.3 MyLinuxPool：把手機 App 與 Mac 同步程式登記成 profile
- [ ] 8.4 MyAiEntry：Session 上傳器（排除 SSH 私鑰、LLM key、PAT）、裝置上的原生 OAuth、交接動作（同步 → 寫交接單 → 指定職務 → 建接續 Link → 觸發並等待提交流程）
- [ ] 8.5 MyBrain PR：案件主題檔加不變的 `id`、更新 OKF 規範與 `validate.py`；更新 2026-09-06 那組已被推翻的判準筆記

## 9. 端到端驗收

- [ ] 9.1 交接情境：手機上的 S1 → 交接給員工 → 員工以指定的職務接續 → 產出登錄進 Foundry → 刪除 worker 後，Agora 與 Foundry 的內容都還在
- [ ] 9.2 參考情境：員工用搜尋找到另一個 Session、讀它的閱讀版，留下參考 Link，被參考的 Session 沒有任何變化
- [ ] 9.3 反向測試：員工推不了 Atelier / MyBrain / Agora 的真本、刪不了舊版本；自填的產生者被忽略
- [ ] 9.4 家裡離線情境：所有 worker 都離線時，手機的 Session 仍然能進收件匣，並被提交流程收進 Agora
