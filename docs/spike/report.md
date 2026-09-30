# 技術驗證報告（tasks 1.1〜1.8）

> 狀態：**定稿**（2026-09-27）。使用者判定 **go-with-design-changes**（見 `docs/decision-log.md`「Spike 1.9 go decision」）。只剩 1.7j 未完成，不擋 go。
> 彙整規則依 `docs/spike/test-plan.md` 第 1.9 節。證據都在 `docs/spike/evidence/`。查核狀態：1.1〜1.8 的主要證據與各項補測（1.2m、1.3-followup、1.4f2〜1.4f4、1.6h4、1.7 的更正）都經過 review（架構師）查核；1.4f5 與 1.3 收尾也已查核。

## 一、結論

**判定：go-with-design-changes**（使用者已確認）。條件：
1. 1.8 的計費分鐘數：依 review 的估算，真流程的非空提交是 2〜3 個計費分鐘。使用者選擇排程間隔可設定、預設 6 小時、可手動觸發（Q5），估計 276〜372 分鐘／月，可能落在 caveat，6.3 要監控實際用量。清掃只看 metadata（review-1.4f3 H4）**已驗證**：在 1.8 等級的 repo（517 個檔）上，runner 中位數 3.05 s、只下載 2 個 manifest，1 GB 物件也有 `sha256Checksum` → 交給 3.2 實作，不再是 go 的前提。
2. 第四節的待辦不擋 go（1.7j 之後與使用者一起補測）。
3. 第三節的設計變更寫回 design、ADR、tasks 之後，才開始第 2 組。

唯一碰到 no-go 條件的是 1.4f（`drive.file` 能在 repo 資料夾建檔）。使用者已決定改設計，並接受殘餘風險（見 `docs/decision-log.md`「Spike 1.4f decision」），對策已在 spike 內實測（1.4f2〜1.4f4）。

## 二、逐項判定

類別：go／go-with-caveat／需要改設計（go-with-design-changes）／no-go。

