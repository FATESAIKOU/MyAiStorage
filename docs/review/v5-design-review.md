# v5 設計文件的非作者審查

- 對象：`angareion-v5`（`git diff angareion-v0..angareion-v5`，`eb9d363` 為頂）與 `sidecar-v5`（`git diff e5b246a..sidecar-v5`，頂是 `e6617ab`，含 `docs/review/v5-impact.md`）；依據是本人的 v5 變更與裁定兩份文件。
- 審查者：架構師（Claude Sonnet 5.5，非作者）。只讀文件與既有程式；對照了 LLMGateway 的 `scope/gateway-v5`（只讀 openspec）與 `sidecar-v0-agy4` 的程式。
- 兩個 change 都過 `openspec validate --strict`。

## 結論：**改完 High（2 項）後可以開設計 PR。** Medium 建議同一輪處理，Low 可以帶著開。

本人的變更與裁定幾乎都落進了文件、沒有寫反；問題集中在「設計沒說清楚的地方會讓實作者踩進去」：sidecar 去掉 `env_for` 之後與 agora 的啟動順序對不上，以及 Angareion 的 workflow 在公開 repo 上沒有寫授權與注入的要求。

## 1. 本人的變更與裁定逐條核對

| 條目 | 結果 |
|---|---|
| agent 命名 `<repo>-<pm\|arch\|impl\|test>[-<id>]`，不做身分認證 | 兩邊都寫了；**文法沒定義、沒有 Scenario**，見 M7 |
| channel 是英文名、介面不出現 repo／issue | 寫了（design §4、§5；spec 有 Requirement） |
| 拿掉 `--member`；`send --to` 選填；無 `to`＝廣播算進「寄給我的」 | 寫了，spec 有 Scenario（建 channel 不收成員、不給 to、寄給我的） |
| `channel create` 三步、`channel delete`（關不刪＋unregister）、對應持久化 | 寫了（§6、§7、§8） |
| 兩個 create 的競爭（workflow concurrency）、觸發權限、pull 週期、失敗 | 寫了（§9、§10、§11）；缺口見 H2、M4、M5 |
| backend 設定檔走 PR、channel 設定檔由 workflow 更新 | 寫了；backends.json 的 PR 與 channels.json 的 workflow 分得清楚 |
| sidecar `open` 只收 agora id 或不給；fork／merge 入口拿掉、store 能力保留 | 寫了；spec 有「不收 fork／merge」Scenario。殘留：`proposal.md` 第 9 行（What Changes 第一條）還寫「new／continue／fork／merge」（L1） |
| `/jobs` 保留、不加功能 | 寫了；程式不動；`merge_not_allowed_in_job` 已刪 |
| opencode 改寫 v2、先 spike（`--standalone` 私有 server）、worker 映像升 v2 | design D6、tasks 1.4／3.2／5.8 寫了；**spec 還是 v1 的規則**，見 M2 |
| 拿掉 `env_for`，憑證與環境變數當 `launch` 參數 | 寫了，**但與 agora 啟動順序對不上**，見 H1 |
| 立刻讀＝先打斷再送；`state` 拿不到恢復時間報現在＋1 小時；`stop` 溫和／強制、退出＝結束整個 agent 程序 | 寫了；`stop` 的語意與 SIGTERM 機制矛盾，見 M3；「更正」與 LLMGateway 衝突，見 M1 |
| `cooldown` 只送 `until`；不用 `lgw connect`、直連本機 port | 寫了；與 LLMGateway v5 對照見第 2 節 |
| SubscriptionSource 是內部介面 | 寫了（D7） |
| 兩邊的「待本人確認」 | Angareion §10 與 tasks 第 5 節寫得清楚（A／B／C、token 範圍、main 直推、PR #41）；sidecar 側唯一的待確認（欄位名）在 D7 與 spec 之間已對齊（`base_url`），design D7 仍留「欄位名待 PM 確認」字樣 |

**作者自己加的（本人沒說，要在說明時讓本人知道）**：`channel create --backend <名>`（多個 backend 時必給）、`config_pull_interval` 預設 900 秒、註冊重試 2 次、workflow 的 sha 重試上限 5 次、註冊 issue＋label 這整套傳遞方式（本人只問了權限，作者提了 A／B／C 且建議 C，已標待確認，這是對的）、同名 `channel create` exit 1、`config_dir`／`registry_repo` 個人設定。都合理，但 `--backend` 是新增的對外選項，建議明說。

