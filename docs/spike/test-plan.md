# 技術驗證測試計畫（tasks 1.1〜1.8）

這份文件給 test 設計案例與判準、給 impl 照著執行。**test 不動環境**（不建容器、不碰 Drive 或 GitHub、不安裝東西）；impl 調整環境後，test 依這裡的判準核對 evidence 與實測輸出。每個子項完成後，evidence 依下面的檔名寫進 `docs/spike/evidence/`（目錄由 impl 建立）。

詞彙依 `CONTEXT.md`；設計依 `design.md` D1〜D10 與 ADR 0005〜0007。Drive 操作的範圍規則見第三節（committer 與 SA 只在 `SPIKE_FOLDER_ID`（`~/.config/aistorage-spike/ids.env`，非秘密）底下；`drive.file` client 在自己建的資料夾裡）。

## 一、No-go 條件

以下任一成立且找不到對策，就是 no-go：停止 1.9 的 go 判定，把證據交使用者，回步驟 3 重新選型。**不為了遷就實作而降低 spec。**

1. **design 明定**：`drive.file` 的 client 能在 repo 資料夾（Agora／Foundry 真本所在）建檔，而技術驗證找不到 rclone／git-remote-annex 側的對策。
2. 同等嚴重（test 認為）：
   - **跨 project 的隔離不成立**：用 C（project 1，`other-project`）當攻擊方時，1.4b〜1.4e 任一項看得到、改得到、刪得到或能寫進 committer／別的 profile 的檔案或資料夾（跨 project 都不隔離，正式拓撲即失效）。同 project 不隔離但跨 project 隔離是 **caveat**，不是 no-go（見 1.4；1.9 回饋 design D3）。
   - **真本會被靜默覆蓋**：實測中出現任何一次「push 看似成功、但內容被另一次 push 覆蓋而遺失」，且「push 前重讀遠端 manifest、不同就中止」壓不住窗口（1.2）。
   - **抹除不乾淨**：跑完 1.3 的流程後，canary 仍存在於目前版本、git 歷史、bundle、Drive 舊 revision 或垃圾桶，且沒有可行做法（含刪檔重建＋永久刪除）清掉（1.3）。
   - **reader 能改寫真本**：service account（被分享成 reader、**以完整 `drive` scope** 取得 token）對 repo／讀取視圖能寫入、改寫或刪除，而不是被拒絕（1.5）。
   - **住民能碰真本或提交流程**：只有 Actions 寫入的 fine-grained token 能推 contents、改 workflow 檔、改 repo 設定；或能用它把 workflow 弄成不安全的狀態（1.6）。
   - **接續點不可靠**：`opencode export` 重新匯出／壓縮後，既有訊息位置改變，且沒有替代表示（訊息 id／快照）可行（1.7）。
   - **提交流程超預算**：1.8 的量測換算後，例行提交的**計費分鐘數**超過每月 800 分鐘（與 MLP 共用 2,000 分鐘的預算），且壓縮頻率、快取等做法都救不回。門檻已經使用者確認：≤300 pass；300〜800 caveat；>800 且無壓縮做法才 no-go。
   - **憑證撐不住**：自建 OAuth client 即使設成「正式版」，restricted `drive` scope 下 refresh token 仍無法穩定存活（spike 期間出現 `invalid_grant`），提交流程無法穩定運作（1.1）。
   - **能力邊界不成立**：1.7 佈置的容器內拿得到白名單以外的憑證（以 `env`、`/proc/mounts`、`docker.sock`、privileged、capabilities 五項判定；提早看到 D3 的破口，交 5.1 判定）。

## 二、相依順序

```
1.1 ──┬── 1.2 ──┬── 1.3（自己的 repo 前綴 agora-erase/）
      │         ├── 1.4（需要 1.2 提供建 repo 的腳本；1.4f 用拋棄式的攻擊 repo 前綴 agora-attack/）
      │         ├── 1.5（自己的前綴 agora-read/）
      │         └── 1.8（自己的前綴 agora-load/；最耗時，最早開始）
      └── 1.6（只需要 1.1 的 PAT 與測試 repo）
1.7（只需要 1.1 的 ids.env 與 ollama-cloud key，全程可並行）
1.9（全部完成後）
```

- 1.1 最先，其餘全部依賴它。
- 1.2 只負責提供建 repo 的腳本與量測基準；**1.3、1.4、1.5、1.8 不得共用同一個 repo 前綴**（H1）：1.3 會改寫歷史並 force push、1.8 會推 500 次、1.4f 會刻意塞同名檔，並行在同一個 repo 上就是 ADR 0006 的「同時 push 悄悄互相覆蓋」，會互相污染結果。各自用 `agora-erase/`、`agora-attack/`、`agora-read/`、`agora-load/`（1.4f 的攻擊 repo 驗完即丟）。
- 1.2 完成後，{1.3, 1.4, 1.5, 1.8} 彼此可並行（不同前綴）。
- 1.6 與 1.7 可全程並行。
- 1.2／1.8 的 runner 測試需要測試 repo 裡有一個 `workflow_dispatch` 的 spike workflow。**前置缺口**：`docs/spike/setup.md` 沒有交代這個 workflow 檔怎麼進 repo（PAT 只有 Actions 權限、推不了 contents）。由使用者用管理憑證放上去，或由使用者授權 PM 代推；開工前先解決。
- **Drive 拓撲**（H3、H6）：committer＝project 2（`aistorage-spike-2-260926`）；A＝`mac-opencode`、B＝`test-profile` 也在 project 2；攻擊方一律用 **C＝`other-project`（project 1）**，因為正式環境是「profile 的 project ≠ committer 的 project」。A 對 B 的結果只回答「隔離單位是 client 還是 project」。所有 conf（committer、SA、之後的管理憑證）一律設 `root_folder_id = SPIKE_FOLDER_ID`，special remote 的 `rcloneprefix` 用相對路徑（不含 `aistorage-spike/`），否則 committer 與 SA 的根目錄不同、`annex::` 會解析不到（H6）；此為部署規則，1.9 寫回 design D1、D5。

## 三、證據與秘密

- 所有輸出只寫進 `docs/spike/evidence/`。證據裡只准出現：**檔名、路徑**（`~/.config/aistorage-spike/…`）、以及 `ids.env` 裡的非秘密 id（project id、`SPIKE_FOLDER_ID`、`SA_EMAIL`）。
- **Drive 操作的範圍**：committer 與 SA 的操作一律在 `SPIKE_FOLDER_ID` 底下。`drive.file` 的 client（A、B、C）看不到這個資料夾，改在自己建的資料夾裡操作（放在「我的雲端硬碟」根目錄，名稱加 `aistorage-spike-` 前綴，例如 `aistorage-spike-inbox-mac-opencode`），這些資料夾納入 1.9 的清理清單。**負向測試不要用 rclone 路徑指令**（H2）：rclone 以名稱逐層解析，`drive.file` client 看不到目標資料夾時會在同名路徑下另建一個、回報成功，同時產生假綠燈與假紅燈；1.4 的負向測試統一用小型 Drive API 腳本，依 **file id** 做 `files.get`／`files.create(parents=[id])`／`files.update`／`files.delete`／`permissions.create`，token 由腳本從 conf 讀進記憶體、不印出，輸出只記 HTTP 狀態碼與 `error.reason`（同時解決 rclone 分不出 404 與 403 的問題）。
- 不得讀進秘密檔的內容（含 `rclone config show`、`cat client-*.json`）；需要引用時用環境變數或檔案路徑，例如 `RCLONE_CONFIG=~/.config/aistorage-spike/rclone-committer.conf rclone …`。**明文禁止**任何會把秘密印出來的做法：`rclone --dump headers`／`-vv --dump`（會印 Authorization）、腳本裡的 `set -x`、Actions 的 `ACTIONS_STEP_DEBUG`。
- 證據不得出現的字串形狀：`GOCSPX-`（client secret）、`ya29.`（access token）、`1//0`（refresh token）、`ghp_`／`github_pat_`（PAT）、`-----BEGIN PRIVATE KEY-----`（SA key）、rclone conf 的 `token = {…}` 內容。
- **提交前檢查**（impl 在每個 evidence 檔寫完後、test 在核對時各跑一次）：
  1. 形狀掃描（**掃描範圍排除 `1.9-secret-scan.md` 本身**，因為它會記下這些形狀；M3）：

     ```bash
     grep -rnE 'GOCSPX-|ya29\.|1//0[0-9A-Za-z_-]|ghp_|github_pat_|-----BEGIN (RSA )?PRIVATE KEY-----|"token"|refresh_token' docs/spike/evidence/ --exclude=1.9-secret-scan.md && echo 'HAS-SECRET' || echo clean
     ```

     `report.md` 也照同一道掃（不排除）；報告與說明文件**不得貼出這些形狀本身**（用「client secret 前綴」之類的描述代替），否則掃描永遠誤報。

  2. **以實際秘密值比對**（形狀掃描抓不到 ollama key 等；M3）：從 `client-*.json`、`rclone-*.conf`、`sa-reader.json`、`gh-pat-actions.txt`、`ollama-cloud-key.txt` 取出秘密值寫進 600 權限的暫存 pattern 檔（去空行，空行會讓每行都命中），`grep -rlFf <pattern> docs/spike/evidence/ docs/spike/report.md`，跑完立刻刪除 pattern 檔。
  3. 再以 `git diff` 目視一次；不確定的字串一律刪掉重寫。runner log 也照同一規則存（log 存檔前先跑上述兩道）。

