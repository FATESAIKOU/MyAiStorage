# Agora lite 驗收測試清單

2026-10-02，review。給 impl 照著寫自動化測試。範圍是 `docs/design.md` 第 5 節的六個指令。
「對應」一欄的 S1…H7、L1…L4 指的是 `docs/review/design.md` 的編號；「5.x」指 design.md 的小節；T1、T2 是這份清單新發現的問題（見第 0.4 節）。

> **前提**：標了 S／H／L 編號的測試，是假設 design 會採納 review 的建議。PM／使用者如果不採納某一條，就刪掉或改寫對應的測試，不要讓測試偷偷定義規格。

## 0. 共通約定

### 0.1 使用者已確認的閱讀版規格（2026-10-02）
閱讀版只包含 **user／assistant 的文字**，加上**每次工具呼叫的一行摘要**（`[tool] <名稱> <參數摘要，最多 200 字>`）。**不收**工具的結果，也**不收** thinking／reasoning。搜尋只比對閱讀版與 header。

### 0.2 給 impl 的測試接縫（為了能測，程式要提供）
| 接縫 | 用途 |
|---|---|
| `AGORA_CONFIG`、`AGORA_CACHE_DIR`（鏡像與索引）、`AGORA_STATE_DIR`（outbox、pending） | 每個測試用自己的暫存目錄；同一個 Drive 根目錄搭配兩組 cache／state，可以模擬兩台機器 |
| `AGORA_RCLONE`（rclone 執行檔的路徑） | 單元測試換成假的 rclone |
| `AGORA_OPENCODE_CMD`、`AGORA_CLAUDE_CMD` | 單元測試換成假的 agent；整合測試換成非互動的 `opencode run`／`claude -p` |
| `AGORA_CLAUDE_HOME`（預設是 `~`） | 單元測試把 `~/.claude/projects` 指到暫存目錄，**絕對不能讀到真的 `~/.claude`** |
| `AGORA_TEST_FAULT=<點>` | 在指定的點讓程式直接 `os._exit(137)`：`after-raw-upload`、`after-session-upload`、`after-agent-launch`、`before-finalize` |
| 可以注入的時鐘（例如 `AGORA_NOW`） | 測 sync 的節流 |

### 0.3 假的 rclone 與假的 agent（單元測試用）
- **假的 rclone**：寫一個小腳本，把 `AGORA_RCLONE` 指向它。它把「遠端」放在一個本機資料夾裡，支援 agora 會用到的子指令（`lsjson -R --hash`、`copyto`、`deletefile`、`purge`、`mkdir`），並且：(a) 把每次呼叫的參數依序記到 `calls.log`；(b) 每次呼叫後，把整個遠端拍一份快照；(c) 用 `FAKE_RCLONE_FAIL=<第 N 次呼叫>|<子指令>:<路徑樣式>` 讓指定的呼叫以 exit 1 失敗；(d) 用 `FAKE_RCLONE_DUP=<路徑>` 讓 `lsjson` 多回傳一筆同名的資料夾。
  - 也可以用真的 rclone 搭配 local backend（`:local:` 路徑）取代 (a)～(b) 以外的部分，但同名與失敗的注入還是要靠包一層腳本。
