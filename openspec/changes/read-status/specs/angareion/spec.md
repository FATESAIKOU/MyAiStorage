## RENAMED Requirements

- FROM: `### Requirement: 六個介面`
- TO: `### Requirement: 七個介面`

## MODIFIED Requirements

### Requirement: 七個介面
Angareion 對外 MUST 只有七個介面：建 channel、刪 channel、送訊息、確認有沒有新訊息、讀訊息、查詢訊息（唯讀）、查詢已讀狀態（唯讀）；指令分別是 `angareion channel create`、`angareion channel delete`、`angareion send`、`angareion wait`、`angareion read`、`angareion log`、`angareion status`。MUST NOT 有 ack、已讀回條或限流，也 MUST NOT 有任何會寫入、或會由平台產生回條的已讀機制；`log` 與 `status` 是唯讀查詢（語意見「唯讀查詢 log」與「唯讀查詢 status」），不得寫入或推進游標。channel 名 MUST 是英文名（非空的 ASCII 字母、數字、`-`、`_`）；介面（參數、stdout、stderr、錯誤訊息）MUST NOT 出現 GitHub repo／issue 的概念。第一期 channel 對所有隊員公開，沒有成員設定：`channel create` MUST NOT 收 `--member`。`send` 的 `--channel` 必填、`--to` 選填（不給＝發到整個 channel）。`channel create` 與 `channel delete` 的 stdout MUST 只印 channel 名。`channel create` MUST 在送出註冊留言前先確認後端實體（存在而且標題等於 channel 名；不符 MUST exit 2、什麼都沒註冊——複審 H2），留言後 MUST 輪詢 fetch 到對應出現為止（上限 2 分鐘；逾時 MUST 在 stderr 印提示、仍 exit 0）；`channel delete` MUST NOT 等落地（送出註銷留言成功即 exit 0）。（註冊簿與重建見 `angareion/channel-config` 與 design §9／§10。）

`log` 是唯讀的查詢介面（語意見「唯讀查詢 log」）：它 MUST NOT 讀取、改動或推進游標，MUST NOT 參與「同一個身分只留一個讀者」規則；它的旗標與錯誤訊息同樣 MUST NOT 出現 GitHub repo／issue 的概念（只用 channel 名、AI 名、訊息 id（ULID）、RFC 3339 時刻與字面字串表達）。`status` 同樣適用這三條（唯讀、不推進游標、不參與單一讀者規則、旗標與錯誤訊息只用 channel 名、AI 名、訊息 id 與字面字串）。

#### Scenario: 送出並讀到
- **WHEN** A 對 channel X 送一則 `to` 是 B 的訊息，B 執行 `angareion read`
- **THEN** B 讀到那則訊息；再執行一次 `angareion read` 讀不到同一則

#### Scenario: 不給 to 就是發到整個 channel
- **WHEN** A 對 channel X 送一則不帶 `to` 的訊息
- **THEN** 送出成功（stdout 印訊息 id）；X 的任何成員 `read --channel X` 都讀得到它

#### Scenario: 建 channel
- **WHEN** `angareion channel create X`（不帶 `--member`；後端與本體都可用）
- **THEN** 後端出現 X 的實體、註冊簿上出現 X 的 `register` 留言；stdout 只印 `X`，exit 0；沒有給名稱時 exit 1

#### Scenario: 刪 channel
- **WHEN** `angareion channel delete X`（X 在設定檔裡有對應）
- **THEN** 後端實體被關掉（不刪除）、註冊簿上出現 X 的 `unregister` 留言；stdout 只印 `X`，exit 0；X 不在設定檔裡時 exit 1

#### Scenario: 建 channel 不再收成員
- **WHEN** `angareion channel create X --member <位址>`
- **THEN** exit 1（未知參數）；什麼都沒建

#### Scenario: 建 channel 名已存在
- **WHEN** channel 設定檔已經有 `X`，執行 `angareion channel create X`
- **THEN** exit 1，stderr 說明 channel 已存在；什麼都沒建

#### Scenario: 建 channel 等落地
- **WHEN** `angareion channel create X` 的第 2 步送出成功，而本機複本還沒有 `X`
- **THEN** 指令輪詢 fetch 到 `X` 出現才回：stdout 只印 `X`、exit 0；2 分鐘還沒出現→stderr 印提示、仍 exit 0

#### Scenario: 建 channel 的後端開實體失敗
- **WHEN** 第 1 步（在 backend 的 repo 開實體）失敗（網路、403、5xx）
- **THEN** exit 2，什麼都沒註冊；介面不提後端細節