---

## 1.1 自建 OAuth client、發佈狀態、token 期限、配額與 Drive API

**要回答的問題**：自建的 OAuth client（提交流程 restricted `drive`、兩個 profile 用 `drive.file`）在「已發佈、未驗證」下能否穩定長期運作，且專用帳號的配額與 API 行為正常、不是 rclone 內建 client？

**前置**：setup 的 `ids.env`、`client-{committer,mac-opencode,test-profile,other-project}.json`、`rclone-*.conf`；兩個 GCP project 的同意畫面已在 Console 設成「正式版」。

**步驟**：
1. 對四個 conf 各跑一次 `rclone about gdrive:`。寫入 smoke test 依 scope 分開做（H2）：
   - committer（restricted `drive`）：在 `SPIKE_FOLDER_ID` 下建 `committer-smoke.txt`、ls 看到、刪除。
   - A／B／C（`drive.file`）：各自在**自己建的資料夾**（根目錄、名稱 `aistorage-spike-smoke-<client>`）裡建檔、ls 看到、刪除；不要對 `aistorage-spike/` 路徑做寫入（會另外建出同名資料夾、污染後續測試）。
   - 全部指令一律帶 `--config`（zsh 不拆字串變數，用 `RCLONE_CONFIG=<檔案>` 或直接 `--config <檔案>`），輸出貼進 evidence。這些資料夾納入 1.9 清理清單。
2. 建立「非 A 建的」對照：用 committer conf（restricted `drive`）確認 `rclone lsf gdrive:` 列得出 `SPIKE_FOLDER_ID` 的內容（root_folder_id 生效的證明）→ 完整 scope 生效。**可見性判準不在這裡做**（H3）：是否被 `drive.file` client 看到，一律交給 1.4 用 C 依 file id 判定；也不要請使用者放 `user-created.txt`（1.4e 已改以 committer 建的資料夾為對象）。
3. 記錄同意畫面狀態（Console「目標對象」顯示「正式版」，記檢查時間）、以及 rclone 輸出中**沒有**「shared client_id／內建 client 將停用」類警告。
4. 配額：`rclone about gdrive:` 的 total／free（家庭共用的 5TB 級），排除 15GB 個人免費額度。
5. Token 期限：記下首次授權時間；spike 期間（數天）每次使用都確認成功，任一 `invalid_grant`／需重新授權即 fail。**驗證期不到 7 天，本身驗不到「7 天過期」**（M2）：1.9 的判定只以 Console 正式版狀態為依據、寫成 pass-with-caveat，並排一次「首次授權後第 8 天」的複查；複查失敗就回頭處理，但不必擋住 go。

**pass／fail 判準**：
- Pass：四個 conf 全數 smoke test 成功（各自 scope 範圍內）；committer 看得到 `SPIKE_FOLDER_ID` 及其內容；Console 狀態為正式版；total 為 5TB 級；spike 期間無任何授權失效；無內建 client 警告。
- Fail：任一個 conf 無法授權、或 7 天內失效、或用的是內建 client、或配額是 15GB、或 drive.file client 的 smoke test 其實落在自己新建的同名資料夾（假成功）。**drive.file 的可見性不列入本項判準**（同 project 是否隔離只影響 1.4a 的 caveat）。

**證據**：`docs/spike/evidence/1.1-oauth-clients.md`（各指令與輸出摘錄、Console 狀態、時間戳、配額數字）。

**fail 的設計影響**：Drive 是唯一儲存實體，憑證不穩則提交流程與讀取全滅 → Risks「OAuth client 停在 Testing」那條升級為 no-go，重新選型或改發佈策略。

## 1.2 git-remote-annex＋rclone 在 Drive；arm64 容器與 amd64 runner；manifest 預檢

**要回答的問題**：以 git-annex 內建 git-remote-annex 加 rclone special remote，能在專用帳號 Drive 上完成 clone／commit／push／log／diff／revert、push 中斷後恢復，且「push 前重讀遠端 manifest 與 clone 時比對」可行嗎？兩邊架構的安裝方式與版本是什麼？

**前置**：1.1 完成；`rclone-committer.conf`（**已由 PM 補上 `root_folder_id = SPIKE_FOLDER_ID` 並重新寫入 Actions secret `RCLONE_CONF`；見 setup.md 末尾的「追加」**）、`SPIKE_FOLDER_ID`；colima；測試 repo `FATESAIKOU/aistorage-spike` 與其上的 spike workflow（見第二節的前置缺口）。
備註：1.2 的容器是**拋棄式驗證容器**（非住民容器），暫時掛 committer conf 才能 push；住民容器只掛白名單（5.1 再驗）。此偏離要寫進 evidence 與 1.9 報告。

**步驟**：
1. arm64 容器：`colima start`；起 Ubuntu 容器；裝 git-annex 與 rclone 並記錄安裝方式與版本。**不要預設 apt 版可用**（L）：Ubuntu 24.04 的 apt git-annex 很可能低於所需版本（git-remote-annex 需 ≥ 10.20240531），先採用官方 standalone tarball 或較新發行版，兩種架構用同一種安裝法；內建 rclone special remote 需 rclone ≥ 1.67。
2. **root_folder_id 存證（H6）**：以 committer conf 執行 `rclone lsf gdrive:`，確認列出的就是 `SPIKE_FOLDER_ID` 的內容（開工時應為空或只有 smoke 殘留）；截圖／輸出貼進 evidence。**若列出的不是 `aistorage-spike` 的內容，先停下來修 conf 與 secret，不繼續。**
3. 建 repo（容器內或 Mac 皆可，用 committer conf）：`git init`、`git annex initremote drive type=external externaltype=rclone-builtin encryption=none rcloneremotename=gdrive rcloneprefix=<相對前綴，不含 aistorage-spike/> autoenable=true`（**語法照官方文件；`encryption=none` 與關閉 chunk 是 D5「依 annex key 直接取物件」的前提**，1.5 要照同樣設定驗）、`git remote add drive annex::<remote>`（URL 形狀以 `git-remote-annex` 文件為準）、放小檔與大檔（設 `annex.largefiles`）、commit、push。**前綴一律用相對路徑**（H6）。此步驟同時是提供給 1.3／1.4／1.5／1.8 的建 repo 腳本；那幾項各自用自己前綴（`agora-erase/` 等）重跑，不共用這一個（H1）。
4. arm64 clone：容器內 `git clone annex::…`；`git annex get` 大檔；`git log`／`git diff`／`git revert` 各做一次並 push revert；**再開全新 clone** 比對 HEAD 與檔案雜湊。
5. 中斷測試：下一個大一點的 commit，push 中用 `kill`（或 `timeout`）真的中斷——**必須留下中斷證據**（程序被 kill 的輸出、或遠端只出現部分物件）；然後重新 push，再全新 clone 驗證中斷前已 commit 的內容都在（沒有遺失）。
6. manifest 預檢（M8）：
   - 正向：clone 時記錄遠端 manifest 物件識別（`GITMANIFEST` 的 file id 或雜湊）；立即再讀一次比對，相同則允許 push。量測 **`預檢讀取結束 → push 完成、manifest 寫入完成` 的總時間**（這才是預檢真正沒保護到的窗口），而不是只量兩次讀取。
   - 記下 git-remote-annex／rclone 更新 GITMANIFEST 時是**原地更新（file id 不變、產生新 revision）還是刪掉重建（id 會變）**；這決定預檢能不能以 id 比對、1.3 要清的是 revision 還是垃圾桶，也影響同名解析。
   - 負向：在另一個 clone 先 push 一次改變 manifest，再在原本的 clone 執行預檢 → **必須中止**。這一步是防假綠燈的關鍵，不可省略。
7. amd64 runner：觸發 spike workflow（ubuntu-latest），安裝兩工具（同一種安裝法）、用 `RCLONE_CONF` secret、clone、push 一個 commit；輸出（版本、耗時）存檔。再在本機新 clone 驗證該 commit 可見。
8. 記錄兩邊版本與安裝方式（供 1.9 的 arm64／amd64 欄位）。