- **假的 opencode**：一個腳本。`export <id>` 把 fixture 寫到 stdout；`import <file>` 把檔案複製到 `$FAKE_HOME/opencode-imported/`，記下內容中的 session id；`--session <id>`／`run --session <id>` 依 `FAKE_AGENT_MODE` 決定動作：`append`（新增一則訊息）、`noop`、`crash`（exit 2）、`sleep:<秒>`、`ignore-int`（忽略 SIGINT 後正常結束）。另外把 `pwd` 記到 `$FAKE_HOME/opencode-cwd.log`。
- **假的 claude**：一個腳本。`--resume <id>` 會在 `$AGORA_CLAUDE_HOME/.claude/projects/<編碼後的 pwd>/<id>.jsonl` 依 `FAKE_AGENT_MODE` 追加幾行；同時記下參數與 `pwd`。
- **fixture**：全部手寫、自編，內容像「把 CSV 轉成 Markdown 表格，先列三個步驟」這樣，**不准從任何真實 Session 複製或改寫**。fixture 的形狀以 V1／V2 spike 實測到的欄位為準。每份 fixture 都要放進幾個「標記字串」，讓測試可以用 grep 判斷有沒有出現：工具結果裡放 `ZZTOOLOUT`，thinking 裡放 `ZZTHINK`，原始的 session／message／part id 用 `ZZSRCID-` 開頭。
  - `oc-basic.json`：opencode export，含 user／assistant 的 text、reasoning、tool（含 output）、file、step 這幾種 part，再加一個未知型態的 part `zzunknown`。
  - `oc-large.json`：同上，但 assistant 的文字超過 256 KB（用來測 pipe 截斷）。
  - `cl-basic.jsonl`：Claude jsonl，含 user／assistant 的 text、tool_use、tool_result、thinking、summary、meta 行；每一行都有 `sessionId`、`cwd`。
  - `cl-aux/`：同一個 session 的附屬資料夾 `<uuid>/subagents/a.jsonl`（形狀以 V2 實測為準）。
  - `cl-partial.jsonl`：最後一行只寫了一半。
  - `cjk.json`：中文與日文的內容，包含：二字詞「表格」、四字詞「表格轉換」、日文「変換する」「テーブル」、全形「ＣＳＶ」。

### 0.4 這份清單新發現的問題（要補進 design）
| # | 嚴重度 | 問題 | 建議 |
|---|---|---|---|
| T1 | High | FTS5 trigram 分詞**比不到少於 3 個字的關鍵字**。「表格」「合併」這種二字中文詞是最常見的查詢，結果會是零筆，而且沒有任何錯誤訊息。設計的「找」在中文上等於壞掉 | 關鍵字少於 3 個字時，改用 `instr()`／`LIKE '%kw%'` 掃描閱讀版的表（個人用的規模夠快）。design 5.1 要寫明這個退路。期 1「驗證過可行」可能沒有測到二字詞 |
| T2 | Low | 全形與半形（`ＣＳＶ`／`CSV`）、日文的半形片假名，在 trigram 裡算不同的字 | 建索引和查詢時都先做 NFKC 正規化 |

## (a) 單元測試（不碰 Drive、不跑真的 agent）

每個測試都用全新的暫存目錄，並設定 `AGORA_CONFIG`／`AGORA_CACHE_DIR`／`AGORA_STATE_DIR`／`AGORA_CLAUDE_HOME`／`AGORA_RCLONE`。另外加一個全域的 fixture：測試期間只要有程式碼去開 `~/.claude` 或 `~/.local/share/opencode` 底下的檔，測試就直接失敗（例如用 monkeypatch 包住 `open`，或在 CI 裡把 HOME 指到一個空資料夾）。

### U-HDR header

| ID | 前置 | 步驟 | 預期 | 對應 |
|---|---|---|---|---|
| U-HDR-01 | 一份 header，所有欄位都有值；title 是中文；note 含換行和一整行 `---` | 寫出 session.md 再讀回來 | 讀回來的欄位全部相同；內文裡的 `---` 不會被當成 header 的一部分 | H6（其他） |
| U-HDR-02 | 無 | `--header 'title=規劃' --header 'case=mybrain:案件/x' --header 'refs=mybrain:a.md' --header 'refs=foundry:b' --header 'tags=csv'`（如果最後決定用 `key: value`，就照那個寫法） | title、case、refs（2 筆，順序不變）、tags 各自寫進對應欄位 | H6 |
| U-HDR-03 | 無 | `--note '注意: 先做讀取'` 或不帶 key 的自由文字 | 整段文字進 `note`，不會產生 `注意` 這個欄位 | H6 |
| U-HDR-04 | 無 | search 用 `--header foo=bar`（不在別名清單裡） | exit ≠ 0，訊息列出可以用的 key | H6 |
| U-HDR-05 | 一份 header，內容是 `header: 99`，外加一個不認得的頂層欄位 | 讀取；改 title 後寫回去 | 印出警告；共通欄位照常讀到；不認得的欄位寫回後還在 | H3、H4 |
| U-HDR-06 | 無 | 解析 ref：`mybrain:a:b/c.md`、`atelier:職務@v2`、`foundry:x#sec`、`nobody:x` | 依序得到 (mybrain, `a:b/c.md`)、(atelier, 職務, rev=v2)、(foundry, x, frag=sec)；最後一個報錯 | H2 |
| U-HDR-07 | 無 | `case` 設成 `xyz`（沒有前綴） | 報錯；`null` 或 `mybrain:...` 可以通過 | H2 |
| U-HDR-08 | 用 oc-basic 匯入 | 讀新 Session 的 header | `source.agent`、`source.session_id`、`source.dir`、`source.host`、`source.agent_version`、`source.created_at`、`created_at`、`header: 1` 都有值，`created_at` 和 `source.created_at` 不同 | H1、H4、H7 |