| 子項 | 結果 | 類別 | 證據 | 前提與備註 |
|---|---|---|---|---|
| 1.1 OAuth client、正式版、配額 | pass | go-with-caveat | `1.1-oauth-clients.md` | 7 天 refresh token 過期要在 2026-10-04 前後複查。committer 搬到新 project 之後，要對新的 client 重做 smoke test、scope 檢查與第 8 天複查。配額監控要看家庭共用的整體用量（`limit - usage`）。 |
| 1.2 clone／push／revert／中斷恢復（arm64＋amd64 runner） | pass | go-with-caveat | `1.2-git-annex-drive.md`、`1.2m-followup.md` | push 會靜默失敗 → 「成功」的定義改為 `ls-remote` 確認遠端已更新。在 GITMANIFEST 替換的空檔中斷，打中 9/9 次，資料都沒有遺失。clone 要帶完整 URL，clone 之後要執行 `annex init`。在替換空檔被中斷、遠端只剩 `.bak` 時，釘選機制會讓每一輪都卡住 → D2 要加恢復規則。 |
| 1.2 manifest 預檢 | pass | go-with-caveat | 同上 | 預檢只能防受信任的寫入者互相覆蓋，**不是**完整性檢查（防注入靠 tip 釘選）。出錯就放行（fail-open）的問題已修正並實測。GITMANIFEST 的 file id 每次 push 都會變。 |
| 1.3 抹除 | 整個 repo 的抹除與部分抹除都 pass | 需要改設計（go-with-design-changes） | `1.3-erase.md`、`1.3-followup.md`、`1.3-closeout.md`、`1.3-verify/` | 抹除＝依 file id 永久刪除全部 bundle、manifest 與要抹除的 annex key，保留其他物件再重推；丟進垃圾桶不算刪除。已完成：誤刪事件的揭露（被刪的是 impl1 自己的 agora-erase，其他 repo 經全新 clone 查核完整）、垃圾桶 10 個舊 bundle 屬於 1.2（1.3 的 7 代 uuid 一個都不在垃圾桶）、多個 bundle 的陽性對照。6.1 必要修正：查驗腳本還有兩個會放行的漏洞（remote 上不在 manifest 的 bundle 沒被查驗、cat-file 失敗被吞）；永久刪除之前以 API 確認 `trashed=true`，且目標不是 live repo 資料夾（`--drive-trashed-only` 的遞迴清單會混入 live 資料夾，很可能就是誤刪的原因）。 |
| 1.4a 同 project 的隔離 | 不隔離 | go-with-caveat | `1.4-drive-file-isolation.md` | 使用者已決定：worker 共用同一個 project 與 client，視為同一個信任範圍；committer 放在另一個 project。 |
| 1.4b〜1.4c 跨 project 讀取、改寫、刪除 | pass | go | 同上 | |
| 1.4d、1.4e、1.4h、1.4i 跨 project 建檔 | fail | 需要改設計 | 同上 | 收件匣偽造 → 收件匣簽章是必要條件（使用者已決定）。id 視為公開資訊。 |
| 1.4f repo 前綴注入、偽造歷史 | fail → 對策實測 pass | **需要改設計（使用者已接受殘餘風險）** | `1.4f2`〜`1.4f4-*.md` | 對策：內容雜湊釘選、兩階段釘選、核對全部 ref、遞迴清掃、依 metadata `sha256Checksum` 驗證（小檔已實測，決策與整份下載一致）。寫回 GitHub 只實測了**機制**：正式環境**不得有可以被觸發、又接受輸入的寫入路徑**，否則住民可以偽造釘選值；釘選值只由提交流程的 job 依自己觀測到的遠端狀態寫入。review-1.4f3 的 H1〜H3、M1、M2 列為 3.2 的必要修正；`f3_meta_clean.py` 沿用舊的信任規則，不能直接當 3.2 的起點。另外 impl5 發現：consolidate 過的 manifest 裡有大量以 `-` 開頭的「已被取代」bundle 行（agora-load 有 508 行，active 只有 6 個）。現在的腳本不認得這種格式，把 manifest 判成解析失敗 → **不修的話，第一次 consolidate 之後提交流程每一輪都會中止**。3.2 的解析要分成 active 與已移除兩個集合，已移除的依 file id 永久刪除（bundle 回收）。H4 後半已驗證（`1.4f5-sweep-cost.md`）。 |
| 1.4g 讀取視圖 manifest 以固定 id 讀取 | pass | go-with-caveat | 同上 | 所有讀者與工具一律以 id 讀取。 |
| 1.5 SA 讀取身分 | pass | go-with-caveat | `1.5-service-account-reader.md` | 只有一個 SA；讀取身分要不要共用，待使用者決定（第五節 Q2）。SA 沒有儲存配額，所以只能當讀取身分。 |
| 1.6 只有 Actions 寫入權的 token | pass | go-with-caveat | `1.6-pat-actions.md`、`1.6h4-rerun.md` | 住民能停用、啟用 workflow，能取消、刪除、rerun run，能讀 log。rerun 會用舊程式碼配上現在的秘密 → workflow 開頭要檢查 `github.sha == main HEAD`，修補漏洞之後要刪掉舊 run 並輪替秘密。住民也能用**其他分支**觸發 workflow，執行該分支上的舊定義 → 也要檢查 `github.ref == refs/heads/main`，除了 main 不保留帶 workflow 檔的分支。實例：測試 repo 的 `pin-state` 分支裡就有 `.github/`（PM 以 gh 確認），正式環境要做成 orphan 分支。 |
| 1.7a export 與能力邊界 | pass | go-with-caveat | `1.7a-container-export.md` | ollama key 會被 opencode 複製成 `/work` 裡的明文 `auth.json` → 5.1 改成以檔案引用。 |
| 1.7b 變化偵測 | pass | go-with-caveat | `1.7b-*.md` | 生成中的快照會含半截訊息，判斷依據：最後一則 assistant 訊息有沒有 `time.completed`。 |
| 1.7c 位置穩定 | pass | 需要改設計 | `1.7c-*.md` | 編輯（/undo 後重新輸入）會刪掉後面的訊息 → 接續點改記「快照識別＋message id」，並從被釘住的快照讀取。 |
| 1.7d 壓縮 | pass | go-with-caveat | `1.7d-*.md` | 自動壓縮與 prune 都不會改變匯出的內容。 |
| 1.7e 停止訊號 | pass | go-with-caveat | `1.7e-*.md` | 可以用 `time.archived` 當「明確宣告」的管道；封存後的 Session 仍在清單裡。 |
| 1.7f Session id | pass | go | `1.7f-*.md` | plugin 要放在唯讀路徑；在子 Session 裡拒絕認領。 |
| 1.7g 以指定內容開新 Session | pass | go | `1.7g-*.md` | |
| 1.7h 子 Session 與 fork | pass | go-with-caveat | `1.7h-*.md` | 同步器要遞迴列舉子 Session（`/session/{id}/children`）。fork 沒有 parent 欄位（進待辦）。 |
| 1.7i export 不含憑證 | pass | go | `1.7i-*.md` | |
| 1.7j 時鐘 | 未完成 | 未完成（不擋 go；待使用者配合補測） | `1.7j-*.md` | 容器時鐘落後 0.1〜1 秒。睡眠喚醒的補測需要使用者配合。 |
| 1.8 提交流程量測 | pass（量測本身） | go-with-caveat（前提：H4、12 小時排程與觸發政策） | `1.8-commit-pipeline.md` | 合成流程的一次提交：clone 15.5 s、push 15.2 s、ls-remote 8.1 s，共約 53 s＝1 計費分鐘；空收件匣＝1 計費分鐘；concurrency group 符合 D2。impl1 的表（390／270／210）把非空提交算成 1 分鐘，**低估**；review 以真流程估非空提交 2〜3 分鐘：每 12 小時＋每天 2 次觸發＝198〜276（pass），每 3 小時＋每天 5 次＝612〜834（caveat 到 no-go）。500 次 push：`annex.max-git-bundles` 讓 active bundle 在 500 次時只有 5 個，但**舊 bundle 永遠不會被刪**（513 個），檔案數與儲存量沒有上限 → D2 需要 bundle 回收。這個設定是 clone 的本地設定，runner 的 workflow 每輪都要設。預檢窗口約 6〜25 s，會隨總檔案數成長。 |