**pass／fail 判準**：
- Pass：兩架構都完成 clone＋get＋revert＋push，且新 clone 的 HEAD／雜湊與預期一致；中斷後重推不遺失已 commit 內容（且有真中斷的證據）；manifest 預檢在負向案例確實中止、在正向案例放行；預檢窗口時間與 manifest 更新方式有記錄；耗時可量。
- Fail：clone／push 任一環節失敗、revert 不可行、或中斷後遺失內容、或預檢無法阻止被覆蓋。
- 假綠燈檢查：中斷測試若因 push 太快而沒中斷到，視為未測；clone 成功要驗到具體檔案與雜湊，不能只看 exit code；負向測試沒有真的改變 manifest 就不算通過；安裝成功的判定要含版本輸出（不能用「apt install 沒報錯」代替）。

**證據**：`docs/spike/evidence/1.2-git-annex-drive.md`（指令、輸出、時間、版本）、`docs/spike/evidence/1.2-runner-log.txt`（runner 輸出）。

**fail 的設計影響**：D1／ADR 0005 的核心不成立 → no-go、重新選型（例如 S3 或混合）。若只有 manifest 預檢不可行：D2 的「管理與提交錯開」只剩 workflow 開關，而住民 token 可重新啟用 workflow（1.6），此風險升級，需回設計。

## 1.3 抹除驗證

**要回答的問題**：`git annex drop --force`、`git annex forget`、改寫歷史並重推 bundle 後，被抹除的內容在目前版本、git 歷史、bundle、Drive 舊 revision 與垃圾桶都找不到嗎？實際可行的手法是什麼（覆寫夠不夠，是否需要刪檔重建＋永久刪除）？

**前置**：1.2 的**專屬 repo 前綴 `agora-erase/`**（不與 1.4／1.5／1.8 共用，H1）；committer conf（spike 期間用它代表管理憑證，已設 `root_folder_id`）；一組隨機 canary 字串（例如 `SPIKE-ERASE-CANARY-<8 位隨機>`，非秘密，寫進 evidence 供比對）。**查驗用的 clone 與暫存目錄一律放在 repo 工作樹之外**（evidence 本身寫著 canary，在工作樹裡 `grep -r` 會命中自己、得到假紅燈；H4）。

**步驟**：
1. 佈置 canary：加入 `erase-canary.txt`（內容含 canary 字串）commit＋push；再修改一次並 commit＋push（讓舊版留在歷史）。另放一個進 annex 的檔案（讓 annex 物件存在）。
2. 抹除：對 annex 內容 `git annex drop --force --from=drive`（或等效）、`git annex forget --force`、以 `git filter-repo`（`--replace-text` 或移除路徑）改寫歷史、`git push --force`；Drive 端依結果採「覆寫」或「刪檔重建 GITBUNDLE／GITMANIFEST 後永久刪除舊檔」（記錄實際採用哪一種）。
3. 查驗（每一項都要留下輸出）：
   - a. 目前版本：新 clone 後對工作樹 `grep -r CANARY` 與 `ls` 無命中；`git grep` 於 HEAD 無命中（在 clone 目錄內 grep，canary 只存在於別處的 evidence）。
   - b. git 歷史：`git log --all -S CANARY -p` 無輸出；`git rev-list --all --objects` 不含 canary 檔名；`git annex fsck`／`git annex unused` 無殘留。
   - c. bundle：列出 special remote 上**全部** GITBUNDLE 物件，逐一取回後**先 `git bundle unbundle`（或 `index-pack`）進一個空 repo，再用 `git cat-file --batch-all-objects --batch`／`git log --all -S` 查**；**不得直接對 bundle 檔 grep**（物件是 zlib 壓縮過的，直接 grep 永遠找不到、是假綠燈；H4）。annex 物件以 key（含 SHA256）確認已不存在。
   - d. Drive 舊 revision：以 committer conf 列出曾經含 canary 的檔案的 revisions（rclone 相應旗標或直接呼叫 Drive API `revisions.list`），確認**只剩下目前的 revision，且它的內容經 unbundle 查驗不含 canary**；revision 清單要能證明涵蓋這個檔（不是 API 回空）。否則執行刪檔重建＋永久刪除後再查（N5）。
   - e. 垃圾桶：列出帳號垃圾桶（rclone `--drive-trashed-only` 或 web UI）確認無 canary 檔；注意 rclone 預設丟垃圾桶，**刪除 ≠ 永久刪除**。
   - f. 抹除紀錄：留下一行 who／when／why／which part（供 6.1 參考）。
4. 查驗完整性：查驗前先確認被檢查的物件集合完整（`git annex whereis`／列出 remote 物件清單並計數），不能只查手邊那幾個檔。

**pass／fail 判準**：
- Pass：a〜e 全部找不到 canary（內容與檔名皆無），且 f 有紀錄；evidence 寫明採用「覆寫」或「刪檔重建」哪條路與其耗時。
- Fail：任一處仍找得到 canary，或查驗方式無法證明覆蓋範圍（列不出完整集合）。
- 假綠燈檢查：「垃圾桶是空的」若查詢範圍只有 `aistorage-spike/` 子資料夾就不算；Drive revisions 查不到要能證明查詢涵蓋該檔的 revision 清單，不是 API 回空。

**證據**：`docs/spike/evidence/1.3-erase.md`。

**fail 的設計影響**：spec「抹除」（`agora/session-record`）不成立、違反隱私條款 → no-go 或改設計（例如抹除必然伴隨刪檔重建）；design Risks 的「抹除後內容仍在 Drive revision 與垃圾桶」升級。

## 1.4 `drive.file` 的隔離（拆到最細）

**共同前置**：1.1 完成；四個 client 都在同一專用帳號授權。簡稱：`mac-opencode`＝A、`test-profile`＝B（都在 project 2 `aistorage-spike-2-260926`）、`other-project`＝C（project 1 `aistorage-spike-1-260926`，依 setup.md 的實際配置）；committer 在 project 2。**攻擊方一律用 C**（H3）：正式拓撲是「profile 的 project ≠ committer 的 project」，只有跨 project 的結果能直接對應正式環境；A 對 B（同 project）只用來回答「隔離單位是 client 還是 project」。1.4a〜1.4c、1.4e 不需要 1.2；1.4f／1.4g／1.4h 需要 1.2 的建 repo 腳本與**拋棄式的攻擊 repo 前綴 `agora-attack/`**（H1）。所有 file id 與腳本輸出寫進同一份 evidence。所有 conf（含 SA；見 1.5）一律設 `root_folder_id = SPIKE_FOLDER_ID`（H6）。

**共同方法（H2）**：負向與可見性測試一律用一支小型 Drive API 探測腳本，依 **file id** 操作：`files.get`、`files.create(parents=[id])`、`files.update`、`files.delete`、`permissions.create`、children 查詢（`'<id>' in parents`）。token 由腳本從對應 conf／client JSON 讀進記憶體，只放進 HTTP header，**不印出、不進 argv、不落檔**；輸出只有操作、目標 id、HTTP 狀態碼與 `error.reason`。**不得用 `rclone mkdir`／`copyto`／`moveto`／`deletefile` 做負向測試**：rclone 以名稱逐層解析，看不到目標時會自行建出同名資料夾並回報成功（假綠燈），或根本沒碰到目標檔（假綠燈）；`drive.file` 的可見性也不能只看列舉——授權逐檔，父資料夾看不到時列舉走不到，但依 id `files.get` 可能取得到。

**共同假綠燈防護**：每個 client 先用同一支腳本在**自己的空間**跑完整的正向對照——`files.create(parents=[自己的 id])`、`files.update`、`files.delete`、`files.get` **各成功一次**（N6；只做 get／list 的話，腳本本身的 multipart 寫法錯誤會被誤記成「被拒」）；通過後才開始負向測試。失敗一律記 HTTP 狀態與 `error.reason`，區分 404（看不到）與 403（看得到但不准）；事後由 committer 或檔案擁有者確認目標未變；「列舉不到」本身不算隔離成功，可見性判準一律以 `files.get` 為準。

**證據（全部子項）**：`docs/spike/evidence/1.4-drive-file-isolation.md`（每子項一節）。

#### 1.4a 同 project、不同 client（A vs B）的可見性
- **要回答的問題**：`drive.file` 的可見範圍以 OAuth client 為單位，還是以 GCP project 為單位？
- **步驟**：A 以腳本在自己空間建 `a-visible.txt`（記 file id）；B 先證明自己空間的 get／list 正常；B 依 A 的 file id `files.get`，並列舉根目錄；A 事後確認檔未變。
- **pass／fail 判準**：Pass＝B `files.get` 得 404、列舉不含 A 的檔；Fail＝B 取得到或列得到。**本項單獨不判 no-go**：結果與 1.4b 合判——同 project 不隔離、跨 project 隔離＝**caveat**（D3 補「committer 自己一個 project，每個 profile 各一個 project」）；跨 project 也不隔離（1.4b fail）＝no-go。
- **fail 的設計影響**：若為 project 級：caveat，1.9 回饋 design D3（committer 與每個 profile 各一個 project）；同 project 隔離則維持 client 級、無設計變更。