### U-RV 閱讀版

| ID | 前置 | 步驟 | 預期 | 對應 |
|---|---|---|---|---|
| U-RV-01 | oc-basic | 轉成閱讀版 | 有 user／assistant 的文字；每次工具呼叫各一行 `[tool] <名稱> ...`；**沒有** `ZZTOOLOUT`、`ZZTHINK`；`zzunknown` 輸出成 `[skip zzunknown]`，程式不會失敗 | L1、0.1 |
| U-RV-02 | cl-basic | 轉成閱讀版 | 同上；tool_result、thinking、meta 都不出現；summary 的處理方式依 L1 規格決定（輸出成一行，或者 skip），要和 opencode 一致 | L1、0.1 |
| U-RV-03 | 一次工具呼叫，參數長 1,000 字 | 轉成閱讀版 | 那一行的參數摘要不超過 200 字，並且標示截斷 | L1 |
| U-RV-04 | 閱讀版裡出現某個 Session 的段落 | 對應的 header | 每一段開頭都標出來源的 agora id（給 merge 用） | 5.3 |

### U-ST 儲存、寫入順序、outbox、sync

| ID | 前置 | 步驟 | 預期 | 對應 |
|---|---|---|---|---|
| U-ST-01 | 空的假遠端 | 匯入 oc-basic | `calls.log` 裡 raw 的上傳出現在 session.md **之前**；session.md 是最後一次寫入 | S1 |
| U-ST-02 | 同上 | 讀取遠端的 session.md | `raw.file` 是 `raw-<md5 前 12 碼>.json`；`raw.md5`、`raw.size` 和遠端檔案的實際值相同 | S1 |
| U-ST-03 | 已經匯入過；把 fixture 改一個字 | 重新匯入（沒有子 Session） | 拿每一次 rclone 呼叫之後的快照檢查：**任何一份快照**裡，只要有 session.md，它指向的 raw 都存在，而且 md5 相符。最後只剩新的 raw，舊的 raw 已經刪除 | S1 |
| U-ST-04 | 遠端只有 session.md，它指向的 raw 不存在 | 用另一組 cache 執行 `agora sync`，再 `search` | 不建這個 Session 的索引，exit 0，警告一行；補上 raw 後再 sync，就搜得到 | S1 |
| U-ST-05 | 遠端的 raw md5 和 header 不一樣 | `sync` | 同 U-ST-04，不建索引 | S1 |
| U-ST-06 | `FAKE_RCLONE_FAIL` 設成 raw 上傳失敗 | 匯入 | exit ≠ 0，訊息寫明「已存入 outbox」；`$AGORA_STATE_DIR/outbox/<ULID>/` 有完整的 session.md 和 raw；遠端沒有這個 Session 的 session.md | S2 |
| U-ST-07 | `FAKE_RCLONE_FAIL` 設成 session.md 上傳失敗 | 匯入；再用另一組 cache 執行 sync | outbox 有這筆；遠端只有 raw；另一組 cache 不會建索引 | S1、S2 |
| U-ST-08 | U-ST-06 跑完的狀態，拿掉失敗注入 | `agora sync` | `calls.log` 裡先推再拉；推完確認 md5 一致後，才把那筆移出 outbox；之後搜得到 | S2 |
| U-ST-09 | outbox 裡有一筆 | `rm -rf $AGORA_CACHE_DIR`，然後 `agora sync` | outbox 不受影響，而且被推上去；鏡像與索引都重建好 | S2 |
| U-ST-10 | outbox 裡有一筆 | 執行 `search`／`show`／`import` 任何一個 | stderr 都印一行「outbox 有 N 筆未上傳」 | S2 |
| U-ST-11 | 遠端某個檔的內容變了，大小相同，mtime 比本機舊 | `sync` | 這個檔還是會被重新下載（用 checksum 判斷，不看時間）；`calls.log` 裡沒有 `--update` | S5 |
| U-ST-12 | 遠端某個 Session 資料夾被 purge；outbox 裡也有另一筆 | `sync` | 鏡像裡那個資料夾和它的索引都被刪掉，search 找不到；outbox 那筆不受影響 | S5 |
| U-ST-13 | 遠端有 3 個 Session | 在新的 cache 執行 `sync` | 鏡像裡只有 3 份 session.md，沒有 raw；`calls.log` 裡只有一次遞迴列檔（`lsjson -R --fast-list --hash` 或同等的指令） | L2 |
| U-ST-14 | 用假時鐘 | 連續兩次 search，間隔 1 分鐘；再一次間隔 6 分鐘；再一次加 `--no-sync` | 第 2 次與第 4 次不呼叫 rclone；第 3 次會 | L2 |
| U-ST-15 | 假的 rclone 一律失敗（模擬離線） | `search 'CSV'` | 從本機索引回傳結果，exit 0，警告一行 | L2 |
| U-ST-16 | `FAKE_RCLONE_DUP=agora` | `sync` | 警告「Drive 上有同名資料夾」；所有呼叫都用 config 裡的 folder ID，不靠名字找 | S8 |
| U-ST-17 | 沒有 config（第一次執行） | `agora sync` | 建好根資料夾，把它的 folder ID 寫進 config；第二次執行不再建 | S8 |