## 2. 兩邊與 LLMGateway v5 的一致性

- agent 命名、角色字（`pm|arch|impl|test`）兩邊一致；Angareion 的收件人與 sidecar 的 `id` 是同一個字串。
- `cooldown` 只送 `until`、直連本機 port、「拿不到恢復時間用現在＋1 小時」與 LLMGateway `scope/gateway-v5` 的 subscription-registry 一致；`list` 的 `cooldown` 單一 `{until}`、`available`＝已部署且沒有未來的 `until` 也和 sidecar 的 `at` 算式、「沒部署＝available false 且沒有 `until`」對得上。
- **不一致**（M1）：LLMGateway「重複登錄時取較晚的 `until`」，sidecar D4 寫「`cooldown` 覆寫同一把的 `until`」。
- **對不上**（L10）：LLMGateway v5 的新錯誤碼是 `invalid_until`、`until_out_of_range`；sidecar 只寫「超出範圍類」，spec 的 Scenario 也沒有用碼名。
- Angareion 與 sidecar 之間沒有 channel 概念的耦合（sidecar 沒有 channel），沒有衝突。

## 3. 問題

### High

**H1. 去掉 `env_for` 之後，continue 路徑的環境變數沒有地方放（design D3／D4／D6 對不上 agora 的啟動順序）。**
- 現有程式：`core.py:254-286`（及換把 `759-778`、重開 `882-892`）是 `get` → `driver.env_for(sub)` → `store.start(…, env=env)` → `driver.launch(running, env, workdir)`。因為 `agora continue --headless` 自己是 agent 的父行程，subscription 的憑證與 `OPENCODE_CONFIG_CONTENT` 等必須在 **`SessionStore.start` 時就放進 agora 行程的環境**，`launch` 只是接上已經開起來的 agent。
- v5 設計寫「憑證與環境變數直接當 `launch` 的參數」「driver 在 `launch` 裡組出子行程的完整環境」，而順序仍是 `start` → `launch`（D4 的 open 流程）；`SessionStore.start` 簽名還留著 `env`，但沒說它從哪來。`launch` 只在 `new` 模式（沒有 agora 行程）才真的啟動 agent。
- 連帶：`no_model`（沒有可用模型來源，原本在 `env_for` 丟、open 回 400，這時什麼都沒開）現在寫成「`launch` MUST 丟錯」，但 `launch` 在 `start` **之後**：agora 行程已經起來、Session 已上鎖、可能已寫過一次，失敗要怎麼收拾（停 agora、放鎖、不留半成品）設計沒說，spec 的 `no_model` Scenario 寫「什麼都沒開」會被實作違反。
- 建議（二選一，請 PM 決定）：(a) 保留一個「由 subscription 組環境」的純函式（不叫 `env_for` 也行，例如 `driver.build_env(credentials)`），在 `start` 之前呼叫，結果同時給 `start` 與 `launch`；`no_model` 照舊在 `start` 之前失敗。(b) 照現在寫，但明寫 `start` 之前由 driver 先驗證（`validate(credentials)`）、環境怎麼傳給 `start`。本人要的是「不要有 `env_for` 這個介面」還是「不要這個名字」，建議問一句；只拿掉名字最省事。