#### 1.4b 不同 project（A vs C）的可見性
- **要回答的問題**：跨 project 的 client 是否被隔離？（正式拓撲的關鍵）
- **步驟**：C 以腳本依 A 的 file id `files.get`，並列舉根目錄；A 事後確認檔未變。
- **pass／fail 判準**：Pass＝C `files.get` 得 404 且列舉不含 A 的檔；Fail＝C 取得到 → **no-go**（開頭清單）。與 1.4a 合寫一句「隔離單位＝client／project」的結論。
- **fail 的設計影響**：no-go；若 1.4a 顯示為 project 級則依合判記為 caveat。

#### 1.4c 改不到、刪不到別的 client 建的檔案（C 主測，A→B 加測）
- **要回答的問題**：攻擊方能否改寫（改名／改內容）、刪除、或改共用權限別人的檔？
- **步驟**：目標＝B 的 `b-own.txt`（記 id 與雜湊）。C 依序 `files.update`（改名、改內容）、`files.delete`、`permissions.create`；每步記狀態與 reason；A 對 B 加測同一組（補充「隔離單位」資料）。B 事後確認檔名、內容雜湊與 permissions 未變。
- **pass／fail 判準**：Pass＝C 全部被拒（記 403／404 與 reason）且檔未變；Fail＝C 任一操作生效 → **no-go**（跨 project）。A→B 的結果只併入「隔離單位」結論、不單獨判 no-go。
- **fail 的設計影響**：spec `common/identity`「禁止的能力不存在」不成立（跨 project 情形）→ no-go。

#### 1.4d C 在 B 建立的資料夾裡建檔
- **要回答的問題**：`drive.file` client 能不能在別的 client 建立的資料夾裡建檔（能不能偽造產生者）？
- **步驟**：B 以腳本在自己空間建 `inbox-b/` 並把資料夾 id 給 C；C 依該 id 嘗試 `files.create(parents=[id])`（含一個與 B 既有檔**同名**的）、`files.update`、`files.delete`；C 事後列舉自己的空間；B 事後清點 `inbox-b/` 子項與雜湊。
- **pass／fail 判準**：Pass＝C 全部被拒（404／403＋reason）且 B 的資料夾沒有新增；Fail＝C 建得進去 → **no-go**（跨 project 情形；1.4 的共同判定與 1.4a／1.4b 合判）。
- **fail 的設計影響**：收件匣隔離與產生者章不成立（偽造項目會被蓋上 B 的章）→ no-go，或改設計（例如收件匣改由提交流程建、另以機制辨識來源）。
- **與 1.4e 的分工（M9）**：本項測「別的 profile 的 client 建的資料夾」；1.4e 測「committer client 建的資料夾（`SPIKE_FOLDER_ID`）」，也就是 bootstrap 實際會遇到的形狀。兩項一起回答「A 能不能在**所有非自己建**的資料夾裡建檔」。

#### 1.4e C 在 committer 建的資料夾（`SPIKE_FOLDER_ID`）裡建檔
- **要回答的問題**：`drive.file` client 能不能在 committer client 建的資料夾（也就是正式環境的收件匣／repo 的宿主 `aistorage-spike/`）裡建檔、列舉或看到內容？
- **步驟**：C 依 `SPIKE_FOLDER_ID` 依序嘗試 `files.get`（讀資料夾本身）、children 查詢、`files.create(parents=[SPIKE_FOLDER_ID])`（建子資料夾與檔案）、`files.update`／`files.delete` 一個由 committer 建的既有檔；每步記狀態與 reason；事後由 committer conf 清點 root 與子資料夾、比對既有檔雜湊。**不要請使用者另建資料夾**——`SPIKE_FOLDER_ID` 就是實際會發生、且 setup 已經建好的對象。
- **pass／fail 判準**：Pass＝C 全部被拒（404／403＋reason），committer 事後確認沒有新檔；Fail＝C 能建、能列舉或能改到既有檔 → 依 1.4 共同判定：跨 project 情形即 **no-go**。
- **fail 的設計影響**：這是「收件匣由 profile 自己的 client 建立、再登記資料夾 id」（D3、tasks 2.3）與「提交流程用 committer 建的資料夾」能否共存的前提；失敗則 bootstrap 不可行，需回設計。

#### 1.4f C 在 repo 資料夾裡建檔（no-go 觸發點）
- **要回答的問題**：`drive.file` client 能不能在 git-remote-annex special remote 的資料夾裡建檔，特別是**同名**的 GITMANIFEST／GITBUNDLE？
- **步驟**：用 1.2 的建 repo 腳本在 `agora-attack/` 建拋棄式 repo；取得該 repo 前綴的資料夾 id 與 GITMANIFEST 的 **file id**（不以名稱操作）；C 依序 `files.get`（前綴資料夾）、`files.create` 放 `c-into-repo.txt`、建與 GITMANIFEST 同名的檔（隨機內容）、建與既有 GITBUNDLE 同名的檔；每步記狀態與 reason；由 committer 清點（同名會看到兩個）並比對既有檔的 revision／雜湊有無被動到；最後在**全新 clone** 驗證 repo 仍可讀、沒有解析到錯的 manifest。**加測上一層（H6）**：C 能不能在 repo 前綴的**上一層**（例如 `agora-attack` 這一層，或 `SPIKE_FOLDER_ID` 下）建一個與前綴同名的資料夾，讓 rclone 名稱解析可能走錯。找不到對策就明確寫 no-go。
- **pass／fail 判準**：Pass＝C 建不進去（404／403），**或**建得進去但能出示可行對策（例如 manifest 以固定 file id 定位、有完整性檢查，且實驗證明同名檔不被解析、上一層同名資料夾不影響 clone）；Fail＝C 建得進去且無對策 → **no-go**（design Risks 原文）。
- **fail 的設計影響**：真本可被住民污染／覆蓋 → no-go，回步驟 3 重新選型。

#### 1.4g C 在讀取視圖資料夾裡建檔
- **要回答的問題**：`drive.file` client 能不能污染讀取視圖（manifest 或其他檔）？
- **步驟**：同 1.4f，對象改為讀取視圖資料夾與其 manifest 的 file id；另驗 reader（service account）以固定 file id 取值時不受同名新檔影響。
- **pass／fail 判準**：Pass＝建不進去，或建得進去但不影響以 id 定位的讀取（註明這是 D5 的前提）；Fail＝能改到 reader 依 id 會拿到的檔。
- **fail 的設計影響**：D5「整條讀取路徑以固定檔案 id 定位」的前提動搖；需改設計或限制寫入路徑。

#### 1.4h 同名檔在以上各處的行為（含上一層前綴）
- **要回答的問題**：Drive 允同名；在 C 建得進去的位置，同名檔會變重複檔還是覆寫？列舉與「以名取值」會拿到哪一個？上一層的同名前綴資料夾會不會讓 rclone 解析走錯？
- **步驟**：對 1.4f〜1.4g 中允許建檔的每個位置各建兩個同名檔，用 committer 依 file id 與名稱各列舉一次並計數；對 repo 前綴的上一層另建同名資料夾，再跑一次 clone／annex 操作觀察解析結果。
- **pass／fail 判準**：Pass＝行為明確記錄，且真本與讀取路徑都以 file id 或唯一名稱定位、上一層同名不影響；Fail＝存在以檔名定位且可被同名混淆的路徑。
- **fail 的設計影響**：D2（bundle／manifest 定位）與 D5 需改為一律以 id／雜湊定位；H6 的相對路徑規則要升級成「連前綴資料夾都以 id 記在 annex 設定或由 committer 擁有」。

#### 1.4i 收件匣 bootstrap 與「固定檔案 id、原地更新 manifest」
- **要回答的問題**：收件匣由 profile 自己的 client 建立是否可行？manifest 以固定 file id 原地更新是否可行、更新後對方多久讀得到？
- **步驟**：(1) A 以腳本在自己空間建 `aistorage-spike-inbox-mac-opencode`（根目錄），記下資料夾 id；(2) A 在裡面建檔、列舉、刪除自己的檔；(3) 以 committer 依 id 讀取該資料夾（證明提交流程拿得到、能把「資料夾 id ↔ profile」登記起來）；(4) **C**（不是 B；H3）依 id 對 A 的收件匣嘗試 `files.create`／children 查詢（應失敗，與 1.4d 互證；A→B 的結果只併入 1.4a 的「隔離單位」結論）；(5) manifest：committer 建 `manifest.json`、記 file id，**原地更新 3 次**，每次確認 (a) file id 不變、(b) 內容為最新、(c) 沒有同名重複檔、(d) SA reader（1.5）依 id 取得最新版；**(e) 量測每次更新到 SA 讀到新版本的延遲（L）**——「同步並提交」的等待時間下限之一，寫進 1.8／D9 的估算。
- **pass／fail 判準**：Pass＝bootstrap 可行、收件匣 id 可登記、manifest id 穩定且無重複、SA 依 id 讀得到新版本且有延遲數字；Fail＝bootstrap 不可行，或原地更新換 id／產生重複／SA 長期讀到舊版。
- **fail 的設計影響**：tasks 2.3（收件匣↔profile 登記）或 D5（manifest 定位）不成立 → 回設計。