### U-IMP import

| ID | 前置 | 步驟 | 預期 | 對應 |
|---|---|---|---|---|
| U-IMP-01 | 假的 opencode，fixture 用 oc-basic | `agora import --format opencode --session-id ZZSRCID-s1 --header 'title=規劃'` | stdout 第一欄是 `agora:<ULID>`；遠端有 session.md 和 raw；`relation: import`、`parents: []`；raw 和 fixture 的每一個位元組都相同 | 5.2 |
| U-IMP-02 | 假的 opencode，fixture 用 oc-large | 匯入 | raw 和 fixture 的每一個位元組都相同（沒有被 pipe 截斷）；export 的 stderr 沒有混進 raw | 5.2、S10 |
| U-IMP-03 | 假的 opencode 輸出不合法的 JSON；另一個 case 是 0 則 message | 匯入 | exit ≠ 0；遠端和 outbox 都沒有東西 | S10 |
| U-IMP-04 | `AGORA_CLAUDE_HOME` 底下放 `.claude/projects/-tmp-x/<uuid>.jsonl`（cl-basic）和 `<uuid>/`（cl-aux） | `agora import --format claude --session-id <uuid>` | raw 是 `{"main": [...], "aux": {...}}`，main 的行數和 aux 的檔名都和 fixture 相同 | S9 |
| U-IMP-05 | 同上，main 改用 cl-partial | 匯入 | 只丟掉最後一行，警告一行；其他行都匯入 | S10 |
| U-IMP-06 | 已經匯入過，fixture 沒變 | 再匯入一次 | 印出同一個 agora id；raw 不會重新上傳（`calls.log` 裡沒有 raw 的 copyto） | 5.2 |
| U-IMP-07 | 已經匯入過，fixture 變了，沒有子 Session | 再匯入一次 | 同一個 agora id；`updated_at` 變新；內容是新的 | 5.2、S1 |
| U-IMP-08 | 已經匯入成 A，並且有一個 continue 的子 Session B（`parents[0].raw_md5` 是 A 的舊 md5）；fixture 變了 | 再匯入一次 | 印出**新的** id C：`relation: import`、`parents: [A]`；A 的 session.md 和 raw 都沒有變 | S7 |
| U-IMP-09 | cache 1 匯入了 A；cache 2 從來沒有 sync 過 | 用 cache 2 匯入同一個來源 | 印出 A（import 會先做一次不節流的 sync），不會產生新的 ULID | S6 |
| U-IMP-10 | 在遠端手動放兩個 Session，它們的 `source` 相同、`updated_at` 不同 | `search`、`show` | 只顯示比較新的那個，警告一行 | S6 |