**H2. Angareion 的 `register-channel`／`unregister-channel` 是「公開 repo 的 issue 事件觸發、直接寫 main」，設計沒有寫授權來源與注入的要求（design §9、§10）。**
- MyAiStorage 是 public，任何 GitHub 使用者都能開 issue；§10 的 C 方案是「`on: issues`，過濾 label」。label 只有有 triage 權限的人能加，所以「帶 label」其實是門，但設計沒有把它寫成要求，也沒寫要驗**誰**加的 label／誰開的 issue（`github.event.sender`、`author_association` 或 token 所屬帳號）。實作者照文字寫會漏。
- 請求內容（標題、本文 JSON）來自 issue，沒有寫「MUST NOT 把它插進 shell／`${{ }}` 展開、一律經環境變數交給腳本、用嚴格的 JSON 解析與 channel 名的正規式」。在可以寫 main 的 workflow 裡，這是標準的 script injection 缺口。
- 另外沒要求 workflow 驗證 `{backend, issue}` 指向的實體真的存在（至少 issue 編號存在於該 backend 的 repo、標題等於 channel 名），否則 register 可以把名字指到任何 issue。
- 建議：§9 加一條「授權與輸入」：只處理「由指定帳號開立（或指定 label 由指定帳號加上）」的請求；請求只經環境變數傳入；先驗 channel 名、backend 名、issue 存在且標題相符；任何不符一律在請求上回報、不動檔。spec 的 register Requirement 補一個「請求不是授權帳號開的」與「issue 不存在」的 Scenario。

### Medium

**M1. `cooldown` 的「更正」與 LLMGateway 的語意相反，而且沒有人去更正（sidecar design D4、spec 144 行附近）。**
LLMGateway v5：「重複登錄時取較晚的 `until`」。sidecar 寫「下次刷新有時間了再更正（`cooldown` 覆寫同一把的 `until`）」。(1) 若先報現在＋1 小時、之後測到的真實時刻更早，LLMGateway 不會縮短；更晚則可以延長，所以只有「延長」成立，要把文字改成「更晚才有效」。(2) 更嚴重的是：額度用完之後 agent 已經 stop、進 `waiting-quota`，**沒有任何程式會再刷新那一把**，沒有「下次」。要嘛刪掉「更正」這句，要嘛寫清楚誰、何時重報（例如自動重開 job 到時挑不到又被拒，才再報）。另外「現在＋1 小時」對 5 小時窗口意味著每小時一次「開起來又用完」，這是本人接受的取捨，但建議在 Risks 寫一行。

**M2. sidecar spec 的 opencode 規則還是 v1，與 design「v1 作廢、不得照 v1 猜」互相矛盾（spec 337–372、422–430 行；全文沒有出現 `v2`）。**
provider id 對應（`opencode-zen`→`opencode`）、`OPENCODE_CONFIG_CONTENT` 形狀、`POST /message` 背景執行緒與 1 小時逾時、`/session/status` 只用於 retry 等都是 v1 的實測；v2 要先 spike。目前這些仍是 MUST 與 Scenario，實作者與審查者無從判斷該遵守哪個。建議：這些 Scenario 保留但標成「（v1；spike 後依 tasks 1.4 重定）」，或搬到 design 的 v1 保留段；spec 加一個 Requirement「opencode driver 的規則以 v2 spike 的結果為準」，不再有具體的 v1 MUST。

**M3. `stop`「溫和＝閒了才退」與機制「對 agora 行程送 SIGTERM」矛盾，close 對忙碌的 agent 會怎樣沒說（D6 停下、spec 58–66 行）。**
SIGTERM 送出去，agora 轉給 agent，agent 會被中斷，不是「等手上這輪做完」。若真要等閒，機制是「輪詢 driver 的 `state` 到 idle 再送 SIGTERM」，那麼 `close_timeout`（預設 30 秒）對一輪好幾分鐘的 agent 幾乎一定逾時，接著「SIGKILL agora」＝不做最後一次寫回，比 v0（SIGTERM 轉給 agent 並寫回）更容易掉對話。建議明寫三段：等閒（上限 X）→ SIGTERM（agora 溫和停、寫回）→ SIGKILL；並說明 `close_timeout`／kill timeout 公式是否改變（目前寫「不變」，但多了一段等待）。spec 66 行「等手上這輪做完才退」要對應一個可測的 driver 層描述。

**M4. `channel create` 之後馬上 `send` 會失敗最久 15 分鐘（design §6、§11）。**
create 不等落地，本機複本要等下一次 lazy pull（預設 900 秒）；而「複本裡沒有就 exit 1（channel 不存在）」。最常見的流程「建 channel → 馬上送」會被擋，其他隊員也要等一個 pull 週期才看得到新 channel。建議：查無該 channel 時**忽略間隔、立刻 pull 一次**（加最短間隔如 30 秒防止刷新），`channel create` 成功後也可以輪詢 pull 到出現為止（有上限，例如 2 分鐘，逾時印提示但 exit 0）。spec 加對應 Scenario（「複本落後時，查無 channel 會先 pull 再判斷」）。

