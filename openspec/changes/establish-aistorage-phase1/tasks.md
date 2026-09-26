執行方式：PM 負責拆解與驗收；每個任務由實作與測試兩位隊員分開完成，再經 review 審查。第 1 組是閘門：技術驗證報告經使用者確認 go 之前，不開始第 2 組之後的任務。期 1 之外的事項一律在 `docs/backlog.md`，不在這裡。所有 Drive 操作都在 AiStorage 專用帳號的測試資料夾進行；驗證只用新開的測試 Session，不碰真實的 Session 內容。

## 1. 技術驗證（閘門）

- [ ] 1.1 建立自己的 Google OAuth client，設成已發佈（個人使用例外），記錄建立步驟與 token 期限行為（含提交流程要的 restricted `drive` scope 在「已發佈、未驗證」狀態下的行為）；確認專用帳號的配額來自家庭共用的 5TB，rclone 與 Drive API 在這個帳號下正常
- [ ] 1.2 在專用帳號的 Drive 上用 git-annex 內建的 git-remote-annex 加 rclone special remote 建一個測試 repo；Mac 上的 Ubuntu 容器（arm64）與 Actions runner（amd64）都要 clone，確認 commit / push / log / diff / revert 可行、push 中斷後能恢復；push 前重讀遠端 manifest、與 clone 時比對的做法可行並量耗時；記下兩邊 git-annex 與 rclone 的安裝方式與版本
- [ ] 1.3 驗證抹除：`git annex drop --force`、`git annex forget`、改寫歷史並重推 bundle 之後，被抹除的內容在目前版本、git 歷史、bundle、Drive 自己保留的舊 revision 與垃圾桶裡都找不到（必要時刪檔重建而不是覆寫，再永久刪除）
- [ ] 1.4 驗證 `drive.file` 的隔離：client A 看不到、刪不到、改不到 client B 建的檔案；A 能不能在 B 建立的資料夾、使用者（專用帳號）建立的資料夾、repo 資料夾、讀取視圖資料夾裡建檔，以及在這些地方建同名檔會怎樣；分別測同一個 GCP project 與不同 project 的情況；結論要寫出收件匣怎麼建立（bootstrap），以及固定檔案 id、原地更新的 manifest 可行；**client 能在 repo 資料夾建檔、又找不到 rclone／git-remote-annex 側的對策，就是 no-go**
- [ ] 1.5 驗證 service account 被分享成 reader 時，能讀取該資料夾（含 `git clone annex::…`、依 annex key 直接取得 special remote 裡的物件），不能寫入或刪除
- [ ] 1.6 驗證只有「Actions 寫入」（沒有 contents 權限）的 fine-grained token：能觸發 workflow_dispatch，不能推 contents，不能修改 workflow 檔案；另外確認它能不能 enable／disable workflow、能不能 cancel run（影響管理操作的錯開方式）
- [ ] 1.7 驗證 opencode（只用驗證時新開的測試 Session）：在 Mac 上的 Ubuntu 容器裡定期 `opencode export` 可行；能以內容判斷有沒有新內容；同一個 Session 重新匯出時既有訊息的位置不變（接續點靠它）；壓縮對話之後的行為；有沒有可信的結束或封存訊號；skill 能不能取得目前的 Session id、能不能用指定內容啟動新 Session；子 Session（task 工具）與 fork 匯出時的樣子；匯出內容不含帳號與憑證
- [ ] 1.8 量測提交流程：用合成資料（約 500 次 push、1 GB 內容）量 runner 上 clone 與 push 的耗時；連續觸發三次以上，記錄排隊與被取消的 run；收件匣是空的時能不能不 clone 就結束；寫出每月 Actions 分鐘數的估算（每 3 小時一次約 240 次/月，加上主動觸發）
- [ ] 1.9 撰寫技術驗證報告（逐項 pass / fail，附證據），並把驗證時建立的實體資源（專用帳號、Drive 資料夾、OAuth client、service account、token、測試 repo，不含秘密的值）記進 `docs/resources.md`，交使用者判斷 go / no-go；1.7 的每一個子項分開判定 pass / fail

## 2. 共通 schema