## 三、要寫回的設計變更（摘要）

- **ADR 0008（新增）**：寫入 repo 資料夾的能力依然存在，這是 REQ「能力根本不存在」原則的有意識例外，改以「偵測＋隔離＋釘選」保證完整性；列出四項殘餘風險與使用者接受的日期。
- **ADR 0006 修訂**：產生者章改靠 profile 的簽章金鑰。
- **D2 提交流程**：
  - 每一輪的固定順序：
    1. 檢查 `github.sha == main HEAD`
    2. 逐層檢查上層同名資料夾
    3. 以內容判定、遞迴清掃（讀不到就中止，不做移動）
    4. `clone -b main`
    5. 核對全部 ref 與 manifest 內容雜湊
    6. `annex init`
    7. 處理收件匣並驗章
    8. 預檢
    9. 寫入「待定」釘選值
    10. push `main` 與 `git-annex`
    11. 用 `ls-remote` 驗證，並比對 manifest 引用集合
    12. 轉正釘選值
    13. 以 API 原地更新的方式發佈讀取視圖
    14. 刪除收件匣項目
  - clone 之後一律設 `annex.max-git-bundles`；**bundle 回收**：manifest 裡 `-` 開頭的「已移除」集合依 file id 永久刪除；不在 active、也不在已移除集合裡的，才當作疑似注入而隔離（第一次回收約 3〜4 分鐘，一次性）。
  - 轉正釘選值時，把當下 `git-annex` 分支裡這個 remote 的 key 集合寫進釘選值；下一輪清掃直接用它判斷 annex 物件，不必先 clone。`sha256Checksum` 缺少時，對新檔下載驗證一次並記住結果。
  - 釘選值只在提交流程的 job 內，依自己觀測到的遠端狀態寫入；只有寫回的那個 job 開 `contents: write`；`pin-state` 是 orphan 分支；workflow 開頭檢查 `github.sha == main HEAD` 與 `github.ref == refs/heads/main`；抹除後重建釘選值由管理者在 Mac 上直接 push，不經過 Actions。
  - 「主 manifest 不存在、`.bak` 內容與遠端 refs 都等於 promoted」→ 丟棄 pending、照常處理並 push（重建主 manifest）。
  - 預檢與 verify 改成只查名稱符合 GITMANIFEST 的檔，不列舉整個資料夾；空收件匣只算形狀符合收件匣項目格式的檔。
  - 隔離資料夾的保存天數；`use_trash` 的設定；log 最小化。