#### Scenario: 建 channel 的用戶端驗證（複審 H2）
- **WHEN** 第 1 步開好實體後，用戶端 GET 到它的標題不是 channel 名（或查不到那個 issue）
- **THEN** exit 2，不留註冊留言、什麼都沒註冊

#### Scenario: 刪 channel 的關實體失敗
- **WHEN** `angareion channel delete X` 的第 2 步（關實體）失敗（不是 404）
- **THEN** exit 2；沒有送註銷（對應仍在）

#### Scenario: 刪 channel 的 404 視同已關
- **WHEN** `channel delete X` 的實體已經被刪（後端回 404）
- **THEN** 視同已關、照送註銷；stdout 印 `X`、exit 0

#### Scenario: 刪 channel 的註銷送出失敗
- **WHEN** `channel delete X` 的第 3 步（送註銷）失敗
- **THEN** exit 2；實體已關、對應還在；重跑安全（再關一次是 404 視同已關）

#### Scenario: 多 backend 沒給 --backend
- **WHEN** `backends.json` 有兩個 backend，`angareion channel create X` 沒給 `--backend`
- **THEN** exit 1，什麼都沒建

#### Scenario: backend 名不存在
- **WHEN** `angareion channel create X --backend nosuch`
- **THEN** exit 1，什麼都沒建

#### Scenario: 送註冊失敗後重試成功
- **WHEN** 第 2 步的留言送出第一次失敗、重試（短間隔）成功
- **THEN** `channel create` 照常完成：stdout 印 `X`、exit 0

#### Scenario: channel 名不是英文名
- **WHEN** `angareion channel create 小明` 或 `angareion send --channel 小明 …`
- **THEN** exit 1；什麼都沒建、沒送

#### Scenario: log 是第六個介面
- **WHEN** `angareion --help` 印出的子指令清單、`angareion channel --help` 印出的子子指令清單，與 `angareion log --help` 印出的旗標清單（低-14：三份清單都由 WHEN 的輸出觀察）
- **THEN** 頂層子指令恰好是 `channel`、`send`、`wait`、`read`、`log`、`status` 共六個（由 `angareion --help` 斷言）。七個介面由這六個頂層指令展開：`channel` 展開成 `create` 與 `delete` 兩個介面，其餘五個各為一個（`status` 見「唯讀查詢 status」）；`channel` 下的子子指令恰好是 `create`、`delete`（由 `angareion channel --help` 斷言）；`log` 的旗標是查詢類（`--channel`、`--from`、`--to`、`--mentioned`、`--since`、`--until`、`--grep`、`--around`、`--thread`、`--before`、`--after`、`--limit`、`--json`），沒有 `--body` 等寫入旗標（log 不寫信；`--to` 在 log 是收件人的「過濾」條件，不是 `send` 的寫入）

#### Scenario: status 是第七個介面，旗標沒有寫入項
- **WHEN** `angareion status --help` 印出的旗標清單
- **THEN** 旗標恰好是 `--channel`、`--message`、`--to`、`--json`（沒有 `--body` 等寫入旗標，也沒有其他內容參數）

#### Scenario: log 的介面不出現後端概念
- **WHEN** 檢查 `angareion log` 與 `angareion status` 的參數清單、stdout、stderr 與說明（`--help`）
- **THEN** 沒有 repo、issue、comment 等後端概念的字樣；只有 channel 名、AI 名、訊息 id、時刻與字面字串

### Requirement: 輸出與結束碼
stdout MUST 只放結果（`channel create`／`channel delete` 印 channel 名，`send` 印訊息 id，`read` 印訊息或 `--json`，`log` 印訊息或 `--json`（空結果也是成功），`status` 印三態結果或 `--json`），說明與錯誤放 stderr。結束碼：0 成功（`status` 為已讀）；1 用法或輸入錯誤；2 後端、網路或設定錯誤；3 `wait` 逾時沒有新訊息，或 `status` 明確未讀；4 `status` 未知（不是錯誤，是查不到，語意見「唯讀查詢 status」）。`status` 的 3 與 `wait` 的 3 是同一個數字、不同的指令，各指令的說明 MUST 分別定義它；腳本 MUST 依指令判讀，不得跨指令共用。

#### Scenario: 設定缺少
- **WHEN** 沒有設定檔（或缺 channel／backend 設定檔）就執行 `angareion send`
- **THEN** exit 2，stderr 說明缺什麼

#### Scenario: 後端錯誤
- **WHEN** 後端回 HTTP 錯誤（例如 403 次級限制或網路中斷）
- **THEN** exit 2，stderr 說明狀態碼，不自動重試；輸出裡沒有 token

#### Scenario: HTTP 逾時
- **WHEN** 後端請求超過逾時（固定 30 秒；`wait` 另受剩餘時間限制）沒有回應
- **THEN** exit 2，stderr 說明逾時，不自動重試；`wait --timeout N` 不會因為一次卡住的請求而遠超過 N 秒