## 1.5 service account 被分享成 reader

**要回答的問題**：service account 被分享成 reader 時，能讀取該資料夾（含 `git clone annex::…`、依 annex key 直接取得 special remote 裡的物件），且不能寫入或刪除嗎？

**前置**：1.1（`sa-reader.json`、`SA_EMAIL`）、1.2 的專屬前綴 `agora-read/`（H1）；SA 已在 `aistorage-spike` 為檢視者。**setup 缺口**：setup.md 沒有 SA 用的 rclone conf；impl 建立 `~/.config/aistorage-spike/rclone-sa-reader.conf`（引用 `sa-reader.json`，不新增秘密），設 `root_folder_id = SPIKE_FOLDER_ID`（H6），並記進 1.9 的資源清單。**H5：SA conf 明確設 `scope = drive`（完整），不是 `drive.readonly`**——要證明的是 Drive 的 ACL 只有 reader，不是 token 的 scope；住民拿到 SA 金鑰後可以自己換完整 scope 的 token，唯讀 scope 下「寫不進去」什麼都證明不了。此事要寫進 evidence。

**步驟**：
1. 列舉與讀取：SA conf `lsjson gdrive:`（根目錄就是 `SPIKE_FOLDER_ID`，看得到 committer 建的 repo 與讀取視圖）；以檔名與檔案 **id** 各讀一次同一個既有檔。
2. Clone：在乾淨目錄、且 `RCLONE_CONFIG` 只指 SA conf 的環境（或根本沒掛 committer conf 的容器）執行 `git clone annex::…`；驗 HEAD、`git log`、檔案雜湊。
3. `git annex get` 一個 annex 大檔，比對 SHA256 與 key 一致。
4. 依 annex key 直接取物件：用 `git annex whereis`／`git-annex examinekey` 取得 key 與對應路徑，以 SA conf `rclone cat` 取回並比對雜湊（記下 key 對應 Drive 路徑的規則，供 D5 結論用；**remote 是 `encryption=none`、未 chunk，key 才能直接對到物件**）。
5. 寫入失敗：SA 以腳本對 `SPIKE_FOLDER_ID`、repo 資料夾、讀取視圖資料夾依序 `files.create`（含與既有檔同名者）、`files.update`、`permissions.create`；預期 403＋`error.reason`（SA 看得到這些資料夾，所以不是 404）並抄原文；事後以 committer conf 確認沒有新檔、既有檔雜湊不變。
6. 刪除失敗：SA 以腳本 `files.delete` 一個看得到的既有檔；事後比對雜湊不變。
7. 同名寫入：SA 建一個與既有檔同名的檔，確認沒有生效（或記錄行為）。

**pass／fail 判準**：
- Pass：列舉、clone、`annex get`、依 key 取物件全部成功且雜湊吻合；**在完整 `drive` scope 下**所有寫／刪／同名操作被 Drive 以權限理由拒絕（403／`insufficientFilePermissions` 之類），事後目標檔與資料夾不變。
- Fail：任一讀取失敗（含「clone 只有用 committer conf 才成功」）；或任何寫入／刪除生效。
- 假綠燈檢查：讀取成功必須附具體雜湊，不能只看 exit code；寫入失敗必須是 403（不是 404、不是唯讀 scope 造成的 403）、且事後由 committer 確認沒變；clone 必須證明來源是 SA（不能默默 fallback 到本機快取或 committer conf）；evidence 必須寫出 SA conf 的 scope 設定。

**證據**：`docs/spike/evidence/1.5-service-account-reader.md`。

**fail 的設計影響**：D3 的「每 profile 一個讀取身分」與 D5 的讀取路徑（SA 讀讀取視圖、以 annex key 取收容產出）不成立 → no-go 或改讀取實作並回設計。

## 1.6 只有 Actions 寫入權的 fine-grained token

**要回答的問題**：只有 `Actions: Read and write`、沒有 contents 權限的 PAT，能做什麼、不能做什麼？（觸發 workflow_dispatch、推 contents、改 workflow 檔、enable／disable workflow、cancel／刪除 run、讀 contents／secrets）

**前置**：`gh-pat-actions.txt`（記到期日）；測試 repo `FATESAIKOU/aistorage-spike` 與其 spike workflow（見第二節前置缺口）；一律以 `GH_TOKEN="$(cat ~/.config/aistorage-spike/gh-pat-actions.txt)" gh …` 使用，不 echo、不貼進 prompt 或 evidence。

**步驟**：
1. 證明 token 有效：`GH_TOKEN=… gh api repos/FATESAIKOU/aistorage-spike --jq .full_name` 成功（這是後續所有失敗的對照組）。
2. 觸發：`gh workflow run <spike workflow> --repo FATESAIKOU/aistorage-spike`；以 `gh run list` 確認出現 `workflow_dispatch` 的 run，記 run id。
3. 推 contents（用 gh 的 credential helper，不把 token 放進 URL 或 argv）：`GH_TOKEN=… git -c credential.helper='!gh auth git-credential' push origin HEAD:refs/heads/probe-pat` 與 `HEAD:main` 各一次；抄原始錯誤。另以 API 佐證：`gh api -X PUT repos/FATESAIKOU/aistorage-spike/contents/probe-pat.txt -f message=probe -f content=cHJvYmU=`。
4. 改 workflow 檔：`gh api -X PUT repos/FATESAIKOU/aistorage-spike/contents/.github/workflows/<file>`（帶當前的 sha）；預期被拒。
5. enable／disable：`gh workflow disable <file> --repo …`、需要時 `gh workflow enable <file> --repo …`；每次操作後 `gh workflow list` 確認狀態。**無論結果如何都要把 workflow 復原成啟用**，並記下用哪個身分復原。
6. cancel run：對步驟 2 的 run（或另觸發一個）執行 `gh run cancel <run-id>`；記結果。
7. 刪 run：`gh run delete <run-id>`；記結果（design 說這個 token 能刪執行紀錄；這是稽核相關的事實）。
8. 重跑：`gh run rerun <run-id>`；記結果。
9. 讀 contents 與 secrets：`gh api repos/…/contents/README.md`（預期被拒）；`gh secret list --repo …`（記能不能讀到名稱，不可能讀到值）。
10. **讀 run 產物與 cache（M4）**：對步驟 2 的 run 執行 `gh run view <run-id> --log`（下載 log）、列出與下載 artifact（`gh api repos/…/actions/runs/<id>/artifacts`；若有 artifact 再抓）、`gh cache list` 與 `gh cache delete <key>`（或 API）；另在 `probe-pat` 分支（若步驟 3 成功）或既有分支上 `gh workflow run <file> --ref <別的 ref>`，記能不能以別的 ref 觸發。**判定重點**：住民能不能讀到提交流程的 log／artifact、能不能刪 cache——若 log／artifact 含 Session 內容，住民就讀得到，1.3 的抹除範圍要涵蓋它們，D2 也要明文規定 workflow 不得輸出內容、不留 artifact。

**pass／fail 判準**：
- Pass：步驟 1、2 成功；步驟 3、4、9（讀 contents）被拒且錯誤是**權限類**（403／"Permission … denied"），不是認證錯；步驟 5〜10 逐項有結論；沒有任何一步能取得 secrets 的值或改動 repo 設定。
- Fail：token 能推 contents 或能改 workflow 檔（→ 住民可改 spike workflow 偷走 `RCLONE_CONF`，等於取得提交流程憑證 → 真本可被住民改寫，no-go 導向）；或認證本身不通（測試方法錯，重做）。
- 註：若步驟 5〜8、10 能做到 enable／disable／cancel／刪 run／讀 log／刪 cache，**不算 fail**，但必須寫進 1.9 結論：(a) 管理操作與提交的錯開（D2、6.5）不能依賴 workflow 開關或執行紀錄，只能靠 manifest 預檢；(b) 提交流程的 log／artifact／cache 屬於住民讀得到的範圍，抹除範圍與 workflow 設計（不輸出內容、不留 artifact）要照這個結論收緊。
- 假綠燈：步驟 3 的失敗若其實是 401／認證錯，不能記成「被拒」；必須先有步驟 1／2 的成功做對照。步驟 5 執行後無論結果都要檢查狀態已復原。步驟 10 若 repo 上根本沒有 artifact／cache，要寫「無可測對象」而不是記成「不能讀」。