**M5. 註冊請求可能被 GitHub 取消而沒人重跑；同名競爭的輸家收不到結果（§9、§6、Risks）。**
設計正確地指出 pending run 會被後來的取消，但補救只是「沒有完成記號＝看得出來；人工重跑」——`channel create` 已經 exit 0、沒有人在看。建議 workflow 一次處理「所有尚未完成的註冊／註銷 issue」（drain：每次 run 掃 open 的帶 label issue、逐一處理、冪等），這樣被取消的那個會被下一次 run 帶走；加一個定時 sweep（`schedule`）當保險。另外兩個 create 同名時輸家的 exit 是 0，之後它拉到的對應指向勝出者的實體，輸家自己開的實體成為孤兒；至少在 design 寫「輸家看不到結果，只有註冊 issue 上的留言」，並考慮 `channel create` 提供 `--wait`（等落地並回報成功或同名衝突）。

**M6. 建議的 C 方案（註冊 issue 開在 MyAiStorage）與 repo 的工單規則衝突，設計沒有算這筆成本（§10）。**
MyAiStorage 的 AGENTS.md：issue 是工單，沒有 `層級:` label 的會被 MyPMO 每天的檢查列為「樹外」並通知本人。每次 create／delete 開一個註冊 issue＝每次都產生一筆樹外工單與通知。§10 的比較表沒有這一欄。建議把這點寫進 §10，並提出替代：註冊 issue 開在專用的 registry repo（個人設定已有 `registry_repo`，但 channels.json 在 MyAiStorage，workflow 要跨 repo 寫，需要權限評估）；或用一個長期存在的、有正確 label 的「註冊簿」issue，每次請求是一則留言（`issue_comment` 觸發，授權用留言作者判斷）。這是本人確認 §10 時要看的資訊。

**M7. 命名文法沒定義，spec 與 scenario 互相不一致。**
Angareion：`validate_address` 只認 agent 名，但沒給文法（repo 名含 `-` 時 `my-repo-impl-2` 怎麼拆？`<id>` 允許哪些字元？），Scenario 只有兩個拒絕案例，沒有「接受」案例與邊界（`LLMGateway-pm` 通過、角色不在清單、空 id、大小寫）。sidecar：Requirement 寫「`id`（共通命名…）」是 MUST，但沒有 Scenario，其餘大量 Scenario 仍用 `t1`、`claude-personal`——若命名是強制的，這些 `id` 都不合格；若只是慣例，要寫「sidecar 不驗證格式，只是慣例」（程式的 `valid_id` 字元集也比命名寬）。請 PM 擇一並補 Scenario；Angareion 建議給正規式（例：`^[A-Za-z0-9][A-Za-z0-9._]*-(pm|arch|impl|test)(-[A-Za-z0-9_.]+)?$`，並說明 repo 名裡的 `-` 靠右側角色字解析）。

**M8. design 裡的行為沒有對應的 Scenario，不能寫成單元測試（Angareion）。**
spec 只有 `create`／`delete` 的成功與少數失敗；缺：多 backend 時沒給 `--backend`（exit 1）與名字不存在（exit 1）；第 1 步開實體失敗（exit 2、什麼都沒註冊）；註銷前關實體失敗（exit 2、不送註銷）；404 視同已關；註冊送出重試 2 次的次數；pull 的鎖（同機並行）、逾時 20 秒、`config_pull_interval=0`、`wait` 輪詢中不 pull；workflow 的 5 次重試用盡怎麼回報；冪等的「完成記號」格式（§9 提到卻沒定義）。建議補到 channel-config spec 與五個介面 Requirement。

### Low