#### Scenario: 非 JSON 的回應
- **WHEN** 後端回 HTTP 200，但本文不是 JSON（例如 proxy 的登入頁）
- **THEN** exit 2（後端錯誤），不是 exit 1；輸出裡沒有 token

#### Scenario: 送註冊失敗
- **WHEN** `channel create X` 的第 2 步（送註冊）失敗後重試 2 次（共 3 次嘗試）都失敗
- **THEN** exit 2，stderr 說明 channel 沒有建立成功；後端可能留著沒有對應的實體，但介面不提它

### Requirement: log 的本機快取
`log` 的查詢 MUST 以本機快取服務：對查詢涉及的每個 channel 先做**增量同步**，同步成功（304 的條件式請求也算成功）之後才在本機過濾回答。

- 同步用後端的「列舉」動作（見「後端可以替換」），帶 `since = 上一次同步到的後端時刻 − 60 秒`（與游標同源的 margin；快取為空時不帶 `since`）；合併 MUST 是冪等的 upsert：到達位置比已記錄的新、**或從未出現過**（接住亂序晚到的較小位置）就進鏡射，同位置內容不同（後端被編輯）就覆蓋；已記錄的最大位置與時刻 MUST NOT 後退。
- 回應含新訊息時 MUST NOT 記錄 ETag（不得成為下次「沒有新訊息」的驗證器）；回應完整掃過且乾淨、單頁未滿才記錄（沿用 `wait` 的條件式查詢規則）；下次同步同一視窗 MUST 送 `If-None-Match`，304 MUST 視為「沒有增量」且不計後端額度。
- 任一涉入 channel 的同步失敗（HTTP 錯、逾時）MUST 以 exit 2 結束且 MUST NOT 輸出任何查詢結果——不用「部分同步＋舊鏡射」當完整答案。
- 快取 MUST 每身分一份（檔名跟身分的 slug，同游標檔的分位），放在 state 目錄；channel 的檔名 MUST 以 channel 名的**可逆轉義**成檔（屬 `[a-z0-9_-]` 的字元原樣，**大寫 ASCII 字母與其餘字元一律以 `%XX` 編碼其 UTF-8 位元組**——大寫也轉義是低-16 定案：大小寫不敏感的檔案系統上，只差大小寫的兩個 channel 名 MUST NOT 落到同一個檔，`A` 與 `a` MUST 各有自己的快取檔；轉義 MUST 無條件可逆且單射），MUST NOT 依賴 channel 名的字元限制成立——組態檔裡出不合法的名字（`a/b`、`../x` 這類）時 MUST 仍不出快取目錄（轉義後不成路徑分隔；design D3.2 的 `channel_slug`），檔內記原始 channel 名並在載入時比對（不符＝整檔視為空）；與游標檔、`wait` 的 ETag 狀態檔 MUST NOT 共用檔案，也 MUST NOT 被對方讀寫。快取 MUST NOT 寫進或讀出任何游標。
- 快取檔 MUST 以「同目錄暫存＋原子替換」寫出，檔案權限 0600、目錄 0700（快取含頻道訊息內文，只能本身分讀）。
- 快取檔不存在、不是合法 JSON、版本不認得、缺欄位、或檔內記的 channel↔entity 對應與設定檔不符時，MUST 整檔視為空並全量重建；MUST NOT 部分信任。刪掉快取 MUST 只造成下一次全量同步，MUST NOT 讓訊息漏掉或查詢結果變異。
- 快取寫不進去（唯讀目錄、磁碟滿）MUST 只在 stderr 警告一次（同一行程最多一次），查詢照常以本輪記憶體內的鏡射回答；exit code 與 stdout MUST NOT 因此改變。
- 兩個 `log` 行程並行同一身分時，快取檔 MUST NOT 半寫（原子替換）；最壞情況是其中一輪的增量被下一輪重補（upsert 冪等），結果不漏。
- **已知限制**：合併只有 upsert，MUST NOT 解後端的刪除——後端刪掉的留言會留在快取與結果裡、`--since` 增量 MUST NOT 回報刪除；它在全量重建之後才不再出現。skill 的說明 MUST 寫明 log 的結果不是「這則現在還在」的證據。

#### Scenario: 增量同步接住新訊息
- **WHEN** B 先 `log` 過一次（快取建好），之後 A 又送一則信，B 再 `angareion log --channel X`
- **THEN** 第一次查詢是全量，第二次是增量（只列到新訊息），查得到 A 的新信