**證據**：`docs/spike/evidence/1.6-pat-actions.md`（逐項指令、結果、run id、原始錯誤；不含 token 字串）。

**fail 的設計影響**：住民觸發與權限邊界不成立 → no-go 或改觸發方式（例如改用 GitHub App、或 workflow 設計改成被觸發也拿不到秘密）。enable／disable／cancel／讀 log／刪 cache 可行的話，D2「管理腳本先停 workflow」的做法不可靠，必須以 manifest 預檢為主；並回饋 1.3：抹除範圍要含 Actions 的 log／artifact／cache（或設計成根本不產生）。

## 1.7 opencode `export` 與 Session 取得（拆到最細）

**共同前置**：1.1 的 `ids.env` 與 `ollama-cloud-key.txt`；Mac 上 colima 的 Ubuntu 容器（裝 opencode 與所需依賴，記安裝方式與版本）；**只用驗證時新開的測試 Session，不碰真實 Session**；容器只掛 LLM key（1.7 不需要 profile 憑證）。opencode CLI 的實際語法依當版 `--help` 為準，把用到的完整指令寫進 evidence。

**共同假綠燈防護**：所有「結構穩定」的結論都要至少兩個獨立匯出（最好包含重開 process 後的一次）比對；「有變化」要用具體新內容驗證，不能只看檔案大小或 mtime；匯出檔案裡若混有每次都會變的欄位（例如匯出時間），要先找出並排除它，再談穩定。

**共同證據**：每個子項一份檔（見各子項），最後供 1.9 逐項判定。

#### 1.7a 容器內能定期匯出（可行性）
- **要回答的問題**：在 Ubuntu 容器裡，`opencode export` 能不能非互動地匯出一個指定 Session，輸出完整且可解析，並能列出本機所有 Session 與其 id？
- **步驟**：記 `opencode --version`；新開測試 Session 說幾句話；跑 export（記完整指令、exit code、格式（JSON／JSONL）、大小、是否含全部訊息）；找列舉 Session 與 id 的指令並執行。
- **能力邊界盤點（M6，只記名稱不記值）**：(1) `env` 的變數**名稱**清單，比對白名單（應該只有 LLM key 相關；出現任何 profile／Drive／GitHub 憑證的名稱即異常）；(2) `/proc/mounts`：列出所有掛載點，特別是 `docker.sock` 有沒有被掛進去、有沒有 host 路徑 bind mount；(3) 是否 privileged（`/proc/1/status` 的 CapEff 或 `docker inspect`）；(4) capabilities；(5) **註明 colima 預設會把 `~` 掛進 VM**——任何 `~` 底下的 bind mount 等於把 Mac 上的檔案給了容器，要逐一記下容器實際看得到什麼。完整白名單驗收在 5.1，這裡只記錄與標記異常。
- **pass／fail 判準**：Pass＝非互動匯出成功、輸出可解析且內容完整、能列出 Session 與 id、版本與依賴記下；能力邊界五項（含 `~` 掛載）都有記錄且沒有白名單外的憑證。Fail＝匯出只能互動觸發、輸出破損、或無法列舉 → D4 同步器不可行，回設計；任一白名單外憑證可見 → 提早觸發 no-go 條件「能力邊界不成立」。
- **證據**：`docs/spike/evidence/1.7a-container-export.md`。

#### 1.7b 以內容判斷有沒有新內容
- **要回答的問題**：同一個 Session 沒有新內容時重新匯出，能不能用內容或雜湊穩定判斷「沒有變化」；有新內容時一定判斷得出來？
- **步驟**：匯出 E1 → 記位元組雜湊與訊息層雜湊；不改動再匯出 E2；比對原始位元組與正規化後的內容，**列出所有每次匯出都會變的欄位**（若無則寫「無」）。再加一則訊息，匯出 E3；比對訊息層雜湊與新訊息。
- **pass／fail 判準**：Pass＝未變時訊息層比較一致（位元組層的差異都落在已列出的揮發欄位）、有變時一定偵測得到並可指出新訊息。Fail＝未變時無法穩定判定（會產生假變化）或變化被漏掉 → 違反 spec「定期同步」的冪等，回設計。
- **證據**：`docs/spike/evidence/1.7b-change-detection.md`（含揮發欄位清單與正規化規則）。

#### 1.7c 既有訊息的位置穩定性（接續點靠它）
- **要回答的問題**：同一個 Session 重新匯出（含重開 process、追加訊息、**進行中匯出**、**undo／編輯／revert**）時，既有訊息的順序、索引與 id 是否不變？
- **步驟**：
  1. 對有 N 則訊息的 Session 匯出，記每則的 index／role／（若有）message id／內容雜湊；追加 2 則後匯出；重啟 opencode 後再匯出；三次比對前 N 則。
  2. **進行中匯出（M5a）**：同步器每 10 分鐘跑一次時，同一個容器裡的 opencode 可能正在寫入。讓 opencode 一邊產生回覆、一邊在另一端同時跑 export 數次：記有沒有鎖、有沒有匯出到寫了一半的訊息、exit code 與輸出可解析性。
  3. **undo／編輯／revert（M5b）**：對既有 Session 執行 /undo、編輯一則舊訊息、revert；每次操作後匯出，記被影響訊息的 index／id／內容雜湊與後續訊息位置。
  4. 寫下「接續點要用什麼表示」的建議（優先 message id，其次索引＋快照）。
- **pass／fail 判準**：Pass＝前 N 則的 index 與 id（若有）完全不變，可據以定接續點；進行中匯出與 undo／編輯／revert 的行為有明確記錄（若會移除／改寫訊息，寫明哪些位置會失效）。Fail＝位置或 id 會漂移且無規律（依 design：接續點改以訊息 id 或由同步器保存完整快照，結論回步驟 3 與使用者決定）；或進行中匯出會產出無法解析的半截輸出而同步器無法辨識。
- **證據**：`docs/spike/evidence/1.7c-position-stability.md`。

#### 1.7d 壓縮對話之後的行為
- **要回答的問題**：壓縮（/compact 或當版等效功能）之後匯出長什麼樣：舊訊息還在不在、有沒有摘要項目、既有訊息位置變不變、Session id 變不變？
- **步驟**：做一個夠長的測試 Session，記壓縮前的匯出與訊息清單；觸發壓縮；匯出並比對：總數、被移除的訊息、摘要項目的表示、留下的訊息位置。
- **pass／fail 判準**：Pass＝行為有明確記錄，留下的訊息位置穩定；若壓縮會讓匯出**移除**舊訊息，這不算 fail 但必須寫進設計影響：Agora 的原始紀錄只能靠「壓縮前已同步的快照」保住內容，同步間隔（10 分鐘）與壓縮時機的關係要提醒使用者與 3.5 轉換器。Fail＝壓縮後位置漂移且不穩定（接 1.7c 的替代表示）或匯出損壞。
- **證據**：`docs/spike/evidence/1.7d-compaction.md`。

#### 1.7e 有沒有可信的結束或封存訊號
- **要回答的問題**：opencode 有沒有「這個 Session 已結束／已封存」的可信訊號？
- **步驟**：檢查本機儲存（DB 欄位／狀態檔）與 CLI，找結束、封存、刪除等狀態；逐一記錄候選訊號在哪、怎麼觀察、什麼時候寫入。檢查它是不是其實只是「閒置時間」或「程序結束」（這兩者不算可信訊號）。
- **pass／fail 判準**：Pass＝有可信訊號（記下觀察方式），或明確下了「沒有可信訊號」的結論並附檢查清單——後者讓 D4 的明確宣告繼續成立。Fail＝把閒置或程序結束誤記成可信訊號。
- **證據**：`docs/spike/evidence/1.7e-stop-signal.md`。

#### 1.7f skill 能不能取得目前的 Session id
- **要回答的問題**：在容器裡執行的 opencode，能不能可靠地知道自己**目前這個** Session 的 id（供認領使用）？
- **步驟**：新開 Session S，用 skill／工具／CLI 取得「目前 Session 的 id」；與 1.7a 的列舉結果比對必須等於 S 的 id；從 Session 內非互動地執行一次（模擬 skill 呼叫）。
- **pass／fail 判準**：Pass＝有確定性、可寫成指令的取得方式。Fail＝取不到或只能靠猜（例如從「最近一個」推測）→ D10 的認領不成立，**回步驟 3 跟使用者重新設計**。
- **證據**：`docs/spike/evidence/1.7f-session-id.md`。