- [ ] 2.1 定義共通 metadata 的 JSON Schema：分成「收件匣項目」（寫入者提供，不含產生者）與「真本項目」（提交流程蓋章後）兩份；六個欄位，其中時間明確分成建立與最後更新，並說明它們跟快照時間、提交時間的關係；id 的產生規則（Session 用 `<來源應用>:<來源 Session id>`，其他項目用 `<型態>:<ULID>`；同 id 同產生者同型態＝更新，否則＝撞號拒絕）；擴充欄位的規則與預留的 `role`、`role_version`；附驗證器與測試（缺少必填欄位時拒絕、不認得的欄位忽略）
- [ ] 2.2 定義收件匣項目的格式：原始紀錄＋sidecar（含快照時間、狀態與停止時間、原始紀錄的內容雜湊；sidecar 最後寫，構成不可分的單位）、交接單（被接續的 Session、接續點、內容）、認領、參考 Link（讀到的快照時間）、改寫提案、Foundry 登錄項目；附格式驗證器
- [ ] 2.3 定義「收件匣（資料夾 id）↔ profile」的對照與授權規則、收件匣的建立步驟（由該 profile 的 client 自己建立，再登記），以及期 1 的 profile（Mac opencode、測試用 profile，各自的讀取身分）
- [ ] 2.4 定義閱讀版的共通格式（訊息、工具呼叫、圖片的表示方式、接續點怎麼指到位置），附範例；所有轉換器都照這份格式輸出

## 3. 提交流程：Agora 路徑

- [ ] 3.1 建立提交流程的 workflow 骨架：定時（預設每 3 小時）、workflow_dispatch（不接受會進 shell 的自由字串輸入）、concurrency group、secrets 的讀取方式、收件匣是空的就不 clone 直接結束；確認被任意觸發時也安全
- [ ] 3.2 掃描收件匣、驗證項目與授權；只看到一半的項目留到下一輪；不合格的項目把拒絕原因發佈到讀取視圖，不進真本；push 成功之後才刪除已處理的收件匣項目；被拒收與逾時的孤兒項目在 24 小時後刪除並永久刪除；push 前重讀遠端 manifest，跟 clone 時不同就中止
- [ ] 3.3 依來源收件匣蓋產生者章，忽略寫入者自填的產生者；以收件匣檔案的建立時間當快照時間的上限
- [ ] 3.4 以內容雜湊判斷重複；同一內容重複上傳不產生新 commit，重跑保持冪等
- [ ] 3.5 opencode 轉換器：由原始紀錄產生閱讀版
- [ ] 3.6 Claude Code 轉換器（只用於手動匯入）：由 Claude Code 的 jsonl 產生閱讀版
- [ ] 3.7 處理交接單與認領：同一次執行裡先收原始紀錄、再收交接單與認領，接續點超過已收進真本位置的交接單拒絕；交接單成為項目；認領時建立接續 Link（新 Session → 被接續的 Session，記接續點）；一張交接單只能被認領一次，重複認領拒絕；一個 Session 可以認領多張（統合）
- [ ] 3.8 處理參考 Link：記錄讀到的快照時間；目標可以是並行中的 Session，允許互相參考；同一對 Session 只保留一條，更新為最新的快照時間
- [ ] 3.9 更新 Session 狀態：停止中依明確宣告（或可信訊號），記停止時間；宣告後又有新內容就回到運作中
- [ ] 3.10 處理改寫提案：做成新 commit，並驗證沒有改變任何內容的位置（接續點仍然有效）
- [ ] 3.11 單一 Session 手動匯入工具：把現存的 opencode 與 Claude Code Session 包成收件匣項目，重複匯入對到同一個 id

## 4. 讀取視圖與讀取介面

- [ ] 4.1 每次提交後增量發佈讀取視圖：只重寫有變動的檔案，附帶世代號的 manifest（記檔案 id）；內容包括 metadata、閱讀版、Session Link 與反向索引、交接單、快照時間、拒絕原因、產出目錄
- [ ] 4.2 建立 SQLite FTS5 索引（trigram 分詞），用中日文關鍵字驗證命中
- [ ] 4.3 Agora 的讀取介面（函式庫與 CLI，arm64 與 amd64 都能跑）：條件篩選、全文搜尋、兩者組合；讀取單一 Session（閱讀版、交接單、兩個方向的 Session Link，接續時只取接續點之前）；列出尚未被認領的交接單；依 manifest 的檔案 id 定位、依世代號快取；讀取授權以要素為單位，沒有讀取權的身分會被拒絕，而不是回傳空結果
- [ ] 4.4 新鮮度：每筆結果附快照時間；指定新鮮度而未達時照樣回傳並附警告；停止中的 Session（明確宣告過）視為符合；確認讀取不會觸發任何同步或提交流程
- [ ] 4.5 重建指令：從原始紀錄重建全部閱讀版與索引，並比對重建前後的結果一致

## 5. Mac 上的 opencode