#### Scenario: 亂序晚到的留言
- **WHEN** 鏡射已記錄到到達位置 100，一則位置 98 的訊息晚一秒才出現
- **THEN** 下一次同步把它合併進鏡射（「從未出現過的位置」也納入），`log` 查得到它；已記錄的最大位置不後退

#### Scenario: 後端被編輯的留言
- **WHEN** 已進鏡射的一則留言在後端被編輯（同一位置的內容變了）
- **THEN** 下一次同步以同位置覆蓋內容；沒建立新位置、不在結果裡重複（機理：列舉的 `since` 按最後編輯時刻過濾，編輯過的舊留言重新出現在列表上）

#### Scenario: 後端刪掉的留言是已知限制
- **WHEN** 一則留言已在快取裡，隨後在後端被刪掉；之後只做增量的 `log`
- **THEN** 它仍然出現在結果裡（已知限制：合併不解刪除）；刪掉快取目錄、全量重建之後它就不出現了

#### Scenario: 快取檔名不依賴 channel 名的字元限制（轉義）
- **WHEN** `channels.json` 被手改出一個名字含 `/` 的 channel（例如 `a/b`），執行觸及它的 `angareion log`
- **THEN** 它的快取檔落在 `log-<身分>/` 內（檔名用轉義後的名字），MUST NOT 寫到或讀出快取目錄外面的路徑；`log` 的查詢照常回答

#### Scenario: 安靜 channel 重複查詢近零額度
- **WHEN** 對同一沒有新訊息的 channel 連續多次 `angareion log`
- **THEN** 第一次全量（或增量）之後記下 ETag；後續每次同步送 `If-None-Match`、後端回 304、照常以鏡射回答；中間若有新訊息，該筆記錄清掉、同步用完整請求接住它
- **WHEN** 回應有下一頁或第 1 頁已滿
- **THEN** 不留 ETag（同 `wait` 的規則），下一次用完整請求

#### Scenario: 同步失敗不出結果
- **WHEN** 查詢涉及的某一個 channel 在同步時後端回 HTTP 500（或逾時）
- **THEN** exit 2，stdout 沒有內容；不會用其餘 channel 的舊鏡射交出「看似完整」的答案

#### Scenario: 壞檔與對應失配都重建
- **WHEN** 快取檔被截斷／不是 JSON／版本不認得／缺欄位；或 channel X 對應的 issue 換了（檔內記的是舊實體）
- **THEN** 該 channel 整檔視為空、全量重建，`log` 的結果照常正確（exit 0）；不部分信任舊內容

#### Scenario: 權限
- **WHEN** 首次執行 `angareion log`（快取目錄與檔案初次建立）
- **THEN** 目錄是 0700，快取檔是 0600；同一身分以外的座位不共用這份快取（檔名含身分 slug）

#### Scenario: 快取寫不進去
- **WHEN** state 目錄唯讀，B 執行 `angareion log`
- **THEN** stderr 警告一次，`log` 照常以本輪記憶體內的鏡射回答，exit 0 與 stdout 正常；下一輪仍可用

#### Scenario: 兩個 log 並行
- **WHEN** 同一身分的兩個 `angareion log` 同時跑
- **THEN** 快取檔不會半寫；兩邊的查詢答案都正確、不漏訊息（最壞情況：其中一輪的增量下一輪重補）

#### Scenario: 快取與游標各行其是
- **WHEN** B 刪掉 `log-<身分>/` 後執行 `log`；或 B 執行 `log` 跟 `read` 交替
- **THEN** `log` 全量重建後照常回答；`read` 的游標不受 `log`／快取影響，`log` 也不受游標影響；`status` 的查詢結果同樣不受快取影響（`status` 不讀快取）

### Requirement: 給 agent 的說明
repo MUST 附一份給 agent 讀的說明（`skills/angareion/SKILL.md`）：七個指令的用法（含 `channel create`／`channel delete` 與落地有時間差、`angareion log` 的查詢用途與「不動游標」、`angareion status` 的三態判讀：只有「未讀」才考慮中斷對方，「未知」一律不戳，「已讀」只代表收件人的游標越過）、身分、閒著時用 `angareion wait` 阻塞地等、收到高 urgency 訊息的處理。說明 MUST NOT 含任何秘密或私人資訊。

#### Scenario: agent 照說明收信
- **WHEN** 一個讀了說明的 agent 手上沒事
- **THEN** 它執行 `angareion wait --timeout <秒>`，而不是反覆呼叫模型查信

#### Scenario: agent 要回頭找信會用 log
- **WHEN** 一個讀了說明的 agent 要找「昨天某決策的串」或「某則訊息的前後文」
- **THEN** 它用 `angareion log` 的過濾與錨點查詢，而不是用 `read --since` 反覆撈；說明寫明 log 不動游標

