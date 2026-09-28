執行方式：PM 負責拆解與驗收；每個任務由實作與測試兩位隊員分開完成，再經 review 審查。第 1 組是閘門：技術驗證報告經使用者確認 go 之前，不開始第 2 組之後的任務。期 1 之外的事項一律在 `docs/backlog.md`，不在這裡。所有 Drive 操作都在 AiStorage 專用帳號的測試資料夾進行；驗證只用新開的測試 Session，不碰真實的 Session 內容。

## 1. 技術驗證（閘門）

2026-09-27 使用者判定 go-with-design-changes（`docs/spike/report.md`）。1.7j（睡眠喚醒後的容器時鐘）未完成、不擋 go，之後與使用者一起補測。

- [x] 1.1 建立自己的 Google OAuth client，設成已發佈（個人使用例外），記錄建立步驟與 token 期限行為（含提交流程要的 restricted `drive` scope 在「已發佈、未驗證」狀態下的行為）；確認專用帳號的配額來自家庭共用的 5TB，rclone 與 Drive API 在這個帳號下正常
- [x] 1.2 在專用帳號的 Drive 上用 git-annex 內建的 git-remote-annex 加 rclone special remote 建一個測試 repo；Mac 上的 Ubuntu 容器（arm64）與 Actions runner（amd64）都要 clone，確認 commit / push / log / diff / revert 可行、push 中斷後能恢復；push 前重讀遠端 manifest、與 clone 時比對的做法可行並量耗時；記下兩邊 git-annex 與 rclone 的安裝方式與版本
- [x] 1.3 驗證抹除：`git annex drop --force`、`git annex forget`、改寫歷史並重推 bundle 之後，被抹除的內容在目前版本、git 歷史、bundle、Drive 自己保留的舊 revision 與垃圾桶裡都找不到（必要時刪檔重建而不是覆寫，再永久刪除）
- [x] 1.4 驗證 `drive.file` 的隔離：client A 看不到、刪不到、改不到 client B 建的檔案；A 能不能在 B 建立的資料夾、使用者（專用帳號）建立的資料夾、repo 資料夾、讀取視圖資料夾裡建檔，以及在這些地方建同名檔會怎樣；分別測同一個 GCP project 與不同 project 的情況；結論要寫出收件匣怎麼建立（bootstrap），以及固定檔案 id、原地更新的 manifest 可行；**client 能在 repo 資料夾建檔、又找不到 rclone／git-remote-annex 側的對策，就是 no-go**
- [x] 1.5 驗證 service account 被分享成 reader 時，能讀取該資料夾（含 `git clone annex::…`、依 annex key 直接取得 special remote 裡的物件），不能寫入或刪除
- [x] 1.6 驗證只有「Actions 寫入」（沒有 contents 權限）的 fine-grained token：能觸發 workflow_dispatch，不能推 contents，不能修改 workflow 檔案；另外確認它能不能 enable／disable workflow、能不能 cancel run（影響管理操作的錯開方式）
- [x] 1.7 驗證 opencode（只用驗證時新開的測試 Session）：在 Mac 上的 Ubuntu 容器裡定期 `opencode export` 可行；能以內容判斷有沒有新內容；同一個 Session 重新匯出時既有訊息的位置不變（接續點靠它）；壓縮對話之後的行為；有沒有可信的結束或封存訊號；skill 能不能取得目前的 Session id、能不能用指定內容啟動新 Session；子 Session（task 工具）與 fork 匯出時的樣子；匯出內容不含帳號與憑證
- [x] 1.8 量測提交流程：用合成資料（約 500 次 push、1 GB 內容）量 runner 上 clone 與 push 的耗時；連續觸發三次以上，記錄排隊與被取消的 run；收件匣是空的時能不能不 clone 就結束；寫出每月 Actions 分鐘數的估算（每 3 小時一次約 240 次/月，加上主動觸發）
- [x] 1.9 撰寫技術驗證報告（逐項 pass / fail，附證據），並把驗證時建立的實體資源（專用帳號、Drive 資料夾、OAuth client、service account、token、測試 repo，不含秘密的值）記進 `docs/resources.md`，交使用者判斷 go / no-go；1.7 的每一個子項分開判定 pass / fail

## 2. 共通 schema