### U-MRG merge-session

| ID | 前置 | 步驟 | 預期 | 對應 |
|---|---|---|---|---|
| U-MRG-01 | Session A（opencode）、B（claude） | `merge-session A B`；再執行一次 `merge-session B A` | 兩個新 id，`relation: merge`，`parents` 分別是 [A, B] 和 [B, A]；閱讀版依同樣的順序串接，每段開頭標出來源 id | 5.3、U-RV-04 |
| U-MRG-02 | 同上 | 讀 merge 結果的 raw | 內容只有 `parents` 的 id 和 raw_md5，不含 A、B 的對話內容（檔案很小） | L3、S7 |
| U-MRG-03 | 無 | `merge-session A agora:不存在` | exit ≠ 0；遠端和 outbox 都沒有新東西 | 5.3 |
| U-MRG-04 | A 的 raw 還沒寫完（狀態同 U-ST-04） | `merge-session A B` | exit ≠ 0，訊息說 A 還不完整；或者先 sync 再試一次。兩種做法擇一，寫進 design | S1 |

### U-CON continue-session（假的 agent）

| ID | 前置 | 步驟 | 預期 | 對應 |
|---|---|---|---|---|
| U-CON-01 | A 是從 oc-basic 匯入的；假的 opencode 設成 `append` | `continue-session A --agent opencode --dir $T/proj` | `import` 收到的檔案裡：**沒有任何** `ZZSRCID-` 字串；session id、每一個 message id、每一個 part id 都是新的；所有 `sessionID`／`messageID` 參照都換成對應的新 id，而且前後一致 | S4 |
| U-CON-02 | 同上 | 檢查假的 opencode 收到的參數與 cwd | 用 `--session <新 id>` 啟動，`opencode-cwd.log` 是 `$T/proj`；結束後 export 的是新 id | S4、H1 |
| U-CON-03 | 同上 | 讀新 Session | stdout 第一欄是新的 agora id；`relation: continue`；`parents: [{id: A, raw_md5: <A 當時的 md5>}]`；閱讀版裡有 append 進來的那則訊息 | 5.4、S7 |
| U-CON-04 | A 是從 cl-basic 匯入的，`source.dir=$T/proj`；假的 claude 設成 `append` | `continue-session A --agent claude`（不帶 `--dir`） | `$AGORA_CLAUDE_HOME/.claude/projects/<編碼後的 $T/proj>/<新 uuid>.jsonl` 存在，裡面每一行的 `sessionId` 都是新的 uuid，`cwd` 都是 `$T/proj`；aux 也複製到同一個位置；用 `--resume <新 uuid>` 啟動，cwd 是 `$T/proj` | S9、H1 |
| U-CON-05 | 同 U-CON-04，但假的 claude 設成 `noop` | continue | 不會存成新 Session；pending 留著；警告一行「session 沒有新增內容」 | S9 |
| U-CON-06 | A 的 `source.dir` 在這台機器上不存在 | 在 `$T/other` 底下執行 continue（不帶 `--dir`） | 用 `$T/other`，stderr 印出實際用的目錄；帶 `--dir $T/x` 時就用 `$T/x` | H1 |
| U-CON-07 | A 是 opencode，目標是 claude；另一個 case 是 A 為 merge 的結果，目標是 opencode | continue | 不呼叫 `opencode import`，也不複製 jsonl；寫出一個閱讀版的檔案；agent 的初始訊息裡包含這個檔的路徑 | 5.4 |
| U-CON-08 | 假的 agent 啟動時會去檢查 `$AGORA_STATE_DIR/pending/` | continue | agent 啟動的那一刻，pending 已經存在，內容有 `parent`、`agent`、`agent_session_id`、`dir`、`started_at`；正常結束後 pending 被刪掉 | S3 |
| U-CON-09 | 假的 agent 設成 `sleep:5` 後再 `append` | 啟動 continue，1 秒後 `kill -9` agora 的行程（不殺 agent）；等 agent 結束後，執行 `agora show A` | 執行 `show` 時會先補存：產生新的 continue Session，`parents` 正確，pending 被清掉，stdout／stderr 印出補存的新 id | S3 |
| U-CON-10 | 同上，改用 `AGORA_TEST_FAULT=before-finalize` | continue | 同 U-CON-09：下一個指令會補存 | S3 |
| U-CON-11 | 假的 agent 設成 `ignore-int`；用 `setsid`／新的 process group 啟動 continue | agent 執行時對整個 process group 送 SIGINT | agora 不會死；agent 結束後照常存檔 | S3 |
| U-CON-12 | 假的 agent 先 `append` 再 `crash`（exit 2） | continue | 有新增內容就照樣存，stderr 註記 agent 的 exit code；pending 被清掉 | S3 |
| U-CON-13 | `FAKE_RCLONE_FAIL` 設成收尾時的上傳失敗 | continue | 新 Session 進 outbox，pending 被清掉（outbox 已經接手）；下一次 sync 會推上去 | S2、S3 |
| U-CON-14 | 鏡像只有 A 的 session.md，沒有 raw | continue（原生載入） | 先從遠端抓 raw，並且檢查 md5 後才繼續；md5 不符就 exit ≠ 0 | L2、S1 |