### Requirement: 後端可以替換（設定檔指位）
七個介面 MUST 經由一個後端介面實作，後端的動作是：建 channel＝開實體、刪 channel＝關實體、送、確認有沒有新訊息、讀，以及一個唯讀的「列舉」（列出實體上的留言與其到達位置、後端時刻，並標出留言是否是 angareion 訊息——非訊息的留言也要列得出來；`log`、它的本機快取，以及 `status` 使用這個動作）。列舉帶 `since` 時 MUST 按留言的**最後編輯時刻**過濾（與 GitHub 列表 API 的語意一致：被編輯過的舊留言 MUST 因編輯重新出現在結果裡；增量同步的「同 id 內容變就覆蓋」仰賴這條）——假後端 MUST 照守，MUST NOT 以「照 `created_at` 過濾或直接忽略 `since`」實作它。對外的 `wait` 是在後端「確認有沒有新訊息」上輪詢（不阻塞＝確認一次）。這次的實作是 GitHub API＋Issues：channel 名經 channel 設定檔指到某個 backend 的某個實體（issue；訊息＝留言）。解析 MUST 只認設定檔，MUST NOT 用標題掃描挑實體——同名標題不影響操作。後端 MUST NOT 存成員（第一期 channel 全公開）。換後端時，指令與訊息格式 MUST NOT 改變。

#### Scenario: 用假後端
- **WHEN** 測試以記憶體裡的假後端執行七個指令
- **THEN** 行為和用 GitHub 後端時相同，不連網

#### Scenario: 多頁時第 1 頁不變
- **WHEN** 「寄給我的」的留言列表超過一頁，第 1 頁（都是寄給別人的）內容和上一次一樣，第 2 頁多了一則寄給我的；`wait` 先前已經查過一次
- **THEN** `angareion wait --timeout 0` exit 0（不因第 1 頁的 304 而回報沒有新訊息）

#### Scenario: 同名標題不影響對應
- **WHEN** 後端裡有兩個標題相同的實體（編號 3 與編號 7），channel 設定檔把名字指到編號 7
- **THEN** 訊息送到編號 7，不會因為「編號 3 比較舊」改送編號 3

#### Scenario: 列舉的行為與讀共用同一個後端實作
- **WHEN** 假後端上對同一實體做「列舉」與「讀」
- **THEN** 同一批留言的到達位置與後端時刻一致；非 angareion 訊息的留言在「列舉」看得到（標示為非訊息），在「讀」（`read` 的輸出）則不會出現

## ADDED Requirements

### Requirement: 唯讀查詢 status
`angareion status` MUST 回答「某則訊息對某個收件人是已讀、未讀，還是未知」。指令形式 `angareion status --channel <ch> --message <訊息 id> --to <AI 名> [--json]`；`--channel`、`--message`、`--to` 都必填。

- **身分與 token**：`status` MUST 走與其他指令相同的完整啟動（設定、身分、token）。`ANGAREION_IDENTITY` 未設且設定檔沒有 `identity`，或 token 缺，MUST exit 2（用法與設定錯，不是未知 exit 4），stdout 沒有內容。身分的**值**不影響結果（見下一條）。
- `--to` 是**被查的收件人**，不是呼叫者；呼叫者身分（`ANGAREION_IDENTITY`）的值 MUST NOT 影響結果。`--to` 只接受既有文法的 AI 名；大寫、前後空白、超出長度 MUST exit 1；`me` MUST NOT 被解析（exit 1）。
- **唯讀**：`status` MUST NOT 寫入任何東西（狀態目錄、游標檔、`wait` 的 ETag 狀態檔、設定 repo 的本機複本、`log` 的快取目錄 `log-<身分>/`、後端上的任何留言），MUST NOT 推進任何游標，MUST NOT 呼叫 `save`／`save_many`，MUST NOT 產生回條或 ack。
- **不讀快取**：`status` MUST NOT 讀取 `log` 的本機快取，也 MUST NOT 建立 `log-<身分>/`。
- **不參與單一讀者**：`status` MUST NOT 參與「同一個身分只留一個讀者」規則：它不消費訊息，不與背景讀信任務競爭。
- **只讀本機設定複本**：channel 只能用本機設定複本的 `channels()` 查。MUST NOT 經 `resolve()`／`entity_for()`（它們在 miss 時會 fetch 設定 repo），MUST NOT 呼叫任何會從設定 repo 拉取或寫入本機複本的載入。複本缺或無法解析時，status 在自己內部接住（不讓 main 轉成 exit 2），答未知。找到 X 之後，`status` 還要 Entity（列舉需要 repo 與 issue）：它 MUST 自己呼叫 `backends()` 與 `backend(X 指向的名字)`（即 `entity_for` 拆解出的後兩步，但不經 `resolve()`）。`backends()` 缺或壞、X 指向的 backend 不存在、或其型別不是 github，一律答未知、`reason` 是 `config-unavailable`（exit 4），MUST NOT 因此 exit 2。
- **列舉成本**：找 `--message` 所在的留言用既有的列舉動作，分頁讀整條 issue 的留言，找到即停止翻頁，找不到則讀完（**待拍板 D-cost，見 design；本段是預設（a）**）。驗收門檻：在 1000 則留言的假後端上，單次 `status` 的列表請求數 MUST ≤ 11（每頁 100 則）。說明 MUST 寫明 `status` 的成本與 channel 留言數線性相關。
- **列舉介面的擴充（向後相容）**：要「找到即停止翻頁」，既有的列舉動作 MUST 提供提前停止（回呼或迭代器）；這是對既有列舉介面的向後相容擴充，不是新的寫入動作。`log` 與它的快取 MUST 照舊讀完整條列舉，行為與輸出不變。