- [x] 2.1 定義共通 metadata 的 JSON Schema：分成「收件匣項目」（寫入者提供，不含產生者）與「真本項目」（提交流程蓋章後）兩份；六個欄位，其中時間明確分成建立與最後更新，並說明它們跟快照時間、提交時間的關係；id 的產生規則（Session 用 `<來源應用>:<來源 Session id>`，其他項目用 `<型態>:<ULID>`；同 id 同產生者同型態＝更新，否則＝撞號拒絕）；擴充欄位的規則與預留的 `role`、`role_version`；附驗證器與測試（缺少必填欄位時拒絕、不認得的欄位忽略）
- [x] 2.2 定義收件匣項目的格式：原始紀錄＋sidecar＋分離式簽章檔（sidecar 含快照時間、狀態與停止時間、原始紀錄的內容雜湊、`in_progress`、`parent_id`（子 Session）、簽章檔簽 sidecar 的原始位元組、最後寫，構成不可分的單位）、交接單（被接續的 Session、接續點＝快照識別＋最後一則已完成訊息的 message id、內容）、認領、參考 Link（讀到的快照時間）、改寫提案、Foundry 登錄項目；附格式驗證器
- [x] 2.3 定義身分的設定（design D3）：簽章金鑰 ↔ profile 的登錄（公開金鑰、輪替、撤銷）、收件匣資料夾 id 的登記（收件匣由 worker 的 client 建在專用帳號根目錄；位置不證明產生者）、期 1 的 profile（Mac opencode、測試用 profile）；worker 共用的 Drive 憑證與讀取身分，committer 的獨立 GCP project
- [x] 2.4 定義閱讀版的共通格式（訊息、工具呼叫、圖片的表示方式、接續點怎麼指到位置、opencode 的 `info.revert` 之後的訊息怎麼處理、子 Session 的呈現），附範例；所有轉換器都照這份格式輸出
- [x] 2.5 身分佈置（你在 Console 操作、PM 以 CLI 協助）：建立 pin repo 與它的 deploy key（放進 MyAiStorage 的 Actions secrets）；為提交流程建立獨立的 GCP project 與 OAuth client（正式版、`drive` scope），重做技術驗證 1.1 的核心檢查（smoke test、scope、第 8 天 refresh token 複查）；worker 共用的 OAuth client 與讀取身分；更新 `docs/resources.md`；把舊的 committer client 撤銷
- [x] 2.6 原始紀錄放 git 還是 annex：用真實大小的測試 Session 匯出（例如 10 個 Session、每個 200 次同步）模擬一個月，量 consolidate 時的 push 耗時與歷史大小，決定 `annex.largefiles` 與 `annex.max-git-bundles` 的值（design Risks）

## 3. 提交流程：Agora 路徑

- [ ] 3.1 建立提交流程的 workflow 骨架：定時（間隔可以設定，預設每 6 小時）、workflow_dispatch（不接受任何自由輸入，也供你手動觸發）、concurrency group、secrets 的讀取方式；開頭檢查 `github.sha == main HEAD` 與 `github.ref == refs/heads/main`；收件匣是空的（只算形狀符合格式的檔）就不 clone 直接結束；log 只輸出 id、計數與耗時，repo 的 log 保留天數設到最短（repo 設定，由你操作）；整個提交流程是單一 job，`GITHUB_TOKEN` 只有 contents 讀取；確認被任意觸發、rerun 舊 run、用其他分支觸發時都安全
- [x] 3.2 完整性機制（design D2、ADR 0008）：釘選值存在獨立的 pin repo（`MyAiStorage-pin`，不放 workflow；提交流程以只對它有寫入權的 deploy key 寫入）（全部 ref、manifest 內容雜湊、active 與已移除 bundle 清單、annex key 集合），兩階段寫入（待定→正式），只由提交流程依觀測到的遠端狀態寫入；每一輪先結算待定（內容重放，全程唯讀）再清掃；只看 metadata（`sha256Checksum`）的清掃，逐層檢查同名資料夾、遞迴，讀不到就中止、不做移動，移動失敗也中止；manifest 依角色判定（`.bak` 只能等於目前或上一個正式 manifest）、解析 active 與已移除兩個集合；bundle 回收（已移除清單依 file id 永久刪除），並驗證回收之後 clone、push、consolidate 仍正常；隔離資料夾保存 7 天；clone 後核對全部 ref；「只剩 `.bak`」的恢復規則。起點不要用 spike 的 `f3_*` 腳本（它們沿用舊的信任規則）
- [x] 3.3 掃描收件匣、驗證項目、驗章（沒有簽章或驗章失敗一律拒收），依驗章通過的金鑰蓋產生者章，忽略寫入者自填的產生者；以收件匣檔案的建立時間當快照時間的上限；只看到一半的項目留到下一輪；不合格的項目把拒絕原因發佈到讀取視圖，不進真本；push 後以 `git ls-remote` 確認全部 ref、重新讀 manifest 比對 active 清單，通過才刪除已處理的收件匣項目（push 會靜默失敗）；被拒收與逾時的孤兒項目在 24 小時後刪除並永久刪除；push 前預檢：遠端 manifest 的內容雜湊仍等於正式釘選值（只查名稱符合 GITMANIFEST 的檔，出錯就中止）
- [x] 3.4 以內容雜湊判斷重複；同一內容重複上傳不產生新 commit，重跑保持冪等
- [x] 3.5 opencode 轉換器：由原始紀錄產生閱讀版
- [x] 3.6 Claude Code 轉換器（只用於手動匯入）：由 Claude Code 的 jsonl 產生閱讀版
- [x] 3.7 處理交接單與認領：同一次執行裡先收原始紀錄、再收交接單與認領，接續點指向的快照或 message id 不在已收進真本的原始紀錄裡的交接單拒絕；交接單成為項目；認領時建立接續 Link（新 Session → 被接續的 Session，記接續點）；一張交接單只能被認領一次，重複認領拒絕；一個 Session 可以認領多張（統合）
- [x] 3.8 處理參考 Link：記錄讀到的快照時間；目標可以是並行中的 Session，允許互相參考；同一對 Session 只保留一條，更新為最新的快照時間
- [x] 3.9 更新 Session 狀態：停止中依明確宣告（opencode 的 `time.archived > 0` 而且封存後沒有新訊息），停止時間用同步器觀測到的時間；宣告後又有新內容就回到運作中
- [x] 3.10 （2026-09-27 使用者決定期 1 不做改寫，移到待辦）收到改寫提案一律拒收（`rewrite_not_supported`），原因發佈在讀取視圖；原本的內容：處理改寫提案：做成新 commit，並驗證已經成立的接續點仍然有效（接續點釘在快照上）；來源端刪掉訊息屬於原始紀錄的新版本，不是改寫提案，不拒收
- [x] 3.11 單一 Session 手動匯入工具：把現存的 opencode 與 Claude Code Session 包成收件匣項目，重複匯入對到同一個 id