### U-SHW show

| ID | 前置 | 步驟 | 預期 | 對應 |
|---|---|---|---|---|
| U-SHW-01 | Session A | `show A` | 第一行的第一欄是 A；接著是 header 和閱讀版 | 5 |
| U-SHW-02 | 無 | `show agora:不存在` | exit ≠ 0，一行錯誤訊息 | 5 |

### U-SRC search（含中日文）

前置：用 cjk.json、oc-basic、cl-basic 各匯入一個 Session，`--no-sync`。

| ID | 步驟 | 預期 | 對應 |
|---|---|---|---|
| U-SRC-01 | `search session 'csv'` | 比得到內文是 `CSV` 的 Session（不分大小寫）；每一行的格式是 `agora:<ULID>  YYYY-MM-DD  <agent>  …片段…` | 5.1 |
| U-SRC-02 | `search session '表格轉換'`（4 個字） | 比得到 cjk | 5.1 |
| U-SRC-03 | `search session '表格'`（2 個字） | **比得到** cjk（走 T1 的退路） | T1 |
| U-SRC-04 | `search session '表'`（1 個字） | 比得到（同一個退路），或者回一個明確的「關鍵字太短」錯誤。兩者擇一，寫進 design，不可以靜默回傳 0 筆 | T1 |
| U-SRC-05 | `search session '変換する'`、`'テーブル'` | 都比得到 cjk | 5.1 |
| U-SRC-06 | `search session 'ＣＳＶ'`（全形） | 比得到內文是半形 `CSV` 的 Session | T2 |
| U-SRC-07 | 依序搜尋 `C++`、`a"b`、`AND`、`NEAR`、`*`、`-x`、`表格 OR` | 每一個都不會丟例外，都當成字面字串比對 | 5.1 |
| U-SRC-08 | 搜尋 oc-basic 裡的工具名稱；再搜尋 `ZZTOOLOUT`、`ZZTHINK` | 工具名稱比得到；後面兩個都是 0 筆 | 0.1、L1 |
| U-SRC-09 | `search session 'CSV' --header agent=claude`；`--header relation=merge`；`--header case=mybrain:案件/x` | 只回傳同時符合關鍵字和 header 條件的 Session | 5.1、H6 |
| U-SRC-10 | 刪掉 `index.sqlite` 後再搜尋 | 從鏡像重建索引，結果和刪掉之前相同 | 第 4 節 |
| U-SRC-11 | 遠端刪掉一個 Session 之後 sync，再搜尋 | 找不到那個 Session | S5 |

## (b) 整合測試（只用 Drive 的 agora-test/、自編短對話）