**游標格式（唯讀解析；與 `CursorStore.save`／`save_many` 寫出的形狀同形）**：游標檔整體 MUST 是 JSON 物件；每個值 MUST 是物件，含 `position`、`at`、`seen` 三欄，型別如下：`position` MUST 是十進位數字字串（例 `"123"`）或 null；`at` MUST 是字串或 null；`seen` MUST 是字串陣列。`position` 與 `at` 都是 null 的條目（首讀空 channel 時真的會寫出的形狀）MUST 視為「該組從未越過」，不是壞檔。任何一項不符（不是 JSON、整體不是物件、條目不是物件、舊格式〔有 `id` 無 `position`〕、型別真的不符）MUST 答 `cursor-unparseable`；MUST NOT 略過壞掉的條目後繼續判定。游標檔沒有版本欄位，MUST NOT 有「版本不認得」的判準。整體解析成功、且**該 key 不存在，或存在但 `position` 與 `at` 都是 null** 時，該組才視為「從未越過」；三組判定一律用 `comment_is_new`（它對這兩種情形都回答「新」）。

**未知的判定順序（固定）**：下列檢查依序進行，第一個成立者即為 `reason`，之後不再檢查（多個原因同時成立時，`reason` 只取最先成立的那一個）：

1. `config-unavailable`：本機設定複本缺失或無法解析，或 X 指向的 backend 在 `backends()` 缺、型別不支援（見「唯讀查詢 status」的「只讀本機設定複本」）。
2. `channel-not-local`：`--channel` 不在本機設定複本裡。
3. `cursor-missing`：X 的游標檔整個不存在。
4. `cursor-unreadable`：游標檔存在但讀不到（權限等）。
5. `cursor-unparseable`：游標檔存在且讀得到，但不符合上面的游標格式。
6. `message-not-found`：後端列舉找不到 `--message` 的 ULID（或它不是 angareion 訊息）。
7. `out-of-scope`：m 不在 X 的 scope 內。

**scope**：m 的 `to` 是 X，或 `mentions` 含 X（`to` 是別人、但 `mentions` 含 X 也在 scope 內）。廣播（`to` 為空且不點名 X）MUST NOT 在 scope 內。

**三態**（第 1–7 項都不成立時才判定）：
- **已讀**：m 在 X 的 scope 內，且三組游標 `to:X:<ch>`、`mention:X:<ch>`、`channel:<ch>` 中**任一組**存在且越過 m（`comment_is_new` 為假）。
- **未讀**：m 在後端查得到，m 在 X 的 scope 內，且三組游標都沒有越過 m（缺的組視為沒有越過）。
- **未知**：第 1–7 項任一成立，答未知，MUST NOT 答未讀。

**結束碼**：0 已讀；3 未讀；4 未知；1 用法錯（缺參數、ULID 格式不對、AI 名不合法、`--channel` 格式不對、`--to me`）；2 身分或 token 缺、設定錯（非複本缺失的設定錯）、後端錯。

**輸出**：
- 文字模式（stdout 只放以下固定行，不印其他字）：已讀 `read: true` 一行；未讀 `read: false` 一行；未知兩行 `read: unknown` 與 `reason: <字面原因>`。
- `--json`：`{"message", "channel", "to", "read", "read_at", "reason"}`；`read_at` MUST 為 null（游標給不出讀取時刻）；已讀為 `read: true`、未讀為 `read: false`、未知為 `read: null`；`reason` 在已讀或未讀時為 null，未知時為字面原因（`config-unavailable`、`channel-not-local`、`cursor-missing`、`cursor-unreadable`、`cursor-unparseable`、`message-not-found`、`out-of-scope`）。