## 4. 讀取視圖與讀取介面

- [x] 4.1 每次提交後增量發佈讀取視圖：只重寫有變動的檔案，附帶世代號的 manifest（記檔案 id，只用 API 的 `files.update` 原地更新）；讀取視圖資料夾也納入清掃；內容包括 metadata、閱讀版、每個 Session 最新的原始紀錄內容雜湊（同步器以它比對）、Session Link 與反向索引、交接單、快照時間、拒絕原因、產出目錄
- [x] 4.2 建立 SQLite FTS5 索引（trigram 分詞），用中日文關鍵字驗證命中
- [x] 4.3 Agora 的讀取介面（函式庫與 CLI，arm64 與 amd64 都能跑）：條件篩選、全文搜尋、兩者組合；讀取單一 Session（閱讀版、交接單、兩個方向的 Session Link，接續時從被釘住的快照只取接續點之前）；列出尚未被認領的交接單；依 manifest 的檔案 id 定位、依世代號快取；讀取授權以要素為單位，沒有讀取權的身分會被拒絕，而不是回傳空結果
- [x] 4.4 新鮮度：每筆結果附快照時間；指定新鮮度而未達時照樣回傳並附警告；停止中的 Session（明確宣告過）視為符合；確認讀取不會觸發任何同步或提交流程
- [x] 4.5 重建指令：從原始紀錄重建全部閱讀版與索引，並比對重建前後的結果一致

## 5. Mac 上的 opencode