- **L1** `sidecar-v0/proposal.md` 第 9 行（What Changes 第一條）仍寫「new／continue／fork／merge」；design D7 仍有「欄位名待 PM 確認」。
- **L2** sidecar `tasks.md`：第 5 節（5.2、5.4、5.5）與 2.4、2.5、3.2 的內容大量重疊（同一件事寫兩次），容易重複實作或漏勾；建議把舊項目改寫成指向 5.x，或把 5.x 併進原項目。
- **L3** Angareion spec「token 的位置與讀法」寫「權限範圍依 design §10 的裁定」——spec 引用一個還沒定案的設計項目；建議在本人確認前寫成「權限範圍見 design」，確認後再寫死。
- **L4** pull 用 `git pull --ff-only`：clone 若有本機改動或 detached HEAD 就會失敗，建議改 `git fetch` 再從 `origin/main` 讀檔（`git show`），不依賴工作樹；「上次 fetch 的時間」存在哪裡（例如 `FETCH_HEAD` 的 mtime）、第一次的 clone 由誰做，設計沒說（複本不存在目前是 exit 2）。
- **L5** 「寄給我的」＝掃所有 channel：每次 `wait` 輪詢要對每個 channel 的實體各查一次，channel 變多後 API 用量線性增加；ETag 只在單頁時有效。第一期可接受，建議寫進 Risks。
- **L6** `channel delete` 之後的短時間內，其他人用過期的複本仍能對已關閉的 issue 送留言（GitHub 允許）：訊息靜靜進了死 channel。可在 Risks 提一句；`channel delete` 的「送註銷失敗」沒有 Scenario（重跑是安全的：關已關的實體是冪等）。
- **L7** sidecar：重開 exited 的 id 的 Scenario 寫「接續它的 Session」，但 v5 的 `open` 要自己帶 `session`；沒說呼叫方要從 `GET /agents` 的 `session` 取回再帶。fork 拿掉後每次接續都把 `sidecar:<id>` 附加在原 Session 上（累積）；同一個 Session 被兩個 agent 同時接續時 agora 的鎖會擋，D4 的錯誤表沒有這一列。
- **L8** sidecar：D7 重寫成自己的 `Result`／對應表（直連 HTTP），與 LLMGateway 保留的 `lgw.client` 是兩份實作；本人要的是相依最低所以合理，建議在測試計畫加一條「打 LLMGateway 的真實 loopback 服務（不經 `lgw`）」，避免兩邊的路徑與錯誤碼各自漂移。
- **L9** PR #41 提案（堆疊 PR）：**合理，同意推薦做法**（工單一對一、#41 不被撐大、docs-only 小 PR 好審）。兩個細節：(a) 若 #41 用 squash merge，`angareion-v5` 仍帶著 v0 的原 commit，base 改指 main 後 PR 會重新顯示 v0 的 commit；#41 合併後要 `git rebase --onto main angareion-v0 angareion-v5`（或 merge main）。(b) sidecar 側同理：`sidecar-v5` 應以 `sidecar-v0` 為 base 堆疊；它的設計變更直接改在 `sidecar-v0` 這個 change 目錄裡（沒有另開 `sidecar-v5` change），所以 #31 與 v5 設計 PR 的 diff 會在同一批檔案上，合併順序要跟 PM 對一下。
- **L10** sidecar spec／design 寫「`until` 超出範圍類」：請直接寫 LLMGateway v5 的碼名（`invalid_until`、`until_out_of_range`），Scenario 用碼名，才可以用假 port 測。

## 4. Scenario 可測性

- 能直接寫成單元測試：sidecar 的 until／現在＋1 小時、`open` 的 id／不給 session、`poke` 的 interrupt、`/jobs` 不變；Angareion 的位址、廣播、設定檔讀取與 pull 失敗沿用、workflow 腳本的併發／同名／冪等（可注入的檔案 API，tasks 已寫）。
- 目前寫不成測試的：M7（文法）、M8（缺 Scenario 的行為）、M2（v1 與 v2 規則並存）、H2（授權與輸入）、M3 的「等這輪做完才退」（需要一個 driver 層的可觀察行為）。

## 5. 建議順序

1. 先處理 H1（問本人一句「去 env_for」要不要保留純函式）與 H2（補授權、注入、實體驗證）。
2. 同一輪改 M1、M2、M3、M4、M5、M6（M6 影響 §10 要問本人的內容）。
3. M7、M8、Low 可在設計 PR 的審查回合補。
4. 之後請非作者再看一次 H1／H2／M1／M3 的改動。