### 安全規則（每次執行都要遵守）
- 每次執行前先 `mkdir -p /tmp/agora-it-<run>/{proj,proj2}`，兩個資料夾**各自** `git init && git commit --allow-empty -m init`。所有 opencode 指令只在這兩個資料夾裡跑。
- Drive 的根目錄設成 `gdrive:agora-test/it-<run>/`（`--config ~/.config/agora/rclone.conf`）。不准碰 `agora-test/` 以外的地方。rclone.conf 不准印出，也不准貼出來。
- 只用 opencode zen 的免費模型（`opencode/space-bunny-free`、`opencode/muse-spark-1.3-contributor-free`），以及 `claude -p`。prompt 都是自編的短句子。
- 清理時，只用這次執行記下來的 id **一個一個**刪：opencode session、`~/.claude/projects/<編碼後的 /private/tmp/agora-it-<run>/...>/` 底下的檔案（只刪自己建的 uuid），以及 `agora-test/it-<run>/`。不准批次刪除，也不准列出其他的 session。注意 macOS 的 `/tmp` 實際路徑是 `/private/tmp`，Claude 會用後者來編碼專案路徑。
- 檢查結果時只比對形狀與欄位（行數、md5、欄位是否存在、有沒有某個自編的關鍵字），不把對話內容寫進 log。
- 每一步都 `nohup` 到檔案，單一步驟不超過 90 秒，會等 agent 的步驟要設 timeout。

### 呼叫額度
| 測試 | `claude -p` 次數 | opencode 免費模型次數 |
|---|---|---|
| I-02 | 0 | 1 |
| I-03 | 1 | 0 |
| I-04 | 0 | 1 |
| I-05 | 1 | 0 |
| I-06 | 1 | 1 |
| I-07 | 0 | 1 |
| I-10 | 0 | 1 |
| I-12 | 0 | 1 |
| **每跑一次的合計** | **3** | **6** |

整晚最多跑 8 次，也就是 `claude -p` ≤ 24 次（符合「幾十次以內」）。其他測試都重複使用這些 session，不會多呼叫模型。

### 測試項目