- [x] 5.1 opencode 的執行容器：Mac 上的 Ubuntu 容器（colima），裝 opencode 與同步器；只掛載白名單上的憑證（worker 的 Drive 憑證、讀取身分、Mac opencode 的簽章金鑰、LLM provider 金鑰、需要時限定單一測試 repo 的 token；全部唯讀掛在 `/secrets/`，conf 寫容器內路徑，啟動時複製一份可寫的暫存 rclone conf；LLM key 以檔案引用、不落進 `/work` 的 `auth.json`）與自己的工作目錄；取得 Session id 的 plugin 裝在 image 的唯讀路徑；在實際的住民容器上錄能力邊界；可以同時開多個容器，彼此不共用工作目錄與 opencode 的本機資料；確認容器內碰不到白名單以外的憑證
- [x] 5.2 opencode 同步器：定期（預設每 10 分鐘）用 API 的全域清單（含子 Session）列舉，匯出有變化的 Session，連同簽章的 sidecar 放進收件匣；以 Agora（讀取視圖）比對決定要不要上傳，已上傳未提交的視為等待中，下一次提交後仍沒有才補傳；生成中的快照記 `in_progress`；偵測到宣告停止後又有新內容時，立刻同步並提交；用「子代理再開子代理」驗一次遞迴
- [x] 5.3 「同步並提交」指令（觸發時一律帶 `--ref main`；逾時的訊息提示「提交流程可能被停用或遭到注入」）：同步指定的 Session（可以連同交接單、認領等項目）、觸發提交流程，等到讀取介面看得到這次放進去的每一個項目或它的拒絕原因（顯示進度，逾時明確告知）
- [x] 5.4 給 opencode 用的 skill：分裂（同步，為每一份工作各寫一張交接單，一起提交）、交出末端（統合前，同步並寫一張交接單，一起提交）、認領（找出並認領交接單，同步並提交，等讀取介面確認 Link 屬於自己才開工、看到拒絕就停下；接著先讀交接單，再讀接續點之前的閱讀版）、統合（一個新 Session 認領多張交接單）、參照（用讀取介面找與讀，留下參考 Link，把快照時間與警告帶進上下文）、宣告停止（以 API 設 `time.archived`）；認領與宣告停止只能在主 Session 執行，子 Session 呼叫時拒絕
- [x] 5.5 技術驗證：opencode 匯入 session。開頭位元組相同、1→n 共用開頭、n→1 能否合併（`docs/spike/session-import.md`）
- [ ] 5.6 實作 `agora` CLI（find／show／read／handoff／checkout）與起點包格式：起點可以是 `handoff:<id>` 或 `<session>[@<訊息>]`；原始紀錄原封不動放進起點包；起點是交接單時登記認領並等確認，被拒就不產出；n→1 最長的一段放最前面，超過目標模型的 context 上限就明確拒絕（design D10）
- [ ] 5.7 實作轉接器 `agora-opencode`：`load <起點包>` 截斷、重編 id、在目標專案目錄執行 `opencode import`，印出新 session id；同一個 coding agent 之間開頭位元組相同
- [ ] 5.8 skill／plugin 改成 `agora_find`、`agora_read`、`agora_handoff`、`agora_checkout`，拿掉 claim；宣告停止與 checkout 只能在主 Session 執行

## 6. 管理操作

- [x] 6.1 抹除腳本（只限你本人的管理憑證，做法見 design D2「改寫與抹除」），附驗證測試：部分抹除（保留其他 Session 與 annex 物件）；抹除的內容在目前版本、git 歷史、bundle、Drive 的舊 revision 與垃圾桶、讀取視圖與索引、收件匣、隔離資料夾、Actions run log 都找不到，並留下抹除紀錄（含每一代 repo uuid）；後置條件（remote 的 bundle 集合＝manifest 的 active 集合、垃圾桶沒有任何一代 uuid 的 bundle）；抹除後由管理者重建正式釘選值；目標以 file id 指定、先 dry-run、永久刪除前以 API 確認；查驗腳本修好 spike 找到的兩個會放行的漏洞（remote 上不在 manifest 的 bundle、cat-file 失敗）；列出已知的 clone 與快取提醒處理；確認 Mac opencode 的憑證無法執行抹除
- [x] 6.2 回滾腳本：把某個 Session 退回指定版本，閱讀版隨之重建
- [x] 6.3 健康檢查：OAuth refresh token 是否有效、提交流程的 workflow 有沒有被停用、被取消的 run 數、Actions 分鐘數用量（連續兩週超過 300 就提醒調整間隔）、定時提交的實際間隔、距離上一次成功提交的時間、連續中止的輪數（連續 N 輪就通知你）、隔離資料夾的增長、家庭共用的整體配額（`limit - usage`）、搜尋索引大小與讀取視圖檔案數（對照 design D5 的門檻）；升級 opencode 時重驗 prune 不清除匯出內容
- [x] 6.4 復原手冊與演練：Drive 上的 repo 被刪除時，從任一個 git clone 重建並重新推回 Drive，重建釘選值，實際演練一次並記錄步驟；手冊記每個 repo 的完整 clone URL，以及「只剩 `.bak`」「主 manifest 與 `.bak` 都不在」時的恢復方式（技術驗證 1.2m）
- [ ] 6.5 管理操作與提交流程錯開：6.1、6.2、6.4 的腳本先停用提交流程的 workflow、確認沒有執行中的 run，push 前重讀遠端 manifest，重建釘選值期間暫停清掃，做完再恢復；驗收時與提交流程同時觸發、以及在管理操作期間用住民的 token 重新啟用 workflow，確認都不會互相覆蓋