- **D9 新鮮度**：沒人主動提交時，最多落後「排程間隔＋約 20 分鐘的排程延遲」（延遲樣本只有 8 個）。
- **原始紀錄放 git 還是 annex**（待決定，需要實測）：放 git 有 delta 壓縮、但 consolidate 成本隨歷史成長；放 annex 的 bundle 小、但儲存量隨版本數成長。consolidate 的成本尚未量測，先寫進 Risks。
- **D3 身分**：committer 自己一個 project；worker 共用一個 client。Drive 寫入憑證的粒度是 worker 群組；profile 的粒度靠簽章金鑰。refresh token 由所有 worker 共用同一份（PM 的已知資訊，**未實測、出處待補**：Google 對同一帳號、同一 client 最多保留約 100 個 refresh token，超過會讓最舊的失效；共用一份就不會碰到上限，代價是 Drive 層級無法分開撤銷）。
- **D4、5.2 同步器**：遞迴列舉子 Session；以讀取視圖確認自己上傳的項目有沒有被提交，沒有就補傳；生成中的判斷依據。
- **D5 讀取**：所有讀者與工具一律以 id 讀取；讀取視圖的 manifest 只用 API `files.update` 寫入。
- **D10、2.2 接續點**：記「快照識別＋message id」；接續點＝最後一則已完成的訊息。
- **抹除（spec 與 6.1）**：依 file id 永久刪除 bundle、manifest 與目標 key 之後重推；後置條件是「remote 的 bundle 集合等於 manifest 的集合」且「垃圾桶裡沒有任何一代 uuid 的 bundle」；抹除要與釘選機制整合；範圍涵蓋隔離資料夾、收件匣、Actions log、垃圾桶與已知的 clone。
- **6.3 監控**：連續中止的輪數、隔離資料夾的增長、workflow 是否被停用、距離上一次成功提交的時間、家庭共用的整體配額。
- **MLP 工單**：新增「每個 profile 的簽章金鑰」的發放；SA 金鑰掛在 `/secrets/`，conf 裡寫容器內的路徑。

各條的出處：`scratchpad` 裡的 review-1.2-1.6、review-1.4、review-1.4f2、review-1.4f3、review-1.7、review-1.1-1.3-1.5、review-1.3f、review-1.8、review-followups、review-1.4f5、review-1.3c（定稿時會把要點轉進 design，不引用 scratchpad）。

## 四、spike 內待補（opencode 隊員暫停中）

| 項目 | 負責 | 需要使用者嗎 |
|---|---|---|
| 待辦（不擋 go）：補上誤刪事件的時間點與根本原因 | 第 2 組以後 | 否 |
| 1.7j 睡眠喚醒後的時鐘 | 待定 | **是**（蓋上 Mac 超過 30 分鐘） |
| 待辦（不擋 go）：bundle 回收之後 clone／push／consolidate 是否正常；真實大小的 Session 匯出模擬一個月，量 consolidate 成本 | 第 2 組以後 | 否 |
| 1.5 兩個 SA 的撤銷測試（只在 Q2 選「不共用」時才做） | — | 視 Q2 而定 |

## 五、待使用者決定

- **Q1（已決定）**：多帳號不可行 → worker 共用同一個 project 與 client；接受備案 A 的四項殘餘風險；收件匣簽章是必要條件。
- **Q2（已決定）**：所有 worker 共用一個讀取身分（SA）；撤銷讀取權＝輪替這把共用金鑰，影響所有 worker。
- **Q3（已決定）**：同步器以 Agora（讀取視圖）比對決定是否上傳：Agora 沒有或較舊就上傳；已上傳未提交的視為等待中，下一次提交後仍沒有才補傳。
- **Q5（已決定）**：排程間隔可設定，預設每 6 小時；提交流程也可以手動觸發。分鐘數來自使用者 GitHub 帳號的免費額度（每月 2,000 分鐘，與 MyLinuxPool 共用）。
- **Q4（已決定）**：go-with-design-changes。

## 六、資源與清理

- 資源清單：`docs/resources.md`（定稿時補上這次新建的：測試 repo 的 `pin-state` 分支、測試用 workflow、各個拋棄式前綴）。
- **已先處理（2026-09-27）**：測試 repo 裡所有 workflow（`sweep-cost-measure`、`spike-commit-pipeline`、`spike-git-annex`、`pin-writeback-impl2`、`h4-sha-guard-test`、`spike-empty-check`）都已 disable。其中三個帶 `RCLONE_CONF`（專用帳號的完整 Drive 權限，範圍不只測試資料夾）。**正式資料進入專用帳號之前**，必須刪除這些 workflow 與 secret，並輪替 committer token。副本在 `spike/workflows/`。
- 垃圾桶的清理方式：使用者判定之後，清空整個專用帳號的垃圾桶（它只放 AiStorage 的資料），再以 `trashed=true` 的查詢確認結果是 0；impl4 列的 22 個 id 包含在內，不單獨處理。
- **2026-09-27 已完成清理**（使用者決定只清測試資料、保留帳號設定；紀錄 `1.9-cleanup.md`）。原本列的完整範圍如下，其中 GCP project、OAuth client、SA 金鑰、PAT 保留給第 2 組：所有 `agora-*` 前綴、隔離資料夾（目前約 29 筆）、`drive.file` 在根目錄建的資料夾與收件匣、**整個帳號的垃圾桶**、測試 repo 的 workflow、分支與 run、各容器工作目錄裡的 `auth.json`、refresh token、PAT、SA 金鑰與兩個 GCP project。