#### 1.7g 能不能用指定內容啟動新 Session
- **要回答的問題**：能不能非互動地用指定內容啟動一個新 Session（供交接後開新 Session 用）？
- **步驟**：用 CLI（如 `opencode run "<內容>"` 或當版等效）啟動新 Session，帶入指定文字；確認新 id、匯出含該文字、且可再被列舉與匯出。
- **pass／fail 判準**：Pass＝可以，指令記下。Fail＝只能互動開啟 → 影響 D10/E2E 的開場方式，回步驟 3 與使用者確認。
- **證據**：`docs/spike/evidence/1.7g-start-session.md`。

#### 1.7h 子 Session（task 工具）與 fork 匯出時的樣子
- **要回答的問題**：task 工具產生的子 Session、以及 fork 出來的 Session，在匯出裡長什麼樣、跟母 Session 的關係怎麼表示？
- **步驟**：在測試 Session 觸發 task 工具；匯出母 Session，看子 Session 內容是否在裡面、以什麼結構呈現；找子 Session 自己的 id 並單獨匯出；使用 fork 功能（若當版有），比對 fork 的 id、與母 Session 的關聯欄位。
- **pass／fail 判準**：Pass＝結構有明確記錄，3.5 轉換器能把每一則訊息歸給正確的 Session。Fail＝子 Session 內容取不到、或母／子訊息混在一起無法區分、或 fork 會破壞 1.7c 的位置穩定性 → 影響 D4 同步器與轉換器設計。
- **證據**：`docs/spike/evidence/1.7h-subsessions-fork.md`。

#### 1.7i 匯出內容不含帳號與憑證
- **要回答的問題**：驗證用的匯出內容裡，有沒有帳號、LLM key 或其他憑證？
- **步驟**：對一個用過 LLM 的測試 Session 匯出後執行：
  - `grep -cFf ~/.config/aistorage-spike/ollama-cloud-key.txt <export>`（以檔案當 pattern，輸出只有次數，不曝露 key）；
  - `grep -cE 'GOCSPX-|ya29\.|1//0[0-9A-Za-z_-]|ghp_|github_pat_|-----BEGIN (RSA )?PRIVATE KEY-----' <export>`；
  - 檢視匯出頂層結構，列出所有 metadata／設定類欄位**名稱**（不貼值），確認沒有帳號、provider 設定或憑證區塊。
- **pass／fail 判準**：Pass＝實際 key 命中 0 次、憑證形狀命中 0 次、無帳號／憑證欄位。Fail＝任一命中或存在憑證區塊 → 違反 spec「Agora MUST NOT 保存來源應用的憑證」，同步器／轉換器必須過濾，並回設計確認「原始紀錄」的過濾規則。
- **證據**：`docs/spike/evidence/1.7i-export-secrets.md`。⚠️ 若真的命中，**只記命中次數、行號與類型，不貼命中內容**；這份 evidence 本身也要通過第三節的秘密檢查。

#### 1.7j colima 容器時鐘漂移（相對於外部時間來源；M5c、N1）
- **要回答的問題**：Mac 睡眠喚醒後，colima VM／容器時鐘會不會落後到足以產生假的新鮮度警告？快照時間取自容器時鐘，漂移量有多少？
- **步驟**：用**不需要任何憑證**的外部時間來源對照（N1）：容器內 `curl -sI https://www.googleapis.com` 取回應的 `Date` header（精度到秒即可），或與 Mac 主機的 NTP 時間比對。記下容器 `date -u` 與外部時間的差距；讓 Mac 睡眠再喚醒，喚醒後立刻再量一次；再等 1〜2 分鐘量一次（看是否自行校正）。記下漂移方向（落後／超前）與秒數。**不要在 1.7 的容器掛 committer conf 來讀 Drive `createdTime`**——那會讓 1.7a 的能力邊界盤點標出白名單外的憑證、誤觸 no-go；`createdTime` 的上限夾制驗證移到 1.8 的 runner 或 3.3。
- **pass／fail 判準**：Pass＝漂移有具體數字與方向（相對外部時間），且已評估對新鮮度判定的影響。Fail（視為 caveat，不單獨 no-go）＝漂移量大到足以讓「未達新鮮度」警告誤觸或快照時間被 D4 的上限錯誤夾制 → 寫進 1.9 的 D4／1.8 caveat，建議同步器記錄快照時間前先對外部時間校正。
- **證據**：`docs/spike/evidence/1.7j-clock-drift.md`。

## 1.8 量測提交流程

**要回答的問題**：提交流程在全新 runner 上 clone 與 push 的耗時、每月 Actions 分鐘數的估算、concurrency group 的排隊與取消行為、收件匣空時能不能不 clone 就結束。

**前置**：1.1（`rclone-committer.conf` 存進 repo secret `RCLONE_CONF`、`gh-pat-actions.txt`）；1.2 的專屬前綴 `agora-load/` 與 spike workflow（見第二節前置缺口）；合成資料產生器（本地小工具即可，不入 repo 也可）。**收件匣的資料夾 id（1.4i 產生的 `aistorage-spike-inbox-mac-opencode`）以 workflow 的設定（非秘密，例如 vars 或 workflow 檔裡的常數）傳入**（N2）：收件匣依第三節規定在「我的雲端硬碟」根目錄、不在 `SPIKE_FOLDER_ID` 之下，而 committer conf 的根目錄是 `SPIKE_FOLDER_ID`，用路徑找不到它；workflow 一律以 `--drive-root-folder-id <inbox id>`（或 Drive API）依 id 列舉。備註：量測用的 workflow 是**合成 spike**（比真提交流程簡化），報告要註明它與真流程的差距（真流程多了驗證、轉換、讀取視圖、索引）；1GB 內容的 Actions **cache 用量**在免費方案跟 MyLinuxPool 共用，量測中順便記錄。

**步驟**：
1. 建工作 repo：`seq`／腳本產生約 500 個 commit（每個幾 KB，讓 push 次數到位）；1GB 內容用 `dd if=/dev/urandom` 產生（設 `annex.largefiles` 讓它進 annex）。
2. 在本地（或一次性容器）用 committer conf 把 500 次 push 推上 Drive repo；記錄本地每次 push 耗時與總時間。
3. runner clone：觸發 spike workflow 做 `git clone annex::…`（從零），**把「git 歷史／bundle 取回」與「`annex get` 內容」分開計時**（L；提交流程平常不需要 annex 的內容，混在一起會高估）。至少跑 2 次記差異（runner 快取狀態可能不同）。
4. runner push：在 runner 上改一個檔 commit＋push，量 push 耗時。
5. 定時提交負載：記錄連續數次的 clone＋push 總時間（模擬提交流程本身）。
6. Schedule 與主動觸發：觀察 repo 的 `schedule` run 實際執行時間間隔（是否延遲／跳過）；用 PAT 觸發 `workflow_dispatch` 記結果。
7. 連發與取消：以 PAT `gh workflow run` 連續觸發 3 次以上（間隔短），用 `gh run list` 記錄每個 run 的狀態與時間線（executing／pending／cancelled），驗證 concurrency group 是否為「一個執行中、一個等待中、第三個取消等待中的」；確認每次 run 都掃完整收件匣（用 log）。
8. **空收件匣（M1、N2）**：清空收件匣後觸發一次，確認 workflow **以資料夾 id** 列出收件匣、在**安裝 git-annex 之前**就結束（安裝工具不該是空跑成本的一部分），記其計費分鐘數。
9. 估算（M1、N3）：**Actions 計費是每個 job 無條件進位到整分鐘**，估算一律用計費分鐘、不能用實際耗時。**不要只給一個數字**：以量到的單次計費分鐘，做出下表（例行負載＝排程空跑＋非空提交），讓使用者在 1.9 直接選排程間隔與主動觸發的預算：

   | 排程間隔 | 空跑計費/月 | ＋主動觸發 5 次/天 | ＋主動觸發 20 次/天 |
   |---|---|---|---|
   | 每 3 小時（240 run/月） | 240 | 240＋150＝390 | 240＋600＝840 |
   | 每 6 小時（120 run/月） | 120 | 270 | 720 |
   | 每 12 小時（60 run/月） | 60 | 210 | 660 |

   （表中「主動觸發每次 1 計費分鐘」為起算假設：以實測的「非空提交」計費分鐘替換重算，表要附實測值與算式。）對照門檻：≤300 pass；300〜800 caveat；>800 且無壓縮做法才 no-go（已使用者確認）。schedule 的實際延遲與失敗重試另記、另加。