## 7. Foundry

- [x] 7.1 移除 git-annex 版 Foundry（程式、測試、測試 Drive 殘留），提交流程只處理 Agora（ADR 0009）

以下是原本的 7.x，已被 ADR 0009 取代，留著看歷史：

- [x] ~~舊 7.1 在 Drive 上建立 Foundry 的 git-annex repo（與 Agora 分開）~~（已被 ADR 0009 取代）
- [x] ~~舊 7.2 產出目錄的項目格式，以及透過收件匣登錄（原處產出是連結型項目，記錄產出它的 Session）~~（已被 ADR 0009 取代）
- [x] ~~舊 7.3 收容產出入庫：經提交流程，單檔上限 100 MB；超過上限明確拒收並發佈原因~~（已被 ADR 0009 取代）
- [x] ~~舊 7.4 產出目錄進讀取視圖；Foundry 的讀取介面可以依型態、所屬案件、產生者、時間、產出它的 Session 查詢，依相同的新鮮度規則附快照時間，並能依 annex key 取得收容產出的本體~~（已被 ADR 0009 取代）

## 8. 需求設計與對外工單

- [x] 8.1 Atelier 需求設計 `docs/atelier/`：需要哪些部門（profile）、需要哪些職能（職務），只到需求層級；與 `docs/atelier/role.md`、`external-copy.md` 保持一致
- [x] 8.2 MyLinuxPool 工單 `docs/tickets/mylinuxpool.md`：依期 1 實作的結果更新（worker 共用的 Drive 憑證與讀取身分、每個 profile 的簽章金鑰的發放與輪替、`/secrets/` 的掛載形狀、收件匣上傳格式、觸發並等待提交流程的方式、能力清單格式、驗證過的 CPU 架構），經使用者同意後交給 MyLinuxPool 那邊

## 9. 端到端驗收

每個 Session 都在各自的容器裡執行，不共用工作目錄與 opencode 的本機資料；接手或參照的一方唯一的資訊來源是讀取介面。

- [ ] 9.1 分裂（1→n）：S1 剛講完幾句就用 `agora handoff` 同步、寫兩張交接單，一起提交（一次提交流程）；每張各跑一次 `agora checkout handoff:Hx` 加 `agora-opencode load`，建出 S2、S3，兩者的開頭與 S1 在接續點之前的內容位元組相同；接續點包含最後那幾句；S2、S3 讀得到兩張交接單；S1 照常繼續、不受影響；之後對 S1 做 /undo 再重新輸入，S2 承接的內容不變
- [ ] 9.2 統合（n→1）：S2、S3 各自交出末端；`agora checkout handoff:H2 handoff:H3` 加 `agora-opencode load` 建出 S4，S4 開頭就帶著兩者接續點之前的內容（最長的一段在最前面），並有兩條接續 Link；超過 context 上限時 checkout 明確拒絕
- [ ] 9.3 相互參照（n↔m）：並行中的 S2、S3 反覆用讀取介面讀對方，各自只有一條指向對方的參考 Link、記最新的快照時間；指定新鮮度而對方未主動提交時得到警告；對方同步並提交後讀到新的快照；被參考的一方沒有任何變化，讀取沒有觸發任何寫入
- [ ] 9.4 反向測試：Mac opencode 的憑證推不了、改不了、刪不了 Agora 的真本與舊版本；沒有簽章或簽章不符的收件匣項目被拒收，自填的產生者被忽略；兩次 checkout 同一張交接單時，被拒的一方不產出起點包；注入（換掉 main、真 main＋偽造 `git-annex` 分支、多餘 ref、同名 DoS、上一版 manifest 冒充目前版本、名稱與內容相符但沒被引用的 annex 物件、prepare 之後 push 之前的注入、多層同名資料夾）都被偵測，偽造的歷史不會成為真本，清掃後能恢復；push 後、轉正前被取消，下一輪自動恢復，而且真的新 manifest 與新 bundle 沒有被隔離；在 GITMANIFEST 替換空檔 kill 提交流程，下一輪自動恢復；consolidate 之後的 prepare 與 push 正常；暫時性的讀取錯誤不會移動任何真檔；被刪掉的收件匣項目由同步器補傳；撤銷 Mac opencode 的簽章金鑰之後，它的項目一律被拒收、其他 profile 不受影響
- [ ] 9.5 持久性：刪掉所有 opencode 容器與它們的本機資料之後，Agora 的內容都還在，讀取介面照常讀得到；子代理產生的子 Session 在 Agora 裡查得到、記有母 Session