| ID | 前置 | 步驟 | 預期 | 對應 |
|---|---|---|---|---|
| I-01 | 空的 `agora-test/it-<run>/` | `agora sync`（第一次）；用 rclone 從外部放一份 session.md 和 raw；再 `sync` | 建好根資料夾，並把 folder ID 寫進 config；第二次只下載新的那一份 session.md；`rclone lsjson --hash` 拿得到 md5 | V4、S8、L2 |
| I-02 | proj | `opencode run -m <免費模型> '把 CSV 轉成 Markdown 表格，先列三個步驟'`，記下 session id；`agora import --format opencode --session-id <id>`；`show`；`search 'CSV'`、`'表格'`、`'步驟'` | 印出 agora id；Drive 上有 session.md 和 `raw-<md5>.json`，而且 md5 相符；三個搜尋都有結果（`表格`、`步驟` 是二字詞，測的是 T1）；閱讀版沒有工具結果 | 5.2、S1、T1、0.1 |
| I-03 | proj | 在 proj 裡執行 `claude -p --session-id <uuid> 'CSV を Markdown の表に変換する手順を三つ挙げて'`；`agora import --format claude --session-id <uuid>`；`search '変換'`、`'手順'` | 匯入成功；如果有附屬資料夾，raw 裡的 `aux` 也有（只記檔名）；兩個搜尋都有結果 | 5.2、S9、T1 |
| I-04 | I-02 的 A；先 export A 原本的 opencode session，記下 message 數與 md5 | `AGORA_OPENCODE_CMD='opencode run -m <免費模型> --session {id} 續：把第二步寫成程式碼'`，執行 `continue-session A --agent opencode --dir proj`；再 export 一次原本的 session | 原本那個 session 的 message 數和 md5 都沒變；新的 agora Session 的 `parents[0]` 是 A，閱讀版的 message 比 A 多 | S4、V1 |
| I-05 | I-03 的 B；記下原本 jsonl 的行數與 md5 | `AGORA_CLAUDE_CMD='claude -p --resume {id} 續：把第一步寫成指令'`，執行 `continue-session B --agent claude` | 原本那個 jsonl 沒有變；新 uuid 的 jsonl 在編碼後的 proj 底下，而且行數變多；新的 agora Session 的 `parents[0]` 是 B | S9、H1、V2 |
| I-06 | A（opencode）、B（claude） | `continue-session A --agent claude`（1 次 `claude -p`）；`continue-session B --agent opencode`（1 次 opencode） | 兩邊都是用閱讀版注入；回覆裡出現自編內容的關鍵字（例如「CSV」）。只做寬鬆的比對 | 5.4、V3 |
| I-07 | A、B | `merge-session A B`；`continue-session <M> --agent opencode` | M 的 `parents` 是 [A, B]，raw 很小；continue 用的是閱讀版注入，而且存成新的 Session | 5.3、L3 |
| I-08 | proj 裡已經有一個自編的 opencode session（可以重複使用 I-02 的） | 用 cache 1 加上 `AGORA_TEST_FAULT=after-raw-upload` 匯入；用 cache 2 `sync` 再 `search`；用 cache 1（不加 fault）`sync`；用 cache 2 再 `sync` 再 `search` | 第一次 cache 2 搜不到，也沒有錯誤；cache 1 的 sync 把 outbox 推上去；第二次 cache 2 就搜得到 | S1、S2 |
| I-09 | 用 `AGORA_RCLONE` 包一層，讓真的 rclone 在第 1 次 copyto 時失敗 | 匯入 → 拿掉那一層包裝 → `sync` | 第一次 exit ≠ 0，outbox 裡有那一筆；sync 之後 Drive 上的內容齊全，outbox 是空的 | S2 |
| I-10 | A | `AGORA_OPENCODE_CMD` 用 `opencode run --session {id} '再加一個驗證步驟'`；continue 在背景啟動，3 秒後 `kill -9` agora 的行程；等 agent 結束（timeout 80 秒）後執行 `agora sync` | sync 時把 pending 補存成新的 continue Session，`parents` 是 A；pending 是空的 | S3 |
| I-11 | 兩組 cache（cache 1、cache 2），同一個 Drive 根目錄 | cache 2 從來沒 sync 過，直接匯入 I-02 的來源；用 cache 1 `rclone purge` 自己建的某一個測試 ULID 資料夾；cache 2 `sync` 後 `search` | 匯入印出的 id 和 I-02 的相同；被刪掉的那一筆在 cache 2 裡搜不到 | S6、S5 |
| I-12 | proj2（另一個 git 專案） | `continue-session A --agent opencode --dir proj2`（opencode 1 次）；只在 proj2 裡執行 `opencode session list` | 新的 session 出現在 proj2 的清單裡；agora 新 Session 的 `source.dir` 是 proj2 | H1、V5 |

### 人工確認（不自動化，每個環境只做一次）
| ID | 項目 | 預期 | 對應 |
|---|---|---|---|
| M-01 | worker OAuth client 的 consent screen 狀態（在 GCP console 看，不要印出任何憑證） | 是「In production」；如果是「Testing」，design 要寫明 7 天要重新授權一次 | S8 |
| M-02 | 用 Drive 網頁在 `agora-test/it-<run>/` 底下手動上傳一個檔，然後 `agora sync` | agora 看不到這個檔，也不會出錯（確認 drive.file 的行為，並寫進 design） | S8 |
| M-03 | 互動模式的 continue：真的打開 opencode TUI 和互動式 claude，打一句自編的話，然後用各自的方式離開（包括按 Ctrl-C 離開） | 兩邊都會存回去；按 Ctrl-C 不會讓 agora 先死掉 | S3、5.4 |

## 對照：review 的每一條 High 都有測試

| review 編號 | 單元測試 | 整合測試 |
|---|---|---|
| S1 半份寫入 | U-ST-01～05、U-ST-07、U-IMP-07、U-MRG-04、U-CON-14 | I-02、I-08 |
| S2 上傳失敗重送 | U-ST-06～10、U-CON-13 | I-08、I-09 |
| S3 continue 中途被打斷 | U-CON-08～13 | I-10、M-03 |
| S4 opencode id 改寫 | U-CON-01、U-CON-02 | I-04 |
| H1 工作目錄 | U-HDR-08、U-CON-02、U-CON-04、U-CON-06 | I-05、I-12 |
| T1 中文二字詞 | U-SRC-03、U-SRC-04 | I-02、I-03 |