**pass／fail 判準**：
- Pass：clone／push 成功且耗時有數字（歷史與 annex 分開）；空收件匣確認依 id 列舉、在安裝工具前結束、不 clone；排隊與取消行為與 design D2 描述相符或落差有記錄；計費分鐘表給出、例行負載 ≤300 分鐘（且列出可行的排程槓桿）。
- Caveat：例行的計費分鐘在 300〜800 之間（可行但要盯著用量、準備壓縮做法或自架 runner）。
- Fail：clone 或 push 在 runner 上失敗；空收件匣仍 clone／仍需先裝工具；計費分鐘 >800 且沒有可行的壓縮做法（**no-go 條件**之一）。
- 假綠燈檢查：runner 耗時必須在「全新 runner、無熱快取」的前提量（用 `actions/cache` 要記錄並標明）；排隊測試不能只觸發一次就宣稱「不會互相覆蓋」；每月估算不能只乘以理想間隔、忽略 schedule 延遲與失敗重試，也不能用實際耗時代替計費分鐘；確認「不 clone」要看 log 或時間，不能只看 workflow 成功。
- 排隊測試不得在 run 之間用等 run 完成再觸發的序列化方式，那測不到 concurrency。

**證據**：`docs/spike/evidence/1.8-commit-pipeline.md`（時間線、耗時表、計費分鐘估算式）、`docs/spike/evidence/1.8-runner-log.txt`（至少一次完整 run 的 log）與 `docs/spike/evidence/1.8-schedule-observed.md`（定時間隔觀察；若觀測期不足，明確寫「未達可信樣本數」）。

**fail 的設計影響**：design Risks「git-remote-annex 每次 push 多一個增量 bundle、耗時成長」與 Actions 預算那兩條升級；回步驟 3 考慮 Actions cache、定期重整 bundle、或自架 runner（都在 backlog 的擴張點內）。若耗時隨 500 次 push 明顯劣化，1.2 的結論也要標註成長曲線。

## 1.9 報告的彙整規則

**要回答的問題**：1.1〜1.8 的逐項結果怎麼彙整成給使用者的 go／no-go 建議？

**判定四類（M7）**：每個子項預先標好它 fail 時屬於哪一類：
- `go`：pass。
- `go-with-caveat`：pass，但有待辦或已知限制，不改設計。子項的 caveat 情形（如同 project 不隔離、時鐘漂移、只有一個 SA）。
- `需要改設計`：結果動搖某個 D 決定，但替代方案存在；寫成給使用者的提問，**附建議答案**（照 REQ 的提問慣例）。例如 1.7f／1.7g fail（D10 認領要重新設計）、1.4e fail（bootstrap 要改）、1.7c／1.7d 位置漂移（接續點表示要改）。
- `no-go`：觸及第一節任一 no-go 條件 → 停止、重新選型。例如 1.4 跨 project fail、1.2 靜默覆蓋、1.3 抹除不乾淨、1.5 寫得進去、1.6 token 能碰真本、1.8 超預算、1.1 憑證不穩、1.7 白名單外憑證可見。

**子項 fail 的預標類別**（彙整時逐項對照；不在表上的子項以「go-with-caveat」為預設，除非動搖 D 決定）：

| 子項 | fail 類別 | 說明 |
|---|---|---|
| 1.1 | no-go | 憑證不穩，提交流程全滅 |
| 1.2（clone／push／revert／中斷恢復） | no-go | D1 核心不成立 |
| 1.2（manifest 預檢） | 需要改設計 | 預檢不可行時，管理與提交的錯開只剩 workflow 開關；與 1.6 的結果一起考慮 |
| 1.3 | 需要改設計／no-go | 看是「有替代做法」（刪檔重建，改設計）或完全清不掉（no-go） |
| 1.4a | go-with-caveat | 同 project 不隔離＝caveat（D3 各 project），跨 project 隔離即可 |
| 1.4b〜1.4e | no-go（跨 project fail 時） | 隔離不成立 |
| 1.4f | no-go | design 明定的 no-go |
| 1.4g〜1.4h | 需要改設計 | D5 定位方式要改 |
| 1.4i | 需要改設計 | 2.3／D5 要改 |
| 1.5 | no-go | 讀取身分／讀取路徑不成立 |
| 1.6 | no-go／go-with-caveat | 能碰真本＝no-go；只能 enable／cancel／讀 log／刪 cache＝go-with-caveat（**前提是 1.2 的 manifest 預檢 pass**），同時回饋 1.3 抹除範圍與 D2「workflow 不輸出內容、不留 artifact」；若 1.2 預檢也不可行才升級為「需要改設計」 |
| 1.7a | 需要改設計 | 同步器不可行 |
| 1.7b | 需要改設計 | 冪等不成立 |
| 1.7c〜1.7d | 需要改設計 | 接續點表示要改（回步驟 3 與使用者定案） |
| 1.7e | go-with-caveat | 「沒有可信訊號」是預期結論，明確宣告續用 |
| 1.7f〜1.7g | 需要改設計 | D10 認領／開場要重新設計 |
| 1.7h | go-with-caveat／需要改設計 | 子 Session 結構影響 3.5 轉換器 |
| 1.7i | 需要改設計 | 原始紀錄的過濾規則要改 |
| 1.7j | go-with-caveat | 對時做法進 D4 待辦 |
| 1.8 | no-go（>800 且無壓縮做法）／go-with-caveat（300〜800） | 已確認的門檻 |

**步驟**：
1. 報告建在 `docs/spike/report.md`。逐項判定（1.7 的 a〜j、1.4 的 a〜i 逐子項），每項附 evidence 檔名與一句判準摘要。
2. 彙整矩陣：一列一子項，欄位＝結果、**依賴的前提**（M7，例如 1.4 依賴 H3 的 project 拓撲、1.5 依賴 H6 的 root_folder_id、1.8 依賴合成 workflow 與收件匣資料夾 id、1.6 的 caveat 依賴 1.2 的 manifest 預檢）、證據、是否觸及 no-go 條件、fail 類別、對設計（D 編號）的影響。依賴的前提沒滿足的 caveat 不得當成 pure pass。
3. 建議：有任何 no-go 條件成立 → 建議 no-go；有「需要改設計」→ 逐條寫成給使用者的提問附建議答案，不偷偷降 spec；全部 go／go-with-caveat → 建議 go，把 caveat 轉成第 2 組之後的待辦或設計補充。
4. **已知 caveat 一定要列**（不因其他項 pass 而省略）：
   - **只有一個 SA**：驗不到 D3「每個 profile 各自一個讀取身分」與以 profile 為單位的撤銷（L）。
   - **7 天 refresh token 過期驗不到（M2）**：驗證期不到 7 天，1.9 當天只以 Console「正式版」狀態判定為 go-with-caveat；排一次「首次授權後第 8 天」的複查，複查失敗就回頭處理，但不擋 go。
   - **收件匣散在根目錄（N2）**：若 1.4e pass（`drive.file` 建不進 committer 的資料夾），收件匣必然在「我的雲端硬碟」根目錄、不在 `SPIKE_FOLDER_ID` 之下，提交流程只能以資料夾 id 追蹤。回饋 design D3 與 tasks 2.3；清理與抹除的範圍也照這個形狀寫（收件匣 id 要列入 resources.md 與抹除清單）。
   - 同 project 不隔離（若發生）；1.7j 的時鐘漂移數字。
5. 資源清單：把驗證期間建立的實體（專用帳號、Drive 資料夾 id、四個 OAuth client 名稱、service account email、PAT 名稱與到期日、測試 repo、spike workflow，不含秘密的值）依 `docs/resources.md` 的格式填進去。
6. 秘密檢查：對 `docs/spike/evidence/`（排除 `1.9-secret-scan.md`）與 `report.md` 跑第三節的兩道檢查（形狀掃描＋實際值比對），確認乾淨後才交給使用者。
7. **清理（在報告送出、使用者判定 go／no-go 之後才做；L）**：刪除驗證用的 Drive 資料夾——含 committer 的 `aistorage-spike/` 與 `drive.file` client 在根目錄建的 `aistorage-spike-*` 資料夾（含收件匣；N2）——永久刪除垃圾桶、撤銷 refresh token、撤銷 PAT 與 SA 金鑰、刪除兩個 GCP project、把 `RCLONE_CONF` 從測試 repo 移除（測試 repo 本身依使用者決定保留或刪除）。清理完成後更新 `docs/resources.md`。清理步驟與結果另存 `docs/spike/evidence/1.9-cleanup.md`。
8. 交使用者判斷 go／no-go（tasks 1.9）；未經使用者確認，不開始第 2 組。

**pass／fail 判準**：
- Pass：每個子項都有明確判定、fail 類別與 evidence 檔；沒有任何子項以「看起來正常」結案；no-go 觸發檢查與依賴前提檢查完成；資源清單與秘密檢查完成；清理已排定（或已執行）並有紀錄。
- Fail：有子項缺判定、或缺 evidence、或 evidence 未通過秘密檢查（此時**不得提交**，先修正）。

**證據**：`docs/spike/report.md` 本身；`docs/spike/evidence/1.9-secret-scan.md`（grep 輸出，含 clean 字樣與掃描時間）；`docs/spike/evidence/1.9-cleanup.md`（清理紀錄，判定之後）。