**同機限制**：狀態目錄在本機，`status` 只能查同一台機器、同一個狀態目錄下的身分；跨機的身分答未知（reason `cursor-missing`）。說明寫明這一點。

**說明**：skill 的說明 MUST 寫明「已讀」只代表收件人的游標越過該訊息；經過 `aicomm inbox` 讀取時，游標可能在 agent 看到之前就推進，所以已讀不代表對方已處理。說明 MUST 寫明 `status` 不是寫入的回條，也不是「對方一定會處理」的保證；建議用法是只有未讀才考慮中斷，未知一律不戳。

#### Scenario: 已讀（寄給我的游標越過）
- **WHEN** 收件人 X 的 `to:X:<ch>` 游標越過訊息 m，且 m 寄給 X
- **THEN** exit 0；文字模式 stdout 只有 `read: true`；`--json` 的 `read` 是 true、`read_at` 是 null、`reason` 是 null

#### Scenario: 未讀
- **WHEN** X 的游標檔存在且可解析，m 寄給 X、後端查得到，三組游標都沒有越過 m
- **THEN** exit 3；文字模式 stdout 只有 `read: false`；`--json` 的 `read` 是 false、`reason` 是 null；stderr 沒有錯誤

#### Scenario: 只用 read --channel 讀過也算已讀
- **WHEN** X 只執行過 `angareion read --channel <ch>`（只有 `channel:<ch>` 游標越過 m），`to:X:<ch>` 沒有越過 m
- **THEN** exit 0（已讀）；`to:` 游標不被修改

#### Scenario: 只用 read --mentioned 讀過也算已讀
- **WHEN** X 只執行過 `angareion read --mentioned`，`mention:X:<ch>` 越過 m，m 點名 X
- **THEN** exit 0（已讀）

#### Scenario: mention 在 to 為別人的訊息上算 scope
- **WHEN** m 的 `to` 是 C（別人），`mentions` 含 X，且 `mention:X:<ch>` 越過 m
- **THEN** exit 0（已讀）；同一則若 `mentions` 不含 X，則 exit 4、`reason` 是 `out-of-scope`

#### Scenario: 游標檔不存在是未知，不是未讀
- **WHEN** X 從未在這台機器上讀過，`cursor-<X>.json` 不存在，查詢 X 寄給它的訊息 m
- **THEN** exit 4；`read` 為 null；`reason` 是 `cursor-missing`；不是 exit 3；文字模式 stdout 恰為 `read: unknown` 與 `reason: cursor-missing` 兩行

#### Scenario: 游標檔壞掉是未知
- **WHEN** `cursor-<X>.json` 不是 JSON（截斷）
- **THEN** exit 4；`reason` 是 `cursor-unparseable`；不因為壞檔被當成空而答未讀

#### Scenario: 游標檔裡有舊格式條目是未知，不是略過
- **WHEN** `cursor-<X>.json` 是合法 JSON，但其中一個條目是舊格式（有 `id` 無 `position`），而該條目正是 m 所屬那一組
- **THEN** exit 4；`reason` 是 `cursor-unparseable`；MUST NOT 把該條目當成「從未越過」而答未讀

#### Scenario: 游標檔無法讀取是未知
- **WHEN** `cursor-<X>.json` 存在，但權限不允許讀取
- **THEN** exit 4；`reason` 是 `cursor-unreadable`；stderr 說明讀不到游標檔，stdout 不印 `read: false`

#### Scenario: 首讀空 channel 留下的 null 條目不是壞檔（H-1）
- **WHEN** `cursor-<X>.json` 是真的 `CursorStore.save_many` 寫出的形狀：`to:X:<ch>` 為 `{"position": null, "at": null, "seen": []}`，`channel:<ch>` 為 `{"position": "123", "at": "2026-01-01T00:00:00Z", "seen": []}`；查詢 X 寄給它的訊息 m，m 的後端位置大於 `"123"`
- **THEN** exit 3（未讀）；`reason` 是 null；MUST NOT 答 `cursor-unparseable`

#### Scenario: 亂序晚到、位置較小的留言在窗口內仍算新（L-B）
- **WHEN** X 的 `to:X:<ch>` 游標是 `{"position": "100", "at": "2026-01-01T00:00:00Z", "seen": []}`，m 寄給 X、後端位置是 98、後端時刻是 `2025-12-31T23:59:30Z`（在 `at` 減 60 秒的窗口內），且 98 不在 `seen` 中
- **THEN** 該組判為「新」，三組都沒有越過 m：exit 3（未讀）
- **WHEN** 同上，但 `seen` 含 `"98"`
- **THEN** 該組判為「不是新」，exit 0（已讀）