- [ ] 5.1 opencode 的執行容器：Mac 上的 Ubuntu 容器（colima），裝 opencode 與同步器；只掛載白名單上的憑證（Mac opencode profile 的憑證、LLM provider 金鑰、需要時限定單一測試 repo 的 token）與自己的工作目錄；可以同時開多個容器，彼此不共用工作目錄與 opencode 的本機資料；確認容器內碰不到白名單以外的憑證
- [ ] 5.2 opencode 同步器：定期（預設每 10 分鐘）匯出有變化的 Session，連同 sidecar 放進收件匣；偵測到宣告停止後又有新內容時，立刻同步並提交
- [ ] 5.3 「同步並提交」指令：同步指定的 Session（可以連同交接單、認領等項目）、觸發提交流程，等到讀取介面看得到這次放進去的每一個項目或它的拒絕原因（顯示進度，逾時明確告知）
- [ ] 5.4 給 opencode 用的 skill：分裂（同步，為每一份工作各寫一張交接單，一起提交）、交出末端（統合前，同步並寫一張交接單，一起提交）、認領（找出並認領交接單，同步並提交，等讀取介面確認 Link 屬於自己才開工、看到拒絕就停下；接著先讀交接單，再讀接續點之前的閱讀版）、統合（一個新 Session 認領多張交接單）、參照（用讀取介面找與讀，留下參考 Link，把快照時間與警告帶進上下文）、宣告停止

## 6. 管理操作

- [ ] 6.1 抹除腳本（只限你本人的管理憑證），附驗證測試：抹除的內容在目前版本、git 歷史、Drive 的舊 revision 與垃圾桶、讀取視圖與索引、收件匣都找不到，並留下抹除紀錄；列出已知的 clone 與快取提醒處理；確認 Mac opencode 的憑證無法執行抹除
- [ ] 6.2 回滾腳本：把某個 Session 退回指定版本，閱讀版隨之重建
- [ ] 6.3 健康檢查：OAuth refresh token 是否有效、提交流程的 workflow 有沒有被停用、Actions 分鐘數用量、定時提交的實際間隔、搜尋索引大小與讀取視圖檔案數（對照 design D5 的門檻）
- [ ] 6.4 復原手冊與演練：Drive 上的 repo 被刪除時，從任一個 git clone 重建並重新推回 Drive，實際演練一次並記錄步驟
- [ ] 6.5 管理操作與提交流程錯開：6.1、6.2、6.4 的腳本先停用提交流程的 workflow、確認沒有執行中的 run，push 前重讀遠端 manifest，做完再恢復；驗收時與提交流程同時觸發、以及在管理操作期間用住民的 token 重新啟用 workflow，確認都不會互相覆蓋

## 7. Foundry（最小）

- [ ] 7.1 在 Drive 上建立 Foundry 的 git-annex repo（與 Agora 分開）
- [ ] 7.2 產出目錄的項目格式，以及透過收件匣登錄（原處產出是連結型項目，記錄產出它的 Session）
- [ ] 7.3 收容產出入庫：經提交流程，單檔上限 100 MB；超過上限明確拒收並發佈原因
- [ ] 7.4 產出目錄進讀取視圖；Foundry 的讀取介面可以依型態、所屬案件、產生者、時間、產出它的 Session 查詢，依相同的新鮮度規則附快照時間，並能依 annex key 取得收容產出的本體

## 8. 需求設計與對外工單

- [ ] 8.1 Atelier 需求設計 `docs/atelier/`：需要哪些部門（profile）、需要哪些職能（職務），只到需求層級；與 `docs/atelier/role.md`、`external-copy.md` 保持一致
- [ ] 8.2 MyLinuxPool 工單 `docs/tickets/mylinuxpool.md`：依期 1 實作的結果更新（profile 憑證的名稱與形狀、收件匣上傳格式、觸發並等待提交流程的方式、能力清單格式、驗證過的 CPU 架構），經使用者同意後交給 MyLinuxPool 那邊

## 9. 端到端驗收

每個 Session 都在各自的容器裡執行，不共用工作目錄與 opencode 的本機資料；接手或參照的一方唯一的資訊來源是讀取介面。

- [ ] 9.1 分裂（1→n）：S1 剛講完幾句就同步、寫兩張交接單，一起提交（一次提交流程）；兩個新 Session 各自認領一張，成為 S2、S3；接續點包含最後那幾句；S2、S3 讀得到兩張交接單；S1 照常繼續、不受影響
- [ ] 9.2 統合（n→1）：S2、S3 各自交出末端；S4 認領兩張交接單，讀到兩者接續點之前的內容；S4 產出一份報告登錄進 Foundry，產出目錄查得到它、記錄產出它的是 S4，本體取得到
- [ ] 9.3 相互參照（n↔m）：並行中的 S2、S3 反覆用讀取介面讀對方，各自只有一條指向對方的參考 Link、記最新的快照時間；指定新鮮度而對方未主動提交時得到警告；對方同步並提交後讀到新的快照；被參考的一方沒有任何變化，讀取沒有觸發任何寫入
- [ ] 9.4 反向測試：Mac opencode 的憑證推不了 Agora / Foundry 的真本、刪不了舊版本、寫不進別的收件匣；自填的產生者被忽略；不在授權規則裡的 profile 被拒收；重複認領被拒絕
- [ ] 9.5 持久性：刪掉所有 opencode 容器與它們的本機資料之後，Agora 與 Foundry 的內容都還在，讀取介面照常讀得到