#### Scenario: 查不到訊息是未知
- **WHEN** 後端查不到 `--message` 給的 id（剛送、還沒落地，或 id 寫錯但格式合法）
- **THEN** exit 4；`reason` 是 `message-not-found`

#### Scenario: 廣播不作答
- **WHEN** m 是廣播（`to` 為空且不點名 X），且 X 的 `to:X:<ch>` 游標已越過 m（例如新身分首讀過濾的結果）
- **THEN** exit 4；`reason` 是 `out-of-scope`；MUST NOT 答已讀，也 MUST NOT 答未讀

#### Scenario: 原因依固定順序取第一個
- **WHEN** X 的游標檔不存在，且後端也查不到 `--message`
- **THEN** exit 4；`reason` 是 `cursor-missing`（第 3 項先於第 6 項）
- **WHEN** 本機設定複本缺失，且 X 的游標檔也不存在
- **THEN** exit 4；`reason` 是 `config-unavailable`（第 1 項先於第 3 項）

#### Scenario: 設定複本缺或壞是未知，且不讓 main 轉成 exit 2
- **WHEN** 本機設定複本缺失或無法解析，執行 `angareion status`
- **THEN** exit 4；`reason` 是 `config-unavailable`；stdout 印未知；stderr 說明設定複本不可用；MUST NOT 因此 exit 2

#### Scenario: channel 指向的 backend 壞掉也是未知（M-A）
- **WHEN** 本機複本的 `channels` 裡 X 存在，但它指向的 backend 不在 `backends()` 裡，或該 backend 的型別不是 github，執行 `angareion status --channel X …`
- **THEN** exit 4；`reason` 是 `config-unavailable`；MUST NOT exit 2；列舉請求數為 0

#### Scenario: channel 不在本機複本是未知，且不刷新
- **WHEN** `--channel` 給了一個不在本機設定複本裡的名字，或設定複本落後
- **THEN** exit 4；`reason` 是 `channel-not-local`；MUST NOT 發出 fetch 設定 repo 的請求，MUST NOT 寫入本機複本；以會計數 `fetch`、`refresh_for_lookup`、`ensure_fresh` 的假設定 store 驗證，三者呼叫次數都是 0

#### Scenario: 身分缺是設定錯，身分的值不影響結果
- **WHEN** `ANGAREION_IDENTITY` 未設且設定檔沒有 `identity`，執行 `angareion status`
- **THEN** exit 2，stdout 沒有內容
- **WHEN** 兩個不同的 `ANGAREION_IDENTITY`（都合法）查同一則訊息、同一個 `--to`
- **THEN** 兩次的 stdout 與 exit code 相同

#### Scenario: 不寫入、不推進游標、不建快取目錄
- **WHEN** 對同一身分連續執行多次 `angareion status`（涵蓋已讀、未讀、未知三種結果）
- **THEN** 狀態目錄裡的游標檔、`wait` 的 ETag 狀態檔、本機設定複本的檔案內容與修改時間都與執行前相同；`log-<身分>/` 不存在（若執行前就不存在）或內容不變；隨後的 `angareion read` 讀到的內容與沒有執行 `status` 時相同

#### Scenario: status 不讀 log 快取
- **WHEN** `log-<身分>/` 內的快取與游標的判定結果不同（快取說某則已讀、游標沒有），執行 `angareion status`
- **THEN** 結果只依游標與後端判定，與快取內容無關；快取檔的內容與修改時間不變

#### Scenario: 不搶讀者
- **WHEN** 同一身分的背景讀信任務在執行中，前景同時執行 `angareion status`
- **THEN** `status` 不呼叫 `read`（以假後端的 `read` 呼叫次數驗證，與沒有 `status` 時相同）；`status` 前後游標檔的 bytes 相同；背景讀取的結果與沒有 `status` 時相同

#### Scenario: 用法錯
- **WHEN** 缺 `--to`、`--message` 不是 26 字 ULID、`--to` 是 `me`、`--to` 含大寫或超出長度、或不合法的 AI 名
- **THEN** exit 1，stdout 沒有內容，stderr 說明缺什麼或哪裡不合法

#### Scenario: 大 channel 的列舉成本
- **WHEN** 假後端的 issue 有 1000 則留言，執行 `angareion status` 查最後一則，或查一個不存在的 id
- **THEN** 列表請求數 ≤ 11（每頁 100 則）；找到即停止翻頁

#### Scenario: 跨機的身分答未知
- **WHEN** 查詢的身分只存在於另一台機器的狀態目錄（這台機器上沒有它的游標檔）
- **THEN** exit 4；`reason` 是 `cursor-missing`；說明寫明 `status` 只能查同一台機器上的身分
